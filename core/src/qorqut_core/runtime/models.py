"""Trusted session bindings and the deliberately narrow public runtime view."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from ..contracts import CoreError, Model, Revision, Timestamp
from ..runner.models import ProcessFingerprint
from .native_models import NativeCollectionView, NativeSetupView


class SessionConflict(CoreError):
    reason = "session_conflict"

    def __init__(self):
        super().__init__("invalid_transition", "Another session is connected to this agent. "
                         "Tell the user and wait until that session is closed.")


class OwnerCandidate(Model):
    """Association is supplied by a trusted bridge, not established by this type."""

    agent_id: UUID
    terminal_id: UUID
    recovery_epoch: UUID
    environment_generation: Revision
    provider: Literal["claude", "codex"]
    cli_version: Annotated[str, Field(min_length=1, max_length=100)]
    native_session_id: UUID
    process: ProcessFingerprint


class ProcessObservation(Model):
    state: Literal["live", "unknown", "exited"]
    observed_at: Timestamp
    reason: Annotated[str, Field(min_length=1, max_length=100)] = "process_observation"


class OwnerConnection(Model):
    outcome: Literal["connected", "reconnected"]
    owner_id: UUID
    run_generation: Revision

    @property
    def run_id(self) -> UUID:
        return self.owner_id


class RuntimeGet(Model):
    agent_id: UUID


class CurrentOwner(Model):
    owner_id: UUID
    run_id: UUID
    run_generation: Revision
    conversation_id: UUID
    terminal_id: UUID
    recovery_epoch: UUID
    environment_generation: Revision
    boot_id: UUID
    provider: Literal["claude", "codex"]
    cli_version: str
    native_session_id: UUID
    connected_at: Timestamp


class RuntimeView(Model):
    agent_id: UUID
    status: Literal["disconnected", "connected", "unknown"]
    current_owner: CurrentOwner | None
    observed_at: Timestamp | None
    input_ready: Literal[False] = False
    native_setup: NativeSetupView | None = None
    native_capture: NativeCollectionView | None = None
