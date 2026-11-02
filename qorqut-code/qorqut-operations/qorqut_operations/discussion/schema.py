"""Storage for the canonical inbox records and the shared item discussions.

The Operations engine owns the immutable inbox event identity, the per-agent deliveries and the manual subscriptions
(their v1 definitions, plus one ``required`` flag on a delivery), and the new shared discussion of a Task, Action or Table:
append-only messages and the follow state with its sticky unfollow. The private owner/peer conversations, broadcasts
and the transport-facing inbox tables stay with the host; they reference ``inbox_events`` and keep writing their
delivery rows directly, which is why ``required`` defaults to 1: a directly addressed delivery is a mandatory one.

An item discussion has no table of its own. Its identity is the item (``item_type`` plus ``item_id`` inside the
item's owning namespace), and ``thread_id`` is derived from it, so a thread can never exist without its item.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

# The item kinds a discussion can attach to. A DM is a different concept and never appears here. A Table discussion is
# the shared thread of a CRM table (the ``crm`` package guards which tables may have one).
ITEM_TYPES = ("task", "action", "table")

INBOX_SCHEMA = (
    """CREATE TABLE inbox_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT REFERENCES businesses(id),
 source_type TEXT NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER,
 actor_id TEXT, event_type TEXT NOT NULL, summary TEXT NOT NULL,
 visibility_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
)""",
    """CREATE TABLE inbox_deliveries (
 agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id),
 event_id INTEGER NOT NULL REFERENCES inbox_events(id),
 acknowledged_at TEXT, outcome TEXT,
 required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
 PRIMARY KEY(agent_id,event_id)
)""",
    """CREATE INDEX inbox_pending ON inbox_deliveries(agent_id,acknowledged_at,event_id)""",
    """CREATE TABLE inbox_subscriptions (
 id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id),
 business_id TEXT REFERENCES businesses(id), source_type TEXT NOT NULL,
 source_id TEXT NOT NULL, include_related INTEGER NOT NULL DEFAULT 0 CHECK(include_related IN (0,1)),
 created_at TEXT NOT NULL
)""",
    """CREATE UNIQUE INDEX inbox_subscription_target ON inbox_subscriptions(agent_id,coalesce(business_id,''),source_type,source_id)""",
    """CREATE TRIGGER inbox_events_immutable_update BEFORE UPDATE ON inbox_events BEGIN
 SELECT RAISE(ABORT,'Inbox events are immutable'); END""",
    """CREATE TRIGGER inbox_events_immutable_delete BEFORE DELETE ON inbox_events BEGIN
 SELECT RAISE(ABORT,'Inbox events are immutable'); END""",
    """CREATE TRIGGER inbox_ack_immutable BEFORE UPDATE ON inbox_deliveries
WHEN OLD.acknowledged_at IS NOT NULL AND (NEW.acknowledged_at IS NOT OLD.acknowledged_at OR NEW.outcome IS NOT OLD.outcome)
BEGIN SELECT RAISE(ABORT,'Inbox acknowledgement is immutable'); END""",
    """CREATE TRIGGER inbox_delivery_required_immutable BEFORE UPDATE ON inbox_deliveries
WHEN NEW.required IS NOT OLD.required
BEGIN SELECT RAISE(ABORT,'A delivery keeps the reason it was made'); END""",
)

THREAD_SCHEMA = (
    """CREATE TABLE thread_messages (
 business_id TEXT REFERENCES businesses(id),
 id TEXT NOT NULL,
 item_type TEXT NOT NULL CHECK(item_type IN ('task','action','table')),
 item_id TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT 'comment' CHECK(kind IN ('comment','system')),
 body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 20000),
 summary TEXT CHECK(summary IS NULL OR length(summary) BETWEEN 1 AND 500),
 refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(refs_json)),
 author_id TEXT REFERENCES principals(id),
 recorder_id TEXT NOT NULL REFERENCES principals(id),
 attribution_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(attribution_json)),
 change_json TEXT CHECK(change_json IS NULL OR json_valid(change_json)),
 event_id INTEGER REFERENCES inbox_events(id),
 created_at TEXT NOT NULL,
 PRIMARY KEY (business_id, id),
 CHECK((kind='system') = (author_id IS NULL))
)""",
    """CREATE UNIQUE INDEX thread_messages_global_key ON thread_messages(id) WHERE business_id IS NULL""",
    """CREATE INDEX thread_messages_item ON thread_messages(business_id,item_type,item_id,created_at,id)""",
    """CREATE TRIGGER thread_messages_immutable_update BEFORE UPDATE ON thread_messages
WHEN OLD.event_id IS NOT NULL OR NEW.id IS NOT OLD.id OR NEW.business_id IS NOT OLD.business_id
 OR NEW.item_type IS NOT OLD.item_type OR NEW.item_id IS NOT OLD.item_id OR NEW.kind IS NOT OLD.kind
 OR NEW.body IS NOT OLD.body OR NEW.summary IS NOT OLD.summary OR NEW.refs_json IS NOT OLD.refs_json
 OR NEW.author_id IS NOT OLD.author_id OR NEW.recorder_id IS NOT OLD.recorder_id
 OR NEW.attribution_json IS NOT OLD.attribution_json OR NEW.change_json IS NOT OLD.change_json
 OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'Thread messages are immutable; only the delivering event is attached once'); END""",
    """CREATE TRIGGER thread_messages_immutable_delete BEFORE DELETE ON thread_messages
BEGIN SELECT RAISE(ABORT,'Thread messages are immutable'); END""",
    """CREATE TABLE thread_follows (
 business_id TEXT REFERENCES businesses(id),
 item_type TEXT NOT NULL CHECK(item_type IN ('task','action','table')),
 item_id TEXT NOT NULL,
 principal_id TEXT NOT NULL REFERENCES principals(id),
 following INTEGER NOT NULL CHECK(following IN (0,1)),
 explicit INTEGER NOT NULL DEFAULT 0 CHECK(explicit IN (0,1)),
 reason TEXT NOT NULL CHECK(reason IN ('creator','assignee','commenter','represented','explicit')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 PRIMARY KEY (business_id, item_type, item_id, principal_id),
 CHECK(following=1 OR (explicit=1 AND reason='explicit'))
)""",
    """CREATE UNIQUE INDEX thread_follows_global_key ON thread_follows(item_type,item_id,principal_id) WHERE business_id IS NULL""",
    """CREATE INDEX thread_follows_principal ON thread_follows(principal_id,following,business_id,item_type,item_id)""",
)

TABLES = {
    "inbox_events": TableSpec(("id",), "business_id", False, None),
    "inbox_deliveries": TableSpec(("agent_id", "event_id"), revision_column=None),
    "inbox_subscriptions": TableSpec(("id",), "business_id", revision_column=None),
    "thread_messages": TableSpec(("business_id", "id"), "business_id", False, None),
    "thread_follows": TableSpec(("business_id", "item_type", "item_id", "principal_id"), "business_id", revision_column=None),
}

# The host tables these records reference or are written beside.
REQUIRED_TABLES = frozenset()


def storage_contribution() -> StorageContribution:
    """The inbox and discussion storage, for the host's assembly."""
    return StorageContribution("discussion", TABLES, schema=(*INBOX_SCHEMA, *THREAD_SCHEMA),
                               required_tables=REQUIRED_TABLES)
