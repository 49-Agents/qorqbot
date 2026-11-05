"""Customer projections, Task ports and record links through the real composed host, in business and Global scope.

Qualification of operations-f6663c03 with the host (no stub between request and database):

* the compact projections (Focus card, related-Task rows, children, parent, overview, activity) carry a Customer's kind and
  state label and never its profile or notes, while the authorized ``tasks_get``/``tasks_list`` detail rows are complete;
* the host Task ports (assignees, Focus, owner work routing, campaign rules, setup guard) keep working under the engine's stricter
  rule that a Task plan may change ``business_configs`` only by the one vocabulary append (a port may still pin the ``tasks``
  configuration to read it);
* a Customer links to other Tasks (``related_task``), to a same-scope email conversation and to registered Context material as
  ``concerns`` relationships, with the operation names and arguments ``record-panel.js`` sends, never widening access.

Synthetic only: a tmp instance and rows a test appends (a real registered note, a real email conversation row); no model, provider,
network, native session or operational data."""

import json
from dataclasses import replace

import pytest

from customer_host_support import (NOTES, PROFILE, choices, code, customer, data, host, invoke, ok, operate, post, read, refused, rows,
                                   seed_email, snapshot, task, vocabulary)  # noqa: F401  (host is the fixture)

CAMPAIGN = "marketing-campaign"


def link_task(host, source, target, slug="first", *, linked=True):
    """The Task links panel's write: ``related_task`` on ``source`` naming the other Task and its owning business."""
    return data(operate(host, slug, "tasks_update", task_id=source["id"], expected_revision=source["revision"], changes={
        "related_task": {"business_id": target["business_id"], "task_id": target["id"], "linked": linked}}))["task"]


def scope_of(slug):
    return {} if slug else {"scope": "global"}


def concerns(host, slug, record):
    """What ``recordRelated`` reads: the Task's ``concerns`` edges, both directions, exactly as the panel asks."""
    page = data(operate(host, slug, "relationships_list", item={"type": "task", "id": record["id"]}, role="concerns", direction="both",
                        include_archived=False, limit=50, **scope_of(slug)))
    return page["items"]


def link_to(host, slug, record, target):
    """``recordRelated.link``: the new edge runs from the linked item to the Task."""
    return data(operate(host, slug, "relationships_create", source=target, target={"type": "task", "id": record["id"]}, role="concerns",
                        **scope_of(slug)))["relationship"]


def unlink(host, slug, edge):
    return data(operate(host, slug, "relationships_remove", relationship_id=edge["id"], expected_revision=edge["revision"],
                        **scope_of(slug)))


def other_end(edge, record):
    return edge["target_item"] if edge["source"]["type"] == "task" and edge["source"]["id"] == record["id"] else edge["source_item"]


# -- compact projections, complete detail ----------------------------------------------------------------------------------

