"""Storage of the shared tag catalog and of the tags applied to Tasks and Actions.

A tag is only a label. The catalog holds each normalized label once (``label_key``) with the display form it was first
created with; previously used labels stay discoverable. ``item_tags`` associates a catalog label with one item in
that item's owning namespace. Neither table carries ownership or access: a label that names a business is still just
a label, it creates no business, folder or second owner, and it never widens who can read or write an item.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

SCHEMA = (
    """CREATE TABLE tags (
 id TEXT PRIMARY KEY CHECK(length(id)=36),
 label_key TEXT NOT NULL UNIQUE CHECK(length(label_key) BETWEEN 1 AND 64),
 label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 64),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL
)""",
    """CREATE TABLE item_tags (
 business_id TEXT REFERENCES businesses(id),
 item_type TEXT NOT NULL CHECK(item_type IN ('task','action')),
 item_id TEXT NOT NULL,
 tag_id TEXT NOT NULL REFERENCES tags(id),
 applied_by TEXT NOT NULL REFERENCES principals(id),
 applied_at TEXT NOT NULL,
 PRIMARY KEY (business_id, item_type, item_id, tag_id)
)""",
    """CREATE UNIQUE INDEX item_tags_global_key ON item_tags(item_type,item_id,tag_id) WHERE business_id IS NULL""",
    """CREATE INDEX item_tags_tag ON item_tags(tag_id,business_id,item_type,item_id)""",
    """CREATE TRIGGER tags_immutable_update BEFORE UPDATE ON tags
BEGIN SELECT RAISE(ABORT,'Tag labels are immutable: no rename or merge'); END""",
    """CREATE TRIGGER tags_keep_history BEFORE DELETE ON tags
BEGIN SELECT RAISE(ABORT,'Previously used tags remain in the catalog'); END""",
)

TABLES = {
    "tags": TableSpec(("id",), mutable=False, revision_column=None),
    "item_tags": TableSpec(("business_id", "item_type", "item_id", "tag_id"), "business_id", revision_column=None),
}


def storage_contribution() -> StorageContribution:
    return StorageContribution("tags", TABLES, schema=SCHEMA)
