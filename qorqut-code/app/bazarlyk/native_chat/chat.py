"""Owner-facing derived Chat for any registered agent, global or business.

Chat is a projection. It never launches, resumes or stops an agent, and opening it or switching agents starts nothing:
* the conversation is read from the transcript archive (the backend keeps it current whether or not Chat is open);
* delivery state comes from the shared input arbiter's durable inputs;
* "read" is a boundary on the agent's per-agent completion sequence, owned by the installation owner.

Every method re-validates, in its own transaction, that the caller is the installation owner, that the agent is an active
registered profile and, for a business agent, that its business is live and the owner holds it.
"""

from __future__ import annotations

import logging

from qorqut_operations.business_lifecycle import require_active_business
from qorqut_operations.contracts import DomainError
from bazarlyk.management.identity import installation_owner
from bazarlyk.native_archive import CaptureError

from . import store
from .adapters import CodexQueueAdapter, TerminalAdapter
from .arbiter import AgentLocks, InputArbiter
from .captures import Indexer
from .contracts import (InputRequest, NativeChatError, PROVIDERS, RESULT_DEFERRED, RESULT_REJECTED, TYPE_CHAT)
from .probe import (ProbeResult, REASON_CAPTURE_DISABLED, REASON_CAPTURE_NOT_READY, REASON_LIVE, REASON_UNSUPPORTED,
                    REASON_UNSUPPORTED_RUNTIME, SessionProbe)
from .projection import Lineage, SPEAKER_AGENT
from .reader import ConversationReader, entry_view, visible
from .service import NativeBackend
from .sessions import SessionRegistry
from .transcripts import ArchiveTranscripts

LOG = logging.getLogger(__name__)
MAX_PAGE = 100
# A live, captured session whose open binding the backend observer has not recorded yet: the composer waits for it.
REASON_BINDING_NOT_READY = "binding_not_ready"

_REFUSALS = {
    "terminal_offline": ("terminal_offline", "Start or resume the agent in its terminal first"),
    "capture_disabled": ("capture_disabled", "Transcript capture is off for this agent, so Chat has no conversation to show"),
    "capture_not_ready": ("capture_not_ready", "Transcript capture is on but this session has not published its transcript yet; send again shortly"),
    "unsupported_target": ("operation_unavailable", "Chat needs this agent's native session on the host"),
    "unsupported_runtime": ("unsupported_runtime", "Chat supports Claude Code and Codex agents only"),
    "stale_session": ("terminal_changed", "The terminal conversation changed; send again"),
    "agent_unavailable": ("not_found", "Select an active registered agent"),
}


