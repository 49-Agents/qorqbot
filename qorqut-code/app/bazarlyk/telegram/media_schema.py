"""The typed host storage contribution of outbound Telegram file snapshots: one metadata table, never bytes.

``telegram_file_payloads`` is the authority for a sealed private snapshot of a file an agent asked Ordubashy to send. The
bytes live only in the bridge's private state directory (``media_payloads``); a row holds the request identity, the
frozen manifest, the lifecycle state, the quota reservation and, once sealed, the filename, mode, size, SHA-256 and the
verified metadata that remain after the bytes are purged. The ``telegram_outbox`` row of the delivery carries only the
closed envelope ``{"version": 1, "payload_id": <id>}``.

Lifecycle: ``staging`` (a copy holds a lease) -> ``ready`` (sealed, unattached) -> ``attached`` (bound to exactly one queued
send) -> ``deleting`` (a cleanup claim, bytes being removed) -> ``deleted`` (bytes gone, metadata kept). A staging attempt
that fails while its process is alive moves straight to ``deleting`` with ``failure_code`` set. An attempt that is merely
abandoned (a crash: its lease expired with nobody to end it) stays ``staging``, with its record, bytes and reservation, until
``expires_at``; only then, and only with its lease also expired, does a cleanup claim move it to ``deleting`` with
``failure_code`` ``interrupted``. A failed or abandoned request key is spent either way.
Revisions are compare-and-swap: every update raises the revision by exactly one. The registration lives in
``composition.assembly``; ``telegram_outbox.kind`` in the committed retained-domain schema (``composition.schema_ddl``) also
allows ``photo``.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "telegram-media"
TABLE = "telegram_file_payloads"

OPERATION_NAME = "telegram_send_file"
SEND_AS = ("document", "photo")
STATES = ("staging", "ready", "attached", "deleting", "deleted")
CLEANUP_REASONS = ("failed", "expired", "delivered")
# The most one payload may hold per mode; capacity is reserved at this size and settled to the real size when sealed.
MAX_BYTES = {"document": 50_000_000, "photo": 10_000_000}

TABLE_SPECS = {TABLE: TableSpec(("id",))}

# Everything that names or freezes the request, plus the opaque storage name and the reservation ceiling.
IMMUTABLE_FIELDS = {TABLE: frozenset({"installation_id", "principal_id", "operation_name", "request_key", "arguments_hash",
                                      "source_binding_json", "expected_sha256", "filename", "send_as", "storage_name",
                                      "max_bytes"})}

_HEX = "NOT GLOB '*[^0-9a-f]*'"

SCHEMA = (
    f"""CREATE TABLE {TABLE} (
    id TEXT PRIMARY KEY CHECK(length(id) = 32 AND id {_HEX}),
    installation_id TEXT NOT NULL CHECK(length(installation_id) = 36),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    operation_name TEXT NOT NULL CHECK(operation_name = '{OPERATION_NAME}'),
    request_key TEXT NOT NULL CHECK(length(request_key) BETWEEN 1 AND 200),
    arguments_hash TEXT NOT NULL CHECK(length(arguments_hash) = 64 AND arguments_hash {_HEX}),
    source_binding_json TEXT NOT NULL CHECK(json_valid(source_binding_json) AND json_type(source_binding_json) = 'object'
        AND length(source_binding_json) <= 8192),
    expected_sha256 TEXT NOT NULL CHECK(length(expected_sha256) = 64 AND expected_sha256 {_HEX}),
    filename TEXT NOT NULL CHECK(length(CAST(filename AS BLOB)) BETWEEN 1 AND 255),
    send_as TEXT NOT NULL CHECK(send_as IN ('document','photo')),
    storage_name TEXT NOT NULL UNIQUE CHECK(length(storage_name) = 32 AND storage_name {_HEX}),
    max_bytes INTEGER NOT NULL CHECK(max_bytes BETWEEN 1 AND {MAX_BYTES['document']}
        AND (send_as = 'document' OR max_bytes <= {MAX_BYTES['photo']})),
    reserved_bytes INTEGER NOT NULL CHECK(reserved_bytes BETWEEN 0 AND {MAX_BYTES['document']}),
    state TEXT NOT NULL DEFAULT 'staging' CHECK(state IN ('staging','ready','attached','deleting','deleted')),
    size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes BETWEEN 1 AND {MAX_BYTES['document']}),
    sha256 TEXT CHECK(sha256 IS NULL OR (length(sha256) = 64 AND sha256 {_HEX} AND sha256 = expected_sha256)),
    metadata_json TEXT CHECK(metadata_json IS NULL OR (json_valid(metadata_json) AND json_type(metadata_json) = 'object'
        AND length(metadata_json) <= 4096)),
    sealed_at TEXT,
    lease_owner TEXT CHECK(lease_owner IS NULL OR (length(lease_owner) = 32 AND lease_owner {_HEX})),
    lease_expires_at TEXT,
    expires_at TEXT,
    message_id TEXT REFERENCES telegram_messages(id),
    outbox_id TEXT REFERENCES telegram_outbox(id),
    attached_at TEXT,
    failure_code TEXT CHECK(failure_code IS NULL OR length(failure_code) BETWEEN 1 AND 64),
    failure_message TEXT CHECK(failure_message IS NULL OR length(failure_message) BETWEEN 1 AND 500),
    cleanup_reason TEXT CHECK(cleanup_reason IS NULL OR cleanup_reason IN ('failed','expired','delivered')),
    cleanup_attempts INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_attempts >= 0),
    next_cleanup_at TEXT,
    error_code TEXT CHECK(error_code IS NULL OR length(error_code) BETWEEN 1 AND 64),
    error_message TEXT CHECK(error_message IS NULL OR length(error_message) BETWEEN 1 AND 500),
    error_at TEXT,
    deleted_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(installation_id, principal_id, operation_name, request_key),
    CHECK(size_bytes IS NULL OR size_bytes <= max_bytes),
    CHECK((size_bytes IS NULL) = (sha256 IS NULL) AND (sha256 IS NULL) = (sealed_at IS NULL)
        AND (sealed_at IS NULL) = (metadata_json IS NULL)),
    CHECK(state != 'staging' OR (sealed_at IS NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
        AND expires_at IS NOT NULL AND reserved_bytes = max_bytes AND message_id IS NULL)),
    CHECK(state = 'staging' OR (lease_owner IS NULL AND lease_expires_at IS NULL)),
    CHECK(state NOT IN ('ready','attached') OR (sealed_at IS NOT NULL AND reserved_bytes IS size_bytes)),
    CHECK(state != 'ready' OR (expires_at IS NOT NULL AND message_id IS NULL)),
    CHECK(state != 'attached' OR message_id IS NOT NULL),
    CHECK((message_id IS NULL) = (outbox_id IS NULL) AND (outbox_id IS NULL) = (attached_at IS NULL)),
    CHECK((failure_code IS NULL) = (failure_message IS NULL)),
    CHECK(failure_code IS NULL OR (sealed_at IS NULL AND message_id IS NULL AND cleanup_reason IS 'failed')),
    CHECK((error_code IS NULL) = (error_message IS NULL) AND (error_message IS NULL) = (error_at IS NULL)),
    CHECK(state NOT IN ('deleting','deleted') OR cleanup_reason IS NOT NULL),
    CHECK(state != 'deleted' OR (reserved_bytes = 0 AND deleted_at IS NOT NULL)),
    CHECK(state = 'deleted' OR deleted_at IS NULL)
) STRICT""",
    f"""CREATE INDEX telegram_file_payloads_state ON {TABLE}(state, expires_at)""",
    f"""CREATE UNIQUE INDEX telegram_file_payloads_message ON {TABLE}(message_id) WHERE message_id IS NOT NULL""",
    f"""CREATE UNIQUE INDEX telegram_file_payloads_outbox ON {TABLE}(outbox_id) WHERE outbox_id IS NOT NULL""",
    f"""CREATE TRIGGER telegram_file_payloads_identity BEFORE UPDATE ON {TABLE}
