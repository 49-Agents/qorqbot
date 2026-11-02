"""Durable exact session bindings for registered agents.

``SessionRegistry.reconcile`` turns one probe result into the agent's binding state. A binding is created only from a verified
live observation (hook marker plus the live process and pane/thread-lock), is reused while the same conversation is still
held by the same process, and ends only on evidence: the client's own ``SessionEnd``, the bound process or thread writer
being gone, or the selected conversation being re-held by a new process after the old one is verified gone. A momentary
failure to verify (a tmux hiccup, a screen read) never ends a binding; waiting inputs then simply keep waiting for the same
session.

Which conversation may hold a binding is decided by the agent's selection (``selection.py``, the ``native_session_owners``
row), not by whichever conversation was observed last: an observation of any other conversation is only a candidate. It
creates and supersedes nothing; it is bound only after the owner explicitly selects it. With no selection yet nothing is
bound either (the first verified registration creates the selection).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from datetime import datetime, timezone

from qorqut_operations.contracts import utc_now

from . import liveness, selection, store
from .contracts import RUNTIME_PROVIDERS, SessionBinding
from .probe import ProbeResult, REASON_LIVE, REASON_SESSION_END

LOG = logging.getLogger(__name__)
REFRESH_SECONDS = 60


@dataclass
class Reconciled:
    """``session`` is the agent's open binding row (``None`` when it has none); ``live`` says whether the probe verified exactly
    that binding just now; ``ended`` lists the bindings this call closed (their final capture is due)."""

    session: dict | None
    live: bool
    probe: ProbeResult
    ended: list[dict] = field(default_factory=list)
    created: bool = False

    @property
    def binding(self) -> SessionBinding | None:
        return store.binding_of(self.session) if self.session else None


def session_gone(row: dict) -> bool:
    """Whether the process (or Codex thread writer) a binding was made for is *verifiably* gone. ``liveness`` has three answers:
    only ``dead`` ends a binding. A process that cannot be read (permission, I/O, malformed evidence) is ``unknown``, which is
    neither alive nor gone, so it neither ends the binding nor lets another process replace it."""
    return liveness.of_session(row) == liveness.DEAD


def _age(text: str) -> float:
    try:
        then = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then.astimezone(timezone.utc)).total_seconds()


class SessionRegistry:
    def __init__(self, runtime, *, gone=session_gone):
        self.runtime = runtime
        self._gone = gone

    @property
    def database(self):
        return self.runtime.database

    def current(self, agent_id: str) -> dict | None:
        with self.database.read() as uow:
            return store.live_session(uow.connection, agent_id)

    def reconcile(self, profile: dict, result: ProbeResult) -> Reconciled:
        agent_id = profile["agent_id"]
        with self.database.read() as uow:
            latest = store.live_session(uow.connection, agent_id)
            chosen = selection.selected(uow.connection, agent_id)
        observation = result.observation if result.reason == REASON_LIVE else None
        if observation is not None and (chosen is None or not chosen.holds(observation)):
            # Not bindable: another conversation (a candidate), or the selected conversation held by a process the owner row
            # was not adopted for. The latter waits for the authoritative bootstrap/adoption (``start_session``) to renew the
            # owner row; until then the earlier binding ends only on its own evidence below, and nothing new is created.
            observation = None
        if (observation is not None and latest is not None and not store.same_session(latest, observation)
                and chosen.matches(latest["provider"], latest["conversation_id"]) and not self._ended_by_gone(latest)):
            # The selected conversation is still held by its earlier process: a second holder is never adopted.
            return Reconciled(latest, False, result)
        if observation is not None and latest is not None and store.same_session(latest, observation):
            attach = bool(observation.transcript_path and latest["transcript_path"] is None)
            if attach or _age(latest["verified_at"]) >= REFRESH_SECONDS:
                with self.database.transaction(operation="native_chat.session_verified") as uow:
                    self.runtime.application.ensure_writes_open(uow)
                    store.touch_session(uow.connection, latest["id"], utc_now())
                    if attach:
                        store.attach_transcript(uow.connection, latest, observation.transcript_path)
                        latest = store.get_session(uow.connection, latest["id"])
                        store.ensure_capture(uow.connection, latest)
                        store.bump(uow.connection, latest["agent_id"])
            return Reconciled(latest, True, result)
        if observation is None:
            if latest is None or not self._ended(latest, result):
                return Reconciled(latest, False, result)
            reason = "session_end" if result.reason == REASON_SESSION_END else "process_gone"
            with self.database.transaction(operation="native_chat.session_end") as uow:
                self.runtime.application.ensure_writes_open(uow)
                closed = store.end_session(uow.connection, latest["id"], reason, utc_now())
                row = store.get_session(uow.connection, latest["id"])
            return Reconciled(None, False, result, [row] if closed else [])
        with self.database.transaction(operation="native_chat.session_bind") as uow:
            self.runtime.application.ensure_writes_open(uow)
            conn, now, ended = uow.connection, utc_now(), []
            # Another transaction may have bound the same observation since the read above.
            current = store.live_session(conn, agent_id)
            if current is not None and store.same_session(current, observation):
                return Reconciled(current, True, result)
            if current is not None:
                store.end_session(conn, current["id"], "superseded", now)
                ended.append(store.get_session(conn, current["id"]))
            row = store.create_session(conn, observation, now)
            if row["transcript_path"]:
                store.ensure_capture(conn, row)
        LOG.info("Bound native session for %s: generation %s", agent_id, row["generation"])
        return Reconciled(row, True, result, ended, created=True)

    @staticmethod
    def _end_marker_is_for(latest: dict, marker: dict) -> bool:
        """Whether a ``SessionEnd`` hook marker ends exactly this binding. The conversation alone is not enough: a resumed
        conversation keeps its id, so a late hook from the earlier process would otherwise close the live one. The hook records
        the native process that ran it (pid and start ticks), the provider runtime and the agent; all must be the binding's."""
        started = marker.get("started")
        return (marker.get("agent_id") == latest["agent_id"] and marker.get("session_id") == latest["conversation_id"]
                and RUNTIME_PROVIDERS.get(marker.get("runtime")) == latest["provider"]
                and type(marker.get("pid")) is int and marker["pid"] == latest["process_pid"]
                and isinstance(started, str) and started == latest["process_started"])

    def _ended_by_gone(self, row: dict) -> bool:
        try:
            return bool(self._gone(row))
        except OSError:
            return False

    def _ended(self, latest: dict, result: ProbeResult) -> bool:
        """Evidence that the open binding is over: the client reported ``SessionEnd`` from this binding's own process, or what
        it was bound to is gone. A stale end marker of another process never ends a binding whose process is alive; only the
        verified death of the bound process (or Codex thread writer) does."""
        if result.reason == REASON_SESSION_END and self._end_marker_is_for(latest, result.marker or {}):
            return True
        try:
            return bool(self._gone(latest))
        except OSError:
            return False
