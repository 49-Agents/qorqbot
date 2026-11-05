"""Call and meeting uploads: metadata, participant validation, filing and listing."""

import json

import pytest
from starlette.testclient import TestClient

from bazarlyk.background.context import ContextPrograms
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id
from qorqut_operations.documents.serialization import split_document
from foundation_support import BUSINESS, HUMAN, OTHER, context
from test_background_programs import Model, runtime, invoke
from test_filing_materials import file_upload
from test_service_background import wait_job
from test_service_transports import ORIGIN, ServiceFixture

TRANSCRIPT = "Michael [10:00]: Can we pilot in October?\nPriya [10:01]: Yes, the budget is approved.\n"
HQ = "first/hq/"


def other_context():
    return InvocationContext(Principal(HUMAN, "human"), business_id=OTHER,
                             allowed_business_ids=frozenset({OTHER}), source="dashboard")


def reject(runtime, args, *, code="validation_error", ctx=None, operation="context_upload"):
    result = runtime.application.invoke(ctx or context(), operation, args, {"idempotency_key": new_id()})
    assert result.status == "error", result.to_dict()
    assert result.error["code"] == code, result.error
    return result.error


def person(runtime, name, **args):
    return invoke(runtime, "context_people_create", {"name": name, **args})["document"]


def upload_documents(runtime, business_id=BUSINESS):
    with runtime.database.read() as uow:
        return [row for row in uow.repository("documents", business_id=business_id).list()
                if row["kind"] in {"context_upload", "context_dump"}]


def test_schema_and_prepare_reject_invalid_encounter_details_without_writing(runtime):
    body = {"body": TRANSCRIPT}
    error = reject(runtime, {**body, "origin": "phone"})
    assert "origin" in error["field_errors"]
    for extra in ({"participants": [{"name": "Priya"}]}, {"occurred_on": "2026-09-17"}, {"participants": []}):
        error = reject(runtime, {**body, **extra})
        assert error["message"] == "Participants and dates apply to call or meeting uploads"
    call = {**body, "origin": "call"}
    assert "occurred_on" in reject(runtime, {**call, "occurred_on": "2026-13-45"})["field_errors"]
    assert "participants.0" in reject(runtime, {**call, "participants": [{"name": "A", "role": "cto"}]})["field_errors"]
    assert "participants.0.person_id" in reject(runtime, {**call, "participants": [{"person_id": "nope", "name": "A"}]})["field_errors"]
    assert "participants" in reject(runtime, {**call, "participants": [{"name": f"P{i}"} for i in range(21)]})["field_errors"]
    assert reject(runtime, {**call, "participants": [{"name": "  \t "}]})["message"] == "Participant name cannot be blank"
    assert reject(runtime, {**call, "participants": [{"name": "Priya"}, {"name": " PRIYA "}]})["message"] == "Participant names must be distinct"
    michael = person(runtime, "Michael Linford")
    pair = [{"person_id": michael["id"], "name": "Mike"}, {"person_id": michael["id"], "name": "Michael"}]
    assert reject(runtime, {**call, "participants": pair})["message"] == "Participant Persons must be distinct"
    assert not upload_documents(runtime)
    with runtime.database.read() as uow:
        assert not uow.repository("jobs", business_id=BUSINESS).list()
    assert not list((runtime.root / HQ / "context").glob("_inbox/*"))


def test_cross_business_and_global_persons_fail_in_prepare_before_anything_is_written(runtime):
    foreign = runtime.application.invoke(other_context(), "context_people_create", {"name": "Zed"}, {"idempotency_key": new_id()})
    assert foreign.status == "ok", foreign.to_dict()
    participants = [{"person_id": foreign.data["document"]["id"], "name": "Zed"}]
    reject(runtime, {"body": TRANSCRIPT, "origin": "call", "participants": participants}, code="not_found")
    reject(runtime, {"body": TRANSCRIPT, "origin": "call", "participants": [{"person_id": new_id(), "name": "Ghost"}]}, code="not_found")
    # Document lookup falls back to Global rows, but a Global Person is not a Person of this business.
    shared = new_id()
    with runtime.database.transaction() as uow:
        uow.repository("documents", business_id=None).insert({"id": shared, "namespace": "global", "kind": "context_person",
            "path": "context/_people/shared-sam.md", "content_hash": "0" * 64, "metadata_json": {"name": "Shared Sam"},
            "created_at": "2026-09-19T00:00:00Z", "updated_at": "2026-09-19T00:00:00Z"})
    reject(runtime, {"body": TRANSCRIPT, "origin": "call", "participants": [{"person_id": shared, "name": "Sam"}]}, code="not_found")
    assert not upload_documents(runtime)
    assert not upload_documents(runtime, OTHER)
    with runtime.database.read() as uow:
        assert not uow.repository("jobs", business_id=BUSINESS).list()
    assert not list((runtime.root / HQ / "context").glob("_inbox/*"))


