"""Focused synthetic coverage of the six Project record operations and their private-thread integration.

One existing Project (the ``businesses`` row) is read as a Project: its real name, slug and summary, its inventory of
owned records and explicitly linked records from any readable scope, one item with the appropriate existing domain
read, and the one Operations-owned association store. Every check runs against the real host composition: the frozen
neutral item resolver, the canonical Task/Signoff/Action/routine/reminder/dev-ticket readers, the private
owner–Ordubashy conversation service and the Focus board. No model, network, live provider, deployment or UI.

SYNTHETIC STORED SHAPE. Reminder rows are stored as the Telegram outbox stores them (the delivery bridge is inert
here), the extra Projects and their owner memberships are inserted as project creation seeds them, and one project
Task is edited to carry the synthetic ``$task`` shape a Customer Task stores. Those seeds only exist because the
checks need an existing Project, an existing reminder and a store the reads must not mutate; nothing about the
operations under test is replaced.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from host_foundation_support import build_instance, create_registered_global, register_sources, select_coordinator

FORM = {"title": "Which price?", "explanation": "Choose the number we quote to the pilot.", "kind": "question",
        "selection_mode": "single", "choices": [
            {"id": "low", "label": "Lower", "explanation": "Win the pilot."},
            {"id": "high", "label": "Higher", "explanation": "Prove the value."}]}


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    root = tmp_path_factory.mktemp("project-records")
    instance, ids = build_instance(root, businesses=("first",))
    first = ids["first"]
    register_sources(instance, first, "first")
    ordubashy = create_registered_global(instance, "ordubashy")
    select_coordinator(instance, ordubashy)
    instance.runtime.freeze_registry()
    principal = Principal(ordubashy, "agent", "Ordubashy")
    def owner_context(business):
        with instance.database.read() as uow:
            return instance.runtime.policy.context_for(instance.owner, business_id=business,
                                                       review_business_id=business, uow=uow, source="dashboard")

    def agent_context(business, source="mcp"):
        return instance.runtime.resolver.invocation_context(Principal(ordubashy, "agent", "Ordubashy"),
                                                            business_id=business, source=source)

    try:
        yield SimpleNamespace(instance=instance, db=instance.database, runtime=instance.runtime, first=first,
                              ordubashy=ordubashy, owner_context=owner_context, agent_context=agent_context,
                              business_owner=instance.human(first))
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


def call(host, ctx, name, arguments, *, revision=None):
    return host.runtime.application.invoke(
        ctx, name, arguments, InvocationOptions(idempotency_key=new_id(), expected_revision=revision))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def person(host, name, kind="human"):
    identity = new_id()
    with host.db.transaction() as uow:
        uow.repository("principals").insert({"id": identity, "kind": kind, "display_name": name})
    return identity


def seed_project(host, *, name, slug, member=None, archived=False):
    """An existing Project row as creation seeds it, with an optional owner membership for someone else."""
    project = new_id()
    from qorqut_operations.contracts import utc_now
    root = host.instance.paths.instance_root
    (root / "business" / slug / "hq").mkdir(parents=True, exist_ok=True)
    with host.db.transaction() as uow:
        uow.repository("businesses").insert({
            "id": project, "slug": slug, "name": name, "stage": "operating",
            "workspace": str(root / "business" / slug), "hq_path": str(root / "business" / slug / "hq"),
            "config_json": json.dumps({"one_liner": f"{name} is a synthetic project."}),
            "archived_at": utc_now() if archived else None})
        uow.repository("business_memberships", business_id=project).insert(
            {"principal_id": host.instance.owner if member is None else member, "is_owner": True})
    return project


def context(host, business):
    """The installation owner's current context for one Project (memberships refreshed like any invocation)."""
    return host.owner_context(business)


def context_global(host):
    return host.owner_context(None)


def agent(host, business, source="mcp"):
    return host.agent_context(business, source)


def as_principal(host, project, principal_id):
    with host.db.read() as uow:
        return host.runtime.policy.context_for(principal_id, business_id=project, review_business_id=project,
                                               uow=uow, source="dashboard")


