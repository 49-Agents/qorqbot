"""Offline, non-mutating export of the retired per-Project mail records of a copied legacy database.

    python -m qorqut_fastmail.export SOURCE.sqlite3 TARGET_DIRECTORY [--no-archive]

``SOURCE`` must be a *copy* of a legacy instance database taken while the instance was stopped (or drained and checkpointed).
It is opened read-only and immutable, is never written, and is hashed before and after reading: an export of a file that changed
underneath it is refused. A database with a pending write-ahead log or rollback journal is refused too, because reading it
without those files would silently miss records, and reading it with them would write next to the source.

``TARGET_DIRECTORY`` must not exist. The export is built in a sibling ``.partial`` directory and moved into place only after it
was verified, so a refusal or failure leaves nothing behind that could be mistaken for a finished export::

    manifest.json          counts, column lists, sha256 of every file, sequences, foreign-key findings, source hash
    tables/<table>.jsonl   every row of every legacy mail table, one JSON object per line, in storage order
    related/<name>.jsonl   rows of other tables that point at mail records (Signoffs, owner conversations, jobs)
    archive.sqlite3        the mail tables as a standalone, queryable database (unless --no-archive)

Values keep their SQLite type: integers and floats are JSON numbers, text is a JSON string, NULL is null and a BLOB is
``{"$blob": "<base64>"}``. Every record and identifier is copied verbatim; nothing is dropped, renamed, merged or rewritten. The
archive is verified by reading it back and comparing the canonical row lines with the exported files. References between the
mail tables are kept and checked (``PRAGMA foreign_key_check``); references to the core ``businesses``/``principals``/
``signoffs`` tables stay as plain identifiers because those tables are not part of the archive. Findings in the source itself
(rows that already violated a reference) are reported in the manifest and in the exit status, never repaired.

A database holding none of the legacy mail tables is refused rather than reported as a successful, empty export."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import legacy

FORMAT = "qorqut-fastmail-legacy-mail/1"
CHUNK = 1 << 20
# Rows of other core tables that point at legacy mail records, selected by plain read-only SELECTs with fixed clauses. A change
# set is parsed only when it is valid JSON (SQLite does not guarantee the evaluation order of boolean operands).
SIGNOFF_TOOL = ("CASE WHEN json_valid(CAST(change_set AS TEXT)) THEN json_extract(CAST(change_set AS TEXT), '$.arguments.tool') END"
                " = 'email.send'")
CONVERSATION_OWNED = "conversation_id IN (SELECT id FROM owner_conversations WHERE source_type = 'email')"
RELATED = ("signoffs", "owner_conversations", "owner_conversation_messages", "owner_conversation_reads", "jobs")


def related_selector(name: str, tables: set[str]) -> tuple[str, str] | None:
    """``(table, WHERE clause)`` of the related rows of ``name``, or None when the source has no such table to select from."""
    if name == "signoffs":
        if "signoffs" not in tables:
            return None
        linked = []
        if "email_decisions" in tables:
            linked += ["SELECT signoff_id FROM email_decisions", "SELECT replaces_id FROM email_decisions WHERE replaces_id IS NOT NULL"]
        if "email_conversations" in tables:
            linked.append("SELECT pending_signoff_id FROM email_conversations WHERE pending_signoff_id IS NOT NULL")
        return "signoffs", " OR ".join([f"id IN ({' UNION '.join(linked)})"] * bool(linked) + [SIGNOFF_TOOL])
    if name == "owner_conversations":
        return ("owner_conversations", "source_type = 'email'") if name in tables else None
    if name in {"owner_conversation_messages", "owner_conversation_reads"}:
        return (name, CONVERSATION_OWNED) if name in tables and "owner_conversations" in tables else None
    return ("jobs", "kind LIKE 'email.%'") if name == "jobs" and "jobs" in tables else None


class ExportError(Exception):
    """A refusal or verification failure. ``code`` is stable; nothing is left behind at the target."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# -- canonical encoding -------------------------------------------------------------------------------------------------
