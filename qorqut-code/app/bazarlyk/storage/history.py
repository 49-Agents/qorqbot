"""The host's history adapter: learning documents and facts, working-memory history, and the composed callbacks.

Exact document history is generic and lives in ``qorqut_operations.document_history`` (the text and event mechanism, the file
receipt capture, document and Task events, the readers and the library's tables). This module keeps what only the host
knows and composes it into that mechanism, explicitly:

* ``SQL_DOCUMENTS``: the SQL projection descriptors of the learning tables (exact text fields rendered as deterministic JSON);
* ``learning_fact``: the host fact callback for the append-only ``learning_history`` table;
* ``capture_working_memory``: the byte-capture callback of private working memory, whose content is shared history and whose
  provenance stays in the host's tables;
* ``RECORDS`` and ``RECEIPTS``: the one record-history callback (the union of tracked tables and the per-record order) and the
  one receipt-history callback the host composition registers;
* working-memory version conversion and the whole-instance maintenance utilities (``migrate_100``, ``verify_working_memory``,
  ``import_facts``), which never run at serve time.

It never opens a connection, commits or rolls back; every write goes through the generic mechanism on the caller's
unit-of-work connection, inside the same transaction as the committed source change.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
import hashlib
import json

from qorqut_business_docs_versioning import HistoryStore, ImportConflict, NotFound, initialize_schema

from qorqut_operations.contracts import DomainError
from qorqut_operations.document_history.files import DocumentReceiptHistory
from qorqut_operations.document_history.records import RecordHistory
from qorqut_operations.document_history.schema import HISTORY_TABLES
from qorqut_operations.document_history.text import BASELINE_AUTHOR, SqlDocuments, append_event, aware, latest, namespace, save_text

WORKING_MEMORY = "working_memory"
# SQL-authored documents: exact text fields rendered as deterministic JSON.
# Revisions, timestamps and counters are deliberately excluded.
SQL_DOCUMENTS = {
    "learning_candidates": ("learning-candidate", "id", ("candidate_json", "agent_note")),
    "learning_decisions": ("learning-decision", "candidate_id", ("feedback", "changes_json")),
}
# Append-only source facts of the host's own tables, summarised as business activity (Action logs are generic).
FACT_TABLES = ("learning_history",)
SOURCE_KINDS = {"learning_candidate": "learning_candidates", "learning_decision": "learning_decisions"}
SQL = SqlDocuments(SQL_DOCUMENTS)


def sql_document_id(table, row):
    return SQL.document_id(table, row)


def projection(table, row):
    return SQL.projection(table, row)


def baseline_sql_documents(connection, *, reason, apply=True):
    """Baseline each SQL document with no shared history; returns captured IDs."""
    captured = []
    for table in SQL_DOCUMENTS:
        cursor = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        names = [column[0] for column in cursor.description]
        for raw in cursor.fetchall():
            row = {name: (json.loads(value) if name.endswith("_json") and value is not None else value)
                   for name, value in zip(names, raw)}
            space, document_id = namespace(row.get("business_id")), SQL.document_id(table, row)
            if latest(connection, space, document_id) is not None:
                continue
            if apply:
                save_text(connection, space, document_id, SQL.projection(table, row), BASELINE_AUTHOR, reason)
            captured.append({"source_kind": table, "namespace": space, "document_id": document_id})
    return captured


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


# Learning facts and the composed callbacks


def learning_fact(row, *, operation):
    """The summary event for one committed learning change; its ID is the source row's."""
    return {"business_id": row["business_id"], "event_id": f"learning-history:{row['id']}", "kind": "learning.changed",
            "actor_id": row["actor_id"], "summary": "Learning changed", "subject_type": "learning",
            "subject_id": row["candidate_id"], "occurred_at": row["created_at"], "data": {"learning_id": row["candidate_id"],
            "learning_history_id": row["id"], "event": row["event"], "operation": operation}}


# The one record-history callback (tracked tables and per-record order) and the one receipt-history callback.
RECORDS = RecordHistory(sql_documents=SQL, facts={"learning_history": learning_fact})
RECEIPTS = DocumentReceiptHistory(byte_capture={WORKING_MEMORY: capture_working_memory})
TRACKED_TABLES = RECORDS.tracked_tables


def record_applied(uow, plan, table, before, after, *, actor_id=None):
    """History and activity for one committed record change, in its own transaction."""
    return RECORDS.record_applied(uow, plan, table, before, after, actor_id=actor_id)


def capture_receipt(uow, receipt, plan):
    """Final verified file effects of an applied receipt, in its finalising transaction."""
    return RECEIPTS.capture_receipt(uow, receipt, plan)


# Whole-instance maintenance


def import_facts(connection, *, apply):
    """Existing append-only source facts with a real actor and time, under the same IDs as live capture.

    Present events are left untouched; rows without a business, actor or timezone-aware time are
    reported, never guessed. Task completions are not imported: task_completions has no actor and
    no revision identity, and historical completion times may be unknown.
    """
    report = {"imported": 0, "present": 0, "missing_evidence": 0}
    store = HistoryStore(connection)
    for table, fact_event in RECORDS.facts.items():
        cursor = connection.execute(f'SELECT * FROM "{table}" WHERE business_id IS NOT NULL ORDER BY rowid')
        names = [column[0] for column in cursor.description]
        for raw in cursor.fetchall():
            event = fact_event(dict(zip(names, raw)), operation="history_import")
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