def task(host, business, title, ctx=None):
    return ok(call(host, ctx or context(host, business), "tasks_create", {"title": title}))["task"]


def signoff(host, business, key):
    return ok(call(host, context(host, business), "signoffs_input_create",
                   {"request_key": key, "form": FORM}))["signoff"]


def routine(host, business, name):
    """One stored Project routine as the routine operations store it; no worker or program runs in this check."""
    identity, now = new_id(), "2026-10-30T09:00:00Z"
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO routines(namespace,business_id,id,name,scope,target,schedule,timezone,status,job_kind,"
            "arguments_json,timeout_seconds,metadata_json,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,'business','business','0 0 * * *','UTC','paused','test.fixture','{}',900,'{}',1,?,?)",
            (business, business, identity, name, now, now))
    return {"id": identity, "name": name, "business_id": business, "status": "paused", "revision": 1}


def reminder(host, business, text, at="2026-11-01T09:00:00+00:00"):
    identity, now = new_id(), "2026-10-30T09:00:00Z"
    source = "business:" + business if business else "global"
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO telegram_outbox(id,chat_name,chat_id,kind,source_kind,source_key,part,payload_json,state,"
            "binding_fingerprint,config_generation,agent_id,created_at,updated_at,next_attempt_at) "
            "VALUES(?,'ordubashy',1,'text','owner_reminder',?,?,?,'queued',?,1,?,?,?,?)",
            (identity, source + "/" + identity, at, json.dumps({"text": text}), "0" * 64,
             host.ordubashy, now, now, at))
    return identity


def list_items(host, ctx, project, **arguments):
    return ok(call(host, ctx, "project_items_list", {"project_id": project, **arguments}))


def item_of(items, type, identity):
    return next(item for item in items if item["type"] == type and item["id"] == identity)


def test_record_get_reads_the_existing_project_and_refuses_every_other_caller(host):
    project = seed_project(host, name="River Studio", slug="river-studio")
    record = ok(call(host, context(host, project), "project_record_get", {"project_id": project}))["project"]
    assert record == {"id": project, "name": "River Studio", "slug": "river-studio",
                      "summary": "River Studio is a synthetic project.", "revision": 1, "archived": False,
                      "source": {"type": "project", "id": project, "business_id": project}}
    assert ok(call(host, context_global(host), "project_record_get",
                   {"project_id": project, "scope": "global"}))["project"]["name"] == "River Studio"
    # The exact configured Ordubashy reads the same existing record, from the root route and its own route.
    assert ok(call(host, agent(host, project), "project_record_get", {"project_id": project}))["project"]["slug"] == "river-studio"
    # A business route must agree with project_id.
    assert call(host, context(host, host.first), "project_record_get", {"project_id": project}).error["code"] in {
        "forbidden", "validation_error"}
    # An ordinary member and a bystander agent are refused by the identity gate.
    teammate = Principal(new_id(), "human", "Teammate")
    with host.db.transaction() as uow:
        uow.repository("principals").insert({"id": teammate.id, "kind": "human", "display_name": "Teammate"})
        uow.repository("business_memberships", business_id=project).insert({"principal_id": teammate.id, "is_owner": True})
    member = replace(context(host, project), principal=teammate, allowed_business_ids=frozenset({project}))
    assert call(host, member, "project_record_get", {"project_id": project}).error["code"] == "forbidden"
    bystander = new_id()
    with host.db.transaction() as uow:
        uow.repository("principals").insert({"id": bystander, "kind": "agent", "display_name": "Other"})
    assert call(host, replace(context_global(host), principal=Principal(bystander, "agent")),
                "project_record_get", {"project_id": project}).error["code"] in {
        "forbidden", "inactive_agent", "policy_denied"}
    # A Project outside the caller's reach, an unknown one and an archived one are all not found for current work.
    outside = seed_project(host, name="Elsewhere", slug="elsewhere", member=person(host, "Other owner"))
    assert call(host, context_global(host), "project_record_get",
                {"project_id": outside, "scope": "global"}).error["code"] in {
        "not_found", "forbidden", "scope_required"}
    assert call(host, context_global(host), "project_record_get",
                {"project_id": new_id(), "scope": "global"}).error["code"] in {
        "not_found", "forbidden", "scope_required"}
    archived = seed_project(host, name="Old Studio", slug="old-studio")
    archived_ctx = context(host, archived)
    with host.db.transaction() as uow:
        row = uow.repository("businesses").get(archived)
        uow.repository("businesses").update(archived, {"archived_at": "2026-01-01T00:00:00Z"},
                                            expected_revision=row["revision"])
    # An archived Project stays stored; current work (and its record read) refuses it for every caller.
    assert call(host, archived_ctx, "project_record_get",
                {"project_id": archived}).error["code"] == "business_archived"


