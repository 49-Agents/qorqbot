"""Owner-facing derived Chat for any registered agent, global or project.

Chat is a projection. It never launches, resumes or stops an agent, and opening it or switching agents starts nothing:
(the one exception, in managed mode, is an owner message to an agent with no session open: sending it starts that agent's
headless session, see ``send``; when the installation runs its agents in terminals, Chat never starts anything and a message
reaches only the session its pin names)
* the conversation is read from the transcript archive (the backend keeps it current whether or not Chat is open);
* delivery state comes from the shared input arbiter's durable inputs;
* "read" is a boundary on the agent's per-agent completion sequence, owned by the installation owner.

Every method re-validates, in its own transaction, that the caller is the installation owner, that the agent is an active
registered profile.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import uuid

from qorqut_operations.contracts import DomainError, Principal
from qorqut import account_state
from qorqut.bootstrap.client import path_has_symlink
from qorqut.management.identity import installation_owner, require_protected_control
from qorqut.native_archive import CaptureError

from qorqut.managed_runtime import mode

from . import selection, store
from .adapters import ManagedAdapter, TerminalAdapter
from .account_switch import AccountSwitch
from .attachment import AttachmentService
from .arbiter import AgentLocks, InputArbiter, MANAGED_COMMAND, SESSION_CLOSED, TERMINAL_OFFLINE
from .commands import unrunnable
from .captures import Indexer
from .opener import SessionOpener, WAKE_TEXT
from .contracts import (InputRequest, NativeChatError, PROVIDER_RUNTIMES, PROVIDERS, RESULT_DEFERRED, RESULT_REJECTED, RUNTIME_PROVIDERS,
                        TYPE_CHAT)
from .probe import (BINDING, ProbeResult, REASON_CAPTURE_DISABLED, REASON_CAPTURE_NOT_READY, REASON_LIVE, REASON_NO_MARKER,
                    REASON_PROCESS_GONE, REASON_SESSION_END, REASON_UNSUPPORTED, REASON_UNSUPPORTED_RUNTIME, SessionProbe)
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
# The probe's reasons for "no session": a managed agent in one of them has an open host (still connecting) or a closed session.
_NO_SESSION = frozenset({REASON_NO_MARKER, REASON_PROCESS_GONE, REASON_SESSION_END})

# The one command Chat names while an agent's terminal is not running (Claude Code only runs in terminals).
LAUNCH_COMMANDS = ("claude",)


def launch_commands(profile) -> list[str]:
    return list(LAUNCH_COMMANDS)


_REFUSALS = {
    "capture_disabled": ("capture_disabled", "Transcript capture is off for this agent, so Chat has no conversation to show"),
    "capture_not_ready": ("capture_not_ready", "Transcript capture is on but this session has not published its transcript yet; send again shortly"),
    "unsupported_target": ("operation_unavailable", "Chat needs this agent's native session on the host"),
    "unsupported_runtime": ("unsupported_runtime", "Chat supports Claude Code and Codex agents only"),
    "stale_session": ("terminal_changed", "The agent's conversation changed; send again"),
    "agent_unavailable": ("not_found", "Select an active registered agent"),
    # Qorqbot runs every agent without a terminal: the owner never has to open one.
    MANAGED_COMMAND: ("managed_command", "This command isn't available when Qorqbot runs the agent"),
    SESSION_CLOSED: ("session_closed", "This agent's session is not open. Send the message again to start it"),
}
# What differs when the installation runs its agents in terminals: nothing is opened for a message, so a closed agent is offline.
_TERMINAL_REFUSALS = {
    TERMINAL_OFFLINE: ("terminal_offline", "Start or resume the agent in its terminal first"),
    "stale_session": ("terminal_changed", "The terminal conversation changed; send again"),
}

# How long an owner message to a closed agent waits in all: for its host to be ready (another caller's open included) and for
# its session to bind. One shared budget (the host's own start-up limit is the same 45 seconds); the host keeps starting after this
# call gives up, and the owner's retry with the same key finds it.
OPEN_TOTAL_SECONDS = 45.0
OPEN_POLL_SECONDS = 0.25
# A fresh session is sent ``/role`` by its host, which makes Claude announce the session. When the session is still not bound this
# long after the host was ready (the role prompt was blocked by a hook, which writes nothing), the owner's own message is delivered
# to the host directly: it is the message that makes a resumed session announce itself too.
ROLE_GRACE_SECONDS = 6.0
# How long a pinless retry of an open that gave up is still recognised by its key (the session may have come up since).
OPEN_RECORD_SECONDS = 600.0
# What ``ManagedRuntime.ensure_open`` can fail with, as the refusal the owner sees (nothing was stored or sent). The message is
# fixed here: a launcher's own detail may carry paths. ``code`` is what the dashboard's refusal table keys on.
_OPEN_REFUSALS = {
    "cli_unavailable": ("cli_unavailable", "The agent's client is not installed. Download it under Settings > Agents first"),
    "cli_not_signed_in": ("cli_not_signed_in", "The agent's client is not signed in. Sign in under Settings > Agents first"),
    "resume_failed": ("resume_failed", "The earlier conversation could not be resumed, so no session was started"),
    "resume_unavailable": ("resume_unavailable", "The earlier conversation can't be continued because its file is missing. "
                                                 "Start a new conversation to carry on without it."),
    "open_timeout": ("open_timeout", "The session did not become ready in time; send again"),
    "open_busy": ("open_busy", "Several sessions are starting right now. Try again in a moment"),
    "host_unverified": ("host_unverified", "A session of this agent could not be verified; try again shortly"),
    "session_opening": ("session_opening", "A session is being opened for this agent; try again in a moment"),
    "session_closed": ("session_closed", "This agent's session is not open. Send the message again to start it"),
    "session_starting": ("binding_not_ready", "The session is still starting; try again in a moment"),
}
# The refusals whose own message is written for the owner (the manager words them in plain language, naming the agent, its client and what to do): every other message is Chat's fixed one.
_OWN_MESSAGE = frozenset({"cli_unavailable", "cli_not_signed_in", "resume_unavailable"})
_OPEN_FAILED = ("driver_unavailable", "The session could not be started")
# The fields of the managed state Chat shows (the dashboard reads exactly these under ``session.managed``).
MANAGED_FIELDS = ("open", "phase", "activity", "turn_started_at", "can_stop", "pending", "last_turn_status", "turn_error", "turn_error_kind", "account_problem", "closed_reason", "closed_text", "off")
# An answer to a permission request, or to a question: ``{"answers": {<question text>: <answer text>}}``.
ANSWER_DECISIONS = ("allow", "deny")


class NativeChat:
    """The runtime's Chat facade: owner-facing reads and sends, the shared arbiter, and the backend lifecycle."""

    def __init__(self, runtime, *, probe: SessionProbe, registry: SessionRegistry, indexer: Indexer, arbiter: InputArbiter,
                 backend: NativeBackend, reader: ConversationReader | None = None, managed=None):
        self.runtime, self.probe, self.registry, self.indexer = runtime, probe, registry, indexer
        self.arbiter, self.backend, self.reader = arbiter, backend, reader or ConversationReader()
        # The managed runtime (``managed_runtime.manager.ManagedRuntime``), used duck-typed. None while it is not composed: an agent
        # then has no session that can be opened.
        self.managed = managed
        self.transcripts = ArchiveTranscripts(runtime, indexer)
        # The per-agent locks of the arbiter and the backend, so Chat sends, selection and session lifecycle are never driven by two
        # threads at once.
        self.locks = getattr(arbiter, "locks", None) or AgentLocks()
        # The selected-conversation attachment of the agent's terminal (Terminals mode only; it refuses in managed mode).
        self.attachment = AttachmentService(runtime, probe, self.locks)
        # The one opener of an agent's session, for the owner's message and for every other input source (``opener.py``);
        # the arbiter wakes an asleep agent through it, and so do the nudge pass, reminders and Telegram's retry.
        self.opener = SessionOpener(runtime, managed_of=lambda: self.managed, probe=probe, registry=registry, locks=self.locks,
                                    wake=lambda: self.backend.wake(), adopt=lambda profile: self.adopt_session(profile), failure=self._open_failure,
                                    limits=lambda: (OPEN_TOTAL_SECONDS, OPEN_POLL_SECONDS, ROLE_GRACE_SECONDS, OPEN_RECORD_SECONDS))
        self.arbiter.opener = self.opener
        self.backend.adopt = self.adopt_session
        # What follows a change of account: the sessions of the product are restarted, one agent can move to the other product.
        self.account_switch = AccountSwitch(self)

    @classmethod
    def build(cls, runtime, *, probe: SessionProbe | None = None, adapter=None, archive_factory=None,
              interval: float = 1.0, managed=None) -> "NativeChat":
        """The default object graph of one host runtime, composed for both ways of running agents at once; which one applies is
        asked at call time (``mode``). ``managed`` is the managed runtime: the probe verifies an agent's session through it, the
        ``managed`` adapter (``adapter``, when given) delivers to it, and the backend pass reads its host state. The terminal
        backend is read late: it is attached to the runtime after construction."""
        def terminal_backend():
            return getattr(getattr(runtime, "terminal_control", None), "backend", None)

        locks = AgentLocks()
        probe = probe or SessionProbe(managed=managed, terminal_backend=terminal_backend,
                                      terminal_mode=lambda: mode.is_terminal(runtime.paths))
        registry = SessionRegistry(runtime)
        indexer = Indexer(runtime, archive_factory=archive_factory)
        adapters = {"managed": adapter or ManagedAdapter(managed), "terminal": TerminalAdapter(terminal_backend)}
        arbiter = InputArbiter(runtime, probe, registry, adapters, locks=locks)
        backend = NativeBackend(runtime, probe, registry, indexer, arbiter, locks, interval=interval, managed=managed)
        return cls(runtime, probe=probe, registry=registry, indexer=indexer, arbiter=arbiter, backend=backend, managed=managed)

    @property
    def database(self):
        return self.runtime.database

    def _terminals(self) -> bool:
        """Whether this installation runs its agents in terminals now. Asked every time: the owner can switch while the service runs."""
        return mode.is_terminal(getattr(self.runtime, "paths", None))

    # ----- lifetime --------------------------------------------------------------------------------------------------

    def start(self) -> None:
        # The managed runtime starts with Chat. It spawns nothing: a host exists only because an owner message opened one, and a
        # running one is found again when asked. Nothing stops hosts: they outlive the service on purpose.
        if self.managed is not None:
            self.managed.start()
            from qorqut import agent_cli_sign_in
            agent_cli_sign_in.set_completion_handler(self.account_switch.after_sign_in)
        self.backend.start()

    def stop_observer(self, timeout: float = 5) -> None:
        self.backend.stop_observer(timeout)

    # ----- authorization ---------------------------------------------------------------------------------------------

    def _target(self, uow, principal_id: str, agent_id: str) -> dict:
        """Any active supported global agent: its exact saved profile row, never a name or navigation pin."""
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

        The view follows the conversation adopted by the observer (``chosen``, read here when not supplied). During a switch the
        composer waits for adoption instead of retargeting an already queued input.

        The view adds ``managed`` (the host's phase, activity and pending questions). An agent with no session open reports
        ``can_send`` with no binding: sending a message is what starts one."""
        if self._terminals():
            return self._terminal_view(profile, persisted, observed, chosen)
        observed = observed or self.probe.observe(profile)
        if chosen is _UNREAD:
            with self.database.read() as uow:
                chosen = selection.selected(uow.connection, profile["agent_id"])
        observation = observed.observation
        live = observed.reason == REASON_LIVE
        relation = selection.relation(chosen, observation) if live else None
        other = observation if relation == selection.CANDIDATE else None
        candidate = ({"provider": other.provider, "conversation_id": other.conversation_id, "transport": other.transport}
                     if other is not None else None)
        pending = relation == selection.PENDING
        if relation in (selection.CANDIDATE, selection.PENDING):
            live = False
        bound = bool(live and persisted is not None and store.same_session(persisted, observation))
        binding = store.binding_of(persisted).public() if bound else None
        captured = bool(live and observation.capture)
        sendable = captured and bound
        summary = self._managed_summary(profile)
        state, can_send = "live" if live else "offline", sendable
        if live:
            connected = "Session open"
            if sendable:
                reason, detail = REASON_LIVE, "Working…" if observation.working else connected
            elif captured:
                reason, detail = REASON_BINDING_NOT_READY, f"{connected}; connecting to this session"
            elif observation.capture_enabled:
                # Capture is on in the registration; the client's hook has not published this session's transcript yet.
                reason, detail = REASON_CAPTURE_NOT_READY, f"{connected}; waiting for the session's transcript to be published"
            else:
                reason, detail = REASON_CAPTURE_DISABLED, "Transcript capture is off for this agent, so Chat has no conversation to show"
        elif pending:
            reason, detail = selection.REASON_PENDING, "Connecting to the conversation…"
        elif candidate is not None:
            reason = selection.REASON_CANDIDATE if chosen is not None else selection.REASON_NO_SELECTION
            detail = "Connecting to the conversation…"
        elif observed.reason in _NO_SESSION:
            if summary["open"]:
                # The host runs but its session is not bound yet (the client's hook has not published it): connecting, not closed.
                state, reason, detail = "live", REASON_BINDING_NOT_READY, "Starting the session…"
            else:
                # Closed is the steady state of an agent nobody opened: nothing is wrong, and a message opens it.
                reason, detail, can_send = SESSION_CLOSED, "Sending a message starts a session", True
        else:
            reason = observed.reason
            detail = {REASON_UNSUPPORTED: "Chat needs this agent's native session on the host",
                      REASON_UNSUPPORTED_RUNTIME: "Chat supports Claude Code and Codex agents only"}.get(
                reason, "Start the session by sending a message")
        view = {"state": state, "reason": reason, "can_send": can_send,
                "working": bool(captured and observation.working), "provider": observation.provider if live else None,
                "conversation_id": observation.conversation_id if live else None,
                "transport": observation.transport if live else None, "binding": binding, "detail": detail,
                "selection": chosen.public() if chosen is not None else None, "candidate": candidate, "managed": summary}
        return view

    def _terminal_view(self, profile: dict, persisted: dict | None, observed: ProbeResult | None, chosen) -> dict:
        """``session_view`` for an installation whose agents run in terminals: the view of a terminal agent as it always was.
        ``execution`` is ``terminal`` and ``launch_commands`` names what the owner types to start the agent; there is no
        ``managed`` summary. An agent whose terminal is not running is offline and cannot be sent to: nothing opens it."""
        observed = observed or self.probe.observe(profile)
        if chosen is _UNREAD:
            with self.database.read() as uow:
                chosen = selection.selected(uow.connection, profile["agent_id"])
        observation = observed.observation
        live = observed.reason == REASON_LIVE
        relation = selection.relation(chosen, observation) if live else None
        other = observation if relation == selection.CANDIDATE else None
        candidate = ({"provider": other.provider, "conversation_id": other.conversation_id, "transport": other.transport}
                     if other is not None else None)
        pending = relation == selection.PENDING
        if relation in (selection.CANDIDATE, selection.PENDING):
            live = False
        bound = bool(live and persisted is not None and store.same_session(persisted, observation))
        binding = store.binding_of(persisted).public() if bound else None
        captured = bool(live and observation.capture)
        sendable = captured and bound
        state = "live" if live else "offline"
        if live:
            connected = "Terminal connected"
            if sendable:
                reason, detail = REASON_LIVE, "Working…" if observation.working else connected
            elif captured:
                reason, detail = REASON_BINDING_NOT_READY, f"{connected}; connecting to this session"
            elif observation.capture_enabled:
                reason, detail = REASON_CAPTURE_NOT_READY, f"{connected}; waiting for the session's transcript to be published"
            else:
                reason, detail = REASON_CAPTURE_DISABLED, "Transcript capture is off for this agent, so Chat has no conversation to show"
        elif pending:
            reason, detail = selection.REASON_PENDING, "Connecting to the conversation in Terminal…"
        elif candidate is not None:
            reason = selection.REASON_CANDIDATE if chosen is not None else selection.REASON_NO_SELECTION
            detail = "Connecting to the conversation in Terminal…"
        else:
            reason = observed.reason
            if reason != REASON_UNSUPPORTED and self._codex_in_terminal(profile):
                reason = REASON_UNSUPPORTED_RUNTIME          # nothing would start it: the owner reads why instead of "start it"
            detail = {REASON_UNSUPPORTED: "Chat needs this agent's native session on the host",
                      REASON_UNSUPPORTED_RUNTIME: "Only Claude Code agents can run in terminals"}.get(
                reason, "Start or resume the agent in its terminal")
        return {"state": state, "reason": reason, "can_send": sendable,
                "working": bool(captured and observation.working), "provider": observation.provider if live else None,
                "conversation_id": observation.conversation_id if live else None,
                "transport": observation.transport if live else None, "binding": binding, "detail": detail,
                "selection": chosen.public(terminal=True) if chosen is not None else None, "candidate": candidate,
                "launch_commands": launch_commands(profile), "execution": mode.TERMINAL}

    def _codex_in_terminal(self, profile: dict) -> bool:
        """Whether a start of this agent would be refused because it runs on Codex. The owner's choice in Settings decides, read
        from its one small file; only an agent with no choice whose profile names Codex needs the full rule (which client is
        ready), so the ordinary agent's view costs no status read."""
        from qorqut.managed_runtime import options
        try:
            stored = options.read(getattr(self.runtime, "paths", None), profile["agent_id"]).client
        except Exception:  # noqa: BLE001 - a choice that cannot be read does not make the agent a Codex one
            return False
        if stored is not None:
            return stored == "codex"
        return options.profile_client(profile) == "codex" and self._client_of(profile) == "codex"

    def _managed_summary(self, profile: dict) -> dict:
        """What the managed host reports about this agent's session, for Chat: ``open``, ``phase``, ``activity``,
        ``turn_started_at``, ``can_stop``, ``pending`` (``request_id``, ``kind``, ``tool``, ``input_summary``, ``questions``)
        and ``last_turn_status``. A runtime that is not composed or cannot be read is a closed session."""
        found = None
        if self.managed is not None:
            try:
                found = self.managed.snapshot(profile["agent_id"])
            except Exception:
                LOG.debug("Managed session state is unavailable for %s", profile["agent_id"], exc_info=True)
        found = found if isinstance(found, dict) else {}
        summary = {name: found.get(name) for name in MANAGED_FIELDS}
        summary["open"] = bool(found.get("open"))
        summary["can_stop"] = bool(found.get("can_stop"))
        summary["pending"] = [dict(item) for item in found.get("pending") or () if isinstance(item, dict)]
        summary["account_problem"] = self._account_problem(found)
        # ``off``: the owner ended this agent's session (or Ordubashy killed the worker) and nothing but the owner's own input
        # starts it again. A closed session that is not off is asleep: anything that reaches the agent opens it.
        summary["off"] = self._is_off(profile["agent_id"])
        return summary

    @staticmethod
    def _account_problem(found: dict):
        """The notice for a session whose last turn ended in a usage limit or a sign-in problem (``account_state.problem``), else
        None. It is the same in every chat of an agent on that product: ``managed_runtime.account_errors`` labels the turn, and the
        account and the other product's readiness come from the status Settings shows (kept a short while)."""
        kind, provider = found.get("turn_error_kind"), found.get("provider")
        if kind not in ("usage_limit", "sign_in") or provider not in ("claude", "codex"):
            return None
        try:
            return account_state.problem(provider, kind, found.get("turn_error_resets_at"), found.get("turn_error_resets_text"))
        except Exception:  # noqa: BLE001 - the plain error text is still shown
            LOG.debug("The account notice could not be composed", exc_info=True)
            return None

    def _is_off(self, agent_id: str) -> bool:
        try:
            return self.opener.is_off(agent_id)
        except Exception:  # noqa: BLE001 - a read of the view never fails because of this flag
            LOG.debug("The on/off state of %s is unavailable", agent_id, exc_info=True)
            return False

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
            default = self._default(captures, latest, chosen)
            selected = default if pair is None else self._explicit(captures, pair)
            continues = self._continues(uow, captures, selected)
        view = self.session_view(profile, persisted, chosen=chosen)
        entries, older, info, rendered = [], None, None, 0
        if selected is not None:
            entries, older, info, rendered = self._entries(principal_id, profile, selected, before, limit, continues)
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

    def _continues(self, uow, captures: list[dict], capture: dict | None) -> dict | None:
        """The conversation that a compaction closed to open this one (``compaction.py``), which Chat loads above it:
        ``{conversation_id, provider, at}``, or None. It is named only when it was captured for this agent under the same
        provider, which a compaction never changes: Chat asks for it by that pair, and ``_explicit`` refuses an uncaptured one.
        One read of the agent's compactions, on every state read; no archive is opened for it."""
        handoff = getattr(self.runtime, "compaction_handoff", None)
        if handoff is None or capture is None:
            return None
        link = handoff.continued(uow, capture["agent_id"], capture["conversation_id"])
        if link is None:
            return None
        earlier = {"conversation_id": link["conversation_id"], "provider": capture["provider"], "at": link["at"]}
        return earlier if any(self._same(c, earlier) for c in captures) else None

    def _entries(self, principal_id, profile, capture, before, limit, continues=None):
        """One page of the selected conversation, with the facts that belong to exactly the entries it returns: the verified
        provenance of the system markers on it, the stored outgoing agent messages its notices name, the owner's current view of
        the references its presentations name, the durable completion bound to each assistant entry, and
        ``rendered_boundary`` -- the newest completion sequence among the completed assistant entries this page actually returns
        after projection and pagination (0 when it returns none). A client acknowledges that value, never the agent's newest
        sequence, so a page that did not display a reply cannot acknowledge it. ``continues`` (``_continues``) is part of the
        conversation's info whatever the page holds, so a fresh conversation with nothing captured yet still points back."""
        info = {"conversation_id": capture["conversation_id"], "provider": capture["provider"],
                "complete": bool(capture["complete"]), "finalized": bool(capture["finalized"]), "error": capture["error_code"],
                "captured_length": capture["captured_length"], "continues": continues}
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
            profile = self._target(uow, principal_id, agent_id)
            counters = store.counters(uow.connection, agent_id)
            pending = store.waiting_inputs(uow.connection, agent_id)
        latest = counters["revision"]
        if cursor >= latest:
            return [], latest
        data = {"revision": latest, "newest_sequence": counters["completion_sequence"], "waiting": len(pending)}
        # The managed summary rides on the event, so Stop, the activity line and the request cards follow a turn without waiting for
        # the page read that every event also triggers. A terminal agent's event carries none.
        if not self._terminals():
            data["managed"] = self._managed_summary(profile)
        return [{"cursor": latest, "kind": "changed", "data": data}], latest

    # ----- owner actions ---------------------------------------------------------------------------------------------

    def upload(self, principal_id, agent_id, key, expected_binding, name, data):
        from . import uploads
        uploads.validate(key, name, data)
        expected_binding = self._session_for_upload(principal_id, agent_id, key, expected_binding)
        with self.runtime.application.writer_admission.writer(), self.arbiter.locks(agent_id):
            with self.database.read() as uow:
                profile = self._target(uow, principal_id, agent_id)
                self.runtime.application.ensure_writes_open(uow)
                persisted = store.live_session(uow.connection, agent_id)
            session = self.session_view(profile, persisted)
            if not session["can_send"] or session["binding"] is None:
                if session.get("execution") == mode.TERMINAL:
                    raise DomainError("terminal_offline", "Start the agent in Terminal before sending files")
                # A session is opened by a message; files are attached to a session that is open.
                closed = not (session.get("managed") or {}).get("open")
                raise DomainError("session_closed" if closed else "binding_not_ready",
                                  "Send a message to start the session, then attach files" if closed
                                  else "The session is still starting; try again in a moment")
            if not expected_binding or session["binding"]["binding_id"] != expected_binding:
                raise DomainError("terminal_changed", "The agent conversation changed; send again")
            saved = uploads.save(self.runtime.root, profile, principal_id, expected_binding, key, name, data)
            if session.get("execution") == mode.TERMINAL:
                return saved
            # The session the file was attached to: the pin a following send names (the same field the chat state uses), which the
            # page does not have yet when this upload is what opened the session.
            return {**saved, "binding_id": expected_binding}

    def _session_for_upload(self, principal_id, agent_id, key, expected_binding):
        """A file for an asleep agent is owner input like a message: it turns the agent on and opens its session (nothing is held
        while it opens), and the file is attached to the binding that comes up, whatever the page last saw. A session that is open
        keeps the binding the caller named, and so does every agent when the installation runs in terminals (nothing opens there)."""
        with self.database.read() as uow:
            profile = self._target(uow, principal_id, agent_id)
        if (self._terminals() or self.managed is None or self._managed_summary(profile)["open"]
                or not self._asleep(profile)):
            return expected_binding  # a host that is up but not bound yet is the caller's ``binding_not_ready``, as before
        return self.opener.open(profile, WAKE_TEXT, "upload:" + key)

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

    def send(self, principal_id: str, agent_id: str, text: str, key: str, expected_binding: str | None = None,
             new_conversation: bool = False) -> dict:
        """Admit one owner message for the exact native session ``expected_binding`` names and return at once; the backend types
        it when that same conversation's composer is ready. The pin is the ``binding_id`` the owner's page last saw. After the
        current authorization, an exact repeat (same key, text and pin) returns the original input even if its session has since
        ended; the same key with another text or pin conflicts; a new request for a pin that is no longer the live session is
        ``terminal_changed`` and admits nothing.

        In managed mode an agent with no session open is the one case without a pin: the message itself opens its session. It is checked
        (text, key, a menu-only command) before anything starts, the host is started and waited for, the session's binding is
        waited for, and the message is then admitted for that binding exactly as a pinned one is. An agent whose session is
        open still needs the pin. Opening Chat or switching agents never starts anything.

        ``new_conversation`` is the owner's explicit choice to start a fresh conversation for an agent whose earlier one
        cannot be resumed (``resume_unavailable``): meaningful only for an agent with no session open, without a pin; it is
        refused for any other send. A session is never silently replaced by a fresh conversation.

        When the installation runs its agents in terminals the pin is always required, nothing is opened or turned on, and an
        agent whose terminal is not running is ``terminal_offline``."""
        if not isinstance(key, str) or not 8 <= len(key) <= 120:
            raise DomainError("validation_error", "An idempotency key of 8-120 characters is required")
        if not isinstance(new_conversation, bool):
            raise DomainError("validation_error", "new_conversation is true or false")
        if new_conversation and expected_binding is not None:
            raise DomainError("validation_error", "A new conversation can only be started when no session is open")
        if expected_binding is not None and (not isinstance(expected_binding, str) or not 8 <= len(expected_binding) <= 120):
            raise DomainError("validation_error", "The binding_id of the session being written to is required")
        with self.database.read() as uow:
            profile = self._target(uow, principal_id, agent_id)
        terminal = self._terminals()
        if terminal:
            # Nothing is opened or woken for a message here: it goes to the live session its pin names, or it is refused.
            if new_conversation:
                raise DomainError("validation_error", "A new conversation can only be started for an agent Qorqbot runs")
            if expected_binding is None:
                raise DomainError("validation_error", "The binding_id of the session being written to is required")
        opened = False
        if expected_binding is not None and not new_conversation and not terminal and self._asleep(profile):
            # A pin names a session that has since closed (idle close, a restart): the agent is asleep, so this is the message that
            # starts it, exactly as one without a pin. A pin for a live session of another conversation is still refused below.
            expected_binding = None
        if expected_binding is None:
            expected_binding, opened = self._binding_for_open(profile, principal_id, text, key, new_conversation)
        elif new_conversation:
            raise DomainError("validation_error", "A new conversation can only be started when no session is open")
        elif not terminal:
            self.opener.turn_on(agent_id)  # owner input turns the agent on, whatever the session state
        request = InputRequest(agent_id, TYPE_CHAT, store.chat_source_key(principal_id, key), text, wait=True,
                               principal_id=principal_id, expected_binding=expected_binding, steer=True)
        try:
            result = self.arbiter.submit(request)
        except NativeChatError as error:
            code = "idempotency_conflict" if error.code == "idempotency_conflict" else "validation_error"
            raise DomainError(code, error.message) from None
        if result.state in (RESULT_REJECTED, RESULT_DEFERRED):
            refusals = {**_REFUSALS, **_TERMINAL_REFUSALS} if terminal else _REFUSALS
            code, message = refusals.get(result.reason or "", ("operation_unavailable", "Chat cannot send to this agent now"))
            raise DomainError(code, message)
        with self.database.read() as uow:
            row = store.get_input(uow.connection, result.input_id)
            bindings = store.public_bindings(uow.connection, [row["session_id"]])
        # The binding is the input's own (its immutable session), also on a repeat after that session was replaced.
        binding = bindings.get(row["session_id"])
        return {"input": store.input_view(row, binding), "binding": binding, "created": result.created}

    def _binding_for_open(self, profile: dict, principal_id: str, text: str, key: str, new_conversation: bool = False) -> tuple[str, bool]:
        """The binding a pinless message to an agent is admitted for, and whether this call opened the session.

        An exact repeat of a message already admitted names the session it was admitted for (the first response may have been
        lost), and nothing is opened. A live session needs its pin, except for the retry of the very message (same key) whose open
        gave up while its host kept starting: that one is admitted for the session that has come up. Otherwise the request is
        checked and, only then, the session is opened and its binding waited for: the agent's lock is held for each check, never
        across the wait, because the backend observer needs it to adopt the new conversation."""
        agent_id = profile["agent_id"]
        request = InputRequest(agent_id, TYPE_CHAT, store.chat_source_key(principal_id, key), text, wait=True,
                               principal_id=principal_id, steer=True)
        with self.arbiter.locks(agent_id):
            with self.database.read() as uow:
                prior = store.input_by_key(uow.connection, agent_id, request.source_key)
            if prior is not None:
                return prior["session_id"], False
            try:
                self.arbiter.validate(request)
            except NativeChatError as error:
                raise DomainError("validation_error", error.message) from None
            if unrunnable(self._client_of(profile), text) is not None:
                # Refused before anything starts: it could not run in the session that would be opened for it.
                raise DomainError(*_REFUSALS[MANAGED_COMMAND])
            live = self.registry.reconcile(profile, self.probe.observe(profile))
            if live.live and live.session is not None:
                if self._opened_by(agent_id, key):
                    return live.session["id"], True
                raise DomainError("validation_error", "The binding_id of the session being written to is required")
            if text.strip() == "/exit":
                raise DomainError("session_closed", "There is no open session to end")
        return self._open_session(profile, text, key, new_conversation), True

    def _client_of(self, profile: dict):
        """The client (``claude`` or ``codex``) a session opened now for this agent would run: the managed runtime's effective
        client, else the profile runtime's. Only used to say which commands could run in it."""
        try:
            if self.managed is not None:
                return self.managed.client_for(profile)
        except Exception:  # noqa: BLE001 - a check that cannot be made falls back to the profile
            LOG.debug("The client of %s could not be resolved", profile["agent_id"], exc_info=True)
        return RUNTIME_PROVIDERS.get(store.profile_runtime(profile))

    def _asleep(self, profile: dict) -> bool:
        """Whether this agent has no verified live session now (nothing opens or writes)."""
        with self.arbiter.locks(profile["agent_id"]):
            return not self.registry.reconcile(profile, self.probe.observe(profile)).live

    def _opened_by(self, agent_id: str, key: str) -> bool:
        return self.opener.opened_by(agent_id, key)

    def _open_session(self, profile: dict, text: str, key: str, new_conversation: bool = False) -> str:
        """Start (or find) this agent's host, get the owner's message to it, and wait until its session is bound with a
        transcript Chat can show. The binding id is the pin the message is then admitted for. ``DomainError`` with a refusal code
        when it cannot be opened; nothing was admitted then, and the host (if it started) keeps starting for the retry.

        The flow is the opener's (``SessionOpener.open``), shared with every other input source: one shared deadline
        (``OPEN_TOTAL_SECONDS``) covers the host's readiness, another caller's open it had to wait for, and the binding. The binding
        cannot appear before Claude has taken up a first message, so a fresh session is given ``ROLE_GRACE_SECONDS`` to bind from
        the ``/role`` its host sends, and when it does not or when the session was resumed (no role is sent) the owner's message is
        delivered to the host directly, with a uuid derived from the request. The input admitted afterwards carries the same
        uuid, which the host recognises as already taken: it is typed once, and a retry of the same request can neither send it
        twice nor open a second host. Opening turns the agent on."""
        return self.opener.open(profile, text, key, new_conversation)

    @staticmethod
    def _open_failure(error: DomainError) -> DomainError:
        code, message = _OPEN_REFUSALS.get(error.code, _OPEN_FAILED)
        if error.code in _OWN_MESSAGE and getattr(error, "message", None):
            message = error.message
        return DomainError(code, message, retryable=bool(error.retryable))

    # ----- managed session controls ----------------------------------------------------------------------------------

    def _managed_session(self, principal_id: str, agent_id: str, expected_binding, *, pinned: bool) -> tuple[dict, dict | None]:
        """Authorize a control of an agent's session and return its profile and its verified binding view. Call with the
        agent's lock held. When ``pinned`` the pin must name the live, verified session: any other is ``terminal_changed``."""
        if pinned and (not isinstance(expected_binding, str) or not 8 <= len(expected_binding) <= 120):
            raise DomainError("validation_error", "The binding_id of the session being controlled is required")
        if not pinned and expected_binding is not None and (not isinstance(expected_binding, str) or not 8 <= len(expected_binding) <= 120):
            raise DomainError("validation_error", "The binding_id of the session being controlled is invalid")
        with self.database.read() as uow:
            profile = self._target(uow, principal_id, agent_id)
            require_protected_control(uow, principal_id, "human", [agent_id])
            persisted = store.live_session(uow.connection, agent_id)
        if self._terminals():
            raise DomainError("not_managed", "This agent runs in its terminal")
        if self.managed is None:
            raise DomainError("managed_unavailable", "Qorqbot cannot run this agent's session right now")
        session = None
        if expected_binding is not None:
            session = self.session_view(profile, persisted)
            if session["binding"] is None or session["binding"]["binding_id"] != expected_binding:
                raise DomainError("terminal_changed", "The agent conversation changed; try again")
        return profile, session

    @staticmethod
    def _control_failure(error: DomainError) -> DomainError:
        code, message = _OPEN_REFUSALS.get(error.code, ("operation_unavailable", "The session could not be reached"))
        return DomainError(code, message, retryable=bool(error.retryable))

    def interrupt(self, principal_id: str, agent_id: str, expected_binding) -> dict:
        """Stop the turn the session is running. A control operation, not an input: it takes no key, is never stored and
        never waits for anything. ``{"stopped": bool}``: false when nothing was running."""
        with self.arbiter.locks(agent_id):
            self._managed_session(principal_id, agent_id, expected_binding, pinned=True)
            try:
                reply = self.managed.interrupt(agent_id)
            except DomainError as error:
                raise self._control_failure(error) from None
        if isinstance(reply, dict) and reply.get("status") == "rejected":
            raise DomainError(*_REFUSALS[SESSION_CLOSED])
        self.backend.wake()
        return {"stopped": bool(isinstance(reply, dict) and reply.get("sent"))}

    @staticmethod
    def _decision(decision):
        """The answer as the host takes it: ``"allow"``/``"deny"`` for a permission request, ``{"answers": {question: answer}}``
        for a question; anything else is refused before it reaches the host."""
        if decision in ANSWER_DECISIONS:
            return decision
        if (isinstance(decision, dict) and set(decision) == {"answers"} and isinstance(decision["answers"], dict)
                and 0 < len(decision["answers"]) <= 20
                and all(isinstance(name, str) and name and len(name) <= 2000 and isinstance(value, str) and len(value) <= 4000
                        for name, value in decision["answers"].items())):
            return {"answers": dict(decision["answers"])}
        raise DomainError("validation_error", "The decision is allow, deny, or answers to the question's text")

    @staticmethod
    def _checked_answer(waiting: dict, decision):
        """The decision for this pending request, or ``validation_error``. A permission request is answered ``"allow"`` or
        ``"deny"``. A question is answered ``{"answers": {<question text>: <answer>}}`` with an answer for every question it asks
        (keyed by the exact question text; the answer is one of its option labels, or the owner's own words, and for a question
        that allows several the chosen labels joined by ", "), never a permission word."""
        if waiting.get("kind") != "question":
            if decision not in ANSWER_DECISIONS:
                raise DomainError("validation_error", "A permission request is answered with allow or deny")
            return decision
        if not isinstance(decision, dict):
            raise DomainError("validation_error", "A question is answered with an answer to each of its questions")
        asked = [item.get("question") for item in waiting.get("questions") or [] if isinstance(item, dict)]
        answers = decision["answers"]
        if not asked or set(answers) != set(asked):
            raise DomainError("validation_error", "Answer each question the session asked")
        if not all(value.strip() for value in answers.values()):
            raise DomainError("validation_error", "Every question needs an answer")
        return {"answers": dict(answers)}

    def answer(self, principal_id: str, agent_id: str, expected_binding, request_id, decision) -> dict:
        """Answer one pending permission request or question of the managed session. ``{"answered": true}``.

        The pending request is read from the session itself, right now (not from the cached view the page showed): a request that
        is not waiting any more (answered elsewhere, the turn ended) is ``not_found``, and the decision must be of the request's
        kind (``allow``/``deny`` for a permission request, the answers object for a question) or it is ``validation_error``;
        nothing reaches the host unless both hold."""
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise DomainError("validation_error", "request_id is required")
        decision = self._decision(decision)
        with self.arbiter.locks(agent_id):
            self._managed_session(principal_id, agent_id, expected_binding, pinned=True)
            try:
                live = self.managed.status(agent_id)
            except DomainError as error:
                raise self._control_failure(error) from None
            waiting = next((item for item in (live or {}).get("pending") or []
                            if isinstance(item, dict) and item.get("request_id") == request_id), None)
            if waiting is None:
                raise DomainError("not_found", "This request is no longer waiting for an answer")
            decision = self._checked_answer(waiting, decision)
            try:
                reply = self.managed.answer(agent_id, request_id, decision)
            except DomainError as error:
                raise self._control_failure(error) from None
        if isinstance(reply, dict) and reply.get("status") == "rejected":
            raise DomainError(*_REFUSALS[SESSION_CLOSED])
        if not (isinstance(reply, dict) and reply.get("delivered")):
            raise DomainError("not_found", "This request is no longer waiting for an answer")
        self.backend.wake()
        return {"answered": True}

    def close(self, principal_id: str, agent_id: str, expected_binding=None) -> dict:
        """End the managed agent's open session (its host closes the Claude session and exits) and turn the agent off.
        ``{"closing": bool}``: false when no session was open. The optional pin must name the live session. Resuming is a message
        away (the owner's input in Chat or Telegram turns it on again); nothing is deleted."""
        with self.arbiter.locks(agent_id):
            self._managed_session(principal_id, agent_id, expected_binding, pinned=False)
            # The owner's End session turns the agent off: the intent is recorded before the effect, and put back when the close
            # fails, so nothing but the owner's own input (Chat, Telegram) starts it again.
            before = self.opener.desired(agent_id)
            self.opener.turn_off(agent_id)
            try:
                closing = bool(self.managed.close(agent_id, "graceful"))
            except DomainError as error:
                if before is not None:
                    self.opener.set_desired(agent_id, before)
                raise self._control_failure(error) from None
        self.backend.wake()
        return {"closing": closing}

    def attachment_state(self, principal_id: str, agent_id: str) -> dict:
        """The shared selected-conversation attachment of this agent's terminal, for the Chat surface. Starts nothing. It exists
        in Terminals mode only: a managed installation answers as if the route were not registered."""
        with self.database.read() as uow:
            self._target(uow, principal_id, agent_id)
            require_protected_control(uow, principal_id, "human", [agent_id])
        if not self._terminals():
            raise DomainError("not_found", "Chat endpoint is not registered")
        return self.attachment.state(agent_id)

    def adopt_session(self, profile: dict) -> None:
        """Adopt the session's verified current conversation through the existing binding transaction."""
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
            LOG.debug("Conversation adoption deferred for %s", profile["agent_id"], exc_info=True)

    def select(self, principal_id: str, agent_id: str, provider, conversation, expected_generation) -> dict:
        """The installation owner explicitly selects the native conversation this agent's Chat shows.

        The observer also uses this transaction to adopt a verified session's conversation. ``expected_generation`` is the
        owner generation the caller saw (0 when none exists). The named conversation must be the one the agent's native client
        is running right now, proved here by the same trusted probe the binding uses (hook marker, live process with its start
        ticks, its verified host): an id from the caller is never proof. The change goes through the
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
        terminal = mode.is_terminal(getattr(self.runtime, "paths", None))
        place = "terminal" if terminal else "session"
        with self.runtime.application.writer_admission.writer(), self.arbiter.locks(agent_id):
            with self.database.read() as uow:
                profile = self._target(uow, principal_id, agent_id)
                require_protected_control(uow, principal_id, "human", [agent_id])
                self.runtime.application.ensure_writes_open(uow)
                current = selection.selected(uow.connection, agent_id)
            # Any supported client the session verifiably runs may be selected; the profile runtime is only the default.
            if (current.generation if current is not None else 0) != expected_generation:
                raise DomainError("revision_conflict", "The selected conversation changed; reload and try again")
            observed = self.probe.observe(profile)
            live = observed.observation if observed.reason == REASON_LIVE else None
            if live is None or live.provider != provider or live.conversation_id != conversation:
                raise DomainError("not_found", "The terminal conversation is still connecting" if terminal
                                  else "The session's conversation is still connecting")
            from qorqut.bootstrap.native_binding import NativeBindingAdapter
            adapter = getattr(self.runtime, "native_binding_adapter", None) or NativeBindingAdapter(self.runtime)
            if current is not None and current.holds(live):
                self._publish_local_binding(adapter, profile, current)
                return {"selection": current.public(terminal=terminal), "changed": False}
            marker = self._candidate_marker(profile, agent_id, conversation, PROVIDER_RUNTIMES[provider], place)
            try:
                binding, old_state = adapter.verify(agent_id, marker["registration_id"], conversation,
                                                    previous=current.binding if current is not None else None)
            except DomainError:
                raise DomainError("not_found", f"That conversation is not verifiably running in this agent's {place}") from None
            if binding.get("session_id") != conversation or binding.get("runtime") != PROVIDER_RUNTIMES[provider]:
                raise DomainError("not_found", f"That conversation is not verifiably running in this agent's {place}")
            confirmed = self.probe.observe(profile)
            latest = confirmed.observation if confirmed.reason == REASON_LIVE else None
            if (latest is None or latest.provider != provider or latest.conversation_id != conversation
                    or latest.pid != binding.get("pid") or latest.started != binding.get("started")
                    # A managed session's observation owner is its host (``host:<pid>:<ticks>``); the verified binding names the
                    # client process, which pid and started above already compare.
                    or (getattr(latest, "transport", None) != "managed"
                        and (latest.owner or f"{latest.pid}:{latest.started}") != binding.get("owner"))):
                raise DomainError("revision_conflict", "The terminal conversation changed; reconnecting" if terminal
                                  else "The session's conversation changed; reconnecting")
            with self.runtime.resolver.require_agentic_view().read_snapshot():
                context = self.runtime.agent_context(Principal(agent_id, "agent"))
            self.runtime.inbox.start_session(context, marker["registration_id"], conversation,
                                             binding.get("event") or "startup", binding=binding, old_state=old_state,
                                             expected_generation=expected_generation, owner_selection=True)
            with self.database.transaction(operation="native_chat.select") as uow:
                self.runtime.application.ensure_writes_open(uow)
                store.bump(uow.connection, agent_id)
                chosen = selection.selected(uow.connection, agent_id)
            self._publish_local_binding(adapter, profile, chosen)
        self.backend.wake()
        return {"selection": chosen.public(terminal=terminal), "changed": True}

    @staticmethod
    def _publish_local_binding(adapter, profile, chosen):
        publish = getattr(adapter, 'publish_local', None)
        if publish is not None:
            try:
                publish(profile, chosen)
            except (OSError, ValueError, KeyError, DomainError):
                # Adoption is already durable. Its ordinary supervisor can
                # retry publication; do not report that the selection failed.
                LOG.warning("Local native binding publication deferred", exc_info=True)

    @staticmethod
    def _candidate_marker(profile: dict, agent_id: str, conversation: str, runtime: str, place: str = "session") -> dict:
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
            raise DomainError("not_found", f"That conversation is not the one running in this agent's {place}") from None

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
