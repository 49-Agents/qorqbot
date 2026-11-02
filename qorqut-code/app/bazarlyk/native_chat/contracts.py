"""The public vocabulary of native-session Chat: providers, session bindings and the shared input contract.

Nothing here touches the database, a process, a terminal or the archive. The arbiter, the backend service and every input
producer (Chat, Telegram, broadcasts, terminal-control direct messages, and later the inbox nudge dispatcher) import these
names, so the request and result of an input have exactly one definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# Archive providers; the registered profile runtime names them ``claude-code`` and ``codex``.
PROVIDERS = ("claude", "codex")
RUNTIME_PROVIDERS = {"claude-code": "claude", "codex": "codex"}
PROVIDER_RUNTIMES = {value: key for key, value in RUNTIME_PROVIDERS.items()}

# How an input reaches the native session. ``terminal`` types into the registered tmux pane's verified empty composer;
# ``codex_queue`` queues into one conversation of a shared Codex desktop server.
TRANSPORTS = ("terminal", "codex_queue")

# Application origin of an input. It states who the application says wrote it. It is never inferred from the transcript:
# providers record every input as a user turn and give no origin, so a native user entry carries no origin of its own.
ORIGIN_OWNER = "owner"
ORIGIN_SYSTEM = "system"
ORIGINS = (ORIGIN_OWNER, ORIGIN_SYSTEM)

# Application type of an input. The owner types ``chat``; Telegram, broadcasts and terminal-control direct messages are
# owner-origin relays; the system-origin types are ``inbox_nudge`` (pending inbox events, dispatched by the nudge backend) and
# ``bootstrap_recovery`` (the session-recovery wake). Neither is ever dispatched by Chat, and neither is a provider-native role.
TYPE_CHAT = "chat"
TYPE_TELEGRAM = "telegram"
TYPE_BROADCAST = "broadcast"
TYPE_DIRECT = "direct_message"
TYPE_NUDGE = "inbox_nudge"
TYPE_RECOVERY = "bootstrap_recovery"
INPUT_TYPES = {TYPE_CHAT: ORIGIN_OWNER, TYPE_TELEGRAM: ORIGIN_OWNER, TYPE_BROADCAST: ORIGIN_OWNER,
               TYPE_DIRECT: ORIGIN_OWNER, TYPE_NUDGE: ORIGIN_SYSTEM, TYPE_RECOVERY: ORIGIN_SYSTEM}

# Delivery states. ``waiting`` was admitted and nothing was typed; it is delivered only into the binding it was admitted for.
# ``submitting`` is the durable claim: from it on the input is never typed again by this code. ``submitted`` is the
# transport's report that the submit key was sent (processing is not confirmed). ``uncertain`` may have reached the
# session and is never replayed. ``failed`` and ``undelivered`` were definitely not typed: the transport refused it, or the
# session ended/changed while it waited.
STATE_WAITING = "waiting"
STATE_SUBMITTING = "submitting"
STATE_SUBMITTED = "submitted"
STATE_UNCERTAIN = "uncertain"
STATE_FAILED = "failed"
STATE_UNDELIVERED = "undelivered"
STATES = (STATE_WAITING, STATE_SUBMITTING, STATE_SUBMITTED, STATE_UNCERTAIN, STATE_FAILED, STATE_UNDELIVERED)
# A state an input never leaves.
FINAL_STATES = (STATE_SUBMITTED, STATE_UNCERTAIN, STATE_FAILED, STATE_UNDELIVERED)
# What a result reports when no durable row was created: nothing was admitted and nothing was typed.
RESULT_DEFERRED = "deferred"
RESULT_REJECTED = "rejected"

MAX_TEXT_CHARS = 17000


class NativeChatError(Exception):
    """A request refused before any input; ``code`` is a stable status for callers."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass(frozen=True)
class SessionBinding:
    """One exact registered native session: provider conversation, the process that holds it and the way to reach it.

    ``generation`` is a host-assigned, per-agent monotonic integer. It increases when the agent's native conversation
    changes or the same conversation is resumed by a new process, and is unchanged while the same verified conversation
    is re-observed with the same process. It is independent of the legacy inbox lease epoch. Process and thread-lock
    identity are verification evidence for the transport, not part of the conversation identity.
    """

    id: str
    agent_id: str
    provider: str
    conversation_id: str
    generation: int
    transport: str
    process_pid: int
    process_started: str
    owner: str | None = None
    transcript_path: str | None = None

    @property
    def lineage(self) -> tuple[str, str, str]:
        return (self.agent_id, self.provider, self.conversation_id)

    def public(self) -> dict[str, Any]:
        return {"binding_id": self.id, "agent_id": self.agent_id, "provider": self.provider,
                "conversation_id": self.conversation_id, "generation": self.generation, "transport": self.transport}


@dataclass(frozen=True)
class InputRequest:
    """One input to a native session, from any producer.

    ``source_key`` makes the request idempotent per agent: a repeated key returns the saved result and never types twice,
    and a key reused for different content is refused. ``wait`` selects the producer's semantics: ``True`` (Chat) admits the
    input durably and delivers it when the same session's composer is ready, ``False`` (Telegram, broadcasts, direct
    messages, nudges) attempts it once now and reports ``deferred`` when the session is not ready. ``body`` is what the
    owner sees for it; ``text`` is what is typed when it differs (private payloads are never stored: a sensitive input
    carries only ``text`` and no durable body). ``steer`` lets the text be typed while the native turn is still running, where the
    client's composer accepts it; shared Codex servers receive explicit turn/steer while busy and turn/start while idle.
    Terminal-only clients follow their native submit behavior; otherwise input waits for an idle
    composer. ``expected_binding`` pins a session the producer already observed: a different
    live session rejects the request instead of typing into it.
    """

    agent_id: str
    input_type: str
    source_key: str
    text: str
    body: str | None = None
    wait: bool = False
    principal_id: str | None = None
    expected_binding: str | None = None
    sensitive: bool = False
    steer: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def origin(self) -> str:
        return INPUT_TYPES[self.input_type]


@dataclass(frozen=True)
class InputResult:
    """What every producer gets back.

    ``input_id`` is the durable input (``None`` when nothing was admitted: ``deferred`` and ``rejected``). ``binding`` is the
    exact session the input is bound to. ``state`` is a delivery state or ``deferred``/``rejected``. ``reason`` is a stable
    code. ``evidence`` is the transport evidence observed once input was attempted (client, a hash of the terminal identity,
    the composer fingerprint, or the Codex queue report); it never carries screen text or the typed text. ``created`` is
    ``False`` for an idempotent replay.
    """

    state: str
    input_id: str | None = None
    binding: SessionBinding | None = None
    origin: str | None = None
    input_type: str | None = None
    reason: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    created: bool = True
    marker: str | None = None

    @property
    def delivered(self) -> bool:
        return self.state == STATE_SUBMITTED

    def public(self) -> dict[str, Any]:
        return {"input_id": self.input_id, "state": self.state, "reason": self.reason, "origin": self.origin,
                "input_type": self.input_type, "binding": self.binding.public() if self.binding else None,
                "evidence": dict(self.evidence), "created": self.created}


def provider_of(runtime: str | None) -> str | None:
    return RUNTIME_PROVIDERS.get(runtime) if runtime else None
