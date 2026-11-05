"""The offline cleanup of the retired per-worker owner conversations (``bazarlyk.maintenance.private_peer_cleanup``).

SYNTHETIC ONLY. ``private_peer_support.plant`` writes the stored shapes earlier releases left into a temporary host database
(real stock schema, triggers and engine tables); each check works on a throwaway file copy, reached by plain connections the
way a reviewed maintenance run would reach it. Nothing here touches a live database, a model, a native session or a network, and
nothing prints a message body: the planted texts carry a marker that no inspection may contain.

What is proven: the inspection is read-only and counts/identifiers only; apply needs an explicit target, the resolved Ordubashy,
maintenance + quiescence authorization and the exact reviewed digest; a target that changed since the review, a dependent outside
the set, an unexpected trigger or an anomalous row refuses the WHOLE apply and changes nothing; any failure rolls schema (the
dropped immutability triggers) and contents back; a repeat is a no-op; the schema fingerprint is unchanged; and the surviving
data - the Ordubashy channel, other forwarding kinds, ordinary messages, the engine's nudge records - is byte-identical.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from qorqut_operations.inbox_nudges import state as nudges

from bazarlyk.inbox.owner_forwarding import forwarded_id
from bazarlyk.maintenance import private_peer_cleanup as cleanup
from bazarlyk.maintenance.private_peer_cleanup import CleanupError, MaintenanceAuthorization
from private_peer_support import MARKER, NOW, copy_store, plant, retired_request_hash


@pytest.fixture(scope="module")
def planted(tmp_path_factory):
    legacy = plant(tmp_path_factory.mktemp("planted"))
    try:
        yield legacy
    finally:
        try:
            legacy.instance.runtime.release_instance()
        finally:
            legacy.database.close()


@pytest.fixture
def store(planted, tmp_path):
    return copy_store(planted, tmp_path / "target.sqlite3")


def retired(store):
    """The identifiers the planted store retires, named independently of the cleanup's own derivation."""
    ids = store.ids
    letters = ("a", "b")
    messages = [ids[f"{who}_{letter}{n}"] for letter in letters for who, n in (("owner", 1), ("worker", 1), ("owner", 2))]
    messages.append(ids["owner_b3"])
    conversation_events = [ids[f"owner_{letter}{n}:event"] for letter in letters for n in (1, 2)] + [ids["owner_b3:event"]]
    notices = [ids[name] for name in ("notice_a1", "notice_a2", "notice_b1", "notice_b2", "notice_b3")]
    notice_events = [ids[name + ":event"] for name in ("notice_a1", "notice_a2", "notice_b1", "notice_b2", "notice_b3")]
    receipts = [ids[name] for name in ("send_owner_a", "send_worker_a", "mark_owner_a", "send_owner_b", "send_worker_b",
                                       "mark_owner_b", "mark_peer_only")]
    return {"conversations": [ids["conversation_a"], ids["conversation_b"]], "messages": messages,
            "events": sorted(conversation_events + notice_events), "notices": notices, "receipts": receipts}


def without_retired(store, before):
    """What ``before`` (a ``contents()`` snapshot) must become: the same rows minus exactly the retired ones."""
    gone = retired(store)
    events, receipts = set(gone["events"]), {tuple(item) for item in gone["receipts"]}
    keep = {
        "owner_conversations": lambda row: row[0] not in gone["conversations"],
        "owner_conversation_messages": lambda row: row[1] not in gone["messages"],
        "owner_conversation_reads": lambda row: row[0] not in gone["conversations"],
        "inbox_events": lambda row: row[0] not in events,
        "inbox_deliveries": lambda row: row[1] not in events,
        "agent_messages": lambda row: row[0] not in gone["notices"],
        "idempotency_receipts": lambda row: tuple(row[:4]) not in receipts,
    }
    return {table: [row for row in rows if keep.get(table, lambda _: True)(row)] for table, rows in before.items()}


MISSING = object()


def inspect(store, ordubashy=MISSING):
    return cleanup.inspect_target(store.path, ordubashy_id=store.coordinator if ordubashy is MISSING else ordubashy)


def authorized(report, **changes):
    values = {"maintenance_authorized": True, "writers_quiesced": True, "reviewed_digest": report["digest"],
              "reference": "synthetic-approval-1", **changes}
    return MaintenanceAuthorization(**values)


