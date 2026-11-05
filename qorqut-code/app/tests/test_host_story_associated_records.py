"""The Story associated-records read through the real host: catalog, tool policy and both operation transports.

The fixture is the composed host behind the real HTTP application (see ``customer_host_support``): the owner's
business-or-Global ``/operations/...`` routes are exactly what the record panel calls, and the registered global agents
act through the same ``Application`` the agent transports use. Synthetic instance only; no provider, model, network or
live data."""

from __future__ import annotations

from qorqut_operations.contracts import Principal

from bazarlyk.composition.policy import resolve_operation_policy
from customer_host_support import data, host, invoke, ok, operate, refused  # noqa: F401  (host is the fixture)
from host_foundation_support import set_agent_overrides


def story(host, title, **fields):
    return data(operate(host, "first", "tasks_create", title=title, **fields))["task"]


def associated(host, task_id, record_type, *, slug="first", ctx=None, **arguments):
    """The read through the ordinary business-or-Global operation transport, or through the agent application path."""
    arguments = {"task_id": task_id, "record_type": record_type, **arguments}
    if ctx is not None:
        return ok(invoke(host, ctx, "tasks_associated_records", arguments))
    return data(operate(host, slug, "tasks_associated_records", **arguments))


def test_the_operations_route_reads_actions_and_decided_signoffs_across_the_story(host):
    root = story(host, "Launch", assignee_ids=[host.agent])
    child = story(host, "Write the notes", parent_id=root["id"])
    grandchild = story(host, "Draft the page", parent_id=child["id"])
    # A business-stored Action: an agent without read_global_context records in the selected project.
    action = ok(invoke(host, host.bare_context, "actions_record", {"text": "Drafted the page", "task_id": grandchild["id"]}))["action"]
    assert action["action_id"].startswith("business/" + host.first + "/")
    # The child concerns the Action as an ordinary explicit relationship, through the same business route.
    data(operate(host, "first", "relationships_create", source={"type": "action", "id": action["action_id"]},
                 target={"type": "task", "id": child["id"]}, role="concerns"))
    # A Task update is staged for review and the child's own discussion cites that Signoff as a typed reference.
    set_agent_overrides(host.instance, host.agent, {"tasks_update": "signoff"}, [host.first, host.second])
    staged = invoke(host, host.agent_context, "tasks_update",
                    {"task_id": child["id"], "changes": {"title": "Write the release notes"}},
                    revision=child["revision"])
    assert staged.status == "signoff_required", staged.error
    signoff_id = staged.signoff["id"]
    data(operate(host, "first", "tasks_comment", task_id=child["id"], text="Notes are ready for review",
                 note_refs=[{"type": "signoff", "id": signoff_id}], expected_revision=child["revision"]))
    # The owner's route returns the whole branch, one row per canonical record, with history.
    found = associated(host, root["id"], "action")["items"]
    assert [item["id"] for item in found] == [action["action_id"]]
    assert found[0]["type"] == "action" and found[0]["business_id"] == host.first
    assert found[0]["label"] == "Drafted the page" and found[0]["task_ids"] == sorted([child["id"], grandchild["id"]])
    signoffs = associated(host, root["id"], "signoff")["items"]
    assert [item["id"] for item in signoffs] == [signoff_id]
    assert signoffs[0]["business_id"] == host.first and signoffs[0]["state"] == "pending"
    assert signoffs[0]["task_ids"] == [child["id"]]
    # The root alone has neither: its Action and Signoff are on descendants.
    assert associated(host, root["id"], "action", include_descendants=False)["items"] == []
    assert associated(host, root["id"], "signoff", include_descendants=False)["items"] == []
    # Pagination continues the same result set.
    first = associated(host, root["id"], "action", limit=1)
    assert [item["id"] for item in first["items"]] == [action["action_id"]]


def test_the_global_route_and_agent_tool_policy_cover_the_same_read(host):
    descriptor = host.runtime.registry.get("tasks_associated_records")
    assert descriptor.effect_kind == "read" and descriptor.supports_signoff is False
    assert host.runtime.registry.metadata("tasks_associated_records").effect_kind == "read"
    # The host policy re-enters the ordinary Task read grant for this name; without that it would fall back to deny.
    decision = resolve_operation_policy("tasks_associated_records", {"tasks_get": "allow"}, {}, host.runtime.registry)
    assert decision.mode == "allow" and decision.provenance == "role_default"
    # The registered agent's tool inventory admits the read by the ordinary Task read grant.
    tools = {row["canonical_operation"]: row for row in
             host.runtime.resolver.resolve_agent_config(Principal(host.agent, "agent")).data["tools"]}
    assert tools["tasks_associated_records"]["available"] is True
    assert tools["tasks_associated_records"]["effective_mode"] == "allow"
    with host.instance.database.read() as uow:
        host.runtime.application.authorize(host.agent_context, descriptor, (), uow,
                                           namespace="business:" + host.first)
    # A Global Task and its Global Action are read through the root transport only.
    global_task = data(operate(host, None, "tasks_create", title="Global story", scope="global"))["task"]
    global_agent = host.runtime.resolver.invocation_context(Principal(host.agent, "agent"))
    action = ok(invoke(host, global_agent, "actions_record",
                       {"text": "Global result", "category": "global-operational", "task_id": global_task["id"]}))["action"]
    assert action["action_id"].startswith("global/")
    found = associated(host, global_task["id"], "action", slug=None, scope="global")["items"]
    assert [item["id"] for item in found] == [action["action_id"]]
    assert found[0]["business_id"] is None and found[0]["task_ids"] == [global_task["id"]]
    # The business transport cannot name that Global Task at all, so it can never borrow its records.
    assert refused(operate(host, "first", "tasks_associated_records", task_id=global_task["id"],
                           record_type="action")) == "not_found"