def test_upload_metadata_is_persisted_exactly_and_retry_keeps_it(runtime):
    michael = person(runtime, "Michael Linford")
    topic = invoke(runtime, "context_topics_create", {"title": "Partners", "slug": "partners"})["document"]
    participants = [{"person_id": michael["id"], "name": "Mike"}, {"name": "Priya"}]
    upload = invoke(runtime, "context_upload", {"body": TRANSCRIPT, "title": "Pilot call", "origin": "call",
        "occurred_on": "2026-09-17", "participants": [{"person_id": michael["id"], "name": " Mike "}, {"name": "Priya "}]})
    document = upload["document"]
    assert upload["job"]["state"] == "queued"
    expected = {"origin": "call", "occurred_on": "2026-09-17", "participants": participants}
    frontmatter, body = split_document((runtime.root / HQ / document["path"]).read_bytes())
    assert body == TRANSCRIPT
    assert {key: frontmatter[key] for key in expected} == expected
    assert frontmatter["status"] == "pending" and frontmatter["material_type"] == "mixed"
    with runtime.database.read() as uow:
        row = uow.repository("documents", business_id=BUSINESS).get(document["id"])
        assert {key: row["metadata_json"][key] for key in expected} == expected
        queued = uow.repository("jobs", business_id=BUSINESS).get(upload["job"]["id"])
    assert queued["kind"] == "context.file" and queued["arguments_json"]["upload_id"] == document["id"]
    # A failed filing keeps the details, and Retry preserves them while clearing only the failure record.
    runtime.model.answers.extend([{"notes": [{"title": "Partial", "topic_id": topic["id"], "paragraph_ids": []}]}] * 2)
    assert runtime.background.run_one(queued["id"])["state"] == "failed"
    with runtime.database.read() as uow:
        failed = uow.repository("documents", business_id=BUSINESS).get(document["id"])
    assert failed["metadata_json"]["status"] == "failed" and failed["metadata_json"]["error"]
    retried = invoke(runtime, "context_retry", {"upload_id": document["id"]}, failed["revision"])
    assert retried["job"]["state"] == "queued" and retried["job"]["id"] != queued["id"]
    with runtime.database.read() as uow:
        again = uow.repository("documents", business_id=BUSINESS).get(document["id"])
    assert {key: again["metadata_json"][key] for key in expected} == expected
    assert again["metadata_json"]["status"] == "pending"
    assert "error" not in again["metadata_json"] and "last_attempt" not in again["metadata_json"]
    assert {key: split_document((runtime.root / HQ / document["path"]).read_bytes())[0][key] for key in expected} == expected


def test_encounter_without_participants_or_date_stores_only_the_origin(runtime):
    document = invoke(runtime, "context_upload", {"body": TRANSCRIPT, "origin": "meeting"})["document"]
    frontmatter = split_document((runtime.root / HQ / document["path"]).read_bytes())[0]
    assert frontmatter["origin"] == "meeting"
    assert "participants" not in frontmatter and "occurred_on" not in frontmatter


def test_ordinary_upload_metadata_is_unchanged(runtime):
    document = invoke(runtime, "context_upload", {"body": TRANSCRIPT, "title": "Plain"})["document"]
    frontmatter = split_document((runtime.root / HQ / document["path"]).read_bytes())[0]
    assert frontmatter["origin"] == "upload"
    assert not {"participants", "occurred_on"} & set(frontmatter)


TWO = "Michael [10:00]: Can we pilot in October?\nPriya [10:01]: Yes, the budget is approved.\n\nMichael [10:05]: Great, send the plan.\n"
FIRST, SECOND = TWO.split("\n\n")[0] + "\n\n", TWO.split("\n\n")[1]


def topic(runtime):
    return invoke(runtime, "context_topics_create", {"title": "Partners", "slug": "partners"})["document"]


def people_named(runtime, name):
    with runtime.database.read() as uow:
        return [row for row in uow.repository("documents", business_id=BUSINESS).list()
                if row["kind"] == "context_person" and row["metadata_json"].get("name") == name]


def mentioned(runtime, note_id):
    with runtime.database.read() as uow:
        return {edge["to_id"] for edge in uow.repository("relationships", business_id=BUSINESS).list()
                if edge["from_type"] == "material" and edge["from_id"] == note_id
                and edge["to_type"] == "person" and edge["role"] == "mentions"}


def current(runtime, person_id):
    return runtime.application.invoke(context(), "context_people_get", {"document_id": person_id}).data["document"]


def failed_filing(runtime, upload, answer):
    runtime.model.answers.extend([answer] * 2)
    outcome = runtime.background.run_one(upload["job"]["id"])
    assert outcome["state"] == "failed", outcome
    with runtime.database.read() as uow:
        return uow.repository("documents", business_id=BUSINESS).get(upload["document"]["id"])


