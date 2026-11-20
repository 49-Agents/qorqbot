"""The owner's switch between managed sessions and terminals, while the service runs.

``RuntimeSwitch(runtime)`` is behind the owner-only route ``/api/root/agents/runtime`` (``view`` for GET, ``start`` for POST). It is
not an operation, an MCP tool or anything an agent can call: only the owner's Settings reach it.

A switch ends every session of the current mode, writes the new mode last (``managed_runtime.mode``), and only after a switch to
Terminals starts a terminal for every agent that is on and has a Claude Code conversation to resume. A service that dies in the
middle is therefore still in the old mode; what was already ended stays ended and starts again by itself (managed) or by the
owner's Start (terminals). Only one switch runs at a time: a lock in this object and a ``flock`` on ``switch.lock`` for another
process. While it runs, ``mode.switching`` is true and every opener of either kind refuses retryably.

Managed to Terminals: each agent with a host is closed gracefully, then at once, then by pid (``managed_runtime.shutdown``'s own
verified force), one agent at a time under the agent's locks in the product's order (writer admission, the managed lifecycle hold,
the arbiter's lock). Terminals to Managed: the cutover tool (``migration/managed_cutover.py``) ends each agent's client and tmux
session, one agent at a time, and the owner's Workspace shells are ended too; a pane it does not understand refuses beforehand.

An agent's folder is prepared again for the new mode by the paths that start its session (``TerminalService.open`` and
``ManagedLaunch.prepare_managed`` both prepare on every start), so the switch itself prepares nothing. Stored model and effort are
never touched here. Every outside effect goes through a seam of the constructor so tests run with fakes.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone

from qorqut_operations.contracts import DomainError

from qorqut import terminal_support
from qorqut.management.identity import installation_owner
from qorqut.managed_runtime import mode

LOGGER = logging.getLogger(__name__)

MANAGED, TERMINAL = mode.MANAGED, mode.TERMINAL
RECOMMENDED = MANAGED
POLL_SECONDS = 0.2
CLOSE_GRACE = 25.0         # a graceful close interrupts a running turn and ends the client: a few seconds, with room to spare
CLOSE_NOW = 10.0
CLOSE_FORCE = 5.0
BUSY_CACHE_SECONDS = 2.0
LOCK_FILE = "switch.lock"

FORBIDDEN = "Only the owner can change how agents run."
SWITCH_RUNNING = "Agents are being switched right now. This takes a minute or two."
UNAVAILABLE = "Qorqbot is busy with a backup or maintenance. Try again in a few minutes."
DONE = {TERMINAL: "Agents now run in terminals.", MANAGED: "Agents now run as managed sessions."}
ABORTED = "Nothing was switched."
CLOSED_BEFORE = " Some agents were closed; they start again on their next message."
ENDED_BEFORE = " Terminals that were already ended stay ended; switch again to finish."
NOT_TRUSTED = ("Claude Code asks once whether to trust each agent's folder, so no agent was started. "
               "Open each agent's Terminal and run claude there.")
UNEXPECTED = "Qorqbot ran into an unexpected problem."
ENDED_REASONS = {
    "turn_in_progress": "it is in the middle of a turn",
    "client_did_not_end": "it did not stop when asked",
    "refused": "its terminal holds something Qorqbot does not recognise",
    "other_processes_in_pane": "its terminal holds another program",
}


class Abort(Exception):
    """A switch that cannot go on before the commit. ``message`` is the sentence the owner reads."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def names_of(items) -> str:
    names = [str(item) for item in items]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def _plural(items, one, many) -> str:
    return one if len(items) == 1 else many


def _default_spawn(target) -> None:
    threading.Thread(target=target, name="runtime-switch", daemon=True).start()