def test_every_compact_projection_shows_kind_and_state_and_never_the_profile(host):
    parent = task(host, title="Account plan")
    record = customer(host, customer_state="Prospect")
    story = task(host, title="Relationship", task_type="story", overview="Next steps")
    child = task(host, title="Send proposal", parent_id=record["id"])
    sub = customer(host, title="Child customer", customer_kind="person", customer_state="Lead", customer_profile=PROFILE)
    sub = data(operate(host, "first", "tasks_update", task_id=sub["id"], expected_revision=sub["revision"], changes={"parent_id": parent["id"]}))["task"]
    story = link_task(host, story, record)
    update = data(operate(host, "first", "tasks_update", task_id=record["id"], expected_revision=record["revision"],
                          changes={"customer_state": "Active", "customer_profile": {**PROFILE, "notes": NOTES + " (updated)"}}))["task"]

    # Focus: both the presented card and the later read, from the Global owner route the dashboard uses.
    for step in ("present", "get"):
        if step == "present":
            answer = data(operate(host, None, "focus_present", scope="global", reason="Needs a look", requested_response="Decide",
                                  source={"type": "task", "id": record["id"], "business_id": host.first}))["item"]
            item_id = answer["id"]
        else:
            answer = data(operate(host, None, "focus_get", scope="global", item_id=item_id))["item"]
        source = answer["resolved_source"]
        assert (source["task_type"], source["customer_kind"], source["customer_state"]) == ("customer", "company", "Active")
        assert NOTES not in json.dumps(answer) and "customer_profile" not in source and "buyer@example.test" not in json.dumps(answer)
    # An ordinary Task and a Story gain no Customer keys on the same path.
    plain_card = data(operate(host, None, "focus_present", scope="global", reason="Look", requested_response="Decide",
                              source={"type": "task", "id": child["id"], "business_id": host.first}))["item"]["resolved_source"]
    assert plain_card["task_type"] == "task" and not {"customer_kind", "customer_state"} & set(plain_card)

    # Related-Task rows (both directions) and the child/parent summaries.
    story_row = next(row for row in read(host, story["id"])["related_tasks"] if row["task_id"] == record["id"])
    assert (story_row["task_type"], story_row["customer_kind"], story_row["customer_state"]) == ("customer", "company", "Active")
    assert "customer_profile" not in story_row and NOTES not in json.dumps(story_row)
    back = next(row for row in read(host, record["id"])["related_tasks"] if row["task_id"] == story["id"])
    assert (back["task_type"], back["customer_kind"], back["customer_state"]) == ("story", None, "") and "customer_profile" not in back
    children = read(host, parent["id"])["children"]
    assert [row["id"] for row in children] == [sub["id"]] and NOTES not in json.dumps(children)
    assert NOTES not in json.dumps(read(host, child["id"])["parent"])

    # Overview, activity and the update's own system entries never carry the profile either.
    assigned = task(host, title="Assigned customer", task_type="customer", customer_state="Lead",
                    customer_profile={"notes": NOTES}, assignee_ids=[host.agent])
    overview = ok(invoke(host, replace(host.agent_context, business_id=None, review_business_id=None), "tasks_overview"))
    entries = [entry for scope in overview["scopes"] for key in ("active", "snoozed") for entry in scope[key]]
    shown = next(entry for entry in entries if entry["id"] == assigned["id"])
    assert shown["task_type"] == "customer" and NOTES not in json.dumps(overview) and "customer_profile" not in json.dumps(overview)
    thread = data(operate(host, "first", "tasks_thread_get", task_id=record["id"], limit=20))
    assert NOTES not in json.dumps(thread) and "customer_profile" not in json.dumps(thread)

    # The authorized detail rows are complete.
    detail = read(host, record["id"])
    listed = next(row for row in data(operate(host, "first", "tasks_list", task_type="customer", include_closed=True))["tasks"]
                  if row["id"] == record["id"])
    for shown in (detail, listed, update):
        assert shown["customer_profile"] == {**PROFILE, "notes": NOTES + " (updated)"} and shown["customer_state"] == "Active"


def test_a_customer_in_a_business_the_reader_lost_access_to_vanishes_from_related_tasks(host):
    both = replace(host.owner, allowed_business_ids=frozenset({host.first, host.second}))
    mine = customer(host, customer_state="Prospect")
    theirs = customer(host, "second", title="Their customer", customer_state="Their label",
                      customer_profile={"notes": "THEIR-PRIVATE-NOTES"})
    linked = ok(invoke(host, both, "tasks_update", {"task_id": mine["id"], "changes": {"related_task": {
        "business_id": host.second, "task_id": theirs["id"], "linked": True}}}, revision=mine["revision"]))["task"]
    [row] = linked["related_tasks"]
    assert (row["task_id"], row["business_id"], row["customer_kind"], row["customer_state"]) == (theirs["id"], host.second, "company", "Their label")
    assert "customer_profile" not in row and "THEIR-PRIVATE-NOTES" not in json.dumps(linked)
    # Removing the reader's membership of that business revokes the cross-scope read: no row, and nothing of the Customer.
    with host.instance.database.transaction() as uow:
        member = uow.repository("business_memberships", business_id=host.second).get(host.instance.owner)
        uow.repository("business_memberships", business_id=host.second).update(host.instance.owner, {"active": False},
                                                                             expected_revision=member["revision"])
    again = ok(invoke(host, both, "tasks_get", {"task_id": mine["id"]}))["task"]
    assert again["related_tasks"] == [] and "THEIR-PRIVATE-NOTES" not in json.dumps(again) and "Their label" not in json.dumps(again)


# -- ports ----------------------------------------------------------------------------------------------------------------