def test_inventory_merges_owned_records_with_explicit_links_from_any_readable_scope(host):
    project = seed_project(host, name="Blue Harbor", slug="blue-harbor")
    owner = context(host, project)
    parent = task(host, project, "Ship the pilot")
    child_id = ok(call(host, owner, "tasks_create", {"title": "Call the pilot", "parent_id": parent["id"]}))["task"]["id"]
    proposal = signoff(host, project, "blue-harbor-price")
    action = ok(call(host, owner, "actions_record", {
        "text": "Quoted the pilot price at 1200 EUR", "category": "business-operational",
        "real_time": "2026-10-20", "business_ids": [project]}))["action"]
    schedule = routine(host, project, "Daily pilot review")
    stored_reminder = reminder(host, project, "Follow up the pilot")
    foreign = seed_project(host, name="Maple Works", slug="maple-works")
    foreign_task = task(host, foreign, "Foreign deliverable")
    owner = context(host, project)

    linked = ok(call(host, owner, "project_items_link", {"project_id": project, "item": {
        "type": "task", "id": foreign_task["id"], "business_id": foreign}}))
    assert linked["already_present"] is False
    assert linked["link"]["item"] == {"type": "task", "id": foreign_task["id"], "business_id": foreign}
    assert linked["link"]["target_namespace"] == "business:" + foreign
    assert linked["item"]["association"] == {"owned": False, "linked": True, "link_id": linked["link"]["id"],
                                             "link_revision": 1}
    # Linking the same target again converges on that one stored association.
    again = ok(call(host, owner, "project_items_link", {"project_id": project, "item": {
        "type": "task", "id": foreign_task["id"], "business_id": foreign}}))
    assert again["already_present"] is True and again["link"]["id"] == linked["link"]["id"]
    # An owned record is a harmless no-op: it never creates a redundant stored link.
    owned_noop = ok(call(host, owner, "project_items_link", {"project_id": project, "item": {
        "type": "task", "id": parent["id"], "business_id": project}}))
    assert owned_noop["already_present"] is True and owned_noop["link"] is None

    listing = list_items(host, owner, project, include_closed=True)
    items = listing["items"]
    assert item_of(items, "task", parent["id"])["association"] == {"owned": True, "linked": False}
    assert item_of(items, "task", child_id)["parent_id"] == parent["id"]
    assert item_of(items, "signoff", proposal["id"])["state"] == "pending"
    assert item_of(items, "action", action["action_id"])["association"]["owned"] is True
    assert item_of(items, "routine", schedule["id"])["state"] == "paused"
    assert item_of(items, "reminder", stored_reminder)["at"] == "2026-11-01T09:00:00+00:00"
    foreign_item = item_of(items, "task", foreign_task["id"])
    assert foreign_item["association"]["linked"] is True and foreign_item["association"]["owned"] is False
    assert foreign_item["business_slug"] == "maple-works" and foreign_item["can_thread"] is True
    # Owned and linked never duplicate a row, regardless of the reading order of the two families.
    assert len([item for item in items if item["type"] == "task" and item["id"] == parent["id"]]) == 1
    # Hierarchy filtering keeps only top-level Tasks.
    roots = list_items(host, owner, project, root_tasks_only=True)["items"]
    assert not [item for item in roots if item["type"] == "task" and item.get("parent_id")]
    # Sections narrow the same authorized page.
    assert {item["type"] for item in list_items(host, owner, project, section="work")["items"]} == {"task"}
    assert {item["type"] for item in list_items(host, owner, project, section="automation")["items"]} == {"routine"}
    # Bounded keyset pagination over the merged, deterministically ordered page.
    first_page = list_items(host, owner, project, limit=2)
    assert len(first_page["items"]) == 2 and first_page["next_cursor"]
    rest = list_items(host, owner, project, limit=100, cursor=first_page["next_cursor"])["items"]
    assert len({item["id"] for item in first_page["items"]} & {item["id"] for item in rest}) == 0
    # The linked foreign record is not owned by the other Project either; its own inventory shows it as owned only.
    foreign_items = list_items(host, context(host, foreign), foreign, limit=100)["items"]
    assert item_of(foreign_items, "task", foreign_task["id"])["association"] == {"owned": True, "linked": False}