class RuntimeSwitch:
    """``view`` and ``start`` for the owner's route, ``recover`` for the service start.

    Seams (all optional, read late from ``runtime`` when not given): ``managed`` (the managed runtime), ``backend`` (the terminal
    backend), ``agents`` (the active agents: ``[{"agent_id", "name", "client", "on", "resumable"}]``), ``availability``,
    ``survey``/``apply``/``machine``/``tmux_factory``/``env`` (the cutover tool), ``force_stop`` (end one host by pid), ``spawn``
    (run the worker), ``clock``, ``sleep``, ``trusted``/``grant_trust`` (the owner's consent to mark agent folders as trusted in
    Claude Code, ``agent_trust.py``)."""

    def __init__(self, runtime, *, managed=None, backend=None, agents=None, availability=None, survey=None, apply=None, machine=None,
                 tmux_factory=None, env=None, force_stop=None, spawn=None, clock=time.monotonic, sleep=time.sleep, bump=None,
                 trusted=None, grant_trust=None):
        self.runtime = runtime
        self._trusted_seam, self._grant_trust_seam = trusted, grant_trust
        self._managed_seam, self._backend_seam, self._agents_seam = managed, backend, agents
        self._availability = availability or (lambda refresh=False: terminal_support.availability(refresh=refresh))
        self._survey_seam, self._apply_seam = survey, apply
        self._machine, self._tmux_factory, self._env = machine, tmux_factory, env
        self._force_stop_seam, self._spawn = force_stop, spawn or _default_spawn
        self._clock, self._sleep, self._bump_seam = clock, sleep, bump
        self._lock = threading.Lock()          # held for the whole switch; a second request finds it taken
        self._guard = threading.Lock()         # protects the fields below
        self._progress = None
        self._last = None
        self._seen = None                      # what ``view`` computed last, served while a switch runs (it is polled every 2 s)
        self._busy_cache = None

    # ----- the objects around ------------------------------------------------------------------------------------------

    @property
    def paths(self):
        return getattr(self.runtime, "paths", None)

    def _managed(self):
        if self._managed_seam is not None:
            return self._managed_seam
        return getattr(self.runtime, "managed_runtime", None) or getattr(getattr(self.runtime, "native_chat", None), "managed", None)

    def _backend(self):
        if self._backend_seam is not None:
            return self._backend_seam
        return getattr(getattr(self.runtime, "terminal_control", None), "backend", None)

    def _native(self):
        return getattr(self.runtime, "native_chat", None)

    # ----- who may ask -------------------------------------------------------------------------------------------------

    def _require_owner(self, principal_id) -> None:
        with self.runtime.database.read() as uow:
            principal = uow.repository("principals").get(principal_id) if principal_id else None
            allowed = bool(principal and principal["kind"] == "human" and principal["active"] and installation_owner(uow) == principal_id)
        if not allowed:
            raise DomainError("forbidden", FORBIDDEN)

    # ----- what the agents are -----------------------------------------------------------------------------------------

    def _agents(self) -> list:
        """Every active agent Chat knows: ``agent_id``, ``name``, its effective ``client`` (as an open would use now), whether it is
        ``on`` (the owner did not turn it off) and whether it has a Claude Code conversation to ``resume``."""
        if self._agents_seam is not None:
            return [dict(item) for item in self._agents_seam()]
        from qorqut.managed_runtime import options
        from qorqut.native_chat import selection, store
        ready = options.ready_function()
        found = []
        with self.runtime.database.read() as uow:
            for profile in store.observed_agents(uow.connection, self.paths):
                agent_id = profile["agent_id"]
                chosen = selection.selected(uow.connection, agent_id)
                try:
                    client = options.effective_client(self.paths, agent_id, profile, ready)
                except (KeyError, TypeError, ValueError):
                    client = None
                found.append({"agent_id": agent_id, "name": store.profile_label(profile), "client": client,
                              "on": chosen is None or chosen.desired_state != "stopped",
                              "resumable": bool(chosen is not None and chosen.provider == "claude")})
        return found

    def _labels(self, agents=None) -> dict:
        return {item["agent_id"]: item["name"] for item in (self._agents() if agents is None else agents)}

    # ----- reads -------------------------------------------------------------------------------------------------------

    def _terminals_view(self, agents) -> dict:
        """Whether Terminals can be chosen and why not, and ``trusted``: whether the owner already allowed Qorqbot to mark its
        agent folders as trusted in Claude Code (without it Claude Code asks in each terminal, and nothing can be started for
        the owner)."""
        found, trusted = self._availability(), self._trusted()
        if not found.get("available"):
            return {"available": False, "reason": found.get("reason"), "detail": found.get("detail"), "agents": [], "trusted": trusted}
        codex = [{"agent_id": item["agent_id"], "name": item["name"]} for item in agents if item["client"] == "codex"]
        if codex:
            return {"available": False, "reason": "codex_agents", "detail": self._codex_sentence(codex), "agents": codex,
                    "trusted": trusted}
        return {"available": True, "reason": None, "detail": None, "agents": [], "trusted": trusted}

    def _trusted(self) -> bool:
        try:
            if self._trusted_seam is not None:
                return bool(self._trusted_seam())
            from qorqut import agent_trust
            return bool(agent_trust.consent(self.paths.runtime)["granted"])
        except Exception:  # noqa: BLE001 - a consent that cannot be read is not given
            return False

    def _grant_trust(self) -> None:
        """The owner's consent, given in the switch's own confirmation: recorded, and every agent's folder marked as trusted."""
        if self._grant_trust_seam is not None:
            self._grant_trust_seam()
            return
        from qorqut import agent_trust
        agent_trust.owner_view(self.runtime, {"granted": True})

    @staticmethod
    def _codex_sentence(codex) -> str:
        names = [item["name"] for item in codex]
        return (f"{names_of(names)} {_plural(names, 'uses', 'use')} Codex. Only Claude Code agents can run in terminals. "
                "Set them to Claude Code under Client, model and effort first.")

    def _busy(self) -> list:
        """Agents with a running turn or a waiting question in the current mode: ``[{"agent_id", "name"}]``."""
        now = self._clock()
        with self._guard:
            cached = self._busy_cache
        if cached is not None and now - cached[0] < BUSY_CACHE_SECONDS and cached[1] == mode.current(self.paths):
            return [dict(item) for item in cached[2]]
        current = mode.current(self.paths)
        ids = self._busy_ids_terminal() if current == TERMINAL else self._busy_ids_managed()
        labels = self._labels()
        found = [{"agent_id": item, "name": labels.get(item, item)} for item in ids]
        with self._guard:
            self._busy_cache = (now, current, found)
        return [dict(item) for item in found]

    def _busy_ids_managed(self) -> list:
        managed = self._managed()
        if managed is None:
            return []
        found = []
        for host in managed.running_hosts():
            try:
                shown = managed.snapshot(host["agent_id"])
            except Exception:  # noqa: BLE001 - a host that cannot be read is not shown as busy
                continue
            if shown.get("can_stop") or shown.get("pending"):
                found.append(host["agent_id"])
        return sorted(found)

    def _busy_ids_terminal(self) -> list:
        try:
            state = self._survey()
        except Exception:  # noqa: BLE001
            return []
        return sorted(view["agent_id"] for view in state["agents"] if view.get("turn") == "running")

    def view(self, principal_id) -> dict:
        """The owner's Settings read. Cheap while a switch runs: it then serves what was read when the switch was asked for."""
        self._require_owner(principal_id)
        with self._guard:
            progress, seen = (dict(self._progress) if self._progress else None), self._seen
        if progress is not None and seen is not None:
            terminals, busy = seen
        else:
            agents = self._agents()
            terminals, busy = self._terminals_view(agents), self._busy()
            with self._guard:
                self._seen = (terminals, busy)
        with self._guard:
            last = self._last
        if last is None:
            last = mode.document(self.paths).get("last") or None
        return {"mode": mode.current(self.paths), "recommended": RECOMMENDED, "terminals": dict(terminals), "busy": list(busy),
                "switching": progress, "last": last}

    # ----- the request -------------------------------------------------------------------------------------------------

    def start(self, principal_id, to, force=False, trust=False) -> dict:
        """Check everything, journal the switch and run it on a worker thread. Returns at once: ``{"changed": False, ...view}``
        when the installation already runs that way, else ``{"accepted": True, ...view}``. Refuses with a ``DomainError``.
        ``trust`` (a switch to Terminals only) is the owner's consent to mark the agent folders as trusted in Claude Code; it is
        recorded only once the switch is done."""
        self._require_owner(principal_id)
        if to not in mode.MODES or not isinstance(force, bool) or not isinstance(trust, bool):
            raise DomainError("validation_error", "Agents run as managed sessions or in terminals")
        if not self._lock.acquire(blocking=False):
            raise self._running()
        taken = True
        try:
            current = mode.current(self.paths)
            if current == to:
                return {"changed": False, **self.view(principal_id)}
            flock = self._flock()
            try:
                plan = self._check(to, force)
                plan["trust"] = bool(trust and to == TERMINAL)
                mode.begin(self.paths, to, force=force)
            except BaseException:
                self._close_flock(flock)
                raise
            with self._guard:
                self._progress = {"to": to, "step": "ending", "done": 0, "total": len(plan["ending"]), "current": None}
                self._seen = (plan["terminals"], plan["busy"])
            taken = False                      # from here ``_finish`` releases the lock, on the worker or when it cannot start
            try:
                self._spawn(lambda: self._work(to, force, plan, flock))
            except BaseException:
                self._finish(to, ok=False, message=ABORTED + " " + UNEXPECTED, flock=flock)
                raise
            return {"accepted": True, **self.view(principal_id)}
        finally:
            if taken:
                self._lock.release()

    def _running(self) -> DomainError:
        with self._guard:
            progress = dict(self._progress) if self._progress else None
        return DomainError("switch_running", SWITCH_RUNNING, retryable=True, details={"switching": progress} if progress else None)

    def _flock(self):
        """The cross-process lock. ``switch_running`` when another process holds it; ``operation_unavailable`` when it cannot be made."""
        try:
            folder = mode.mode_file(self.paths).parent
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(folder / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except (OSError, AttributeError, TypeError) as error:
            raise DomainError("operation_unavailable", "Qorqbot could not start the switch. Try again in a few minutes.") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise DomainError("switch_running", SWITCH_RUNNING, retryable=True) from None
        return descriptor

    @staticmethod
    def _close_flock(descriptor) -> None:
        if descriptor is None:
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    # ----- the checks, before anything is touched ----------------------------------------------------------------------

    def _check(self, to, force) -> dict:
        agents = self._agents()
        labels = self._labels(agents)
        available = self._availability(True)
        terminals = self._terminals_view(agents)
        if to == TERMINAL:
            if not available.get("available"):
                raise DomainError("terminal_unavailable", available.get("detail") or "Terminals need tmux, which is not installed on this computer.")
            if terminals["reason"] == "codex_agents":
                raise DomainError("codex_agents", terminals["detail"], details={"agents": terminals["agents"]})
        self._writes_open()
        if to == TERMINAL:
            plan = self._check_managed(labels, force)
        else:
            plan = self._check_terminals(labels, force, available.get("available"))
        plan.update(terminals=terminals, labels=labels)
        return plan

    def _writes_open(self) -> None:
        try:
            with self.runtime.database.read() as uow:
                self.runtime.application.ensure_writes_open(uow)
        except DomainError as error:
            raise DomainError("operation_unavailable", UNAVAILABLE, retryable=True) from error
        except AttributeError:
            return

    def _check_managed(self, labels, force) -> dict:
        managed = self._managed()
        hosts = managed.running_hosts() if managed is not None else []
        ids = [host["agent_id"] for host in hosts]
        unverified = [item for item in sorted(labels) if self._unverified(managed, item)]
        if unverified:
            named = [labels[item] for item in unverified]
            raise DomainError("session_open", f"{names_of(named)} {_plural(named, 'has a session', 'have sessions')} that cannot be checked "
                              "right now. Try again in a minute.", details={"agents": [{"agent_id": i, "name": labels[i]} for i in unverified]})
        busy = self._busy_ids_managed()
        found = [{"agent_id": item, "name": labels.get(item, item)} for item in busy]
        if found and not force:
            raise self._turns(found)
        return {"ending": ids, "busy": found}

    @staticmethod
    def _unverified(managed, agent_id) -> bool:
        if managed is None:
            return False
        from qorqut.managed_runtime.manager import HostUnverified
        try:
            managed.handle(agent_id)
        except HostUnverified:
            return True
        except Exception:  # noqa: BLE001 - a read that fails is not a refusal
            return False
        return False

    def _check_terminals(self, labels, force, tmux) -> dict:
        if not tmux:
            return {"ending": [], "busy": [], "survey": None}
        from qorqut.migration import managed_cutover as cutover
        state = self._survey()
        unclear = [view for view in state["agents"] if cutover.refusals(view, force=force, allow_mismatch=False)]
        if unclear:
            named = [view["name"] for view in unclear]
            sentence = (f"{named[0]}'s terminal holds something Qorqbot does not recognise (for example a second program). "
                        "Close it yourself, then try again.") if len(named) == 1 else (
                f"The terminals of {names_of(named)} hold something Qorqbot does not recognise (for example a second program). "
                "Close them yourself, then try again.")
            raise DomainError("terminal_unclear", sentence,
                              details={"agents": [{"agent_id": v["agent_id"], "name": v["name"]} for v in unclear]})
        busy = [{"agent_id": v["agent_id"], "name": v["name"]} for v in state["agents"] if v.get("turn") == "running"]
        if busy and not force:
            raise self._turns(busy)
        ending = [view["agent_id"] for view in state["agents"] if view["session_present"] or view["leftovers"]]
        return {"ending": ending, "busy": busy, "survey": state}

    @staticmethod
    def _turns(busy) -> DomainError:
        names = [item["name"] for item in busy]
        return DomainError("turns_running", f"{names_of(names)} {_plural(names, 'is', 'are')} in the middle of a turn. Wait for "
                           f"{_plural(names, 'it', 'them')} to finish, or switch now and cut the turn off.", details={"agents": busy})

    # ----- the cutover tool --------------------------------------------------------------------------------------------

    def _environment(self) -> dict:
        return dict(os.environ if self._env is None else self._env)

    def _survey(self) -> dict:
        if self._survey_seam is not None:
            return self._survey_seam()
        from qorqut.migration import managed_cutover as cutover
        return cutover.survey(self.paths, self._environment(), machine=self._machine, tmux_factory=self._tmux_factory)

    def _apply(self, agent_id, force) -> dict:
        if self._apply_seam is not None:
            return self._apply_seam(agent_id, force)
        from qorqut.migration import managed_cutover as cutover
        # Not the instance lock (the service holds it itself) and no wait: a turn that began since the check refuses the switch.
        return cutover.run_apply(self.paths, self._environment(), only=agent_id, force=force, wait_seconds=0.0, machine=self._machine,
                                 tmux_factory=self._tmux_factory, lock=False, record=False, switching=True)

    # ----- the worker --------------------------------------------------------------------------------------------------

    def _step(self, **changes) -> None:
        with self._guard:
            if self._progress is not None:
                self._progress.update(changes)

    def _work(self, to, force, plan, flock) -> None:
        committed, closed, message = False, [], None
        try:
            if to == TERMINAL:
                message = self._to_terminal(force, plan, closed)
            else:
                message = self._to_managed(force, plan, closed)
            committed = True
        except Abort as stop:
            message = stop.message
        except Exception:  # noqa: BLE001 - whatever it was, the mode stays and the owner is told in words
            LOGGER.exception("The switch to %s stopped", to)
            message = ABORTED + " " + UNEXPECTED + (CLOSED_BEFORE if closed and to == TERMINAL else ENDED_BEFORE if closed else "")
        self._finish(to, ok=committed, message=message, flock=flock, abort=not committed)

    def _finish(self, to, *, ok, message, flock, abort=True) -> None:
        try:
            if abort:
                try:
                    mode.abort(self.paths, to=to, message=message)
                except (OSError, ValueError):
                    LOGGER.warning("The switch journal could not be cleared", exc_info=True)
            with self._guard:
                self._last = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "to": to, "ok": bool(ok),
                              "message": str(message)[:400]}
                self._progress, self._busy_cache = None, None
        finally:
            self._close_flock(flock)
            self._lock.release()

    @contextmanager
    def _agent_locks(self, agent_id):
        """One agent's locks in the product's order: writer admission, the managed lifecycle hold, the arbiter's lock."""
        with ExitStack() as stack:
            admission = getattr(getattr(self.runtime, "application", None), "writer_admission", None)
            if admission is not None:
                stack.enter_context(admission.writer())
            managed = self._managed()
            if managed is not None and hasattr(managed, "hold"):
                stack.enter_context(managed.hold(agent_id))
            locks = getattr(self._native(), "locks", None)
            stack.enter_context(locks(agent_id) if callable(locks) else nullcontext())
            yield

    def _wait(self, until, seconds) -> bool:
        deadline = self._clock() + seconds
        while True:
            if until():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(POLL_SECONDS)

    # ----- managed to terminals ----------------------------------------------------------------------------------------

    def _to_terminal(self, force, plan, closed) -> str:
        managed = self._managed()
        labels = plan["labels"]
        ids = sorted(set(plan["ending"]) | ({host["agent_id"] for host in managed.running_hosts()} if managed is not None else set()))
        self._step(step="ending", done=0, total=len(ids))
        for index, agent_id in enumerate(ids):
            name = labels.get(agent_id, agent_id)
            self._step(current=name)
            try:
                with self._agent_locks(agent_id):
                    self._end_session(agent_id, name)
            except Abort as stop:
                raise Abort(self._aborted(stop.message, closed, True)) from stop
            except DomainError as error:
                raise Abort(self._aborted(f"{name} could not be closed ({error.message.rstrip('.')}).", closed, True)) from error
            closed.append(agent_id)
            self._step(done=index + 1)
        left = managed.running_hosts() if managed is not None else []
        if left:
            named = [labels.get(item["agent_id"], item["agent_id"]) for item in left]
            raise Abort(self._aborted(f"{names_of(named)} {_plural(named, 'still has a session', 'still have sessions')} that did not close.",
                                      closed, True))
        mode.commit(self.paths, TERMINAL, message=DONE[TERMINAL])
        self._settle(labels)
        if plan.get("trust"):
            try:
                self._grant_trust()
            except Exception:  # noqa: BLE001 - the switch is done; without the consent nothing is started below
                LOGGER.warning("The agent folders could not be marked as trusted after the switch", exc_info=True)
        try:
            return self._start_terminals(labels)
        except Exception:  # noqa: BLE001 - the switch is done; only the starting went wrong
            LOGGER.warning("The terminals could not be started after the switch", exc_info=True)
            return f"{DONE[TERMINAL]} Some agents could not be started; start them from their Terminals."


    @staticmethod
    def _aborted(reason, closed, managed_side) -> str:
        tail = (CLOSED_BEFORE if managed_side else ENDED_BEFORE) if closed else ""
        return f"{ABORTED} {reason}{tail}"

    def _end_session(self, agent_id, name) -> None:
        """Close one agent's host: asked politely, then at once, then by pid; gone is checked, never assumed."""
        managed = self._managed()

        def gone() -> bool:
            try:
                return managed.handle(agent_id) is None
            except Exception:  # noqa: BLE001 - one that cannot be verified is not gone
                return False
        if gone():
            return
        for how, seconds in (("graceful", CLOSE_GRACE), ("now", CLOSE_NOW)):
            try:
                managed.close(agent_id, how)
            except DomainError:
                pass                                   # unreachable now; the next step or the force below decides
            if self._wait(gone, seconds):
                return
        self._force_stop(agent_id)
        if not self._wait(gone, CLOSE_FORCE):
            raise Abort(f"{name} has a session that did not close.")

    def _force_stop(self, agent_id) -> None:
        if self._force_stop_seam is not None:
            self._force_stop_seam(agent_id)
            return
        from qorqut.managed_runtime import shutdown, state as layout
        directory = layout.agent_state(self.paths, agent_id)
        shutdown.end_by_pid(directory)
        shutdown.finish(directory)

    def _start_terminals(self, labels) -> str:
        """After the commit, best effort: a terminal for every agent that is on and has a Claude Code conversation to resume."""
        backend = self._backend()
        wanted = [item for item in self._agents() if item["on"] and item["resumable"] and item["client"] == "claude"]
        if wanted and not self._trusted():
            # Claude Code would ask about the folder in every terminal, and a start never answers that: each would only time out.
            return f"{DONE[TERMINAL]} {NOT_TRUSTED}"
        self._step(step="starting", done=0, total=len(wanted), current=None)
        failed = []
        for index, item in enumerate(wanted):
            self._step(current=item["name"])
            try:
                backend.start_agent(item["agent_id"])
            except Exception:  # noqa: BLE001 - one agent that does not start never undoes the switch
                LOGGER.warning("Agent %s could not be started in a terminal", item["agent_id"], exc_info=True)
                failed.append(item["name"])
            self._step(done=index + 1)
        if not failed:
            return DONE[TERMINAL]
        return (f"{DONE[TERMINAL]} {names_of(failed)} could not be started; start {_plural(failed, 'it from its Terminal', 'them from their Terminals')}.")

    # ----- terminals to managed ----------------------------------------------------------------------------------------

    def _to_managed(self, force, plan, closed) -> str:
        labels = plan["labels"]
        ending = plan["ending"]
        if self._availability().get("available") and plan.get("survey") is not None:
            self._step(step="ending", done=0, total=len(ending))
            for index, agent_id in enumerate(ending):
                name = labels.get(agent_id, agent_id)
                self._step(current=name)
                try:
                    with self._agent_locks(agent_id):
                        outcome = self._apply(agent_id, force)
                except DomainError as error:
                    raise Abort(self._aborted(f"{name} could not be ended ({error.message.rstrip('.')}).", closed, False)) from error
                except Exception as error:  # noqa: BLE001 - the cutover tool's refusals carry a code
                    raise Abort(self._aborted(f"{name} could not be ended.", closed, False)) from error
                stopped = outcome.get("stopped_at") if isinstance(outcome, dict) else None
                if stopped:
                    why = ENDED_REASONS.get(stopped.get("code"), "it did not end")
                    raise Abort(self._aborted(f"{name} could not be ended: {why}.", closed, False))
                closed.append(agent_id)
                self._step(done=index + 1)
            try:
                self._end_workspace_shells()
                self._verify_terminals_gone()
            except Abort as stop:
                raise Abort(self._aborted(stop.message, closed, False)) from stop
        mode.commit(self.paths, MANAGED, message=DONE[MANAGED])
        self._settle(labels)
        return DONE[MANAGED]

    def _end_workspace_shells(self) -> None:
        """The owner's own Workspace shells go with the agents' terminals; they are found by the tool's survey (exact names only)."""
        from qorqut.migration import managed_cutover as cutover
        state = self._survey()
        folder = cutover.tmux_folder(self.paths.instance_root, self._environment())
        make = self._tmux_factory or (lambda name: cutover.Tmux(name, folder, env=self._environment()))
        try:
            for item in state.get("owner_shell_sessions", []):
                tmux = make(item["socket"])
                found = next((one for one in tmux.sessions() or [] if one["name"] == item["session"]), None)
                if found is not None:
                    tmux.kill_session(found["id"])
        except cutover.CutoverError as error:
            raise Abort("Your Workspace terminal could not be ended.") from error

    def _verify_terminals_gone(self) -> None:
        state = self._survey()
        left = [view["name"] for view in state["agents"] if view["session_present"] or view["client"] != "none"]
        if left:
            raise Abort(f"{names_of(left)} {_plural(left, 'still has a terminal', 'still have terminals')} that did not end.")

    # ----- after the commit --------------------------------------------------------------------------------------------

    def _settle(self, labels) -> None:
        """Open dashboards refresh (every agent's visible revision moves) and the observer looks at once. Never raises."""
        try:
            if self._bump_seam is not None:
                self._bump_seam(sorted(labels))
            else:
                self._bump(sorted(labels))
        except Exception:  # noqa: BLE001
            LOGGER.warning("The agents' views could not be refreshed after the switch", exc_info=True)
        try:
            backend = getattr(self._native(), "backend", None)
            if backend is not None:
                backend.wake()
        except Exception:  # noqa: BLE001
            LOGGER.warning("The observer could not be woken after the switch", exc_info=True)

    def _bump(self, agent_ids) -> None:
        from qorqut.native_chat import store
        with self.runtime.database.transaction(operation="native_chat.runtime_switch") as uow:
            for agent_id in agent_ids:
                store.bump(uow.connection, agent_id)

    # ----- service start -----------------------------------------------------------------------------------------------

    def recover(self):
        """At service start: drop the journal of a switch that a dead process left. Returns what was dropped, or ``None``."""
        return mode.recover(self.paths)
