"""Exact document history at the Operations boundary: receipt versions and events, drift, lifecycle, record callbacks and the readers."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from qorqut_operations.application import OperationRegistry
from qorqut_operations.contracts import DomainError
from qorqut_operations.document_history import schema as history_schema
from qorqut_operations.document_history import text as history_text
from qorqut_operations.document_history.files import DocumentReceiptHistory
from qorqut_operations.document_history.lifecycle import DocumentLifecycle, RESOURCE_TYPE
from qorqut_operations.document_history.reads import DocumentHistory
from qorqut_operations.document_history.records import RecordHistory
from qorqut_operations.document_history.text import BASELINE_AUTHOR, EXTERNAL_AUTHOR, SqlDocuments, namespace, save_text
from qorqut_operations.documents import schema as documents_schema
from qorqut_operations.storage import (AssemblyBuilder, AssemblyError, FileEffectContribution, RecordEffectContribution,
                                       TransactionContribution)
from documents_support import BUSINESS, HUMAN, DocumentsHost, RecordHooks

SEQUENCE = "2026-10-25T10:00:00+00:00"


def create(host, body="Hello\n"):
    return host.ok("notes_create", {"title": "Plan", "body": body, "target_business_id": BUSINESS})["document"]


def update(host, note, body, **extra):
    return host.ok("notes_update", {"document_id": note["id"], "body": body, "target_business_id": BUSINESS, **extra},
                   expected_revision=note["revision"])["document"]


def versions(host, note, **args):
    return host.ok("document_history_get", {"source_kind": "document", "source_id": note["id"], **args})


def events(host, **args):
    return host.ok("business_history_list", args)["events"]


def test_a_receipt_records_one_version_and_one_event_with_its_receipt(tmp_path):
    host = DocumentsHost(tmp_path)
    note = create(host)
    page = versions(host, note)
    assert page["current_revision"] == 1 and [item["history_version"] for item in page["versions"]] == [1]
    assert page["versions"][0]["author_id"] == HUMAN and "notes_create" in page["versions"][0]["reason"]
    created = next(event for event in events(host) if event["kind"] == "document.created")
    receipt = page["versions"][0]["reason"].rsplit("receipt ", 1)[1]
    assert created["subject_id"] == note["id"] and created["actor_id"] == HUMAN
    assert created["data"]["history_version"] == 1 and created["data"]["receipt_id"] == receipt
    stored = versions(host, note, version=1)["version"]["content"]
    assert stored == host.file(note["id"]).read_text()


def test_an_edit_adds_a_version_a_diff_and_an_updated_event(tmp_path):
    host = DocumentsHost(tmp_path)
    note = create(host)
    update(host, note, "Changed\n")
    page = versions(host, note)
    assert [item["history_version"] for item in page["versions"]] == [2, 1] and page["current_revision"] == 2
    diff = versions(host, note, from_version=1, to_version=2)["diff"]
    assert "-Hello" in str(diff) and "+Changed" in str(diff)
    assert versions(host, note, version=2)["version"]["content"] == host.file(note["id"]).read_text()
    updated = [event for event in events(host) if event["kind"] == "document.updated"]
    assert len(updated) == 1 and updated[0]["data"]["history_version"] == 2 and updated[0]["data"]["content_changed"] is True
    older = versions(host, note, before_version=2, limit=1)
    assert [item["history_version"] for item in older["versions"]] == [1] and older["next_before_version"] is None
    assert versions(host, note, version=1)["version"]["content"] != versions(host, note, version=2)["version"]["content"]


def test_an_archive_and_a_global_document_are_recorded_in_their_own_namespaces(tmp_path):
    host = DocumentsHost(tmp_path)
    note = create(host)
    host.ok("notes_delete", {"document_id": note["id"], "target_business_id": BUSINESS}, expected_revision=1)
    assert [event["kind"] for event in events(host)][:2] == ["document.archived", "document.created"]
    entry = host.ok("notes_create", {"title": "Day", "body": "x\n"}, host.owner(None))["document"]
    shown = host.ok("document_history_get", {"source_kind": "document", "source_id": entry["id"]}, host.owner(None))
    assert [item["history_version"] for item in shown["versions"]] == [1]
    # Global activity is not invented: no business-scoped event exists for it.
    assert all(event["subject_id"] != entry["id"] for event in events(host))
    assert host.query("SELECT DISTINCT namespace FROM qbdv_document_versions ORDER BY 1") == [
        {"namespace": "business:" + BUSINESS}, {"namespace": "global"}]


def test_an_externally_changed_file_is_kept_as_labelled_drift_and_never_attributed_to_the_author(tmp_path):
    host = DocumentsHost(tmp_path)
    note = create(host)
    path = host.file(note["id"])
    outside = path.read_bytes() + b"edited outside\n"
    path.write_bytes(outside)
    import hashlib
    update(host, note, "After\n", expected_content_hash=hashlib.sha256(outside).hexdigest())
    page = versions(host, note)["versions"]
    assert [(item["history_version"], item["author_id"]) for item in page] == [(3, HUMAN), (2, EXTERNAL_AUTHOR), (1, HUMAN)]
    assert "edited outside" in versions(host, note, version=2)["version"]["content"]


def test_save_text_baselines_proven_before_content_and_only_labels_accepted_drift(tmp_path):
    host = DocumentsHost(tmp_path)
    with host.database.transaction() as uow:
        connection = uow.connection
        space, identity = namespace(BUSINESS), "sql:thing"
        first = save_text(connection, space, identity, "after\n", HUMAN, "edit", before="before\n")
        assert first.version == 2
        baseline = history_text.latest(connection, space, identity)
        assert baseline.content == "after\n"
        assert save_text(connection, space, identity, "after\n", HUMAN, "same").version == 2
        # The earlier version is the labelled baseline, not attributed to the editor.
        listing = history_text.store(connection).list_versions(space, identity, limit=5).versions
        assert [(item.version, item.author_id) for item in listing] == [(2, HUMAN), (1, BASELINE_AUTHOR)]
        # Differing proven content without a drift author is not recorded as anyone's edit.
        third = save_text(connection, space, identity, "next\n", HUMAN, "edit", before="elsewhere\n")
        assert third.version == 3
        assert [item.version for item in history_text.store(connection).list_versions(space, identity, limit=5).versions] == [3, 2, 1]
        fourth = save_text(connection, space, identity, "last\n", HUMAN, "edit", before="drifted\n", drift_author=EXTERNAL_AUTHOR,
                           drift_reason="accepted")
        assert fourth.version == 5
        assert history_text.store(connection).get_document(space, identity, version=4).author_id == EXTERNAL_AUTHOR
    assert namespace(None) == "global"


def test_the_document_lifecycle_moves_availability_and_reports_completed_documents(tmp_path):
    host = DocumentsHost(tmp_path)
    note = create(host)
    resource = SimpleNamespace(resource_type=RESOURCE_TYPE, resource_id=note["id"], business_id=BUSINESS)
    lifecycle = DocumentLifecycle()
    for method, expected in (("applying", "applying"), ("conflicted", "conflicted"), ("released", "ready"),
                             ("applying", "applying"), ("applied", "ready")):
        with host.database.transaction() as uow:
            getattr(lifecycle, method)(uow, resource)
            if method == "applied":
                assert (BUSINESS, note["id"]) in uow.completed_file_documents
        assert host.document(note["id"])["availability"] == expected
    assert RESOURCE_TYPE == "documents"


def test_a_composition_without_the_document_lifecycle_cannot_freeze():
    builder = (AssemblyBuilder(target_name="no-lifecycle", target_version=1)
               .add(documents_schema.storage_contribution()).add(history_schema.storage_contribution())
               .add(TransactionContribution("documents", (), None))
               .add(FileEffectContribution("documents", {}, DocumentReceiptHistory(byte_capture={})))
               .add(RecordEffectContribution("documents", RecordHooks())))
    with pytest.raises(AssemblyError, match="ifecycle"):
        builder.freeze()


def test_a_byte_capture_callback_records_its_kind_and_the_others_use_the_generic_history(tmp_path):
    calls = []

    def capture(connection, row, text, receipt_id):
        calls.append((row["kind"], text, receipt_id))
        return save_text(connection, namespace(row["business_id"]), row["id"], text, "capture:author", "captured by the owner of the kind")

    host = DocumentsHost(tmp_path, byte_capture={"context_note": capture})
    note = create(host)
    special = host.write("context_note", "context/a.md", "alpha\n", {"title": "A"})
    assert [(kind, receipt_id is not None) for kind, _, receipt_id in calls] == [("context_note", True)]
    assert calls[0][1] == host.file(special["id"]).read_text()
    assert versions(host, special)["versions"][0]["author_id"] == "capture:author"
    created = next(event for event in events(host) if event["subject_id"] == special["id"])
    assert created["data"]["history_version"] == 1 and created["data"]["receipt_id"] == calls[0][2]
    assert versions(host, note)["versions"][0]["author_id"] == HUMAN


def test_untracked_kinds_are_never_recorded(tmp_path):
    host = DocumentsHost(tmp_path)
    host.write("context_note", "context/a.md", "alpha\n", {"title": "A"})
    assert history_text.UNTRACKED_KINDS == {"asset", "data_upload", "email_message"}
    with host.database.transaction() as uow:
        assert history_text.append_event(uow.connection, None, "e", "k", HUMAN, "s", subject_type="t", subject_id="i", occurred_at=SEQUENCE) is None
        assert history_text.append_event(uow.connection, BUSINESS, "e", "k", HUMAN, "s", subject_type="t", subject_id="i", occurred_at="2026-10-25T10:00:00") is None


def test_a_composition_must_name_every_hook_explicitly(tmp_path):
    host = DocumentsHost(tmp_path)
    with pytest.raises(TypeError):
        DocumentReceiptHistory()
    with pytest.raises(TypeError):
        DocumentReceiptHistory(byte_capture={"context_note": "not callable"})
    with pytest.raises(TypeError):
        RecordHistory()
    with pytest.raises(TypeError, match="SqlDocuments"):
        RecordHistory(sql_documents={}, facts={})
    with pytest.raises(TypeError):
        RecordHistory(sql_documents=SqlDocuments({}), facts={"learning": "not callable"})
    with pytest.raises(ValueError, match="cannot be replaced"):
        RecordHistory(sql_documents=SqlDocuments({}), facts={"action_logs": lambda row, *, operation: None})
    with pytest.raises(TypeError):
        DocumentHistory(host.store)
    with pytest.raises(TypeError):
        DocumentHistory(host.store, source_resolvers={"document": lambda c, i, u: None}, private_kinds={})
    with pytest.raises(TypeError):
        DocumentHistory(host.store, source_resolvers={"learning": "not callable"}, private_kinds={})
    with pytest.raises(TypeError):
        DocumentHistory(host.store, source_resolvers={}, private_kinds={"working_memory": None})
    with pytest.raises(TypeError):
        SqlDocuments({"things": ("thing", "id", ())})


def plan(operation="host_change"):
    return SimpleNamespace(operation_name=operation, principal_id=HUMAN, initiated_by_principal_id=None, proposer_agent_id=None,
                           changes=(), preview={})


def test_record_history_tracks_the_union_of_documents_tasks_actions_sql_documents_and_host_facts(tmp_path):
    host = DocumentsHost(tmp_path)
    seen = []

    def fact(row, *, operation):
        seen.append((row["id"], operation))
        return {"business_id": row["business_id"], "event_id": "fact:" + row["id"], "kind": "fact.recorded", "actor_id": HUMAN,
                "summary": "A fact", "subject_type": "fact", "subject_id": row["id"], "occurred_at": SEQUENCE, "data": {}}

    history = RecordHistory(sql_documents=SqlDocuments({"things": ("thing", "id", ("name",))}), facts={"facts": fact})
    assert history.tracked_tables == {"things", "facts", "action_logs", "documents", "tasks"}
    thing = {"id": "t1", "business_id": BUSINESS, "name": "One", "revision": 1}
    with host.database.transaction() as uow:
        assert history.record_applied(uow, plan(), "unknown_table", None, {"id": "x", "business_id": BUSINESS}) is None
        assert history.record_applied(uow, plan(), "things", None, thing).version == 1
        assert history.record_applied(uow, plan(), "things", thing, {**thing, "name": "Two", "revision": 2}).version == 2
        history.record_applied(uow, plan(), "things", {**thing, "name": "Two", "revision": 2}, None)
        history.record_applied(uow, plan("record_fact"), "facts", None, {"id": "f1", "business_id": BUSINESS})
        # An already-recorded fact row is not recorded again, and a fact table without a callback is not a fact.
        history.record_applied(uow, plan("record_fact"), "facts", {"id": "f1", "business_id": BUSINESS}, {"id": "f1", "business_id": BUSINESS})
        history.record_applied(uow, plan("record_action"), "action_logs", None, {
            "id": "a1", "business_id": BUSINESS, "recorded_by": HUMAN, "action_text": "Called\nthe client", "occurred_at": SEQUENCE,
            "action_key": "call", "refs_json": '{"k": "v"}'})
        for status, expected in (("done", "task.completed"), ("open", "task.reopened")):
            before = {"status": "open" if status == "done" else "done"}
            history.record_applied(uow, plan("task_move"), "tasks", before, {
                "id": "k1", "business_id": BUSINESS, "status": status, "revision": 3 if status == "done" else 4, "updated_at": SEQUENCE})
        history.record_applied(uow, plan("task_move"), "tasks", {"status": "open"}, {
            "id": "k1", "business_id": BUSINESS, "status": "open", "revision": 5, "updated_at": SEQUENCE})
    assert seen == [("f1", "record_fact")]
    kinds = [(event["kind"], event["subject_id"]) for event in events(host)]
    assert ("thing.deleted", "t1") in kinds and ("fact.recorded", "f1") in kinds
    assert ("action.recorded", "a1") in kinds and ("task.completed", "k1") in kinds and ("task.reopened", "k1") in kinds
    assert [kind for kind, _ in kinds].count("task.completed") == 1
    action = next(event for event in events(host) if event["kind"] == "action.recorded")
    assert action["summary"] == "Called the client" and action["data"]["refs"] == {"k": "v"} and action["data"]["operation"] == "record_action"
    assert [item.version for item in _list(host, BUSINESS, "thing:t1")] == [2, 1]


def _list(host, business, identity):
    with host.database.read() as uow:
        return history_text.store(uow.connection).list_versions(namespace(business), identity, limit=10).versions


def test_the_readers_are_owner_only_and_choose_exactly_one_selection(tmp_path):
    host = DocumentsHost(tmp_path)
    note = create(host)
    arguments = {"source_kind": "document", "source_id": note["id"]}
    assert host.refused("document_history_get", arguments, host.stranger())["code"] == "forbidden"
    assert host.refused("business_history_list", {}, host.stranger())["code"] == "forbidden"
    # The handlers keep their own owner check behind the engine's owner-only descriptor.
    with host.database.read() as uow:
        with pytest.raises(DomainError, match="Only the installation owner can read document history"):
            host.history.owner(host.stranger(), uow)
        host.history.owner(host.owner(), uow)
    assert host.refused("document_history_get", {**arguments, "version": 1, "limit": 5})["code"] == "validation_error"
    assert host.refused("document_history_get", {**arguments, "from_version": 1})["code"] == "validation_error"
    assert host.refused("document_history_get", {**arguments, "version": 9})["code"] == "not_found"
    assert host.refused("document_history_get", {**arguments, "source_id": "00000000-0000-4000-8000-0000000000ee"})["code"] == "not_found"
    assert host.refused("document_history_get", {"source_kind": "learning_candidate", "source_id": "x"})["code"] == "validation_error"
    assert host.refused("business_history_list", {"source": "learning"})["code"] == "validation_error"
    for page in (host.ok("business_history_list", {"limit": 1}),):
        assert len(page["events"]) == 1 and page["source"] == "core" and page["next_before_sequence"] is None


def test_a_private_kind_names_its_own_reader_and_host_sources_resolve_through_their_owner(tmp_path):
    host = DocumentsHost(tmp_path)
    note = create(host)
    opened = []

    def resolver(ctx, source_id, uow):
        opened.append(source_id)
        return namespace(BUSINESS), "learning:" + source_id, 7

    history = DocumentHistory(host.store, source_resolvers={"learning_decision": resolver, "learning_candidate": resolver},
                              private_kinds={"context_note": "Notes are read with notes_get"})
    registry = OperationRegistry()
    history.register(registry)
    descriptors = registry._descriptors
    assert history.source_kinds == ("document", "learning_decision", "learning_candidate")
    assert tuple(descriptors["document_history_get"].input_schema["properties"]["source_kind"]["enum"]) == history.source_kinds
    assert descriptors["document_history_get"].owner_only and descriptors["business_history_list"].owner_only
    with host.database.transaction() as uow:
        saved = save_text(uow.connection, namespace(BUSINESS), "learning:L1", "decided\n", HUMAN, "decision")
        assert saved.version == 1
    with host.database.read() as uow:
        got = history.get(host.owner(), {"source_kind": "learning_decision", "source_id": "L1", "version": 1}, None, uow)
        assert got["current_revision"] == 7 and got["version"]["content"] == "decided\n" and opened == ["L1"]
        assert history.get(host.owner(), {"source_kind": "document", "source_id": note["id"]}, None, uow)["current_revision"] == 1
    note_two = host.write("context_note", "context/a.md", "alpha\n", {"title": "A"})
    with host.database.read() as uow:
        with pytest.raises(DomainError, match="Notes are read with notes_get"):
            history.get(host.owner(), {"source_kind": "document", "source_id": note_two["id"]}, None, uow)


def test_the_history_schema_is_the_shared_library_and_a_missing_table_is_refused(tmp_path):
    host = DocumentsHost(tmp_path)
    assert set(history_schema.HISTORY_TABLES) == {"qbdv_document_versions", "qbdv_business_events"}
    with host.database.read() as uow:
        history_schema.validate_schema(uow.connection)
    assert history_text.HISTORY_TABLES is history_schema.HISTORY_TABLES
    contribution = history_schema.storage_contribution()
    assert contribution.owner == "document_history" and contribution.validators == (history_schema.validate_schema,)


PACKAGES = ("documents", "document_history")


def test_the_owner_packages_import_no_host_or_context_and_nothing_inside_a_function():
    root = Path(__file__).resolve().parents[1] / "qorqut_operations"
    forbidden = ("bazarlyk", "qorqut_code", "qorqut_roles", "qorqut_context", "context")
    for package in PACKAGES:
        for path in sorted((root / package).glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    assert not any(name.split(".")[0] in forbidden for name in names), f"{path.name}: {names}"
            for function in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                inner = [n for n in ast.walk(function) if isinstance(n, (ast.Import, ast.ImportFrom))]
                assert not inner, f"{path.name}:{function.name} imports inside a function"
            assert "__getattr__" not in path.read_text(), path.name
