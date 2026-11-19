"""The one opener of a managed agent's session, used by every input source, and the agent's on/off state.

A managed session closes after 15 idle minutes and is opened again by the next input. Three words say where an agent stands:

* **asleep** - no host runs for it (never started, idle close, service restart, a crash);
* **off**    - ``native_session_owners.desired_state == 'stopped'``: the owner ended its session, or Ordubashy killed the worker;
* **on**     - anything else.

An asleep-and-on agent is opened by whatever reaches it. An off agent is opened only by the owner's own input (Chat, Telegram,
which also turn it on) or, for a worker, by Ordubashy's start and restart. Idle close, a service restart and a crashed host
change nothing about on/off. ``paused`` (nothing writes it today) is never woken either.

``SessionOpener.open`` is the flow the owner's Chat message always used, generalised: make the host ready
(``ManagedRuntime.ensure_open``, which keeps the limit of concurrent starts and answers ``open_busy`` beyond it), then wait for
the session to be bound, handing the first message to the host (``send_first``) when the session does not announce itself.
Binding cannot appear before the client has taken up a first message, and a resumed session is sent none by its host, so:

* an owner message is its own first message (the arbiter then recognises its uuid and types it once);
* every other input source wakes the agent with a short notice (``WAKE_TEXT``) as that first message and then delivers its input
  the ordinary way, through its own record: the nudge engine's attempt, the reminder claim, the Telegram queue, the broadcast
  delivery. The notice carries no input and no secret, so a failed open leaves every input exactly as it was, and an input is
  delivered once, by its own claim, never by the wake.

One open per agent at a time (``_flights``); an owner or a synchronous caller waits for the open in progress and then finds the
session bound, any other source is told it is already waking. Non-owner sources are paced (``_cooldown``): nothing retries faster
than the source's own wait/retry loop, and a failing open backs off up to ``MAX_BACKOFF_SECONDS``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
import time
import uuid

from qorqut_operations.contracts import DomainError

LOG = logging.getLogger(__name__)

# Starts with an opening tag, so Chat shows it as an internal notice and not as something the owner said.
WAKE_TEXT = ('<qorqut-wake>\nQorqut started this session because input is waiting for you. It is delivered next; '
             'handle it when it arrives. This notice needs no reply.\n</qorqut-wake>')
OFF_STATES = frozenset({"stopped"})
# What a source never wakes: off, and paused (a recorded intent not to run).
NOT_WAKEABLE = frozenset({"stopped", "paused"})
# Open failures that repeat until someone changes something: what ``agents/unreachable.py`` tells Ordubashy about.
REPEATING = frozenset({"cli_unavailable", "resume_failed", "resume_unavailable", "driver_unavailable"})
# A host that is merely busy, starting or slow: asked again soon, never an alarm.
TRANSIENT = frozenset({"open_busy", "session_opening", "open_timeout", "host_unverified", "session_starting", "session_closed",
                       "host_rejected", "host_busy"})
# What a terminal-mode agent (stage 1 only) answers: nothing to open here.
NOT_OPENABLE = frozenset({"not_managed", "terminal_present", "managed_unavailable"})
SPACING_SECONDS = 10.0
TRANSIENT_SECONDS = 5.0
BACKOFF_SECONDS = (30.0, 60.0, 120.0, 300.0)
MAX_BACKOFF_SECONDS = 600.0
# A quiet pause after an open in progress or a busy host: the callers' own loops (3 s pass, Telegram backoff) are not faster.
SESSION_OPENING = ("session_opening", "A session is being opened for this agent; try again in a moment")


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class OpenFailures:
    """The streak of failed opens per agent, for ``agents/unreachable.py``: ``(code, since, count)`` while the last opens failed
    with one of the ``REPEATING`` codes. Memory only; a service restart starts the count again."""

    def __init__(self, clock=_utc):
        self._clock, self._guard, self._streaks = clock, threading.Lock(), {}

    def failed(self, agent_id: str, code: str) -> None:
        with self._guard:
            current = self._streaks.get(agent_id)
            if code not in REPEATING:
                self._streaks.pop(agent_id, None)
                return
            if current is None or current[0] != code:
                self._streaks[agent_id] = (code, self._clock(), 1)
            else:
                self._streaks[agent_id] = (code, current[1], current[2] + 1)

    def opened(self, agent_id: str) -> None:
        with self._guard:
            self._streaks.pop(agent_id, None)

    def streak(self, agent_id: str):
        with self._guard:
            return self._streaks.get(agent_id)


FAILURES = OpenFailures()


class SessionOpener:
    """See the module text. ``managed_of()`` returns the ``ManagedRuntime`` (or None while it is not composed). ``probe``, ``registry`` and
    ``locks`` are the native Chat's own; ``wake`` nudges the backend observer; ``adopt(profile)`` lets Chat adopt the new
    conversation; ``limits()`` returns ``(total, poll, role_grace, record)`` seconds read late (tests change them);
    ``failure(error)`` turns a manager error into the refusal callers show (default: unchanged)."""

    def __init__(self, runtime, *, managed_of, probe, registry, locks, wake, adopt, limits, failure=None, failures=FAILURES,
                 clock=time.monotonic, sleep=time.sleep, spawn=None):
        self.runtime, self._managed_of, self.probe, self.registry, self.locks = runtime, managed_of, probe, registry, locks
        self._wake_backend, self._adopt, self._limits = wake, adopt, limits
        self._failure = failure or (lambda error: error)
        self.failures, self._clock, self._sleep = failures, clock, sleep
        self._spawn = spawn or self._thread
        self._guard = threading.Lock()
        self._flights: dict[str, threading.Lock] = {}
        self._cool: dict[str, tuple[float, str]] = {}
        self._streak: dict[str, int] = {}
        self._episodes: dict[str, str] = {}
        # The pinless owner message that last opened (or tried to open) each agent: ``agent_id -> (key, expires)``. A retry of it,
        # after the open gave up while the host kept starting, is admitted for the session that has come up.
        self._opens: dict[str, tuple[str, float]] = {}

    @property
    def database(self):
        return self.runtime.database

    @property
    def managed(self):
        """The managed runtime, read late: it may be composed or replaced after the opener was built."""
        return self._managed_of()

    # ----- on / off ---------------------------------------------------------------------------------------------------

    def desired(self, agent_id: str) -> str | None:
        """``native_session_owners.desired_state`` of this agent, or None when it never bound a session (it is then on)."""
        with self.database.read() as uow:
            row = uow.connection.execute("SELECT desired_state FROM native_session_owners WHERE agent_id=?", (agent_id,)).fetchone()
        return row["desired_state"] if row is not None else None

    def is_off(self, agent_id: str) -> bool:
        return self.desired(agent_id) in OFF_STATES

    def wakeable(self, agent_id: str) -> bool:
        """Whether a non-owner input may open this agent's session: managed runtime composed, and not off or paused."""
        return self.managed is not None and self.desired(agent_id) not in NOT_WAKEABLE

    def set_desired(self, agent_id: str, state: str) -> bool:
        """Record the intent durably (the Chat view moves with it). False when there is no owner row to write: an agent that never
        bound a session has nothing recorded and is on."""
        from . import store
        with self.database.read() as uow:
            row = uow.connection.execute("SELECT desired_state FROM native_session_owners WHERE agent_id=?", (agent_id,)).fetchone()
        if row is None:
            return False
        if row["desired_state"] == state:
            return True
        with self.database.transaction(operation="native_chat.desired_state") as uow:
            self.runtime.application.ensure_writes_open(uow)
            uow.connection.execute("UPDATE native_session_owners SET desired_state=? WHERE agent_id=?", (state, agent_id))
            store.bump(uow.connection, agent_id)
        return True

    def turn_on(self, agent_id: str) -> bool:
        return self.set_desired(agent_id, "running")

    def turn_off(self, agent_id: str) -> bool:
        return self.set_desired(agent_id, "stopped")

    # ----- the owner's own opens --------------------------------------------------------------------------------------

    def opened_by(self, agent_id: str, key: str) -> bool:
        with self._guard:
            found = self._opens.get(agent_id)
            return found is not None and found[0] == key and found[1] > self._clock()

    def open(self, profile: dict, text: str, key: str, new_conversation: bool = False) -> str:
        """The owner's message opens the agent: its session is made ready and bound, the message handed to the host as its first
        message (the admitted input carries the same uuid, so it is typed once). Returns the binding id the message is admitted for;
        ``DomainError`` with the refusal code when it cannot be opened. Turns the agent on. Waits for an open in progress."""
        agent_id = profile["agent_id"]
        if self.managed is None:
            raise self._failure(DomainError("driver_unavailable", "The session could not be started"))
        with self._guard:
            self._opens[agent_id] = (key, self._clock() + self._limits()[3])
        self.turn_on(agent_id)
        total = self._limits()[0]
        deadline = self._clock() + total
        with self._flight(agent_id, deadline, "open_timeout"):
            try:
                found = self._bound(profile, text, key, new_conversation, deadline, total)
            except DomainError as error:
                self._failed(agent_id, error)
                raise self._failure(error) from None
        self._opened(agent_id, space=False)
        return found

    # ----- every other source -----------------------------------------------------------------------------------------

    def wake(self, profile: dict, source: str, *, wait: bool = False, force: bool = False) -> str:
        """Open an asleep-and-on agent for an input that is not the owner's Chat message.

        ``"live"`` when its session is bound (``wait=True`` waits for the open, an open in progress included, within the usual
        45 seconds); ``"waking"`` when an open runs or was started in the background (``wait=False``: nothing here blocks the
        caller, the source asks again on its own schedule). ``DomainError`` (``retryable``) when it must not or cannot be opened now:
        off, paced, busy or failed; the code is the reason. The input itself is never carried or marked here.

        ``force`` is Ordubashy's start and restart of a worker: neither the off state nor the pacing holds it back (it has just
        turned the agent on), and a failure is still recorded."""
        agent_id = profile["agent_id"]
        if self.managed is None:
            raise DomainError("session_closed", "This agent's session is not open")
        if not force:
            if self.desired(agent_id) in NOT_WAKEABLE:
                raise DomainError("session_closed", "This agent is turned off")
            self._paced(agent_id)
        if wait:
            total = self._limits()[0]
            deadline = self._clock() + total
            with self._flight(agent_id, deadline, SESSION_OPENING[0]):
                return self._woken(profile, source, deadline, total)
        lock = self._lock(agent_id)
        if not lock.acquire(blocking=False):
            return "waking"
        try:
            self._spawn(self._background, profile, source, lock)
        except BaseException:
            lock.release()
            raise
        return "waking"

    def wake_quietly(self, profile: dict, source: str) -> None:
        """``wake`` in the background for a source that has no result to give (reminders, overseer, Ordubashy's inbox): whatever
        stops it is already recorded for pacing and unreachable alerts."""
        try:
            self.wake(profile, source)
        except DomainError:
            LOG.debug("Wake of %s for %s deferred", profile.get("agent_id"), source, exc_info=True)

    def _background(self, profile, source, lock) -> None:
        try:
            deadline = self._clock() + self._limits()[0]
            self._woken(profile, source, deadline, self._limits()[0])
        except DomainError:
            LOG.debug("Background wake of %s for %s failed", profile.get("agent_id"), source, exc_info=True)
        except Exception:  # noqa: BLE001 - nothing may escape a thread of the service
            LOG.warning("Background wake of %s for %s failed", profile.get("agent_id"), source, exc_info=True)
        finally:
            lock.release()

    @staticmethod
    def _thread(target, *args) -> None:
        threading.Thread(target=target, args=args, name="managed-wake", daemon=True).start()

    def _woken(self, profile, source, deadline, total) -> str:
        """Under the agent's flight: the session is already bound (another open finished first), or it is opened now."""
        agent_id = profile["agent_id"]
        try:
            if self._live(profile) is not None:
                self._opened(agent_id)
                return "live"
            with self._guard:
                episode = self._episodes.setdefault(agent_id, uuid.uuid4().hex)
            self._bound(profile, WAKE_TEXT, f"wake:{episode}", False, deadline, total)
        except DomainError as error:
            self._failed(agent_id, error)
            raise self._failure(error) from None
        except Exception:  # noqa: BLE001 - an unexpected failure is paced like any other, never retried at once
            LOG.warning("Opening the session of %s failed unexpectedly", agent_id, exc_info=True)
            error = DomainError("driver_unavailable", "The session could not be started")
            self._failed(agent_id, error)
            raise self._failure(error) from None
        self._opened(agent_id)
        return "live"

    # ----- the open itself --------------------------------------------------------------------------------------------

    def _bound(self, profile, text, key, new_conversation, deadline, total) -> str:
        """Make the host ready, then wait for the session to bind; the first message goes to the host when the fresh session's own
        ``/role`` did not make it announce itself within the grace (a resumed session is sent none). Raises the manager's error."""
        agent_id = profile["agent_id"]
        _, poll, grace, _ = self._limits()
        found = self._live(profile)
        if found is not None:
            return found
        opened = self.managed.ensure_open(profile, timeout=total, new_conversation=new_conversation)
        message_uuid = self.managed.first_message_uuid(agent_id, key, text)
        role_pending = bool((opened or {}).get("role_pending"))
        deliver_at, delivered = self._clock() + (grace if role_pending else 0.0), False
        while True:
            self._wake_backend()
            try:
                self._adopt(profile)
            except Exception:  # noqa: BLE001 - the observer pass retries; adoption is not this call's failure
                LOG.debug("Adoption of the new session of %s deferred", agent_id, exc_info=True)
            found = self._live(profile)
            if found is not None:
                return found
            now = self._clock()
            if not delivered and now >= deliver_at:
                self.managed.send_first(agent_id, text, message_uuid, deadline)
                delivered = True
            if now >= deadline:
                raise DomainError("open_timeout", "The session did not become ready in time", retryable=True)
            self._sleep(poll)

    def _live(self, profile) -> str | None:
        """The binding id of the agent's session when it is bound with a transcript Chat can show, else None."""
        with self.locks(profile["agent_id"]):
            observed = self.probe.observe(profile)
            reconciled = self.registry.reconcile(profile, observed)
        if reconciled.live and reconciled.session is not None:
            capture = observed.observation is not None and observed.observation.capture_enabled
            # The transcript path the session row carries is where the file will be, not a file that exists yet.
            if reconciled.session["transcript_path"] or not capture:
                return reconciled.session["id"]
        return None

    # ----- one open at a time, pacing ---------------------------------------------------------------------------------

    def _lock(self, agent_id: str) -> threading.Lock:
        with self._guard:
            return self._flights.setdefault(agent_id, threading.Lock())

    def _flight(self, agent_id: str, deadline: float, error: str):
        opener = self

        class Flight:
            def __enter__(self):
                lock = opener._lock(agent_id)
                while not lock.acquire(blocking=False):
                    if opener._clock() >= deadline:
                        raise DomainError(error, "A session is being opened for this agent; try again in a moment", retryable=True)
                    opener._sleep(opener._limits()[1])
                self.lock = lock

            def __exit__(self, *exc):
                self.lock.release()
        return Flight()

    def _paced(self, agent_id: str) -> None:
        with self._guard:
            held = self._cool.get(agent_id)
        if held is not None and self._clock() < held[0]:
            raise DomainError(held[1], "This agent's session could not be opened a moment ago; it is tried again shortly", retryable=True)

    def _failed(self, agent_id: str, error: DomainError) -> None:
        code = getattr(error, "code", "") or ""
        if code in TRANSIENT:
            delay = TRANSIENT_SECONDS
        elif code in NOT_OPENABLE:
            delay = MAX_BACKOFF_SECONDS
        else:
            with self._guard:
                step = self._streak.get(agent_id, 0)
                self._streak[agent_id] = step + 1
            delay = BACKOFF_SECONDS[step] if step < len(BACKOFF_SECONDS) else MAX_BACKOFF_SECONDS
        with self._guard:
            self._cool[agent_id] = (self._clock() + delay, code)
        if code not in NOT_OPENABLE:
            self.failures.failed(agent_id, code)

    def _opened(self, agent_id: str, *, space: bool = True) -> None:
        """``space``: a wake that succeeded is not repeated within ``SPACING_SECONDS`` (an input that keeps closing its session again
        cannot spin); the owner's own open is not a wake and starts no spacing."""
        with self._guard:
            self._streak.pop(agent_id, None)
            self._episodes.pop(agent_id, None)
            if space:
                self._cool[agent_id] = (self._clock() + SPACING_SECONDS, "session_starting")
            else:
                self._cool.pop(agent_id, None)
        self.failures.opened(agent_id)


def request_wake(runtime, agent_id: str, source: str) -> bool:
    """For a source that only has a runtime in hand (overseer, reminders): start waking this agent in the background when it is
    asleep and on. Never raises, never blocks. True when a wake was requested."""
    try:
        native = getattr(runtime, "native_chat", None)
        opener = getattr(native, "opener", None)
        if opener is None or not opener.wakeable(agent_id):
            return False
        with runtime.database.read() as uow:
            from . import store
            profile = store.active_profile(uow.connection, agent_id, runtime.paths)
        if profile is None:
            return False
        opener.wake_quietly(profile, source)
        return True
    except Exception:  # noqa: BLE001
        LOG.debug("Wake request of %s for %s failed", agent_id, source, exc_info=True)
        return False


__all__ = ["FAILURES", "NOT_WAKEABLE", "OFF_STATES", "OpenFailures", "REPEATING", "SessionOpener", "WAKE_TEXT", "request_wake"]
