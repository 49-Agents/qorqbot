"""Global context upload: the owner uploads a call or meeting at Global and the filer files it into the shared tree."""

from dataclasses import replace

from starlette.testclient import TestClient

from qorqut_operations.contracts import Principal, new_id
from qorqut_operations.documents.serialization import split_document
from foundation_support import BUSINESS, HUMAN, context
from test_background_programs import Model, runtime, invoke
from test_context_encounters import FIRST, SECOND, TRANSCRIPT, TWO, person
from test_learning_sources import call
from test_service_transports import ORIGIN, ServiceFixture

TEAMMATE = "00000000-0000-4000-8000-0000000000a1"
ANSWER = {"topics": [{"key": "partners", "title": "Partners", "slug": "partners"}], "notes": [
    {"title": "Pilot terms", "material_type": "document", "paragraph_ids": [0], "topic_key": "partners"},
    {"title": "Next step", "material_type": "note", "paragraph_ids": [1], "topic_key": "partners"}]}


def grant(runtime, *humans):
    runtime.policy.human_global_capabilities = {human: {"read_global_context"} for human in humans or (HUMAN,)}


def at_global(principal=None):
    ctx = replace(context(), business_id=None, review_business_id=None)
    return replace(ctx, principal=principal) if principal else ctx


def reject(runtime, args, *, ctx=None, operation="context_upload", code="validation_error"):
    result = runtime.application.invoke(ctx or at_global(), operation, args, {"idempotency_key": new_id()})
    assert result.status == "error", result.to_dict()
    assert result.error["code"] == code, result.error
    return result.error


def global_rows(runtime, kind=None):
    with runtime.database.read() as uow:
        return [row for row in uow.repository("documents", business_id=None).list() if kind in (None, row["kind"])]


def global_jobs(runtime):
    with runtime.database.read() as uow:
        return uow.repository("jobs", business_id=None).list()


def nothing_written(runtime):
    assert not global_rows(runtime) and not global_jobs(runtime)
    assert not list((runtime.root / "context").glob("_inbox/*"))
    with runtime.database.read() as uow:
        assert not uow.repository("documents", business_id=BUSINESS).list()
        assert not uow.repository("jobs", business_id=BUSINESS).list()


def file_global(runtime, body, answer, **args):
    upload = call(runtime, "context_upload", {"body": body, **args}, global_scope=True)
    runtime.model.answers.append(answer)
    outcome = runtime.background.run_one(upload["job"]["id"])
    assert outcome["state"] == "succeeded", outcome
    with runtime.database.read() as uow:
        documents = uow.repository("documents", business_id=None)
        source = documents.get(upload["document"]["id"])
        notes = [runtime.domains.documents.store.view(documents.get(identity), uow) for identity in source["metadata_json"]["result"]["notes"]]
    return notes, source


def people_named(runtime, name):
    return [row for row in global_rows(runtime, "context_person") if row["metadata_json"].get("name") == name]


def mentioned(runtime, note_id):
    with runtime.database.read() as uow:
        return {edge["to_id"] for edge in uow.repository("relationships", business_id=None).list()
                if edge["from_type"] == "material" and edge["from_id"] == note_id
                and edge["to_type"] == "person" and edge["role"] == "mentions"}


def global_person(runtime, identity):
    return call(runtime, "context_people_get", {"document_id": identity}, global_scope=True)["document"]


def test_owner_uploads_at_global_into_the_shared_inbox_and_one_filing_job_is_queued(runtime):
    grant(runtime)
    result = call(runtime, "context_upload", {"body": TRANSCRIPT, "title": "Kickoff"}, global_scope=True)
    document = result["document"]
    assert result["job"]["state"] == "queued"
    (row,) = global_rows(runtime)
    assert row["id"] == document["id"] and row["kind"] == "context_upload"
    assert row["business_id"] is None and row["namespace"] == "global"
    assert row["path"].startswith("context/_inbox/") and (runtime.root / row["path"]).read_bytes()
    assert [path.name for path in (runtime.root / "context/_inbox").glob("*.md")] == [row["path"].rsplit("/", 1)[1]]
    frontmatter, body = split_document((runtime.root / row["path"]).read_bytes())
    assert body == TRANSCRIPT and frontmatter["status"] == "pending" and frontmatter["title"] == "Kickoff"
    (queued,) = global_jobs(runtime)
    assert queued["id"] == result["job"]["id"] and queued["kind"] == "context.file"
    assert queued["arguments_json"]["upload_id"] == document["id"]
    with runtime.database.read() as uow:
        assert not uow.repository("documents", business_id=BUSINESS).list()
        assert not uow.repository("jobs", business_id=BUSINESS).list()


