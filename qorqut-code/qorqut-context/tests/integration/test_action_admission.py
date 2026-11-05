"""Actions: immutable revisions, correction and withdrawal, scoped reads, and one transaction with the Context queue input."""

from __future__ import annotations

import pytest

from operational_support import (AGENT, AGENT2, BUSINESS, COORDINATOR, HUMAN, OTHER, OTHER_AGENT, OperationalHost, STRANGER)
from qorqut_context.integration import queue as context_queue
from qorqut_operations.contracts import DomainError

ASSIGNMENT = "00000000-0000-4000-8000-0000000000e1"
WORDS_21 = " ".join(f"word{index}" for index in range(21))


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def inputs(host):
    return host.query("SELECT * FROM context_inputs ORDER BY sequence")


def revisions(host, key):
    return host.query("SELECT * FROM action_revisions WHERE action_key=? ORDER BY revision", key)


def envelope(host, *labels):
    """The exact Context tag snapshots ``{id, name, business_id}`` for catalog labels, in the one deterministic order."""
    catalog = {row["label"]: row["id"] for row in host.query("SELECT id, label FROM tags")}
    return sorted(({"id": catalog[label], "name": label, "business_id": None} for label in labels),
                  key=lambda tag: (tag["name"].casefold(), tag["id"]))


def test_a_record_is_revision_one_and_queues_exactly_that_revision(host):
    action = host.record_action("Shipped the release", tags=["Launch", "Research"],
                                related_objects=[{"kind": "node", "id": "ab12c", "explanation": "the release"}],
                                evidence={"commit": "abc123"})
    assert action["revision"] == 1 and action["withdrawn"] is False and action["origin"] == "agent"
    assert action["category"] == "business-operational" and [business["id"] for business in action["businesses"]] == [BUSINESS]
    assert action["action_id"] == f"business/{BUSINESS}/" + action["legacy"]["id"] and sorted(action["tags"]) == ["Launch", "Research"]
    [queued] = inputs(host)
    assert queued["input_kind"] == "action" and queued["state"] == "queued" and queued["business_id"] == BUSINESS
    assert queued["item_id"] == action["action_id"] and queued["revision"] == 1 and queued["operation"] == "add"
    assert queued["submitted_by"] == AGENT and queued["context_event_id"] is None and queued["attempt"] == 0
    payload = queued["payload_json"]
    assert payload == {
        "action_ref": action["action_id"], "revision_ref": action["action_id"] + ":1", "operation": "add", "submitted_by": AGENT,
        "tags": envelope(host, "Research", "Launch"), "related_objects": [{"kind": "node", "id": "ab12c", "explanation": "the release"}],
        "scope": {"kind": "business", "business_id": BUSINESS},
        "provenance": {"performed_by": AGENT, "recorded_by": AGENT, "origin": "agent", "category": "business-operational",
                       "via": "actions_record"},
        "text": "Shipped the release"}
    assert "refs" not in payload and "commit" not in str(payload)       # evidence ids stay with the Action, not in the queued envelope
    [revision] = revisions(host, action["action_id"])
    assert revision["operation"] == "add" and revision["snapshot_json"]["text"] == "Shipped the release"
    # The revision froze stable catalog ids and display names; ordinary tags are shared, so no tag carries a business.
    assert revision["snapshot_json"]["tag_snapshots"] == envelope(host, "Research", "Launch") and "tags" not in revision["snapshot_json"]
    assert all(tag["business_id"] is None for tag in payload["tags"]) and payload["scope"] == {"kind": "business", "business_id": BUSINESS}


def test_the_agent_word_rule_and_blank_text(host):
    assert host.error("actions_record", {"text": WORDS_21}, host.agent()) == "validation_error"
    assert host.error("actions_record", {"text": "   "}, host.agent()) == "validation_error"
    assert host.record_action(" ".join(["word"] * 20))["text"].count("word") == 20
    owner = host.data("actions_record", {"text": WORDS_21, "category": "global-systems"}, host.owner(None))["action"]
    assert owner["origin"] == "manual" and owner["business_id" if False else "category"] == "global-systems"
    assert len(inputs(host)) == 2


