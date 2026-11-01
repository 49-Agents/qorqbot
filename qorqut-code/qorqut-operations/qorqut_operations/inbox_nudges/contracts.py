"""Values of the inbox nudge engine: the exact native binding, the progress observation, reservations and outcomes.

Nothing here knows how a native session is reached. The host supplies a ``Binding`` it has verified, an ``Observation`` of real
progress it has seen, and reports what the native transport said as an ``Outcome``; the engine only keeps the durable need, the
immutable attempts and their evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from qorqut_operations.contracts import DomainError, canonical_hash, plain

# What the notice says. It names no event, source or count, so nothing it carries can disclose an unavailable item, and it
# states the delivery semantics the agent must follow: direct user work first, the existing MCP inbox tools, explicit exact
# acknowledgement. It is advisory: it is not an approval, an acknowledgement or a task.
NUDGE_TEXT = (
    "Inbox notice: you have pending inbox updates. Keep priority on any direct user request you are working on now. "
    "Review the pending updates with your existing MCP inbox tools as soon as that work finishes or reaches a suitable "
    "checkpoint, or now if you are not working on one. Acknowledge only the exact events you have handled. "
    "This notice is advisory: it acknowledges nothing, approves nothing, changes no task, action, signoff or routine, "
    "and needs no reply."
)

PROVIDERS = ("claude", "codex")
TRANSPORTS = ("terminal", "codex_queue")

# Why an attempt was allowed (never "because time passed").
KIND_INITIAL = "initial"  # the first notice of a pending cycle
KIND_NEW_SESSION = "new_session"  # an exact new native session of the same cycle gets its own advisory
KIND_FOLLOW_UP = "follow_up"  # the bounded later notice after real progress
KINDS = (KIND_INITIAL, KIND_NEW_SESSION, KIND_FOLLOW_UP)

# Attempt states. ``prepared`` is a reservation before any input. ``submitted`` is the native transport's report that the notice
# was sent (the agent has not been shown to read anything). ``uncertain`` may have reached the session and is never replayed.
# ``deferred``, ``failed``, ``unavailable`` and ``stale`` are definitely not typed: the session was not safely ready, the
# transport refused, the transport is not supported for this exact session, or the reservation was cancelled before input.
PREPARED, SUBMITTED, UNCERTAIN = "prepared", "submitted", "uncertain"
DEFERRED, FAILED, UNAVAILABLE, STALE = "deferred", "failed", "unavailable", "stale"
ATTEMPT_STATES = (PREPARED, SUBMITTED, UNCERTAIN, DEFERRED, FAILED, UNAVAILABLE, STALE)
# An attempt that may have reached the session.
EFFECTIVE = (SUBMITTED, UNCERTAIN)
# What a native transport can report.
OUTCOME_STATES = (SUBMITTED, UNCERTAIN, DEFERRED, FAILED, UNAVAILABLE)
EFFECT = {SUBMITTED: "submitted", UNCERTAIN: "possible", DEFERRED: "none", FAILED: "none", UNAVAILABLE: "none"}

# The diagnostic vocabulary of a need.
IDLE, PENDING, STATUS_DEFERRED, STATUS_SUBMITTED = "idle", "pending", "deferred", "submitted"
STATUS_UNCERTAIN, STATUS_STALE, STATUS_UNAVAILABLE = "uncertain", "stale", "unavailable"
STATUS_NOTE = ("Submitted means the notice was sent to the native session; it does not mean the agent read its inbox or "
               "handled anything. Only an exact acknowledgement does.")

MINIMUM_REPEAT_SECONDS = 60
DEFERRED_RETRY_SECONDS = 5
FAILED_RETRY_SECONDS = 30
UNAVAILABLE_RETRY_SECONDS = 60
PENDING_SAMPLE = 100  # the most event ids one attempt records as its coverage
SCAN_ROWS = 2000  # the most delivery rows one refresh reads before it reports an incomplete scan
SOURCE_KEY_PREFIX = "inbox-nudge:"

_BINDING_KEYS = ("binding_id", "agent_id", "provider", "conversation_id", "generation", "transport", "process_pid",
                 "process_started")


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or value != value.strip():
        raise DomainError("invalid_binding", f"A session binding needs a plain {name} of up to {limit} characters")
    return value


def source_key(attempt_id: str) -> str:
    """The stable native input key of one attempt; the arbiter's idempotency makes a replay return the saved result."""
    return SOURCE_KEY_PREFIX + attempt_id


