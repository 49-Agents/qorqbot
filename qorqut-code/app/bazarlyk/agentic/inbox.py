"""Explicit agentic change-feed connection to the existing Operations inbox.

The caller supplies an opened layer and an admitted Operations write UOW. No
store, thread, runtime adapter, native session or secondary transaction is made
here. The receiver cursor and ordinary inbox appends commit or roll back together.
"""

import json
import re

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.discussion.events import emit_event
from qorqut_operations.global_agents import global_agent_ids, global_profile

from .inbox_schema import CONSUMER

SOURCE = "agentic_changes"
EVENT_TYPE = "agentic_layer_changed"
_INSTANCE = re.compile(r"inst_[0-9a-f]{32}")
_CHANGE = re.compile(r"chg_[0-9a-f]{32}")


def recipient(connection):
    """One currently addressable saved Learning Applier UUID; never a display name."""
    holders = [identity for identity in global_agent_ids(connection)
               if global_profile(connection, identity)["profile"]["role"]["id"] == "learning-applier"]
    if len(holders) > 1:
        raise DomainError("invalid_configuration", "More than one active learning-applier agent is registered")
    if not holders:
        return None
    profile = global_profile(connection, holders[0])["profile"]
    return holders[0] if profile["scope"].get("global_capabilities") else None


class AgenticInboxBridge:
    def __init__(self, layer):
        self.layer = layer

    def _cursor(self, uow):
        instance_id = self.layer.instance_id
        row = uow.connection.execute(
            "SELECT instance_id,last_sequence FROM agentic_inbox_cursor WHERE consumer=?", (CONSUMER,)).fetchone()
        if row is not None and row["instance_id"] != instance_id:
            raise DomainError("agentic_instance_mismatch", "Agentic inbox is bound to a different module store")
        return instance_id, row["last_sequence"] if row is not None else 0, row is not None

    @staticmethod
    def _result(instance_id, cursor, recipient_id, pending, delivered=0, self_skipped=0):
        return {"instance_id": instance_id, "cursor": cursor, "recipient_id": recipient_id,
                "delivered": delivered, "self_skipped": self_skipped, "pending": pending,
                "state": "recipient_unavailable" if recipient_id is None else ("pending" if pending else "idle")}

    def status(self, uow):
        """Read status without binding, initializing or advancing either store."""
        instance_id, cursor, _ = self._cursor(uow)
        batch = self.layer.changes.list(after=cursor, limit=1)
        return self._result(instance_id, cursor, recipient(uow.connection), bool(batch["changes"]))

    def reconcile(self, uow, *, limit=100):
        """Drain at most limit finalized changes inside the caller's admitted write transaction.

        Exceptions must leave the caller's transaction, so its event/cursor
        effects roll back. Module consumer cursors are never touched.
        """
        uow.require_write()
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DomainError("validation_error", "Agentic inbox limit must be an integer from 1 to 1000")
        instance_id, cursor, bound = self._cursor(uow)
        recipient_id = recipient(uow.connection)
        if not bound:
            uow.connection.execute(
                "INSERT INTO agentic_inbox_cursor(consumer,instance_id,last_sequence,updated_at) VALUES(?,?,0,?)",
                (CONSUMER, instance_id, utc_now()))
        if recipient_id is None:
            pending = bool(self.layer.changes.list(after=cursor, limit=1)["changes"])
            return self._result(instance_id, cursor, None, pending)

        delivered = self_skipped = 0
        pending = False
        while delivered + self_skipped < limit:
            # Module pages are bounded more tightly than the host drain budget.
            batch = self.layer.changes.list(after=cursor, limit=min(100, limit - delivered - self_skipped))
            for change in batch["changes"]:
                sequence = change["sequence"]
                if type(sequence) is not int or sequence <= cursor:
                    raise DomainError("agentic_feed_invalid", "Agentic changes must be in strict sequence order")
                actor_id = change["attribution"]["actor_id"]
                if actor_id == recipient_id:
                    self_skipped += 1
                else:
                    if not uow.connection.execute("SELECT 1 FROM principals WHERE id=?", (actor_id,)).fetchone():
                        actor_id = None
                    event_id = emit_event(uow, business_id=None, source_type=SOURCE,
                        source_id=change["change_id"], source_revision=sequence, actor_id=actor_id,
                        event_type=EVENT_TYPE,
                        summary=f"Agentic change {change['change_id']}: {len(change['files'])} changed file(s).",
                        required=[recipient_id], visibility={"instance_id": instance_id,
                            "change_id": change["change_id"], "sequence": sequence, "recipient_id": recipient_id})
                    if event_id is None:
                        raise DomainError("agentic_delivery_failed", "Agentic change could not be appended to the inbox")
                    delivered += 1
                cursor = sequence
                uow.connection.execute(
                    "UPDATE agentic_inbox_cursor SET last_sequence=?,updated_at=? WHERE consumer=?",
                    (cursor, utc_now(), CONSUMER))
            pending = batch["has_more"]
            if not pending:
                break
        return self._result(instance_id, cursor, recipient_id, pending, delivered, self_skipped)

    def visible_reference(self, ctx, row, uow):
        """A bounded reference only for the addressed current recipient and bound store."""
        if (ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None
                or row["business_id"] is not None or row["event_type"] != EVENT_TYPE):
            return None
        try:
            metadata = json.loads(row["visibility_json"])
        except (ValueError, TypeError, RecursionError):
            return None
        if not isinstance(metadata, dict) or set(metadata) != {"instance_id", "change_id", "sequence", "recipient_id"}:
            return None
        instance_id, change_id, sequence = (metadata[key] for key in ("instance_id", "change_id", "sequence"))
        if (not isinstance(instance_id, str) or _INSTANCE.fullmatch(instance_id) is None
                or not isinstance(change_id, str) or _CHANGE.fullmatch(change_id) is None
                or type(sequence) is not int or sequence < 1
                or change_id != row["source_id"] or sequence != row["source_revision"]
                or metadata["recipient_id"] != ctx.principal_id):
            return None
        binding = uow.connection.execute(
            "SELECT instance_id,last_sequence FROM agentic_inbox_cursor WHERE consumer=?", (CONSUMER,)).fetchone()
        if (binding is None or binding["instance_id"] != instance_id or sequence > binding["last_sequence"]
                or instance_id != self.layer.instance_id or recipient(uow.connection) != ctx.principal_id):
            return None
        return {"instance_id": instance_id, "change_id": change_id, "sequence": sequence}


