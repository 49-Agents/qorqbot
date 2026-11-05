"""Schema migration 100: shared history installed and existing working memory imported atomically."""

import base64
import hashlib
import json
import sqlite3

import pytest
from qorqut_business_docs_versioning import HistoryStore

from qorqut_operations.contracts import DomainError
from bazarlyk.storage import Database, history

OWNER = "00000000-0000-4000-8000-000000000001"
AGENT = "00000000-0000-4000-8000-000000000002"
BUSINESS = "00000000-0000-4000-8000-000000000003"
MEMORY = "00000000-0000-4000-8000-000000000005"
TEXTS = [b"First memory.\n", b"Second memory.\n", b"Third memory.\n"]
PRODUCT = {"id": "product-1", "name": "Widget", "attributes_json": {"tagline": "Kept"}}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def database_before_100(tmp_path, chain):
    """A database at schema 099 (Products) holding one agent's legacy working-memory chain and a Product."""
    database = Database(tmp_path / "state.sqlite3").initialize(through=99)
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": OWNER, "kind": "human", "display_name": "Owner"})
        uow.repository("principals").insert({"id": AGENT, "kind": "agent", "display_name": "Agent"})
        uow.repository("businesses").insert({"id": BUSINESS, "slug": "one", "name": "One",
                                             "workspace": str(tmp_path / "one"), "hq_path": str(tmp_path / "one" / "hq")})
        uow.repository("agent_profiles", business_id=BUSINESS).insert({"agent_id": AGENT, "principal_id": AGENT,
            "home_business_id": BUSINESS, "namespace": BUSINESS, "name": "Agent",
            "workspace": str(tmp_path / "agent"), "profile_json": {"role": "coder"}})
        last = chain[-1] if chain else (1, TEXTS[0], digest(TEXTS[0]))
        uow.repository("documents", business_id=BUSINESS).insert({"id": MEMORY, "namespace": BUSINESS,
            "owning_agent_id": AGENT, "kind": "working_memory", "path": "context/agent-memory/agent.md",
            "content_hash": digest(last[1]), "revision": last[0]})
        for version, data, recorded_hash in chain:
            uow.repository("working_memory_versions", business_id=BUSINESS).insert({"document_id": MEMORY,
                "version": version, "file_bytes": base64.b64encode(data).decode(), "content_hash": recorded_hash,
                "source_refs_json": [{"type": "task", "id": f"task-{version}", "token": f"r{version}"}],
                "legacy_plan_json": {"id": "plan-1", "revision": version} if version == 1 else {},
                "author_id": AGENT if version > 1 else OWNER, "created_at": f"2026-09-0{version}T10:00:00.000000+00:00"})
        uow.connection.execute("INSERT INTO action_logs(business_id,id,tag,summary,actor_id,occurred_at,created_at) "
                               "VALUES (?,?,?,?,?,?,?)", (BUSINESS, "old", "operations", "Old", OWNER,
                                                          "2026-09-01T10:00:00+00:00", "2026-09-01T10:00:00+00:00"))
        uow.repository("products", business_id=BUSINESS).insert({"id": PRODUCT["id"], "name": PRODUCT["name"],
            "attributes_json": PRODUCT["attributes_json"], "created_by": OWNER})
    return database


def tables(path):
    connection = sqlite3.connect(path)
    try:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        top = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        # Products (migration 099) must survive the history step whether it applies or rolls back.
        assert connection.execute("SELECT checksum FROM schema_migrations WHERE version=99").fetchone()[0] == \
            next(checksum for version, checksum, _ in Database.migrations() if version == 99)
        product = connection.execute("SELECT name, attributes_json FROM products WHERE business_id=? AND id=?",
                                     (BUSINESS, PRODUCT["id"])).fetchone()
        assert (product[0], json.loads(product[1])) == (PRODUCT["name"], PRODUCT["attributes_json"])
    finally:
        connection.close()
    return names, top


