"""Shared document history and business activity captured by the core."""

import hashlib
import json

import pytest

from dataclasses import replace

from qorqut_business_docs_versioning import HistoryStore

from qorqut_operations.contracts import InvocationContext, Principal, new_id, plain
from bazarlyk.maintenance import history_baseline
from bazarlyk.storage import history
from qorqut_operations.document_history.text import BASELINE_AUTHOR, EXTERNAL_AUTHOR
from foundation_support import AGENT, BUSINESS, HUMAN, OTHER, context, insert_action
from test_domain_documents import invoke, setup_documents
from test_environment_discovery import service  # noqa: F401
from test_working_memory import invoke as wm_invoke, ok as wm_ok, save as wm_save


def install_owner(db):
    with db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": HUMAN})


def installed(tmp_path):
    db, app, domains = setup_documents(tmp_path)
    install_owner(db)
    return db, app, domains


def versions(db, space, document_id):
    with db.read() as uow:
        return HistoryStore(uow.connection).list_versions(space, document_id, limit=200).versions


def events(db, business_id=BUSINESS):
    with db.read() as uow:
        return HistoryStore(uow.connection).list_events(business_id, limit=200).events


NOTE = {"target_business_id": BUSINESS}


def note(app, title, body):
    return ok(invoke(app, "notes_create", {"title": title, "body": body, **NOTE}))["document"]


def ok(result):
    assert result.status == "ok", result.to_dict()
    return result.data


def test_note_edits_are_versioned_with_activity_and_external_drift_is_labelled(tmp_path):
    db, app, _ = installed(tmp_path)
    doc = note(app, "Plan", "One")
    ok(invoke(app, "notes_update", {"document_id": doc["id"], "body": "Two", "target_business_id": BUSINESS}, revision=1))
    (tmp_path / "first" / "hq" / doc["path"]).write_text("Edited outside")
    observed = ok(app.invoke(context(), "notes_get", {"document_id": doc["id"]}))["document"]
    ok(invoke(app, "notes_update", {"document_id": doc["id"], "body": "Three",
                                     "expected_content_hash": observed["content_hash"], "target_business_id": BUSINESS}, revision=observed["revision"]))
    saved = versions(db, "business:" + BUSINESS, doc["id"])
    assert [v.author_id for v in reversed(saved)] == [HUMAN, HUMAN, EXTERNAL_AUTHOR, HUMAN]
    with db.read() as uow:
        store = HistoryStore(uow.connection)
        assert store.get_document("business:" + BUSINESS, doc["id"], version=3).content == "Edited outside"
        assert store.get_document("business:" + BUSINESS, doc["id"]).content.endswith("\nThree")
    kinds = [(e.kind, e.subject_id) for e in reversed(events(db))]
    assert kinds[0] == ("document.created", doc["id"])
    assert [k for k, _ in kinds].count("document.updated") >= 2
    assert all(e.data.get("history_version") for e in events(db) if e.kind == "document.updated")
    assert not events(db, OTHER)


def test_note_archive_records_lifecycle_and_keeps_history(tmp_path):
    db, app, _ = installed(tmp_path)
    doc = note(app, "Old", "Keep me")
    ok(invoke(app, "notes_delete", {"document_id": doc["id"], "target_business_id": BUSINESS}, revision=1))
    assert "document.archived" in [e.kind for e in events(db)]
    assert len(versions(db, "business:" + BUSINESS, doc["id"])) == 1


def test_global_note_is_versioned_without_business_activity(tmp_path):
    db, app, _ = installed(tmp_path)
    owner = replace(context(), business_id=None, global_capabilities=frozenset({"read_global_context"}))
    doc = ok(invoke(app, "notes_create", {"title": "Global", "body": "G"}, ctx=owner))["document"]
    assert len(versions(db, "global", doc["id"])) == 1
    assert not events(db) and not events(db, OTHER)
    read = ok(app.invoke(owner, "document_history_get", {"source_kind": "document", "source_id": doc["id"], "version": 1}))
    assert read["version"]["content"].endswith("\nG") and read["current_revision"] == 1