def apply(store, report, authorization=MISSING, ordubashy=MISSING, target=MISSING):
    return cleanup.apply_cleanup(store.path if target is MISSING else target,
                                 ordubashy_id=store.coordinator if ordubashy is MISSING else ordubashy, reviewed=report,
                                 authorization=authorized(report) if authorization is MISSING else authorization)


def refusal(store, report, code, **kwargs):
    with pytest.raises(CleanupError) as error:
        apply(store, report, **kwargs)
    assert error.value.code == code, (error.value.code, str(error.value))
    return error.value


def unchanged(store, action):
    """Run ``action`` and assert that contents, schema (every trigger's SQL) and the id sequences are exactly as before."""
    before = (store.contents(), store.schema(), store.sequences())
    result = action()
    assert (store.contents(), store.schema(), store.sequences()) == before
    return result


def insert_reply(store, to, key="reply"):
    """An authored reply to a forwarded notice: a hard dependent outside the deletion set."""
    event = store.execute("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
                          "VALUES(?,?,?,?,?,?,?)", None, "agent_messages", key, store.coordinator, "direct_message", "Reply", NOW)
    store.execute("INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,"
                  "summary,current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", key, event, None,
                  store.coordinator, store.owner, to, to, f"{MARKER} authored reply", NOW, None, store.coordinator, store.owner)
    return key


# ----- inspection ---------------------------------------------------------------------------------------------------------