def attach_agentic_inbox(runtime, layer, inbox):
    """Bind an installed layer to HostInbox and compose the existing nudge hook once.

    ``inbox`` is the host InboxService (or its HostInbox adapter). Call explicitly
    after installation; this helper is not called by current host startup.
    """
    host = getattr(inbox, "host", inbox)
    backend = getattr(runtime, "inbox_nudges", None)
    if backend is None:
        raise DomainError("invalid_configuration", "Agentic inbox requires the existing inbox nudge backend")
    existing = getattr(runtime, "agentic_inbox_bridge", None)
    bound = getattr(host, "agentic_inbox_bridge", None)
    if existing is not None or bound is not None:
        if (isinstance(existing, AgenticInboxBridge) and existing is bound and existing.layer is layer
                and getattr(existing, "_backend", None) is backend
                and backend.reconcile is getattr(existing, "_hook", None)):
            return existing
        raise DomainError("invalid_configuration", "Conflicting agentic inbox attachment")
    bridge = AgenticInboxBridge(layer)
    previous = backend.reconcile

    def reconcile(uow):
        if previous is not None:
            previous(uow)
        return bridge.reconcile(uow)

    bridge._backend, bridge._hook = backend, reconcile
    host.agentic_inbox_bridge = runtime.agentic_inbox_bridge = bridge
    backend.reconcile = reconcile
    return bridge
