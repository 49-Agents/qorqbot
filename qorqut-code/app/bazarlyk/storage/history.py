"""Shared document versions and business activity for core records.

Current content stays in its registered file or domain table. The shared
``qorqut-business-docs-versioning`` library owns content history and business
activity facts; every write here goes through it on the caller's unit-of-work
connection, inside the same transaction as the committed source change. This
module never opens a connection, commits or rolls back.

Namespaces are ``business:<uuid>`` or ``global``. They scope storage, not
authority: history readers must apply the owning resource's access rules first.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime, timezone

from qorqut_business_docs_versioning import HistoryStore, ImportConflict, NotFound, initialize_schema

from qorqut_operations.actions import one_line
from qorqut_operations.contracts import DomainError, plain, utc_now

HISTORY_TABLES = ("qbdv_document_versions", "qbdv_business_events")
BASELINE_AUTHOR = "system:history-baseline"
EXTERNAL_AUTHOR = "system:external-content"
WORKING_MEMORY = "working_memory"
# Exact kinds, never name patterns: binary assets/data uploads are not text, and
# source email messages are immutable originals. Editable uploads, dumps, maps and
# pillars are authored text and tracked like every other registered kind.
UNTRACKED_KINDS = frozenset({"asset", "data_upload", "email_message"})
# SQL-authored documents: exact text fields rendered as deterministic JSON.
# Revisions, timestamps and counters are deliberately excluded.
SQL_DOCUMENTS = {
    "learning_candidates": ("learning-candidate", "id", ("candidate_json", "agent_note")),
    "learning_decisions": ("learning-decision", "candidate_id", ("feedback", "changes_json")),
}
# Tables whose committed changes record history or business activity.
# Append-only source facts summarised as business activity.
FACT_TABLES = ("action_logs", "milestone_events", "learning_history")
TRACKED_TABLES = frozenset({*SQL_DOCUMENTS, *FACT_TABLES, "documents", "tasks"})
SOURCE_KINDS = {"learning_candidate": "learning_candidates", "learning_decision": "learning_decisions"}


def namespace(business_id):
    return "business:" + business_id if business_id else "global"


def store(connection):
    return HistoryStore(connection)


def latest(connection, space, document_id):
    try:
        return HistoryStore(connection).get_document(space, document_id)
    except NotFound:
        return None


def aware(value):
    """A timezone-aware ISO timestamp, or None when the source did not record one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def save_text(connection, space, document_id, text, author, reason, *, before=None,
              before_author=BASELINE_AUTHOR, before_reason="History baseline: content before the first tracked change",
              drift_author=None, drift_reason=None):
    """Append text unless it equals the latest version; returns the latest version.

    ``before`` is proven pre-change content. With no history yet it becomes a
    labelled baseline. When history exists but differs from ``before``, the
    observed content is kept only if a ``drift_author`` is given (an explicitly
    accepted out-of-band change), never attributed to an earlier author.
    """
    history = HistoryStore(connection)
    current = latest(connection, space, document_id)
    if before is not None and before != text:
        if current is None:
            current = history.save_document(space, document_id, before, before_author,
                                             expected_version=0, reason=before_reason)
        elif current.content != before and drift_author:
            current = history.save_document(space, document_id, before, drift_author,
                                             expected_version=current.version, reason=drift_reason)
    if current is not None and current.content == text:
        return current
    return history.save_document(space, document_id, text, author,
                                 expected_version=current.version if current else 0, reason=reason)


def append_event(connection, business_id, event_id, kind, actor_id, summary, *, subject_type, subject_id,
                 occurred_at, data=None):
    """Business-scoped facts only; Global actions and unknown times are not invented."""
    occurred = aware(occurred_at)
    if not business_id or occurred is None or not actor_id:
        return None
    return HistoryStore(connection).append_event(business_id, event_id, kind, actor_id, summary,
        subject_type=subject_type, subject_id=subject_id, occurred_at=occurred,
        data={key: value for key, value in (data or {}).items() if value is not None})


# SQL-authored documents


def sql_document_id(table, row):
    prefix, key, _ = SQL_DOCUMENTS[table]
    return f"{prefix}:{row[key]}"