def test_history_reads_are_owner_only_scoped_and_paged(tmp_path):
    db, app, _ = installed(tmp_path)
    doc = note(app, "Paged", "v1")
    for number in range(2, 5):
        ok(invoke(app, "notes_update", {"document_id": doc["id"], "body": f"v{number}", "target_business_id": BUSINESS}, revision=number - 1))
    args = {"source_kind": "document", "source_id": doc["id"]}
    page = ok(app.invoke(context(), "document_history_get", {**args, "limit": 2}))
    assert [v["history_version"] for v in page["versions"]] == [4, 3] and page["next_before_version"] == 3
    assert "content" not in page["versions"][0] and page["current_revision"] == 4
    older = ok(app.invoke(context(), "document_history_get", {**args, "before_version": 3}))
    assert [v["history_version"] for v in older["versions"]] == [2, 1]
    diff = ok(app.invoke(context(), "document_history_get", {**args, "from_version": 1, "to_version": 4}))
    assert "-v1" in diff["diff"] and "+v4" in diff["diff"]
    both = app.invoke(context(), "document_history_get", {**args, "version": 1, "limit": 2})
    assert both.error["code"] == "validation_error"
    half = app.invoke(context(), "document_history_get", {**args, "from_version": 1})
    assert half.error["code"] == "validation_error"
    missing = app.invoke(context(), "document_history_get", {**args, "version": 9})
    assert missing.error["code"] == "not_found"
    activity = ok(app.invoke(context(), "business_history_list", {"limit": 3}))
    assert len(activity["events"]) == 3 and activity["next_before_sequence"]
    assert activity["events"][0]["sequence"] > activity["events"][-1]["sequence"]
    # Another business's scope cannot reach this document.
    other = InvocationContext(Principal(HUMAN, "human"), business_id=OTHER, allowed_business_ids=frozenset({OTHER}),
                              source="dashboard")
    assert app.invoke(other, "document_history_get", args).status != "ok"
    # A business owner who is not the installation owner is refused.
    with db.transaction() as uow:
        uow.repository("principals").insert({"id": AGENT, "kind": "human", "display_name": "Partner"})
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": AGENT, "is_owner": True})
    partner = InvocationContext(Principal(AGENT, "human"), business_id=BUSINESS,
                                allowed_business_ids=frozenset({BUSINESS}), source="dashboard")
    assert app.invoke(partner, "document_history_get", args).error["code"] == "forbidden"
    assert app.invoke(partner, "business_history_list", {}).error["code"] == "forbidden"
    before = (len(versions(db, "business:" + BUSINESS, doc["id"])), len(events(db)))
    ok(app.invoke(context(), "business_history_list", {}))
    assert (len(versions(db, "business:" + BUSINESS, doc["id"])), len(events(db))) == before


def test_working_memory_history_is_not_served_generically(service):
    s = service
    for body in ("First memory", "Second memory"):
        previous = wm_ok(wm_invoke(s, "working_memory_get"))["document"]
        wm_ok(wm_save(s, body, previous))
    doc = wm_ok(wm_invoke(s, "working_memory_get"))["document"]
    ctx = s.fixture.ctx
    with s.fixture.db.transaction() as uow:
        if uow.repository("installation").get("service") is None:
            uow.repository("installation").insert({"id": "service", "installation_id": new_id(),
                                                   "owner_principal_id": s.fixture.owner.id})
    result = s.runtime.application.invoke(ctx, "document_history_get", {"source_kind": "document", "source_id": doc["id"]})
    assert result.error["code"] == "forbidden"
    with s.fixture.db.read() as uow:
        assert history.working_memory_version(uow.connection, doc["business_id"], doc["id"])["version"] == 2
        assert history.verify_working_memory(uow.connection) == []
        facts = HistoryStore(uow.connection).list_events(s.fixture.business_id, limit=200).events
    assert "Second memory" not in repr(facts)


def test_task_completion_and_reopen_follow_configured_closed_states(tmp_path):
    db, app, _ = installed(tmp_path)
    task = ok(invoke(app, "tasks_create", {"title": "Ship"}))["task"]
    ok(invoke(app, "tasks_update", {"task_id": task["id"], "changes": {"status": "doing"}}, revision=1))
    ok(invoke(app, "tasks_complete", {"task_id": task["id"], "status": "done"}, revision=2))
    ok(invoke(app, "tasks_update", {"task_id": task["id"], "changes": {"status": "open"}}, revision=3))
    kinds = [e.kind for e in reversed(events(db)) if e.subject_type == "task"]
    assert kinds == ["task.completed", "task.reopened"]
    completed = next(e for e in events(db) if e.kind == "task.completed")
    assert completed.actor_id == HUMAN and completed.data["status"] == "done"


