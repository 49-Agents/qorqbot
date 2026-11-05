"""Release ordering of the Telegram migrations: main's schema (1..105, including the learning issue bridge, the daily report
attention and the Global general tables) stays byte-identical, and the Telegram bridge (106) and its forum topics (107) apply
after it."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bazarlyk.storage import Database, TABLES
from bazarlyk.storage import database as storage
from bazarlyk.storage.exports import build_exports
from foundation_support import BUSINESS, HUMAN
from test_learning_issue_storage import NEW_TABLES as LEARNING_ISSUE_TABLES, action, candidate, issue

NOW = "2026-09-29T12:00:00.000000Z"
COLUMNS = json.dumps([{"id": "name", "name": "Name", "type": "text"}])
TELEGRAM_TABLES = ("telegram_state", "telegram_updates", "telegram_messages", "telegram_outbox", "telegram_events",
                   "telegram_signoff_posts", "telegram_owner_inputs", "telegram_cleanup_jobs")
# Main e200ec4e1f0be1f8be7285d78c2598e756637093 carries migrations 1..105: the digest of every "version:checksum" line.
MAIN_CHAIN_SHA256 = "94974fdf427d418fceea0cbf174d611ecdaf0476a90bb7f08cbb820f552e3bdf"
MIGRATION_103_SHA256 = "ddb0eb95254f0a5bf4bdbd241b2f7e7ec2dbb4f36e26103085e49ac1f3779ae5"  # learning issue bridge
MIGRATION_104_SHA256 = "cbea7214d452f14c6331752654a10bc3b2ea7c2b4cb84d26f8a420b09c31a067"  # daily report attention
MIGRATION_105_SHA256 = "decb7ef44c2231437f761c7c2f7b529c38029f38802829d191bc0246f6b7de5a"  # Global general tables
MIGRATION_106_SHA256 = "4891446c65f2d6cc146aa5a731cf6fd83ec9111e324f0eefe8197be681f1f65c"  # Telegram bridge


def test_main_migrations_are_frozen_and_telegram_follows_them_as_106_and_107():
    migrations = Database.migrations()
    chain = "\n".join(f"{version}:{checksum}" for version, checksum, _ in migrations[:105])
    assert hashlib.sha256(chain.encode()).hexdigest() == MAIN_CHAIN_SHA256
    assert [(version, checksum) for version, checksum, _ in migrations[102:106]] == [
        (103, MIGRATION_103_SHA256), (104, MIGRATION_104_SHA256), (105, MIGRATION_105_SHA256), (106, MIGRATION_106_SHA256)]
    folder = Path(storage.__file__).with_name("migrations")
    assert [path.name for path in sorted(folder.glob("10[3-9]_*.sql"))] == [
        "103_learning_issue_bridge.sql", "104_daily_report_attention.sql", "105_global_general_tables.sql",
        "106_telegram_bridge.sql", "107_telegram_forum_topics.sql", "108_agent_viewbox.sql",
        "109_marketing_private_conversations.sql"]
    # Agent Viewbox (108) follows the Telegram migrations in the Focus/Viewbox integration and leaves 106 and 107 untouched;
    # the Marketing private conversation binding (109) follows it as the next contiguous number.
    assert len(migrations) == 109


def seed_production_records(database, root):
    """Representative records of the features main already carries: learning issues, daily report attention and tables."""
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": HUMAN, "kind": "human", "display_name": "Owner"})
        uow.repository("businesses").insert({"id": BUSINESS, "slug": "first", "name": "first", "workspace": str(root / "first"),
                                              "hq_path": str(root / "first" / "hq")})
        candidate(uow)
        issue(uow, candidate="c1")
        uow.repository("learning_issue_candidates", business_id=BUSINESS).insert({
            "candidate_id": "c1", "issue_id": "iss_1", "ordinal": 1, "package_revision": 1, "created_by": HUMAN})
        action(uow, reasoning="Recorded reasoning", reasoning_source="owner_direct")
        uow.repository("learning_issue_links", business_id=BUSINESS).insert({
            "link_id": "l1", "issue_id": "iss_1", "predecessor_issue_id": "iss_0", "relation": "split", "recorded_at": NOW})
        uow.repository("learning_engine_packages", business_id=BUSINESS).insert({
            "engine_id": "engine-test", "issue_id": "iss_1", "package_revision": 1, "ordinal": 1, "package_hash": "a" * 64,
            "material_evidence_revision": 1, "outcome": "created", "candidate_id": "c1", "package_json": {"kind": "engine_cluster"},
            "received_by": HUMAN})
        # Global general tables (an explicit NULL business) beside a business general table and its append-only ledger.
        for sequence, (business, purpose) in enumerate(((None, "general"), (BUSINESS, "general"), (BUSINESS, "ledger")), 1):
            table = f"table-{business and 'biz' or 'global'}-{purpose}"
            uow.repository("data_tables", business_id=business).insert({"id": table, "title": table, "columns_json": COLUMNS,
                                                                        "created_by": HUMAN, "purpose": purpose})
            uow.repository("data_rows", business_id=business).insert({
                "id": f"row-{table}", "table_id": table, "position": 0, "cells_json": json.dumps({"amount": 250} if purpose == "ledger"
                                                                                                     else {"name": "kept"}),
                "created_by": HUMAN, "updated_by": HUMAN})
            uow.repository("table_versions", business_id=business).insert({
                "id": f"version-{table}", "table_id": table, "table_revision": 1, "sequence": sequence, "actor_id": HUMAN,
                "operation": "tables_create", "path": f"hq/tables/{table}.json", "snapshot_json": "{}", "snapshot_hash": "h"})
    connection = database._connect()
    try:
        connection.execute("INSERT INTO jobs(id,business_id,target_namespace,kind,principal_id,arguments_json,dedupe_key,state,"
                           "created_at,updated_at) VALUES ('job-1',NULL,'global','reports.daily',?,'{}','job-1','queued',?,?)",
                           (HUMAN, NOW, NOW))
        connection.execute("INSERT INTO daily_report_runs(id,report_date,trigger,requested_by,job_id,state,created_at,updated_at) "
                           "VALUES ('run-1','2026-09-28','manual',?,'job-1','queued',?,?)", (HUMAN, NOW, NOW))
        connection.execute("INSERT INTO daily_report_owner_attention(owner_id,acknowledged_through,snoozed_report_date,snoozed_until,"
                           "created_at,updated_at) VALUES (?,'2026-09-27','2026-09-28',?,?,?)", (HUMAN, NOW, NOW, NOW))
        connection.commit()
    finally:
        connection.close()


def snapshot(database):
    """Every row of every table that is not Telegram's, and every schema object that is not Telegram's."""
    connection = database._connect()
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'telegram_%' ORDER BY name")]
        rows = {table: sorted((tuple(row) for row in connection.execute(f'SELECT * FROM "{table}"')), key=repr) for table in tables
                if table != "schema_migrations"}
        objects = [tuple(row) for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND name NOT LIKE 'telegram_%' "
            "ORDER BY type, name")]
        migrations = [tuple(row) for row in connection.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")]
        return rows, objects, migrations
    finally:
        connection.close()


def test_a_schema_105_database_upgrades_to_107_with_every_existing_record_and_object_unchanged(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize(through=105)
    seed_production_records(database, tmp_path)
    before_rows, before_objects, before_migrations = snapshot(database)
    assert len(before_migrations) == 105 and before_rows["learning_issues"] and before_rows["daily_report_owner_attention"]
    assert len(before_rows["data_tables"]) == 3 and len(before_rows["data_rows"]) == 3 and len(before_rows["table_versions"]) == 3
    connection = database._connect()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 105
    assert not {row[0] for row in connection.execute("SELECT name FROM sqlite_master")} & {*TELEGRAM_TABLES}
    connection.close()

    database.initialize(through=107)

    after_rows, after_objects, after_migrations = snapshot(database)
    # Nothing that existed changed: not a row, not a table, index, trigger or view, not a recorded checksum.
    assert after_rows == before_rows
    assert after_objects == before_objects
    assert after_migrations[:105] == before_migrations
    assert [version for version, _ in after_migrations[105:]] == [106, 107]
    assert [checksum for _, checksum in after_migrations[105:]] == [entry[1] for entry in Database.migrations()[105:107]]

    connection = database._connect()
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 107
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        # The Telegram schema is complete and empty: no poller, cursor, binding or integration state comes from migrating.
        assert {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TELEGRAM_TABLES} == dict.fromkeys(
            TELEGRAM_TABLES, 0)
        assert all(table in TABLES and database.columns(table) for table in TELEGRAM_TABLES)
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1  # only the seeded report job
        assert connection.execute("SELECT count(*) FROM routines").fetchone()[0] == 0
        for table in ("telegram_messages", "telegram_outbox"):
            assert "message_thread_id" in {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        # The Telegram invariants hold on the upgraded database.
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):  # the state row is a singleton
            connection.execute("INSERT INTO telegram_state(id,bot_id,installation_id,owner_principal_id,owner_telegram_user_id,"
                               "config_generation,binding_fingerprint,binding_effective_at,activation_at,created_at,updated_at) "
                               "VALUES ('other',1,?,?,1,1,?,?,?,?,?)", ("0" * 36, HUMAN, "f" * 64, NOW, NOW, NOW, NOW))
        message = ("INSERT INTO telegram_messages(id,direction,chat_name,chat_id,message_thread_id,purpose,body,occurred_at,created_at,"
                   "updated_at) VALUES (?,'outbound','founder',-5,?,'reply','x',?,?,?)")
        with pytest.raises(sqlite3.IntegrityError):  # the General topic is never a destination
            connection.execute(message, ("m0", 1, NOW, NOW, NOW))
        connection.execute(message, ("m1", 12, NOW, NOW, NOW))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE telegram_messages SET message_thread_id=13 WHERE id='m1'")
        connection.rollback()
        # Main's own guards on the table stores survive beside the Telegram schema: an append-only ledger and the rule that
        # only a business may hold a non-general table.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE data_rows SET cells_json='{\"amount\":1}' WHERE table_id='table-biz-ledger'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM data_rows WHERE table_id='table-biz-ledger'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO data_tables(business_id,id,title,columns_json,created_by,created_at,updated_at,purpose) "
                               "VALUES (NULL,'global-ledger','x',?,?,?,?,'ledger')", (COLUMNS, HUMAN, NOW, NOW))
        connection.rollback()
    finally:
        connection.close()

    # Every current-main feature still reads its records through the registered repositories and the export projection.
    with database.read() as uow:
        assert uow.repository("learning_issues", business_id=BUSINESS).get("iss_1")["engine_id"] == "engine-test"
        assert uow.repository("daily_report_owner_attention").get(HUMAN)["acknowledged_through"] == "2026-09-27"
        assert uow.repository("daily_report_runs").get("run-1")["state"] == "queued"
        assert uow.repository("data_tables", business_id=None).get("table-global-general")["title"] == "table-global-general"
        assert uow.repository("data_rows", business_id=None).get("row-table-global-general")["table_id"] == "table-global-general"
        assert {table: uow.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in LEARNING_ISSUE_TABLES[:4]} == {
            "learning_issues": 1, "learning_issue_candidates": 1, "learning_issue_actions": 1, "learning_issue_links": 1}
    database.backup(tmp_path / "after.sqlite3")
    bundle = build_exports(tmp_path / "after.sqlite3", tmp_path)
    assert [row["issue_id"] for row in json.loads(bundle.files["first/hq/exports/learning_issues.json"])["records"]] == ["iss_1"]
    assert [row["id"] for row in json.loads(bundle.files["exports/data_tables.json"])["records"]] == ["table-global-general"]
    assert sorted(row["id"] for row in json.loads(bundle.files["first/hq/exports/data_tables.json"])["records"]) == [
        "table-biz-general", "table-biz-ledger"]
    assert not any("telegram" in name for name in bundle.files)  # the bridge's private conversation is never projected
