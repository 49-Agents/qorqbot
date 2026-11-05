"""The host's neutral item resolver, relationships and reviews, through the actual host lookup and adapters.

A real synthetic host composes ``ItemResolver``, ``RelationshipOperations`` and ``ContextReviews`` over the retained
domains (``domains/items``). These cases prove the five host obligations of that composition with real rows:

* private sources stay private: the owner-only journal, excluded working memory, Global-mailbox discussion, current
  membership and the business/global scope are enforced by the lookup, not by the engine's generic resolver (businesses
  have no resident agents, so a global agent's own notes are Global records);
* a document with an unfinished application is read as last-committed metadata (``allow_busy``) and a fingerprint
  expects the exact managed file target;
* a review and an edit to the same item commit together or not at all, and the edit is judged by its own operation's
  policy (including a Signoff floor);
* Material, archive and navigation are the host's adapters, and no relationship is a protected edge;
* none of it constructs or needs a Story service (there is none).

Synthetic only: a tmp instance and rows the test appends; no model, provider, network or operational data."""

import json
from types import SimpleNamespace

import pytest
from qorqut_operations.application.plans import PlanBuilder
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from host_foundation_support import (build_instance, create_registered_global, create_worker, historical_business_agent, register_sources,
                                     set_agent_overrides)


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    # Businesses have no resident agents: Ordubashy does the ordinary business work selecting the first business, and a second
    # worker is the independent peer, selecting the second business or the Global target (one identity, deliberately reused).
    agent, other = create_registered_global(instance, "ordubashy"), create_worker(instance, "colleague")
    set_agent_overrides(instance, other, {"notes_get": "allow"}, [first, second])        # a worker reads a note only when granted
    global_agent = other
    runtime = instance.runtime
    runtime.freeze_registry()
    return SimpleNamespace(
        instance=instance, runtime=runtime, first=first, second=second, agent=agent, other=other,
        owner=instance.human(first), owner_second=instance.human(second),
        owner_global=InvocationContext(Principal(instance.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES,
                                       source="dashboard"),
        agent_context=runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=first),
        agent_global=runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=None),
        other_context=runtime.resolver.invocation_context(Principal(other, "agent"), business_id=second),
        global_context=runtime.resolver.invocation_context(Principal(global_agent, "agent"), business_id=None))


def call(host, ctx, operation, arguments, *, revision=None, key=None):
    return host.runtime.application.invoke(ctx, operation, arguments, InvocationOptions(
        idempotency_key=key or new_id(), expected_revision=revision))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def resolve(host, ctx, item):
    """The neutral resolver's answer (what Tasks, documents and Focus all call), as data or an error code."""
    with host.instance.database.read() as uow:
        try:
            return host.runtime.domains.items.resolve(ctx, item, uow)
        except DomainError as error:
            return error.code


def rows(host, sql, *parameters):
    with host.instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def reclassify(host, document_id, kind):
    """A synthetic row-classification change (the legacy private kinds are no longer creatable by an operation)."""
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE documents SET kind=? WHERE id=?", (kind, document_id))


def agent_note(host, title="Agent note"):
    """The global agent's private note, written while it selects the first business: it lives in the agent's global workspace
    (tagged with that business), since businesses have no resident agents."""
    return ok(call(host, host.agent_context, "notes_create", {"title": title, "body": "private text\n"}))["document"]


def business_material(host, title="Business note"):
    """A business-owned record of the first business (a strategy note), the material its Tasks and reviews relate."""
    return ok(call(host, host.owner, "notes_create", {"title": title, "body": "business text\n", "target_business_id": host.first}))["document"]


def task(host, title="Plan launch"):
    return ok(call(host, host.owner, "tasks_create", {"title": title, "scope": "business"}))["task"]


def test_a_global_agents_private_note_resolves_only_in_the_global_scope(host):
    note = agent_note(host)
    item = {"type": "material", "id": note["id"]}
    # The note is the global agent's own Global record: its author and the owner resolve it in the Global scope.
    for ctx in (host.owner_global, host.agent_global):
        assert resolve(host, ctx, item)["resource_type"] == "documents"
    # No business selection reaches it, not even the business it was written for, nor the author's own selection of it.
    for ctx in (host.owner, host.agent_context, host.other_context, host.owner_second):
        assert resolve(host, ctx, item) == "not_found"
    # The retained v1 Global document rule: another global agent holding read_global_context reads Global agent notes.
    found = resolve(host, host.global_context, item)
    assert not isinstance(found, str), found
    assert found["resource_type"] == "documents"