def test_categories_and_businesses_follow_the_caller(host):
    assert host.error("actions_record", {"text": "Did it", "business_ids": [OTHER]}, host.agent()) == "forbidden"
    assert host.error("actions_record", {"text": "Did it", "category": "global-operational"}, host.agent()) == "forbidden"
    assert host.error("actions_record", {"text": "Did it"}, host.coordinator()) == "validation_error"            # a global agent chooses
    assert host.error("actions_record", {"text": "Did it", "category": "global-systems"}, host.coordinator()) == "forbidden"
    assert host.error("actions_record", {"text": "Did it", "category": "business-operational"}, host.coordinator()) == "validation_error"
    shared = host.data("actions_record", {"text": "Updated both", "category": "business-operational", "business_ids": [BUSINESS, OTHER]},
                       host.coordinator())["action"]
    assert shared["action_id"].startswith("global/") and {business["id"] for business in shared["businesses"]} == {BUSINESS, OTHER}
    assert inputs(host)[0]["business_id"] is None            # one owning namespace: Global storage, not either business
    # Each named business reads the shared text; an unrelated business sees nothing.
    assert host.read("actions_get", {"action_id": shared["action_id"]}, host.agent())["action"]["text"] == "Updated both"
    assert host.read("actions_get", {"action_id": shared["action_id"]}, host.agent(OTHER_AGENT, OTHER))["action"]["text"] == "Updated both"
    host.record_action("Own work", ctx=host.agent(OTHER_AGENT, OTHER))
    assert [entry["text"] for entry in host.read("actions_list", {}, host.agent())["actions"]] == ["Updated both"]


def test_shared_evidence_is_disclosed_only_to_readers_of_every_linked_business(host):
    shared = host.data("actions_record", {"text": "Updated both", "category": "business-operational", "business_ids": [BUSINESS, OTHER],
                                          "evidence": {"url": "https://example.test/x"}, "tags": ["Shared"]}, host.coordinator())["action"]
    mine = host.read("actions_get", {"action_id": shared["action_id"]}, host.agent())["action"]
    assert mine["refs"] == {} and mine["tags"] == [] and mine["legacy"] is None and [b["id"] for b in mine["businesses"]] == [BUSINESS]
    full = host.read("actions_get", {"action_id": shared["action_id"]}, host.owner(None))["action"]
    assert full["refs"] == {"url": "https://example.test/x"} and full["tags"] == ["Shared"] and len(full["businesses"]) == 2


def test_a_correction_is_the_next_immutable_revision_and_is_queued_as_edit(host):
    action = host.record_action("Sent the draft", tags=["Draft"])
    corrected = host.data("actions_correct", {"action_id": action["action_id"], "text": "Sent the final", "tags": ["Final"],
                                              "related_objects": [{"kind": "edge", "id": "ze9z1"}]},
                          host.agent(), expected_revision=action["revision"])["action"]
    assert corrected["revision"] == 2 and corrected["text"] == "Sent the final" and corrected["tags"] == ["Final"]
    first, second = revisions(host, action["action_id"])
    assert first["snapshot_json"]["text"] == "Sent the draft" and first["snapshot_json"]["tag_snapshots"] == envelope(host, "Draft")
    assert second["operation"] == "edit" and second["snapshot_json"]["text"] == "Sent the final"
    queue = inputs(host)
    assert [(row["revision"], row["operation"]) for row in queue] == [(1, "add"), (2, "edit")]
    assert queue[0]["payload_json"]["text"] == "Sent the draft"                                  # the queued add is not rewritten
    assert queue[0]["payload_json"]["tags"] == envelope(host, "Draft") and queue[1]["payload_json"]["tags"] == envelope(host, "Final")
    # The earlier revision's payload is a function of its stored revision alone: the Action no longer carries Draft.
    assert host.read("actions_get", {"action_id": action["action_id"]}, host.agent())["action"]["tags"] == ["Final"]
    assert context_queue.action_payload(first) == queue[0]["payload_json"]
    assert queue[1]["payload_json"]["related_objects"] == [{"kind": "edge", "id": "ze9z1"}] and queue[1]["sequence"] > queue[0]["sequence"]
    assert host.read("actions_get", {"action_id": action["action_id"], "revision": 1}, host.agent())["action"]["text"] == "Sent the draft"
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE action_revisions SET operation='edit' WHERE action_key=?", (action["action_id"],))
    # A correction that changes nothing is refused rather than minting an identical revision.
    assert host.error("actions_correct", {"action_id": action["action_id"], "text": "Sent the final"}, host.agent(), expected_revision=2) == "validation_error"
    assert host.error("actions_correct", {"action_id": action["action_id"], "text": "Late"}, host.agent(), expected_revision=1) == "revision_conflict"