def test_upgrade_imports_every_working_memory_version_in_order(tmp_path):
    chain = [(number, data, digest(data)) for number, data in enumerate(TEXTS, 1)]
    database = database_before_100(tmp_path, chain).initialize()
    with database.read() as uow:
        store = HistoryStore(uow.connection)
        saved = store.list_versions("business:" + BUSINESS, MEMORY, limit=10).versions
        assert [v.version for v in saved] == [3, 2, 1] and [v.author_id for v in saved] == [AGENT, AGENT, OWNER]
        for number, data in enumerate(TEXTS, 1):
            assert store.get_document("business:" + BUSINESS, MEMORY, version=number).content == data.decode()
            legacy = history.working_memory_version(uow.connection, BUSINESS, MEMORY, number)
            assert base64.b64decode(legacy["file_bytes"]) == data
        assert history.verify_working_memory(uow.connection) == []
        # Activity facts are imported by the explicit baseline, not by the schema migration.
        assert store.list_events(BUSINESS).events == ()
    names, top = tables(tmp_path / "state.sqlite3")
    assert top == len(Database.migrations()) and {"qbdv_document_versions", "working_memory_provenance"} <= names
    # Repeating initialisation or opening the upgraded database adds nothing.
    def count():
        with database.read() as uow:
            return tuple(uow.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("qbdv_document_versions", "qbdv_business_events", "working_memory_provenance"))
    before = count()
    Database(tmp_path / "state.sqlite3").initialize()
    Database(tmp_path / "state.sqlite3").open_existing()
    assert count() == before


def test_open_existing_refuses_a_database_without_history_and_does_not_create_it(tmp_path):
    database_before_100(tmp_path, [(1, TEXTS[0], digest(TEXTS[0]))])
    with pytest.raises(DomainError):
        Database(tmp_path / "state.sqlite3").open_existing()
    names, top = tables(tmp_path / "state.sqlite3")
    assert top == 99 and "qbdv_document_versions" not in names


@pytest.mark.parametrize("chain", [
    [(1, TEXTS[0], digest(TEXTS[0])), (2, TEXTS[1], digest(b"something else"))],
    [(1, TEXTS[0], digest(TEXTS[0])), (3, TEXTS[2], digest(TEXTS[2]))],
], ids=["corrupt", "gap"])
def test_upgrade_rolls_back_entirely_on_corrupt_or_gapped_memory(tmp_path, chain):
    database = database_before_100(tmp_path, chain)
    with pytest.raises(DomainError) as raised:
        database.initialize()
    assert raised.value.code == "migration_conflict"
    names, top = tables(tmp_path / "state.sqlite3")
    assert top == 99 and not {"qbdv_document_versions", "qbdv_business_events", "working_memory_provenance"} & names


def test_receipt_frozen_by_the_previous_release_finalises_from_its_legacy_row(tmp_path):
    database = database_before_100(tmp_path, [(1, TEXTS[0], digest(TEXTS[0]))]).initialize()
    with database.transaction() as uow:
        # An old plan's record step wrote the legacy content row in the finalising transaction.
        uow.repository("working_memory_versions", business_id=BUSINESS).insert({"document_id": MEMORY, "version": 2,
            "file_bytes": base64.b64encode(TEXTS[1]).decode(), "content_hash": digest(TEXTS[1]),
            "source_refs_json": [], "author_id": AGENT, "created_at": "2026-09-20T10:00:00.000000+00:00"})
        row = {"id": MEMORY, "business_id": BUSINESS, "revision": 2}
        saved = history.capture_working_memory(uow.connection, row, TEXTS[1].decode(), "receipt-1")
        assert saved.version == 2 and saved.author_id == AGENT
    with database.read() as uow:
        assert history.working_memory_version(uow.connection, BUSINESS, MEMORY)["version"] == 2
    with database.transaction() as uow:
        row = {"id": MEMORY, "business_id": BUSINESS, "revision": 3}
        with pytest.raises(DomainError) as raised:
            history.capture_working_memory(uow.connection, row, TEXTS[2].decode(), "receipt-2")
        assert raised.value.code == "apply_incomplete"


def raw(database, *statements):
    with database.transaction() as uow:
        for statement in statements:
            uow.connection.execute(statement)


FULL = [(number, data, digest(data)) for number, data in enumerate(TEXTS[:2], 1)]