def projection(table, row):
    fields = SQL_DOCUMENTS[table][2]
    return json.dumps({field: row.get(field) for field in fields}, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def capture_sql_document(connection, table, before, after, *, author, operation):
    """Record a SQL document's text projection in the source change's transaction."""
    row = after or before
    space, document_id = namespace(row.get("business_id")), sql_document_id(table, row)
    old = projection(table, before) if before else None
    if after is None:
        if old is not None and latest(connection, space, document_id) is None:
            save_text(connection, space, document_id, old, BASELINE_AUTHOR,
                      "History baseline: content before deletion; earlier history not recorded")
        return None
    revision = after.get("revision")
    reason = f"{operation}; {table} revision {revision}" if revision is not None else operation
    return save_text(connection, space, document_id, projection(table, after), author, reason, before=old)


def baseline_sql_documents(connection, *, reason, apply=True):
    """Baseline each SQL document with no shared history; returns captured IDs."""
    captured = []
    for table in SQL_DOCUMENTS:
        cursor = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        names = [column[0] for column in cursor.description]
        for raw in cursor.fetchall():
            row = {name: (json.loads(value) if name.endswith("_json") and value is not None else value)
                   for name, value in zip(names, raw)}
            space, document_id = namespace(row.get("business_id")), sql_document_id(table, row)
            if latest(connection, space, document_id) is not None:
                continue
            if apply:
                save_text(connection, space, document_id, projection(table, row), BASELINE_AUTHOR, reason)
            captured.append({"source_kind": table, "namespace": space, "document_id": document_id})
    return captured


# Registered files


def _plan_previews(plan):
    preview = plain(plan.preview)
    result = {}
    for item in preview.get("changes", ()):
        if item.get("kind") == "text" and item["resource"]["resource_type"] == "documents":
            result.setdefault(item["resource"]["resource_id"], []).append(item["before"])
    return result


def proven_before(previews, document_id, before_hash):
    """Pre-edit text frozen in the plan preview, only when it hashes to the verified before bytes."""
    if not before_hash:
        return None
    for text in previews.get(document_id, ()):
        if hashlib.sha256(text.encode("utf-8")).hexdigest() == before_hash.removeprefix("sha256:"):
            return text
    return None


def file_effects(plan):
    """Final written bytes and before hashes of managed document files, per document."""
    writes, deleted = {}, {}
    for change in plan.changes:
        if change.kind != "file" or change.resource.resource_type != "managed_document_files":
            continue
        document_id = json.loads(change.resource.resource_id)["document_id"]
        if change.action == "delete":
            deleted[document_id] = change.values["before_hash"]
        elif change.values.get("content_b64") is not None:
            writes[document_id] = (base64.b64decode(change.values["content_b64"], validate=True), change.values["before_hash"])
    return writes, deleted


def capture_receipt(uow, receipt, plan):
    """Final verified file effects of an applied receipt, in its finalising transaction."""
    connection = uow.connection
    writes, deleted = file_effects(plan)
    previews = _plan_previews(plan) if writes else {}
    pending = getattr(uow, "history_events", {})
    for document_id, (data, before_hash) in writes.items():
        row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        if row is None or row["kind"] in UNTRACKED_KINDS:
            pending.pop(document_id, None)
            continue
        if hashlib.sha256(data).hexdigest() != row["content_hash"].removeprefix("sha256:"):
            raise DomainError("apply_incomplete", "Committed document does not match its verified file effect")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            pending.pop(document_id, None)
            continue
        if row["kind"] == WORKING_MEMORY:
            version = capture_working_memory(connection, row, text, receipt["id"])
        else:
            before = proven_before(previews, document_id, before_hash or deleted.get(document_id))
            reason = f"{plan.operation_name}; documents revision {row['revision']}; receipt {receipt['id']}"
            if before is None and latest(connection, namespace(row["business_id"]), document_id) is None and before_hash:
                reason += "; earlier content was not recorded, history starts here"
            version = save_text(connection, namespace(row["business_id"]), document_id, text, plan.principal_id, reason,
                before=before, drift_author=EXTERNAL_AUTHOR,
                drift_reason="Observed external content accepted by the next edit; author unknown")
        event = pending.pop(document_id, None)
        if event is not None:
            event["data"].update(history_version=version.version, receipt_id=receipt["id"], signoff_id=receipt["signoff_id"])
            append_event(connection, **event)
    for document_id in list(pending):
        # A record-only change of a document in this plan has no file effect to wait for.
        append_event(connection, **pending.pop(document_id))


def seed_document(connection, row, data, *, author, operation, plan=None):
    """Creation of a registered document outside DocumentStore (bootstrap, conversion).

    ``data`` must be trusted bytes already in the seed or plan; they are checked
    against the registered hash. Records version 1 and a creation event.
    """
    if row["kind"] in UNTRACKED_KINDS:
        return None
    if hashlib.sha256(data).hexdigest() != row["content_hash"].removeprefix("sha256:"):
        raise DomainError("document_conflict", "Seeded document bytes do not match the registered hash")
    version = save_text(connection, namespace(row["business_id"]), row["id"], data.decode("utf-8"), author,
                        f"{operation}; documents revision {row['revision']}; created")
    event = document_event(None, row, actor_id=author, operation=operation, plan=plan)
    if event is not None:
        event["data"]["history_version"] = version.version
        append_event(connection, **event)
    return version


def document_changed(connection, before, after, *, actor_id, operation):
    """Lifecycle event for a record-only documents change made by a trusted bypass writer."""
    event = document_event(before, after, actor_id=actor_id, operation=operation)
    return append_event(connection, **event) if event is not None else None


def document_event(row_before, row_after, *, actor_id, operation, plan=None):
    """The lifecycle event for a committed documents row change, or None."""
    row = row_after or row_before
    if row["kind"] in UNTRACKED_KINDS or not row.get("business_id"):
        return None
    if row_after is None:
        kind, revision, occurred = "document.deleted", row_before["revision"], utc_now()
    elif row_before is None:
        kind, revision, occurred = "document.created", row_after["revision"], row_after["updated_at"]
    elif row_after["revision"] == row_before["revision"]:
        return None
    else:
        revision, occurred = row_after["revision"], row_after["updated_at"]
        if bool(row_before.get("archived")) != bool(row_after.get("archived")):
            kind = "document.archived" if row_after.get("archived") else "document.unarchived"
        elif row_before["path"] != row_after["path"]:
            kind = "document.moved"
        else:
            kind = "document.updated"
    data = {"document_id": row["id"], "document_kind": row["kind"], "revision": revision, "operation": operation}
    if row_before is not None and row_after is not None:
        data["content_changed"] = row_before["content_hash"] != row_after["content_hash"]
    if plan is not None:
        data.update(initiated_by_principal_id=plan.initiated_by_principal_id, proposer_agent_id=plan.proposer_agent_id)
    verb = kind.split(".", 1)[1]
    return {"business_id": row["business_id"], "event_id": f"document:{row['id']}:r{revision}:{verb}", "kind": kind,
            "actor_id": actor_id, "summary": f"Document {verb}", "subject_type": "document", "subject_id": row["id"],
            "occurred_at": occurred, "data": data}


# Working memory: shared content, private provenance


def capture_working_memory(connection, row, text, receipt_id):
    """The single writer of working-memory content versions; numbering is the document revision."""
    space, document_id, version = namespace(row["business_id"]), row["id"], row["revision"]
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    provenance = connection.execute("SELECT * FROM working_memory_provenance WHERE document_id=? AND version=?",
                                    (document_id, version)).fetchone()
    history = HistoryStore(connection)
    if provenance is None:
        # A receipt frozen before the shared backend inserted the legacy version row.
        legacy = connection.execute("SELECT * FROM working_memory_versions WHERE document_id=? AND version=?",
                                    (document_id, version)).fetchone()
        if legacy is None:
            raise DomainError("apply_incomplete", "Working-memory version has no committed provenance")
        _copy_provenance(connection, legacy)
        return _import_memory_version(history, space, legacy, text)
    if provenance["content_hash"].removeprefix("sha256:") != digest:
        raise DomainError("apply_incomplete", "Working-memory provenance does not match the committed bytes")
    return history.save_document(space, document_id, text, provenance["author_id"], expected_version=version - 1,
                                 reason=f"working_memory_update; documents revision {version}; receipt {receipt_id}")


def _copy_provenance(connection, legacy):
    connection.execute(
        "INSERT INTO working_memory_provenance(document_id,version,business_id,content_hash,source_refs_json,"
        "legacy_plan_json,author_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (legacy["document_id"], legacy["version"], legacy["business_id"], legacy["content_hash"],
         legacy["source_refs_json"], legacy["legacy_plan_json"], legacy["author_id"], legacy["created_at"]))