def encode(value):
    """One SQLite value as its typed JSON form. Booleans cannot occur (SQLite stores integers)."""
    if isinstance(value, bytes):
        return {"$blob": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return {"$float": repr(value)}
    return value


def decode(value):
    if isinstance(value, dict):
        if set(value) == {"$blob"}:
            return base64.b64decode(value["$blob"], validate=True)
        if set(value) == {"$float"}:
            return float(value["$float"])
    return value


def line(columns, row) -> bytes:
    """The canonical JSONL line of one row: column order of the source, compact separators, UTF-8, newline-terminated."""
    document = {name: encode(item) for name, item in zip(columns, row)}
    return (json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -- source ---------------------------------------------------------------------------------------------------------------
def open_source(path: Path) -> sqlite3.Connection:
    """Open a database file strictly read-only and immutable (no lock, no journal, no ``-shm`` created beside it)."""
    if not path.is_file():
        raise ExportError("source_missing", "The source database file does not exist")
    for suffix in ("-wal", "-journal"):
        sibling = path.with_name(path.name + suffix)
        if sibling.exists() and sibling.stat().st_size:
            raise ExportError("source_not_quiescent", f"The source has a pending {suffix[1:]} file; export a checkpointed copy of a "
                                                      "stopped instance, never a live database")
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def tables_of(connection) -> set[str]:
    return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def columns_of(connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def archive_columns() -> dict[str, list[str]]:
    """The column lists of the archive definitions, read from a scratch in-memory database."""
    scratch = sqlite3.connect(":memory:")
    try:
        for statement in legacy.ARCHIVE_DDL:
            scratch.execute(statement)
        return {table: columns_of(scratch, table) for table in legacy.TABLES}
    finally:
        scratch.close()


def sequences_of(connection, present) -> dict[str, int]:
    if "sqlite_sequence" not in tables_of(connection):
        return {}
    return {name: seq for name, seq in connection.execute("SELECT name, seq FROM sqlite_sequence") if name in present}


# -- export ---------------------------------------------------------------------------------------------------------------
def write_rows(connection, table: str, columns, where: str | None, path: Path) -> dict:
    """Stream the selected rows of one table to ``path`` and describe the file. Rows are read in storage (rowid) order."""
    names = ", ".join(f'"{name}"' for name in columns)
    query = f'SELECT {names} FROM "{table}"' + (f" WHERE {where}" if where else "") + " ORDER BY rowid"
    digest, count = hashlib.sha256(), 0
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "wb") as stream:
            for row in connection.execute(query):
                data = line(columns, row)
                stream.write(data)
                digest.update(data)
                count += 1
    except sqlite3.Error as error:
        raise ExportError("source_unreadable", f"Table {table} could not be read: {error}") from None
    except (ValueError, UnicodeError) as error:
        raise ExportError("source_unreadable", f"Table {table} holds a value that cannot be exported verbatim: {error}") from None
    return {"rows": count, "columns": list(columns), "sha256": digest.hexdigest()}


def build_archive(connection, work: Path, present, expected: dict) -> dict:
    """Create the standalone archive, copy the rows verbatim and verify them against the exported files."""
    path = work / "archive.sqlite3"
    archive = sqlite3.connect(path)
    try:
        archive.execute("PRAGMA foreign_keys = OFF")
        for statement in legacy.ARCHIVE_DDL:
            archive.execute(statement)
        for table in legacy.TABLES:
            if table not in present:
                continue
            columns = expected[table]["columns"]
            names = ", ".join(f'"{name}"' for name in columns)
            marks = ", ".join("?" * len(columns))
            archive.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({marks})',
                                connection.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid'))
        # Explicit sequence numbers keep AUTOINCREMENT counters too, including gaps left by deleted rows.
        sequences = sequences_of(connection, present)
        for table, seq in sequences.items():
            archive.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
            archive.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, seq))
        archive.commit()
        violations = [{"table": table, "rowid": rowid, "references": parent, "constraint": index}
                      for table, rowid, parent, index in archive.execute("PRAGMA foreign_key_check")]
        # Fidelity: rebuild the canonical lines from the archive and compare with what was written from the source.
        for table in legacy.TABLES:
            if table not in present:
                continue
            columns = expected[table]["columns"]
            names = ", ".join(f'"{name}"' for name in columns)
            digest, count = hashlib.sha256(), 0
            for row in archive.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid'):
                digest.update(line(columns, row))
                count += 1
            if count != expected[table]["rows"] or digest.hexdigest() != expected[table]["sha256"]:
                raise ExportError("archive_mismatch", f"The archive copy of {table} differs from the exported records")
        if sequences_of(archive, present) != sequences:
            raise ExportError("archive_mismatch", "The archive does not carry the source's AUTOINCREMENT counters")
        if archive.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ExportError("archive_mismatch", "The archive failed its integrity check")
    except sqlite3.IntegrityError as error:
        raise ExportError("archive_rejected_record", f"The archive's constraints reject a source record ({error}); the JSONL "
                                                     "files are the faithful copy, rerun with --no-archive") from None
    finally:
        archive.close()
    return {"path": "archive.sqlite3", "sha256": sha256_file(path), "foreign_key_violations": violations}