def test_action_logs_are_business_activity_once(tmp_path):
    db, app, _ = installed(tmp_path)
    ok(invoke(app, "logs_add", {"summary": "Called the supplier", "tag": "operations"}, key="log-1"))
    ok(invoke(app, "logs_add", {"summary": "Called the supplier", "tag": "operations"}, key="log-1"))
    actions = [e for e in events(db) if e.kind == "action.recorded"]
    assert len(actions) == 1 and actions[0].summary == "Called the supplier"
    with db.transaction() as uow:
        assert history.import_facts(uow.connection, apply=True)["imported"] == 0


def test_custom_closed_states_decide_completion(tmp_path):
    db, app, _ = installed(tmp_path)
    config = plain(ok(app.invoke(context(), "tasks_config_get")))["config"]
    config["states"] = [{"name": "todo", "kind": "open", "default": True}, {"name": "shipped", "kind": "closed"},
                        {"name": "done", "kind": "open"}]
    ok(invoke(app, "tasks_config_update", {"config": config}))
    task = ok(invoke(app, "tasks_create", {"title": "Custom"}))["task"]
    ok(invoke(app, "tasks_update", {"task_id": task["id"], "changes": {"status": "done"}}, revision=1))
    assert not [e for e in events(db) if e.subject_type == "task"]
    ok(invoke(app, "tasks_update", {"task_id": task["id"], "changes": {"status": "shipped"}}, revision=2))
    assert [e.kind for e in events(db) if e.subject_type == "task"] == ["task.completed"]


def test_stories_and_drafts_are_versioned_sql_documents(tmp_path):
    # Drafts are saved before an installation owner exists (the owner channel check does not apply).
    db, app, _ = setup_documents(tmp_path)
    story = ok(invoke(app, "stories_create", {"title": "Launch", "purpose": "Ship it"}))["story"]
    ok(invoke(app, "stories_update", {"story_id": story["id"], "changes": {"next_step": "Email press"}}, revision=1))
    parent = ok(app.invoke(context(), "stories_timeline", {"story_id": story["id"]}))["events"][0]["id"]
    draft_id = new_id()
    ok(invoke(app, "stories_draft_save", {"story_id": story["id"], "draft_id": draft_id, "parent_event_id": parent,
                                           "body": "Draft one"}))
    ok(invoke(app, "stories_draft_save", {"story_id": story["id"], "draft_id": draft_id, "parent_event_id": parent,
                                           "body": "Draft two"}, revision=1))
    ok(invoke(app, "stories_draft_discard", {"draft_id": draft_id}, revision=2))
    install_owner(db)
    args = {"source_kind": "story", "source_id": story["id"]}
    page = ok(app.invoke(context(), "document_history_get", args))
    assert [v["history_version"] for v in page["versions"]] == [2, 1] and page["current_revision"] == 2
    latest = ok(app.invoke(context(), "document_history_get", {**args, "version": 2}))["version"]["content"]
    assert "Email press" in latest
    drafts = ok(app.invoke(context(), "document_history_get", {"source_kind": "story_draft", "source_id": draft_id}))
    assert len(drafts["versions"]) == 2 and drafts["current_revision"] is None
    assert "story_draft.deleted" in [e.kind for e in events(db)]
    other = InvocationContext(Principal(HUMAN, "human"), business_id=OTHER, allowed_business_ids=frozenset({OTHER}),
                              source="dashboard")
    assert app.invoke(other, "document_history_get", args).error["code"] == "not_found"
    assert app.invoke(other, "document_history_get", {"source_kind": "story_draft", "source_id": draft_id}
                      ).error["code"] == "not_found"


def legacy_document(db, tmp_path, template, name, data, **overrides):
    """A registered document that predates shared history (no version recorded)."""
    row = {**template, "id": new_id(), "path": f"notes/strategy-notes/{name}.md",
           "content_hash": "sha256:" + hashlib.sha256(data).hexdigest(), "revision": 3, **overrides}
    (tmp_path / "first" / "hq" / row["path"]).write_bytes(data)
    with db.transaction() as uow:
        columns = ", ".join(row)
        uow.connection.execute(f"INSERT INTO documents({columns}) VALUES ({', '.join('?' * len(row))})",
                               [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()])
    return row


