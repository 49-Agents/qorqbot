"""Durable inbox nudge state: one pending need per agent, immutable attempts and exact evidence.

A nudge is an advisory that an agent has pending, authorized inbox events. It changes no acknowledgement, Task, Action, Signoff
or routine state, never infers that the agent read or handled anything, and never starts or resumes an agent. The host
supplies the verified native binding, the current event authorization (``Visibility``) and real-progress observations, sends
through its own input arbiter, and settles each attempt with what the transport reported. See ``state`` for the rearm rule."""

from .contracts import (ATTEMPT_STATES, DEFERRED_RETRY_SECONDS, FAILED_RETRY_SECONDS, MINIMUM_REPEAT_SECONDS, NUDGE_TEXT,
                        PENDING_SAMPLE, STATUS_NOTE, UNAVAILABLE_RETRY_SECONDS, Binding, Observation, Outcome, Reservation,
                        Waiting, source_key)
from .ports import Visibility
from .schema import OWNER, REQUIRED_TABLES, storage_contribution
from .service import InboxNudges
from .state import Policy

__all__ = ["ATTEMPT_STATES", "Binding", "DEFERRED_RETRY_SECONDS", "FAILED_RETRY_SECONDS", "InboxNudges", "MINIMUM_REPEAT_SECONDS",
           "NUDGE_TEXT", "OWNER", "Observation", "Outcome", "PENDING_SAMPLE", "Policy", "REQUIRED_TABLES", "Reservation",
           "STATUS_NOTE", "UNAVAILABLE_RETRY_SECONDS", "Visibility", "Waiting", "source_key", "storage_contribution"]