class NativeChat:
    """The runtime's Chat facade: owner-facing reads and sends, the shared arbiter, and the backend lifecycle."""

    def __init__(self, runtime, *, probe: SessionProbe, registry: SessionRegistry, indexer: Indexer, arbiter: InputArbiter,
                 backend: NativeBackend, reader: ConversationReader | None = None):
        self.runtime, self.probe, self.registry, self.indexer = runtime, probe, registry, indexer
        self.arbiter, self.backend, self.reader = arbiter, backend, reader or ConversationReader()
        self.transcripts = ArchiveTranscripts(runtime, indexer)

    @classmethod
    def build(cls, runtime, *, probe: SessionProbe | None = None, adapters=None, archive_factory=None,
              interval: float = 1.0) -> "NativeChat":
        """The default object graph of one host runtime. The terminal backend is read late: it is attached after construction."""
        def terminal_backend():
            return getattr(getattr(runtime, "terminal_control", None), "backend", None)

        locks = AgentLocks()
        probe = probe or SessionProbe(terminal_backend)
        registry = SessionRegistry(runtime)
        indexer = Indexer(runtime, archive_factory=archive_factory)
        adapters = adapters or {"terminal": TerminalAdapter(terminal_backend), "codex_queue": CodexQueueAdapter()}
        arbiter = InputArbiter(runtime, probe, registry, adapters, locks=locks)
        backend = NativeBackend(runtime, probe, registry, indexer, arbiter, locks, interval=interval)
        return cls(runtime, probe=probe, registry=registry, indexer=indexer, arbiter=arbiter, backend=backend)

    @property
    def database(self):
        return self.runtime.database

    # ----- lifetime --------------------------------------------------------------------------------------------------

    def start(self) -> None:
        self.backend.start()

    def stop_observer(self, timeout: float = 5) -> None:
        self.backend.stop_observer(timeout)

    # ----- authorization ---------------------------------------------------------------------------------------------

    def _target(self, uow, principal_id: str, agent_id: str) -> dict:
        """Any active registered agent: its exact saved profile row, never a name or navigation pin. A business agent also
        needs an unarchived business and the owner's active ownership of it, as for its Terminal."""
        principal = uow.repository("principals").get(principal_id)
        if not principal or principal["kind"] != "human" or not principal["active"] or installation_owner(uow) != principal_id:
            raise DomainError("forbidden", "Agent chat is for the installation owner")
        profile = store.active_profile(uow.connection, agent_id, self.runtime.paths)
        if profile is None:
            raise DomainError("not_found", "Select an active registered agent")
        business = profile["home_business_id"]
        if business is not None:
            require_active_business(uow.connection, business)
            if not uow.connection.execute("SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=? AND active=1 AND is_owner=1",
                                          (business, principal_id)).fetchone():
                raise DomainError("forbidden", "Agent chat requires the current business owner")
        return profile

    # ----- reads -----------------------------------------------------------------------------------------------------

    def session_view(self, profile: dict, persisted: dict | None, observed: ProbeResult | None = None) -> dict:
        """The session as the composer needs it. Observing starts nothing and writes nothing: ``persisted`` is the agent's open
        binding row as the caller read it, and ``binding`` is supplied only when a fresh observation matches exactly that row.
        A composer may send only to a captured conversation whose exact binding the backend observer has already recorded; until
        then the view says it is connecting."""
        observed = observed or self.probe.observe(profile)
        observation = observed.observation
        live = observed.reason == REASON_LIVE
        bound = bool(live and persisted is not None and store.same_session(persisted, observation))
        binding = store.binding_of(persisted).public() if bound else None
        captured = bool(live and observation.capture)
        sendable = captured and bound
        if live:
            surface = "Codex" if observation.transport == "codex_queue" else "Terminal"
            if sendable:
                reason, detail = REASON_LIVE, "Working…" if observation.working else f"{surface} connected"
            elif captured:
                reason, detail = REASON_BINDING_NOT_READY, f"{surface} connected; connecting to this session"
            elif observation.capture_enabled:
                # Capture is on in the registration; the client's hook has not published this session's transcript yet.
                reason, detail = REASON_CAPTURE_NOT_READY, f"{surface} connected; waiting for the session's transcript to be published"
            else:
                reason, detail = REASON_CAPTURE_DISABLED, "Transcript capture is off for this agent, so Chat has no conversation to show"
        else:
            reason = observed.reason
            detail = {REASON_UNSUPPORTED: "Chat needs this agent's native session on the host",
                      REASON_UNSUPPORTED_RUNTIME: "Chat supports Claude Code and Codex agents only"}.get(
                reason, "Start or resume the agent in its terminal")
        return {"state": "live" if live else "offline", "reason": reason, "can_send": sendable,
                "working": bool(captured and observation.working), "provider": observation.provider if live else None,
                "conversation_id": observation.conversation_id if live else None,
                "transport": observation.transport if live else None, "binding": binding, "detail": detail}

    def state(self, principal_id: str, agent_id: str, *, before: int | None = None, limit: int = 50,
              conversation: str | None = None, provider: str | None = None) -> dict:
        """The default conversation, or the one explicit ``conversation`` + ``provider`` pair (an archive, view-only). Which
        conversation is current never depends on which one was asked for."""
        limit = max(1, min(int(limit), MAX_PAGE))
        with self.database.read() as uow:
            profile = self._target(uow, principal_id, agent_id)
            conn = uow.connection
            pair = self._pair(conversation, provider)
            counters = store.counters(conn, agent_id)
            captures = store.agent_captures(conn, agent_id)
            latest = store.latest_session(conn, agent_id)
            persisted = store.live_session(conn, agent_id)
            inputs = store.recent_inputs(conn, agent_id, 30)
            bindings = store.public_bindings(conn, [row["session_id"] for row in inputs])
            last_read = store.last_read(conn, agent_id, principal_id)
        view = self.session_view(profile, persisted)
        default = self._default(captures, latest)
        selected = default if pair is None else self._explicit(captures, pair)
        entries, older, info, rendered = [], None, None, 0
        if selected is not None:
            entries, older, info, rendered = self._entries(profile, selected, before, limit)
        return {"agent_id": agent_id, "name": store.profile_label(profile), "session": view, "revision": counters["revision"],
                "cursor": counters["revision"], "conversation": info,
                "conversations": [{"conversation_id": c["conversation_id"], "provider": c["provider"], "updated_at": c["updated_at"],
                                   "current": default is not None and self._same(c, default)} for c in captures],
                "entries": entries, "older": older, "rendered_boundary": rendered,
                "inputs": [store.input_view(row, bindings.get(row["session_id"])) for row in inputs],
                "last_read_sequence": last_read, "newest_sequence": counters["completion_sequence"]}

    def history(self, principal_id: str, agent_id: str, *, before: int, limit: int = 50, conversation: str | None = None,
                provider: str | None = None) -> dict:
        limit = max(1, min(int(limit), MAX_PAGE))
        with self.database.read() as uow:
            profile = self._target(uow, principal_id, agent_id)
            pair = self._pair(conversation, provider)
            captures = store.agent_captures(uow.connection, agent_id)
            latest = store.latest_session(uow.connection, agent_id)
        selected = self._default(captures, latest) if pair is None else self._explicit(captures, pair)
        if selected is None:
            return {"entries": [], "older": None, "rendered_boundary": 0}
        entries, older, _, rendered = self._entries(profile, selected, before, limit)
        return {"entries": entries, "older": older, "rendered_boundary": rendered}

    @staticmethod
    def _pair(conversation: str | None, provider: str | None) -> tuple[str, str] | None:
        """An explicit selection names a conversation by both its provider and its native id, since two providers may use the
        same id; a default request names neither."""
        if conversation is None and provider is None:
            return None
        if conversation is None or provider is None:
            raise DomainError("validation_error", "conversation and provider are given together or not at all")
        if provider not in PROVIDERS:
            raise DomainError("validation_error", "provider must be claude or codex")
        return provider, conversation

    @staticmethod
    def _same(capture: dict, other: dict) -> bool:
        return capture["provider"] == other["provider"] and capture["conversation_id"] == other["conversation_id"]

    @staticmethod
    def _explicit(captures: list[dict], pair: tuple[str, str]) -> dict:
        """The captured conversation of exactly this provider and id. An unknown pair is refused, never replaced by another."""
        provider, conversation = pair
        found = next((c for c in captures if c["provider"] == provider and c["conversation_id"] == conversation), None)
        if found is None:
            raise DomainError("not_found", "That conversation was not captured for this agent")
        return found

    @classmethod
    def _default(cls, captures: list[dict], latest: dict | None):
        """The conversation of the agent's newest binding when it was captured, otherwise the most recently updated capture."""
        if latest is not None:
            found = next((c for c in captures if cls._same(c, latest)), None)
            if found is not None:
                return found
        return max(captures, key=lambda c: c["updated_at"], default=None)

    def _entries(self, profile, capture, before, limit):
        """One page of the selected conversation, with the facts that belong to exactly the entries it returns: the verified
        provenance of the system markers on it, the durable completion bound to each assistant entry, and
        ``rendered_boundary`` -- the newest completion sequence among the completed assistant entries this page actually returns
        after projection and pagination (0 when it returns none). A client acknowledges that value, never the agent's newest
        sequence, so a page that did not display a reply cannot acknowledge it."""
        info = {"conversation_id": capture["conversation_id"], "provider": capture["provider"],
                "complete": bool(capture["complete"]), "finalized": bool(capture["finalized"]), "error": capture["error_code"],
                "captured_length": capture["captured_length"]}
        if not capture["receipt_id"]:
            return [], None, info, 0
        archive = self.indexer.archive(profile)
        try:
            receipt = archive.load_receipt(capture["receipt_id"])
            window = self.reader.window(archive, receipt, capture["provider"], capture["conversation_id"], before=before, limit=limit)
        except (CaptureError, ValueError) as error:
            info["error"] = getattr(error, "code", "archive_unavailable")
            return [], None, info, 0
        agent_id, provider, conversation = profile["agent_id"], capture["provider"], capture["conversation_id"]
        lineage = Lineage(agent_id, provider, conversation, receipt.archive_id)
        with self.database.read() as uow:
            markers = store.conversation_markers(uow.connection, agent_id, provider, conversation,
                                                 [entry.marker for entry in window.entries], capture["transcript_path"])
            replies = {row["raw_offset"]: row for row in store.replies_at(
                uow.connection, agent_id, provider, conversation, [entry.offset for entry in window.entries])}
        shown = visible(window.entries, replies)
        rendered = max((row["completion_sequence"] for entry, row in shown if row is not None and entry.speaker == SPEAKER_AGENT), default=0)
        return [entry_view(entry, lineage, markers, row) for entry, row in shown], window.older, info, rendered

    # ----- stream ----------------------------------------------------------------------------------------------------

    def check_events(self, principal_id: str, agent_id: str) -> None:
        with self.database.read() as uow:
            self._target(uow, principal_id, agent_id)

    def events(self, principal_id: str, agent_id: str, cursor: int, *, limit: int = 200):
        """The stream position is the agent's visible-change revision. A position behind it yields one ``changed`` event that
        says to read the current state; a position ahead of it (a restored database) is reported to the caller as such."""
        with self.database.read() as uow:
            self._target(uow, principal_id, agent_id)
            counters = store.counters(uow.connection, agent_id)
            pending = store.waiting_inputs(uow.connection, agent_id)
        latest = counters["revision"]
        if cursor >= latest:
            return [], latest
        return [{"cursor": latest, "kind": "changed",
                 "data": {"revision": latest, "newest_sequence": counters["completion_sequence"], "waiting": len(pending)}}], latest

    # ----- owner actions ---------------------------------------------------------------------------------------------

    def send(self, principal_id: str, agent_id: str, text: str, key: str, expected_binding: str) -> dict:
        """Admit one owner message for the exact native session ``expected_binding`` names and return at once; the backend types
        it when that same conversation's composer is ready. The pin is the ``binding_id`` the owner's page last saw. After the
        current authorization, an exact repeat (same key, text and pin) returns the original input even if its session has since
        ended; the same key with another text or pin conflicts; a new request for a pin that is no longer the live session is
        ``terminal_changed`` and admits nothing."""
        if not isinstance(key, str) or not 8 <= len(key) <= 120:
            raise DomainError("validation_error", "An idempotency key of 8-120 characters is required")
        if not isinstance(expected_binding, str) or not 8 <= len(expected_binding) <= 120:
            raise DomainError("validation_error", "The binding_id of the session being written to is required")
        with self.database.read() as uow:
            self._target(uow, principal_id, agent_id)
        request = InputRequest(agent_id, TYPE_CHAT, store.chat_source_key(principal_id, key), text, wait=True,
                               principal_id=principal_id, expected_binding=expected_binding, steer=True)
        try:
            result = self.arbiter.submit(request)
        except NativeChatError as error:
            code = "idempotency_conflict" if error.code == "idempotency_conflict" else "validation_error"
            raise DomainError(code, error.message) from None
        if result.state in (RESULT_REJECTED, RESULT_DEFERRED):
            code, message = _REFUSALS.get(result.reason or "", ("operation_unavailable", "Chat cannot send to this agent now"))
            raise DomainError(code, message)
        with self.database.read() as uow:
            row = store.get_input(uow.connection, result.input_id)
            bindings = store.public_bindings(uow.connection, [row["session_id"]])
        # The binding is the input's own (its immutable session), also on a repeat after that session was replaced.
        binding = bindings.get(row["session_id"])
        return {"input": store.input_view(row, binding), "binding": binding, "created": result.created}

    def mark_read(self, principal_id: str, agent_id: str, sequence: int) -> dict:
        if type(sequence) is not int or sequence < 0:
            raise DomainError("validation_error", "A reply sequence is required")
        with self.database.transaction(operation="native_chat.read") as uow:
            self._target(uow, principal_id, agent_id)
            self.runtime.application.ensure_writes_open(uow)
            current = store.mark_read(uow.connection, agent_id, principal_id, sequence)
            newest = store.newest_sequence(uow.connection, agent_id)
        return {"last_read_sequence": current, "newest_sequence": newest}


__all__ = ["NativeChat"]
