import json

import pytest

from qorqut_operations.contracts import DomainError
from qorqut_operations.documents.evidence import extract, paragraphs, source_spans
from qorqut_operations.documents.serialization import document_bytes, split_document
from foundation_support import BUSINESS, context
from test_background_programs import runtime, invoke, job
from test_domain_documents import invoke as operation


def test_exact_spans_keep_leading_whitespace_unicode_and_conversation_boundaries():
    body = "\n\n  Luka [09:01]: Salam\u2014hello!\r\n\r\nAda: Yes.  \n\n[attachment: demo.png]\n"
    assert split_document(document_bytes({"title": "Upload"}, body))[1] == body
    chunks = paragraphs(body)
    assert "".join(chunks) == body
    spans = source_spans(chunks, list(range(len(chunks))))
    assert extract(body, spans, conversation=True) == body
    with pytest.raises(DomainError):
        extract(body, [{"start": 0, "end": 2}, {"start": 4, "end": len(body)}], conversation=True)
    with pytest.raises(DomainError):
        extract(body, [{"start": 0, "end": len(body) + 1}])


def file_upload(runtime, body, answer, **args):
    upload = invoke(runtime, "context_upload", {"body": body, **args})
    runtime.model.answers.append(answer)
    outcome = runtime.background.run_one(upload["job"]["id"])
    assert outcome["state"] == "succeeded", outcome
    with runtime.database.read() as uow:
        source = uow.repository("documents", business_id=BUSINESS).get(upload["document"]["id"])
        ids = source["metadata_json"]["result"]["notes"]
        return [runtime.domains.documents.store.view(uow.repository("documents", business_id=BUSINESS).get(identity), uow) for identity in ids], source


