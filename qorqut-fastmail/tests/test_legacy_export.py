"""Offline legacy mail export: record and identifier fidelity, read-only handling of the source, and honest refusals.

The source databases are generated here from the legacy definitions with invented identifiers and placeholder text; no real
mail, address or account is involved, and no live instance database is opened."""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from qorqut_fastmail import export, legacy
from qorqut_fastmail.extension import create

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
# The generic core tables the legacy mail tables referenced, reduced to the columns the export selects on.
CORE = (
    "CREATE TABLE businesses (id TEXT PRIMARY KEY)",
    "CREATE TABLE principals (id TEXT PRIMARY KEY)",
    "CREATE TABLE signoffs (id TEXT PRIMARY KEY, category TEXT, state TEXT, change_set BLOB NOT NULL)",
    "CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, arguments_json TEXT)",
    "CREATE TABLE owner_conversations (id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, source_type TEXT NOT NULL,"
    " source_id TEXT NOT NULL)",
    "CREATE TABLE owner_conversation_messages (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,"
    " conversation_id TEXT NOT NULL, body TEXT NOT NULL)",
    "CREATE TABLE owner_conversation_reads (conversation_id TEXT NOT NULL, principal_id TEXT NOT NULL,"
    " last_read_sequence INTEGER NOT NULL)",
)
STAMP = "2026-01-02T03:04:05Z"


def change_set(tool):
    return json.dumps({"arguments": {"tool": tool, "payload": {"note": "synthetic"}}}).encode()


def build_source(path: Path, *, orphan=False) -> Path:
    """A legacy-shaped database with synthetic rows in every mail table and in the related generic tables."""
    db = sqlite3.connect(path)
    for statement in CORE:
        db.execute(statement)
    for table in legacy.TABLES:
        for statement in legacy.LEGACY_DDL[table]:
            db.execute(statement)
    db.executemany("INSERT INTO businesses VALUES (?)", [("biz-1",), ("biz-2",)])
    db.executemany("INSERT INTO principals VALUES (?)", [("owner-1",), ("agent-1",)])
    db.executemany("INSERT INTO email_mailboxes (id, business_id, address, aliases_json, cursor, sync_json, revision, created_at,"
                   " updated_at, provider_folder_id, purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        ("mbx-1", "biz-1", "alpha@example.test", '["alias-a@example.test"]', 7, '{"state":"ok"}', 3, STAMP, STAMP, "folder-1",
         "business_mail"),
        ("mbx-2", None, "owner@example.test", "[]", 0, "{}", 1, STAMP, STAMP, None, "owner_mail")])
    db.executemany("INSERT INTO email_conversations (id, business_id, mailbox, provider_id, subject, last_message_at, revision,"
                   " created_at, updated_at, archived, tags_json, pending_signoff_id, thread_revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("conv-1", "biz-1", "mbx-1", "thread-a", "Example subject one", STAMP, 2, STAMP, STAMP, 0, '["x"]', "so-pending", 4),
        ("conv-2", None, "mbx-2", "thread-b", "Example subject two", STAMP, 1, STAMP, STAMP, 1, "[]", None, 1)])
    db.executemany("INSERT INTO email_messages (id, business_id, conversation_id, mailbox, provider_id, message_json, deleted,"
                   " revision, created_at, updated_at, provider_folder_id, origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("msg-1", "biz-1", "conv-1", "mbx-1", "m-a1", '{"subject":"Example subject one","snippet":"placeholder"}', 0, 1, STAMP,
         STAMP, "folder-1", "sync"),
        ("msg-2", "biz-1", "conv-1", "mbx-1", "m-a2", '{"subject":"Re: Example subject one"}', 1, 2, STAMP, STAMP, None, "sent"),
        ("msg-3", None, "conv-2", "mbx-2", "m-b1", '{"subject":"Example subject two"}', 0, 1, STAMP, STAMP, None, "draft")])
    db.execute("INSERT INTO email_routing_history VALUES ('route-1','m-a1','folder-0','folder-1','biz-1','agent-1',?,?)", (STAMP, STAMP))
    db.executemany("INSERT INTO email_history_outbox (sequence, event_id, kind, content_json, created_at) VALUES (?,?,?,?,?)",
                   [(5, "evt-1", "message", '{"id":"msg-1"}', STAMP), (9, "evt-2", "message", '{"id":"msg-3"}', STAMP)])
    db.execute("DELETE FROM email_history_outbox WHERE sequence = 9")      # a gap: the AUTOINCREMENT counter stays at 9
    db.execute("INSERT INTO email_history_deliveries VALUES ('evt-1', ?)", (STAMP,))
    db.execute("INSERT INTO email_account_thread_controls VALUES ('acct-thread-1','[\"t\"]','high',2,?,'owner-1')", (STAMP,))
    db.execute("INSERT INTO email_account_comments (sequence, id, provider_thread_id, body, created_by, created_at)"
               " VALUES (3,'acm-1','acct-thread-1','placeholder comment','owner-1',?)", (STAMP,))
    db.execute("INSERT INTO email_owned_folders VALUES ('folder-1','biz-1','mbx-1','Example folder')")
    db.execute("INSERT INTO email_comments (sequence, id, business_id, conversation_id, body, summary, created_by, created_at)"
               " VALUES (2,'cmt-1','biz-1','conv-1','placeholder comment','short','agent-1',?)", (STAMP,))
    db.execute("INSERT INTO email_thread_controls VALUES ('biz-1','conv-1','[]','normal',1,?,'owner-1',?,'owner-1','placeholder','msg-1')",
               (STAMP, STAMP))
    db.execute("INSERT INTO email_decisions VALUES ('biz-1','conv-1','archive?','so-decision','{\"k\":1}',4,'hash-1','so-replaced',?)",
               (STAMP,))
    # Generic rows: three that point at mail records, three that must not be exported.
    db.executemany("INSERT INTO signoffs VALUES (?,?,?,?)", [
        ("so-pending", "platform_action", "pending", change_set("email.send")),
        ("so-decision", "input_request", "answered", b"{}"),
        ("so-replaced", "input_request", "cancelled", b"\x00\x01 not json \xff"),
        ("so-send", "platform_action", "applied", change_set("email.send")),
        ("so-other", "platform_action", "applied", change_set("calendar.event")),
        ("so-binary", "internal_change", "applied", b"\x80\x81\x82")])
    db.executemany("INSERT INTO jobs VALUES (?,?,?,?)", [("job-1", "email.sync", "succeeded", "{}"), ("job-2", "email.account_mutation",
                   "failed", "{}"), ("job-3", "calendar.sync", "succeeded", "{}")])
    db.executemany("INSERT INTO owner_conversations VALUES (?,?,?,?)", [("oc-1", "email:conv-1", "email", "conv-1"),
                   ("oc-2", "task:t1", "task", "t1")])
    db.executemany("INSERT INTO owner_conversation_messages (sequence, id, conversation_id, body) VALUES (?,?,?,?)",
                   [(1, "ocm-1", "oc-1", "placeholder"), (2, "ocm-2", "oc-2", "placeholder")])
    db.executemany("INSERT INTO owner_conversation_reads VALUES (?,?,?)", [("oc-1", "owner-1", 1), ("oc-2", "owner-1", 2)])
    if orphan:
        db.execute("INSERT INTO email_messages (id, business_id, conversation_id, mailbox, provider_id, message_json, created_at,"
                   " updated_at) VALUES ('msg-orphan','biz-1','conv-missing','mbx-1','m-orphan','{}',?,?)", (STAMP, STAMP))
    db.commit()
    db.execute("PRAGMA journal_mode = DELETE")
    db.close()
    return path