def _import_memory_version(history, space, legacy, text=None):
    try:
        data = base64.b64decode(legacy["file_bytes"], validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise DomainError("migration_conflict", "Working-memory version bytes are invalid") from exc
    if hashlib.sha256(data).hexdigest() != legacy["content_hash"].removeprefix("sha256:"):
        raise DomainError("migration_conflict", "Working-memory version bytes do not match their hash")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DomainError("migration_conflict", "Working-memory version is not UTF-8 text") from exc
    if text is not None and text != content:
        raise DomainError("apply_incomplete", "Working-memory version differs from the committed file")
    if aware(legacy["created_at"]) is None:
        raise DomainError("migration_conflict", "Working-memory version has no timezone-aware creation time")
    try:
        return history.import_document_version(space, legacy["document_id"], legacy["version"], content,
            legacy["author_id"], created_at=legacy["created_at"], reason="Imported from working_memory_versions")
    except ImportConflict as exc:
        raise DomainError("migration_conflict", "Working-memory history is gapped or conflicts with shared history") from exc


def has_schema(connection):
    """Whether migration 100's history and provenance tables exist (offline tools on older copies)."""
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {*HISTORY_TABLES, "working_memory_provenance"} <= names


def import_converted_memory(connection, legacy):
    """An offline conversion's new working-memory version: provenance plus shared content, no legacy row."""
    _copy_provenance(connection, legacy)
    return _import_memory_version(HistoryStore(connection), namespace(legacy["business_id"]), legacy)


def working_memory_version(connection, business_id, document_id, version=None):
    """One committed version joined with its provenance, shaped like the legacy row; None if absent."""
    if version is None:
        provenance = connection.execute("SELECT * FROM working_memory_provenance WHERE document_id=? AND business_id IS ? "
            "ORDER BY version DESC LIMIT 1", (document_id, business_id)).fetchone()
    else:
        provenance = connection.execute("SELECT * FROM working_memory_provenance WHERE document_id=? AND business_id IS ? "
            "AND version=?", (document_id, business_id, version)).fetchone()
    if provenance is None:
        return None
    try:
        content = HistoryStore(connection).get_document(namespace(business_id), document_id, version=provenance["version"])
    except NotFound as exc:
        raise DomainError("memory_drift", "Working-memory version content is missing from shared history") from exc
    return {"document_id": document_id, "version": provenance["version"], "business_id": business_id,
            "file_bytes": base64.b64encode(content.content.encode("utf-8")).decode("ascii"),
            "content_hash": provenance["content_hash"],
            "source_refs_json": json.loads(provenance["source_refs_json"]),
            "legacy_plan_json": json.loads(provenance["legacy_plan_json"]),
            "author_id": provenance["author_id"], "created_at": provenance["created_at"]}


# Schema migration 100


def migrate_100(connection):
    """Install shared history, import complete working memory, baseline SQL documents.

    Runs inside Database.initialize's migration transaction after 100's SQL
    (which copied provenance) and before the migration is recorded.
    """
    initialize_schema(connection)
    history = HistoryStore(connection)
    legacy_rows = connection.execute("SELECT * FROM working_memory_versions ORDER BY document_id, version").fetchall()
    for legacy in legacy_rows:
        _import_memory_version(history, namespace(legacy["business_id"]), legacy)
    problems = verify_working_memory(connection)
    if problems:
        raise DomainError("migration_conflict", "Working-memory history is incomplete or inconsistent with its registered "
                          "documents", details={"problems": problems[:20], "problem_count": len(problems)})
    baseline_sql_documents(connection, reason="History baseline at schema migration 100; earlier edits were not recorded")


def validate_schema(connection):
    """Read-only startup check; never creates or changes history tables."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(HISTORY_TABLES) <= tables:
        raise DomainError("invalid_database", "Existing database is missing shared history tables")


PROVENANCE_FIELDS = ("business_id", "content_hash", "source_refs_json", "legacy_plan_json", "author_id", "created_at")


def _same_time(left, right):
    try:
        return datetime.fromisoformat(left) == datetime.fromisoformat(right)
    except (TypeError, ValueError):
        return False


def verify_working_memory(connection):
    """Problems between registered working memories, their provenance and shared content; [] when consistent.

    Every registered memory has a complete chain 1..n in its own scope, whose shared content, authors and
    hashes match its provenance (and imported legacy rows exactly, including their original times). The
    latest version is the registered revision and content hash. The one lifecycle-only exception is an
    archived memory: offline conversion archives a retired agent's memory by incrementing the revision
    without new content, so its latest version may be older than the revision but must hold its content.
    """
    problems = []

    def problem(document_id, kind, version=None):
        problems.append({"document_id": document_id, "problem": kind,
                         **({"version": version} if version is not None else {})})

    store = HistoryStore(connection)
    documents = {row["id"]: row for row in connection.execute(
        "SELECT id, business_id, kind, revision, content_hash, archived FROM documents WHERE kind=? OR id IN "
        "(SELECT document_id FROM working_memory_provenance UNION SELECT document_id FROM working_memory_versions)",
        (WORKING_MEMORY,))}
    for document_id, document in sorted(documents.items()):
        if document["kind"] != WORKING_MEMORY:
            problem(document_id, "kind_mismatch")
            continue
        chain = connection.execute("SELECT * FROM working_memory_provenance WHERE document_id=? ORDER BY version",
                                   (document_id,)).fetchall()
        legacy = {row["version"]: row for row in connection.execute(
            "SELECT * FROM working_memory_versions WHERE document_id=?", (document_id,))}
        for version, row in sorted(legacy.items()):
            copied = next((item for item in chain if item["version"] == version), None)
            if copied is None:
                problem(document_id, "missing_provenance", version)
            elif any(copied[field] != row[field] for field in PROVENANCE_FIELDS):
                problem(document_id, "provenance_mismatch", version)
        if not chain:
            problem(document_id, "missing_chain")
            continue
        if [row["version"] for row in chain] != list(range(1, len(chain) + 1)):
            problem(document_id, "gapped_chain")
        if any(row["business_id"] != document["business_id"] for row in chain):
            problem(document_id, "scope_mismatch")
            continue
        space = namespace(document["business_id"])
        for row in chain:
            try:
                content = store.get_document(space, document_id, version=row["version"])
            except NotFound:
                problem(document_id, "missing_content", row["version"])
                continue
            if content.content_sha256 != row["content_hash"].removeprefix("sha256:"):
                problem(document_id, "hash_mismatch", row["version"])
            elif content.author_id != row["author_id"]:
                problem(document_id, "author_mismatch", row["version"])
            elif row["version"] in legacy and not _same_time(content.created_at, row["created_at"]):
                problem(document_id, "time_mismatch", row["version"])
        newest, last = latest(connection, space, document_id), chain[-1]
        if newest is not None and newest.version > last["version"]:
            problem(document_id, "unexpected_content", newest.version)
        if (last["content_hash"] or "").removeprefix("sha256:") != (document["content_hash"] or "").removeprefix("sha256:"):
            problem(document_id, "missing_latest", document["revision"])
        elif last["version"] != document["revision"] and not (document["archived"] and last["version"] < document["revision"]):
            problem(document_id, "revision_mismatch", document["revision"])
    return problems


# Committed record changes (apply_records and explicit bypass writers)


def closed_states(connection, business_id):
    from qorqut_operations.tasks import CLOSED_STATES
    return set(CLOSED_STATES)


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _bounded(refs):
    if not isinstance(refs, dict):
        return None
    return {key: str(value)[:200] for key, value in sorted(refs.items())[:20]}


def record_applied(uow, plan, table, before, after, *, actor_id=None):
    """History and activity for one committed record change, in its own transaction."""
    if table not in TRACKED_TABLES:
        return None
    connection = uow.connection
    actor = actor_id or plan.principal_id
    delegated = {"operation": plan.operation_name, "initiated_by_principal_id": plan.initiated_by_principal_id,
                 "proposer_agent_id": plan.proposer_agent_id}
    row = after or before
    if table in SQL_DOCUMENTS:
        version = capture_sql_document(connection, table, before, after, author=actor, operation=plan.operation_name)
        if after is None and row.get("business_id"):
            prefix, key, _ = SQL_DOCUMENTS[table]
            subject = prefix.replace("-", "_")
            append_event(connection, row["business_id"], f"{prefix}:{row[key]}:deleted", f"{subject}.deleted", actor,
                subject.replace("_", " ").capitalize() + " deleted", subject_type=subject, subject_id=row[key],
                occurred_at=utc_now(), data={**delegated, "history_document_id": sql_document_id(table, row),
                                             "revision": row.get("revision")})
        return version
    if table == "documents":
        event = document_event(before, after, actor_id=actor, operation=plan.operation_name, plan=plan)
        if event is None:
            return None
        writes, deleted = file_effects(plan)
        if row["id"] in writes and after is not None:
            uow.history_events[row["id"]] = event
        else:
            append_event(connection, **event)
        return None
    if not row.get("business_id") or after is None:
        return None
    if table == "tasks":
        return task_event(connection, before, after, actor_id=actor, data=delegated)
    if before is not None or table not in FACT_TABLES:
        return None
    return append_event(connection, **fact_event(table, after, operation=plan.operation_name))


def task_event(connection, before, after, *, actor_id, data):
    """task.completed / task.reopened when a committed change crosses the business's configured closed states."""
    if not after.get("business_id"):
        return None
    closed = closed_states(connection, after["business_id"])
    was, now = before is not None and before["status"] in closed, after["status"] in closed
    if was == now:
        return None
    verb = "completed" if now else "reopened"
    return append_event(connection, after["business_id"], f"task:{after['id']}:r{after['revision']}:{verb}",
        f"task.{verb}", actor_id, f"Task {verb}", subject_type="task", subject_id=after["id"],
        occurred_at=after["updated_at"], data={**data, "task_id": after["id"], "status": after["status"],
        "previous_status": before["status"] if before else None, "revision": after["revision"]})


def milestone_event(connection, row, *, operation):
    return append_event(connection, **fact_event("milestone_events", row, operation=operation))


def fact_event(table, row, *, operation):
    """The summary event for one committed append-only source fact; its ID is the source row's."""
    if table == "action_logs":
        return {"business_id": row["business_id"], "event_id": f"action:{row['id']}", "kind": "action.recorded",
                "actor_id": row["recorded_by"], "summary": one_line(row["action_text"]), "subject_type": "action",
                "subject_id": row["id"], "occurred_at": row["occurred_at"], "data": {"log_id": row["id"], "action_key": row["action_key"],
                "refs": _bounded(_json(row.get("refs_json"))), "operation": operation}}
    if table == "learning_history":
        return {"business_id": row["business_id"], "event_id": f"learning-history:{row['id']}", "kind": "learning.changed",
                "actor_id": row["actor_id"], "summary": "Learning changed", "subject_type": "learning",
                "subject_id": row["candidate_id"], "occurred_at": row["created_at"], "data": {"learning_id": row["candidate_id"],
                "learning_history_id": row["id"], "event": row["event"], "operation": operation}}
    if table == "milestone_events":
        return {"business_id": row["business_id"], "event_id": f"milestone-event:{row['id']}", "kind": "milestone.changed",
                "actor_id": row["actor_id"], "summary": "Milestone changed", "subject_type": "milestone",
                "subject_id": row["milestone_id"], "occurred_at": row["created_at"], "data": {"milestone_id": row["milestone_id"],
                "milestone_event_id": row["id"], "status": row["status"], "operation": operation}}
    raise ValueError(table)


def import_facts(connection, *, apply):
    """Existing append-only source facts with a real actor and time, under the same IDs as live capture.

    Present events are left untouched; rows without a business, actor or timezone-aware time are
    reported, never guessed. Task completions are not imported: task_completions has no actor and
    no revision identity, and historical completion times may be unknown.
    """
    report = {"imported": 0, "present": 0, "missing_evidence": 0}
    store = HistoryStore(connection)
    for table in FACT_TABLES:
        cursor = connection.execute(f'SELECT * FROM "{table}" WHERE business_id IS NOT NULL ORDER BY rowid')
        names = [column[0] for column in cursor.description]
        for raw in cursor.fetchall():
            event = fact_event(table, dict(zip(names, raw)), operation="history_import")
            if not event["actor_id"] or aware(event["occurred_at"]) is None:
                report["missing_evidence"] += 1
                continue
            try:
                store.get_event(event["business_id"], event["event_id"])
                report["present"] += 1
                continue
            except NotFound:
                pass
            if apply:
                append_event(connection, **event)
            report["imported"] += 1
    report["task_completions_not_imported"] = connection.execute(
        "SELECT COUNT(*) FROM task_completions").fetchone()[0]
    return report