def test_mixed_upload_creates_exact_materials_and_only_background_changes(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Partners", "slug": "partners"})["document"]
    story = invoke(runtime, "stories_create", {"title": "Pilot", "purpose": "Test demand", "details": "Owner-authored plan."})["story"]
    conversation = "Luka [10:00]: Pilot?\nAda [10:01]: Yes!\n\n"
    document = "Product memo\nPricing is undecided.\n"
    notes, source = file_upload(runtime, conversation + document, {
        "notes": [{"title": "Pilot conversation", "material_type": "conversation", "paragraph_ids": [0], "topic_id": topic["id"], "story_ids": [story["id"]]},
                  {"title": "Pricing memo", "material_type": "document", "paragraph_ids": [1], "topic_id": topic["id"]}],
        "stories": [{"story_id": story["id"], "background": "Ada agreed to a pilot; pricing remains undecided."}]}, story_ids=[story["id"]])
    assert [note["body"] for note in notes] == [conversation, document]
    assert [note["metadata"]["material_type"] for note in notes] == ["conversation", "document"]
    with runtime.database.read() as uow:
        current = uow.repository("stories", business_id=BUSINESS).get(story["id"])
        assert current["details"] == "Owner-authored plan." and current["purpose"] == "Test demand"
        assert current["background"] == "Ada agreed to a pilot; pricing remains undecided."
        assert runtime.domains.documents.store.view(source, uow)["body"] == conversation + document
        edges = uow.repository("relationships", business_id=BUSINESS).list()
        assert len([edge for edge in edges if edge["role"] == "extracted_from"]) == 2
        assert not uow.repository("context_reviews", business_id=BUSINESS).list()
        assert not uow.repository("tasks", business_id=BUSINESS).list()
    listed = runtime.application.invoke(context(), "context_items_list", {}).data["items"]
    assert source["id"] not in {item["id"] for item in listed}
    assert {note["id"] for note in notes}.issubset({item["id"] for item in listed})


def test_filer_cannot_overwrite_existing_person_and_summary_text(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Partners", "slug": "partners"})["document"]
    person = invoke(runtime, "context_people_create", {"name": "Ada", "about": "Owner's biography", "notes": "Owner's exact notes", "summary": "Owner's summary"})["document"]
    file_upload(runtime, "Ada works at Newco.", {"notes": [{"title": "Ada", "paragraph_ids": [0], "topic_id": topic["id"], "people_ids": [person["id"]]}],
        "people": [{"key": "ada", "person_id": person["id"], "name": "Changed name", "org": "Newco", "paragraph_ids": [0]}]})
    current = runtime.application.invoke(context(), "context_people_get", {"document_id": person["id"]}).data["document"]
    assert current["body"] == "Owner's exact notes"
    assert current["metadata"]["name"] == "Ada" and current["metadata"]["about"] == "Owner's biography"
    assert current["metadata"]["summary"] == "Owner's summary"
    assert current["metadata"]["filer_evidence"] == "Ada works at Newco."
    runtime.model.answers.append("Ada works at Newco.")
    with runtime.database.read() as uow:
        summary_job = next(row for row in uow.repository("jobs", business_id=BUSINESS).list()
            if row["kind"] == "context.summarize" and row["arguments_json"]["document_id"] == person["id"])
    assert runtime.background.run_one(summary_job["id"])["state"] == "succeeded"
    current = runtime.application.invoke(context(), "context_people_get", {"document_id": person["id"]}).data["document"]
    assert current["body"] == "Owner's exact notes" and current["metadata"]["summary"] == "Owner's summary"
    assert current["metadata"]["filer_summary"] == "Ada works at Newco."


def test_two_people_entries_for_one_person_get_a_diagnostic_and_a_recorded_failure_not_a_stuck_upload(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Partners", "slug": "partners"})["document"]
    ada = invoke(runtime, "context_people_create", {"name": "Ada Lovelace"})["document"]
    note = {"title": "Hi", "material_type": "conversation", "paragraph_ids": [0], "topic_id": topic["id"]}
    entry = {"key": "a", "person_id": ada["id"], "paragraph_ids": [0]}
    twice = {"people": [entry, {**entry, "key": "b"}], "notes": [{**note, "people_keys": ["a", "b"]}]}
    once = {"people": [entry], "notes": [{**note, "people_keys": ["a"]}]}
    # Two bad answers: the upload ends failed with the reason recorded and can be retried, instead of staying pending.
    upload = invoke(runtime, "context_upload", {"body": "Ada Lovelace [10:00]: Hi.\nAda [10:01]: Hello.\n"})
    runtime.model.answers.extend([twice, twice])
    assert runtime.background.run_one(upload["job"]["id"])["state"] == "failed"
    with runtime.database.read() as uow:
        failed = uow.repository("documents", business_id=BUSINESS).get(upload["document"]["id"])
    assert failed["metadata_json"]["status"] == "failed" and "each existing person_id once" in failed["metadata_json"]["error"]
    assert len(runtime.model.calls) == 2
    # One bad answer, then a corrected one: the second prompt carries the diagnostic and the Person is written once.
    runtime.model.calls.clear()
    runtime.model.answers.append(twice)
    notes, _ = file_upload(runtime, "Ada Lovelace [11:00]: Again.\n", once)
    assert "each existing person_id once" in json.loads(runtime.model.calls[1][0][1]["content"])["validation_error"]
    assert len(notes) == 1
    current = runtime.application.invoke(context(), "context_people_get", {"document_id": ada["id"]}).data["document"]
    assert current["metadata"]["filer_evidence"] == "Ada Lovelace [11:00]: Again.\n"


def test_explicit_continuation_appends_and_authored_material_is_protected(runtime):
    topic = invoke(runtime, "context_topics_create", {"title": "Partners", "slug": "partners"})["document"]
    first, _ = file_upload(runtime, "Thread #17\nAda: Hello.\n", {"notes": [{"title": "Thread #17", "material_type": "conversation", "paragraph_ids": [0], "topic_id": topic["id"]}]})
    second, _ = file_upload(runtime, "Thread #17 continued\nLuka: Hi!\n", {"notes": [{"title": "Thread #17", "material_type": "conversation", "paragraph_ids": [0], "topic_id": topic["id"], "material_id": first[0]["id"]}]})
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["body"] == first[0]["body"] + "\n\nThread #17 continued\nLuka: Hi!\n"
    assert len(second[0]["source_refs"]) == 2
    edited = operation(runtime.application, "context_document_update", {"document_id": second[0]["id"], "body": "My own replacement"}, revision=second[0]["revision"])
    assert edited.status == "ok", edited.to_dict()
    with runtime.database.read() as uow:
        row = uow.repository("documents", business_id=BUSINESS).get(second[0]["id"])
        assert row["metadata_json"]["content_owner"] == context().principal_id
