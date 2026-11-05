"""Acceptance case: the retained Global work contract, composed on the host and the Operations feature wheel.

One synthetic host (two businesses, three registered global agents, the retained Signoff service) behind the
real HTTP application proves what the eight neutral relationship operations, the Task follow and the Action discussion and
shared-tag operations promise on the owner root route:

* the owner builds a Global parent Task with children and explicit ``concerns`` links and lists, reads, updates and removes
  those links through ``/api/root/operations/...``; Global rows carry ``business_id`` NULL, and a business row is never
  visible to (or linkable from) a Global operation, nor the other way round;
* the business routes keep their behavior, the root still refuses a business selector, and one link never spans two namespaces
  or two businesses;
* an exact email-message endpoint stays authorized and is stored as its conversation;
* the root reaches ``tasks_follow``, ``actions_thread_get``, ``actions_follow``, ``actions_comment``, ``tags_list`` and
  ``tags_create`` with their own schemas (Tags and Actions take no scope), and nothing else of the Context family;
* businesses have no resident agents: a global agent selecting a business sees only that business's namespace, a historical
  business-home identity works nowhere, a Global agent needs the capability AND the operation grant, and a protected
  relationship change still goes through the retained prepared Signoff path.

The dashboard route is the installation owner's, so agent refusals go through the same application layer the agent transports
call (an agent credential cannot reach the owner root route). Synthetic only: a tmp instance and rows the test appends; no
model, provider, network, native session or operational data."""

import json
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain
from starlette.testclient import TestClient

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.background.integration import attach_background
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from host_foundation_support import (agent_context as direct_context, build_instance, create_registered_global, create_worker,
                                     global_agent_without_capability, historical_business_agent, register_sources,
                                     set_agent_overrides)

ORIGIN = {"Origin": "http://testserver"}
COLUMNS = [{"id": "metric", "name": "Metric", "type": "text"}]
NOW = "2026-10-11T12:00:00Z"
SCOPED = ("relationship_roles_list", "relationships_list", "relationships_get", "relationships_create",
          "relationships_update", "relationships_remove", "context_items_list", "context_items_get")