def test_a_target_outside_the_callers_reach_is_refused_and_a_revoked_link_disappears(host):
    project = seed_project(host, name="Cedar Lab", slug="cedar-lab")
    hidden_owner = person(host, "Hidden owner")
    outside = seed_project(host, name="Hidden Ridge", slug="hidden-ridge", member=hidden_owner)
    owner = context(host, project)
    foreign = task(host, outside, "Private work", ctx=as_principal(host, outside, hidden_owner))
    denied = call(host, owner, "project_items_link", {"project_id": project, "item": {
        "type": "task", "id": foreign["id"], "business_id": outside}})
    assert denied.error["code"] in {"not_found", "forbidden"}
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM project_item_links WHERE business_id=?",
                                      (project,)).fetchone()[0] == 0
    # A link never grants the target's access: the owner keeps its own context and the list omits the denied item.
    reachable = seed_project(host, name="Open Field", slug="open-field")
    shared = task(host, reachable, "Shared deliverable")
    linked = ok(call(host, context(host, project), "project_items_link", {
        "project_id": project, "item": {"type": "task", "id": shared["id"], "business_id": reachable}}))
    assert linked["item"]["business_id"] == reachable
    # Revoking the target's source (deleting it) leaves the stored association but never its label: the list drops it.
    with host.db.transaction() as uow:
        uow.connection.execute("DELETE FROM tasks WHERE id=?", (shared["id"],))
    items = list_items(host, owner, project, limit=100)["items"]
    assert not [item for item in items if item["id"] == shared["id"]]
    stored = ok(call(host, owner, "project_items_list", {"project_id": project, "limit": 100}))["items"]
    assert not [item for item in stored if item["type"] == "task" and item["id"] == shared["id"]]
    # The association's own revision-gated removal still works from its stored identity.
    removed = ok(call(host, owner, "project_items_unlink", {
        "project_id": project, "link_id": linked["link"]["id"], "expected_revision": linked["link"]["revision"]}))
    assert removed == {"removed": True}
    assert call(host, owner, "project_items_unlink", {
        "project_id": project, "link_id": linked["link"]["id"], "expected_revision": 1}).error["code"] == "not_found"


def test_link_and_unlink_reject_a_self_project_and_keep_exact_revisions(host):
    project = seed_project(host, name="Quiet Pine", slug="quiet-pine")
    other = seed_project(host, name="Loud Pine", slug="loud-pine")
    owner = context(host, project)
    self_link = call(host, owner, "project_items_link", {"project_id": project, "item": {
        "type": "project", "id": project, "business_id": project}})
    assert self_link.error["code"] == "validation_error"
    linked = ok(call(host, owner, "project_items_link", {"project_id": project, "item": {
        "type": "project", "id": other, "business_id": other}}))
    assert linked["item"]["type"] == "project" and linked["item"]["business_id"] == other
    assert linked["item"]["label"] == "Loud Pine" and linked["item"]["can_thread"] is True
    stale = call(host, owner, "project_items_unlink", {
        "project_id": project, "link_id": linked["link"]["id"], "expected_revision": 2})
    assert stale.error["code"] == "revision_conflict"
    ok(call(host, owner, "project_items_unlink", {
        "project_id": project, "link_id": linked["link"]["id"], "expected_revision": 1}))
    # The target Project still exists, unchanged.
    assert ok(call(host, context(host, other), "project_record_get",
                   {"project_id": other}))["project"]["name"] == "Loud Pine"