def test_owner_uploads_a_call_at_global_with_its_details(runtime):
    grant(runtime)
    participants = [{"name": "Priya"}]
    result = call(runtime, "context_upload", {"body": TRANSCRIPT, "origin": "call", "occurred_on": "2026-09-17",
        "participants": [{"name": " Priya "}]}, global_scope=True)
    (row,) = global_rows(runtime)
    expected = {"origin": "call", "occurred_on": "2026-09-17", "participants": participants}
    assert {key: row["metadata_json"][key] for key in expected} == expected
    assert {key: split_document((runtime.root / row["path"]).read_bytes())[0][key] for key in expected} == expected
    assert result["document"]["id"] == row["id"]


def test_upload_at_global_is_denied_without_the_capability_and_to_a_non_owner_teammate(runtime):
    args = {"body": TRANSCRIPT}
    # The owner without the capability gets the engine's capability error.
    runtime.policy.human_global_capabilities = {}
    assert reject(runtime, args, code="forbidden")["message"] == "Global capability is not granted"
    # A teammate who holds the capability is stopped by the existing installation-owner policy.
    with runtime.database.transaction() as uow:
        uow.repository("principals").insert({"id": TEAMMATE, "kind": "human", "display_name": "Teammate"})
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": TEAMMATE, "is_owner": False})
    grant(runtime, HUMAN, TEAMMATE)
    reject(runtime, args, ctx=at_global(Principal(TEAMMATE, "human")), code="policy_denied")
    nothing_written(runtime)
    assert call(runtime, "context_upload", args, global_scope=True)["job"]["state"] == "queued"