def test_the_host_task_ports_work_with_customers_and_the_vocabulary_append(host):
    # Assignees, Focus and the tasks_assign/complete path (a new label in the same plan as the assignment).
    assigned = ok(invoke(host, host.owner, "tasks_create", {"title": "Assigned customer", "task_type": "customer", "customer_state": "Lead",
                                                          "assignee_ids": [host.agent]}))["task"]
    assert assigned["assignee_ids"] == [host.agent] and choices(host, "first") == ["Lead"]
    reassigned = ok(invoke(host, host.owner, "tasks_assign", {"task_id": assigned["id"], "assignee_ids": []}, revision=assigned["revision"]))["task"]
    assert reassigned["assignee_ids"] == [] and reassigned["customer_state"] == "Lead"
    done = ok(invoke(host, host.owner, "tasks_complete", {"task_id": assigned["id"]}, revision=reassigned["revision"]))["task"]
    assert done["status"] == "done" and done["customer_state"] == "Lead"
    # Owner-request routing (the owner work port) with a Customer that introduces a label.
    owner_request = ok(invoke(host, host.owner, "tasks_create", {"title": "Owner asked", "task_type": "customer", "kind": "owner_request",
                                                               "customer_state": "Requested", "assignee_ids": [host.instance.owner]}))["task"]
    assert owner_request["kind"] == "owner_request" and choices(host, "first") == ["Lead", "Requested"]
    # The campaign rules pin the scope's Task configuration inside the same plan; the vocabulary append still applies exactly.
    campaign = ok(invoke(host, host.agent_context, "tasks_create", {"title": "Customer campaign", "task_type": "customer", "tags": [CAMPAIGN],
                                                                  "customer_state": "Campaign"}))["task"]
    assert campaign["campaign"] and choices(host, "first")[-1] == "Campaign"
    moved = ok(invoke(host, host.agent_context, "tasks_update", {"task_id": campaign["id"], "changes": {"customer_state": "Campaign 2"}},
                      revision=campaign["revision"]))["task"]
    assert moved["customer_state"] == "Campaign 2" and moved["tags"] == [CAMPAIGN]
    # The configuration row the ports pin is not the vocabulary row, and the append never revises it.
    configs = {row["domain"] for row in rows(host, "SELECT domain FROM business_configs WHERE business_id=?", host.first)}
    assert "task_customer_states" in configs


# -- Task, email and Context links ----------------------------------------------------------------------------------------

def test_customer_task_links_work_between_scopes_without_widening_access(host):
    business_customer = customer(host, customer_state="Prospect")
    global_customer = customer(host, None)                                       # Global, no label
    story = task(host, title="Relationship", task_type="story")
    linked = link_task(host, story, business_customer)
    assert {row["task_id"] for row in linked["related_tasks"]} == {business_customer["id"]}
    # Global <-> business links follow the existing rule: the owner reads both; the rows carry the owning scope and the compact projection.
    g = data(operate(host, None, "tasks_update", task_id=global_customer["id"], expected_revision=global_customer["revision"], changes={
        "related_task": {"business_id": host.first, "task_id": story["id"], "linked": True}}))["task"]
    rows_global = g["related_tasks"]
    assert [(row["task_id"], row["business_id"], row["task_type"]) for row in rows_global] == [(story["id"], host.first, "story")]
    reverse = next(row for row in read(host, story["id"])["related_tasks"] if row["task_id"] == global_customer["id"])
    assert (reverse["business_id"], reverse["business_name"], reverse["task_type"], reverse["customer_kind"]) == (None, "Global", "customer", "company")
    assert "customer_profile" not in reverse and NOTES not in json.dumps(reverse)
    # Unlinking removes both directions.
    data(operate(host, None, "tasks_update", task_id=global_customer["id"], expected_revision=g["revision"], changes={
        "related_task": {"business_id": host.first, "task_id": story["id"], "linked": False}}))
    assert all(row["task_id"] != global_customer["id"] for row in read(host, story["id"])["related_tasks"])


@pytest.mark.parametrize("slug", ["first", None])
def test_an_email_conversation_of_the_same_scope_links_to_a_customer_and_unlinks(host, slug):
    record = customer(host, slug)
    business = host.first if slug else None
    conversation = seed_email(host, business)
    other_scope = seed_email(host, host.second if slug else host.first, "other-scope-conversation")
    # The picker's list call, with the panel's arguments (Global names its scope): only this scope's conversations are offered.
    listed = data(operate(host, slug, "email_conversations_list", limit=50, **scope_of(slug)))["items"]
    assert [row["id"] for row in listed] == [conversation] and listed[0]["subject"] == "Project proposal"
    edge = link_to(host, slug, record, {"type": "material", "id": conversation})
    assert edge["role"] == "concerns" and edge["source"] == {"type": "material", "id": conversation}
    edges = concerns(host, slug, record)
    assert [e["id"] for e in edges] == [edge["id"]]
    shown = other_end(edges[0], record)
    assert shown["resource_type"] == "email_conversations" and shown["label"] == "Project proposal" and shown["type"] == "material"
    # A conversation of another scope is not linkable and leaks nothing.
    assert refused(operate(host, slug, "relationships_create", source={"type": "material", "id": other_scope},
                           target={"type": "task", "id": record["id"]}, role="concerns", **scope_of(slug))) in {"not_found", "forbidden"}
    assert [e["id"] for e in concerns(host, slug, record)] == [edge["id"]]
    unlink(host, slug, edges[0])
    assert concerns(host, slug, record) == []


