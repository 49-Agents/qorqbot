"""Deterministic, explicitly selected operational projections, never authority."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3

from bazarlyk.contracts import DomainError, canonical_bytes, canonical_hash
from .files import RegisteredFileTarget
from .repositories import TABLES, decode_row
from .safe_io import atomic_write


EXPORT_VERSION = 1
BUSINESS_TABLES = ("relationships", "context_reviews", "context_review_states", "context_entities", "data_tables", "data_rows", "table_versions", "stories", "story_events", "story_drafts", "products", "business_configs", "agent_profiles", "tasks", "task_notes",
    "documents", "action_logs", "action_log_labels", "requests",
    "learning_candidates", "learning_decisions", "learning_history", "learning_runs",
    "learning_issues", "learning_issue_candidates", "learning_issue_actions", "learning_issue_links", "learning_engine_packages",
    "routines", "routine_runs", "email_mailboxes", "email_conversations")
GLOBAL_TABLES = ("stories", "story_events", "story_drafts", "dev_tickets", "dev_ticket_events", "tasks", "task_notes", "relationships", "business_configs", "global_subscriptions", "ideas", "founder_focus", "agent_profiles", "documents", "routines", "routine_runs",
                 "learning_candidates", "learning_decisions", "learning_history", "learning_runs",
                 "learning_issues", "learning_issue_candidates", "learning_issue_actions", "learning_issue_links",
                 "learning_engine_packages",
                 # Global-stored actions and every action's business memberships (the action scope).
                 "action_logs", "action_businesses",
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
            raise DomainError("invalid_configuration", "Registered data path is outside the businesses root") from exc
    return RegisteredFileTarget(root, path.as_posix()).resolve()


def snapshot_connection(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def export_rows(connection, table, business_id=...):
    if table not in (*BUSINESS_TABLES, *GLOBAL_TABLES, "businesses", "principals", "artifacts"):
        raise ValueError("Table is not in the operational export catalog")
    if table == "dev_tickets" and connection.execute("PRAGMA user_version").fetchone()[0] < 58:
        return []
    if table == "dev_ticket_events" and connection.execute("PRAGMA user_version").fetchone()[0] < 60:
        return []
    if table in {"ideas", "founder_focus"} and connection.execute("PRAGMA user_version").fetchone()[0] < 38:
        return []
    if table in {"data_tables", "data_rows", "table_versions"} and connection.execute("PRAGMA user_version").fetchone()[0] < 12:
        return []
    if table == "products" and connection.execute("PRAGMA user_version").fetchone()[0] < 99:
        return []
    if table == "action_businesses" and connection.execute("PRAGMA user_version").fetchone()[0] < 101:
        return []
    if table.startswith(("learning_issue", "learning_engine_")) and connection.execute("PRAGMA user_version").fetchone()[0] < 103:
        return []
    if table in {"relationships", "context_reviews", "context_review_states", "context_entities"} and not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
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


def build_exports(snapshot, businesses_root):
    """Build from one immutable SQLite snapshot; no live multi-transaction reads."""
    root, files, counts = Path(businesses_root).resolve(strict=True), {}, {}

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
            hq = registered_path(root, business["hq_path"])
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


def publish_exports(bundle, businesses_root):
    """Replace only known projection filenames, publishing each scope manifest last."""
    changed = []
    for name in sorted(bundle.files, key=lambda name: (name.endswith("/manifest.json"), name)):
        path = RegisteredFileTarget(Path(businesses_root), name).resolve()
        if atomic_write(path, bundle.files[name]):
            changed.append(name)
    return {"fingerprint": bundle.fingerprint, "record_counts": bundle.counts, "changed": changed}