def test_owner_only_journal_and_excluded_working_memory_are_not_item_sources(host):
    note = agent_note(host)
    item = {"type": "material", "id": note["id"]}
    # A legacy journal kind is owner-only: the owner reads it, the registered agent no longer can.
    reclassify(host, note["id"], "business_note")
    assert resolve(host, host.owner_global, item)["resource_type"] == "documents"
    assert resolve(host, host.agent_global, item) == "not_found"
    # Working memory is never a shared item, for anyone.
    reclassify(host, note["id"], "working_memory")
    assert resolve(host, host.owner_global, item) == "not_found" and resolve(host, host.agent_global, item) == "not_found"

    # The Global journal (the owner's) resolves for the owner and not for a Global agent holding read_global_context.
    journal = ok(call(host, host.owner_global, "notes_create", {"title": "Journal", "body": "owner journal\n"}))["document"]
    entry = {"type": "material", "id": journal["id"]}
    assert resolve(host, host.owner_global, entry)["label"] == "Journal"
    assert resolve(host, host.global_context, entry) == "not_found"


def test_global_mailbox_discussion_is_readable_only_through_account_authority(host):
    now = "2026-10-11T12:00:00Z"
    with host.instance.database.transaction() as uow:
        uow.repository("email_mailboxes", business_id=None).insert({
            "id": "owner-mail", "business_id": None, "address": "owner@example.test", "purpose": "owner_mail",
            "created_at": now, "updated_at": now})
        uow.repository("email_conversations", business_id=None).insert({
            "id": "c0000000-0000-4000-8000-000000000001", "mailbox": "owner-mail", "provider_id": "thread-x",
            "subject": "Private subject", "created_at": now, "updated_at": now})
    item = {"type": "material", "id": "c0000000-0000-4000-8000-000000000001"}
    assert resolve(host, host.owner_global, item)["label"] == "Private subject"
    # A Global agent without the private-account grant is refused, not told the row does not exist.
    assert resolve(host, host.global_context, item) == "forbidden"
    # The same row is invisible from a business scope.
    assert resolve(host, host.owner, item) == "not_found"


def test_people_and_agents_resolve_by_current_membership_and_registered_scope(host):
    stranger = new_id()
    with host.instance.database.transaction() as uow:
        uow.repository("principals").insert({"id": stranger, "kind": "human", "display_name": "Stranger"})
    assert resolve(host, host.owner, {"type": "person", "id": host.instance.owner})["label"] == "Owner"
    assert resolve(host, host.owner, {"type": "person", "id": stranger}) == "not_found"
    # A global agent item resolves as current in the Global scope and in every active selected business, without a
    # membership and never as a business's own row.
    for agent in (host.agent, host.other):
        for ctx in (host.owner_global, host.owner, host.owner_second):
            view = resolve(host, ctx, {"type": "agent", "id": agent})
            assert view["resource_type"] == "agent_profiles" and view["archived"] is False
    # A historical business-home identity stays a record of its own business only, read as archived.
    historical = historical_business_agent(host.instance, host.first, status="inactive")
    assert resolve(host, host.owner, {"type": "agent", "id": historical})["archived"] is True
    assert resolve(host, host.owner_second, {"type": "agent", "id": historical}) == "not_found"
    with host.instance.database.transaction() as uow:  # a retired agent profile
        uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (host.agent,))
    assert resolve(host, host.owner_global, {"type": "agent", "id": host.agent})["archived"] is True


def test_items_are_scoped_to_their_business_and_enumerated_only_inside_it(host):
    own = task(host, "First business task")
    foreign = ok(call(host, host.owner_second, "tasks_create", {"title": "Second business task", "scope": "business"}))["task"]
    assert resolve(host, host.owner, {"type": "task", "id": own["id"]})["label"] == "First business task"
    assert resolve(host, host.owner, {"type": "task", "id": foreign["id"]}) == "not_found"
    assert resolve(host, host.agent_context, {"type": "task", "id": foreign["id"]}) == "not_found"
    first = {entry["id"] for entry in ok(call(host, host.owner, "context_items_list", {"type": "task"}))["items"]}
    second = {entry["id"] for entry in ok(call(host, host.owner_second, "context_items_list", {"type": "task"}))["items"]}
    assert own["id"] in first and foreign["id"] not in first and second == {foreign["id"]}