@dataclass(frozen=True)
class Binding:
    """One exact verified native session: its binding id, agent, provider, conversation, generation and the process and
    transport that hold it. Agent ids are the registered ids as they are (no length or alphabet assumption); the generation is
    the host's per-agent monotonic number, never the conversation id alone."""

    binding_id: str
    agent_id: str
    provider: str
    conversation_id: str
    generation: int
    transport: str
    process_pid: int
    process_started: str

    def __post_init__(self):
        _text(self.binding_id, "binding id", 200)
        _text(self.agent_id, "agent id", 200)
        _text(self.conversation_id, "conversation id", 256)
        _text(self.process_started, "process start", 40)
        if self.provider not in PROVIDERS or self.transport not in TRANSPORTS:
            raise DomainError("invalid_binding", "A session binding names a supported provider and transport")
        if type(self.generation) is not int or self.generation < 1 or type(self.process_pid) is not int or self.process_pid < 1:
            raise DomainError("invalid_binding", "A session binding needs a positive generation and process id")

    @classmethod
    def of(cls, value: "Binding | Mapping[str, Any]", agent_id: str | None = None) -> "Binding":
        if isinstance(value, Binding):
            result = value
        elif isinstance(value, Mapping) and set(value) == set(_BINDING_KEYS):
            result = cls(*(value[key] for key in _BINDING_KEYS))
        else:
            raise DomainError("invalid_binding", "Supply the complete exact session binding")
        if agent_id is not None and result.agent_id != agent_id:
            raise DomainError("invalid_binding", "The session binding belongs to another agent")
        return result

    def public(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in _BINDING_KEYS}

    @property
    def session_key(self) -> str:
        return canonical_hash(self.public())


@dataclass(frozen=True)
class Observation:
    """What the host has really seen of the agent's work, as monotonic counters.

    ``work`` counts completed units of work that were not answers to a nudge; ``user`` counts direct Chat inputs the host
    positively observed delivered; ``working`` is the host's evidence that a turn is running right now. A nudge's own answer
    never advances ``work``, so a nudge cannot rearm itself."""

    work: int = 0
    user: int = 0
    working: bool = False

    def __post_init__(self):
        if type(self.work) is not int or type(self.user) is not int or self.work < 0 or self.user < 0 or type(self.working) is not bool:
            raise DomainError("validation_error", "An observation has nonnegative counters and a boolean working flag")

    def public(self) -> dict[str, Any]:
        return {"work": self.work, "user": self.user, "working": self.working}


@dataclass(frozen=True)
class Reservation:
    """One notice reserved for exactly one session. ``source_key`` is the native input key; ``text`` is what is typed (the
    transport adds its own system marker)."""

    attempt_id: str
    agent_id: str
    cycle: int
    sequence: int
    kind: str
    binding: Binding
    event_ids: tuple[int, ...]
    text: str = NUDGE_TEXT

    @property
    def source_key(self) -> str:
        return source_key(self.attempt_id)


@dataclass(frozen=True)
class Waiting:
    """Nothing is due now. ``reason`` is a stable code; ``not_before`` is a lower bound (never a trigger) when one applies."""

    reason: str
    not_before: str | None = None
    status: str = PENDING


@dataclass(frozen=True)
class Outcome:
    """What the native transport reported for one reservation. ``receipt`` is the exact evidence: the native input id (when a
    durable claim was made), the exact binding, and a hash-safe transport report; it never carries the typed text or a screen."""

    state: str
    reason: str
    receipt: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.state not in OUTCOME_STATES:
            raise DomainError("invalid_outcome", "Unknown native delivery outcome")
        if not isinstance(self.reason, str) or not self.reason or len(self.reason) > 200:
            raise DomainError("invalid_outcome", "A delivery outcome needs an evidence reason")

    def evidence(self, binding: Binding) -> dict[str, Any]:
        """The canonical evidence of this outcome for ``binding``; the same report always hashes the same."""
        receipt = plain(dict(self.receipt))
        if receipt.get("effect", EFFECT[self.state]) != EFFECT[self.state]:
            raise DomainError("invalid_outcome", "The receipt effect does not match this outcome")
        if "binding" in receipt and receipt["binding"] != binding.public():
            raise DomainError("invalid_outcome", "The receipt belongs to a different exact session")
        return {"state": self.state, "reason": self.reason, "receipt": {**receipt, "effect": EFFECT[self.state],
                                                                      "binding": binding.public()}}