@pytest.fixture()
def source(tmp_path):
    return build_source(tmp_path / "copy.sqlite3")


def lines(path: Path):
    return [json.loads(item) for item in path.read_text(encoding="utf-8").splitlines()]


def rows(path: Path, table: str, *, order="rowid"):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cursor = db.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
        names = [column[0] for column in cursor.description]
        return names, cursor.fetchall()
    finally:
        db.close()


def test_every_record_and_identifier_is_copied_verbatim(source, tmp_path):
    target = tmp_path / "out"
    manifest = export.export_legacy_mail(source, target, now=NOW)
    assert manifest["status"] == "complete" and manifest["verified"] is True and manifest["empty"] is False
    assert manifest["created_at"] == "2026-10-25T12:00:00Z"
    for table in legacy.TABLES:
        names, expected = rows(source, table)
        entry = manifest["tables"][table]
        assert entry["present"] and entry["rows"] == len(expected) and entry["columns"] == names
        exported = lines(target / entry["file"])
        assert len(exported) == len(expected)
        for document, row in zip(exported, expected):
            assert list(document) == names
            assert [export.decode(document[name]) for name in names] == list(row)
        assert hashlib.sha256((target / entry["file"]).read_bytes()).hexdigest() == entry["sha256"]
    assert manifest["records"] == sum(entry["rows"] for entry in manifest["tables"].values()) == 16
    ids = {document["id"] for document in lines(target / "tables" / "email_messages.jsonl")}
    assert ids == {"msg-1", "msg-2", "msg-3"}
    assert {document["address"] for document in lines(target / "tables" / "email_mailboxes.jsonl")} \
        == {"alpha@example.test", "owner@example.test"}
    assert json.loads((target / "manifest.json").read_text()) == manifest