def test_a_busy_document_reads_last_committed_metadata_and_a_fingerprint_expects_its_file_target(host):
    note = agent_note(host)
    item = {"type": "material", "id": note["id"]}
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE documents SET availability='applying' WHERE id=?", (note["id"],))
    with host.instance.database.read() as uow:
        with pytest.raises(DomainError) as busy:
            host.runtime.domains.related.get(host.owner_global, uow, "documents", note["id"])
        committed = host.runtime.domains.related.get(host.owner_global, uow, "documents", note["id"], allow_busy=True)
    assert busy.value.code == "busy"
    assert committed["label"] == "Agent note" and committed["revision"] == 1 and committed["kind"] == "agent_note"
    # Focus-style resolution tolerates the unfinished application for the same reason; a plain read does not.
    assert resolve(host, host.owner_global, item) == "busy"
    with host.instance.database.read() as uow:
        assert host.runtime.domains.items.resolve(host.owner_global, item, uow, allow_busy=True)["revision"] == 1

    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE documents SET availability='ready' WHERE id=?", (note["id"],))
    with host.instance.database.read() as uow:
        builder = PlanBuilder(host.owner_global, "probe", {}, "probe")
        token = host.runtime.domains.items.fingerprint(host.owner_global, item, uow, builder)
        document = uow.connection.execute("SELECT path,content_hash FROM documents WHERE id=?", (note["id"],)).fetchone()
        again = host.runtime.domains.items.fingerprint(host.owner_global, item, uow)
    files = [(resource, target) for resource, target in builder.targets.items() if resource.resource_type == "managed_document_files"]
    assert len(files) == 1 and token == again and len(token) == 64
    resource, target = files[0]
    assert json.loads(resource.resource_id) == {"document_id": note["id"], "path": document["path"]}
    assert target.exists and target.content_hash


def related_note_and_task(host):
    """A task and a business note related to it, plus the owner's current observation of that pending review."""
    work, note = task(host), business_material(host)
    edge = ok(call(host, host.owner, "relationships_create", {
        "source": {"type": "material", "id": note["id"]}, "target": {"type": "task", "id": work["id"]},
        "role": "concerns"}))["relationship"]
    pending = ok(call(host, host.owner, "context_reviews_list", {"item": {"type": "task", "id": work["id"]}}))["items"]
    assert [entry["relationship_id"] for entry in pending] == [edge["id"]]
    return work, note, edge, pending[0]


def review_arguments(work, edge, observed, *, edit=None, token=None, explanation="Reviewed the note"):
    arguments = {"item": {"type": "task", "id": work["id"]}, "items": [{
        "relationship_id": edge["id"], "relationship_revision": edge["revision"],
        "review_revision": observed["review_revision"], "source_token": token or observed["source_token"],
        "result": "incorporated", "explanation": explanation}]}
    return {**arguments, "edit": edit} if edit else arguments


def edit_task(work, **changes):
    return {"operation": "tasks_update", "arguments": {"task_id": work["id"], "changes": changes}}


def review_state(host):
    return (rows(host, "SELECT COUNT(*) AS n FROM context_reviews")[0]["n"],
            rows(host, "SELECT COUNT(*) AS n FROM context_review_states")[0]["n"],
            rows(host, "SELECT title,revision FROM tasks")[0])