def export_legacy_mail(source, target, *, archive: bool = True, now: datetime | None = None) -> dict:
    """Export the legacy mail records of the copied database ``source`` into the new directory ``target``; return the manifest."""
    source, target = Path(source), Path(target)
    if target.exists() or target.is_symlink():
        raise ExportError("target_exists", "The target already exists; an export never overwrites or merges")
    if not target.parent.is_dir():
        raise ExportError("target_missing_parent", "The parent directory of the target does not exist")
    work = target.with_name(target.name + ".partial")
    if work.exists() or work.is_symlink():
        raise ExportError("target_exists", "A partial export is left at the target; remove it deliberately and retry")
    before = sha256_file(source) if source.is_file() else None
    connection = open_source(source)
    work.mkdir()
    try:
        present_tables = tables_of(connection)
        present = [table for table in legacy.TABLES if table in present_tables]
        if not present:
            raise ExportError("not_a_legacy_mail_database",
                              "The source holds none of the legacy mail tables; there is nothing to export")
        expected = archive_columns()
        for table in present:
            actual = columns_of(connection, table)
            if set(actual) != set(expected[table]):
                raise ExportError("schema_mismatch", f"Table {table} has columns {sorted(actual)}, not the legacy shape "
                                                     f"{sorted(expected[table])}; refusing to guess a mapping")
        tables = {}
        for table in legacy.TABLES:
            if table in present:
                tables[table] = {"present": True, **write_rows(connection, table, columns_of(connection, table), None,
                                                              work / "tables" / f"{table}.jsonl")}
                tables[table]["file"] = f"tables/{table}.jsonl"
            else:
                tables[table] = {"present": False}
        related = {}
        for name in RELATED:
            selector = related_selector(name, present_tables)
            if selector is None:
                related[name] = {"present": False}
                continue
            table, where = selector
            related[name] = {"present": True, **write_rows(connection, table, columns_of(connection, table), where,
                                                          work / "related" / f"{name}.jsonl"),
                             "file": f"related/{name}.jsonl"}
        manifest = {
            "format": FORMAT,
            "created_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": {"name": source.name, "bytes": source.stat().st_size, "sha256": before},
            "tables": tables,
            "related": related,
            "sequences": sequences_of(connection, present),
            "records": sum(entry.get("rows", 0) for entry in tables.values()),
            "related_records": sum(entry.get("rows", 0) for entry in related.values()),
            "not_exported": ["triggers", "views", "settings and configuration files"],
        }
        manifest["empty"] = manifest["records"] == 0
        if archive:
            manifest["archive"] = build_archive(connection, work, present, tables)
            manifest["foreign_key_violations"] = manifest["archive"].pop("foreign_key_violations")
        else:
            manifest["archive"] = None
            manifest["foreign_key_violations"] = None
        connection.close()
        if sha256_file(source) != before:
            raise ExportError("source_changed", "The source file changed while it was being exported; use a stopped copy")
        manifest["verified"] = True
        manifest["status"] = "needs_review" if manifest["foreign_key_violations"] else "complete"
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        work.rename(target)
        return manifest
    except BaseException:
        try:
            connection.close()
        except sqlite3.Error:
            pass
        shutil.rmtree(work, ignore_errors=True)
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qorqut_fastmail.export", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="a copy of a legacy instance database (never a live one)")
    parser.add_argument("target", help="a new directory for the export")
    parser.add_argument("--no-archive", action="store_true", help="write only the JSONL files and the manifest")
    arguments = parser.parse_args(argv)
    try:
        manifest = export_legacy_mail(arguments.source, arguments.target, archive=not arguments.no_archive)
    except ExportError as error:
        print(json.dumps({"status": "refused", "error": error.code, "message": str(error)}), file=sys.stderr)
        return 2
    summary = {"status": manifest["status"], "records": manifest["records"], "related_records": manifest["related_records"],
               "empty": manifest["empty"], "foreign_key_violations": manifest["foreign_key_violations"],
               "manifest": str(Path(arguments.target) / "manifest.json")}
    print(json.dumps(summary, indent=2))
    return 0 if manifest["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
