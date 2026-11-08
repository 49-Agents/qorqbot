"""Owner-facing derived Chat for any registered agent, global or business.

Chat is a projection. It never launches, resumes or stops an agent, and opening it or switching agents starts nothing:
* the conversation is read from the transcript archive (the backend keeps it current whether or not Chat is open);
* delivery state comes from the shared input arbiter's durable inputs;
* "read" is a boundary on the agent's per-agent completion sequence, owned by the installation owner.

Every method re-validates, in its own transaction, that the caller is the installation owner, that the agent is an active
registered profile and, for a business agent, that its business is live and the owner holds it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import uuid

from qorqut_operations.contracts import DomainError, Principal
from qorqut.bootstrap.client import path_has_symlink
from qorqut.management.identity import installation_owner, require_protected_control
from qorqut.native_archive import CaptureError

from . import selection, store
from .adapters import CodexQueueAdapter, TerminalAdapter
from .attachment import AttachmentService
from .arbiter import AgentLocks, InputArbiter, TERMINAL_COMMAND
from .captures import Indexer
from .contracts import (InputRequest, NativeChatError, PROVIDER_RUNTIMES, PROVIDERS, RESULT_DEFERRED, RESULT_REJECTED,
                        TYPE_CHAT)
from .probe import (BINDING, ProbeResult, REASON_CAPTURE_DISABLED, REASON_CAPTURE_NOT_READY, REASON_LIVE, REASON_UNSUPPORTED,
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
# A read that was not handed the agent's selection reads it itself.
_UNREAD = object()
# The commands the owner may type in an agent's own terminal; Chat names them while the agent is offline. Any supported CLI
# connects; the profile's runtime is only the default, listed first.
LAUNCH_COMMANDS = {"codex": "codex", "claude-code": "claude"}


def launch_commands(profile) -> list[str]:
    preferred = store.profile_runtime(profile)
    ordered = sorted(LAUNCH_COMMANDS, key=lambda runtime: runtime != preferred)
    return [LAUNCH_COMMANDS[runtime] for runtime in ordered]

_REFUSALS = {
    "terminal_offline": ("terminal_offline", "Start or resume the agent in its terminal first"),
    "capture_disabled": ("capture_disabled", "Transcript capture is off for this agent, so Chat has no conversation to show"),
    "capture_not_ready": ("capture_not_ready", "Transcript capture is on but this session has not published its transcript yet; send again shortly"),
    "unsupported_target": ("operation_unavailable", "Chat needs this agent's native session on the host"),
    "unsupported_runtime": ("unsupported_runtime", "Chat supports Claude Code and Codex agents only"),
    "stale_session": ("terminal_changed", "The terminal conversation changed; send again"),
    "agent_unavailable": ("not_found", "Select an active registered agent"),
    TERMINAL_COMMAND: ("terminal_required", "Codex commands run only in the agent's Terminal. Open Terminal and run it there."),
}


class NativeChat:
    """The runtime's Chat facade: owner-facing reads and sends, the shared arbiter, and the backend lifecycle."""

    def __init__(self, runtime, *, probe: SessionProbe, registry: SessionRegistry, indexer: Indexer, arbiter: InputArbiter,
                 backend: NativeBackend, reader: ConversationReader | None = None):
        self.runtime, self.probe, self.registry, self.indexer = runtime, probe, registry, indexer
        self.arbiter, self.backend, self.reader = arbiter, backend, reader or ConversationReader()
        self.transcripts = ArchiveTranscripts(runtime, indexer)
        # One instance for the life of the runtime, on the same per-agent locks as the arbiter and the backend, so Chat sends,
        # selection, reconnect, raw terminal input and controller lifecycle are never driven by two threads at once.
        self.attachment = AttachmentService(runtime, probe, getattr(arbiter, "locks", None) or AgentLocks())
        self.backend.follow_terminal = self.follow_terminal

    @classmethod
    def build(cls, runtime, *, probe: SessionProbe | None = None, adapters=None, archive_factory=None,
              interval: float = 1.0) -> "NativeChat":
        """The default object graph of one host runtime. The terminal backend is read late: it is attached after construction."""
        def terminal_backend():
            return getattr(getattr(runtime, "terminal_control", None), "backend", None)

        def selected_reader(profile):
            with runtime.database.read() as uow:
                agent_id = profile["agent_id"]
                return selection.selected(uow.connection, agent_id), store.live_session(uow.connection, agent_id)

        locks = AgentLocks()
        probe = probe or SessionProbe(terminal_backend, selected_reader=selected_reader)
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
        """Any active supported global agent: its exact saved profile row, never a name or navigation pin. A business-home
        identity is historical and never a Chat target, whatever its stored status."""
        principal = uow.repository("principals").get(principal_id)
        if not principal or principal["kind"] != "human" or not principal["active"] or installation_owner(uow) != principal_id:
            raise DomainError("forbidden", "Agent chat is for the installation owner")
        profile = store.active_profile(uow.connection, agent_id, self.runtime.paths)
        if profile is None:
            raise DomainError("not_found", "Select an active registered agent")
        return profile

    # ----- reads -----------------------------------------------------------------------------------------------------

    def session_view(self, profile: dict, persisted: dict | None, observed: ProbeResult | None = None,
                     chosen=_UNREAD) -> dict:
        """The session as the composer needs it. Observing starts nothing and writes nothing: ``persisted`` is the agent's open
        binding row as the caller read it, and ``binding`` is supplied only when a fresh observation matches exactly that row.
        A composer may send only to a captured conversation whose exact binding the backend observer has already recorded; until
        then the view says it is connecting.

        The view follows the terminal conversation adopted by the observer (``chosen``, read here when not supplied).
        During a switch the composer waits for adoption instead of retargeting an already queued input."""
        observed = observed or self.probe.observe(profile)
        if chosen is _UNREAD:
            with self.database.read() as uow:
                chosen = selection.selected(uow.connection, profile["agent_id"])
        observation = observed.observation
        live = observed.reason == REASON_LIVE
        relation = selection.relation(chosen, observation) if live else None
        other = observed.candidate or (observation if relation == selection.CANDIDATE else None)
        candidate = ({"provider": other.provider, "conversation_id": other.conversation_id, "transport": other.transport}
                     if other is not None else None)
        pending = relation == selection.PENDING
        if relation in (selection.CANDIDATE, selection.PENDING):
            live = False
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
        elif pending:
            reason, detail = selection.REASON_PENDING, "Connecting to the conversation in Terminal…"
        elif candidate is not None:
            reason = selection.REASON_CANDIDATE if chosen is not None else selection.REASON_NO_SELECTION
            detail = "Connecting to the conversation in Terminal…"
        else:
            reason = observed.reason
            detail = {REASON_UNSUPPORTED: "Chat needs this agent's native session on the host",
                      REASON_UNSUPPORTED_RUNTIME: "Chat supports Claude Code and Codex agents only"}.get(
                reason, "Start or resume the agent in its terminal")
        return {"state": "live" if live else "offline", "reason": reason, "can_send": sendable,
                "working": bool(captured and observation.working), "provider": observation.provider if live else None,
                "conversation_id": observation.conversation_id if live else None,
                "transport": observation.transport if live else None, "binding": binding, "detail": detail,
                "selection": chosen.public() if chosen is not None else None, "candidate": candidate,
                "launch_commands": launch_commands(profile)}

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
            chosen = selection.selected(conn, agent_id)
            inputs = store.recent_inputs(conn, agent_id, 30)
            bindings = store.public_bindings(conn, [row["session_id"] for row in inputs])
            last_read = store.last_read(conn, agent_id, principal_id)
        view = self.session_view(profile, persisted, chosen=chosen)
        default = self._default(captures, latest, chosen)
        selected = default if pair is None else self._explicit(captures, pair)
        entries, older, info, rendered = [], None, None, 0
        if selected is not None:
            entries, older, info, rendered = self._entries(principal_id, profile, selected, before, limit)
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
            chosen = selection.selected(uow.connection, agent_id)
        selected = self._default(captures, latest, chosen) if pair is None else self._explicit(captures, pair)
        if selected is None:
            return {"entries": [], "older": None, "rendered_boundary": 0}
        entries, older, _, rendered = self._entries(principal_id, profile, selected, before, limit)
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
    def _default(cls, captures: list[dict], latest: dict | None, chosen=None):
        """The selected conversation, by its exact provider and id: never another capture, however recent. Only an agent with
        no selection yet (history that predates it) shows the conversation of its newest binding when it was captured,
        otherwise the most recently updated capture, so that old history stays readable."""
        if chosen is not None:
            return next((c for c in captures if chosen.matches(c["provider"], c["conversation_id"])), None)
        if latest is not None:
            found = next((c for c in captures if cls._same(c, latest)), None)
            if found is not None:
                return found
        return max(captures, key=lambda c: c["updated_at"], default=None)

    def _entries(self, principal_id, profile, capture, before, limit):
        """One page of the selected conversation, with the facts that belong to exactly the entries it returns: the verified
        provenance of the system markers on it, the stored outgoing agent messages its notices name, the owner's current view of
        the references its presentations name, the durable completion bound to each assistant entry, and
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
                                                 [entry.marker for entry in window.entries],
                                                 (capture["transcript_path"], receipt.source_path))
            replies = {row["raw_offset"]: row for row in store.replies_at(
                uow.connection, agent_id, provider, conversation, [entry.offset for entry in window.entries])}
            sends = store.sent_messages(uow.connection, agent_id,
                                        [identity for entry in window.entries for identity in entry.receipts])
            shown = visible(window.entries, replies, sends)
            named = [reference for entry, _ in shown if entry.references for reference in entry.references]
            user_input = getattr(self.runtime, "user_input", None)
            references = user_input.owner_references(principal_id, named, uow) if named and user_input is not None else {}
        rendered = max((row["completion_sequence"] for entry, row in shown if row is not None and entry.speaker == SPEAKER_AGENT), default=0)
        return ([entry_view(entry, lineage, markers, row, sends, references) for entry, row in shown],
                window.older, info, rendered)

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

    def upload(self, principal_id, agent_id, key, expected_binding, name, data):
        from . import uploads
        uploads.validate(key, name, data)
        with self.runtime.application.writer_admission.writer(), self.arbiter.locks(agent_id):
            with self.database.read() as uow:
                profile = self._target(uow, principal_id, agent_id)
                self.runtime.application.ensure_writes_open(uow)
                persisted = store.live_session(uow.connection, agent_id)
            session = self.session_view(profile, persisted)
            if not session["can_send"]:
                raise DomainError("terminal_offline", "Start the agent in Terminal before sending files")
            if not expected_binding or session["binding"]["binding_id"] != expected_binding:
                raise DomainError("terminal_changed", "The agent conversation changed; send again")
            return uploads.save(self.runtime.root, profile, principal_id, expected_binding, key, name, data)

    def thread_upload_target(self, principal_id):
        from qorqut.management.identity import private_participants
        with self.database.read() as uow:
            agent_id = private_participants(uow)[1]
            return self._target(uow, principal_id, agent_id)["agent_id"]

    def upload_thread(self, principal_id, agent_id, key, name, data):
        """Owner Task attachments go to the configured Ordubashy, including offline.

        Saving is not sending: normal thread permissions still govern the comment.
        Recheck the coordinator chosen before reading the upload, never re-aim it.
        """
        from qorqut.management.identity import private_participants
        from . import uploads
        uploads.validate(key, name, data)
        with self.runtime.application.writer_admission.writer(), self.arbiter.locks(agent_id):
            with self.database.read() as uow:
                profile = self._target(uow, principal_id, agent_id)
                self.runtime.application.ensure_writes_open(uow)
                if private_participants(uow)[1] != agent_id:
                    raise DomainError("revision_conflict", "The attachment recipient changed; try again")
            return uploads.save(self.runtime.root, profile, principal_id, "thread", key, name, data)

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

    def attachment_state(self, principal_id: str, agent_id: str) -> dict:
        """The shared selected-conversation attachment of this agent's terminal, for the Chat surface. Starts nothing."""
        with self.database.read() as uow:
            self._target(uow, principal_id, agent_id)
            require_protected_control(uow, principal_id, "human", [agent_id])
        return self.attachment.state(agent_id)

    def follow_terminal(self, profile: dict) -> None:
        """Adopt the terminal's verified current conversation through the existing binding transaction."""
        observed = self.probe.observe(profile)
        if observed.reason != REASON_LIVE:
            return
        with self.database.read() as uow:
            current = selection.selected(uow.connection, profile["agent_id"])
            owner = installation_owner(uow)
        live = observed.observation
        if owner is None or current is not None and current.holds(live):
            return
        try:
            self.select(owner, profile["agent_id"], live.provider, live.conversation_id,
                        current.generation if current is not None else 0)
        except DomainError:
            # A switch or an unfinished hook is retried by the next observer pass.
            LOG.debug("Terminal conversation adoption deferred for %s", profile["agent_id"], exc_info=True)

    def select(self, principal_id: str, agent_id: str, provider, conversation, expected_generation) -> dict:
        """The installation owner explicitly selects the native conversation this agent's Chat and Terminal show.

        The observer also uses this transaction to follow a verified terminal conversation. ``expected_generation`` is the
        owner generation the caller saw (0 when none exists). The named conversation must be the one the agent's native client
        is running right now, proved here by the same trusted probe the binding uses (hook marker, live process with its start
        ticks, registered pane or held thread-writer lock): an id from the caller is never proof. The change goes through the
        ordinary binding transaction with owner-selection authority; history, captures and append-only identity rows are
        untouched, and a waiting input of the earlier binding is settled by the registry exactly as for any binding change."""
        if provider not in PROVIDERS:
            raise DomainError("validation_error", "provider must be claude or codex")
        try:
            conversation = str(uuid.UUID(conversation))
        except (ValueError, TypeError, AttributeError):
            raise DomainError("validation_error", "conversation_id must be a native conversation id") from None
        if type(expected_generation) is not int or expected_generation < 0:
            raise DomainError("validation_error", "expected_generation is required")
        with self.runtime.application.writer_admission.writer(), self.arbiter.locks(agent_id):
            with self.database.read() as uow:
                profile = self._target(uow, principal_id, agent_id)
                require_protected_control(uow, principal_id, "human", [agent_id])
                self.runtime.application.ensure_writes_open(uow)
                current = selection.selected(uow.connection, agent_id)
            # Any supported runtime the terminal verifiably runs may be selected; the profile runtime only says what a
            # fresh start launches, so switching the terminal between Claude and Codex is followed like any other change.
            if (current.generation if current is not None else 0) != expected_generation:
                raise DomainError("revision_conflict", "The selected conversation changed; reload and try again")
            observed = self.probe.observe(profile)
            live = observed.observation if observed.reason == REASON_LIVE else None
            if live is None or live.provider != provider or live.conversation_id != conversation:
                raise DomainError("not_found", "The terminal conversation is still connecting")
            if current is not None and current.holds(live):
                return {"selection": current.public(), "changed": False}
            marker = self._candidate_marker(profile, agent_id, conversation, PROVIDER_RUNTIMES[provider])
            from qorqut.bootstrap.native_binding import NativeBindingAdapter
            adapter = getattr(self.runtime, "native_binding_adapter", None) or NativeBindingAdapter(self.runtime)
            try:
                binding, old_state = adapter.verify(agent_id, marker["registration_id"], conversation,
                                                    previous=current.binding if current is not None else None)
            except DomainError:
                raise DomainError("not_found", "That conversation is not verifiably running in this agent's terminal") from None
            if binding.get("session_id") != conversation or binding.get("runtime") != PROVIDER_RUNTIMES[provider]:
                raise DomainError("not_found", "That conversation is not verifiably running in this agent's terminal")
            confirmed = self.probe.observe(profile)
            latest = confirmed.observation if confirmed.reason == REASON_LIVE else None
            if (latest is None or latest.provider != provider or latest.conversation_id != conversation
                    or latest.pid != binding.get("pid") or latest.started != binding.get("started")
                    or (latest.owner or f"{latest.pid}:{latest.started}") != binding.get("owner")):
                raise DomainError("revision_conflict", "The terminal conversation changed; reconnecting")
            context = self.runtime.agent_context(Principal(agent_id, "agent"))
            self.runtime.inbox.start_session(context, marker["registration_id"], conversation,
                                             binding.get("event") or "startup", binding=binding, old_state=old_state,
                                             expected_generation=expected_generation, owner_selection=True)
            with self.database.transaction(operation="native_chat.select") as uow:
                self.runtime.application.ensure_writes_open(uow)
                store.bump(uow.connection, agent_id)
                chosen = selection.selected(uow.connection, agent_id)
        self.backend.wake()
        return {"selection": chosen.public(), "changed": True}

    @staticmethod
    def _candidate_marker(profile: dict, agent_id: str, conversation: str, runtime: str) -> dict:
        """The trusted native-binding observation of the named conversation, or ``not_found``. It names the registration the
        probe then verifies against the live process; the file is a claim, not proof."""
        try:
            path = Path(profile["workspace"]) / BINDING
            if path_has_symlink(path) or not path.is_file() or path.stat().st_size > 16384:
                raise ValueError("no marker")
            marker = json.loads(path.read_text())
            if (not isinstance(marker, dict) or marker.get("session_id") != conversation or marker.get("agent_id") != agent_id
                    or marker.get("runtime") != runtime or marker.get("workspace") != profile["workspace"]
                    or not isinstance(marker.get("registration_id"), str)):
                raise ValueError("another conversation")
            return marker
        except (OSError, ValueError, TypeError):
            raise DomainError("not_found", "That conversation is not the one running in this agent's terminal") from None

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
