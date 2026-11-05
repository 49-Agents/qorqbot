"""Focused synthetic checks for the typed operational-history export library.

Every test builds its own temporary Operations target store from the package's real schema machinery plus a small
synthetic domain. The domain mixes safe identity fields with secrets, a nested JSON setting with a nested secret, an
item discussion that shares one table with a private direct-message channel, an opaque event log and a direct-SQL
table that no contribution classifies. Nothing here is a production export definition.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

import pytest

from qorqut_operations.contracts import canonical_bytes
from qorqut_operations.history_exports.fields import FieldValueError
from qorqut_operations.history_exports import (Arr, Boolean, EXPORTED, ExportCatalog, HistoryCompositionError,
                                               HistoryContribution, HistoryExportError, Integer, Obj, PRIVATE_EXCLUDED,
                                               REPRESENTED, Real, RowRequest, SEPARATE_HISTORY, StructuredField,
                                               TECHNICAL_EXCLUDED, TableClassification, TableExport, Text,
                                               declared_json, shard_id)
from qorqut_operations.storage import StorageContribution, TableSpec
from support import database_at

SECRET = "TOPSECRET-value"

SCHEMA = (
    """CREATE TABLE people (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, display_name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        credential_token TEXT, settings_json TEXT, revision INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE threads (id TEXT PRIMARY KEY, channel TEXT NOT NULL, title TEXT NOT NULL)""",
    """CREATE TABLE messages (
        id INTEGER PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id), author_id TEXT NOT NULL, body TEXT NOT NULL,
        score REAL, attachments_json TEXT)""",
    """CREATE TABLE message_search (message_id INTEGER PRIMARY KEY, terms TEXT NOT NULL)""",
    """CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL)""",
    """CREATE TABLE outbox (id INTEGER PRIMARY KEY, destination TEXT NOT NULL, raw_arguments TEXT)""",
    """CREATE TABLE legacy_versions (id INTEGER PRIMARY KEY, body TEXT NOT NULL)""",
    # Direct SQL: absent from the repository specifications, so only reflection can find it.
    """CREATE TABLE scratch_cache (id INTEGER PRIMARY KEY, blob_text TEXT NOT NULL)""",
)
SPECS = {name: TableSpec(("id",), revision_column=None) for name in
         ("people", "threads", "messages", "events", "outbox", "legacy_versions")}
SPECS["message_search"] = TableSpec(("message_id",), revision_column=None)

SETTINGS = Obj(nullable=True, fields={"theme": Text(choices=("light", "dark")), "notify": Boolean(),
                       "labels": Arr(item=Text(max_length=40), max_items=5)})


def internal_only(uow, request: RowRequest):
    """Trusted selector: ordinary item discussion only, never the private direct channel."""
    return request.select(uow, '"messages"."thread_id" IN (SELECT "id" FROM "threads" WHERE "channel" = ?)', ("item",))


def people_export(**changes):
    values = dict(
        table="people", key=("id",),
        columns={"id": Text(), "kind": Text(choices=("human", "agent")), "display_name": Text(), "active": Boolean(),
                 "revision": Integer()},
        structured={"settings_json": StructuredField(SETTINGS, declared_json(SETTINGS))})
    values.update(changes)
    return TableExport(**values)


def messages_export(**changes):
    values = dict(
        table="messages", key=("id",), shard_hex=2, selector=internal_only,
        columns={"id": Integer(), "thread_id": Text(), "author_id": Text(), "body": Text(), "score": Real(nullable=True)})
    values.update(changes)
    return TableExport(**values)


def threads_export(**changes):
    values = dict(table="threads", key=("id",), columns={"id": Text(), "channel": Text(), "title": Text()},
                  selector=lambda uow, request: request.select(uow, '"threads"."channel" = ?', ("item",)))
    values.update(changes)
    return TableExport(**values)


def synthetic_catalog():
    classifications = (
        TableClassification("events", PRIVATE_EXCLUDED, "opaque event payloads can embed private channel data"),
        TableClassification("outbox", TECHNICAL_EXCLUDED, "delivery plumbing with raw provider arguments"),
        TableClassification("message_search", REPRESENTED, "derived terms of the exported messages", "messages"),
        TableClassification("legacy_versions", SEPARATE_HISTORY, "exact per-record versions", "qorqut-tables"))
    contributions = [
        HistoryContribution("identity", exports=(people_export(),)),
        HistoryContribution("discussion", exports=(threads_export(), messages_export()),
                            classifications=classifications)]
    return ExportCatalog(contributions)


def seed(database, *, reverse=False):
    people = [
        ("human-1", "human", "Owner", 1, SECRET, json.dumps({"theme": "dark", "notify": True, "labels": ["a", "b"],
                                                           "api_key": SECRET, "nested": {"password": SECRET}})),
        ("agent-1", "agent", "Assistant", 0, SECRET, None),
    ]
    messages = [
        (1, "t-item", "human-1", "ordinary discussion", 1.5, None),
        (2, "t-dm", "human-1", f"private message {SECRET}", None, None),
        (3, "t-item", "agent-1", "agent reply", None, json.dumps([{"path": SECRET}])),
    ]
    with database.transaction() as uow:
        uow.connection.executemany("INSERT INTO people(id, kind, display_name, active, credential_token, settings_json) "
                                   "VALUES (?,?,?,?,?,?)", reversed(people) if reverse else people)
        uow.connection.executemany("INSERT INTO threads VALUES (?,?,?)", [
            ("t-item", "item", "Item discussion"), ("t-dm", "direct", "Private channel")])
        uow.connection.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?)", reversed(messages) if reverse else messages)
        uow.connection.execute("INSERT INTO message_search VALUES (1, 'ordinary')")
        uow.connection.execute("INSERT INTO events(kind, payload_json) VALUES ('x', ?)", (json.dumps({"token": SECRET}),))
        uow.connection.execute("INSERT INTO outbox(destination, raw_arguments) VALUES ('d', ?)", (SECRET,))
        uow.connection.execute("INSERT INTO legacy_versions(body) VALUES ('v1')")
        uow.connection.execute("INSERT INTO scratch_cache(blob_text) VALUES (?)", (SECRET,))


def synthetic_database(tmp_path, *, reverse=False):
    extra = (StorageContribution("history_demo", SPECS, schema=SCHEMA),)
    database = database_at(tmp_path, extra=extra)
    seed(database, reverse=reverse)
    return database


@pytest.fixture
def database(tmp_path):
    database = synthetic_database(tmp_path)
    yield database
    database.close()


def export_of(database, catalog):
    with database.read() as uow:
        return catalog.export(uow)


def test_selective_closed_export_keeps_secrets_private_dms_and_nested_data_out(database):
    result = export_of(database, synthetic_catalog())

    assert set(result.tables) == {"people", "threads", "messages"}
    people = result.tables["people"].rows
    assert [row["id"] for row in people] == ["agent-1", "human-1"]
    owner = next(row for row in people if row["id"] == "human-1")
    assert owner["active"] is True and owner["revision"] == 1
    # Nested JSON is projected through its closed schema: the nested secret never survives.
    assert dict(owner["settings_json"]) == {"theme": "dark", "notify": True, "labels": ("a", "b")}
    assert next(row for row in people if row["id"] == "agent-1")["settings_json"] is None
    assert set(result.tables["people"].columns) == {"id", "kind", "display_name", "active", "revision", "settings_json"}

    # Filtered ordinary discussion only: the private direct-message row is not exported.
    assert [row["id"] for row in result.tables["messages"].rows] == [1, 3]
    assert [row["id"] for row in result.tables["threads"].rows] == ["t-item"]

    everything = canonical_bytes({name: [dict(row) for row in table.rows] for name, table in result.tables.items()}) \
        + result.manifest_bytes()
    assert SECRET.encode() not in everything and b"private message" not in everything
    assert b"credential_token" in everything  # recorded as an omitted column *name*, never its value
    manifest = json.loads(result.manifest_bytes())
    assert manifest["tables"]["people"]["omitted_columns"] == ["credential_token"]
    assert manifest["tables"]["messages"]["omitted_columns"] == ["attachments_json"]
    assert manifest["tables"]["messages"]["selector"] is True and manifest["tables"]["people"]["selector"] is False
    assert manifest["exclusions"]["events"] == {
        "classification": "private_excluded", "owner": "discussion",
        "reason": "opaque event payloads can embed private channel data"}
    assert manifest["exclusions"]["message_search"]["canonical"] == "messages"
    assert manifest["exclusions"]["legacy_versions"]["canonical"] == "qorqut-tables"
    assert manifest["tables"]["people"]["classification"] == EXPORTED


def test_unclassified_actual_tables_make_coverage_incomplete_and_are_never_selected(database):
    statements = []
    with database.read() as uow:
        uow.connection.set_trace_callback(statements.append)
        result = synthetic_catalog().export(uow)
        uow.connection.set_trace_callback(None)

    assert result.coverage_complete is False
    # Direct-SQL and engine-owned tables are found by reflection, in sorted order; classified tables are not missing.
    assert list(result.missing) == sorted(result.missing)
    assert {"scratch_cache", "target_identity", "change_sequences", "principals", "signoffs"} <= set(result.missing)
    assert "scratch_cache" in result.missing and "events" not in result.missing
    assert json.loads(result.manifest_bytes())["coverage_complete"] is False
    assert not any("scratch_cache" in statement for statement in statements)
    assert not any("select *" in statement.lower() for statement in statements)
    reads = [statement for statement in statements if statement.startswith("SELECT") and "sqlite_master" not in statement]
    assert reads and all('"' in statement for statement in reads)  # explicit quoted column lists only
    for table in ("events", "outbox", "legacy_versions", "message_search"):
        assert not any(table in statement for statement in statements)  # classified exclusions are not read either


def test_classifying_every_reported_table_completes_coverage_without_exporting_it(tmp_path):
    database = synthetic_database(tmp_path)
    with database.read() as uow:
        first = synthetic_catalog().export(uow)
        classified = ExportCatalog([
            *synthetic_catalog().contributions,
            HistoryContribution("rest", classifications=tuple(
                TableClassification(name, TECHNICAL_EXCLUDED, "synthetic: not part of this checkpoint")
                for name in first.missing))])
        complete = classified.export(uow)
    database.close()
    assert first.missing and first.coverage_complete is False
    assert complete.missing == () and complete.coverage_complete is True
    assert set(complete.tables) == set(first.tables)  # classification adds coverage, never rows
    assert json.loads(complete.manifest_bytes())["coverage_complete"] is True


def test_one_consistent_read_snapshot_across_tables(database):
    writer_done = threading.Event()

    def selecting_a(uow, request):
        rows = request.select(uow)
        if not writer_done.is_set():
            # A concurrent writer commits through a different connection while this unit is open.
            def write():
                with database.transaction() as other:
                    other.connection.execute("INSERT INTO threads VALUES ('t-late', 'item', 'Committed mid-export')")
                writer_done.set()
            thread = threading.Thread(target=write)
            thread.start()
            thread.join(10)
            assert writer_done.is_set()
        return rows

    catalog = ExportCatalog([
        HistoryContribution("first", exports=(TableExport("people", {"id": Text()}, ("id",), selector=selecting_a),)),
        HistoryContribution("second", exports=(TableExport("threads", {"id": Text(), "title": Text()}, ("id",)),))])
    with database.read() as uow:
        result = catalog.export(uow)
    assert [row["id"] for row in result.tables["threads"].rows] == ["t-dm", "t-item"]  # the later commit is not visible
    with database.read() as uow:
        assert "t-late" in [row["id"] for row in catalog.export(uow).tables["threads"].rows]


def test_export_is_deterministic_sorted_unsharded_and_shard_ids_are_fixed(tmp_path):
    first = synthetic_database(tmp_path / "a")
    second = synthetic_database(tmp_path / "b", reverse=True)
    one, two = export_of(first, synthetic_catalog()), export_of(second, synthetic_catalog())
    first.close(), second.close()

    assert one.manifest_bytes() == two.manifest_bytes()
    for name in one.tables:
        assert canonical_bytes([dict(row) for row in one.tables[name].rows]) == canonical_bytes(
            [dict(row) for row in two.tables[name].rows])
    assert [row["id"] for row in one.tables["messages"].rows] == sorted(row["id"] for row in one.tables["messages"].rows)
    assert not any(word in one.manifest_bytes().decode() for word in ("created_at", "updated_at", "2026"))

    table = one.tables["messages"]
    shards = [table.shard_of(row) for row in table.rows]
    assert all(len(shard) == 2 and int(shard, 16) >= 0 for shard in shards)
    assert shards == [shard_id((row["id"],), 2) for row in table.rows]
    assert shard_id(("abc", 7), 3) == hashlib.sha256(b'["abc",7]').hexdigest()[:3]
    assert shard_id(("abc",), 0) is None and one.tables["people"].shard_of(one.tables["people"].rows[0]) is None


@pytest.mark.parametrize("build", [
    lambda: TableExport("Bad", {"id": Text()}, ("id",)),
    lambda: TableExport("people", {"id": Text()}, ()),
    lambda: TableExport("people", {"id": Text()}, ("missing",)),
    lambda: TableExport("people", {"id": Real()}, ("id",)),
    lambda: TableExport("people", {"id": Text(), "settings": Obj(fields={"a": Text()})}, ("id",)),
    lambda: TableExport("people", {"id": Text()}, ("id",), structured={"settings": object()}),
    lambda: TableExport("people", {"id": Text()}, ("id", "id")),
    lambda: TableExport("people", {"id": Text()}, "id"),
    lambda: TableExport("people", {"id": Text()}, ("id",), selector="SELECT * FROM people"),
    lambda: TableExport("people", {"id": Text()}, ("id",), shard_hex=9),
    lambda: TableExport("people", {"id": Text()}, ("id",), structured={"id": StructuredField(SETTINGS, lambda raw: raw)}),
    lambda: TableExport("people", ["id"], ("id",)),
    lambda: StructuredField(SETTINGS, None),
    lambda: StructuredField(Text(), lambda raw: raw),
    lambda: TableClassification("events", EXPORTED, "exports are TableExport values"),
    lambda: TableClassification("events", PRIVATE_EXCLUDED, ""),
    lambda: TableClassification("events", PRIVATE_EXCLUDED, "reason", "extra-owner"),
    lambda: TableClassification("events", REPRESENTED, "reason"),
    lambda: TableClassification("events", SEPARATE_HISTORY, "reason"),
    lambda: TableClassification("Events", PRIVATE_EXCLUDED, "reason"),
    lambda: HistoryContribution("Bad Owner", classifications=(TableClassification("events", PRIVATE_EXCLUDED, "r"),)),
    lambda: HistoryContribution("empty"),
    lambda: HistoryContribution("dup", classifications=(TableClassification("events", PRIVATE_EXCLUDED, "r"),
                                                         TableClassification("events", TECHNICAL_EXCLUDED, "r"))),
    lambda: HistoryContribution("arbitrary", exports=({"table": "events"},)),
    lambda: Obj(fields={}),
    lambda: Obj(fields={"Bad": Text()}),
    lambda: Arr(item=Text(), max_items=0),
    lambda: Arr(item=Text(), max_items=10 ** 9),
    lambda: Text(max_length=0),
    lambda: Text(choices=(1,)),
    lambda: declared_json(Text()),
])
def test_invalid_contributions_and_declarations_fail_as_composition_errors(build):
    with pytest.raises(HistoryCompositionError):
        build()


def test_catalog_rejects_duplicate_owners_tables_untyped_contributions_and_dangling_representation():
    one = HistoryContribution("one", classifications=(TableClassification("events", PRIVATE_EXCLUDED, "r"),))
    with pytest.raises(HistoryCompositionError, match="owner defined twice"):
        ExportCatalog([one, HistoryContribution("one", classifications=(
            TableClassification("outbox", TECHNICAL_EXCLUDED, "r"),))])
    with pytest.raises(HistoryCompositionError, match="more than one contribution"):
        ExportCatalog([one, HistoryContribution("two", exports=(people_export(table="events", structured={},
                                                                              columns={"id": Text()}),))])
    with pytest.raises(HistoryCompositionError, match="HistoryContribution"):
        ExportCatalog([one, {"owner": "two"}])
    with pytest.raises(HistoryCompositionError, match="not an exported table"):
        ExportCatalog([HistoryContribution("two", classifications=(
            TableClassification("message_search", REPRESENTED, "r", "messages"),))])
    ExportCatalog([])  # an empty catalog is valid: everything is then reported missing, nothing exported


def test_classifying_a_missing_table_or_declaring_a_missing_column_is_a_composition_error(database):
    with database.read() as uow:
        with pytest.raises(HistoryCompositionError, match="do not exist"):
            ExportCatalog([HistoryContribution("x", classifications=(
                TableClassification("ghost", TECHNICAL_EXCLUDED, "gone"),))]).export(uow)
        with pytest.raises(HistoryCompositionError, match="columns the schema lacks"):
            ExportCatalog([HistoryContribution("x", exports=(
                TableExport("threads", {"id": Text(), "nickname": Text()}, ("id",)),))]).export(uow)


def test_export_requires_an_existing_read_only_unit_and_rejects_mutation(database):
    catalog = synthetic_catalog()
    with database.transaction() as uow:
        with pytest.raises(HistoryExportError) as error:
            catalog.export(uow)
    assert error.value.code == "history_export_read_only"
    with pytest.raises(HistoryExportError):
        catalog.export(object())

    def mutating(uow, request):
        uow.connection.execute("DELETE FROM people")
        return []

    hostile = ExportCatalog([HistoryContribution("hostile", exports=(
        TableExport("people", {"id": Text()}, ("id",), selector=mutating),))])
    with database.read() as uow:
        with pytest.raises(HistoryExportError) as failure:
            hostile.export(uow)
    assert failure.value.code == "history_export_failed" and failure.value.table == "people"
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM people").fetchone()[0] == 2


@pytest.mark.parametrize("rows, field_fragment", [
    ([{"id": "a", "extra": "x"}], "undeclared fields: extra"),
    ([{}], "lack declared fields: id"),
    ([{"id": {"nested": 1}}], "expected text (dict)"),
    ([{"id": ["a"]}], "expected text (list)"),
    ([{"id": b"bytes"}], "expected text (bytes)"),
    ([{"id": 5}], "expected text (int)"),
    ([{"id": None}], "null is not allowed"),
    ([{"id": "a"}, {"id": "a"}], "stable key is not unique"),
    (["not a mapping"], "not a mapping"),
])
def test_selector_output_is_validated_against_the_declared_closed_fields(database, rows, field_fragment):
    catalog = ExportCatalog([HistoryContribution("x", exports=(
        TableExport("threads", {"id": Text()}, ("id",), selector=lambda uow, request: rows),))])
    with database.read() as uow:
        with pytest.raises(HistoryExportError) as error:
            catalog.export(uow)
    assert error.value.code == "history_export_invalid" and error.value.table == "threads"
    assert field_fragment in error.value.message


def test_structured_projectors_cannot_return_undeclared_or_opaque_data_and_errors_carry_no_values(database):
    def export_with(project, raw_column="settings_json"):
        return ExportCatalog([HistoryContribution("x", exports=(
            TableExport("people", {"id": Text()}, ("id",), structured={raw_column: StructuredField(SETTINGS, project)}),))])

    leaking = lambda raw: {"theme": "dark", "notify": True, "labels": [], "api_key": SECRET}
    opaque = lambda raw: {"theme": "dark", "notify": {"deep": SECRET}, "labels": []}
    wrongtype = lambda raw: {"theme": "neon", "notify": True, "labels": []}
    toomany = lambda raw: {"theme": "dark", "notify": True, "labels": list("abcdef")}
    boom = lambda raw: (_ for _ in ()).throw(RuntimeError(SECRET))
    for project, fragment in ((leaking, "undeclared properties: api_key"), (opaque, "expected a boolean (dict)"),
                              (wrongtype, "declared choices"), (toomany, "declared bound"), (boom, "Projection failed")):
        with database.read() as uow:
            with pytest.raises(HistoryExportError) as error:
                export_with(project).export(uow)
        assert fragment in error.value.message and error.value.field == "settings_json"
        assert SECRET not in error.value.message and SECRET not in json.dumps(error.value.to_dict())

    # The helper drops undeclared nested data; invalid JSON text fails rather than leaking.
    with database.read() as uow:
        rows = export_with(declared_json(SETTINGS)).export(uow).tables["people"].rows
        assert [row["id"] for row in rows] == ["agent-1", "human-1"]
        assert dict(rows[1]["settings_json"])["theme"] == "dark" and "api_key" not in rows[1]["settings_json"]
    with database.transaction() as uow:
        uow.connection.execute("UPDATE people SET settings_json = '{not json' WHERE id = 'agent-1'")
    with database.read() as uow:
        with pytest.raises(HistoryExportError, match="Projection failed"):
            export_with(declared_json(SETTINGS)).export(uow)


def test_package_source_has_no_host_dependency():
    package = Path(__file__).resolve().parents[1] / "qorqut_operations" / "history_exports"
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py")))
    assert not any(word in text.lower() for word in ("bazarlyk", "qorqut_code", "qorqut_roles"))


@pytest.mark.parametrize("kind, value, expected", [
    (Boolean(), 1, True), (Boolean(), False, False), (Boolean(nullable=True), None, None),
    (Real(), 3, 3.0), (Real(), 2.5, 2.5), (Integer(), 7, 7), (Text(), "ünï", "ünï"),
    (Text(choices=("a", "b")), "a", "a"), (Integer(choices=(1, 2)), 2, 2), (Text(max_length=3), "abc", "abc"),
    (Arr(item=Integer(), max_items=2), (1, 2), [1, 2]),
    (Obj(fields={"a": Integer(), "b": Text(nullable=True)}), {"a": 1, "b": None}, {"a": 1, "b": None}),
])
def test_field_types_normalize_accepted_values(kind, value, expected):
    assert kind.check(value, "field") == expected


@pytest.mark.parametrize("kind, value, fragment", [
    (Boolean(), 2, "expected a boolean"), (Boolean(), "yes", "expected a boolean"), (Integer(), True, "expected integer"),
    (Integer(), 1.5, "expected integer"), (Integer(choices=(1,)), 2, "declared choices"), (Real(), float("nan"), "finite"),
    (Real(), float("inf"), "finite"), (Real(), True, "finite"), (Real(), "1.0", "finite"), (Text(), b"bytes", "expected text"),
    (Text(), "\ud800", "not valid Unicode"), (Text(max_length=2), "abc", "declared length"),
    (Text(), None, "null is not allowed"), (Arr(item=Integer(), max_items=1), [1, 2], "declared bound"),
    (Arr(item=Integer(), max_items=2), "12", "expected a list"), (Arr(item=Integer(), max_items=2), [1, "x"], "field[1]"),
    (Obj(fields={"a": Integer()}), {"a": 1, "b": 2}, "undeclared properties: b"),
    (Obj(fields={"a": Integer()}), {}, "missing properties: a"), (Obj(fields={"a": Integer()}), [1], "expected an object"),
    (Obj(fields={"a": Obj(fields={"b": Text()})}), {"a": {"b": 3}}, "field.a.b"),
])
def test_field_types_reject_anything_outside_the_closed_declaration(kind, value, fragment):
    with pytest.raises(FieldValueError) as error:
        kind.check(value, "field")
    assert fragment in str(error.value)


def test_nullable_business_key_preserves_global_scope_and_rejects_duplicates(database):
    with database.transaction() as uow:
        uow.connection.execute('CREATE TABLE scoped_history (business_id TEXT, id TEXT NOT NULL, body TEXT NOT NULL)')
        uow.connection.executemany('INSERT INTO scoped_history VALUES (?,?,?)',
                                  [('b', 'same-id', 'Business'), (None, 'same-id', 'Global')])
    catalog = ExportCatalog((HistoryContribution('scoped', exports=(TableExport(
        'scoped_history', {'business_id': Text(nullable=True), 'id': Text(), 'body': Text()},
        ('business_id', 'id'), shard_hex=2),)),))
    first = export_of(database, catalog).tables['scoped_history']
    assert [(r['business_id'], r['id']) for r in first.rows] == [(None, 'same-id'), ('b', 'same-id')]
    assert first.shard_of(first.rows[0]) == shard_id((None, 'same-id'), 2)
    assert export_of(database, catalog).tables['scoped_history'].rows == first.rows
    with database.transaction() as uow:
        uow.connection.execute('INSERT INTO scoped_history VALUES (NULL,?,?)', ('same-id', 'Duplicate'))
    with pytest.raises(HistoryExportError, match='not unique'):
        export_of(database, catalog)