def test_filer_links_every_participant_and_forces_conversation_when_the_model_omits_them(runtime):
    partners, michael = topic(runtime), person(runtime, "Michael Linford", org="Acme")
    participants = [{"person_id": michael["id"], "name": "Mike"}, {"name": "Priya"}]
    notes, source = file_upload(runtime, TWO, {"notes": [
        {"title": "Pilot terms", "material_type": "document", "paragraph_ids": [0], "topic_id": partners["id"]},
        {"title": "Next step", "material_type": "note", "paragraph_ids": [1], "topic_id": partners["id"]}]},
        origin="call", occurred_on="2026-09-17", participants=participants)
    priya = people_named(runtime, "Priya")
    assert len(priya) == 1 and priya[0]["metadata_json"]["content_owner"] == "filer"
    assert [note["body"] for note in notes] == [FIRST, SECOND]
    for note in notes:
        assert note["material_type"] == note["metadata"]["material_type"] == "conversation"
        assert note["origin"] == note["metadata"]["origin"] == "call"
        assert note["metadata"]["occurred_on"] == "2026-09-17"
        assert set(note["metadata"]["people"]) == {michael["id"], priya[0]["id"]}
        assert mentioned(runtime, note["id"]) == {michael["id"], priya[0]["id"]}
    assert source["kind"] == "context_dump"
    assert {key: source["metadata_json"][key] for key in ("origin", "occurred_on", "participants")} == {
        "origin": "call", "occurred_on": "2026-09-17", "participants": participants}
    picked = current(runtime, michael["id"])["metadata"]
    assert picked["name"] == "Michael Linford" and picked["org"] == "Acme" and not picked.get("aka")
    assert not people_named(runtime, "Mike")


def test_call_without_participants_is_still_tagged_and_conversation(runtime):
    notes, _ = file_upload(runtime, TWO, {"notes": [{"title": "Call", "material_type": "document", "paragraph_ids": [0, 1], "topic_id": topic(runtime)["id"]}]},
        origin="meeting")
    assert notes[0]["material_type"] == "conversation" and notes[0]["origin"] == "meeting"
    assert "occurred_on" not in notes[0]["metadata"] and not notes[0]["metadata"]["people"]


def test_typed_name_matched_to_an_existing_person_records_an_alias_only(runtime):
    partners = topic(runtime)
    michael = person(runtime, "Michael Linford", org="Acme", role="Founder", about="Owner's biography")
    line = "Mike Linford [10:00]: Ready when you are.\n"
    notes, _ = file_upload(runtime, line, {
        "people": [{"key": "participant-1", "person_id": michael["id"], "name": "Someone Else", "org": "Elsewhere", "paragraph_ids": [0]}],
        "notes": [{"title": "Kickoff", "material_type": "conversation", "paragraph_ids": [0], "topic_id": partners["id"], "people_keys": ["participant-1"]}]},
        origin="meeting", participants=[{"name": "Mike Linford"}])
    assert len(people_named(runtime, "Michael Linford")) == 1
    assert not people_named(runtime, "Mike Linford") and not people_named(runtime, "Someone Else")
    metadata = current(runtime, michael["id"])["metadata"]
    assert list(metadata["aka"]) == ["Mike Linford"] and metadata["name"] == "Michael Linford"
    assert metadata["org"] == "Acme" and metadata["role"] == "Founder" and metadata["about"] == "Owner's biography"
    assert metadata["filer_evidence"] == line
    assert set(notes[0]["metadata"]["people"]) == {michael["id"]} and mentioned(runtime, notes[0]["id"]) == {michael["id"]}


def test_typed_name_reuses_the_models_own_new_person_entry_instead_of_filing_a_second_one(runtime):
    partners = topic(runtime)
    line = "Michael Linford [10:00]: Ready when you are.\n"
    notes, _ = file_upload(runtime, line, {
        "people": [{"key": "michael", "name": "Michael Linford", "org": "Acme", "paragraph_ids": [0]}],
        "notes": [{"title": "Kickoff", "material_type": "conversation", "paragraph_ids": [0], "topic_id": partners["id"], "people_keys": ["michael"]}]},
        origin="call", participants=[{"name": "michael linford"}])
    created = people_named(runtime, "michael linford")
    assert len(created) == 1 and not people_named(runtime, "Michael Linford")
    assert created[0]["metadata_json"]["org"] == "Acme"
    assert set(notes[0]["metadata"]["people"]) == {created[0]["id"]} and mentioned(runtime, notes[0]["id"]) == {created[0]["id"]}


def test_typed_name_reuses_the_models_own_link_to_an_existing_person_and_records_the_alias(runtime):
    partners, michael = topic(runtime), person(runtime, "Michael Linford")
    line = "Mike [10:00]: Ready when you are.\n"
    notes, _ = file_upload(runtime, line, {
        "people": [{"key": "mike", "person_id": michael["id"], "name": "Mike", "paragraph_ids": [0]}],
        "notes": [{"title": "Kickoff", "material_type": "conversation", "paragraph_ids": [0], "topic_id": partners["id"], "people_keys": ["mike"]}]},
        origin="call", participants=[{"name": "Mike"}])
    assert not people_named(runtime, "Mike")
    assert list(current(runtime, michael["id"])["metadata"]["aka"]) == ["Mike"]
    assert set(notes[0]["metadata"]["people"]) == {michael["id"]} and mentioned(runtime, notes[0]["id"]) == {michael["id"]}