def test_project_item_get_returns_the_authorized_item_and_its_existing_domain_read(host):
    project = seed_project(host, name="Golden Gate", slug="golden-gate")
    owner = context(host, project)
    created = task(host, project, "Prepare the brief")
    read = ok(call(host, owner, "project_item_get", {"project_id": project, "item": {
        "type": "task", "id": created["id"], "business_id": project}}))
    assert read["item"]["association"] == {"owned": True, "linked": False}
    assert read["record"]["task"]["id"] == created["id"] and read["record"]["task"]["title"] == "Prepare the brief"
    proposal = signoff(host, project, "golden-gate-question")
    signoff_read = ok(call(host, owner, "project_item_get", {"project_id": project, "item": {
        "type": "signoff", "id": proposal["id"], "business_id": project}}))
    assert signoff_read["record"]["signoff"]["id"] == proposal["id"]
    schedule = routine(host, project, "Weekly recap")
    routine_read = ok(call(host, owner, "project_item_get", {"project_id": project, "item": {
        "type": "routine", "id": schedule["id"], "business_id": project}}))
    assert routine_read["record"]["routine"]["id"] == schedule["id"]
    # Executions stay in the existing reader under the routine's exact id and scope; no execution path is changed.
    runs = ok(call(host, owner, "routines_runs", {"routine_id": schedule["id"], "view": "recent"}))
    assert isinstance(runs.get("items"), list)
    # A wrong scope or an unknown item is refused, never disclosed.
    assert call(host, owner, "project_item_get", {"project_id": project, "item": {
        "type": "task", "id": created["id"], "business_id": host.first}}).error["code"] == "not_found"
    assert call(host, owner, "project_item_get", {"project_id": project, "item": {
        "type": "task", "id": new_id(), "business_id": project}}).error["code"] == "not_found"
    assert call(host, owner, "project_item_get", {"project_id": project, "item": {
        "type": "made-up", "id": created["id"], "business_id": project}}).error["code"] == "validation_error"


def test_project_item_get_keeps_the_canonical_bounded_context_envelope(host):
    project = seed_project(host, name="Bounded Brief", slug="bounded-brief")
    (host.instance.paths.instance_root / "operations" / "projects" / "bounded-brief" / "hq").mkdir(
        parents=True, exist_ok=True)
    owner = context(host, project)
    topic = ok(call(host, owner, "context_topics_create", {"title": "Bounded pilot", "slug": "bounded-pilot"}))
    body = "Section 1: the complete authorized body.\n\n" + ("the pilot check keeps the full body " * 240)
    note = ok(call(host, owner, "context_notes_create", {
        "topic_id": topic["document"]["id"], "title": "Bounded pilot brief", "body": body}))
    read = ok(call(host, owner, "project_item_get", {
        "project_id": project, "item": {"type": "material", "id": note["document"]["id"], "business_id": project}}))
    record = read["record"]
    # The dashboard shape returned the whole body and no cursor; the canonical shape returns a bounded chunk and
    # the top-level continuation a document reader actually consumes.
    assert record["complete"] is False and record["next_cursor"]
    first = record["document"]
    assert first["id"] == note["document"]["id"] and first["character_offset"] == 0 and first["content_hash"]
    assert len(first["body"]) < len(body)
    more = ok(call(host, owner, "context_get", {
        "document_id": note["document"]["id"], "scope": "business", "cursor": record["next_cursor"]}))
    assert more["complete"] is True and more.get("next_cursor") is None
    assert more["document"]["character_offset"] == len(first["body"])
    assert more["document"]["content_hash"] == first["content_hash"]
    assert first["body"] + more["document"]["body"] == body