WHEN NEW.id IS NOT OLD.id OR NEW.installation_id IS NOT OLD.installation_id OR NEW.principal_id IS NOT OLD.principal_id
  OR NEW.operation_name IS NOT OLD.operation_name OR NEW.request_key IS NOT OLD.request_key
  OR NEW.arguments_hash IS NOT OLD.arguments_hash OR NEW.source_binding_json IS NOT OLD.source_binding_json
  OR NEW.expected_sha256 IS NOT OLD.expected_sha256 OR NEW.filename IS NOT OLD.filename OR NEW.send_as IS NOT OLD.send_as
  OR NEW.storage_name IS NOT OLD.storage_name OR NEW.max_bytes IS NOT OLD.max_bytes OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.sealed_at IS NOT NULL AND (NEW.size_bytes IS NOT OLD.size_bytes OR NEW.sha256 IS NOT OLD.sha256
      OR NEW.metadata_json IS NOT OLD.metadata_json OR NEW.sealed_at IS NOT OLD.sealed_at))
  OR (OLD.message_id IS NOT NULL AND (NEW.message_id IS NOT OLD.message_id OR NEW.outbox_id IS NOT OLD.outbox_id
      OR NEW.attached_at IS NOT OLD.attached_at))
  OR (OLD.failure_code IS NOT NULL AND (NEW.failure_code IS NOT OLD.failure_code OR NEW.failure_message IS NOT OLD.failure_message))