def test_a_business_conversation_cannot_link_to_a_global_customer_and_the_reverse(host):
    business_customer, global_customer = customer(host), customer(host, None)
    business_mail, global_mail = seed_email(host, host.first, "business-mail"), seed_email(host, None, "global-mail")
    assert refused(operate(host, None, "relationships_create", source={"type": "material", "id": business_mail},
                           target={"type": "task", "id": global_customer["id"]}, role="concerns", scope="global")) in {"not_found", "forbidden"}
    assert refused(operate(host, "first", "relationships_create", source={"type": "material", "id": global_mail},
                           target={"type": "task", "id": business_customer["id"]}, role="concerns")) in {"not_found", "forbidden"}
    assert concerns(host, "first", business_customer) == [] and concerns(host, None, global_customer) == []


def test_business_context_material_links_use_a_real_registered_document(host):
    record = customer(host)
    topic = data(operate(host, "first", "context_topics_create", title="Launch", slug="launch"))["document"]
    note = data(operate(host, "first", "context_notes_create", topic_id=topic["id"], title="Launch notes", body="Plan.\n",
                        material_type="note"))["document"]
    # The picker: the same list call and filter the panel makes, over registered documents (authored sources and this note).
    picker = [row for row in data(operate(host, "first", "context_items_list", type="material", limit=50))["items"]
              if row["resource_type"] != "email_conversations"]
    assert note["id"] in {row["id"] for row in picker} and {row["resource_type"] for row in picker} == {"documents"}
    found = next(row for row in picker if row["id"] == note["id"])
    edge = link_to(host, "first", record, {"type": found["type"], "id": found["id"]})
    rows_shown = concerns(host, "first", record)
    shown = other_end(rows_shown[0], record)
    assert [e["id"] for e in rows_shown] == [edge["id"]] and shown["label"] == "Launch notes" and shown["resource_type"] == "documents"
    # Another registered document (an authored source) links too; the material list is what the panel opens from.
    source = next(row for row in picker if row["id"] != note["id"])
    second = link_to(host, "first", record, {"type": source["type"], "id": source["id"]})
    assert {e["id"] for e in concerns(host, "first", record)} == {edge["id"], second["id"]}
    # A material of the other business or a Global note is not linkable here.
    global_note = ok(invoke(host, host.owner_global, "notes_create", {"title": "Journal", "body": "owner journal\n"}))["document"]
    assert refused(operate(host, "first", "relationships_create", source={"type": "material", "id": global_note["id"]},
                           target={"type": "task", "id": record["id"]}, role="concerns")) in {"not_found", "forbidden"}
    # Unlinking removes just that edge.
    unlink(host, "first", rows_shown[0])
    assert [e["id"] for e in concerns(host, "first", record)] == [second["id"]]
    # An agent without a grant cannot create the relationship.
    assert code(invoke(host, host.bare_context, "relationships_create", {"source": {"type": "material", "id": note["id"]},
                                                                           "target": {"type": "task", "id": record["id"]}, "role": "concerns"})) \
        in {"forbidden", "policy_denied", "not_found"}


def test_global_context_material_links_use_a_real_registered_note(host):
    record = customer(host, None)
    note = ok(invoke(host, host.owner_global, "notes_create", {"title": "Journal", "body": "owner journal\n"}))["document"]
    picker = data(operate(host, None, "context_items_list", type="material", limit=50, scope="global"))["items"]
    found = next(row for row in picker if row["id"] == note["id"])
    assert found["resource_type"] == "documents" and found["label"] == "Journal"
    edge = link_to(host, None, record, {"type": found["type"], "id": found["id"]})
    shown = other_end(concerns(host, None, record)[0], record)
    assert shown["label"] == "Journal" and shown["resource_type"] == "documents" and edge["business_id"] is None
    # A business material is not linkable to a Global Customer.
    business_note = ok(invoke(host, host.owner, "notes_create", {"title": "Business note", "body": "x\n", "target_business_id": host.first}))["document"]
    assert refused(operate(host, None, "relationships_create", source={"type": "material", "id": business_note["id"]},
                           target={"type": "task", "id": record["id"]}, role="concerns", scope="global")) in {"not_found", "forbidden"}
    unlink(host, None, concerns(host, None, record)[0])
    assert concerns(host, None, record) == []
