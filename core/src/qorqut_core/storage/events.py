"""Append-only observations. Message bodies and transcript chunks are not owned here."""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from pydantic import Field, JsonValue, model_validator

from ..contracts import CoreError, Counter, Model, Timestamp, canonical
from .database import Transaction


class EventInput(Model):
    kind: Annotated[str, Field(min_length=1, max_length=100)]
    actor_id: UUID
    agent_id: UUID | None = None
    operation_id: UUID | None = None
    occurred_at: Timestamp | None = None
    payload: dict[str, JsonValue]
    payload_version: Annotated[int, Field(strict=True, ge=1)] = 1
    event_id: UUID | None = None
    origin_installation_id: UUID | None = None
    origin_seq: Counter | None = None

    @model_validator(mode="after")
    def provenance(self):
        if (self.origin_installation_id is None) != (self.origin_seq is None):
            raise ValueError("Origin installation and sequence are paired")
        if self.origin_seq is not None and (self.origin_seq == 0 or self.event_id is None or self.occurred_at is None):
            raise ValueError("Imported events require original identity, positive cursor and occurrence time")
        if len(canonical(self.payload).encode("utf-8")) > 65536:
            raise ValueError("Event payload exceeds 64 KiB")
        return self


class EventsList(Model):
    after_seq: Counter = 0
    recovery_epoch: UUID | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 100

    @model_validator(mode="after")
    def bound_cursor(self):
        if self.after_seq and self.recovery_epoch is None:
            raise ValueError("A continuation cursor requires its recovery epoch")
        return self


class EventRecord(Model):
    event_seq: Counter
    event_id: UUID
    kind: str
    actor_id: UUID
    agent_id: UUID | None
    operation_id: UUID | None
    occurred_at: Timestamp
    recorded_at: Timestamp
    origin_installation_id: UUID
    origin_seq: Counter
    payload_version: int
    payload: dict[str, JsonValue]


class EventPage(Model):
    items: list[EventRecord]
    next_after_seq: Counter
    recovery_epoch: UUID
    has_more: bool


def append_event(tx: Transaction, event: EventInput) -> int:
    tx.require_write()
    conn = tx.connection
    installation = conn.execute("SELECT installation_id FROM installation WHERE singleton=1").fetchone()
    local_seq = conn.execute("SELECT coalesce(max(event_seq),0)+1 FROM events").fetchone()[0]
    now = tx.db.clock()
    values = {
        "event_id": str(event.event_id or tx.db.id_factory()), "kind": event.kind,
        "actor_id": str(event.actor_id), "agent_id": str(event.agent_id) if event.agent_id else None,
        "operation_id": str(event.operation_id) if event.operation_id else None,
        "occurred_at": event.occurred_at or now,
        "origin_installation_id": str(event.origin_installation_id or installation[0]),
        "origin_seq": event.origin_seq if event.origin_seq is not None else local_seq,
        "payload_version": event.payload_version, "payload_json": canonical(event.payload),
    }
    existing = conn.execute("SELECT * FROM events WHERE event_id=? OR (origin_installation_id=? AND origin_seq=?)",
                            (values["event_id"], values["origin_installation_id"], values["origin_seq"])).fetchall()
    if existing:
        if len(existing) != 1 or any(existing[0][k] != v for k, v in values.items()):
            raise CoreError("idempotency_conflict", "Event provenance already identifies different content")
        return existing[0]["event_seq"]
    conn.execute("""INSERT INTO events (event_seq,event_id,kind,actor_id,agent_id,operation_id,
        occurred_at,recorded_at,origin_installation_id,origin_seq,payload_version,payload_json)
        VALUES (:event_seq,:event_id,:kind,:actor_id,:agent_id,:operation_id,:occurred_at,:recorded_at,
        :origin_installation_id,:origin_seq,:payload_version,:payload_json)""",
                 {**values, "event_seq": local_seq, "recorded_at": now})
    return local_seq


def list_events(tx: Transaction, arguments: EventsList) -> dict:
    epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
    if arguments.recovery_epoch is not None and str(arguments.recovery_epoch) != epoch:
        raise CoreError("target_changed", "Event cursor belongs to another recovery epoch")
    rows = tx.connection.execute("SELECT * FROM events WHERE event_seq>? ORDER BY event_seq LIMIT ?",
                                 (arguments.after_seq, arguments.limit + 1)).fetchall()
    items = []
    for row in rows[:arguments.limit]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        items.append(item)
    return {"items": items, "next_after_seq": items[-1]["event_seq"] if items else arguments.after_seq,
            "recovery_epoch": epoch, "has_more": len(rows) > arguments.limit}