def test_a_review_and_an_edit_to_the_same_item_commit_together_and_otherwise_not_at_all(host):
    work, note, edge, observed = related_note_and_task(host)
    before = review_state(host)
    assert before == (0, 0, {"title": "Plan launch", "revision": 1})

    def refused(result, code):
        assert result.status == "error" and result.error["code"] == code, result.error
        assert review_state(host) == before  # nothing partial: no decision, no review state, no edit

    # The edit must be to the reviewed item, valid under its own operation, and the observed source must be current.
    other = {"operation": "tasks_update", "arguments": {"task_id": new_id(), "changes": {"title": "Elsewhere"}}}
    refused(call(host, host.owner, "context_review", review_arguments(work, edge, observed, edit=other), revision=1), "validation_error")
    refused(call(host, host.owner, "context_review", review_arguments(work, edge, observed, edit=edit_task(work, status="bogus")), revision=1), "validation_error")
    refused(call(host, host.owner, "context_review", review_arguments(work, edge, observed, edit=edit_task(work, title="Plan launch v2"), token="0" * 64), revision=1), "revision_conflict")
    refused(call(host, host.owner, "context_review", review_arguments(work, edge, observed, edit=edit_task(work, title="Plan launch v2")), revision=7), "revision_conflict")
    # The same global agent (it holds the review grant) selecting another business cannot even see the item.
    elsewhere = host.runtime.resolver.invocation_context(Principal(host.agent, "agent"), business_id=host.second)
    refused(call(host, elsewhere, "context_review", review_arguments(work, edge, observed, edit=edit_task(work, title="Hijack")), revision=1), "not_found")

    reviewed = ok(call(host, host.owner, "context_review", review_arguments(work, edge, observed, edit=edit_task(work, title="Plan launch v2")), revision=1))
    assert reviewed["item"]["label"] == "Plan launch v2" and len(reviewed["review_ids"]) == 1
    assert review_state(host) == (1, 1, {"title": "Plan launch v2", "revision": 2})
    # Reviewing again needs an explicit reopen; the immutable decision stays.
    again = call(host, host.owner, "context_review", review_arguments(work, edge, observed), revision=2)
    assert again.status == "error" and again.error["code"] == "revision_conflict"
    assert review_state(host)[0] == 1


def test_a_review_whose_edit_needs_a_signoff_waits_for_the_owner_and_then_applies_atomically(host):
    work, note, edge, observed = related_note_and_task(host)
    # The edit's own Signoff floor: the owner sets it explicitly for this protected Task change.
    set_agent_overrides(host.instance, host.agent, {"tasks_update": "signoff"}, [host.first])
    agent_context = host.runtime.resolver.invocation_context(Principal(host.agent, "agent"), business_id=host.first)
    arguments = review_arguments(work, edge, observed, edit=edit_task(work, title="Plan launch v2"))

    # The global agent's composed review in the business inherits the edit's own Signoff floor: nothing happens until approval.
    proposed = call(host, agent_context, "context_review", arguments, revision=work["revision"])
    assert proposed.status == "signoff_required", proposed.error
    assert rows(host, "SELECT title,revision FROM tasks") == [{"title": "Plan launch", "revision": 1}]
    assert rows(host, "SELECT COUNT(*) AS n FROM context_reviews")[0]["n"] == 0
    signoff = rows(host, "SELECT id,state,proposal_revision,payload_hash,status_revision FROM signoffs")[-1]
    assert signoff["state"] == "pending"

    decision = host.runtime.signoffs.review(
        host.owner, signoff["id"], decision="approve", proposal_revision=signoff["proposal_revision"],
        payload_hash=signoff["payload_hash"], expected_status_revision=signoff["status_revision"], idempotency_key=new_id())
    assert decision.status == "accepted", decision.error
    assert host.runtime.signoffs.run_pending() == 1
    assert rows(host, "SELECT title,revision FROM tasks") == [{"title": "Plan launch v2", "revision": 2}]
    reviews = rows(host, "SELECT actor_id,result FROM context_reviews")
    assert reviews == [{"actor_id": host.agent, "result": "incorporated"}]
    assert rows(host, "SELECT state FROM signoffs WHERE id=?", signoff["id"]) == [{"state": "applied"}]