def test_alias_is_not_repeated_when_it_matches_the_name_or_a_known_alias(runtime):
    partners = topic(runtime)
    michael = person(runtime, "Michael Linford", aka=["Mike"])
    for spelling in ("MIKE", "michael linford", "Mikey"):
        file_upload(runtime, f"{spelling} [10:00]: Hello.\n", {
            "people": [{"key": "participant-1", "person_id": michael["id"], "paragraph_ids": [0]}],
            "notes": [{"title": "Hello " + spelling, "material_type": "conversation", "paragraph_ids": [0], "topic_id": partners["id"]}]},
            origin="call", participants=[{"name": spelling}])
    assert list(current(runtime, michael["id"])["metadata"]["aka"]) == ["Mike", "Mikey"]


def test_typed_name_the_model_matches_to_a_picked_person_links_that_person_once(runtime):
    partners, michael = topic(runtime), person(runtime, "Michael Linford")
    notes, _ = file_upload(runtime, "Mike [10:00]: Hello.\n", {
        "people": [{"key": "participant-2", "person_id": michael["id"], "paragraph_ids": [0]}],
        "notes": [{"title": "Hello", "material_type": "conversation", "paragraph_ids": [0], "topic_id": partners["id"]}]},
        origin="call", participants=[{"person_id": michael["id"], "name": "Michael Linford"}, {"name": "Mike"}])
    with runtime.database.read() as uow:
        edges = [edge for edge in uow.repository("relationships", business_id=BUSINESS).list()
                 if edge["from_id"] == notes[0]["id"] and edge["role"] == "mentions"]
    assert [edge["to_id"] for edge in edges] == [michael["id"]]
    assert list(current(runtime, michael["id"])["metadata"]["aka"]) == ["Mike"]
    assert not people_named(runtime, "Mike")


def all_people(runtime):
    with runtime.database.read() as uow:
        return [row for row in uow.repository("documents", business_id=BUSINESS).list() if row["kind"] == "context_person"]


def test_typed_name_equal_to_one_existing_persons_name_or_alias_links_them_without_a_new_person(runtime):
    partners = topic(runtime)
    michael = person(runtime, "Michael Linford", aka=["Mike"], org="Acme")
    person(runtime, "Priya Rao")
    note = {"title": "Pilot", "material_type": "conversation", "paragraph_ids": [0, 1], "topic_id": partners["id"]}
    for spelling in ("michael LINFORD", "MIKE"):
        notes, _ = file_upload(runtime, TWO, {"notes": [note]}, origin="call", participants=[{"name": spelling}])
        assert len(all_people(runtime)) == 2, spelling
        assert mentioned(runtime, notes[0]["id"]) == {michael["id"]} and set(notes[0]["metadata"]["people"]) == {michael["id"]}
    kept = current(runtime, michael["id"])["metadata"]
    assert kept["name"] == "Michael Linford" and list(kept["aka"]) == ["Mike"] and kept["org"] == "Acme"
    assert not kept.get("filer_evidence")
    picked, _ = file_upload(runtime, TWO, {"notes": [note]}, origin="call",
        participants=[{"person_id": michael["id"], "name": "Michael Linford"}, {"name": "Mike"}])
    assert len(all_people(runtime)) == 2 and mentioned(runtime, picked[0]["id"]) == {michael["id"]}


def test_typed_name_equal_to_several_existing_people_or_none_creates_a_new_person(runtime):
    partners = topic(runtime)
    acme, beta = person(runtime, "Sam", org="Acme"), person(runtime, "Samuel Ade", aka=["sam"], org="Beta")
    note = {"title": "Pilot", "material_type": "conversation", "paragraph_ids": [0, 1], "topic_id": partners["id"]}
    notes, _ = file_upload(runtime, TWO, {"notes": [note]}, origin="call", participants=[{"name": "SAM"}])
    created = [row for row in all_people(runtime) if row["id"] not in {acme["id"], beta["id"]}]
    assert len(created) == 1 and created[0]["metadata_json"]["name"] == "SAM"
    assert mentioned(runtime, notes[0]["id"]) == {created[0]["id"]}
    assert not current(runtime, acme["id"])["metadata"].get("aka") and list(current(runtime, beta["id"])["metadata"]["aka"]) == ["sam"]
    notes, _ = file_upload(runtime, TWO, {"notes": [note]}, origin="call", participants=[{"name": "Samuel"}])
    assert len(all_people(runtime)) == 4 and len(people_named(runtime, "Samuel")) == 1