def test_candidates_list_only_actual_readable_records_of_the_chosen_scope(host):
    project = seed_project(host, name="Silver Fern", slug="silver-fern")
    other = seed_project(host, name="Copper Fern", slug="copper-fern")
    owner = context(host, project)
    here = task(host, project, "Local candidate")
    there = task(host, other, "Cohort candidate")
    candidates = ok(call(host, owner, "project_items_candidates", {
        "project_id": project, "source_business_id": other, "limit": 100}))["items"]
    assert [item["id"] for item in candidates if item["type"] == "task"] == [there["id"]]
    assert not [item for item in candidates if item["id"] == here["id"]]
    # Another Project is a link candidate when the caller can currently reach it; this Project never is.
    projects = [item for item in candidates if item["type"] == "project"]
    assert other in {item["id"] for item in projects}
    assert project not in {item["id"] for item in projects}
    typed = ok(call(host, owner, "project_items_candidates", {
        "project_id": project, "source_business_id": other, "type": "project"}))["items"]
    assert other in {item["id"] for item in typed} and project not in {item["id"] for item in typed}
    searched = ok(call(host, owner, "project_items_candidates", {
        "project_id": project, "source_business_id": other, "query": "cohort"}))["items"]
    assert [item["id"] for item in searched] == [there["id"]]
    # Global candidates exist only when the caller may read that Global scope.
    global_project = seed_project(host, name="Global Scope", slug="global-scope")
    global_task = ok(call(host, context_global(host), "tasks_create", {"title": "Global item", "scope": "global"}))["task"]
    owner = context(host, project)                                    # reach after the last Project was seeded
    globals_list = ok(call(host, owner, "project_items_candidates", {
        "project_id": project, "source_business_id": None, "limit": 100}))["items"]
    assert [item["id"] for item in globals_list if item["type"] == "task"] == [global_task["id"]]
    global_projects = {item["id"] for item in globals_list if item["type"] == "project"}
    assert global_project in global_projects and project not in global_projects
    # A source scope outside the caller's reach is refused before any candidate is enumerated.
    hidden = seed_project(host, name="Hidden Scope", slug="hidden-scope", member=person(host, "Other owner"))
    denied = call(host, owner, "project_items_candidates", {"project_id": project, "source_business_id": hidden})
    assert denied.error["code"] in {"not_found", "forbidden"}
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM project_item_links WHERE business_id=?",
                                      (project,)).fetchone()[0] == 0