def test_history_baseline_preview_apply_and_repeat(tmp_path):
    db, app, _ = installed(tmp_path)
    tracked = note(app, "Tracked", "Already versioned")
    with db.read() as uow:
        template = dict(uow.connection.execute("SELECT * FROM documents WHERE id=?", (tracked["id"],)).fetchone())
    ready = legacy_document(db, tmp_path, template, "ready", b"Old text")
    drifted = legacy_document(db, tmp_path, template, "drifted", b"Registered")
    (tmp_path / "first" / "hq" / drifted["path"]).write_text("Changed on disk")
    missing = legacy_document(db, tmp_path, template, "missing", b"Gone")
    (tmp_path / "first" / "hq" / missing["path"]).unlink()
    busy = legacy_document(db, tmp_path, template, "busy", b"Applying", availability="applying")
    binary = legacy_document(db, tmp_path, template, "binary", b"\xff\xfe\x00")
    asset = legacy_document(db, tmp_path, template, "asset", b"image", kind="asset")
    with db.transaction() as uow:
        for identity, occurred in (("aware", "2026-09-01T10:00:00+00:00"), ("naive", "2026-09-01T10:00:00")):
            insert_action(uow, BUSINESS, identity, tag="operations", summary="Old action", actor_id=HUMAN, occurred_at=occurred)
    before = events(db)
    preview = history_baseline.run(db, tmp_path)
    assert preview["applied"] is False
    documents = preview["documents"]
    assert (documents["tracked"], documents["ready"], documents["baselined"], documents["untracked_kind"]) == (1, 1, 0, 1)
    for status, row in (("drifted", drifted), ("missing", missing), ("busy", busy), ("not_text", binary)):
        assert [item["document_id"] for item in documents[status]["items"]] == [row["id"]], status
    assert preview["activity"]["imported"] == 1 and preview["activity"]["missing_evidence"] == 1
    assert versions(db, "business:" + BUSINESS, ready["id"]) == () and events(db) == before

    applied = history_baseline.run(db, tmp_path, apply=True)
    assert applied["documents"]["baselined"] == 1 and applied["activity"]["imported"] == 1
    saved = versions(db, "business:" + BUSINESS, ready["id"])
    assert len(saved) == 1 and saved[0].author_id == BASELINE_AUTHOR and "revision 3" in saved[0].reason
    for row in (drifted, missing, busy, binary, asset):
        assert versions(db, "business:" + BUSINESS, row["id"]) == ()
    assert [e.event_id for e in events(db) if e.kind == "action.recorded"] == ["action:aware"]

    again = history_baseline.run(db, tmp_path, apply=True)
    assert again["documents"]["baselined"] == 0 and again["documents"]["tracked"] == 2
    assert again["activity"]["imported"] == 0 and again["activity"]["present"] == 1
    assert again["sql_documents"] == {"baselined": 0} and again["working_memory"]["count"] == 0
    assert len(versions(db, "business:" + BUSINESS, ready["id"])) == 1