BEGIN
    SELECT RAISE(ABORT, 'A Telegram file payload keeps its request, manifest, sealed content and delivery association');
END""",
    f"""CREATE TRIGGER telegram_file_payloads_revision BEFORE UPDATE ON {TABLE}
WHEN NEW.revision IS NOT OLD.revision + 1
BEGIN
    SELECT RAISE(ABORT, 'A Telegram file payload changes by compare-and-swap: each update raises the revision by one');
END""",
    f"""CREATE TRIGGER telegram_file_payloads_state BEFORE UPDATE OF state ON {TABLE}
WHEN NEW.state != OLD.state AND NOT (
       (OLD.state = 'staging' AND NEW.state IN ('ready','deleting'))
    OR (OLD.state = 'ready' AND NEW.state IN ('attached','deleting'))
    OR (OLD.state = 'attached' AND NEW.state = 'deleting')
    OR (OLD.state = 'deleting' AND NEW.state = 'deleted'))
BEGIN
    SELECT RAISE(ABORT, 'Invalid Telegram file payload state transition');
END""",
    f"""CREATE TRIGGER telegram_file_payloads_insert BEFORE INSERT ON {TABLE}
WHEN NEW.state IS NOT 'staging'
BEGIN
    SELECT RAISE(ABORT, 'A Telegram file payload starts in staging');
END""",
    f"""CREATE TRIGGER telegram_file_payloads_associate BEFORE UPDATE ON {TABLE}
WHEN OLD.message_id IS NULL AND (NEW.message_id IS NOT NULL OR NEW.outbox_id IS NOT NULL OR NEW.attached_at IS NOT NULL)
  AND NOT (OLD.state = 'ready' AND NEW.state = 'attached')
BEGIN
    SELECT RAISE(ABORT, 'A Telegram file payload is associated with its delivery only when a ready payload attaches');
END""",
    f"""CREATE TRIGGER telegram_file_payloads_attach BEFORE UPDATE ON {TABLE}
WHEN OLD.state = 'ready' AND NEW.state = 'attached' AND NOT EXISTS (
    SELECT 1 FROM telegram_outbox o JOIN telegram_messages m ON m.id = o.message_id
    WHERE o.id = NEW.outbox_id AND m.id = NEW.message_id
      AND o.source_kind = 'agent_file' AND o.kind = NEW.send_as AND o.agent_id = NEW.principal_id AND o.state = 'queued'
      AND m.direction = 'outbound' AND m.agent_id = NEW.principal_id AND (m.outbox_id IS NULL OR m.outbox_id = NEW.outbox_id)
      AND CASE WHEN json_valid(o.payload_json) AND json_type(o.payload_json) = 'object' THEN
              (SELECT count(*) FROM json_each(o.payload_json)) = 2
              AND json_type(o.payload_json, '$.version') IS 'integer' AND json_extract(o.payload_json, '$.version') IS 1
              AND json_type(o.payload_json, '$.payload_id') IS 'text' AND json_extract(o.payload_json, '$.payload_id') IS NEW.id
          ELSE 0 END)
BEGIN
    SELECT RAISE(ABORT, 'A Telegram file payload attaches only to the queued agent file send of its own message');
END""",
    f"""CREATE TRIGGER telegram_file_payloads_open_delivery BEFORE UPDATE OF state ON {TABLE}
WHEN OLD.state = 'attached' AND NEW.state = 'deleting'
  AND (SELECT state FROM telegram_outbox WHERE id = OLD.outbox_id) NOT IN ('sent','failed','cancelled')
BEGIN
    SELECT RAISE(ABORT, 'A queued, dispatching or uncertain Telegram delivery keeps its file payload');
END""",
    f"""CREATE TRIGGER telegram_file_payloads_no_delete BEFORE DELETE ON {TABLE}
BEGIN
    SELECT RAISE(ABORT, 'Telegram file payload receipts are retained');
END""",
)


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, TABLE_SPECS, IMMUTABLE_FIELDS, SCHEMA)


__all__ = ["CLEANUP_REASONS", "IMMUTABLE_FIELDS", "MAX_BYTES", "OPERATION_NAME", "OWNER", "SCHEMA", "SEND_AS", "STATES", "TABLE",
           "TABLE_SPECS", "storage_contribution"]