def test_material_archive_and_navigation_adapters_compose_the_owning_domains(host):
    topic = ok(call(host, host.owner, "context_topics_create", {"title": "Launch", "slug": "launch"}))["document"]
    material = ok(call(host, host.owner, "context_notes_create", {
        "topic_id": topic["id"], "title": "Launch notes", "body": "Plan.\n", "material_type": "note"}))["document"]
    item = {"type": "material", "id": material["id"]}

    classified = ok(call(host, host.owner, "materials_update", {
        "material_id": material["id"], "changes": {"material_type": "excerpt", "tags": ["draft"]}}, revision=material["revision"]))
    assert classified["item"]["material_type"] == "excerpt" and classified["item"]["tags"] == ["draft"]
    # Agent notes are not Materials: classification refers the caller to the item's dedicated tools. A business-scoped agent
    # note is only historical now (a v1 business agent's), so the case reclassifies a business row to that kind.
    private = business_material(host, "Historical agent note")
    reclassify(host, private["id"], "agent_note")
    refused = call(host, host.owner, "materials_update", {"material_id": private["id"], "changes": {"material_type": "note"}},
                   revision=private["revision"])
    assert refused.status == "error" and refused.error["code"] == "workflow_required"

    # Archive and restore go through the owning domain's operation (a Task here, a document there).
    work = task(host)
    archived = ok(call(host, host.owner, "context_items_archive", {"item": {"type": "task", "id": work["id"]}, "archived": True}, revision=work["revision"]))
    assert archived["item"]["archived"] is True
    restored = ok(call(host, host.owner, "context_items_archive", {"item": {"type": "task", "id": work["id"]}, "archived": False}, revision=archived["item"]["revision"]))
    assert restored["item"]["archived"] is False
    current = resolve(host, host.owner, item)
    gone = ok(call(host, host.owner, "context_items_archive", {"item": item, "archived": True}, revision=current["revision"]))
    assert gone["item"]["archived"] is True
    assert material["id"] not in {entry["id"] for entry in ok(call(host, host.owner, "context_items_list", {}))["items"]}

    counts = ok(call(host, host.owner, "context_navigation_get", {}))
    assert counts["navigation_only"] is True and set(counts["counts"]) == {"table", "person"}
    # No shared Context graph exists in this fixture, so the person count is unknown rather than a guess.
    assert counts["counts"]["person"] is None and counts["counts"]["table"] == 0


def test_no_relationship_is_a_protected_edge(host):
    work, parent = task(host, "Child"), task(host, "Parent")
    edge = ok(call(host, host.owner, "relationships_create", {
        "source": {"type": "task", "id": work["id"]}, "target": {"type": "task", "id": parent["id"]}, "role": "part_of"}))["relationship"]
    updated = ok(call(host, host.owner, "relationships_update", {"relationship_id": edge["id"], "changes": {"context": "child of parent"}},
                      revision=edge["revision"]))["relationship"]
    removed = call(host, host.owner, "relationships_remove", {"relationship_id": edge["id"]}, revision=updated["revision"])
    assert removed.status == "ok", removed.error                      # no setup (or any other domain) locks an edge against its owner
    assert rows(host, "SELECT COUNT(*) AS n FROM relationships WHERE id=?", edge["id"]) == [{"n": 0}]


def test_no_domain_service_holds_a_story_service_and_ordinary_reads_do_not_need_one(host):
    domains = host.runtime.domains
    # Structurally: the host has no Story service, and no retained domain keeps a back-reference to one.
    assert not hasattr(domains, "stories")
    for holder in ("documents", "tables", "data_uploads", "assets", "tasks", "items", "related", "relationships", "reviews"):
        assert not hasattr(getattr(domains, holder), "stories"), holder
    assert host.runtime.focus.items is domains.items
    assert not [d.name for d in host.runtime.application.registry.installed() if d.name.startswith("stories_")]

    # Behaviorally: Task, document, relationship, review and Focus reads and the same-item review edit all work.
    work, note, edge, observed = related_note_and_task(host)
    assert ok(call(host, host.owner, "tasks_get", {"task_id": work["id"]}))["task"]["title"] == "Plan launch"
    assert ok(call(host, host.owner, "context_items_get", {"item": {"type": "material", "id": note["id"]}}))["item"]["label"] == "Business note"
    assert ok(call(host, host.owner, "relationships_get", {"relationship_id": edge["id"]}))["relationship"]["source_item"]["label"] == "Business note"
    ok(call(host, host.owner, "context_review", review_arguments(work, edge, observed, edit=edit_task(work, title="Reviewed")), revision=1))
    with host.instance.database.read() as uow:
        card = host.runtime.focus.resolve(host.owner, {"type": "task", "id": work["id"], "business_id": host.first,
                                                       "reason": "Reviewed", "needs_input": False}, uow)
    assert card["label"] == "Reviewed" and card["resource_type"] == "tasks" and card["business_slug"] == "first"
