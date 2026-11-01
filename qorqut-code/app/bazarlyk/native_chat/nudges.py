"""The inbox nudge backend: an advisory to a running native session that it has pending, authorized inbox events.

The engine (``qorqut_operations.inbox_nudges``) keeps the durable need, the immutable attempts and the rearm rule. This module
is the host side of it and nothing else:

* ``HostVisibility`` answers, from the inbox's own current authorization, which events an agent may see right now.
* ``NudgeBackend`` is one bounded thread, independent of any Chat view. Each pass it finds the agents that have pending events,
  turns the verified native binding and the progress the host has really seen into the engine's typed values, reserves at most
  one notice per agent, sends it through the one input arbiter (``admit_nudge``: system origin, ``inbox_nudge`` marker, the
  reserved exact binding) and settles the attempt with exactly what the transport reported.

Nothing here starts, resumes or stops an agent, acknowledges an event, or changes a Task, Action, Signoff or routine. An offline
agent keeps its pending need and is nudged only after someone starts a session by hand. ``submitted`` is the transport's report,
never proof that the agent read its inbox.

The drain is bounded: ``stop`` joins the thread within its timeout and raises ``TimeoutError`` when it survives, keeping the
reference so a later stop can still prove the drain.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Callable

from qorqut_operations.contracts import DomainError, Principal
from qorqut_operations.inbox_nudges import (Binding, InboxNudges, Observation as Progress, Outcome, Reservation, Waiting,
                                            source_key)

from . import store
from .arbiter import StaleSession
from .contracts import (InputResult, RESULT_DEFERRED, RESULT_REJECTED, STATE_FAILED, STATE_SUBMITTED, STATE_SUBMITTING,
                        STATE_UNCERTAIN)

LOG = logging.getLogger(__name__)
PASS_SECONDS = 3.0
REVALIDATE_SECONDS = 30.0
MAX_RECEIPT_KEYS = 12
# Reasons the transport gives when it can never type this exact session: the nudge is visibly unavailable, never retried at once.
UNAVAILABLE_REASONS = frozenset({"unsupported_transport", "unsupported_target", "unsupported_runtime", "capture_disabled",
                                 "terminal_unavailable"})
# A session that changed or an agent that is no longer registered: the reservation is stale, not a failed delivery.
STALE_REASONS = frozenset({"stale_session", "agent_unavailable"})
# Waiting verdicts that cannot change until the pending set, the binding or a progress counter changes.
STEADY = frozenset({"coalesced", "awaiting_progress", "uncertain_unreconciled", "offline"})
RECOVERY_TEXT = ("The session helper has refreshed your instructions. The trusted hook delivers them with this notice. "
                 "Continue the owner's authorized work using the hook's current status; no startup command or status reply is needed.")
RECOVERY_KIND = "bootstrap_recovery"


class HostVisibility:
    """The engine's authorization port, answered by the inbox itself: the agent's own invocation context, the ``inbox_list``
    policy, and ``event_visible`` per event. An event whose authorization cannot be proven discloses nothing."""

    def __init__(self, inbox):
        self.inbox = inbox
        self._cached: tuple | None = None

    def _context(self, uow, agent_id: str):
        cached = self._cached
        if cached is not None and cached[0] is uow and cached[1] == agent_id:
            return cached[2]
        inbox = self.inbox
        try:
            ctx = inbox.context(inbox.resolver.invocation_context(Principal(agent_id, "agent")), uow)
            descriptor = inbox.app.registry.get("inbox_list")
            if ctx.business_id is not None:
                descriptor = replace(descriptor, global_capability=None)
            if inbox.app.policy.resolve(ctx, descriptor, (), uow).mode != "allow":
                ctx = None
        except DomainError:
            ctx = None
        self._cached = (uow, agent_id, ctx)
        return ctx

    def authorized(self, uow, agent_id: str, events) -> set[int]:
        ctx = self._context(uow, agent_id)
        if ctx is None:
            return set()
        granted: set[int] = set()
        for row in events:
            try:
                if self.inbox.event_visible(ctx, row, uow):
                    granted.add(row["id"])
            except DomainError:
                LOG.debug("Inbox event %s is not provably visible to %s", row["id"], agent_id, exc_info=True)
        return granted


def _scalar(value) -> bool:
    return isinstance(value, (bool, int)) or (isinstance(value, str) and len(value) <= 120)


def receipt_of(result: InputResult, binding: Binding) -> dict:
    """The exact, bounded receipt of one transport result: the native input and the report's plain facts. It never carries the
    typed text, a screen or a terminal identity (the arbiter's evidence does not either)."""
    facts = {key: value for key, value in dict(result.evidence).items()
             if isinstance(key, str) and len(key) <= 40 and _scalar(value) and key not in {"binding", "effect"}}
    return {"input_id": result.input_id, "input_state": result.state, "marker_recorded": bool(result.marker),
            "transport": dict(list(sorted(facts.items()))[:MAX_RECEIPT_KEYS]), "binding": binding.public()}


def outcome_of(result: InputResult, binding: Binding) -> Outcome | str:
    """Map the arbiter's answer to the engine's outcome, or to the reason the reservation is stale (nothing was typed)."""
    reason = result.reason or result.state
    receipt = receipt_of(result, binding)
    if result.state == STATE_SUBMITTED:
        return Outcome("submitted", reason, receipt)
    if result.state == STATE_UNCERTAIN:
        return Outcome("uncertain", reason, receipt)
    if result.state == STATE_FAILED:
        return Outcome("failed", reason, receipt)
    if result.state in {RESULT_DEFERRED, RESULT_REJECTED}:
        if reason in STALE_REASONS:
            return reason
        base = reason.removeprefix("not_typed:")
        return Outcome("unavailable" if base in UNAVAILABLE_REASONS else "deferred", reason, receipt)
    # A state this code does not know: it may have reached the session, so it is never assumed untyped.
    return Outcome("uncertain", f"unexpected_result:{result.state}"[:200], receipt)


def binding_of(session: dict) -> Binding:
    return Binding(session["id"], session["agent_id"], session["provider"], session["conversation_id"], session["generation"],
                   session["transport"], session["process_pid"], session["process_started"])


def _moment(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


class NudgeBackend:
    """``reconcile`` is the one optional hook run first in every pass inside its own write transaction (the later combined host
    passes the accepted Task snooze reconciliation here). A failing hook never blocks nudges."""

    def __init__(self, runtime, engine: InboxNudges, probe, registry, arbiter, locks, *, interval: float = PASS_SECONDS,
                 reconcile: Callable[[object], object] | None = None, monotonic: Callable[[], float] = time.monotonic):
        self.runtime, self.engine, self.probe, self.registry, self.arbiter, self.locks = runtime, engine, probe, registry, arbiter, locks
        self.interval, self.reconcile, self._monotonic = interval, reconcile, monotonic
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._memo: dict[str, tuple] = {}

    @classmethod
    def build(cls, runtime, inbox, *, interval: float = PASS_SECONDS, reconcile=None) -> "NudgeBackend":
        """The default graph of one host runtime: the engine over the inbox's authorization, the native Chat's own probe,
        registry, arbiter and per-agent locks (the same serialization every native input uses)."""
        chat = runtime.native_chat
        return cls(runtime, InboxNudges(runtime.database, HostVisibility(inbox)), chat.probe, chat.registry, chat.arbiter,
                   chat.arbiter.locks, interval=interval, reconcile=reconcile)

    @property
    def database(self):
        return self.runtime.database

    # ----- lifetime --------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch, name="inbox-nudge-backend", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        """Bounded drain. A surviving thread is an unproved drain: the reference is kept so a later stop can prove it."""
        self._stop_event.set()
        self._wake.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("Inbox nudge backend is still draining")
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _watch(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                LOG.debug("Inbox nudge backend paused", exc_info=True)
            self._wake.wait(self.interval)
            self._wake.clear()

    # ----- one pass --------------------------------------------------------------------------------------------------

    def run_once(self) -> None:
        """One complete pass; also the synchronous entry synthetic checks use."""
        self.runtime.require_authority()
        self._run_hook()
        self.reconcile_reservations()
        with self.database.read() as uow:
            agents = store.observed_agents(uow.connection, self.runtime.paths)
        due = set(self.engine.candidates([profile["agent_id"] for profile in agents]))
        for profile in agents:
            if self._stop_event.is_set():
                return
            if profile["agent_id"] not in due:
                self._memo.pop(profile["agent_id"], None)
                continue
            try:
                self.step(profile)
            except DomainError:
                raise
            except Exception:
                LOG.debug("Inbox nudge deferred for %s", profile["agent_id"], exc_info=True)

    def _run_hook(self) -> None:
        if self.reconcile is None:
            return
        try:
            # A snooze reconciliation writes events, so it is an admitted writer: the actual write admission first (a pass
            # begun before refusal settles, one begun after it writes nothing), the SQLite transaction inside it.
            with self.runtime.application.writer_admission.writer():
                with self.database.transaction(operation="inbox_nudges.reconcile") as uow:
                    self.runtime.application.ensure_writes_open(uow)
                    self.reconcile(uow)
        except DomainError as error:
            if error.code in {"authority_invalid", "maintenance"}:
                raise
            LOG.warning("Inbox nudge reconcile hook failed: %s", error.code)
        except Exception:
            LOG.warning("Inbox nudge reconcile hook failed", exc_info=True)

    # ----- restart reconciliation ------------------------------------------------------------------------------------

    def reconcile_reservations(self) -> None:
        """Settle reservations a crash or an exception left open, from the native input record only.

        A reservation with no native input was never claimed and is cancelled; it is never typed later. One with an input is
        settled with that input's final state. A claim still being recovered (``submitting``) waits for the native backend's own
        restart recovery, which turns it uncertain; nothing is ever sent twice."""
        for attempt in self.engine.unsettled(states=("prepared",)):
            agent_id = attempt["agent_id"]
            with self.locks(agent_id):
                with self.database.read() as uow:
                    row = store.input_by_key(uow.connection, agent_id, attempt["source_key"])
                binding = Binding.of(attempt["binding"])
                if row is None:
                    self.engine.cancel(attempt["attempt_id"], "unclaimed")
                    continue
                if row["state"] == STATE_SUBMITTING:
                    continue
                result = InputResult(state=row["state"], input_id=row["id"], reason=row["reason"])
                outcome = outcome_of(result, binding)
                if row["session_id"] != binding.binding_id:
                    # An input for another session than the reservation's can only have been typed there: never assume less.
                    outcome = Outcome("uncertain", "input_session_differs", receipt_of(result, binding))
                if isinstance(outcome, Outcome):
                    self.engine.settle(attempt["attempt_id"], outcome)
                else:
                    self.engine.cancel(attempt["attempt_id"], outcome)

    # ----- one agent -------------------------------------------------------------------------------------------------

    def _fingerprint(self, conn, agent_id: str):
        session = store.live_session(conn, agent_id)
        counters = store.counters(conn, agent_id)
        pending = conn.execute("SELECT COUNT(*),COALESCE(MAX(event_id),0) FROM inbox_deliveries WHERE agent_id=? AND acknowledged_at IS NULL",
                               (agent_id,)).fetchone()
        return (session["id"] if session else None, counters["work_sequence"], counters["user_sequence"], pending[0], pending[1]), session, counters

    def _quiet(self, agent_id: str, fingerprint) -> bool:
        memo = self._memo.get(agent_id)
        if memo is None or memo[0] != fingerprint or self._monotonic() - memo[2] >= REVALIDATE_SECONDS:
            return False
        reason, not_before = memo[1], memo[3]
        if not_before is not None:
            return _moment(self.engine.now()) < not_before
        return reason in STEADY

    def step(self, profile: dict) -> str:
        """Reserve and send at most one notice for this agent now. Returns a stable code for what happened."""
        agent_id = profile["agent_id"]
        with self.locks(agent_id):
            with self.database.read() as uow:
                fingerprint, session, counters = self._fingerprint(uow.connection, agent_id)
                chat_waiting = bool(store.waiting_inputs(uow.connection, agent_id))
            if chat_waiting:
                return "chat_input_first"  # a direct Chat input is ahead of any advisory; user work keeps its priority
            if self._quiet(agent_id, fingerprint):
                return "quiet"
            binding, working = None, False
            if session is not None:
                observed = self.probe.observe(profile)
                reconciled = self.registry.reconcile(profile, observed)
                if reconciled.session is not None and reconciled.live:
                    session, binding = reconciled.session, binding_of(reconciled.session)
                    working = bool(observed.observation and observed.observation.working)
            progress = Progress(work=counters["work_sequence"], user=counters["user_sequence"], working=working)
            verdict = self.engine.prepare(agent_id, binding, progress)
            if isinstance(verdict, Waiting):
                self._memo[agent_id] = (fingerprint, verdict.reason, self._monotonic(),
                                        _moment(verdict.not_before) if verdict.not_before else None)
                return verdict.reason
            self._memo.pop(agent_id, None)
            return self._send(session, verdict)

    def _send(self, session: dict, reservation: Reservation) -> str:
        stale: dict[str, bool] = {}

        def checkpoint() -> None:
            # The freshest check there is: the exact binding and some covered event still pending and authorized, under the
            # arbiter's per-agent serialization, immediately before the durable claim and the first input.
            if not self.engine.validate(reservation.attempt_id, reservation.binding):
                stale["now"] = True
                raise StaleSession()

        result = self.arbiter.admit_nudge(
            reservation.agent_id, reservation.source_key, reservation.text, expected_binding=session["id"],
            metadata={"nudge_attempt": reservation.attempt_id, "nudge_kind": reservation.kind}, on_checkpoint=checkpoint)
        if stale:
            return "stale"
        outcome = outcome_of(result, reservation.binding)
        if isinstance(outcome, Outcome):
            self.engine.settle(reservation.attempt_id, outcome)
            return outcome.state
        self.engine.cancel(reservation.attempt_id, outcome)
        return "stale"

    # ----- bootstrap recovery ----------------------------------------------------------------------------------------

    def recovery_notice(self, agent_id: str, episode_id: str, *, consumer_id: str) -> dict:
        """The retained bootstrap context-recovery wake, through the same arbiter, as its own system input type
        (``bootstrap_recovery``, never ``inbox_nudge``). It is unrelated to inbox events: it covers none, acknowledges nothing and
        neither spends nor rearms an inbox notice allowance. The episode id makes it idempotent: a repeat returns the saved result
        and never types twice.

        ``consumer_id`` is required. It is the native conversation of the hook that asked: the wake is pinned to the live session
        that holds exactly that conversation, and any other session (or none) leaves it ``deferred`` for the caller to ask again.
        Nothing here starts or resumes a session."""
        if not isinstance(consumer_id, str) or not consumer_id:
            raise DomainError("validation_error", "A recovery notice names the conversation it is for")
        with self.database.read() as uow:
            live = store.live_session(uow.connection, agent_id)
        if live is None or live["conversation_id"] != consumer_id:
            return {"state": RESULT_DEFERRED, "reason": "session_not_current", "input_id": None, "binding": None}
        result = self.arbiter.admit_recovery(agent_id, f"{RECOVERY_KIND}:{episode_id}", RECOVERY_TEXT, expected_binding=live["id"],
                                             metadata={"episode": episode_id})
        return {"state": result.state, "reason": result.reason, "input_id": result.input_id,
                "binding": result.binding.public() if result.binding else None}

    # ----- diagnostics -----------------------------------------------------------------------------------------------

    def status(self, agent_id: str) -> dict:
        """The operator view: idle, pending, deferred, submitted, uncertain, stale or unavailable, never 'read'. A pending need of
        an agent with no live native session says it is offline and waits for a manual start."""
        view = self.engine.status(agent_id)
        with self.database.read() as uow:
            live = store.live_session(uow.connection, agent_id)
        if view["state"] == "pending" and live is None:
            view = {**view, "reason": "offline"}
        return view


def delivery_view(backend, agent_id: str) -> dict:
    """Where an agent's inbox notice stands, for the owner-facing read models: ``idle``, ``pending``, ``deferred``, ``submitted``,
    ``uncertain``, ``stale`` or ``unavailable``, or ``unknown`` with the reason diagnostics are not available. It says nothing about
    whether the agent read anything and carries no event, count or text. One definition serves the activity list and the owner's
    inbox read."""
    if backend is None:
        return {"state": "unknown", "reason": "nudge_backend_not_running", "last_attempt_at": None}
    try:
        status = backend.status(agent_id)
    except DomainError:
        return {"state": "unknown", "reason": "nudge_status_unavailable", "last_attempt_at": None}
    attempt = status["attempts"][0] if status.get("attempts") else None
    return {"state": status["state"], "reason": status.get("reason"),
            "last_attempt_at": (attempt["settled_at"] or attempt["prepared_at"]) if attempt else None}


__all__ = ["HostVisibility", "NudgeBackend", "RECOVERY_KIND", "RECOVERY_TEXT", "binding_of", "delivery_view", "outcome_of",
           "receipt_of", "source_key"]
