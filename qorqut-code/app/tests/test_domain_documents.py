import base64
from dataclasses import replace
from datetime import datetime, timezone

from qorqut_business_docs_versioning import HistoryStore

from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from qorqut_operations.contracts import InvocationOptions, canonical_hash, new_id
from bazarlyk.domains import register_business_domains
from qorqut_operations.storage.executor import PreparedFileExecutor
from bazarlyk.policy import PolicyResolver
from bazarlyk.storage.files import FileApplier
from foundation_support import BUSINESS, OTHER, FixedPolicy, context, database_at
from platform_support import setup_platform


def invoke(app, op, args, revision=None, ctx=None, key=None):
    return app.invoke(ctx or context(), op, args, InvocationOptions(expected_revision=revision,
        idempotency_key=key or canonical_hash([op, args, revision])))


def setup_documents(tmp_path):
    db = database_at(tmp_path)
    for name in ("first", "second"): (tmp_path / name / "hq").mkdir(parents=True)
    registry = builtin_registry()
    app = Application(db, registry, policy=FixedPolicy())
    domains = register_business_domains(registry, app, tmp_path, catalog=None, agent_resolver=None,
        clock=lambda: datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
    app.file_executor = PreparedFileExecutor(db, FileApplier(db, domains.resolve_file_target))
    return db, app, domains


def test_notes_exact_file_update_replay_and_external_edit_conflict(tmp_path):
    db, app, domains = setup_documents(tmp_path)
    from foundation_support import HUMAN
    with db.transaction() as uow:
        uow.repository("installation").insert({"id":"service", "installation_id":new_id(), "owner_principal_id":HUMAN})
    owner = replace(context(), business_id=None, global_capabilities=frozenset({"read_global_context"}))
    created = invoke(app, "notes_create", {"title": "Next steps", "body": "First version"}, ctx=owner)
    assert created.status == "ok", created.to_dict()
    doc = created.data["document"]
    edited = invoke(app, "notes_update", {"document_id": doc["id"], "body": "Second version"}, revision=1, ctx=owner)
    assert edited.status == "ok", edited.to_dict()
    read = app.invoke(owner, "notes_get", {"document_id": doc["id"]})
    assert read.data["document"]["body"] == "Second version"
    assert read.data["document"]["revision"] == 2
    assert invoke(app, "notes_update", {"document_id": doc["id"], "body": "Second version"}, revision=1, ctx=owner).data == edited.data
    file = tmp_path / doc["path"]
    file.write_text("User-authored third version")
    assert invoke(app, "notes_update", {"document_id": doc["id"], "body": "Fourth"}, revision=2, ctx=owner).error["code"] == "revision_conflict"
    observed = app.invoke(owner, "notes_get", {"document_id": doc["id"]}).data["document"]
    assert observed["metadata_stale"]
    reconciled = invoke(app, "notes_update", {"document_id": doc["id"], "body": "Fourth", "expected_content_hash": observed["content_hash"]}, revision=2, ctx=owner)
    assert reconciled.status == "ok", reconciled.to_dict()


def test_topic_move_and_person_merge_preserve_body_and_references(tmp_path):
    _, app, _ = setup_documents(tmp_path)
    topic = invoke(app, "context_topics_create", {"title": "Customers", "slug": "customers"}).data["document"]
    first = invoke(app, "context_people_create", {"name": "Ada", "notes": "First person notes"}).data["document"]
    second = invoke(app, "context_people_create", {"name": "Ada Again", "notes": "Second person notes"}).data["document"]
    note = invoke(app, "context_notes_create", {"topic_id": topic["id"], "title": "Interview", "body": "Verbatim detail", "people_ids": [second["id"]]}).data["document"]
    merged = invoke(app, "context_people_merge", {"document_id": first["id"], "merge_id": second["id"], "merge_revision": 1}, revision=1)
    assert merged.status == "ok", merged.to_dict()
    read = app.invoke(context(), "context_people_get", {"document_id": first["id"]}).data["document"]
    assert "First person notes" in read["body"] and "Second person notes" in read["body"]
    assert app.invoke(context(), "context_get", {"document_id": note["id"]}).data["document"]["metadata"]["people"] == (first["id"],)
    moved = invoke(app, "context_topics_move", {"document_id": topic["id"], "slug": "interviews"}, revision=1)
    assert moved.status == "ok", moved.to_dict()
    read_note = app.invoke(context(), "context_get", {"document_id": note["id"]}).data["document"]
    assert read_note["path"].startswith("context/interviews/") and read_note["body"] == "Verbatim detail"
    index = (tmp_path / "first/hq/context/INDEX.md").read_text()
    assert "interviews/_topic.md" in index and "customers/_topic.md" not in index


def test_asset_bytes_and_metadata_and_business_scope(tmp_path):
    _, app, _ = setup_documents(tmp_path)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nreal asset bytes").decode()
    uploaded = invoke(app, "assets_upload", {"filename": "Launch Image.PNG", "content_b64": encoded, "group": "polished"})
    assert uploaded.status == "ok", uploaded.to_dict()
    asset = uploaded.data["asset"]
    assert asset["path"] == "assets/polished/launch-image.png"
    updated = invoke(app, "assets_update", {"asset_id": asset["id"], "changes": {"notes": "Approved image"}}, revision=1)
    assert updated.status == "ok", updated.to_dict()
    cross = replace(context(), business_id=OTHER, allowed_business_ids=frozenset({OTHER}))
    assert app.invoke(cross, "assets_get", {"asset_id": asset["id"]}).error["code"] == "not_found"
    assert invoke(app, "assets_delete", {"asset_id": asset["id"]}, revision=2).status == "ok"
    assert (tmp_path / "first/hq" / asset["path"]).read_bytes() == base64.b64decode(encoded)
    assert not app.invoke(context(), "assets_list", {}).data["items"]


def test_agents_read_teammate_plan_and_notes_but_only_write_own(tmp_path):
    fixture, app, _, ctx = setup_platform(tmp_path)
    coordinator = fixture.create("coordinator", "ordubashy", runtime="codex")
    reader = replace(ctx, principal=coordinator, enabled_plugin_refs=())
    created = invoke(app, "notes_create", {"title": "Evidence", "body": "Observed result"}, ctx=ctx)
    assert created.status == "ok", created.to_dict()
    identity = created.data["document"]["id"]
    notes = app.invoke(reader, "notes_list", {"agent_id": ctx.agent_id})
    assert notes.data["items"][0]["id"] == identity
    assert app.invoke(reader, "plans_get", {"agent_id": ctx.agent_id}).status == "ok"
    denied = invoke(app, "notes_update", {"document_id": identity, "body": "Changed by teammate"}, revision=1, ctx=reader)
    assert denied.error["code"] == "forbidden"
    assert invoke(app, "notes_create", {"title": "Not authorized", "body": "X", "target_agent_id": ctx.agent_id}, ctx=reader).error["code"] == "forbidden"


def test_claimed_filing_job_uses_frozen_outputs_and_preserves_source(tmp_path):
    from qorqut_operations.background.authority import BackgroundAuthority
    from qorqut_operations.background.programs import Program, ProgramRegistry
    from bazarlyk.domains.common import schema
    db, app, domains = setup_documents(tmp_path)
    programs = ProgramRegistry()
    permissions = {name: "allow" for name in ("context_filing_apply", "context_topics_create", "context_people_create", "context_notes_create", "logs_add")}
    programs.register(Program("context.file", 1, schema({"upload_id": {"type": "string"}, "expected_revision": {"type": "integer"}}, ("upload_id", "expected_revision")),
        lambda *_: None, permissions=permissions, outputs=("context_filing_apply",),
        resource_types=("documents", "managed_document_files", "document_index_sources", "action_logs", "relationships")))
    authority = BackgroundAuthority(db, programs)
    with db.transaction() as uow:
        uow.repository("installation").insert({"id": "service", "installation_id": new_id(), "owner_principal_id": context().principal_id})
        authority.register_workers(uow, [BUSINESS])
    domains.documents.programs = programs
    policy = PolicyResolver(app.registry, worker_profiles=authority.worker_profiles,
        job_consequence_verifier=authority.verify_consequence, job_plan_validator=authority.validate_plan)
    app.policy, app.context_resolver = policy, policy.refresh_context
    submitted = invoke(app, "context_upload", {"title": "Interview", "body": "Verbatim interview paragraph."})
    assert submitted.status == "accepted", submitted.to_dict()
    upload = submitted.data["document"]
    job_id = submitted.data["job"]["id"]
    arguments = {"upload_id": upload["id"], "topics": [{"key": "customers", "title": "Customers", "slug": "customers"}],
        "people": [{"key": "ada", "name": "Ada", "source_spans": [{"start": 0, "end": 29}]}],
        "notes": [{"topic_key": "customers", "title": "Interview", "source_spans": [{"start": 0, "end": 29}], "material_type": "conversation", "people_keys": ["ada"], "summary": "A first customer interview."}]}
    outputs = [{"operation": "context_filing_apply", "arguments": arguments, "options": {"expected_revision": 1}}]
    with db.transaction() as uow:
        job = uow.repository("jobs", business_id=BUSINESS).get(job_id)
        uow.repository("jobs", business_id=BUSINESS).update(job_id, {"state": "applying", "result_json": {"outputs": outputs, "outputs_hash": canonical_hash(outputs)}}, expected_revision=job["revision"])
    with db.read() as uow:
        worker = policy.context_for(job["principal_id"], business_id=BUSINESS, review_business_id=BUSINESS, uow=uow,
            initiated_by_principal_id=job["initiated_by_principal_id"], job_id=job_id)
    forged = invoke(app, "context_filing_apply", {**arguments, "summary": "Unfrozen extra instruction"}, revision=1, ctx=worker)
    assert forged.error["code"] == "invalid_plan"
    completed = invoke(app, "context_filing_apply", arguments, revision=1, ctx=worker)
    assert completed.status == "ok", completed.to_dict()
    with db.read(business_id=BUSINESS) as uow:
        stored = uow.repository("documents").get(upload["id"])
        assert stored["kind"] == "context_dump" and stored["metadata_json"]["status"] == "processed"
        assert "Verbatim interview paragraph." in (tmp_path / "first/hq" / stored["path"]).read_text()
        assert not (tmp_path / "first/hq" / upload["path"]).exists()
        note = next(row for row in uow.repository("documents").list() if row["kind"] == "context_note")
        assert domains.documents.store.view(note, uow)["metadata"]["people"]
        assert len(uow.repository("action_logs").list()) == 1
        # The editable upload and its filed dump are one shared-history chain with truthful attribution.
        chain = HistoryStore(uow.connection).list_versions("business:" + BUSINESS, upload["id"]).versions
        assert [v.author_id for v in reversed(chain)] == [context().principal_id, job["principal_id"]]
        assert HistoryStore(uow.connection).list_versions("business:" + BUSINESS, note["id"]).versions[0].author_id == job["principal_id"]
