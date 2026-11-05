"""Storage of the project-shared integrated UIs: five small tables in the existing operational database.

``integrated_uis`` is the one mutable identity row per UI: its project, authored source directory, creator, optional
Task, archive flag, three independent revision counters (metadata, accepted code, saved data) and the per-UI event
sequence. ``integrated_ui_code`` keeps every accepted executable document immutably; serving never reads the live file
system. ``integrated_ui_values`` keeps one row per ``(ui_id, key, data_revision)`` so an exact historical read selects
the newest version of each key at or before the requested revision; removal is a tombstone row, never a delete.
``integrated_ui_events`` is the ordered change/notification ledger (a local event carries its own notification receipt
once one was requested). ``integrated_ui_views`` is the latest presentation state per human, latest-write-wins.

The schema is additive: nothing here alters a retained host table, and the tables reference only existing identity,
project and Task rows. The recovery worker owns the existing-install upgrade entry point and the history
classification of these tables; the API worker owns the generated ``composition/schema_ddl.py`` publication of
``DDL``. ``TABLE_SPECS``/``IMMUTABLE_FIELDS`` are consumed by the host composition exactly as every other domain's.

``integrated_uis`` declares ``metadata_revision`` as its repository revision column, so an ordinary metadata change
is a plain repository update with revision CAS. The code and data revisions move only through the domain's own
check-and-set statements in ``service``: a code activation and a data commit are not metadata edits, and must not
rewrite the metadata revision a pending metadata edit is checked against.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "integrated-ui"

TABLE_NAMES = ("integrated_uis", "integrated_ui_code", "integrated_ui_values", "integrated_ui_events",
               "integrated_ui_views")

TABLE_SPECS = {
    # One identity row per UI: revisions are compared by the domain, ``metadata_revision`` is its repository CAS column.
    "integrated_uis": TableSpec(("business_id", "id"), "business_id", revision_column="metadata_revision"),
    # Immutable accepted documents: an appended version is never changed or deleted.
    "integrated_ui_code": TableSpec(("business_id", "ui_id", "revision"), "business_id", False, None),
    # Versioned values: the whole point is the retained version per key, so rows are append-only.
    "integrated_ui_values": TableSpec(("business_id", "ui_id", "key", "data_revision"), "business_id", False, None),
    # Ordered local history and notification receipts; rows are appended, never rewritten except the receipt itself.
    "integrated_ui_events": TableSpec(("business_id", "id"), "business_id", False, None),
    # Latest presentation state per human; latest-write-wins, so an ordinary mutable revision row.
    "integrated_ui_views": TableSpec(("business_id", "ui_id", "principal_id"), "business_id"),
}

# Identity columns of the two mutable tables. The append-only tables refuse updates and deletes through their
# table specification, so they need no separate immutable set.
IMMUTABLE_FIELDS = {
    "integrated_uis": frozenset({"id", "business_id", "slug", "source_path", "creator_id", "created_at"}),
    "integrated_ui_views": frozenset({"business_id", "ui_id", "principal_id", "created_at"}),
}

DDL = (
    """CREATE TABLE integrated_uis (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    id TEXT NOT NULL,
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
    slug TEXT NOT NULL CHECK(length(slug) BETWEEN 1 AND 64),
    source_path TEXT NOT NULL CHECK(length(source_path) BETWEEN 1 AND 512),
    creator_id TEXT NOT NULL REFERENCES principals(id),
    task_id TEXT,
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    metadata_revision INTEGER NOT NULL DEFAULT 1 CHECK(metadata_revision >= 1),
    code_revision INTEGER NOT NULL CHECK(code_revision >= 1),
    data_revision INTEGER NOT NULL DEFAULT 1 CHECK(data_revision >= 1),
    event_sequence INTEGER NOT NULL DEFAULT 0 CHECK(event_sequence >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(business_id,id),
    UNIQUE(business_id,source_path),
    FOREIGN KEY(business_id,task_id) REFERENCES tasks(business_id,id)
) STRICT""",
    """CREATE INDEX integrated_uis_library ON integrated_uis(business_id,archived,updated_at,id)""",
    """CREATE INDEX integrated_uis_task ON integrated_uis(business_id,task_id) WHERE task_id IS NOT NULL""",
    """CREATE TABLE integrated_ui_code (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    ui_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
    api_version INTEGER NOT NULL DEFAULT 1 CHECK(api_version >= 1),
    html TEXT NOT NULL,
    byte_length INTEGER NOT NULL CHECK(byte_length >= 0 AND byte_length <= 5242880),
    actor_id TEXT NOT NULL REFERENCES principals(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(business_id,ui_id,revision),
    FOREIGN KEY(business_id,ui_id) REFERENCES integrated_uis(business_id,id)
) STRICT""",
    """CREATE TABLE integrated_ui_values (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    ui_id TEXT NOT NULL,
    key TEXT NOT NULL CHECK(length(key) BETWEEN 1 AND 200),
    data_revision INTEGER NOT NULL CHECK(data_revision >= 1),
    value_json TEXT CHECK(value_json IS NULL OR json_valid(value_json)),
    tombstone INTEGER NOT NULL DEFAULT 0 CHECK(tombstone IN (0,1)),
    actor_id TEXT NOT NULL REFERENCES principals(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(business_id,ui_id,key,data_revision),
    FOREIGN KEY(business_id,ui_id) REFERENCES integrated_uis(business_id,id),
    CHECK((tombstone = 1) = (value_json IS NULL))
) STRICT""",
    """CREATE INDEX integrated_ui_values_revision ON integrated_ui_values(business_id,ui_id,data_revision,key)""",
    """CREATE TABLE integrated_ui_events (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    id TEXT NOT NULL,
    ui_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    event_type TEXT NOT NULL CHECK(length(event_type) BETWEEN 1 AND 120),
    data_json TEXT CHECK(data_json IS NULL OR json_valid(data_json)),
    actor_id TEXT NOT NULL REFERENCES principals(id),
    code_revision INTEGER NOT NULL CHECK(code_revision >= 1),
    observed_revision INTEGER NOT NULL CHECK(observed_revision >= 1),
    data_revision INTEGER NOT NULL CHECK(data_revision >= 1),
    task_id TEXT,
    notification_key TEXT CHECK(notification_key IS NULL OR length(notification_key) BETWEEN 1 AND 300),
    notification_payload_hash TEXT CHECK(notification_payload_hash IS NULL OR length(notification_payload_hash) = 64),
    notification_status TEXT CHECK(notification_status IS NULL OR notification_status IN ('queued','no_recipients')),
    inbox_event_id INTEGER,
    delivery_count INTEGER CHECK(delivery_count IS NULL OR delivery_count >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(business_id,id),
    UNIQUE(ui_id,sequence),
    UNIQUE(business_id,ui_id,notification_key),
    FOREIGN KEY(business_id,ui_id) REFERENCES integrated_uis(business_id,id),
    FOREIGN KEY(business_id,task_id) REFERENCES tasks(business_id,id),
    CHECK((notification_key IS NULL) = (notification_payload_hash IS NULL)),
    CHECK(inbox_event_id IS NULL OR notification_key IS NOT NULL)
) STRICT""",
    """CREATE TABLE integrated_ui_views (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    ui_id TEXT NOT NULL,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    state_json TEXT NOT NULL CHECK(json_valid(state_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(business_id,ui_id,principal_id),
    FOREIGN KEY(business_id,ui_id) REFERENCES integrated_uis(business_id,id)
) STRICT""",
)


def storage_contribution() -> StorageContribution:
    """The typed contribution the host composition registers for this owner."""
    return StorageContribution(OWNER, TABLE_SPECS, IMMUTABLE_FIELDS, DDL)