def test_the_tag_envelope_is_frozen_per_revision_and_exact(host):
    action = host.record_action("Tagged result", tags=["Alpha", "beta"])
    host.data("actions_withdraw", {"action_id": action["action_id"]}, host.agent(), expected_revision=1)
    add, remove = inputs(host)
    # A withdrawal keeps the tags its revision froze; both queued payloads hold exact {id, name, business_id} snapshots.
    assert add["payload_json"]["tags"] == remove["payload_json"]["tags"] == envelope(host, "Alpha", "beta")
    assert all(set(tag) == {"id", "name", "business_id"} and tag["business_id"] is None for tag in remove["payload_json"]["tags"])
    # The admission check rejects the earlier label-list shape and any envelope that is not the stored revision's.
    [stored, _] = revisions(host, action["action_id"])
    exact = context_queue.action_payload(stored)
    for tags in (["Alpha", "beta"], exact["tags"][:1], [{**tag, "business_id": BUSINESS} for tag in exact["tags"]]):
        with pytest.raises(DomainError) as refused:
            with host.database.transaction() as uow:
                context_queue.enqueue_action(uow, action_id=action["action_id"], revision=1, operation="add", payload={**exact, "tags": tags})
        assert refused.value.code == "validation_error"
    assert len(inputs(host)) == 2


def test_withdrawal_keeps_history_queues_a_removal_and_is_final(host):
    action = host.record_action("Posted the update")
    withdrawn = host.data("actions_withdraw", {"action_id": action["action_id"], "rationale": "Posted by mistake"}, host.agent(),
                          expected_revision=1)["action"]
    assert withdrawn["withdrawn"] is True and withdrawn["revision"] == 2 and withdrawn["can_change"] is False
    removal = inputs(host)[1]
    assert removal["operation"] == "remove" and removal["revision"] == 2
    assert "text" not in removal["payload_json"] and removal["payload_json"]["related_objects"] == []
    assert host.error("actions_correct", {"action_id": action["action_id"], "text": "Again"}, host.agent(), expected_revision=2) == "invalid_transition"
    assert host.error("actions_withdraw", {"action_id": action["action_id"]}, host.agent(), expected_revision=2) == "invalid_transition"
    assert host.read("actions_list", {}, host.agent())["actions"] == []
    listed = host.read("actions_list", {"include_withdrawn": True}, host.agent())["actions"]
    assert [entry["withdrawn"] for entry in listed] == [True] and host.read("actions_get", {"action_id": action["action_id"]}, host.agent())["action"]["text"]
    # Later activity is a new Action, not a revival.
    assert host.record_action("Posted the corrected update")["revision"] == 1
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            uow.connection.execute("DELETE FROM action_logs WHERE action_key=?", (action["action_id"],))