ALSO_REACHED = ("tasks_follow", "actions_thread_get", "actions_follow", "actions_comment", "tags_list", "tags_create")
NOT_EXPOSED = ("context_graph_get", "context_navigation_get", "context_items_archive", "materials_update")


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    # Ordinary business work is the coordinator role's; a second worker is the independent Global peer. The roster has one
    # holder per role, so the revoked-capability case needs a third identity: Learning Applier's registration.
    agent, keeper = create_registered_global(instance, "ordubashy"), create_worker(instance, "colleague")
    bare = global_agent_without_capability(instance, "learning-applier")
    runtime = instance.runtime
    attach_background(runtime, runtime.programs)
    runtime.freeze_registry()
    with instance.database.transaction() as uow:
        runtime.authority.register_workers(uow, [first, second])
    authentication = Authentication(instance.database, runtime.paths)
    dashboard = tmp_path / "dashboard"
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    app = create_app(runtime, dashboard_root=dashboard, origins=["http://testserver"], authentication=authentication,
                     manage_lifecycle=False)

    def agent_context(identity, business=None):
        return runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)

    with TestClient(app, base_url="http://testserver") as client:
        client.cookies.set("bazarlyk_session", authentication.issue_browser())
        yield SimpleNamespace(
            instance=instance, runtime=runtime, client=client, first=first, second=second, agent=agent,
            keeper=keeper, bare=bare, owner=instance.human(first), owner_second=instance.human(second),
            owner_global=InvocationContext(Principal(instance.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES,
                                           source="dashboard"),
            agent_context=agent_context(agent, first),
            keeper_context=agent_context(keeper),
            bare_context=runtime.resolver.runtime_context(Principal(bare, "agent"), business_id=None))


# -- the real HTTP application and the application layer -------------------------------------------------------------------

def post(host, path, **body):
    body.setdefault("idempotency_key", new_id())
    response = host.client.post("/api/" + path, json=body, headers=ORIGIN)
    return response.status_code, response.json()


def root(host, operation, **body):
    return post(host, "root/operations/" + operation, **body)


def business(host, slug, operation, **body):
    return post(host, "biz/" + slug + "/operations/" + operation, **body)


def good(answer):
    status, body = answer
    assert status == 200 and body["status"] == "ok", body
    return body["data"]


def refused(answer):
    status, body = answer
    assert status != 200 and body["status"] == "error", body
    return body["error"]["code"]


def call(host, ctx, operation, arguments=None, *, revision=None):
    return host.runtime.application.invoke(ctx, operation, arguments or {}, InvocationOptions(
        idempotency_key=new_id(), expected_revision=revision))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def rows(host, sql, *parameters):
    with host.instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def item(kind, identity):
    return {"type": kind, "id": identity}


def global_task(host, title, **arguments):
    return good(root(host, "tasks_create", title=title, scope="global", **arguments))["task"]


def business_task(host, slug, title, **arguments):
    return good(business(host, slug, "tasks_create", title=title, **arguments))["task"]


def global_table(host, title="Results"):
    return ok(call(host, host.owner_global, "tables_create", {"scope": "global", "title": title, "columns": COLUMNS}))["table"]


def seed_mailbox(host, business_id, mailbox, address, conversation, message, purpose):
    with host.instance.database.transaction() as uow:
        uow.repository("email_mailboxes", business_id=business_id).insert({
            "id": mailbox, "business_id": business_id, "address": address, "purpose": purpose,
            "created_at": NOW, "updated_at": NOW})
        uow.repository("email_conversations", business_id=business_id).insert({
            "id": conversation, "business_id": business_id, "mailbox": mailbox, "provider_id": "thread-" + conversation,
            "subject": "Subject " + conversation[-4:], "created_at": NOW, "updated_at": NOW})
        uow.repository("email_messages", business_id=business_id).insert({
            "id": message, "business_id": business_id, "conversation_id": conversation, "mailbox": mailbox,
            "provider_id": "message-" + message, "message_json": {"subject": "Subject", "text": "private body"},
            "created_at": NOW, "updated_at": NOW})


def global_mail(host):
    ids = {"conversation": new_id(), "message": new_id()}
    seed_mailbox(host, None, "owner-mail", "owner@example.test", ids["conversation"], ids["message"], "owner_mail")
    return SimpleNamespace(**ids)


def set_global_overrides(host, agent_id, overrides):
    """The owner's explicit permission overrides for a Global agent (it has no home business)."""
    revision = rows(host, "SELECT revision FROM agent_profiles WHERE agent_id=?", agent_id)[0]["revision"]
    ok(call(host, host.owner_global, "permissions_set", {"agent_id": agent_id, "overrides": overrides}, revision=revision))
    host.keeper_context = host.runtime.resolver.invocation_context(Principal(host.keeper, "agent"), business_id=None)


def link_args(source, target, role="concerns", **extra):
    return {"source": source, "target": target, "role": role, **extra}


def stored(host, identity):
    return rows(host, "SELECT * FROM relationships WHERE id=?", identity)


# -- 1. the owner's Global parent Task through the root routes --------------------------------------------------------------

def test_the_owner_lists_gets_updates_and_removes_global_work_links_through_the_root_routes(host):
    parent = global_task(host, "Spring launch")
    first_child = global_task(host, "Write copy", parent_id=parent["id"])
    second_child = global_task(host, "Schedule posts", parent_id=parent["id"])
    table = global_table(host)
    mail = global_mail(host)
    assert parent["business_id"] is None and first_child["parent_id"] == parent["id"]

    made = {}
    for label, source in (("child", item("task", first_child["id"])), ("table", item("table", table["id"])),
                          ("mail", item("material", mail.conversation))):
        made[label] = good(root(host, "relationships_create", scope="global", **link_args(source, item("task", parent["id"]))))[
            "relationship"]
        assert made[label]["business_id"] is None and made[label]["role"] == "concerns"
        assert stored(host, made[label]["id"])[0]["business_id"] is None
    # An unscoped root call is the Global namespace too (the root has no business), and reaches the same rows.
    unscoped = good(root(host, "relationships_list", target=item("task", parent["id"])))["items"]
    assert {entry["id"] for entry in unscoped} == {value["id"] for value in made.values()}

    listed = good(root(host, "relationships_list", scope="global", target=item("task", parent["id"]), role="concerns"))
    assert {entry["source"]["id"] for entry in listed["items"]} == {first_child["id"], table["id"], mail.conversation}
    assert all(entry["business_id"] is None for entry in listed["items"])
    one = good(root(host, "relationships_get", scope="global", relationship_id=made["table"]["id"]))["relationship"]
    assert one["source"] == item("table", table["id"]) and one["target"] == item("task", parent["id"])
    assert "concerns" in json.dumps(good(root(host, "relationship_roles_list", scope="global")))

    # Items are read through the same namespace: the Global Tasks are there, the other children are just Tasks.
    tasks = {entry["id"] for entry in good(root(host, "context_items_list", scope="global", type="task"))["items"]}
    assert {parent["id"], first_child["id"], second_child["id"]} <= tasks
    resolved = good(root(host, "context_items_get", scope="global", item=item("task", parent["id"])))
    assert "Spring launch" in json.dumps(resolved)

    # Update with the current revision; a stale revision is refused and changes nothing.
    changed = good(root(host, "relationships_update", scope="global", relationship_id=made["table"]["id"],
                        changes={"context": "Results of the launch"}, expected_revision=made["table"]["revision"]))["relationship"]
    assert changed["context"] == "Results of the launch" and changed["revision"] == made["table"]["revision"] + 1
    assert refused(root(host, "relationships_update", scope="global", relationship_id=made["table"]["id"],
                        changes={"context": "Late edit"}, expected_revision=made["table"]["revision"])) == "revision_conflict"
    assert stored(host, made["table"]["id"])[0]["context"] == "Results of the launch"
    # A Global edge stays a `concerns` edge to a Global Task (the storage constraint), whether made or edited.
    assert refused(root(host, "relationships_update", scope="global", relationship_id=made["table"]["id"],
                        changes={"role": "related_to"}, expected_revision=changed["revision"])) == "validation_error"
    assert refused(root(host, "relationships_create", scope="global", **link_args(
        item("table", table["id"]), item("task", parent["id"]), "related_to"))) == "validation_error"
    assert stored(host, made["table"]["id"])[0]["role"] == "concerns"

    removed = good(root(host, "relationships_remove", scope="global", relationship_id=made["child"]["id"],
                        expected_revision=made["child"]["revision"]))
    assert removed == {"removed_id": made["child"]["id"]} and stored(host, made["child"]["id"]) == []
    # Removing a link keeps both items; hierarchy is the Task's own parent link, not the removed edge.
    after = good(root(host, "tasks_get", task_id=first_child["id"], scope="global"))["task"]
    assert after["parent_id"] == parent["id"] and rows(host, "SELECT id FROM data_tables WHERE id=?", table["id"])
    assert refused(root(host, "relationships_remove", scope="global", relationship_id=made["child"]["id"],
                        expected_revision=made["child"]["revision"])) == "not_found"


# -- 2. business counterparts keep their behavior; the namespaces never mix --------------------------------------------------

def test_business_counterparts_keep_their_behavior_and_the_namespaces_never_mix(host):
    parent = global_task(host, "Global parent")
    child = global_task(host, "Global child", parent_id=parent["id"])
    alpha, beta = business_task(host, "first", "Alpha"), business_task(host, "first", "Beta")
    foreign = business_task(host, "second", "Elsewhere")

    local = good(business(host, "first", "relationships_create", **link_args(item("task", beta["id"]), item("task", alpha["id"]))))[
        "relationship"]
    assert local["business_id"] == host.first and stored(host, local["id"])[0]["business_id"] == host.first
    global_link = good(root(host, "relationships_create", scope="global", **link_args(item("task", child["id"]), item("task", parent["id"]))))[
        "relationship"]
    # Each namespace lists only its own rows, and the other's id is simply not there.
    assert [entry["id"] for entry in good(business(host, "first", "relationships_list"))["items"]] == [local["id"]]
    assert [entry["id"] for entry in good(root(host, "relationships_list", scope="global"))["items"]] == [global_link["id"]]
    assert refused(root(host, "relationships_get", scope="global", relationship_id=local["id"])) == "not_found"
    assert refused(business(host, "first", "relationships_get", relationship_id=global_link["id"])) == "not_found"
    assert refused(business(host, "second", "relationships_get", relationship_id=local["id"])) == "not_found"
    # Business Tasks are not Global items, and Global Tasks are not business items.
    global_items = {entry["id"] for entry in good(root(host, "context_items_list", scope="global", type="task"))["items"]}
    business_items = {entry["id"] for entry in good(business(host, "first", "context_items_list", type="task"))["items"]}
    assert {parent["id"], child["id"]} <= global_items and not ({alpha["id"], beta["id"], foreign["id"]} & global_items)
    assert {alpha["id"], beta["id"]} <= business_items and not ({parent["id"], child["id"], foreign["id"]} & business_items)
    assert refused(root(host, "context_items_get", scope="global", item=item("task", alpha["id"]))) == "not_found"
    assert refused(business(host, "first", "context_items_get", item=item("task", parent["id"]))) == "not_found"

    # Both endpoints are re-resolved in the one namespace: nothing links a business row to a Global row or across businesses.
    assert refused(root(host, "relationships_create", scope="global", **link_args(item("task", alpha["id"]), item("task", parent["id"])))) == "not_found"
    assert refused(root(host, "relationships_create", scope="global", **link_args(item("task", child["id"]), item("task", alpha["id"])))) == "not_found"
    assert refused(business(host, "first", "relationships_create", **link_args(item("task", alpha["id"]), item("task", parent["id"])))) == "not_found"
    assert refused(business(host, "first", "relationships_create", **link_args(item("task", foreign["id"]), item("task", alpha["id"])))) == "not_found"
    assert rows(host, "SELECT COUNT(*) AS n FROM relationships")[0]["n"] == 2
    # A link edit cannot move an endpoint across namespaces either.
    assert refused(business(host, "first", "relationships_update", relationship_id=local["id"],
                            changes={"target": item("task", parent["id"])}, expected_revision=local["revision"])) == "not_found"
    assert refused(root(host, "relationships_update", scope="global", relationship_id=global_link["id"],
                        changes={"target": item("task", alpha["id"])}, expected_revision=global_link["revision"])) == "not_found"
    assert stored(host, local["id"])[0]["to_id"] == alpha["id"] and stored(host, global_link["id"])[0]["to_id"] == parent["id"]

    # The root still takes no business selector.
    assert refused(root(host, "relationships_list", business_id=host.first)) == "forbidden"
    assert refused(root(host, "relationships_create", review_business_id=host.first,
                        **link_args(item("task", child["id"]), item("task", parent["id"])))) == "forbidden"
    # The owner's business route already reaches the Global namespace for Tasks (the existing owner-or-authorized-global-agent
    # check); the relationship reads follow it exactly and show the Global rows, never the business ones.
    assert good(business(host, "first", "tasks_list", scope="global")) == good(root(host, "tasks_list", scope="global"))
    assert [entry["id"] for entry in good(business(host, "first", "relationships_list", scope="global"))["items"]] == [global_link["id"]]
    # The business owner keeps updating and removing the business link as before.
    updated = good(business(host, "first", "relationships_update", relationship_id=local["id"], changes={"context": "Beta follows Alpha"},
                            expected_revision=local["revision"]))["relationship"]
    assert updated["context"] == "Beta follows Alpha"
    good(business(host, "first", "relationships_remove", relationship_id=local["id"], expected_revision=updated["revision"]))
    assert stored(host, local["id"]) == [] and stored(host, global_link["id"])


# -- 3. exact email-message resolution stays authorized and links the conversation --------------------------------------------

def test_exact_email_message_endpoints_stay_authorized_and_link_the_conversation(host):
    mail = global_mail(host)
    own = SimpleNamespace(conversation=new_id(), message=new_id())
    seed_mailbox(host, host.first, "first-mail", "first@example.test", own.conversation, own.message, "business_mail")
    parent = global_task(host, "Owner correspondence")
    local = business_task(host, "first", "Reply to the supplier")

    # The exact Global message is resolved by the owner and stored as its conversation.
    link = good(root(host, "relationships_create", scope="global", **link_args(
        item("material", mail.message), item("task", parent["id"]))))["relationship"]
    assert link["source"] == item("material", mail.conversation)
    assert rows(host, "SELECT from_type, from_id, business_id FROM relationships WHERE id=?", link["id"]) == [
        {"from_type": "material", "from_id": mail.conversation, "business_id": None}]
    # The same business message links to a business Task as its conversation, and each mailbox is invisible to the other namespace.
    business_link = good(business(host, "first", "relationships_create", **link_args(
        item("material", own.message), item("task", local["id"]))))["relationship"]
    assert business_link["source"] == item("material", own.conversation) and business_link["business_id"] == host.first
    assert refused(root(host, "relationships_create", scope="global", **link_args(
        item("material", own.message), item("task", parent["id"])))) == "not_found"
    assert refused(business(host, "first", "relationships_create", **link_args(
        item("material", mail.message), item("task", local["id"])))) == "not_found"
    assert refused(business(host, "second", "context_items_get", item=item("material", own.message))) == "not_found"

    # The Global mailbox is read through account authority: a Global agent without the private-account grant is refused,
    # and neither agent of the businesses can see it.
    assert code(call(host, host.keeper_context, "relationships_create", link_args(
        item("material", mail.message), item("task", parent["id"]), scope="global"))) == "forbidden"
    assert code(call(host, host.keeper_context, "context_items_get", {"scope": "global", "item": item("material", mail.message)})) == "forbidden"
    assert code(call(host, host.agent_context, "context_items_get", {"item": item("material", mail.message)})) == "not_found"


# -- 4. Task follow, Action discussion and shared tags on the root route -----------------------------------------------------

def test_the_root_reaches_task_follow_action_discussion_and_shared_tags_with_their_own_schemas(host):
    task = global_task(host, "Global follow")
    assert good(root(host, "tasks_follow", task_id=task["id"], following=False))["following"] is False
    assert good(root(host, "tasks_follow", task_id=task["id"], following=True))["following"] is True

    tag = good(root(host, "tags_create", label="spring"))["tag"]
    assert tag["label"] == "spring"
    assert [entry["label"] for entry in good(root(host, "tags_list"))["tags"]] == ["spring"]
    # Tags and Actions declare no scope input, so a scope is a schema error, never silently ignored.
    for operation, arguments in (("tags_create", {"label": "x"}), ("tags_list", {})):
        assert refused(root(host, operation, scope="global", **arguments)) == "validation_error"

    action = good(root(host, "actions_record", text="Published the spring update", category="global-operational"))["action"]
    identity = action["action_id"]
    thread = good(root(host, "actions_thread_get", action_id=identity))
    assert thread["action_id"] == identity and thread["messages"] == []
    assert good(root(host, "actions_follow", action_id=identity, following=True))["following"] is True
    for operation, arguments in (("actions_thread_get", {"action_id": identity}), ("actions_follow", {"action_id": identity, "following": True}),
                                 ("actions_comment", {"action_id": identity, "text": "Noted"})):
        assert refused(root(host, operation, scope="global", **arguments)) == "validation_error"
    # The owner keeps using the private channel rather than the shared Action discussion, exactly as before.
    assert refused(root(host, "actions_comment", action_id=identity, text="Owner note")) == "channel_required"
    assert rows(host, "SELECT COUNT(*) AS n FROM thread_messages WHERE item_type='action'")[0]["n"] == 0

    # A Global agent with the grant comments, and the owner then reads the discussion through the root.
    set_global_overrides(host, host.keeper, {"actions_comment": "allow"})
    comment = ok(call(host, host.keeper_context, "actions_comment", {"action_id": identity, "text": "Agent note"}))
    assert comment["message"]["body"] == "Agent note" and comment["message"]["item_type"] == "action"
    assert [entry["body"] for entry in good(root(host, "actions_thread_get", action_id=identity))["messages"]] == ["Agent note"]

    # The business routes are unchanged for the same operations.
    local = business_task(host, "first", "Business follow")
    assert good(business(host, "first", "tasks_follow", task_id=local["id"], following=True))["following"] is True
    assert good(business(host, "first", "tags_list"))["tags"]
    assert refused(business(host, "first", "tags_create", label="spring", scope="global")) == "validation_error"


def test_nothing_else_of_the_context_family_is_exposed_on_the_root_route(host):
    installed = {descriptor.name for descriptor in host.runtime.application.registry.installed()}
    assert set(SCOPED) | set(ALSO_REACHED) <= installed
    assert set(NOT_EXPOSED) <= installed
    for name in NOT_EXPOSED:
        status, body = root(host, name)
        assert status == 404 and body["error"]["code"] == "not_found", (name, body)
    status, body = root(host, "context_graph_get", scope="global")
    assert status == 404


# -- 5. agents: business refusal, capability, grant and foreign endpoints --------------------------------------------------

def test_agents_work_in_the_global_namespace_only_with_the_capability_and_the_operation_grant(host):
    parent = global_task(host, "Global parent")
    child = global_task(host, "Global child")
    alpha = business_task(host, "first", "Alpha")
    foreign = business_task(host, "second", "Elsewhere")
    global_link = good(root(host, "relationships_create", scope="global", **link_args(item("task", child["id"]), item("task", parent["id"]))))[
        "relationship"]

    # A historical business-home identity works in no namespace, whichever operation it names.
    historical = historical_business_agent(host.instance, host.first)
    for operation, arguments in (("relationships_list", {}), ("relationship_roles_list", {}), ("context_items_list", {"type": "task"}),
                                 ("relationships_get", {"relationship_id": global_link["id"]}),
                                 ("context_items_get", {"item": item("task", parent["id"])}),
                                 ("relationships_create", link_args(item("task", child["id"]), item("task", parent["id"])))):
        for business_id, scope in ((host.first, {}), (None, {"scope": "global"})):
            assert code(call(host, direct_context(historical, business_id), operation, {**arguments, **scope})) == "inactive_agent"
    # A global agent selecting a business works in that business's namespace, which never shows the Global row or another
    # business's rows.
    assert ok(call(host, host.agent_context, "relationships_list", {}))["items"] == []
    assert code(call(host, host.agent_context, "relationships_get", {"relationship_id": global_link["id"]})) == "not_found"
    assert code(call(host, host.agent_context, "relationships_create", link_args(
        item("task", foreign["id"]), item("task", alpha["id"])))) == "not_found"

    # A Global agent without the capability cannot even be oriented for ordinary work, and the application refuses each of the
    # eight operations for the identity-only context the trusted paths build for it.
    with pytest.raises(DomainError) as unoriented:
        host.runtime.resolver.invocation_context(Principal(host.bare, "agent"), business_id=None)
    assert unoriented.value.code == "invalid_scope"
    for operation, arguments in (("relationships_list", {}), ("relationship_roles_list", {}), ("context_items_list", {"type": "task"}),
                                 ("relationships_get", {"relationship_id": global_link["id"]}),
                                 ("context_items_get", {"item": item("task", parent["id"])}),
                                 ("relationships_create", link_args(item("task", child["id"]), item("task", parent["id"]))),
                                 ("relationships_update", {"relationship_id": global_link["id"], "changes": {"context": "x"}}),
                                 ("relationships_remove", {"relationship_id": global_link["id"]})):
        assert code(call(host, host.bare_context, operation, {**arguments, "scope": "global"})) == "forbidden"
    assert stored(host, global_link["id"])[0]["context"] == ""

    # With the capability the operation grant still decides: an explicit deny stops it, an explicit allow opens exactly it.
    assert ok(call(host, host.keeper_context, "relationships_list", {"scope": "global"}))["items"]
    set_global_overrides(host, host.keeper, {"relationships_list": "deny", "relationships_create": "deny"})
    assert code(call(host, host.keeper_context, "relationships_list", {"scope": "global"})) == "policy_denied"
    assert code(call(host, host.keeper_context, "relationships_create", {
        "scope": "global", **link_args(item("task", child["id"]), item("task", parent["id"]), "related_to")})) == "policy_denied"
    set_global_overrides(host, host.keeper, {"relationships_create": "allow", "relationships_update": "allow",
                                             "relationships_remove": "allow"})
    created = ok(call(host, host.keeper_context, "relationships_create", {
        "scope": "global", **link_args(item("task", child["id"]), item("task", parent["id"]))}))["relationship"]
    assert created["business_id"] is None and stored(host, created["id"])[0]["business_id"] is None
    # A Global agent still cannot see or touch a business row, nor link one.
    assert code(call(host, host.keeper_context, "relationships_create", {
        "scope": "global", **link_args(item("task", alpha["id"]), item("task", parent["id"]))})) == "not_found"
    assert code(call(host, host.keeper_context, "context_items_get", {"scope": "global", "item": item("task", alpha["id"])})) == "not_found"
    edited = ok(call(host, host.keeper_context, "relationships_update", {
        "scope": "global", "relationship_id": created["id"], "changes": {"context": "Agent edit"}}, revision=created["revision"]))
    assert edited["relationship"]["context"] == "Agent edit"
    ok(call(host, host.keeper_context, "relationships_remove", {"scope": "global", "relationship_id": created["id"]},
            revision=edited["relationship"]["revision"]))
    assert stored(host, created["id"]) == []


# -- 6. a protected relationship change keeps the retained prepared Signoff path ------------------------------------------------

def decide(host, ctx, signoff, **changes):
    return host.runtime.signoffs.review(
        ctx, signoff["id"], decision="approve", proposal_revision=signoff["proposal_revision"],
        payload_hash=changes.get("payload_hash", signoff["payload_hash"]), expected_status_revision=signoff["status_revision"],
        idempotency_key=new_id())


def pending(host):
    return rows(host, "SELECT id, state, proposal_revision, payload_hash, status_revision, review_scope_kind, review_business_id "
                      "FROM signoffs WHERE state='pending' ORDER BY updated_sequence DESC")


def test_a_protected_global_relationship_change_still_uses_the_prepared_signoff_path(host):
    parent, child, other = global_task(host, "Parent"), global_task(host, "Child"), global_task(host, "Other")
    set_global_overrides(host, host.keeper, {"relationships_create": "signoff"})
    arguments = {"scope": "global", **link_args(item("task", child["id"]), item("task", parent["id"]))}
    proposed = call(host, host.keeper_context, "relationships_create", arguments)
    assert proposed.status == "signoff_required", proposed.error
    assert rows(host, "SELECT COUNT(*) AS n FROM relationships")[0]["n"] == 0
    signoff = pending(host)[0]
    assert signoff["review_scope_kind"] == "global" and signoff["review_business_id"] is None

    stale = decide(host, host.owner_global, signoff, payload_hash="0" * 64)
    assert stale.status == "error" and stale.error["code"] == "stale_signoff"
    assert decide(host, host.owner_global, signoff).status == "accepted"
    assert rows(host, "SELECT COUNT(*) AS n FROM relationships")[0]["n"] == 0    # approval prepares; nothing applies until the executor
    assert host.runtime.signoffs.run_pending() == 1
    applied = rows(host, "SELECT * FROM relationships")
    assert len(applied) == 1 and applied[0]["business_id"] is None and applied[0]["from_id"] == child["id"]
    assert host.runtime.signoffs.run_pending() == 0
    assert rows(host, "SELECT state FROM signoffs WHERE id=?", signoff["id"])[0]["state"] == "applied"

    # A prepared target that changed after preparation is refused when it is validated, and nothing is applied.
    second = call(host, host.keeper_context, "relationships_create", {
        "scope": "global", **link_args(item("task", other["id"]), item("task", parent["id"]))})
    assert second.status == "signoff_required", second.error
    again = pending(host)[0]
    assert decide(host, host.owner_global, again).status == "accepted"
    ok(call(host, host.owner_global, "tasks_update", {"task_id": other["id"], "changes": {"title": "Renamed"}, "scope": "global"},
            revision=other["revision"]))
    assert host.runtime.signoffs.run_pending() == 1
    assert rows(host, "SELECT state FROM signoffs WHERE id=?", again["id"])[0]["state"] == "conflicted"
    assert len(rows(host, "SELECT id FROM relationships")) == 1


def test_a_protected_business_relationship_change_keeps_the_prepared_signoff_path_in_its_business(host):
    alpha, beta = business_task(host, "first", "Alpha"), business_task(host, "first", "Beta")
    set_agent_overrides(host.instance, host.agent, {"relationships_create": "signoff"}, [host.first])
    host.agent_context = host.runtime.resolver.invocation_context(Principal(host.agent, "agent"), business_id=host.first)
    proposed = call(host, host.agent_context, "relationships_create", link_args(item("task", beta["id"]), item("task", alpha["id"])))
    assert proposed.status == "signoff_required", proposed.error
    signoff = pending(host)[0]
    assert signoff["review_scope_kind"] == "business" and signoff["review_business_id"] == host.first
    assert decide(host, host.owner, signoff).status == "accepted"
    assert host.runtime.signoffs.run_pending() == 1
    applied = rows(host, "SELECT business_id, from_id, to_id FROM relationships")
    assert applied == [{"business_id": host.first, "from_id": beta["id"], "to_id": alpha["id"]}]
    # The same request in the Global namespace cannot see the business rows; no Signoff is staged for it.
    refused_call = call(host, host.agent_context, "relationships_create", {"scope": "global", **link_args(
        item("task", beta["id"]), item("task", alpha["id"]))})
    assert code(refused_call) == "not_found" and pending(host) == []