def test_a_new_person_entry_for_a_typed_name_equal_to_one_existing_person_links_that_person_instead(runtime):
    partners = topic(runtime)
    michael = person(runtime, "Michael Linford", aka=["Mike"], org="Acme")
    person(runtime, "Priya Rao")
    for spelling, entry in (("michael LINFORD", {"key": "participant-1", "name": "Michael Linford", "org": "Elsewhere", "paragraph_ids": [0, 1]}),
                            ("MIKE", {"key": "participant-1", "name": "Mike", "aka": ["Mickey"], "paragraph_ids": [0]})):
        notes, _ = file_upload(runtime, TWO, {"people": [entry], "notes": [
            {"title": "Pilot", "material_type": "conversation", "paragraph_ids": [0, 1], "topic_id": partners["id"], "people_keys": ["participant-1"]}]},
            origin="call", participants=[{"name": spelling}])
        assert len(all_people(runtime)) == 2, spelling
        assert mentioned(runtime, notes[0]["id"]) == {michael["id"]} and set(notes[0]["metadata"]["people"]) == {michael["id"]}
    kept = current(runtime, michael["id"])["metadata"]
    assert kept["name"] == "Michael Linford" and list(kept["aka"]) == ["Mike"] and kept["org"] == "Acme"
    assert not kept.get("filer_evidence")


def test_a_new_person_entry_for_a_typed_name_equal_to_several_existing_people_stands(runtime):
    partners = topic(runtime)
    acme, beta = person(runtime, "Sam", org="Acme"), person(runtime, "Samuel Ade", aka=["sam"], org="Beta")
    notes, _ = file_upload(runtime, TWO, {"people": [{"key": "participant-1", "name": "Sam Model"}], "notes": [
        {"title": "Pilot", "material_type": "conversation", "paragraph_ids": [0, 1], "topic_id": partners["id"], "people_keys": ["participant-1"]}]},
        origin="call", participants=[{"name": "SAM"}])
    created = [row for row in all_people(runtime) if row["id"] not in {acme["id"], beta["id"]}]
    assert len(created) == 1 and created[0]["metadata_json"]["name"] == "SAM"
    assert mentioned(runtime, notes[0]["id"]) == {created[0]["id"]}


def test_a_link_the_model_made_for_a_typed_name_is_left_to_the_model(runtime):
    partners = topic(runtime)
    exact, rivera = person(runtime, "Sam"), person(runtime, "Sam Rivera", aka=["Sammy"])
    notes, _ = file_upload(runtime, TWO, {"people": [{"key": "participant-1", "person_id": rivera["id"]}], "notes": [
        {"title": "Pilot", "material_type": "conversation", "paragraph_ids": [0, 1], "topic_id": partners["id"], "people_keys": ["participant-1"]}]},
        origin="call", participants=[{"name": "Sam"}])
    assert mentioned(runtime, notes[0]["id"]) == {rivera["id"]} and len(all_people(runtime)) == 2
    assert list(current(runtime, rivera["id"])["metadata"]["aka"]) == ["Sammy", "Sam"] and not current(runtime, exact["id"])["metadata"].get("aka")


def test_a_matched_typed_name_and_a_new_one_in_the_same_call_when_the_model_proposes_both(runtime):
    partners = topic(runtime)
    michael = person(runtime, "Michael Linford")
    notes, _ = file_upload(runtime, TWO, {"people": [{"key": "participant-1", "name": "Michael Linford"}, {"key": "participant-2", "name": "Zed"}], "notes": [
        {"title": "Pilot", "material_type": "conversation", "paragraph_ids": [0, 1], "topic_id": partners["id"], "people_keys": ["participant-1", "participant-2"]}]},
        origin="meeting", participants=[{"name": "michael linford"}, {"name": "Zed"}])
    zed = people_named(runtime, "Zed")
    assert len(all_people(runtime)) == 2 and len(zed) == 1
    assert mentioned(runtime, notes[0]["id"]) == {michael["id"], zed[0]["id"]}


def test_archived_participant_is_refused_in_prepare_before_anything_is_written(runtime):
    michael = person(runtime, "Michael Linford")
    invoke(runtime, "context_items_archive", {"item": {"type": "person", "id": michael["id"]}, "archived": True}, 1)
    before = upload_documents(runtime)
    error = reject(runtime, {"body": TWO, "origin": "call", "participants": [{"person_id": michael["id"], "name": "Mike"}]})
    assert "archived" in error["message"] and upload_documents(runtime) == before
    invoke(runtime, "context_items_archive", {"item": {"type": "person", "id": michael["id"]}, "archived": False}, 2)
    assert invoke(runtime, "context_upload", {"body": TWO, "origin": "call", "participants": [{"person_id": michael["id"], "name": "Mike"}]})["job"]


def note_view(runtime, identity):
    with runtime.database.read() as uow:
        return runtime.domains.documents.store.view(uow.repository("documents", business_id=BUSINESS).get(identity), uow)


