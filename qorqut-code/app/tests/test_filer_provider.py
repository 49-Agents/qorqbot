"""Opt-in real-provider checks; only synthetic uploads leave this test database."""

import os
from pathlib import Path

import pytest

from bazarlyk.background.providers import BackgroundModelClient, ProviderConfig
from bazarlyk.service.local import LocalFiles
from foundation_support import BUSINESS, context
from test_background_programs import runtime, invoke


@pytest.mark.skipif(os.environ.get("QORQUT_TEST_FILER_LIVE") != "1", reason="Real provider test is opt-in")
@pytest.mark.parametrize("mixed", [False, True])
def test_provider_preserves_conversation_and_disambiguates_mixed_upload(runtime, mixed):
    values = dict(os.environ)
    if path := values.get("QORQUT_FILER_ENV_FILE"):
        files = LocalFiles(None)
        files.env_path = lambda business: Path(path)
        values.update(files.environment(BUSINESS))
    client = BackgroundModelClient(ProviderConfig.from_environment(values))
    runtime.model.chat = client.chat
    runtime.model.config = client.config
    invoke(runtime, "context_topics_create", {"title": "Pilot feedback", "slug": "pilot-feedback"})
    conversation = ("Conversation: Synthetic pilot interview, 2026-09-01\n"
                    "Luka [10:00]: Can you try the prototype?\n\n"
                    "Alex Chen (Northstar Labs) [10:01]: Yes, but the export must keep Unicode: caf\u00e9.\n\n"
                    "Luka [10:02]: Understood. Nothing has been promised yet.\n\n")
    memo = ("Separate document: Internal pricing memo\n"
            "The trial price is undecided. Alex Rivera at Riverworks is a different person "
            "from Alex Chen at Northstar Labs. This memo is not part of the interview.\n") if mixed else ""
    body = conversation + memo
    upload = invoke(runtime, "context_upload", {"title": "Synthetic mixed evidence" if mixed else "Synthetic interview", "body": body})
    outcome = runtime.background.run_one(upload["job"]["id"])
    assert outcome["state"] == "succeeded", outcome
    with runtime.database.read() as uow:
        source = uow.repository("documents", business_id=BUSINESS).get(upload["document"]["id"])
        notes = [runtime.domains.documents.store.view(uow.repository("documents", business_id=BUSINESS).get(identity), uow)
                 for identity in source["metadata_json"]["result"]["notes"]]
        conversations = [note for note in notes if note["metadata"]["material_type"] == "conversation"]
        assert len(conversations) == 1
        assert conversations[0]["body"] == conversation
        assert sum(len(note["body"]) for note in notes) == len(body)
        if mixed:
            assert len(notes) >= 2
            assert any(note["body"] == memo for note in notes)
        people = [person for person in uow.repository("documents", business_id=BUSINESS).list() if person["kind"] == "context_person"]
        for person in people:
            evidence = runtime.domains.documents.store.view(person, uow)["body"]
            assert not (person["metadata_json"].get("name") == "Alex" and "Northstar" in evidence and "Riverworks" in evidence)
        assert runtime.domains.documents.store.view(source, uow)["body"] == body
        assert not uow.repository("tasks", business_id=BUSINESS).list()
        assert not uow.repository("context_reviews", business_id=BUSINESS).list()
    graph = runtime.application.invoke(context(), "context_graph_get", {}).data
    assert source["id"] not in {item["id"] for item in graph["nodes"]}