def test_inspection_reports_counts_and_identifiers_only_and_changes_nothing(store):
    ids, gone = store.ids, retired(store)
    before = (store.contents(), store.schema(), store.sequences())
    report = inspect(store)
    assert (store.contents(), store.schema(), store.sequences()) == before
    assert report["blocked"] is False and report["plan"]["blockers"] == []
    assert report["counts"] == {"owner_conversations": 2, "owner_conversation_messages": 7, "owner_conversation_reads": 2,
                                "inbox_events": 10, "inbox_deliveries": 10, "agent_messages": 5, "idempotency_receipts": 7}
    selection = report["plan"]["selection"]
    assert sorted(item[0] for item in selection["owner_conversations"]) == sorted(gone["conversations"])
    assert sorted(item[0] for item in selection["inbox_events"]) == gone["events"]
    assert sorted(item[0] for item in selection["agent_messages"]) == sorted(gone["notices"])
    assert sorted(tuple(item) for item in selection["idempotency_receipts"]) == sorted(tuple(item) for item in gone["receipts"])
    # The Ordubashy channel (null peer) and the row stored with Ordubashy's own identity are only counted as protected.
    assert report["protected"] == {"null_peer": 1, "explicit_ordubashy": 1}
    named = {item for rows in selection.values() for row in rows for item in row}
    protected = {ids["conversation_c"], ids["conversation_d"], ids["owner_c1"], ids["owner_d1"], ids["ordubashy_c1"],
                 ids["chat_notice"], ids["chat_notice:event"], "ordinary", ids["ordinary_event"], ids["owner_c1:event"],
                 ids["send_owner_c"][3], ids["send_owner_d"][3], ids["unrelated"][3]}
    assert not named & protected
    # Nothing readable leaves an inspection: not a body, a summary or a header.
    text = json.dumps(report)
    assert MARKER not in text and "Forwarded notice" not in text and "Owner message" not in text
    # Retained references are counts: two attempts name a retired event; the content digests are per table, not per row.
    assert report["retained_references"]["inbox_nudge_attempts_naming_a_deleted_event"] == 2
    assert set(report["plan"]["content"]) == set(report["counts"]) and all(len(v) == 64 for v in report["plan"]["content"].values())
    # The same target gives the same digest, by path or through a caller's connection; a different Ordubashy is not accepted.
    assert inspect(store)["digest"] == report["digest"]
    connection = sqlite3.connect(store.path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        assert cleanup.inspect_target(connection, ordubashy_id=store.coordinator)["digest"] == report["digest"]
        assert not connection.in_transaction and connection.isolation_level == ""
    finally:
        connection.close()


@pytest.mark.parametrize("who", ["worker", "owner", "unknown", ""])
def test_the_resolved_ordubashy_identity_is_required_and_cross_checked(store, who):
    wrong = {"worker": store.workers["a"], "owner": store.owner, "unknown": new_id(), "": ""}[who]
    with pytest.raises(CleanupError) as error:
        inspect(store, wrong)
    assert error.value.code == "invalid_identity"


@pytest.mark.parametrize("target", ["relative.sqlite3", "missing", "directory", None, 7])
def test_an_explicit_existing_absolute_target_is_required(store, tmp_path, target):
    chosen = {"relative.sqlite3": "relative.sqlite3", "missing": tmp_path / "missing.sqlite3", "directory": tmp_path, None: None,
              7: 7}[target]
    with pytest.raises(CleanupError) as error:
        cleanup.inspect_target(chosen, ordubashy_id=store.coordinator)
    assert error.value.code == "invalid_target"
    report = inspect(store)
    with pytest.raises(CleanupError) as error:
        cleanup.apply_cleanup(chosen, ordubashy_id=store.coordinator, reviewed=report, authorization=authorized(report))
    assert error.value.code == "invalid_target"


# ----- authorization ------------------------------------------------------------------------------------------------------

def test_apply_without_every_authorization_and_the_exact_review_changes_nothing(store):
    report = inspect(store)
    tampered_plan = json.loads(json.dumps(report["plan"]))
    tampered_plan["selection"]["owner_conversations"].pop()
    worker = store.workers["a"]
    forged_plan = {**report["plan"], "ordubashy_id": worker}
    cases = [
        ("not_authorized", report, {"authorization": None}),
        ("not_authorized", report, {"authorization": authorized(report, maintenance_authorized=False)}),
        ("not_authorized", report, {"authorization": authorized(report, writers_quiesced=False)}),
        ("not_authorized", report, {"authorization": authorized(report, maintenance_authorized="yes")}),
        ("not_authorized", report, {"authorization": authorized(report, reference="  ")}),
        ("not_authorized", report, {"authorization": authorized(report, reviewed_digest="f" * 64)}),
        ("not_authorized", report, {"authorization": authorized(report, reviewed_digest=None)}),
        ("invalid_plan", {**report, "digest": "0" * 64}, {"authorization": authorized(report)}),
        ("invalid_plan", {**report, "plan": tampered_plan}, {"authorization": authorized(report)}),
        ("invalid_plan", None, {"authorization": authorized(report)}),
        ("invalid_plan", report, {"ordubashy": worker}),
        # A forged review that is internally consistent still meets the stored configured Ordubashy.
        ("invalid_identity", {"plan": forged_plan, "digest": cleanup.sha(forged_plan)}, {"ordubashy": worker}),
    ]
    for code, reviewed, options in cases:
        if "authorization" not in options and reviewed is not None:
            options = {**options, "authorization": authorized(reviewed)}
        unchanged(store, lambda: refusal(store, reviewed, code, **options))
    # Nothing was left locked or half-open by the refusals: the reviewed apply still works.
    assert apply(store, report)["applied"] is True


# ----- the exact removal --------------------------------------------------------------------------------------------------

def test_apply_removes_exactly_the_reviewed_set_keeps_everything_else_and_the_schema(store):
    ids = store.ids
    before, schema, sequences = store.contents(), store.schema(), store.sequences()
    report = inspect(store)
    result = apply(store, report)
    assert result["applied"] is True and result["noop"] is False and result["digest"] == report["digest"]
    assert result["removed"] == report["counts"]
    assert result["schema_fingerprint"] == report["plan"]["schema_fingerprint"] == cleanup.schema_fingerprint(sqlite3.connect(store.path))
    # Contents: exactly the retired rows are gone from the seven tables, every other table and row is identical.
    assert store.contents() == without_retired(store, before)
    # Schema: every table, index and trigger - the immutability triggers included - is back with its original SQL.
    assert store.schema() == schema
    assert store.query("PRAGMA foreign_key_check") == []
    # The engine's nudge records and the id sequences are unchanged: a number once used is never allocated again.
    assert store.sequences() == sequences
    for table in ("inbox_nudge_needs", "inbox_nudge_attempts", "inbox_nudge_evidence"):
        assert store.contents((table,)) == {table: before[table]}
    newest = sequences["inbox_events"]
    assert newest == max(retired(store)["events"])         # the newest event ever made was a retired one
    created = store.execute("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
                            "VALUES(?,?,?,?,?,?,?)", None, "agent_messages", "later", store.owner, "direct_message", "Later", NOW)
    assert created == newest + 1 and created not in retired(store)["events"]
    # Survivors, by name: the Ordubashy channel, the explicit-UUID row, other forwarding kinds, ordinary messages and receipts.
    for name in ("conversation_c", "conversation_d"):
        assert store.query("SELECT 1 FROM owner_conversations WHERE id=?", ids[name])
    for name in ("owner_c1", "ordubashy_c1", "owner_d1"):
        assert store.query("SELECT 1 FROM owner_conversation_messages WHERE id=?", ids[name])
    assert len(store.query("SELECT 1 FROM agent_messages WHERE id IN (?,?)", ids["chat_notice"], "ordinary")) == 2
    for name in ("send_owner_c", "mark_owner_c", "send_owner_d", "unrelated"):
        principal, namespace, operation, key = ids[name]
        assert store.query("SELECT 1 FROM idempotency_receipts WHERE principal_id=? AND target_namespace=? AND operation_name=? "
                           "AND idempotency_key=?", principal, namespace, operation, key)


def test_a_repeat_after_success_is_a_noop_and_a_fresh_inspection_is_empty(store):
    report = inspect(store)
    apply(store, report)
    after = (store.contents(), store.schema(), store.sequences())
    again = apply(store, report)
    assert again["applied"] is False and again["noop"] is True and again["removed"] == dict.fromkeys(report["counts"], 0)
    fresh = inspect(store)
    assert set(fresh["counts"].values()) == {0} and fresh["protected"] == report["protected"] and fresh["digest"] != report["digest"]
    assert apply(store, fresh)["noop"] is True
    assert (store.contents(), store.schema(), store.sequences()) == after


def test_deleted_events_leave_nudge_selection_and_a_mixed_attempt_keeps_its_surviving_delivery(store):
    ids = store.ids
    connection = store.connect()
    connection.row_factory = sqlite3.Row

    class Uow:
        pass

    uow = Uow()
    uow.connection = connection

    class AllowAll:
        def authorized(self, _uow, _agent, events):
            return {row["id"] for row in events}

    survivors = [ids["ordinary_event"], ids["owner_c1:event"]]
    mixed = json.loads(store.query("SELECT event_ids_json FROM inbox_nudge_attempts WHERE id=?", ids["mixed"])[0][0])
    deleted = set(retired(store)["events"])
    assert set(mixed) & deleted and set(mixed) - deleted
    assert sorted(row["id"] for row in nudges.pending_among(uow, store.coordinator, mixed)) == sorted(mixed)
    assert sorted(row["id"] for row in nudges.pending_among(uow, store.workers["a"], sorted(deleted))) != []
    report = inspect(store)
    apply(store, report)
    # The retired event numbers are gone from every pending read; the survivors of the same attempt still deliver.
    assert sorted(row["id"] for row in nudges.pending_among(uow, store.coordinator, mixed)) == sorted(set(mixed) - deleted)
    assert nudges.pending_among(uow, store.workers["a"], sorted(deleted)) == []
    for agent in (store.coordinator, store.workers["a"], store.workers["b"]):
        scan = nudges.scan_pending(uow, AllowAll(), agent)
        assert not set(scan.ids) & deleted
    assert set(survivors) <= set(nudges.scan_pending(uow, AllowAll(), store.coordinator).ids)
    # The attempts, their evidence and the retained references are untouched and inert.
    assert connection.execute("SELECT count(*) FROM inbox_nudge_attempts").fetchone()[0] == 3
    assert inspect(store)["retained_references"]["inbox_nudge_attempts_naming_a_deleted_event"] == 0
    connection.close()


# ----- refusals: the target is not the reviewed one -----------------------------------------------------------------------

@pytest.mark.parametrize("name", ["new retired message", "new retired conversation", "new retired receipt", "retired read moved",
                                  "schema changed"])
def test_each_post_review_change_is_refused_and_a_fresh_review_then_applies(planted, tmp_path, name):
    store = copy_store(planted, tmp_path / "each.sqlite3")
    ids = store.ids
    report = inspect(store)
    if name == "new retired message":
        store.execute("INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,created_at) "
                      "VALUES(?,?,?,?,?,?)", new_id(), ids["conversation_a"], store.owner, "owner", f"{MARKER} late", NOW)
    elif name == "new retired conversation":
        store.execute("INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,created_at,updated_at,"
                      "peer_agent_id) VALUES(?,?,?,?,?,?,?,?,?)", new_id(), f"peer:{store.workers['b']}:source:global:task:{new_id()}",
                      None, "task", new_id(), "owner_ordubashy", NOW, NOW, store.workers["b"])
    elif name == "new retired receipt":
        store.execute("INSERT INTO idempotency_receipts(principal_id,target_namespace,operation_name,idempotency_key,input_hash,"
                      "result_json,created_at) VALUES(?,?,?,?,?,?,?)", store.owner, "global", "owner_conversation_send", new_id(),
                      "c" * 64, json.dumps({"request_id": "x", "status": "ok",
                                            "data": {"conversation": {"agent_id": store.workers["a"]}}}), NOW)
    elif name == "retired read moved":
        store.execute("UPDATE owner_conversation_reads SET last_read_sequence=last_read_sequence+1 WHERE conversation_id=?",
                      ids["conversation_a"])
    else:
        store.execute("CREATE INDEX synthetic_extra ON owner_conversation_reads(updated_at)")
    error = unchanged(store, lambda: refusal(store, report, "schema_changed" if name == "schema changed" else "target_changed"))
    assert MARKER not in str(error) and MARKER not in json.dumps(error.details, default=str)
    assert apply(store, inspect(store))["applied"] is True and set(inspect(store)["counts"].values()) == {0}


def test_activity_in_the_protected_channel_between_review_and_apply_neither_blocks_nor_is_lost(store):
    ids = store.ids
    report = inspect(store)
    sequence = store.execute("INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,created_at) "
                             "VALUES(?,?,?,?,?,?)", new_id(), ids["conversation_c"], store.owner, "owner", f"{MARKER} new", NOW)
    store.execute("UPDATE owner_conversation_reads SET last_read_sequence=? WHERE conversation_id=?", sequence, ids["conversation_c"])
    kept = store.contents(("owner_conversation_messages", "owner_conversation_reads"))
    assert apply(store, report)["applied"] is True
    survivors = store.contents(("owner_conversation_messages", "owner_conversation_reads"))
    assert survivors["owner_conversation_reads"] == [row for row in kept["owner_conversation_reads"] if row[0] != ids["conversation_a"]
                                                     and row[0] != ids["conversation_b"]]
    assert any(row[0] == sequence for row in survivors["owner_conversation_messages"])


# ----- refusals: dependents and unexpected shapes -------------------------------------------------------------------------

def test_an_authored_reply_to_a_notice_present_at_review_blocks_the_apply_and_is_reported_by_identifier(store):
    reply = insert_reply(store, store.ids["notice_a1"])
    report = inspect(store)
    assert report["blocked"] is True
    columns = {(item["table"], item["column"]): item for item in report["plan"]["blockers"]}
    assert columns[("agent_messages", "reply_to")]["ids"] == [[reply]] and columns[("agent_messages", "thread_id")]["ids"] == [[reply]]
    assert MARKER not in json.dumps(report)
    error = unchanged(store, lambda: refusal(store, report, "blocked"))
    assert {(item["table"], item["column"]) for item in error.details["blockers"]} == set(columns)
    assert MARKER not in json.dumps(error.details, default=str)


def test_an_authored_reply_added_after_a_clean_review_blocks_and_changes_nothing(store):
    report = inspect(store)
    assert report["blocked"] is False
    reply = insert_reply(store, store.ids["notice_b2"], key="late-reply")
    error = unchanged(store, lambda: refusal(store, report, "blocked"))
    listed = {(item["table"], item["column"]): item["ids"] for item in error.details["blockers"]}
    assert listed[("agent_messages", "reply_to")] == [[reply]]
    # Once the reviewer has the dependent in front of them, a fresh inspection reports the same block (it is never silently skipped).
    assert inspect(store)["blocked"] is True


def test_a_task_thread_event_that_names_a_retired_inbox_event_blocks(store):
    ids = store.ids
    event = ids["owner_a1:event"]
    store.execute("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,refs_json,author_id,recorder_id,"
                  "attribution_json,event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", store.business, new_id(), "task",
                  ids["task_a"], "comment", f"{MARKER} canonical comment", "[]", store.owner, store.owner, "{}", event, NOW)
    report = inspect(store)
    assert report["blocked"] is True
    blocker = next(item for item in report["plan"]["blockers"] if item["table"] == "thread_messages")
    assert (blocker["column"], blocker["parent"], blocker["count"]) == ("event_id", "inbox_events", 1) and MARKER not in json.dumps(report)
    unchanged(store, lambda: refusal(store, report, "blocked"))
    assert store.query("SELECT count(*) FROM thread_messages") == [(1,)]


def test_a_forwarded_look_alike_with_the_derived_identifier_but_other_provenance_blocks_instead_of_being_guessed(store):
    ids = store.ids
    # The owner sent a thread-root message to worker A; its identifier happens to equal a retired message's derived notice
    # identifier for that recipient, but it has no historical header, so it is neither deleted nor ignored.
    look_alike = forwarded_id("private_conversation", ids["owner_b1"], store.workers["a"])
    event = store.execute("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
                          "VALUES(?,?,?,?,?,?,?)", None, "agent_messages", look_alike, store.owner, "direct_message",
                          "Owner message to a worker", NOW)
    store.execute("INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,"
                  "summary,current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", look_alike, event, None,
                  store.owner, store.workers["a"], None, look_alike, f"{MARKER} an ordinary message", NOW, None, store.owner,
                  store.workers["a"])
    store.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", store.workers["a"], event)
    report = inspect(store)
    blocker = next(item for item in report["plan"]["blockers"] if item["reason"] == "notice_relationship_mismatch")
    assert blocker["ids"] == [look_alike] and MARKER not in json.dumps(report)
    unchanged(store, lambda: refusal(store, report, "blocked"))


def test_unexpected_triggers_a_missing_guard_and_anomalous_keys_fail_closed(planted, tmp_path):
    cases = {
        "extra trigger on a touched table": ("CREATE TRIGGER synthetic_extra AFTER DELETE ON owner_conversation_reads BEGIN SELECT 1; END",
                                             "unexpected_triggers"),
        "missing immutability guard": ("DROP TRIGGER owner_conversation_messages_no_delete", "unexpected_triggers"),
        "changed immutability guard": ("DROP TRIGGER agent_messages_immutable_delete;"
                                       "CREATE TRIGGER agent_messages_immutable_delete BEFORE UPDATE ON agent_messages "
                                       "BEGIN SELECT RAISE(ABORT,'x'); END", "unexpected_triggers"),
        "null peer with a peer key": ("INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,audience,"
                                      "created_at,updated_at,peer_agent_id) VALUES('anomaly','peer:xyz:source:global:task:t',NULL,"
                                      "'task','t','owner_ordubashy','2026','2026',NULL)", "key_peer_mismatch"),
        "retired row whose key names no peer": ("INSERT INTO owner_conversations(id,source_key,business_id,source_type,source_id,"
                                                "audience,created_at,updated_at,peer_agent_id) VALUES('anomaly','source:global:task:t',"
                                                "NULL,'task','t','owner_ordubashy','2026','2026','WORKER')", "key_peer_mismatch"),
    }
    for number, (name, (statement, reason)) in enumerate(cases.items()):
        store = copy_store(planted, tmp_path / f"case-{number}.sqlite3")
        connection = store.connect()
        connection.executescript(statement.replace("WORKER", store.workers["a"]))
        connection.close()
        report = inspect(store)
        assert report["blocked"] is True, name
        assert any(item["reason"] == reason for item in report["plan"]["blockers"]), name
        unchanged(store, lambda: refusal(store, report, "blocked"))


# ----- rollback -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("failing_table", ["owner_conversation_messages", "owner_conversations", "agent_messages"])
def test_a_failure_part_way_rolls_back_the_deletions_and_the_dropped_immutability_triggers(store, monkeypatch, failing_table):
    report = inspect(store)
    original = cleanup.delete_row

    def broken(connection, statement, identity):
        if statement.startswith(f"DELETE FROM {failing_table} "):
            raise sqlite3.OperationalError("synthetic disk failure")
        return original(connection, statement, identity)

    with monkeypatch.context() as patch:
        patch.setattr(cleanup, "delete_row", broken)
        error = unchanged(store, lambda: refusal(store, report, "apply_failed"))
    assert "synthetic disk failure" in str(error)
    # The immutability triggers are present and active again after the rollback, and nothing remains locked.
    with pytest.raises(sqlite3.DatabaseError):
        store.execute("DELETE FROM owner_conversation_messages WHERE id=?", store.ids["owner_a1"])
    # The same reviewed apply succeeds once the failure is gone.
    assert apply(store, report)["applied"] is True


def test_a_failure_after_the_triggers_are_restored_still_rolls_everything_back(store, monkeypatch):
    report = inspect(store)
    original, calls = cleanup.schema_fingerprint, {"count": 0}

    def flaky(connection):
        calls["count"] += 1
        if calls["count"] == 2:        # the check after the deletions and the restored triggers
            raise sqlite3.OperationalError("synthetic failure after restore")
        return original(connection)

    with monkeypatch.context() as patch:
        patch.setattr(cleanup, "schema_fingerprint", flaky)
        unchanged(store, lambda: refusal(store, report, "apply_failed"))
    assert apply(store, report)["applied"] is True


class SqliteWith:
    """The ``sqlite3`` module as the helper sees it, with only ``connect`` replaced (a short busy timeout and a statement trace)."""

    def __init__(self, connect):
        self.connect = connect

    def __getattr__(self, name):
        return getattr(sqlite3, name)


@pytest.mark.parametrize("mode", ["supplied-default-isolation", "supplied-autocommit", "owned-path"])
def test_a_failure_at_commit_rolls_back_restores_the_connection_and_the_same_review_then_applies(store, monkeypatch, mode):
    setup = store.connect()
    setup.execute("PRAGMA journal_mode=DELETE")                      # rollback-journal: a held reader blocks the writer's COMMIT
    setup.close()
    report = inspect(store)
    before = (store.contents(), store.schema(), store.sequences())
    conversations = store.query("SELECT count(*) FROM owner_conversations")[0][0]
    trace, real_connect = [], sqlite3.connect
    if mode == "owned-path":
        def short(location, *arguments, **keywords):                  # the helper's own connection, no 30 s wait
            connection = real_connect(location, *arguments, **{**keywords, "timeout": 0.01})
            if "mode=rw" in str(location):
                connection.set_trace_callback(trace.append)
            return connection

        monkeypatch.setattr(cleanup, "sqlite3", SqliteWith(short))
        target, isolation = store.path, None
    else:
        isolation = "" if mode == "supplied-default-isolation" else None
        target = real_connect(store.path, timeout=0.01, isolation_level=isolation)
        target.create_function("inbox_actor", 0, lambda: None)
        target.execute("PRAGMA foreign_keys=ON")
        target.set_trace_callback(trace.append)
    reader = real_connect(store.path, isolation_level=None)
    reader.execute("BEGIN")
    assert reader.execute("SELECT count(*) FROM owner_conversations").fetchone()[0] == conversations
    try:
        error = pytest.raises(CleanupError, apply, store, report, target=target)
        assert error.value.code == "apply_failed" and "locked" in str(error.value) and "rolled back" in str(error.value)
        # The failure is at COMMIT itself: the deletions ran, then COMMIT, then the rollback.
        deleted = next(index for index, text in enumerate(trace) if text.startswith("DELETE FROM owner_conversations"))
        assert deleted < trace.index("COMMIT") < trace.index("ROLLBACK")
        if mode != "owned-path":
            assert not target.in_transaction                         # no manual rollback was needed
            assert target.isolation_level == isolation
            assert target.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert target.execute("SELECT count(*) FROM owner_conversations").fetchone()[0] == conversations
        assert reader.execute("SELECT count(*) FROM owner_conversations").fetchone()[0] == conversations
        assert (store.contents(), store.schema(), store.sequences()) == before     # data, schema, triggers, counters
    finally:
        reader.rollback()
        reader.close()
    # Once the reader has released its lock the same reviewed inspection applies, exactly as an undisturbed run would.
    result = apply(store, report, target=target)
    assert result["applied"] is True and result["removed"] == report["counts"]
    if mode != "owned-path":
        assert not target.in_transaction and target.isolation_level == isolation
        assert target.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        target.close()
    monkeypatch.undo()
    assert store.contents() == without_retired(store, before[0])
    assert store.schema() == before[1] and store.sequences() == before[2]
    assert store.query("PRAGMA foreign_key_check") == []


# ----- retired-peer request replays ---------------------------------------------------------------------------------------

@pytest.fixture
def live(tmp_path):
    legacy = plant(tmp_path)
    try:
        yield legacy
    finally:
        try:
            legacy.instance.runtime.release_instance()
        finally:
            legacy.database.close()


def test_a_retired_peer_request_replay_is_refused_before_and_after_the_cleanup(live):
    """Receipts of retired-peer requests (stored under the hash the engine really computes, proven by a control) are never
    replayed by the Ordubashy-only service, and the cleanup then removes them while the Ordubashy channel replays as before."""
    owner, worker = live.instance.human(live.business), live.workers["a"]
    source = {"type": "task", "id": live.ids["task_a"], "business_id": live.business}
    # CONTROL: a real Ordubashy-channel send is replayed from its receipt, and the helper reproduces its stored hash exactly.
    control = {"source": source, "text": "Control to Ordubashy"}
    control_key = new_id()
    sent = live.call(owner, "owner_conversation_send", control, control_key)
    assert sent.status == "ok", sent.error
    computed, _ = retired_request_hash(live, owner, "owner_conversation_send", control)
    assert live.query("SELECT input_hash FROM idempotency_receipts WHERE principal_id=? AND operation_name=? AND idempotency_key=?",
                      live.owner, "owner_conversation_send", control_key) == [(computed,)]
    replayed = live.call(owner, "owner_conversation_send", control, control_key)
    assert replayed.status == "ok" and replayed.data["message"]["id"] == sent.data["message"]["id"]
    # The saved result of a retired-peer request, stored under the hash that very request has today.
    request, key = {"source": source, "text": "To the worker", "agent_id": worker}, new_id()
    digest, namespace = retired_request_hash(live, owner, "owner_conversation_send", request)
    live.sql("INSERT INTO idempotency_receipts(principal_id,target_namespace,operation_name,idempotency_key,input_hash,result_json,"
             "created_at) VALUES(?,?,?,?,?,?,?)", live.owner, namespace, "owner_conversation_send", key, digest,
             json.dumps({"request_id": "old", "status": "ok", "warnings": [],
                         "data": {"message": {"id": live.ids["owner_a1"]},
                                  "conversation": {"id": live.ids["conversation_a"], "agent_id": worker}}}), NOW)

    def refused(arguments, idempotency):
        result = live.call(owner, "owner_conversation_send", arguments, idempotency)
        assert result.status == "error" and result.error["code"] == "forbidden", (result.status, result.error)
        assert MARKER not in str(dict(result.error))

    messages = live.query("SELECT count(*) FROM owner_conversation_messages")
    refused(request, key)                                           # the stored receipt is not replayed
    refused({**request, "text": "Another request to the worker"}, new_id())      # nor is a new request served
    assert live.query("SELECT count(*) FROM owner_conversation_messages") == messages
    # The cleanup of this very store, with the runtime idle, removes the retired receipt and thread with everything else.
    report = cleanup.inspect_target(live.path, ordubashy_id=live.coordinator)
    assert report["blocked"] is False and any(tuple(item)[3] == key for item in report["plan"]["selection"]["idempotency_receipts"])
    cleanup.apply_cleanup(live.path, ordubashy_id=live.coordinator, reviewed=report, authorization=authorized(report))
    assert live.query("SELECT count(*) FROM idempotency_receipts WHERE idempotency_key=?", key) == [(0,)]
    assert live.query("SELECT count(*) FROM owner_conversation_messages WHERE conversation_id IN (?,?)",
                      live.ids["conversation_a"], live.ids["conversation_b"]) == [(0,)]
    refused(request, key)                                           # still refused, now with no receipt at all
    refused({**request, "text": "Another request to the worker"}, new_id())
    # The Ordubashy channel is unaffected: its history is served and its saved send still replays.
    again = live.call(owner, "owner_conversation_send", control, control_key)
    assert again.status == "ok" and again.data["message"]["id"] == sent.data["message"]["id"]
    read = live.call(owner, "owner_conversation_get", {"source": source})
    assert read.status == "ok" and [m["body"] for m in read.data["messages"]] == ["Control to Ordubashy"]