def test_call_notes_never_append_to_an_existing_material(runtime):
    partners = topic(runtime)
    first, _ = file_upload(runtime, "Thread #17\nLuka: Hi!\n", {"notes": [
        {"title": "Thread #17", "material_type": "conversation", "paragraph_ids": [0], "topic_id": partners["id"]}]})
    notes, _ = file_upload(runtime, "Thread #17 continued\nMichael: Hello again.\n", {"notes": [
        {"title": "Continued", "material_type": "document", "paragraph_ids": [0], "topic_id": partners["id"], "material_id": first[0]["id"]}]},
        origin="call", occurred_on="2026-09-17", participants=[{"name": "Michael"}])
    assert len(notes) == 1 and notes[0]["id"] != first[0]["id"] and notes[0]["body"] == "Thread #17 continued\nMichael: Hello again.\n"
    assert (notes[0]["origin"], notes[0]["material_type"], notes[0]["metadata"]["occurred_on"]) == ("call", "conversation", "2026-09-17")
    older = note_view(runtime, first[0]["id"])
    assert (older["body"], older["revision"], older["origin"]) == (first[0]["body"], first[0]["revision"], "upload")


def test_two_entries_for_one_person_are_rejected_with_a_diagnostic_and_can_be_corrected(runtime):
    partners, michael = topic(runtime), person(runtime, "Michael Linford")
    line, note = "Mike Linford [10:00]: Hi.\n", {"title": "Hi", "material_type": "conversation", "paragraph_ids": [0], "topic_id": partners["id"]}
    twice = {"people": [{"key": "participant-1", "person_id": michael["id"], "paragraph_ids": [0]},
                        {"key": "michael", "person_id": michael["id"], "paragraph_ids": [0]}], "notes": [note]}
    once = {"people": [{"key": "participant-1", "person_id": michael["id"], "paragraph_ids": [0]}], "notes": [note]}
    upload = invoke(runtime, "context_upload", {"body": line, "origin": "call", "participants": [{"name": "Mike Linford"}]})
    failed = failed_filing(runtime, upload, twice)
    assert failed["metadata_json"]["status"] == "failed" and "each existing person_id once" in failed["metadata_json"]["error"]
    assert not current(runtime, michael["id"])["metadata"].get("aka") and len(runtime.model.calls) == 2
    runtime.model.calls.clear()
    runtime.model.answers.append(twice)
    notes, _ = file_upload(runtime, line, once, origin="call", participants=[{"name": "Mike Linford"}])
    assert "each existing person_id once" in json.loads(runtime.model.calls[1][0][1]["content"])["validation_error"]
    assert mentioned(runtime, notes[0]["id"]) == {michael["id"]} and list(current(runtime, michael["id"])["metadata"]["aka"]) == ["Mike Linford"]


def test_a_participant_key_without_a_people_entry_is_corrected_on_the_second_attempt(runtime):
    partners = topic(runtime)
    partial = {"notes": [{"title": "Kickoff", "material_type": "conversation", "paragraph_ids": [0], "topic_id": partners["id"], "people_keys": ["participant-1"]}]}
    runtime.model.answers.append(partial)
    notes, _ = file_upload(runtime, "Priya: Hello.\n", {"people": [{"key": "participant-1", "name": "Priya"}], "notes": partial["notes"]},
        origin="call", participants=[{"name": "Priya"}])
    assert "people_keys must reference" in json.loads(runtime.model.calls[1][0][1]["content"])["validation_error"]
    assert mentioned(runtime, notes[0]["id"]) == {people_named(runtime, "Priya")[0]["id"]}


def test_model_cannot_add_aliases_or_rename_people_outside_participants(runtime):
    partners, ada = topic(runtime), person(runtime, "Ada")
    file_upload(runtime, "Ada works at Newco.", {
        "people": [{"key": "ada", "person_id": ada["id"], "alias": "Lovelace", "aka": ["Countess"], "paragraph_ids": [0]}],
        "notes": [{"title": "Ada", "paragraph_ids": [0], "topic_id": partners["id"], "people_ids": [ada["id"]]}]})
    metadata = current(runtime, ada["id"])["metadata"]
    assert not metadata.get("aka") and metadata["name"] == "Ada" and metadata["filer_evidence"] == "Ada works at Newco."


def test_participant_names_come_from_the_owner_but_other_people_must_be_in_the_transcript(runtime):
    partners = topic(runtime)
    line = "Speaker A: Shall we start?\n"
    notes, _ = file_upload(runtime, line, {
        "people": [{"key": "participant-1", "name": "Priya Sharma"}],
        "notes": [{"title": "Kickoff", "material_type": "note", "paragraph_ids": [0], "topic_id": partners["id"], "people_keys": ["participant-1"]}]},
        origin="call", participants=[{"name": "Priya"}])
    created = people_named(runtime, "Priya")
    assert len(created) == 1 and not people_named(runtime, "Priya Sharma")
    assert created[0]["metadata_json"]["content_owner"] == "filer"
    assert set(notes[0]["metadata"]["people"]) == {created[0]["id"]}
    guess = {"people": [{"key": "ghost", "name": "Ghost Person"}],
             "notes": [{"title": "Kickoff", "paragraph_ids": [0], "topic_id": partners["id"], "people_keys": ["ghost"]}]}
    upload = invoke(runtime, "context_upload", {"body": "Speaker B: Yes.\n", "origin": "call", "participants": [{"name": "Priya"}]})
    assert "absent from the source" in failed_filing(runtime, upload, guess)["metadata_json"]["error"]


