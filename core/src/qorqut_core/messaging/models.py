"""Frozen message operation schemas shared by every transport."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, model_validator

from ..contracts import Counter, Model, Mutation, Revision, Timestamp, _utf8


def _body(value: str) -> str:
    if not 1 <= len(value.encode("utf-8")) <= 65536:
        raise ValueError("Message body requires 1–65,536 UTF-8 bytes")
    return value


Body = Annotated[str, Field(strict=True, min_length=1, max_length=65536), AfterValidator(_body)]
Note = Annotated[str, Field(strict=True, max_length=4000), AfterValidator(_utf8)]
PageLimit = Annotated[int, Field(strict=True, ge=1, le=100)]


class MessagesSend(Mutation):
    recipient_agent_id: UUID
    body: Body
    thread_id: UUID | None = None
    reply_to_message_id: UUID | None = None


class MessagesList(Model):
    message_id: UUID | None = None
    agent_id: UUID | None = None
    thread_id: UUID | None = None
    after_seq: Counter = 0
    limit: PageLimit = 50
    recovery_epoch: UUID | None = None
    unhandled_only: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def cursor(self):
        if self.after_seq and self.recovery_epoch is None:
            raise ValueError("A continuation requires its recovery epoch")
        return self


class MessagesAcknowledge(Mutation):
    message_ids: Annotated[list[UUID], Field(min_length=1, max_length=100)]
    note: Note | None = None

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("Message IDs must be distinct")
        return self


class DeliveriesGet(Model):
    message_id: UUID


class MessageView(Model):
    message_id: UUID
    message_seq: Revision
    thread_id: UUID
    reply_to_message_id: UUID | None
    sender_actor_id: UUID
    sender_agent_id: UUID | None
    recipient_agent_id: UUID
    body: Body
    occurred_at: Timestamp
    recorded_at: Timestamp


class DeliveryView(Model):
    message_id: UUID
    recipient_agent_id: UUID
    state: Literal["pending", "deferred", "dispatching", "submitted", "uncertain", "failed", "cancelled"]
    reason: Annotated[str, Field(max_length=100)] | None
    updated_at: Timestamp


class HandlingView(Model):
    message_id: UUID
    handled_by: UUID
    handled_at: Timestamp
    note: Note | None


class EntryReference(Model):
    entry_id: UUID
    agent_id: UUID
    conversation_id: UUID
    conversation_seq: Revision


class MessageSent(Model):
    message: MessageView
    delivery: DeliveryView
    entries: list[EntryReference]


class MessageItem(Model):
    message: MessageView
    delivery: DeliveryView
    handling: HandlingView | None


class MessagePage(Model):
    items: list[MessageItem]
    next_after_seq: Counter
    has_more: bool
    recovery_epoch: UUID


class MessagesHandled(Model):
    items: list[HandlingView]


class DeliveryResult(Model):
    delivery: DeliveryView
    # Dispatch owns the future attempts schema. This increment has no attempts.
    attempts: Annotated[list, Field(max_length=0)]


class MessageDeliveryJob(Model):
    message_id: UUID