def test_history_baseline_cli_previews_by_default(tmp_path, capsys):
    db, app, _ = installed(tmp_path)
    (tmp_path / ".bazarlyk").mkdir()
    database = tmp_path / ".bazarlyk" / "state.sqlite3"
    database.write_bytes((tmp_path / "state.sqlite3").read_bytes())
    assert history_baseline.main(["--root", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["applied"] is False and report["documents"]["ready"] == 0


class Interrupted(BaseException):
    pass


def crash(resource):
    raise Interrupted()


def test_interrupted_note_edit_recovers_one_version_and_one_event(tmp_path, monkeypatch):
    db, app, _ = installed(tmp_path)
    doc = note(app, "Recover", "Before")
    applier = app.file_executor.file_applier
    monkeypatch.setattr(applier, "after_replace", crash)
    args = {"document_id": doc["id"], "body": "After", "target_business_id": BUSINESS}
    with pytest.raises(Interrupted):
        invoke(app, "notes_update", args, revision=1, key="recover")
    assert len(versions(db, "business:" + BUSINESS, doc["id"])) == 1
    monkeypatch.setattr(applier, "after_replace", None)
    ok(invoke(app, "notes_update", args, revision=1, key="recover"))
    ok(invoke(app, "notes_update", args, revision=1, key="recover"))
    saved = versions(db, "business:" + BUSINESS, doc["id"])
    assert len(saved) == 2 and saved[0].author_id == HUMAN
    assert [e.kind for e in events(db)].count("document.updated") == 1


def test_legacy_context_families_and_moves_keep_one_chain(tmp_path):
    db, app, _ = installed(tmp_path)
    topic = ok(invoke(app, "context_topics_create", {"title": "Customers", "slug": "customers"}))["document"]
    person = ok(invoke(app, "context_people_create", {"name": "Ada", "notes": "Met at the fair"}))["document"]
    material = ok(invoke(app, "context_notes_create", {"topic_id": topic["id"], "title": "Interview",
                                                       "body": "Verbatim", "people_ids": [person["id"]]}))["document"]
    space = "business:" + BUSINESS
    for row in (topic, person, material):
        assert len(versions(db, space, row["id"])) == 1, row["kind"]
    moved = ok(invoke(app, "context_topics_move", {"document_id": topic["id"], "slug": "clients"},
                      revision=topic["revision"]))["document"]
    assert moved["id"] == topic["id"] and moved["path"] != topic["path"]
    assert "document.moved" in [e.kind for e in events(db) if e.subject_id == topic["id"]]
    assert 1 <= len(versions(db, space, topic["id"])) <= 2


def test_task_activity_initial_closed_retry_and_business_separation(tmp_path):
    db, app, _ = installed(tmp_path)
    ok(invoke(app, "tasks_create", {"title": "Already done", "status": "done"}))
    task = ok(invoke(app, "tasks_create", {"title": "Twice"}))["task"]
    for _ in range(2):
        ok(invoke(app, "tasks_complete", {"task_id": task["id"], "status": "done"}, revision=1, key="complete-once"))
    ok(invoke(app, "tasks_update", {"task_id": task["id"], "changes": {"status": "dropped"}}, revision=2))
    ok(invoke(app, "tasks_update", {"task_id": task["id"], "changes": {"status": "open"}}, revision=3))
    ok(invoke(app, "tasks_complete", {"task_id": task["id"], "status": "done"}, revision=4))
    kinds = [(e.kind, e.subject_id == task["id"]) for e in reversed(events(db)) if e.subject_type == "task"]
    assert kinds == [("task.completed", False), ("task.completed", True), ("task.reopened", True), ("task.completed", True)]
    assert not events(db, OTHER)


def test_failed_source_change_rolls_back_its_history(tmp_path, monkeypatch):
    db, app, _ = installed(tmp_path)
    story = ok(invoke(app, "stories_create", {"title": "Atomic", "purpose": "Roll back together"}))["story"]
    original = history.record_applied

    def failing(uow, plan, table, before, after, **kw):
        result = original(uow, plan, table, before, after, **kw)
        if table == "stories":
            raise RuntimeError("history failure after capture")
        return result
    monkeypatch.setattr(history, "record_applied", failing)
    with pytest.raises(RuntimeError):
        invoke(app, "stories_update", {"story_id": story["id"], "changes": {"next_step": "Never saved"}}, revision=1)
    monkeypatch.setattr(history, "record_applied", original)
    assert ok(app.invoke(context(), "stories_get", {"story_id": story["id"]}))["story"]["revision"] == 1
    assert len(versions(db, "business:" + BUSINESS, "story:" + story["id"])) == 1


def test_fields_outside_the_projection_create_no_version(tmp_path):
    db, app, _ = installed(tmp_path)
    story = ok(invoke(app, "stories_create", {"title": "Quiet", "purpose": "Tags only"}))["story"]
    ok(invoke(app, "stories_update", {"story_id": story["id"], "changes": {"details": "Longer notes"}}, revision=1))
    ok(invoke(app, "stories_update", {"story_id": story["id"], "changes": {"archived": True}}, revision=2))
    assert len(versions(db, "business:" + BUSINESS, "story:" + story["id"])) == 1


def test_history_stays_out_of_public_exports(tmp_path):
    from bazarlyk.storage.exports import build_exports
    db, app, _ = installed(tmp_path)
    doc = note(app, "Private", "SUPERSEDED-PRIVATE-TEXT")
    ok(invoke(app, "notes_update", {"document_id": doc["id"], "body": "Current", "target_business_id": BUSINESS},
              revision=1))
    bundle = build_exports(db.path, tmp_path)
    assert not [name for name in bundle.files if "qbdv" in name or "provenance" in name]
    assert not [name for name, body in bundle.files.items() if b"SUPERSEDED-PRIVATE-TEXT" in body]


def test_editable_uploads_maps_and_pillars_are_versioned_through_the_central_path(tmp_path):
    from qorqut_operations.background.authority import BackgroundAuthority
    from qorqut_operations.background.programs import Program, ProgramRegistry
    from qorqut_operations.contracts import canonical_hash
    from bazarlyk.domains.common import schema
    from bazarlyk.policy import PolicyResolver
    db, app, domains = installed(tmp_path)
    programs = ProgramRegistry()
    programs.register(Program("context.file", 1, schema({"upload_id": {"type": "string"},
        "expected_revision": {"type": "integer"}}, ("upload_id", "expected_revision")), lambda *_: None,
        permissions={"context_map_apply": "allow"}, outputs=("context_map_apply",),
        resource_types=("documents", "managed_document_files", "document_index_sources")))
    authority = BackgroundAuthority(db, programs)
    with db.transaction() as uow:
        authority.register_workers(uow, [BUSINESS])
    domains.documents.programs = programs
    policy = PolicyResolver(app.registry, worker_profiles=authority.worker_profiles,
        job_consequence_verifier=authority.verify_consequence, job_plan_validator=authority.validate_plan)
    app.policy, app.context_resolver = policy, policy.refresh_context
    space = "business:" + BUSINESS
    submitted = invoke(app, "context_upload", {"title": "Call", "body": "Original transcript."})
    assert submitted.status == "accepted", submitted.to_dict()
    upload, job_id = submitted.data["document"], submitted.data["job"]["id"]
    # The owner corrects the upload through its editable native path.
    ok(invoke(app, "context_document_update", {"document_id": upload["id"], "body": "Corrected transcript.",
                                               "expected_content_hash": upload["content_hash"]}, revision=1))
    chain = versions(db, space, upload["id"])
    assert [v.author_id for v in chain] == [HUMAN, HUMAN]
    with db.read() as uow:
        assert HistoryStore(uow.connection).get_document(space, upload["id"]).content.endswith("Corrected transcript.")
    # An accepted model-generated map is saved by the worker, not attributed to the owner.
    arguments = {"upload_id": upload["id"], "title": "Call map", "outline": [{"text": "Pricing", "children": []}]}
    outputs = [{"operation": "context_map_apply", "arguments": arguments, "options": {"expected_revision": 2}}]
    with db.transaction() as uow:
        jobs = uow.repository("jobs", business_id=BUSINESS)
        job = jobs.get(job_id)
        jobs.update(job_id, {"state": "applying", "result_json": {"outputs": outputs, "outputs_hash": canonical_hash(outputs)}},
                    expected_revision=job["revision"])
    with db.read() as uow:
        worker = policy.context_for(job["principal_id"], business_id=BUSINESS, review_business_id=BUSINESS, uow=uow,
            initiated_by_principal_id=job["initiated_by_principal_id"], job_id=job_id)
    ok(invoke(app, "context_map_apply", arguments, revision=2, ctx=worker))
    with db.read() as uow:
        mapped = uow.connection.execute("SELECT id FROM documents WHERE kind='context_map'").fetchone()[0]
    saved = versions(db, space, mapped)
    assert len(saved) == 1 and saved[0].author_id == job["principal_id"]
    assert "document.created" in [e.kind for e in events(db) if e.subject_id == mapped]
    # A legacy pillar edited for the first time keeps its proven earlier text as a labelled baseline.
    tracked = note(app, "Template", "x")
    with db.read() as uow:
        template = dict(uow.connection.execute("SELECT * FROM documents WHERE id=?", (tracked["id"],)).fetchone())
    pillar = legacy_document(db, tmp_path, template, "pillar", b"# Product\n\nOld pillar\n", kind="pillar",
                             path="context/product.md", metadata_json={})
    ok(invoke(app, "context_document_update", {"document_id": pillar["id"], "body": "New pillar",
                                               "expected_content_hash": pillar["content_hash"]}, revision=3))
    chain = versions(db, space, pillar["id"])
    assert [v.author_id for v in reversed(chain)] == [BASELINE_AUTHOR, HUMAN]