def test_archived_participant_fails_filing_and_retry_works_after_restoring_them(runtime):
    partners, michael = topic(runtime), person(runtime, "Michael Linford")
    upload = invoke(runtime, "context_upload", {"body": TWO, "origin": "call", "participants": [{"person_id": michael["id"], "name": "Mike"}]})
    invoke(runtime, "context_items_archive", {"item": {"type": "person", "id": michael["id"]}, "archived": True}, 1)
    answer = {"notes": [{"title": "Pilot", "material_type": "conversation", "paragraph_ids": [0, 1], "topic_id": partners["id"]}]}
    failed = failed_filing(runtime, upload, answer)
    assert failed["metadata_json"]["status"] == "failed"
    assert failed["metadata_json"]["error"] == "Participant is no longer a Person in this business"
    assert len(runtime.model.calls) == 2
    with runtime.database.read() as uow:
        assert not [row for row in uow.repository("documents", business_id=BUSINESS).list() if row["kind"] == "context_note"]
    invoke(runtime, "context_items_archive", {"item": {"type": "person", "id": michael["id"]}, "archived": False}, 2)
    runtime.model.answers.append(answer)
    retried = invoke(runtime, "context_retry", {"upload_id": upload["document"]["id"]}, failed["revision"])
    assert runtime.background.run_one(retried["job"]["id"])["state"] == "succeeded"
    with runtime.database.read() as uow:
        notes = [row for row in uow.repository("documents", business_id=BUSINESS).list() if row["kind"] == "context_note"]
    assert len(notes) == 1 and mentioned(runtime, notes[0]["id"]) == {michael["id"]}


def test_items_list_filters_derived_call_notes_by_origin_and_never_returns_the_raw_upload(runtime):
    partners = topic(runtime)
    call_notes, call_source = file_upload(runtime, TWO, {"notes": [
        {"title": "Pilot terms", "material_type": "conversation", "paragraph_ids": [0], "topic_id": partners["id"]},
        {"title": "Next step", "material_type": "conversation", "paragraph_ids": [1], "topic_id": partners["id"]}]},
        origin="call", participants=[{"name": "Priya"}])
    plain_notes, plain_source = file_upload(runtime, "Ada wants a pilot.\n", {"notes": [
        {"title": "Pilot", "material_type": "document", "paragraph_ids": [0], "topic_id": partners["id"]}]})
    def listed(**args):
        return {item["id"] for item in runtime.application.invoke(context(), "context_items_list", args).data["items"]}
    assert listed(origin="call") == {note["id"] for note in call_notes}
    assert listed(origin="upload") == {note["id"] for note in plain_notes}
    assert listed(origin="meeting") == set()
    assert {call_source["id"], plain_source["id"]}.isdisjoint(listed())
    assert {note["id"] for note in call_notes + plain_notes} <= listed()
    assert plain_notes[0]["origin"] == "upload" and plain_notes[0]["material_type"] == "document"


def prompts(runtime):
    messages = runtime.model.calls[0][0]
    return messages[0]["content"], json.loads(messages[1]["content"])


def test_prompt_carries_encounter_details_only_for_calls_and_meetings(runtime):
    partners = topic(runtime)
    file_upload(runtime, "Ada wants a pilot.\n", {"notes": [{"title": "Pilot", "paragraph_ids": [0], "topic_id": partners["id"]}]})
    system, data = prompts(runtime)
    assert "encounter" not in data and "transcript of a" not in system
    assert system.endswith("Never change existing business briefs, pillars, instructions or marketing content.")
    runtime.model.calls.clear()
    michael = person(runtime, "Michael Linford")
    file_upload(runtime, TWO, {"notes": [{"title": "Call", "paragraph_ids": [0, 1], "topic_id": partners["id"]}]},
        origin="call", occurred_on="2026-09-17", participants=[{"person_id": michael["id"], "name": "Mike"}, {"name": "Priya"}])
    system, data = prompts(runtime)
    assert data["encounter"] == {"origin": "call", "occurred_on": "2026-09-17", "participants": [
        {"key": "participant-1", "person_id": michael["id"], "name": "Mike"}, {"key": "participant-2", "name": "Priya"}]}
    assert "This upload is a transcript of a call." in system and "participant-1" not in system


def test_normalization_unions_in_order_without_duplicates_and_leaves_the_model_answer_intact():
    programs, picked, other = ContextPrograms(None, None), new_id(), new_id()
    snapshot = {"people": [{"id": picked}, {"id": other}]}
    source = {"metadata": {"origin": "call", "participants": [{"person_id": picked, "name": "Mike"}, {"name": "Priya"}]}}
    answer = {"people": [{"key": "x", "name": "X"}], "notes": [
        {"title": "A", "material_type": "document", "people_ids": [other, picked], "people_keys": ["x"]},
        {"title": "B", "people_keys": ["participant-2"]}]}
    original = json.loads(json.dumps(answer))
    result, typed = programs._apply_participants(answer, source, snapshot)
    assert answer == original and list(typed) == ["participant-2"]
    assert result["people"] == [{"key": "x", "name": "X"}, {"key": "participant-2", "name": "Priya"}]
    assert [(n["material_type"], n["people_ids"], n["people_keys"]) for n in result["notes"]] == [
        ("conversation", [other, picked], ["x", "participant-2"]), ("conversation", [picked], ["participant-2"])]
    ordinary, none = programs._apply_participants(answer, {"metadata": {"origin": "upload"}}, snapshot)
    assert ordinary is answer and none == {}
    gone = {"metadata": {"origin": "meeting", "participants": [{"person_id": new_id(), "name": "Gone"}]}}
    with pytest.raises(DomainError) as error:
        programs._apply_participants(answer, gone, snapshot)
    assert error.value.code == "validation_error" and "no longer a Person" in error.value.message