def test_unconfigured_agent_cannot_upload_globally_even_when_its_policy_allows_the_tool(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    coordinator = service.fixture.create("coordinator", "ordubashy")
    service.fixture.update(coordinator, {"tool_policy": {"overrides": {"context_upload": "allow"}}})
    ctx = service.runtime.resolver.invocation_context(coordinator)
    assert ctx.business_id is None and "read_global_context" in ctx.global_capabilities
    result = service.runtime.application.invoke(ctx, "context_upload", {"body": TRANSCRIPT}, {"idempotency_key": new_id()})
    assert result.status == "error", result.to_dict()
    assert result.error["code"] == "forbidden" and result.error["message"] == "Only the owner or the authorized Global coordinator can upload Global context"
    with service.runtime.database.read() as uow:
        assert not uow.repository("documents", business_id=None).list(filters={"kind": "context_upload"})
        assert not uow.repository("jobs", business_id=None).list()
    assert not list((service.fixture.root / "context").glob("_inbox/*"))


def test_stories_are_per_business_and_an_empty_list_is_accepted_at_global(runtime):
    grant(runtime)
    assert reject(runtime, {"body": TRANSCRIPT, "story_ids": [new_id()]})["message"] == "Stories are per business"
    nothing_written(runtime)
    assert call(runtime, "context_upload", {"body": TRANSCRIPT, "story_ids": []}, global_scope=True)["job"]["state"] == "queued"


def test_invalid_details_are_rejected_at_global_before_anything_is_written(runtime):
    grant(runtime)
    assert "origin" in reject(runtime, {"body": TRANSCRIPT, "origin": "phone"})["field_errors"]
    assert reject(runtime, {"body": TRANSCRIPT, "participants": [{"name": "Priya"}]})["message"] == "Participants and dates apply to call or meeting uploads"
    assert reject(runtime, {"body": TRANSCRIPT, "origin": "call", "participants": [{"name": "Priya"}, {"name": " PRIYA "}]})["message"] == "Participant names must be distinct"
    nothing_written(runtime)


def test_filing_at_global_with_participants_creates_a_global_person_and_links_it(runtime):
    grant(runtime)
    notes, source = file_global(runtime, TWO, ANSWER, origin="call", occurred_on="2026-09-17", participants=[{"name": "Priya"}])
    (priya,) = people_named(runtime, "Priya")
    assert priya["business_id"] is None and priya["namespace"] == "global"
    assert priya["path"].startswith("context/_people/") and (runtime.root / priya["path"]).is_file()
    assert priya["metadata_json"]["content_owner"] == "filer"
    (topic,) = global_rows(runtime, "context_topic")
    assert topic["path"].startswith("context/") and (runtime.root / topic["path"]).exists()
    assert [note["body"] for note in notes] == [FIRST, SECOND]
    for note in notes:
        assert note["metadata"]["topic_id"] == topic["id"] and note["metadata"]["occurred_on"] == "2026-09-17"
        assert set(note["metadata"]["people"]) == {priya["id"]} and mentioned(runtime, note["id"]) == {priya["id"]}
        assert (runtime.root / note["path"]).is_file() and note["path"].startswith("context/")
        # Classification (Stage 2): a call/meeting note is always "conversation", regardless of what the model proposed.
        assert note["material_type"] == "conversation" and note["origin"] == "call" and note["tags"] == []
        assert note["metadata"]["material_type"] == "conversation" and note["metadata"]["origin"] == "call"
    assert source["kind"] == "context_dump"
    assert {key: source["metadata_json"][key] for key in ("origin", "occurred_on", "participants")} == {
        "origin": "call", "occurred_on": "2026-09-17", "participants": [{"name": "Priya"}]}
    with runtime.database.read() as uow:
        assert not uow.repository("documents", business_id=BUSINESS).list()
        assert not uow.repository("jobs", business_id=BUSINESS).list()


def test_filing_links_existing_global_people_by_pick_by_exact_name_and_by_the_models_link(runtime):
    grant(runtime)
    file_global(runtime, TWO, ANSWER, origin="call", participants=[{"name": "Priya Rao"}])
    (priya,) = people_named(runtime, "Priya Rao")
    (topic,) = global_rows(runtime, "context_topic")
    note = {"title": "Follow-up", "material_type": "conversation", "paragraph_ids": [0, 1], "topic_id": topic["id"]}
    picked, _ = file_global(runtime, TWO, {"notes": [note]}, origin="meeting",
        participants=[{"person_id": priya["id"], "name": "Priya"}, {"name": "Zed"}])
    (zed,) = people_named(runtime, "Zed")
    assert set(picked[0]["metadata"]["people"]) == {priya["id"], zed["id"]}
    assert mentioned(runtime, picked[0]["id"]) == {priya["id"], zed["id"]}
    assert not global_person(runtime, priya["id"])["metadata"].get("aka")
    typed, _ = file_global(runtime, TWO, {"notes": [note]}, origin="call", participants=[{"name": "PRIYA rao"}])
    assert len(global_rows(runtime, "context_person")) == 2 and mentioned(runtime, typed[0]["id"]) == {priya["id"]}
    linked, _ = file_global(runtime, TWO, {"people": [{"key": "participant-1", "person_id": priya["id"], "paragraph_ids": [0]}],
        "notes": [{**note, "people_keys": ["participant-1"]}]}, origin="call", participants=[{"name": "Pri"}])
    assert len(global_rows(runtime, "context_person")) == 2 and mentioned(runtime, linked[0]["id"]) == {priya["id"]}
    kept = global_person(runtime, priya["id"])["metadata"]
    assert kept["name"] == "Priya Rao" and list(kept["aka"]) == ["Pri"]


def test_a_business_person_is_not_a_global_participant_and_nothing_is_written(runtime):
    grant(runtime)
    michael = person(runtime, "Michael Linford")
    error = reject(runtime, {"body": TWO, "origin": "call", "participants": [{"person_id": michael["id"], "name": "Mike"}]}, code="not_found")
    assert error["message"] == "Document not found"
    reject(runtime, {"body": TWO, "origin": "call", "participants": [{"person_id": new_id(), "name": "Ghost"}]}, code="not_found")
    assert not global_rows(runtime) and not global_jobs(runtime)
    assert not list((runtime.root / "context").glob("_inbox/*"))
    with runtime.database.read() as uow:
        assert {row["kind"] for row in uow.repository("documents", business_id=BUSINESS).list()} <= {"context_person", "context_index"}
        assert not uow.repository("jobs", business_id=BUSINESS).list()


def test_global_filed_material_is_classified_and_filterable_by_origin_at_shared_scope_only(runtime):
    grant(runtime)
    notes, _ = file_global(runtime, TWO, ANSWER, origin="call", participants=[{"name": "Priya"}])
    note = notes[0]
    assert (note["material_type"], note["origin"], note["tags"]) == ("conversation", "call", [])
    assert (note["metadata"]["material_type"], note["metadata"]["origin"]) == ("conversation", "call")
    shared = call(runtime, "context_list", {"scope": "shared", "origin": "call"}, global_scope=True)["items"]
    assert note["id"] in {item["id"] for item in shared}
    business = invoke(runtime, "context_list", {"origin": "call"})["items"]
    assert note["id"] not in {item["id"] for item in business}
    searched = call(runtime, "context_search", {"scope": "shared", "query": "pilot", "origin": "call"}, global_scope=True)["items"]
    assert note["id"] in {item["id"] for item in searched}


def test_an_archived_global_participant_is_refused_and_restoring_them_allows_the_upload(runtime):
    grant(runtime)
    file_global(runtime, TWO, ANSWER, origin="call", participants=[{"name": "Priya"}])
    (priya,) = people_named(runtime, "Priya")
    before = (len(global_rows(runtime)), len(global_jobs(runtime)))
    def archive(flag, revision):
        with runtime.database.transaction() as uow:
            uow.repository("documents", business_id=None).update(priya["id"], {"archived": flag}, expected_revision=revision)
    archive(True, priya["revision"])
    picked = {"body": TWO, "origin": "call", "participants": [{"person_id": priya["id"], "name": "Priya"}]}
    assert "archived" in reject(runtime, picked)["message"]
    assert (len(global_rows(runtime)), len(global_jobs(runtime))) == before
    archive(False, priya["revision"] + 1)
    assert call(runtime, "context_upload", picked, global_scope=True)["job"]["state"] == "queued"


def test_people_list_and_get_at_global_never_cross_the_business_boundary(runtime):
    grant(runtime)
    business = person(runtime, "Michael Linford", aka=["Mike"])
    file_global(runtime, TWO, ANSWER, origin="call", participants=[{"name": "Michael Global"}, {"name": "Priya"}])
    (shared,) = people_named(runtime, "Michael Global")
    def names(scope, **args):
        items = (call(runtime, "context_people_list", args, global_scope=True) if scope == "global"
                 else invoke(runtime, "context_people_list", args))["items"]
        return {item["metadata"]["name"] for item in items}
    assert names("global", query="michael") == {"Michael Global"}
    assert names("global", query="mike") == set() and names("global", query="zzz") == set()
    assert names("global", query="PRIYA") == {"Priya"} and names("global") == {"Michael Global", "Priya"}
    assert names("business", query="michael") == {"Michael Linford"} and names("business", query="mike") == {"Michael Linford"}
    assert names("business") == {"Michael Linford"}
    assert global_person(runtime, shared["id"])["metadata"]["name"] == "Michael Global"
    assert reject(runtime, {"document_id": business["id"]}, operation="context_people_get", code="not_found")["message"] == "Document not found"
    assert reject(runtime, {"document_id": new_id()}, operation="context_people_get", code="not_found")


def test_people_reads_at_global_need_the_capability_and_an_owner(runtime):
    runtime.policy.human_global_capabilities = {}
    reject(runtime, {"query": "a"}, operation="context_people_list", code="forbidden")
    reject(runtime, {"document_id": new_id()}, operation="context_people_get", code="forbidden")
    with runtime.database.transaction() as uow:
        uow.repository("principals").insert({"id": TEAMMATE, "kind": "human", "display_name": "Teammate"})
        uow.repository("business_memberships", business_id=BUSINESS).insert({"principal_id": TEAMMATE, "is_owner": False})
    grant(runtime, HUMAN, TEAMMATE)
    reject(runtime, {"query": "a"}, ctx=at_global(Principal(TEAMMATE, "human")), operation="context_people_list", code="policy_denied")


def test_registration_adds_exactly_three_operations_to_global_and_widens_no_exposure(runtime):
    registry = runtime.registry
    for name in ("context_upload", "context_people_list", "context_people_get"):
        descriptor = registry.get(name)
        assert set(descriptor.scope_kinds) == {"business", "global"} and descriptor.global_capability == "read_global_context"
    assert set(registry.get("context_upload").exposure) == {"mcp", "dashboard", "worker"}
    assert set(registry.get("context_people_list").exposure) == set(registry.get("context_people_get").exposure) == {"dashboard", "worker"}
    for name in ("context_filing_apply", "context_filing_fail", "context_summary_apply", "context_map_apply"):
        assert set(registry.get(name).exposure) == {"worker"}
    business_only = ("context_people_create", "context_people_update", "context_people_merge", "context_people_affiliation_set",
                     "context_topics_create", "context_topics_move", "context_notes_create", "context_notes_move",
                     "context_document_update", "context_summarize", "context_map_generate",
                     "context_items_list", "context_items_get", "context_items_archive", "materials_update", "notes_upload")
    for name in business_only:
        assert "global" not in registry.get(name).scope_kinds, name


def test_http_root_context_upload_and_people_routes(tmp_path):
    model = Model()
    service = ServiceFixture(tmp_path, full=True, model_client_factory=lambda business: model)
    with TestClient(service.web) as client:
        service.login(client)
        queued = client.post("/api/root/context/upload", json={"text": TRANSCRIPT, "idempotency_key": new_id()}, headers=ORIGIN)
        assert queued.status_code == 202, queued.text
        body = queued.json()
        assert body["ok"] is True and body["job"]["state"] == "queued" and body["path"].startswith("_inbox/")
        with service.runtime.database.read() as uow:
            row = uow.repository("documents", business_id=None).get(body["document_id"])
            job = uow.repository("jobs", business_id=None).get(body["job"]["id"])
        assert row["business_id"] is None and row["namespace"] == "global" and row["kind"] == "context_upload"
        assert job["kind"] == "context.file" and job["arguments_json"]["upload_id"] == body["document_id"]
        rejected = client.post("/api/root/context/upload",
            json={"text": "x", "story_ids": [new_id()], "idempotency_key": new_id()}, headers=ORIGIN)
        assert rejected.status_code not in (200, 202), rejected.text
        assert rejected.json()["error"]["message"] == "Stories are per business"
        with service.runtime.database.read() as uow:
            assert len(uow.repository("documents", business_id=None).list(filters={"kind": "context_upload"})) == 1
        no_match = client.post("/api/root/context/people", json={"query": "nobody-by-this-name", "idempotency_key": new_id()}, headers=ORIGIN)
        assert no_match.status_code == 200, no_match.text
        assert no_match.json()["data"]["items"] == []
        placeholder = service.fixture.document("priya-http", "Priya HTTP\n", "context/_people/priya-http.md")
        with service.runtime.database.transaction() as uow:
            uow.repository("documents", business_id=None).update(placeholder["id"],
                {"kind": "context_person", "metadata_json": {"name": "Priya HTTP"}}, expected_revision=placeholder["revision"])
        found = client.post("/api/root/context/people", json={"query": "Priya", "idempotency_key": new_id()}, headers=ORIGIN)
        assert found.status_code == 200, found.text
        assert [item["metadata"]["name"] for item in found.json()["data"]["items"]] == ["Priya HTTP"]
        cross = client.post("/api/biz/widget/context/people", json={"query": "Priya", "idempotency_key": new_id()}, headers=ORIGIN)
        assert cross.status_code == 404, cross.text