def test_the_row_revision_and_queue_input_are_written_in_one_transaction(host, monkeypatch):
    def refuse(uow, **kwargs):
        raise DomainError("operation_failed", "the queue refused the input")

    monkeypatch.setattr("qorqut_context.integration.queue.queue.enqueue_action", refuse)
    assert host.error("actions_record", {"text": "Will not stick"}, host.agent()) == "operation_failed"
    assert host.count("action_logs") == 0 and host.count("action_revisions") == 0 and host.count("context_inputs") == 0
    assert host.count("item_tags") == 0 and host.count("inbox_events") == 0
    monkeypatch.undo()
    action = host.record_action("Sticks")
    monkeypatch.setattr("qorqut_context.integration.queue.queue.enqueue_action", refuse)
    assert host.error("actions_correct", {"action_id": action["action_id"], "text": "Changed"}, host.agent(), expected_revision=1) == "operation_failed"
    assert host.query("SELECT revision, action_text FROM action_logs")[0] == {"revision": 1, "action_text": "Sticks"}
    assert host.count("action_revisions") == 1 and host.count("context_inputs") == 1


def test_the_queue_entry_point_accepts_only_the_exact_stored_revision(host):
    action = host.record_action("Exact")
    payload = inputs(host)[0]["payload_json"]
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            context_queue.enqueue_action(uow, action_id=action["action_id"], revision=1, operation="add", payload={**payload, "text": "Forged"})
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            context_queue.enqueue_action(uow, action_id=action["action_id"], revision=2, operation="edit", payload=payload)
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            context_queue.enqueue_action(uow, action_id=action["action_id"], revision=1, operation="remove", payload=payload)
    with pytest.raises(Exception):          # a revision is queued once
        with host.database.transaction() as uow:
            context_queue.enqueue_action(uow, action_id=action["action_id"], revision=1, operation="add", payload=payload)
    assert host.count("context_inputs") == 1


def test_correction_and_withdrawal_belong_to_the_recorder_or_the_owner(host):
    action = host.record_action("Mine", ctx=host.agent(AGENT))
    assert host.error("actions_correct", {"action_id": action["action_id"], "text": "Theirs"}, host.agent(AGENT2), expected_revision=1) == "forbidden"
    assert host.error("actions_withdraw", {"action_id": action["action_id"]}, host.agent(OTHER_AGENT, OTHER), expected_revision=1) in {"not_found", "forbidden"}
    assert host.error("actions_correct", {"action_id": action["action_id"], "text": "Theirs"}, host.stranger(), expected_revision=1) in {"forbidden", "policy_denied"}
    corrected = host.data("actions_correct", {"action_id": action["action_id"], "text": "Owner fixed it"}, host.owner(BUSINESS), expected_revision=1)["action"]
    assert corrected["text"] == "Owner fixed it" and corrected["recorded_by"]["id"] == AGENT       # the original recorder is kept
    assert inputs(host)[1]["submitted_by"] == HUMAN                                                 # the revision names who submitted it
    # Policy still decides: an agent without the grant cannot correct even its own Action.
    host.agents.overrides["actions_correct"] = "deny"
    assert host.error("actions_correct", {"action_id": action["action_id"], "text": "Mine again"}, host.agent(AGENT), expected_revision=2) in {"forbidden", "policy_denied"}


def test_protected_corrections_stage_a_signoff_and_apply_once_approved(host):
    action = host.record_action("Needs review")
    host.agents.overrides["actions_correct"] = "signoff"
    signoff_id = host.stage("actions_correct", {"action_id": action["action_id"], "text": "Reviewed text"}, expected_revision=1)
    assert len(inputs(host)) == 1 and host.query("SELECT action_text FROM action_logs")[0]["action_text"] == "Needs review"
    assert host.apply_approved(signoff_id)["state"] == "applied"
    assert [(row["revision"], row["operation"]) for row in inputs(host)] == [(1, "add"), (2, "edit")]
    assert inputs(host)[1]["payload_json"]["text"] == "Reviewed text" and inputs(host)[1]["submitted_by"] == AGENT