def test_http_upload_carries_encounter_fields_files_them_and_reports_bad_fields(tmp_path):
    model = Model()
    service = ServiceFixture(tmp_path, full=True, model_client_factory=lambda business: model)
    with TestClient(service.web) as client:
        service.login(client)
        def operation(name, args):
            result = client.post("/api/biz/widget/operations/" + name, json={**args, "idempotency_key": new_id()}, headers=ORIGIN)
            assert result.status_code == 200, result.text
            return result.json()["data"]
        def upload(body, status):
            result = client.post("/api/biz/widget/context/upload", json={**body, "idempotency_key": new_id()}, headers=ORIGIN)
            assert result.status_code == status, result.text
            return result.json()
        topic = operation("context_topics_create", {"title": "Partners", "slug": "partners"})["document"]
        michael = operation("context_people_create", {"name": "Michael Linford", "role": "Founder", "org": "Acme", "aka": ["Mike"]})["document"]
        operation("context_people_create", {"name": "Michelle Ortiz"})
        operation("context_people_create", {"name": "Priya Rao"})
        # The dashboard's participant typeahead uses the generic operation passthrough.
        found = operation("context_people_list", {"query": "mich", "limit": 8})
        assert {item["metadata"]["name"] for item in found["items"]} == {"Michael Linford", "Michelle Ortiz"}
        shown = next(item for item in found["items"] if item["id"] == michael["id"])["metadata"]
        assert (shown["role"], shown["org"], list(shown["aka"])) == ("Founder", "Acme", ["Mike"])
        participants = [{"person_id": michael["id"], "name": "Mike"}, {"name": "Priya"}]
        queued = upload({"text": TRANSCRIPT, "notes": "Pilot call notes", "topic": "Pilot call", "origin": "call",
                         "occurred_on": "2026-09-17", "participants": participants}, 202)
        assert queued["ok"] is True and queued["status"] == "accepted" and queued["path"].startswith("_inbox/")
        assert set(queued) >= {"ok", "id", "document_id", "path", "job"}
        stored = operation("context_get", {"document_id": queued["document_id"]})["document"]
        assert stored["metadata"]["title"] == "Pilot call" and stored["origin"] == "call"
        assert {key: stored["metadata"][key] for key in ("origin", "occurred_on")} == {"origin": "call", "occurred_on": "2026-09-17"}
        assert [dict(item) for item in stored["metadata"]["participants"]] == participants
        assert split_document((service.fixture.hq / "context" / queued["path"]).read_bytes())[0]["participants"] == participants
        model.answers.append({"notes": [{"title": "Pilot call", "material_type": "document", "paragraph_ids": [0], "topic_id": topic["id"]}]})
        assert wait_job(client, service, queued["job"]["id"])["state"] == "succeeded"
        notes = operation("context_items_list", {"origin": "call"})["items"]
        assert len(notes) == 1
        note = operation("context_get", {"document_id": notes[0]["id"]})["document"]
        priya = next(item for item in operation("context_people_list", {"query": "Priya"})["items"] if item["metadata"]["name"] == "Priya")
        assert note["material_type"] == "conversation" and note["metadata"]["occurred_on"] == "2026-09-17"
        assert set(note["metadata"]["people"]) == {michael["id"], priya["id"]}
        # Ordinary uploads are unchanged; bad or orphaned encounter fields keep the validation_error contract.
        plain = upload({"text": "Plain note.\n"}, 202)
        plain_meta = operation("context_get", {"document_id": plain["document_id"]})["document"]["metadata"]
        assert plain_meta["origin"] == "upload" and not {"participants", "occurred_on"} & set(plain_meta)
        unknown = upload({"text": "x", "bogus": 1}, 422)
        assert unknown["error"]["code"] == "validation_error" and unknown["error"]["field_errors"] == {"bogus": "unknown field"}
        assert "origin" in upload({"text": "x", "origin": "phone"}, 422)["error"]["field_errors"]
        assert upload({"text": "x", "participants": [{"name": "A"}]}, 422)["error"]["message"] == "Participants and dates apply to call or meeting uploads"
        assert upload({"text": "x", "origin": "call", "participants": [{"person_id": new_id(), "name": "Ghost"}]}, 404)["error"]["code"] == "not_found"
