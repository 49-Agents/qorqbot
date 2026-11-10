"""Retention of settled bookkeeping: which old records may be removed, and removing exactly those.

Three kinds of record are written for every piece of background or tool work and were never removed, so they are most
of the database: a routine run with its job, the idempotency receipt of a keyed call, and the application receipt of a
prepared change. Each rule below names the rows of one kind that have no further use once they are older than the
retention window, and nothing else is ever touched.

``routine-runs``
    A scheduled or manual routine run that succeeded, together with the job it started. Only the pair is removed: the
    run names the job, the job names the run, both succeeded, the job's dedupe key is its own ID (so removing it cannot
    let anything run twice) and it records no external receipt or dispatch intent. Failed runs stay; they are the ones
    operational history exports and the ones a person looks for.

``idempotency-receipts``
    The saved result of a keyed call. Its only use is to answer a repeat of the same key with the same result. After
    the window a repeat of that key is a new call. This is the one rule that changes behaviour: keys are remembered for
    the window, not for ever.

``apply-receipts``
    The receipt of a prepared change that was applied, is not bound to a signoff and holds no resource claim. An
    applied receipt is read again only to answer a repeat of the same application; a signoff's receipt is how an
    approved signoff is known to be applied, so those are never removed, and neither is anything unfinished.

A row that another table references through a foreign key is never eligible, whichever table that is: the references
are read from the schema, and a reference this module does not understand makes the rule refuse instead of guess.
Removal runs in small transactions with foreign keys enforced, every delete repeats the rule's own conditions, and a
count that differs from the selection rolls its batch back. Freed pages are reused by SQLite; the file itself only
shrinks with ``VACUUM``, which is a separate, explicit step.

Nothing here runs on its own. The host command (``qorqut.migration.record_retention``) inspects read-only and applies
only under exclusive instance ownership.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from qorqut_operations.contracts import DomainError

RULES = ("routine-runs", "idempotency-receipts", "apply-receipts")
DEFAULT_DAYS = 30
MINIMUM_DAYS = 7
BATCH = 500

_TABLES = {"routine-runs": ("routine_runs", "jobs"), "idempotency-receipts": ("idempotency_receipts",),
           "apply-receipts": ("apply_receipts", "resource_claims")}
_COLUMNS = {
    "routine_runs": {"id", "state", "started_at", "finished_at", "result_json"},
    "jobs": {"id", "state", "dedupe_key", "definition_ref_json", "result_json", "updated_at"},
    "idempotency_receipts": {"created_at", "result_json"},
    "apply_receipts": {"id", "state", "signoff_id", "updated_at", "change_set", "progress_json", "result_json"},
    "resource_claims": {"apply_id"},
}


def cutoff(now: datetime, days: int) -> str:
    """The instant before which a settled record is old, in the form the tables store (UTC, ``Z``)."""
    if type(days) is not int or days < MINIMUM_DAYS:
        raise DomainError("validation_error", f"The retention window is at least {MINIMUM_DAYS} days")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise DomainError("validation_error", "Retention needs the current time with its zone")
    return (now.astimezone(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require(connection, rule):
    if rule not in RULES:
        raise DomainError("validation_error", f"Unknown retention rule: {rule}")
    for table in _TABLES[rule]:
        present = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if not _COLUMNS[table] <= present:
            raise DomainError("invalid_database", f"Retention does not recognise the {table} table of this store")


def _unreferenced(connection, table, alias):
    """SQL conditions excluding every row of ``table`` that a foreign key of any other table points at."""
    references = {}
    for child, number, column, target in connection.execute(
            'SELECT m.name, f.id, f."from", f."to" FROM sqlite_master m, pragma_foreign_key_list(m.name) f '
            "WHERE m.type = 'table' AND f.\"table\" = ? ORDER BY m.name, f.id, f.seq", (table,)):
        references.setdefault((child, number), []).append((column, target))
    conditions = []
    for (child, _), columns in sorted(references.items()):
        if len(columns) != 1 or columns[0][1] not in ("id", None):
            raise DomainError("invalid_database", f"Retention does not understand how {child} references {table}")
        if child == "resource_claims":
            continue                                      # named by the rule itself
        conditions.append(f' AND NOT EXISTS (SELECT 1 FROM "{child}" WHERE "{child}"."{columns[0][0]}" = {alias}.id)')
    return "".join(conditions)


def _selection(connection, rule):
    """``(select, sizes)``: the eligible keys before the cutoff ``:before``, and the stored size of what they hold."""
    if rule == "routine-runs":
        condition = ("FROM routine_runs r JOIN jobs j ON j.id = json_extract(r.result_json, '$.job_id') "
                     "WHERE r.state = 'succeeded' AND r.started_at < :before AND r.finished_at IS NOT NULL AND r.finished_at < :before "
                     "AND j.state = 'succeeded' AND j.updated_at < :before AND j.dedupe_key = j.id "
                     "AND json_extract(j.definition_ref_json, '$.routine_run_id') = r.id "
                     "AND coalesce(json_type(j.result_json, '$.external_receipt'), 'null') = 'null' "
                     "AND coalesce(json_type(j.result_json, '$.dispatch_intent'), 'null') = 'null'"
                     + _unreferenced(connection, "routine_runs", "r") + _unreferenced(connection, "jobs", "j"))
        return ("SELECT r.id, j.id " + condition + " ORDER BY r.started_at, r.id",
                "SELECT count(*), min(r.started_at), max(r.started_at), coalesce(sum(length(r.result_json) "
                "+ length(j.arguments_json) + length(j.definition_ref_json) + length(coalesce(j.result_json, ''))), 0) "
                + condition)
    if rule == "idempotency-receipts":
        condition = "FROM idempotency_receipts WHERE created_at < :before"
        return ("SELECT rowid " + condition + " ORDER BY rowid",
                "SELECT count(*), min(created_at), max(created_at), coalesce(sum(length(result_json)), 0) " + condition)
    condition = ("FROM apply_receipts a WHERE a.state = 'applied' AND a.signoff_id IS NULL AND a.updated_at < :before "
                 "AND NOT EXISTS (SELECT 1 FROM resource_claims c WHERE c.apply_id = a.id)"
                 + _unreferenced(connection, "apply_receipts", "a"))
    return ("SELECT a.id " + condition + " ORDER BY a.updated_at, a.id",
            "SELECT count(*), min(a.updated_at), max(a.updated_at), coalesce(sum(length(a.change_set) "
            "+ length(a.progress_json) + length(coalesce(a.result_json, ''))), 0) " + condition)


_KEYS = {"routine-runs": ("run", "job"), "idempotency-receipts": ("row",), "apply-receipts": ("receipt",)}
_DELETES = {
    "routine-runs": (
        ("DELETE FROM routine_runs WHERE id = :run AND state = 'succeeded' AND finished_at < :before "
         "AND json_extract(result_json, '$.job_id') = :job"),
        ("DELETE FROM jobs WHERE id = :job AND state = 'succeeded' AND updated_at < :before AND dedupe_key = id "
         "AND json_extract(definition_ref_json, '$.routine_run_id') = :run")),
    "idempotency-receipts": ("DELETE FROM idempotency_receipts WHERE rowid = :row AND created_at < :before",),
    "apply-receipts": (
        ("DELETE FROM apply_receipts WHERE id = :receipt AND state = 'applied' AND signoff_id IS NULL "
         "AND updated_at < :before AND NOT EXISTS (SELECT 1 FROM resource_claims c WHERE c.apply_id = apply_receipts.id)"),),
}


def inspect(connection, *, now: datetime, days: int = DEFAULT_DAYS, rules=RULES) -> dict:
    """What each rule would remove. Reads only."""
    before = cutoff(now, days)
    report = {"days": days, "before": before, "rules": {}}
    for rule in rules:
        _require(connection, rule)
        rows, oldest, newest, size = connection.execute(_selection(connection, rule)[1], {"before": before}).fetchone()
        report["rules"][rule] = {"rows": rows, "oldest": oldest, "newest": newest, "stored_bytes": size}
    return report


def apply(connection, *, now: datetime, days: int = DEFAULT_DAYS, rules, batch: int = BATCH) -> dict:
    """Remove what the named rules select. The caller owns the store exclusively and gave an autocommit connection."""
    if not rules:
        raise DomainError("validation_error", "Name the retention rules to apply")
    if type(batch) is not int or not 1 <= batch <= 10000:
        raise DomainError("validation_error", "A retention batch holds 1 to 10000 records")
    before = cutoff(now, days)
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1 or connection.in_transaction:
        raise DomainError("storage_configuration", "Retention needs foreign keys enforced and no open transaction")
    report = {"days": days, "before": before, "rules": {}}
    for rule in dict.fromkeys(rules):
        _require(connection, rule)
        keys = [{**dict(zip(_KEYS[rule], row)), "before": before}
                for row in connection.execute(_selection(connection, rule)[0], {"before": before})]
        for start in range(0, len(keys), batch):
            group = keys[start:start + batch]
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in _DELETES[rule]:
                    removed = sum(connection.execute(statement, key).rowcount for key in group)
                    if removed != len(group):
                        raise DomainError("revision_conflict", f"The records of {rule} changed while they were removed")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        report["rules"][rule] = {"removed": len(keys)}
    # Foreign keys were enforced for every delete; this only confirms that no row still points at a removed record.
    parents = {table for rule in report["rules"] for table in _TABLES[rule]} - {"resource_claims"}
    children = {row[0] for table in parents for row in connection.execute(
        "SELECT m.name FROM sqlite_master m, pragma_foreign_key_list(m.name) f WHERE m.type = 'table' AND f.\"table\" = ?",
        (table,))}
    dangling = sum(1 for child in sorted(children) for row in connection.execute(f'PRAGMA foreign_key_check("{child}")')
                   if row[2] in parents)
    if dangling:
        raise DomainError("invalid_database", "A reference to a removed record remains", details={"count": dangling})
    report["free_pages"] = connection.execute("PRAGMA freelist_count").fetchone()[0]
    report["page_size"] = connection.execute("PRAGMA page_size").fetchone()[0]
    return report


def vacuum(connection) -> dict:
    """Rewrite the database file without its free pages. Needs free disk space of about the file's size."""
    before = connection.execute("PRAGMA page_count").fetchone()[0]
    try:
        connection.execute("VACUUM")
    except sqlite3.Error as error:
        raise DomainError("storage_failed", f"The database could not be compacted: {error}") from error
    return {"pages_before": before, "pages_after": connection.execute("PRAGMA page_count").fetchone()[0]}