def test_archive_is_a_faithful_standalone_database(source, tmp_path):
    target = tmp_path / "out"
    manifest = export.export_legacy_mail(source, target, now=NOW)
    archive = target / manifest["archive"]["path"]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive"]["sha256"]
    for table in legacy.TABLES:
        names, original = rows(source, table)
        archived_names, archived = rows(archive, table)
        assert set(archived_names) == set(names)
        index = [archived_names.index(name) for name in names]
        assert [tuple(row[i] for i in index) for row in archived] == original
    # The AUTOINCREMENT counters survive, including the gap left by a deleted outbox row.
    assert manifest["sequences"]["email_history_outbox"] == 9
    db = sqlite3.connect(archive)
    assert dict(db.execute("SELECT name, seq FROM sqlite_sequence")) == manifest["sequences"]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert manifest["foreign_key_violations"] == []
    definitions = " ".join(sql for (sql,) in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    assert not re.search(r"REFERENCES\s+(businesses|principals|signoffs)\b", definitions)
    assert "REFERENCES email_conversations" in definitions or 'REFERENCES "email_conversations"' in definitions
    # Mail-table references are enforced in the archive: an orphan is rejected once foreign keys are on.
    db.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO email_messages (id, conversation_id, mailbox, provider_id, message_json, created_at, updated_at)"
                   " VALUES ('x','missing','mbx-1','p','{}','t','t')")
    db.close()


def test_related_rows_are_only_those_pointing_at_mail(source, tmp_path):
    target = tmp_path / "out"
    manifest = export.export_legacy_mail(source, target, now=NOW)
    ids = lambda name: {item["id"] for item in lines(target / manifest["related"][name]["file"])}
    # Linked from decisions / pending conversations / the retired email.send tool, but not the unrelated Signoffs.
    assert ids("signoffs") == {"so-pending", "so-decision", "so-replaced", "so-send"}
    assert ids("owner_conversations") == {"oc-1"}
    assert ids("owner_conversation_messages") == {"ocm-1"}
    assert [item["conversation_id"] for item in lines(target / "related" / "owner_conversation_reads.jsonl")] == ["oc-1"]
    assert ids("jobs") == {"job-1", "job-2"}
    blob = {item["id"]: item["change_set"] for item in lines(target / "related" / "signoffs.jsonl")}["so-replaced"]
    assert export.decode(blob) == b"\x00\x01 not json \xff"        # BLOBs round-trip byte for byte, even when not JSON
    assert manifest["related_records"] == 4 + 1 + 1 + 1 + 2


def test_source_is_not_modified(source, tmp_path):
    before = (source.read_bytes(), source.stat().st_mtime_ns, sorted(path.name for path in source.parent.iterdir()))
    export.export_legacy_mail(source, tmp_path / "out", now=NOW)
    export.export_legacy_mail(source, tmp_path / "out2", archive=False, now=NOW)
    assert (source.read_bytes(), source.stat().st_mtime_ns, sorted(path.name for path in source.parent.iterdir())) \
        == (before[0], before[1], sorted(before[2] + ["out", "out2"]))
    assert not list(source.parent.glob("copy.sqlite3-*"))


def test_export_without_archive_still_writes_everything_else(source, tmp_path):
    manifest = export.export_legacy_mail(source, tmp_path / "out", archive=False, now=NOW)
    assert manifest["archive"] is None and manifest["foreign_key_violations"] is None and manifest["status"] == "complete"
    assert not (tmp_path / "out" / "archive.sqlite3").exists()
    assert (tmp_path / "out" / "tables" / "email_decisions.jsonl").is_file()


def test_findings_in_the_source_are_reported_not_repaired(tmp_path):
    source = build_source(tmp_path / "copy.sqlite3", orphan=True)
    manifest = export.export_legacy_mail(source, tmp_path / "out", now=NOW)
    assert manifest["status"] == "needs_review"
    assert [(item["table"], item["references"]) for item in manifest["foreign_key_violations"]] \
        == [("email_messages", "email_conversations")]
    ids = {item["id"] for item in lines(tmp_path / "out" / "tables" / "email_messages.jsonl")}
    assert "msg-orphan" in ids                                         # copied, never silently dropped
    assert export.main([str(source), str(tmp_path / "again")]) == 1


def test_database_without_mail_tables_is_refused_not_an_empty_success(tmp_path):
    plain = tmp_path / "plain.sqlite3"
    db = sqlite3.connect(plain)
    db.execute("CREATE TABLE businesses (id TEXT PRIMARY KEY)")
    db.commit()
    db.close()
    with pytest.raises(export.ExportError) as caught:
        export.export_legacy_mail(plain, tmp_path / "out")
    assert caught.value.code == "not_a_legacy_mail_database"
    assert not (tmp_path / "out").exists() and not (tmp_path / "out.partial").exists()


