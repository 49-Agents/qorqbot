"""Conversation entries are immutable; delivery, handling and publication are views."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, model_validator

from ..adapters.native_records import NativeId
from ..contracts import Counter, Digest, ErrorCode, Model, Revision, Timestamp
from ..messaging.models import Body, DeliveryView, HandlingView, PageLimit


def _date(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("A canonical UTC recording date is required")
    return value


RecordingDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$"), AfterValidator(_date)]


class ConversationList(Model):
    agent_id: UUID
    after_seq: Counter | None = None
    before_seq: Counter | None = None
    limit: PageLimit = 50
    recovery_epoch: UUID | None = None

    @model_validator(mode="after")
    def cursors(self):
        if self.after_seq is not None and self.before_seq is not None:
            raise ValueError("Before and after cursors are mutually exclusive")
        if (self.after_seq is not None or self.before_seq is not None) and self.recovery_epoch is None:
            raise ValueError("A cursor requires its recovery epoch, including zero")
        return self


class MessageSource(Model):
    type: Literal["message"] = "message"
    message_id: UUID
    run_id: UUID | None = None
    provider: str | None = None
    source_id: UUID | None = None
    source_epoch: UUID | None = None
    offset: Counter | None = None
    length: Counter | None = None


class NativeProvenance(Model):
    provider: Literal["claude", "codex"]
    run_id: UUID
    source_id: UUID
    source_epoch: UUID
    offset: Counter
    length: Revision
    sha256: Digest
    parser_version: Annotated[str, Field(min_length=1, max_length=100)]


class NativeSource(NativeProvenance):
    type: Literal["native"] = "native"
    native_id: NativeId
    part_index: Counter
    timestamp_origin: Literal["native", "ingestion"]


class Entry(Model):
    entry_id: UUID
    agent_id: UUID
    conversation_id: UUID
    conversation_seq: Revision
    kind: Literal["message"]
    role: Literal["user", "agent"]
    direction: Literal["incoming", "outgoing"]
    text: Body
    sender_actor_id: UUID | None
    source: Annotated[MessageSource | NativeSource, Field(discriminator="type")]
    occurred_at: Timestamp
    recorded_at: Timestamp


class ConversationItem(Entry):
    delivery: DeliveryView | None
    handling: HandlingView | None


class PublicationError(Model):
    code: ErrorCode
    message: Annotated[str, Field(max_length=2000)]


class Publication(Model):
    pending_entries: Counter
    state: Literal["current", "pending", "blocked"]
    error: PublicationError | None


class ConversationPage(Model):
    agent_id: UUID
    conversation_id: UUID
    recovery_epoch: UUID
    items: list[ConversationItem]
    first_seq: Revision | None
    last_seq: Revision | None
    has_older: bool
    has_newer: bool
    publication: Publication


class PublishJob(Model):
    agent_id: UUID
    date: RecordingDate
    required_seq: Revision