def test_private_project_conversation_uses_the_project_identity_and_never_creates_messages(host):
    project = seed_project(host, name="Harbor Light", slug="harbor-light")
    owner = context(host, project)
    source = {"type": "project", "id": project, "business_id": project}
    sent = ok(call(host, owner, "owner_conversation_send", {"source": source, "text": "The pilot is signed."}))
    assert sent["message"]["sequence"] == 1
    read = ok(call(host, owner, "owner_conversation_get", {"source": source}))
    with host.db.read() as uow:
        row = uow.connection.execute("SELECT source_key,source_type,source_id FROM owner_conversations WHERE id=?",
                                     (read["conversation"]["id"],)).fetchone()
    assert tuple(row) == (f"source:{project}:project:{project}", "project", project)
    assert [message["body"] for message in read["messages"]] == ["The pilot is signed."]
    # A business route must agree with the Project and the canonical reference names its own scope.
    assert call(host, context(host, host.first), "owner_conversation_send",
                {"source": source, "text": "No"}).error["code"] in {"validation_error", "forbidden"}
    assert call(host, owner, "owner_conversation_send",
                {"source": {**source, "business_id": host.first}, "text": "No"}).error["code"] == "validation_error"
    # Reads, related records, links and Focus presentation never append a message.
    before = len(ok(call(host, owner, "owner_conversation_get", {"source": source}))["messages"])
    related = ok(call(host, owner, "owner_conversation_related", {"source": source, "limit": 100}))
    assert isinstance(related["items"], list) and "next_offset" in related
    ok(call(host, owner, "project_items_list", {"project_id": project}))
    ok(call(host, owner, "project_items_link", {"project_id": project, "item": {
        "type": "project", "id": host.first, "business_id": host.first}}))
    card = ok(call(host, context_global(host), "focus_present", {
        "scope": "global", "source": source, "reason": "Watch the pilot", "requested_response": "Decide"}))["item"]
    board = ok(call(host, context_global(host), "focus_get", {"scope": "global", "item_id": card["id"]}))["item"]
    assert board["label"] == "Harbor Light" and board["note_count"] == 0
    assert board["conversation"] == source and board["resolved_source"]["label"] == "Harbor Light"
    assert len(ok(call(host, owner, "owner_conversation_get", {"source": source}))["messages"]) == before
    # The one Project directory entry orders by its latest real message only, and carries the real name.
    directory = ok(call(host, context_global(host), "owner_conversations_list", {"scope": "global"}))["items"]
    listed = next(item for item in directory if item["source"]["type"] == "project" and item["source"]["id"] == project)
    assert listed["resolved_source"]["label"] == "Harbor Light"
    assert listed["resolved_source"]["business_name"] == "Harbor Light"
    # A Project card refuses a custom Focus display name and has no shared team reply channel.
    assert call(host, context_global(host), "focus_present", {"scope": "global", "source": source,
        "display_name": "Custom", "reason": "Watch", "requested_response": "Decide"}).error["code"] == "validation_error"
    assert call(host, context_global(host), "focus_edit", {"scope": "global", "item_id": card["id"],
        "expected_card_revision": board["revision"], "changes": {"display_name": "Custom"}}).error["code"] == "validation_error"
    assert call(host, context_global(host), "focus_reply", {"scope": "global", "item_id": card["id"],
        "text": "A team note"}).error["code"] == "channel_required"
    # Renaming the Project keeps the conversation identity and updates the displayed name; no message is created.
    with host.db.transaction() as uow:
        row = uow.repository("businesses").get(project)
        uow.repository("businesses").update(project, {"name": "Harbor Light Studio"}, expected_revision=row["revision"])
    renamed = ok(call(host, owner, "owner_conversation_get", {"source": source}))
    assert renamed["conversation"]["id"] == read["conversation"]["id"]
    assert len(renamed["messages"]) == before
    renamed_directory = ok(call(host, context_global(host), "owner_conversations_list", {"scope": "global"}))["items"]
    renamed_item = next(item for item in renamed_directory
                        if item["source"]["type"] == "project" and item["source"]["id"] == project)
    assert renamed_item["id"] == listed["id"] and renamed_item["resolved_source"]["label"] == "Harbor Light Studio"


