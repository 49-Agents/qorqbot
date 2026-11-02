"""The agent's selected native conversation.

One durable fact says which native conversation an agent's Chat and Terminal show and control: the agent's
``native_session_owners`` row (consumer = the native conversation id, ``binding_json`` = the verified runtime and process
identity, ``generation`` = the owner generation that fences helpers). It persists across any terminal viewer disconnect.
``native_sessions`` stays the history of process bindings and captures; it is never a second authority for "current".

This module only reads that row. The native observer follows the verified foreground terminal conversation through the
existing binding transaction (``NativeChat.select`` / ``InboxService.start_session``). Hooks supply evidence, while the
observer verifies the registered terminal before adoption. Native session history and queued-input identities remain intact.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

from .contracts import RUNTIME_PROVIDERS

# Why a view of the agent is disconnected although a native client may be running.
REASON_NO_SELECTION = "selection_required"
REASON_CANDIDATE = "other_conversation"
REASON_PENDING = "registering"

# How a verified live observation relates to the selection: it is the selected conversation held by exactly the process the
# owner row was adopted for (HELD); the selected conversation held by another process that bootstrap has not adopted yet
# (PENDING); or another conversation altogether (CANDIDATE).
HELD, PENDING, CANDIDATE = "held", "pending", "candidate"


@dataclass(frozen=True)
class Selection:
    agent_id: str
    conversation_id: str
    provider: str | None
    generation: int
    desired_state: str
    binding: dict

    @property
    def revision(self) -> str:
        """Changes whenever the selected conversation or its owner generation does."""
        return f"{self.generation}:{self.provider}:{self.conversation_id}"

    @property
    def owner(self) -> str | None:
        value = self.binding.get("owner")
        return value if isinstance(value, str) else None

    @property
    def shared_server(self) -> bool:
        """A shared Codex app-server thread: its owner is ``pid:ticks:fd:dev:inode`` (the held writer lock)."""
        return self.provider == "codex" and self.owner is not None and len(self.owner.split(":")) == 5

    @property
    def pid(self):
        return self.binding.get("pid")

    @property
    def started(self):
        return self.binding.get("started")

    @property
    def runtime(self) -> str | None:
        value = self.binding.get("runtime")
        return value if isinstance(value, str) else None

    def matches(self, provider: str | None, conversation_id: str | None) -> bool:
        return provider is not None and provider == self.provider and conversation_id == self.conversation_id

    def matches_observation(self, observation) -> bool:
        return observation is not None and self.matches(observation.provider, observation.conversation_id)

    def holds(self, observation) -> bool:
        """Whether ``observation`` is the selected conversation held by exactly the process (and, for a shared Codex thread,
        the exact writer-lock tuple) the owner row was adopted for. Provider and conversation alone are not enough: a
        same-conversation observation of a new process is only ``PENDING`` until bootstrap adopts it."""
        if not self.matches_observation(observation):
            return False
        if observation.pid != self.pid or observation.started != self.started:
            return False
        if self.shared_server:
            return observation.owner == self.owner
        # A terminal-owned conversation's owner tuple is its process, ``pid:ticks``; the row has to agree with the observation.
        return observation.owner is None and self.owner in (None, f"{observation.pid}:{observation.started}")

    def relation(self, observation) -> str | None:
        """``HELD`` / ``PENDING`` / ``CANDIDATE`` for a live observation, ``None`` for no observation."""
        if observation is None:
            return None
        if not self.matches_observation(observation):
            return CANDIDATE
        return HELD if self.holds(observation) else PENDING

    def public(self) -> dict:
        return {"provider": self.provider, "conversation_id": self.conversation_id, "generation": self.generation,
                "revision": self.revision, "transport": "codex_queue" if self.shared_server else "terminal",
                "desired_state": self.desired_state}


def relation(chosen: "Selection | None", observation) -> str | None:
    """``relation`` with no selection: every observation is a candidate."""
    if observation is None:
        return None
    return CANDIDATE if chosen is None else chosen.relation(observation)


def selected(conn, agent_id: str) -> Selection | None:
    """The agent's selected conversation, or ``None`` when no owner exists. A read never creates one."""
    row = conn.execute("SELECT consumer_id,binding_json,generation,desired_state FROM native_session_owners WHERE agent_id=?",
                       (agent_id,)).fetchone()
    if row is None:
        return None
    try:
        binding = json.loads(row["binding_json"])
    except (TypeError, ValueError):
        binding = {}
    if not isinstance(binding, dict):
        binding = {}
    return Selection(agent_id, row["consumer_id"], RUNTIME_PROVIDERS.get(binding.get("runtime")), row["generation"],
                     row["desired_state"], binding)