def test_filters_pagination_and_the_config_read(host):
    for index in range(5):
        host.record_action(f"Result {index}", tags=["Even" if index % 2 == 0 else "Odd"], real_time=f"2026-10-0{index + 1}")
    page = host.read("actions_list", {"limit": 2}, host.agent())
    assert [entry["text"] for entry in page["actions"]] == ["Result 4", "Result 3"] and page["next_cursor"]
    rest = host.read("actions_list", {"limit": 5, "cursor": page["next_cursor"]}, host.agent())
    assert [entry["text"] for entry in rest["actions"]] == ["Result 2", "Result 1", "Result 0"]
    assert [entry["text"] for entry in host.read("actions_list", {"tags": ["odd"]}, host.agent())["actions"]] == ["Result 3", "Result 1"]
    assert [entry["text"] for entry in host.read("actions_list", {"since": "2026-10-04"}, host.agent())["actions"]] == ["Result 4", "Result 3"]
    config = host.read("actions_config_get", {}, host.agent())
    assert config["agent_word_limit"] == 20 and config["scope"]["default_category"] == "business-operational"
    assert config["tags"] == {"source": "catalog", "operation": "tags_list"}


def test_related_objects_are_context_pointers_and_never_required(host):
    bad = [{"kind": "node", "id": "TOO-LONG"}], [{"kind": "page", "id": "ab12c"}], [{"kind": "node", "id": "ab12c"}, {"kind": "node", "id": "ab12c"}]
    for related in bad:
        assert host.error("actions_record", {"text": "Pointers", "related_objects": related}, host.agent()) == "validation_error"
    assert host.record_action("No pointers")["related_objects"] == []


def test_a_delegated_result_is_recorded_once_with_assignment_provenance(host):
    host.delegation.rows[ASSIGNMENT] = {"id": ASSIGNMENT, "parent_principal_id": AGENT, "state": "completed", "result_json": {"summary": "done"},
                                     "scope_json": {"kind": "business"}, "business_id": BUSINESS, "execution_generation": 3}
    action = host.record_action("Worker finished", delegation_assignment_id=ASSIGNMENT)
    assert action["performed_by"] is None and action["attribution"] == "recorded" and action["recorded_by"]["id"] == AGENT
    assert inputs(host)[0]["payload_json"]["provenance"]["delegation_assignment"] == ASSIGNMENT
    assert inputs(host)[0]["payload_json"]["provenance"]["performed_by"] is None
    assert host.error("actions_record", {"text": "Again", "delegation_assignment_id": ASSIGNMENT}, host.agent()) == "conflict"
    assert host.error("actions_record", {"text": "Again", "delegation_assignment_id": "00000000-0000-4000-8000-0000000000e2"}, host.agent()) == "not_found"


def test_following_an_action_reaches_followers_on_correction_and_withdrawal(host):
    action = host.record_action("Followed", ctx=host.agent(AGENT))
    host.data("actions_follow", {"action_id": action["action_id"], "following": True}, host.agent(AGENT2))
    host.data("actions_correct", {"action_id": action["action_id"], "text": "Followed again"}, host.owner(BUSINESS), expected_revision=1)
    assert [(row["agent_id"], row["event_type"], row["required"]) for row in host.deliveries(source_type="actions")
            if row["event_type"] != "created"] == [(AGENT, "updated", 0), (AGENT2, "updated", 0)]
    host.data("actions_follow", {"action_id": action["action_id"], "following": False}, host.agent(AGENT2))
    host.data("actions_withdraw", {"action_id": action["action_id"]}, host.owner(BUSINESS), expected_revision=2)
    assert [row["agent_id"] for row in host.deliveries(source_type="actions") if row["event_type"] == "withdrawn"] == [AGENT]
