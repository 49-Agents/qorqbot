"""The native Chat backend: one bounded observer thread that runs whether or not any Chat view is open.

Each pass, for every registered active agent that has (or had) a native session: verify the session, keep its durable binding,
capture the bound transcript into the archive and index finished replies, deliver waiting Chat inputs, and run the final
capture of sessions that ended. Before observing, waiting inputs of a recipient that no longer qualifies are settled as
undelivered (maintenance, never delivery). Nothing here launches, resumes or stops an agent, and nothing depends on a request.

The drain is bounded: ``stop_observer`` joins the thread within its timeout and raises ``TimeoutError`` when it survives, keeping
the reference so a later stop can still prove the drain.

Each agent's pass also asks the managed runtime for the host's latest state and events: a change of its phase, pending
request or activity reaches Chat as a visible change (activity text at most once a second), and a prompt a hook blocked is
recorded on the input that carried it. When the installation runs its agents in terminals (``managed_runtime.mode``, asked on
every pass) the managed runtime is not consulted: the pass follows the terminal's conversation and bumps only on a working change.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import threading
import time

from qorqut_operations.contracts import DomainError, utc_now
from qorqut.managed_runtime import mode

from . import store
from .arbiter import AgentLocks, InputArbiter
from .captures import Indexer
from .contracts import STATE_SUBMITTED
from .probe import BINDING, MARKER, SessionProbe
from .sessions import SessionRegistry

LOG = logging.getLogger(__name__)
PASS_SECONDS = 1.0
ACTIVITY_SECONDS = 1.0  # a managed activity line changes Chat's revision at most this often
BLOCKED_PASSES = 120  # how many passes a blocked-prompt report waits for the input it names to be settled


class NativeBackend:
    def __init__(self, runtime, probe: SessionProbe, registry: SessionRegistry, indexer: Indexer, arbiter: InputArbiter,
                 locks: AgentLocks, *, interval: float = PASS_SECONDS, managed=None):
        self.runtime, self.probe, self.registry, self.indexer, self.arbiter, self.locks = runtime, probe, registry, indexer, arbiter, locks
        self.interval = interval
        # The managed runtime, used duck-typed: ``refresh(agent_id) -> changed`` and ``drain_events(agent_id) -> [event dict]``.
        self.managed = managed
        self._bumped: dict[str, float] = {}
        self._blocked: dict[str, dict[str, list]] = {}
        self._owed: set[str] = set()  # agents whose host reported a change that the throttle held back
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._recovered = False
        self._working = {}
        self.adopt = None
        arbiter.wake = self.wake

    @property
    def database(self):
        return self.runtime.database

    # ----- lifetime --------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._watch, name="native-chat-backend", daemon=True)
        self._thread.start()

    def stop_observer(self, timeout: float = 5) -> None:
        """Bounded drain. A surviving observer is an unproved drain: the reference is kept so a later stop can prove it."""
        self._stop_event.set()
        self._wake.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("Native Chat backend is still draining")
        self._thread = None

    def wake(self) -> None:
        self._wake.set()

    def _watch(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                LOG.debug("Native Chat backend paused", exc_info=True)
            self._wake.wait(self.interval)
            self._wake.clear()

    # ----- one pass --------------------------------------------------------------------------------------------------

    def run_once(self) -> None:
        """One complete pass; also the synchronous entry synthetic checks use."""
        self.runtime.require_authority()
        if not self._recovered:
            with self.database.transaction(operation="native_chat.recover") as uow:
                self.runtime.application.ensure_writes_open(uow)
                store.recover_claimed(uow.connection)
            self._recovered = True
        self.undeliver_ineligible()
        with self.database.read() as uow:
            agents = store.observed_agents(uow.connection, self.runtime.paths)
            open_agents = {row["agent_id"] for row in store.open_sessions(uow.connection)}
        for profile in agents:
            if self._stop_event.is_set():
                return
            # An agent that never published a session has nothing to bind; its hook marker appears when it does.
            if profile["agent_id"] not in open_agents and not any(
                    os.path.lexists(Path(profile["workspace"]) / name) for name in (MARKER, BINDING)):
                continue
            try:
                self.observe(profile)
            except DomainError:
                raise
            except Exception:
                LOG.debug("Native Chat observation deferred for %s", profile["agent_id"], exc_info=True)
        self.finish_ended()
        retention = getattr(self.runtime, 'overseer_retention', None)
        if retention is not None:
            retention.drain.step()
        # Qorqut's own compaction of the coordinator's conversation (``compaction.py``): one stage per pass, never a wait.
        compaction = getattr(self.runtime, 'compaction', None)
        if compaction is not None:
            compaction.step()

    def undeliver_ineligible(self) -> None:
        """Settle the waiting inputs of every recipient that is no longer an operating supported global as ``undelivered``
        (``agent_inactive``): a waiting input was never typed, and the observer, which sees only eligible agents, would never
        reach it. This is maintenance, never delivery. Nothing is probed, bound, started, stopped or retargeted; sessions,
        their final capture and every other input state are left as they are, and inbox events stay pending.

        Eligibility is checked again under the agent's lock inside the write transaction, so a recipient restored meanwhile
        keeps its queue. Closed writes refuse the transaction, as they refuse startup recovery, and nothing is settled."""
        with self.database.read() as uow:
            candidates = [agent for agent in store.waiting_agents(uow.connection)
                          if store.active_profile(uow.connection, agent) is None]
        for agent_id in candidates:
            if self._stop_event.is_set():
                return
            with self.locks(agent_id), self.database.transaction(operation="native_chat.undeliver_ineligible") as uow:
                self.runtime.application.ensure_writes_open(uow)
                if store.active_profile(uow.connection, agent_id) is None:
                    store.undeliver_waiting(uow.connection, agent_id, "agent_inactive")

    def observe(self, profile: dict) -> None:
        """Bind, capture and index one agent, then deliver its waiting inputs. Safe to repeat."""
        if self.adopt is not None:
            self.adopt(profile)
        managed = None if mode.is_terminal(getattr(self.runtime, "paths", None)) else self.managed
        with self.locks(profile["agent_id"]):
            changed = self._refresh_managed(managed, profile["agent_id"]) if managed is not None else False
            observed = self.probe.observe(profile)
            reconciled = self.registry.reconcile(profile, observed)
            session = reconciled.session
            # Hook-only changes (for example finishing a turn or waiting for
            # permission) need to reach Chat even without new transcript bytes.
            working = (session["id"], bool(observed.observation.working)) if reconciled.live else None
            agent_id = profile["agent_id"]
            if managed is not None:
                self._publish_managed(agent_id, reconciled, observed, changed)
                self._apply_blocked(managed, agent_id)
            elif self._working.get(agent_id) != working:   # no managed runtime composed, or Terminals mode
                with self.database.transaction(operation="native_chat.working") as uow:
                    self.runtime.application.ensure_writes_open(uow)
                    store.bump(uow.connection, agent_id)
                self._working[agent_id] = working
            if session is not None and session["transcript_path"]:
                step = self.indexer.step(profile, session)
                if step.more:
                    self.wake()
            with self.database.read() as uow:
                waiting = bool(store.waiting_inputs(uow.connection, profile["agent_id"]))
            if waiting:
                self.arbiter.dispatch(profile["agent_id"])

    # ----- managed sessions ------------------------------------------------------------------------------------------

    @staticmethod
    def _refresh_managed(managed, agent_id: str) -> bool:
        """Ask the managed runtime to read the host's state and events. A host that cannot be read changes nothing here: the
        probe decides whether the session is verified."""
        try:
            return bool(managed.refresh(agent_id))
        except Exception:
            LOG.debug("Managed runtime refresh failed for %s", agent_id, exc_info=True)
            return False

    def _publish_managed(self, agent_id: str, reconciled, observed, changed: bool) -> None:
        """Make a change a viewer can see (working, phase, a pending request, the activity line) a visible change. The key is
        ``(session, working)`` plus what the host reports. A change of the activity
        text alone waits out ``ACTIVITY_SECONDS`` since the last one, so a chatty turn does not make every viewer re-read
        Chat many times a second; it is not recorded as published until it bumps, so a later pass publishes it."""
        key = None
        if reconciled.live:
            seen = observed.observation
            key = (reconciled.session["id"], bool(seen.working), seen.phase,
                   tuple(sorted(str(item.get("request_id")) for item in seen.pending)), seen.activity)
        previous = self._working.get(agent_id)
        if key == previous and not changed and agent_id not in self._owed:
            return
        activity_only = (key is not None and previous is not None and len(key) == len(previous) and key[:-1] == previous[:-1])
        now = time.monotonic()
        if (key == previous or activity_only) and now - self._bumped.get(agent_id, 0.0) < ACTIVITY_SECONDS:
            self._owed.add(agent_id)  # published on a later pass; the key is not recorded, so a differing one stays due
            return
        with self.database.transaction(operation="native_chat.working") as uow:
            self.runtime.application.ensure_writes_open(uow)
            store.bump(uow.connection, agent_id)
        self._working[agent_id] = key
        self._bumped[agent_id] = now
        self._owed.discard(agent_id)

    def _apply_blocked(self, managed, agent_id: str) -> None:
        """Record on its input every prompt the managed host reports a hook blocked. The input is already ``submitted`` (the
        client accepted the message); the report only adds that it was not processed. A report that names an input not yet
        settled waits a bounded number of passes for it."""
        waiting = self._blocked.setdefault(agent_id, {})
        try:
            events = managed.drain_events(agent_id) or ()
        except Exception:
            LOG.debug("Managed runtime events failed for %s", agent_id, exc_info=True)
            events = ()
        for item in events:
            event = item.get("event") if isinstance(item, dict) and isinstance(item.get("event"), dict) else item
            if not isinstance(event, dict) or event.get("type") != "prompt_blocked" or not isinstance(event.get("message_uuid"), str):
                continue
            reason = event.get("reason") if isinstance(event.get("reason"), str) else ""
            waiting.setdefault(event["message_uuid"], [reason, 0])
        for message_uuid, entry in list(waiting.items()):
            entry[1] += 1
            with self.database.read() as uow:
                row = store.input_by_message_uuid(uow.connection, agent_id, message_uuid)
            if row is not None and row["state"] == STATE_SUBMITTED:
                with self.database.transaction(operation="native_chat.prompt_blocked") as uow:
                    self.runtime.application.ensure_writes_open(uow)
                    store.annotate_input(uow.connection, row["id"], "prompt_blocked", {"prompt_blocked": {"reason": entry[0][:500]}})
                del waiting[message_uuid]
            elif (row is not None and row["state"] != "submitting") or entry[1] >= BLOCKED_PASSES:
                del waiting[message_uuid]  # settled some other way, or never settled: nothing to annotate

    def finish_ended(self) -> None:
        """The final capture of every session that ended: exact exit evidence, a stable observation, then the verified tail."""
        with self.database.read() as uow:
            pending = store.unfinalized_sessions(uow.connection)
        for session in pending:
            if self._stop_event.is_set():
                return
            with self.locks(session["agent_id"]):
                with self.database.read() as uow:
                    profile = store.active_profile(uow.connection, session["agent_id"], self.runtime.paths)
                if profile is None:
                    self.indexer.give_up_final(session, "agent_inactive")
                    continue
                if not session["transcript_path"]:
                    # Bound without capture: there is no archive to finalize.
                    with self.database.transaction(operation="native_chat.no_capture_final") as uow:
                        store.mark_final_captured(uow.connection, session["id"], utc_now())
                    continue
                result = self.indexer.step(profile, session, final=True)
                if result.finalized:
                    continue
                if result.more:
                    self.wake()
                    continue
                if result.error is None:
                    # The archive could not call the tail final (an unstable observation); try a few more times, then say so.
                    result.error, result.retryable = "not_final", True
                if self.indexer.final_failed(session, result):
                    self.indexer.give_up_final(session, result.error)