@pytest.mark.parametrize("chain, statements, expected", [
    # A registered memory with no saved versions at all.
    ([], (), "missing_chain"),
    # The registry points at a committed version 2 whose content row is missing.
    (FULL[:1], (f"UPDATE documents SET revision=2, content_hash='{digest(TEXTS[1])}'",), "missing_latest"),
    # The registered memory lost its business scope while its chain is business-scoped.
    (FULL, ("UPDATE documents SET business_id=NULL",), "scope_mismatch"),
    # Versions attached to a document that is no longer working memory.
    (FULL, ("UPDATE documents SET kind='note'",), "kind_mismatch"),
    # The registered revision is ahead of the chain although its content is the latest version.
    (FULL, ("UPDATE documents SET revision=3",), "revision_mismatch"),
], ids=["absent-chain", "missing-tail", "scope", "kind", "revision"])
def test_upgrade_refuses_memory_inconsistent_with_its_registration(tmp_path, chain, statements, expected):
    raw(database_before_100(tmp_path, chain), *statements)
    with pytest.raises(DomainError) as raised:
        Database(tmp_path / "state.sqlite3").initialize()
    assert raised.value.code == "migration_conflict"
    assert expected in {item["problem"] for item in raised.value.details["problems"]}
    names, top = tables(tmp_path / "state.sqlite3")
    assert top == 99 and not {"qbdv_document_versions", "qbdv_business_events", "working_memory_provenance"} & names


def test_upgrade_copies_provenance_exactly_and_preserves_original_authors_and_times(tmp_path):
    database = database_before_100(tmp_path, FULL).initialize()
    with database.read() as uow:
        fields = "document_id,version,business_id,content_hash,source_refs_json,legacy_plan_json,author_id,created_at"
        legacy = uow.connection.execute(f"SELECT {fields} FROM working_memory_versions ORDER BY version").fetchall()
        copied = uow.connection.execute(f"SELECT {fields} FROM working_memory_provenance ORDER BY version").fetchall()
        assert [tuple(row) for row in copied] == [tuple(row) for row in legacy]
        store = HistoryStore(uow.connection)
        for row in legacy:
            saved = store.get_document("business:" + BUSINESS, MEMORY, version=row["version"])
            assert saved.author_id == row["author_id"] and saved.content_sha256 == row["content_hash"]
            assert saved.created_at.startswith(row["created_at"][:19])
        old = history.working_memory_version(uow.connection, BUSINESS, MEMORY, 1)
        assert old["legacy_plan_json"] == {"id": "plan-1", "revision": 1} and old["author_id"] == OWNER
        assert old["source_refs_json"] == [{"type": "task", "id": "task-1", "token": "r1"}]


def test_upgrade_accepts_archived_conversion_memory_and_an_applying_receipt(tmp_path):
    # Conversion archived a retired agent's memory: its revision moved on without new content.
    raw(database_before_100(tmp_path, FULL), "UPDATE documents SET archived=1, revision=3")
    database = Database(tmp_path / "state.sqlite3").initialize()
    with database.read() as uow:
        assert history.verify_working_memory(uow.connection) == []
        assert HistoryStore(uow.connection).get_document("business:" + BUSINESS, MEMORY).version == 2
    # An archived memory whose registered content is not its latest version is still refused.
    other = tmp_path / "other"
    other.mkdir()
    raw(database_before_100(other, FULL), f"UPDATE documents SET archived=1, revision=3, content_hash='{digest(b'x')}'")
    with pytest.raises(DomainError) as raised:
        Database(other / "state.sqlite3").initialize()
    assert raised.value.code == "migration_conflict"
    # A receipt still applying keeps its last committed registry state and migrates normally.
    pending = tmp_path / "pending"
    pending.mkdir()
    raw(database_before_100(pending, FULL), "UPDATE documents SET availability='applying'")
    database = Database(pending / "state.sqlite3").initialize()
    with database.read() as uow:
        assert history.verify_working_memory(uow.connection) == []


def test_verifier_reports_later_inconsistencies_instead_of_a_clean_result(tmp_path):
    database = database_before_100(tmp_path, FULL).initialize()
    with database.transaction() as uow:
        # A later registry change with no committed content version, and a second memory with no chain.
        uow.connection.execute("UPDATE documents SET revision=3, content_hash=? WHERE id=?", (digest(TEXTS[2]), MEMORY))
        uow.repository("documents", business_id=None).insert({"id": "memory-2", "namespace": "global",
            "owning_agent_id": AGENT, "kind": "working_memory", "path": "context/agent-memory/global.md",
            "content_hash": digest(TEXTS[0]), "revision": 1})
    with database.read() as uow:
        problems = {(item["document_id"], item["problem"]) for item in history.verify_working_memory(uow.connection)}
    assert problems == {(MEMORY, "missing_latest"), ("memory-2", "missing_chain")}