def test_project_reminders_are_read_by_scope_and_never_sent(host):
    project = seed_project(host, name="Willow Creek", slug="willow-creek")
    other = seed_project(host, name="Aspen Creek", slug="aspen-creek")
    owner = context(host, project)
    mine = reminder(host, project, "Call the pilot owner")
    elsewhere = reminder(host, other, "Other project reminder")
    global_reminder = reminder(host, None, "Global reminder")
    business = ok(call(host, owner, "owner_reminders_list", {"scope": "business"}))["reminders"]
    assert [row["reminder_id"] for row in business] == [mine]
    assert business[0]["text"] == "Call the pilot owner" and business[0]["state"] == "scheduled"
    globals_only = ok(call(host, context_global(host), "owner_reminders_list", {"scope": "global"}))["reminders"]
    assert [row["reminder_id"] for row in globals_only] == [global_reminder]
    # Global means Global only, whichever route names it; the project's own reminder is not mixed in.
    business_global = ok(call(host, owner, "owner_reminders_list", {"scope": "global"}))["reminders"]
    assert [row["reminder_id"] for row in business_global] == [global_reminder]
    # The configured Ordubashy reads the same stored rows; an unrelated agent is refused.
    assert [row["reminder_id"] for row in ok(call(host, agent(host, project, "worker"), "owner_reminders_list", {"scope": "business"}))["reminders"]] == [mine]
    # A different registered global agent is still not the configured Ordubashy.
    bystander = create_registered_global(host.instance, "learning-applier")
    outsider = host.runtime.resolver.invocation_context(Principal(bystander, "agent", "Learning Applier"),
                                                        business_id=project, source="worker")
    assert call(host, outsider, "owner_reminders_list", {"scope": "business"}).error["code"] in {
        "forbidden", "policy_denied"}
    # The Project inventory includes its own reminder and links it as the one stored reminder view.
    items = list_items(host, owner, project, limit=100)["items"]
    assert item_of(items, "reminder", mine)["label"] == "Call the pilot owner"
    assert not [item for item in items if item["id"] == elsewhere]
    linked = ok(call(host, owner, "project_items_link", {"project_id": project, "item": {
        "type": "reminder", "id": global_reminder, "business_id": None}}))
    assert linked["item"]["type"] == "reminder" and linked["link"]["item"]["type"] == "reminder"
    assert linked["link"]["target_namespace"] == "global"
    record = ok(call(host, owner, "project_item_get", {"project_id": project, "item": {
        "type": "reminder", "id": global_reminder, "business_id": None}}))
    assert record["record"]["reminder"]["reminder_id"] == global_reminder
    # A reminder never becomes a private thread source.
    assert call(host, owner, "owner_conversation_send", {"source": {
        "type": "reminder", "id": global_reminder, "business_id": None}, "text": "No"}).error["code"] in {
        "validation_error", "unsupported_source", "not_found"}
    # The read created no state: the reminder is still queued and no message was written anywhere.
    with host.db.read() as uow:
        assert uow.connection.execute("SELECT state FROM telegram_outbox WHERE id=?", (mine,)).fetchone()["state"] == "queued"
    assert ok(call(host, owner, "project_items_list", {"project_id": project}))["items"]


def test_related_action_projects_its_bounded_human_readable_text(host):
    """The shared related-resource projection labels an Action by its text, never by its identity."""
    text = "Confirmed the harbor pilot window with the supplier"
    action = ok(call(host, context(host, host.first), "actions_record", {
        "text": text, "category": "business-operational", "business_ids": [host.first]}))["action"]
    with host.db.read() as uow:
        view = host.runtime.domains.related.get(context(host, host.first), uow, "action_logs", action["action_id"])
    assert view["label"] == text and view["revision"] == action["revision"]
    # The same shared resolver denies an Action in a workspace that cannot read it before any label exists.
    stranger = person(host, "Unrelated reviewer")
    foreign = seed_project(host, name="Other Harbour", slug="other-harbour", member=stranger)
    with host.db.read() as uow:
        with pytest.raises(DomainError) as refused:
            host.runtime.domains.related.get(as_principal(host, foreign, stranger), uow, "action_logs", action["action_id"])
    assert refused.value.code == "not_found" and text not in (refused.value.message or "")


def test_schema_keeps_project_sources_and_link_rows_while_private_rows_survive(host):
    """The composed schema carries the Project source and the link table without touching private conversation rows."""
    project = seed_project(host, name="Twin Pines", slug="twin-pines")
    owner = context(host, project)
    source = {"type": "project", "id": project, "business_id": project}
    ok(call(host, owner, "owner_conversation_send", {"source": source, "text": "A private project note."}))
    ok(call(host, owner, "project_items_link", {"project_id": project, "item": {
        "type": "project", "id": host.first, "business_id": host.first}}))
    with host.db.read() as uow:
        sql = uow.connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='owner_conversations'").fetchone()["sql"]
        assert "'project'" in sql
        link_schema = uow.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='project_item_links'").fetchone()["sql"]
        assert "UNIQUE(business_id, target_type, target_namespace, target_id)" in link_schema
        assert uow.connection.execute("SELECT count(*) FROM owner_conversations").fetchone()[0] >= 1
        assert uow.connection.execute("SELECT count(*) FROM owner_conversation_messages").fetchone()[0] >= 1
        assert uow.connection.execute("SELECT count(*) FROM project_item_links WHERE business_id=?",
                                      (project,)).fetchone()[0] == 1