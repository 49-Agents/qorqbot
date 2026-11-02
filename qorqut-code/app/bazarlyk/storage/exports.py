"""Deterministic, explicitly selected operational projections, never authority."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3

from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash
from qorqut_operations.storage.files import RegisteredFileTarget
from qorqut_operations.storage.specs import CORE_TABLES
from bazarlyk.composition.schema import record_contributions
from bazarlyk.composition.tables import TABLE_SPECS
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.storage.safe_io import atomic_write
from bazarlyk.composition.locations import business_hq

# Every table exists in a target store: the version-gated skips of the migrated v1 databases are gone.
# The engine's operational-record tables (Tasks, threads, tags, Actions, the Context queue) are specified by their owners.
TABLES = {**CORE_TABLES, **TABLE_SPECS, **{name: spec for part in record_contributions() for name, spec in part.tables.items()}}
EXPORT_VERSION = 1
BUSINESS_TABLES = ("relationships", "context_reviews", "context_review_states", "context_entities", "data_tables", "data_rows", "table_versions", "business_configs", "agent_profiles", "tasks", "thread_messages", "item_tags",
    "documents", "action_logs", "action_revisions",
    "learning_candidates", "learning_decisions", "learning_history", "learning_runs",
    "learning_issues", "learning_issue_candidates", "learning_issue_actions", "learning_issue_links", "learning_engine_packages",
    "routines", "routine_runs", "email_mailboxes", "email_conversations")
GLOBAL_TABLES = ("dev_tickets", "dev_ticket_events", "tasks", "thread_messages", "item_tags", "relationships", "business_configs", "agent_profiles", "documents", "routines", "routine_runs",
                 "learning_candidates", "learning_decisions", "learning_history", "learning_runs",
                 "learning_issues", "learning_issue_candidates", "learning_issue_actions", "learning_issue_links",
                 "learning_engine_packages",
                 # Global-stored actions and every action's business memberships (the action scope).
                 "action_logs", "action_revisions", "action_businesses", "tags",
                 # Global general tables share the business table stores under a NULL business.
                 "data_tables", "data_rows", "table_versions")
PROJECTION_NOTICE = "Generated from SQLite. Read-only history/recovery projection; edits are not imported."


def registered_path(root, value):
    root = Path(root).resolve(strict=True)
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise DomainError("invalid_configuration", "Registered data path is outside the operations root") from exc
    return RegisteredFileTarget(root, path.as_posix()).resolve()


def snapshot_connection(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def export_rows(connection, table, business_id=...):
    if table not in (*BUSINESS_TABLES, *GLOBAL_TABLES, "businesses", "principals", "artifacts"):
        raise ValueError("Table is not in the operational export catalog")
    spec = TABLES[table]
    where, parameters = "", ()
    if business_id is not ...:
        if not spec.scope_column:
            raise ValueError("Export table is not business-scoped")
        where, parameters = f' WHERE "{spec.scope_column}" IS ?', (business_id,)
    if table == 'documents':
        where += (' AND ' if where else ' WHERE ') + "kind != 'working_memory'"
    order = ", ".join(f'"{key}"' for key in spec.keys)
    selected = "business_id,id,table_id,table_revision,sequence,actor_id,operation,path,snapshot_hash,git_commit,created_at" if table == "table_versions" else "*"
    records = [decode_row(row) for row in connection.execute(f'SELECT {selected} FROM "{table}"{where} ORDER BY {order}', parameters)]
    if table == 'documents':
        for row in records:
            if isinstance(row.get('metadata_json'), dict):
                row['metadata_json'].pop('working_memory_id', None)
    return records


@dataclass(frozen=True)
class ExportBundle:
    files: dict[str, bytes]
    counts: dict[str, int]
    fingerprint: str


def build_exports(snapshot, paths):
    """Build from one immutable SQLite snapshot; no live multi-transaction reads."""
    root, files, counts = paths.operations, {}, {}

    def add(directory, table, rows, scope):
        filename = f"{directory}/{table}.json"
        data = {"schema_version": EXPORT_VERSION, "generated": True, "authority": "sqlite",
                "notice": PROJECTION_NOTICE, "scope": scope, "table": table, "records": rows}
        files[filename] = canonical_bytes(data) + b"\n"
        counts[filename] = len(rows)

    connection = snapshot_connection(snapshot)
    try:
        businesses = export_rows(connection, "businesses")
        add("exports", "businesses", businesses, "global")
        public_principals = [{key: row[key] for key in ("id", "kind", "display_name", "active", "revision")}
                             for row in export_rows(connection, "principals")]
        add("exports", "principals", public_principals, "global")
        add("exports", "artifacts", export_rows(connection, "artifacts"), "global")
        for table in GLOBAL_TABLES:
            add("exports", table, export_rows(connection, table, None if TABLES[table].scope_column else ...), "global")
        for business in businesses:
            hq = business_hq(paths, business)
            directory = hq.relative_to(root).as_posix() + "/exports"
            add(directory, "businesses", [business], business["id"])
            for table in BUSINESS_TABLES:
                add(directory, table, export_rows(connection, table, business["id"]), business["id"])
    finally:
        connection.close()
    hashes = {name: hashlib.sha256(body).hexdigest() for name, body in sorted(files.items())}
    fingerprint = canonical_hash(hashes)
    directories = {str(Path(name).parent) for name in files}
    for directory in sorted(directories):
        owned = {name.rsplit("/", 1)[1]: count for name, count in counts.items() if str(Path(name).parent) == directory}
        files[directory + "/manifest.json"] = canonical_bytes({"schema_version": EXPORT_VERSION, "generated": True,
            "authority": "sqlite", "notice": PROJECTION_NOTICE, "record_counts": owned,
            "content_hashes": {name.rsplit("/", 1)[1]: hashes[name] for name in sorted(hashes)
                               if str(Path(name).parent) == directory}}) + b"\n"
    return ExportBundle(files, counts, fingerprint)


def publish_exports(bundle, paths):
    """Replace only known projection filenames, publishing each scope manifest last."""
    changed = []
    for name in sorted(bundle.files, key=lambda name: (name.endswith("/manifest.json"), name)):
        path = RegisteredFileTarget(paths.operations, name).resolve()
        if atomic_write(path, bundle.files[name]):
            changed.append(name)
    return {"fingerprint": bundle.fingerprint, "record_counts": bundle.counts, "changed": changed}