def test_legacy_database_with_no_rows_is_reported_as_empty(tmp_path):
    path = tmp_path / "empty.sqlite3"
    db = sqlite3.connect(path)
    for table in legacy.TABLES:
        for statement in legacy.archive_statements(table):
            db.execute(statement)
    db.commit()
    db.close()
    manifest = export.export_legacy_mail(path, tmp_path / "out", now=NOW)
    assert manifest["empty"] is True and manifest["records"] == 0 and manifest["status"] == "complete"
    assert all(entry["present"] and entry["rows"] == 0 for entry in manifest["tables"].values())


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_pending_log_makes_the_source_non_quiescent(source, tmp_path, suffix):
    source.with_name(source.name + suffix).write_bytes(b"pending")
    with pytest.raises(export.ExportError) as caught:
        export.export_legacy_mail(source, tmp_path / "out")
    assert caught.value.code == "source_not_quiescent" and not (tmp_path / "out.partial").exists()


def test_refusals_leave_nothing_behind(source, tmp_path):
    target = tmp_path / "out"
    export.export_legacy_mail(source, target, now=NOW)
    with pytest.raises(export.ExportError) as caught:                  # never overwrite or merge
        export.export_legacy_mail(source, target)
    assert caught.value.code == "target_exists"
    with pytest.raises(export.ExportError) as caught:
        export.export_legacy_mail(tmp_path / "absent.sqlite3", tmp_path / "other")
    assert caught.value.code == "source_missing" and not (tmp_path / "other.partial").exists()
    with pytest.raises(export.ExportError) as caught:
        export.export_legacy_mail(source, tmp_path / "no-parent" / "out")
    assert caught.value.code == "target_missing_parent"


def test_unknown_column_shape_is_refused_rather_than_guessed(source, tmp_path):
    db = sqlite3.connect(source)
    db.execute("ALTER TABLE email_mailboxes ADD COLUMN surprise TEXT")
    db.commit()
    db.close()
    with pytest.raises(export.ExportError) as caught:
        export.export_legacy_mail(source, tmp_path / "out")
    assert caught.value.code == "schema_mismatch" and "surprise" in str(caught.value)
    assert not (tmp_path / "out").exists() and not (tmp_path / "out.partial").exists()


def test_archive_constraint_rejection_points_to_the_jsonl_copy(tmp_path):
    source = build_source(tmp_path / "copy.sqlite3")
    db = sqlite3.connect(source)
    db.execute("DROP INDEX IF EXISTS email_decisions_key")             # an older shape that allowed a duplicate decision key
    db.execute("INSERT INTO email_decisions VALUES ('biz-1','conv-1','archive?','so-send','{}',4,'hash-2',NULL,?)", (STAMP,))
    db.commit()
    db.close()
    with pytest.raises(export.ExportError) as caught:
        export.export_legacy_mail(source, tmp_path / "out")
    assert caught.value.code == "archive_rejected_record" and "--no-archive" in str(caught.value)
    assert not (tmp_path / "out").exists()
    manifest = export.export_legacy_mail(source, tmp_path / "records", archive=False, now=NOW)
    assert manifest["tables"]["email_decisions"]["rows"] == 2          # the JSONL copy is still faithful


def test_command_line_reports_a_summary_and_refusals(source, tmp_path, capsys):
    assert export.main([str(source), str(tmp_path / "out")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "complete" and summary["records"] == 16 and summary["related_records"] == 9
    assert export.main([str(source), str(tmp_path / "out")]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "target_exists"


def test_encoding_round_trips_every_sqlite_type():
    for value in (None, 0, -7, 2 ** 62, 1.5, "text", "ünï", "", b"", b"\x00\xff"):
        document = json.loads(export.line(["v"], (value,)))
        assert export.decode(document["v"]) == value
    assert export.decode(json.loads(export.line(["v"], (float("inf"),)))["v"]) == float("inf")


def test_legacy_records_are_not_part_of_a_live_install():
    extension = create({"engine_url": "http://127.0.0.1:1", "admin_token_file": "/nonexistent/token"})
    assert tuple(extension.storage()) == () and tuple(extension.history()) == ()
    archive_sql = " ".join(legacy.ARCHIVE_DDL).lower()
    assert " trigger " not in archive_sql and " view " not in archive_sql


def test_public_tests_and_docs_carry_only_placeholder_addresses():
    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
    allowed = {"example.com", "example.org", "example.net", "127.0.0.1"}
    offenders = {}
    for path in [*root.glob("tests/*.py"), *root.glob("qorqut_fastmail/*.py"), *root.glob("docs/*.md"), root / "README.md"]:
        if not path.is_file():
            continue
        for domain in (item.lower() for item in pattern.findall(path.read_text(encoding="utf-8"))):
            if domain not in allowed and domain.rsplit(".", 1)[-1] not in {"test", "example", "invalid"}:
                offenders.setdefault(path.name, set()).add(domain)
    assert offenders == {}
