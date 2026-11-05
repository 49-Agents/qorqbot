"""Descendant-aware Story associated records: every canonical association source, scope, authorization and paging.

The engine is real (Tasks, Actions, Signoffs, discussion, the item resolver and the application); the synthetic host
supplies the same item and reference ports a composed host does. Nothing here reads or writes a live store."""

from __future__ import annotations

import json

import pytest

from qorqut_operations.contracts import InvocationContext, PolicyDecision, Principal, new_id
from qorqut_operations.policy import PolicyResolver, PolicyRules
from operational_support import AGENT, BUSINESS, OTHER, OTHER_AGENT, STRANGER, OperationalHost


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


# -- stored canonical association facts of the synthetic host -----------------------------------------------------------

def concern(host, source, target, *, business=BUSINESS, role="concerns"):
    """One explicit relationship row, as the relationship writer stores it."""
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO relationships(business_id,id,from_type,from_id,to_type,to_id,role,context,revision,created_at,updated_at,"
            "created_by) VALUES (?,?,?,?,?,?,?,'',1,'now','now',?)",
            (business, new_id(), source["type"], source["id"], target["type"], target["id"], role, AGENT))


def note(host, task_id, refs, *, business=BUSINESS, body="A note"):
    """One stored discussion message with exactly the typed references given (no read-time derivation)."""
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,summary,refs_json,author_id,recorder_id,"
            "attribution_json,created_at) VALUES (?,?,'task',?,'comment',?,NULL,?,?,?, '{\"kind\":\"self\"}', 'now')",
            (business, new_id(), task_id, body, json.dumps(refs), AGENT, AGENT))


def associated(host, task_id, record_type, *, ctx=None, **arguments):
    return host.read("tasks_associated_records", {"task_id": task_id, "record_type": record_type, **arguments}, ctx)


def shared_action(host, text, task_id, *, businesses=(BUSINESS,)):
    """One Global-stored business-operational Action, explicitly shared and carrying bare Task evidence."""
    return host.record_action(text, ctx=host.coordinator(None), category="business-operational",
                              business_ids=list(businesses), evidence={"task": task_id})


def storage_scope_evidence(host, task_ids, *, business=BUSINESS):
    """The pre-existing same-storage-scope task-evidence source, for the negation checks only."""
    with host.database.read() as uow:
        return host.tasks.associated.action_evidence(uow, business, set(task_ids))


def items(host, task_id, record_type, *, ctx=None, **arguments):
    return associated(host, task_id, record_type, ctx=ctx, **arguments)["items"]


def ids(host, task_id, record_type, *, ctx=None, **arguments):
    return [item["id"] for item in items(host, task_id, record_type, ctx=ctx, **arguments)]


# -- the branch, every route and canonical identity ---------------------------------------------------------------------

def test_a_story_gathers_its_whole_branch_once_from_every_route(host):
    root = host.create_task("Launch")
    child = host.create_task("Write the copy", parent_id=root["id"])
    grandchild = host.create_task("Draft the page", parent_id=child["id"])
    sibling = host.create_task("A separate story")
    on_root = host.record_action("Scheduled the launch", task_id=root["id"])
    on_child = host.record_action("Drafted the copy", task_id=child["id"])
    on_grandchild = host.record_action("Approved the draft", task_id=grandchild["id"])
    on_sibling = host.record_action("Separate result", task_id=sibling["id"])
    # The child's Action is also found from the grandchild and through a typed reference on the root's own discussion.
    concern(host, {"type": "action", "id": on_child["action_id"]}, {"type": "task", "id": grandchild["id"]})
    note(host, root["id"], [{"type": "action", "id": on_child["action_id"]}])
    # A record found through several routes keeps one canonical row and combines every matching source Task.
    found = items(host, root["id"], "action")
    assert [item["id"] for item in found] == sorted(item["id"] for item in found)
    assert {item["id"] for item in found} == {on_root["action_id"], on_child["action_id"], on_grandchild["action_id"]}
    by_id = {item["id"]: item for item in found}
    assert by_id[on_child["action_id"]]["task_ids"] == sorted([root["id"], child["id"], grandchild["id"]])
    assert by_id[on_child["action_id"]]["business_id"] == BUSINESS
    assert by_id[on_child["action_id"]]["type"] == "action"
    assert by_id[on_child["action_id"]]["label"] == "Drafted the copy"
    assert by_id[on_grandchild["action_id"]]["task_ids"] == [grandchild["id"]]
    assert on_sibling["action_id"] not in by_id
    # The root's own records are part of the Story; descendants can be left out explicitly.
    assert {item["id"] for item in items(host, root["id"], "action", include_descendants=False)} == {
        on_root["action_id"], on_child["action_id"]}
    # A linked Task is not a descendant of the root.
    link_target = host.create_task("Merely linked")
    concern(host, {"type": "task", "id": root["id"]}, {"type": "task", "id": link_target["id"]})
    linked_result = host.record_action("Linked work", task_id=link_target["id"])
    assert linked_result["action_id"] not in ids(host, root["id"], "action")


def test_closed_and_archived_descendants_stay_in_the_story(host):
    root = host.create_task("Programme")
    child = host.create_task("Finished work", parent_id=root["id"])
    done = host.data("tasks_complete", {"task_id": child["id"]}, expected_revision=child["revision"])["task"]
    child_result = host.record_action("Finished the work", task_id=child["id"])
    archived = host.create_task("Filed work", parent_id=root["id"], archived=True)
    archived_result = host.record_action("Filed the work", task_id=archived["id"])
    found = ids(host, root["id"], "action")
    assert done["status"] == "done" and archived["archived"] is True
    assert set(found) == {child_result["action_id"], archived_result["action_id"]}


def test_withdrawn_actions_stay_in_the_story_history(host):
    root = host.create_task("History")
    action = host.record_action("Superseded result", task_id=root["id"])
    host.data("actions_withdraw", {"action_id": action["action_id"], "rationale": "A corrected result replaces it"},
              expected_revision=action["revision"])
    found = items(host, root["id"], "action")
    assert [item["id"] for item in found] == [action["action_id"]]
    assert host.query("SELECT withdrawn FROM action_logs WHERE action_key=?", action["action_id"])[0]["withdrawn"] == 1


# -- canonical scoped identity and scope isolation ----------------------------------------------------------------------

def test_action_ids_keep_their_canonical_scope_and_never_cross_it(host):
    same = "00000000-0000-4000-8000-0000000000ff"
    business_task = host.create_task("Business story", id=same)
    global_task = host.data("tasks_create", {"title": "Global story", "scope": "global", "id": same}, host.coordinator(None))["task"]
    business_action = host.record_action("Business result", task_id=business_task["id"])
    legacy_id = business_action["legacy"]["id"]
    # A legacy bare ID and the canonical business action_id name the same stored Action.
    concern(host, {"type": "action", "id": legacy_id}, {"type": "task", "id": business_task["id"]})
    note(host, business_task["id"], [{"type": "action", "id": business_action["action_id"]}])
    found = items(host, business_task["id"], "action")
    assert [item["id"] for item in found] == [business_action["action_id"]]
    assert found[0]["id"].startswith("business/" + BUSINESS + "/") and found[0]["task_ids"] == [business_task["id"]]
    # A Global Action stored in Global and sharing a bare Task ID with this business's Task never borrows it.
    global_action = host.record_action("Global result", ctx=host.coordinator(None), category="business-operational",
                                       business_ids=[BUSINESS], task_id=global_task["id"])
    assert [item["id"] for item in items(host, global_task["id"], "action", ctx=host.coordinator(None), scope="global")] == [
        global_action["action_id"]]
    assert items(host, business_task["id"], "action") == found
    # A canonical Action ID of another business is authorized as not visible here, never resolved by its bare suffix.
    other_task = host.create_task("Other story", ctx=host.owner(OTHER))
    other_action = host.record_action("Other result", ctx=host.agent(OTHER_AGENT, OTHER), task_id=other_task["id"])
    concern(host, {"type": "action", "id": other_action["action_id"]}, {"type": "task", "id": business_task["id"]})
    assert other_action["action_id"] not in ids(host, business_task["id"], "action")


def test_action_task_evidence_is_matched_in_the_actions_own_storage_scope(host):
    same = "00000000-0000-4000-8000-0000000000fe"
    business_task = host.create_task("Business story", id=same)
    global_task = host.data("tasks_create", {"title": "Global story", "scope": "global", "id": same}, host.coordinator(None))["task"]
    business_action = host.record_action("Business result", task_id=business_task["id"])
    shared_global = host.record_action("Shared Global result", ctx=host.coordinator(None), category="business-operational",
                                       business_ids=[BUSINESS], task_id=global_task["id"])
    assert [item["id"] for item in items(host, business_task["id"], "action")] == [business_action["action_id"]]
    assert shared_global["action_id"].startswith("global/")
    assert [item["id"] for item in items(host, global_task["id"], "action", ctx=host.coordinator(None), scope="global")] == [
        shared_global["action_id"]]


# -- shared Global Action evidence: the installation owner's business Story ----------------------------------------------

def test_the_installation_owner_reads_a_shared_global_action_on_a_descendant(host):
    root = host.create_task("Launch")
    child = host.create_task("Write the copy", parent_id=root["id"])
    action = shared_action(host, "Drafted the copy", child["id"])
    assert action["action_id"].startswith("global/")
    assert host.query("SELECT business_id FROM action_logs WHERE action_key=?", action["action_id"])[0]["business_id"] is None
    found = items(host, root["id"], "action")
    assert [item["id"] for item in found] == [action["action_id"]]
    # The Action keeps its canonical Global identity and true storage namespace; the Task it names is the source.
    assert found[0]["business_id"] is None and found[0]["task_ids"] == [child["id"]]
    assert found[0]["label"] == "Drafted the copy"
    # The pre-existing same-storage-scope source misses this row entirely: only the new owner path adds it.
    assert storage_scope_evidence(host, {root["id"], child["id"]}) == set()
    # A descendant-only match is not part of a root-only read.
    assert items(host, root["id"], "action", include_descendants=False) == []


def test_a_shared_global_action_is_deduplicated_with_explicit_concerns(host):
    root = host.create_task("Combined")
    child = host.create_task("Child", parent_id=root["id"])
    action = shared_action(host, "One shared result", child["id"])
    concern(host, {"type": "action", "id": action["action_id"]}, {"type": "task", "id": root["id"]})
    found = items(host, root["id"], "action")
    assert [item["id"] for item in found] == [action["action_id"]]
    assert found[0]["business_id"] is None
    assert found[0]["task_ids"] == sorted([root["id"], child["id"]])


def test_shared_metadata_alone_does_not_link_an_action(host):
    root = host.create_task("Membership")
    child = host.create_task("Descendant", parent_id=root["id"])
    elsewhere = host.create_task("Elsewhere", ctx=host.owner(OTHER))
    # Shared with this business, but its evidence names no Story Task.
    unrelated = shared_action(host, "Unrelated shared result", elsewhere["id"])
    # Its evidence names a Story Task, but it is shared with another business only.
    unshared = host.record_action("Unshared Global result", ctx=host.coordinator(None), category="business-operational",
                                  business_ids=[OTHER], evidence={"task": child["id"]})
    # Another business's storage never borrows this business's Task namespace.
    other_stored = host.record_action("Other project result", ctx=host.agent(OTHER_AGENT, OTHER),
                                      evidence={"task": child["id"]})
    assert other_stored["action_id"].startswith("business/" + OTHER + "/")
    assert items(host, root["id"], "action") == []
    assert not {unrelated["action_id"], unshared["action_id"], other_stored["action_id"]} & set(ids(host, root["id"], "action"))


def test_a_bare_task_id_links_a_shared_global_action_only_in_its_unique_business_namespace(host):
    # A Global Task with the same bare ID makes the shared evidence ambiguous, so it is not the business Task.
    same_global = "00000000-0000-4000-8000-0000000000e1"
    collided_story = host.create_task("Global-named business story", id=same_global)
    host.data("tasks_create", {"title": "Global collision", "scope": "global", "id": same_global}, host.coordinator(None))
    collided = shared_action(host, "Colliding result", same_global)
    assert items(host, collided_story["id"], "action") == []
    # The same Task ID in a second business shared with the Action makes it ambiguous there too.
    same_shared = "00000000-0000-4000-8000-0000000000e2"
    shared_story = host.create_task("Two-project story", id=same_shared)
    host.create_task("Second project story", id=same_shared, ctx=host.owner(OTHER))
    ambiguous = shared_action(host, "Ambiguous result", same_shared, businesses=(BUSINESS, OTHER))
    assert items(host, shared_story["id"], "action") == []
    # The same ID in a business the Action is not shared with is no candidate namespace and never blocks the link.
    same_unlinked = "00000000-0000-4000-8000-0000000000e3"
    unlinked_story = host.create_task("Unlinked-namespace story", id=same_unlinked)
    host.create_task("Unrelated project story", id=same_unlinked, ctx=host.owner(OTHER))
    linked = shared_action(host, "Linked result", same_unlinked)
    assert [item["id"] for item in items(host, unlinked_story["id"], "action")] == [linked["action_id"]]
    assert not {collided["action_id"], ambiguous["action_id"]} & set(ids(host, unlinked_story["id"], "action"))


def test_the_shared_global_action_inference_is_owner_only(host):
    root = host.create_task("Owner-only")
    child = host.create_task("Descendant", parent_id=root["id"])
    action = shared_action(host, "Owner-visible result", child["id"])
    assert [item["id"] for item in items(host, root["id"], "action")] == [action["action_id"]]
    # A plain business member, an agent working in the business and a global agent selecting the business gain no
    # association from a Global Action's evidence: the Action domain withholds it outside the installation owner.
    for reader in (host.stranger(), host.agent(), host.coordinator(BUSINESS)):
        assert items(host, root["id"], "action", ctx=reader) == []


class DeniedActionReads(PolicyResolver):
    """The ordinary policy, except that reading one Action record is denied."""

    def resolve(self, context, descriptor, targets, uow):
        if descriptor.name == "actions_get":
            return PolicyDecision("deny", "synthetic_denial")
        return super().resolve(context, descriptor, targets, uow)


def test_a_denied_action_read_excludes_the_shared_global_record(host):
    root = host.create_task("Denied")
    child = host.create_task("Descendant", parent_id=root["id"])
    action = shared_action(host, "Owner-visible result", child["id"])
    assert [item["id"] for item in items(host, root["id"], "action")] == [action["action_id"]]
    host.app.policy = DeniedActionReads(host.registry, host.agents, rules=PolicyRules())
    assert items(host, root["id"], "action") == []


# -- Signoffs: typed Task expected targets, matching scope and history --------------------------------------------------

def test_signoffs_are_matched_by_their_expected_task_target_and_stay_in_history(host):
    root = host.create_task("Release")
    child = host.create_task("Write the notes", parent_id=root["id"])
    # A proposal for the child carries the Task as an exact expected target.
    host.agents.overrides["tasks_update"] = "signoff"
    staged = host.stage("tasks_update", {"task_id": child["id"], "changes": {"title": "Write the release notes"}},
                        expected_revision=child["revision"])
    found = items(host, root["id"], "signoff")
    assert [item["id"] for item in found] == [staged]
    assert found[0]["business_id"] == BUSINESS and found[0]["state"] == "pending"
    assert found[0]["task_ids"] == [child["id"]] and found[0]["label"] == "Tasks Update"
    # A decided proposal remains part of the Story with its current state.
    applied = host.apply_approved(staged)
    resolved = items(host, root["id"], "signoff")
    assert applied["state"] == "applied" and resolved[0]["state"] == "applied" and resolved[0]["revision"] >= 2
    host.agents.overrides.clear()
    # A proposal in another review scope, or for another project's Task, never enters this Story.
    other = host.create_task("Other release", ctx=host.owner(OTHER))
    host.agents.overrides["tasks_update"] = "signoff"
    other_signoff = host.stage("tasks_update", {"task_id": other["id"], "changes": {"title": "Other release"}},
                               ctx=host.agent(OTHER_AGENT, OTHER), expected_revision=other["revision"])
    global_task = host.data("tasks_create", {"title": "Global release", "scope": "global"}, host.coordinator(None))["task"]
    global_signoff = host.stage("tasks_update", {"task_id": global_task["id"], "changes": {"title": "Global release"}},
                                ctx=host.coordinator(None), expected_revision=global_task["revision"])
    host.agents.overrides.clear()
    assert {item["id"] for item in items(host, root["id"], "signoff")} == {staged}
    assert [item["id"] for item in items(host, global_task["id"], "signoff", ctx=host.coordinator(None), scope="global")] == [
        global_signoff]
    assert other_signoff not in {item["id"] for item in items(host, root["id"], "signoff")}


# -- typed discussion references, never arbitrary data ------------------------------------------------------------------

def test_only_typed_references_on_a_story_tasks_own_thread_link_a_record(host):
    root = host.create_task("Review", id="ab12c")
    action = host.record_action("A suggestion", related_objects=[{"kind": "node", "id": "ab12c"}])
    # The Action's Context pointer and a body that spells the Action out are not links.
    note(host, root["id"], [{"type": "topic", "id": "t1"}],
         body="See node ab12c and " + action["action_id"])
    assert items(host, root["id"], "action") == []
    # A typed reference on an Action's own thread is not a Story Task's thread reference.
    other = host.record_action("Another result")
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,summary,refs_json,author_id,recorder_id,"
            "attribution_json,created_at) VALUES (?,?,'action',?,'comment','Note',NULL,?,?,?, '{\"kind\":\"self\"}', 'now')",
            (BUSINESS, "action-note", other["legacy"]["id"], json.dumps([{"type": "action", "id": action["action_id"]}]),
             AGENT, AGENT))
    assert items(host, root["id"], "action") == []
    # A typed reference on the Task's own thread links the record with that source Task.
    note(host, root["id"], [{"type": "action", "id": action["action_id"]}])
    found = items(host, root["id"], "action")
    assert [item["id"] for item in found] == [action["action_id"]] and found[0]["task_ids"] == [root["id"]]


# -- permission and visibility ------------------------------------------------------------------------------------------

def test_denied_record_visibility_hides_only_the_denied_records(host):
    root = host.create_task("Restricted story")
    action = host.record_action("Visible result", task_id=root["id"])
    acting = host.agent()
    host.agents.overrides["actions_get"] = "deny"
    assert items(host, root["id"], "action", ctx=acting) == []
    host.agents.overrides.clear()
    assert [item["id"] for item in items(host, root["id"], "action", ctx=acting)] == [action["action_id"]]
    # A Global Action that this business cannot read is skipped even when a stored reference names it.
    hidden = host.record_action("Private Global result", ctx=host.coordinator(None), category="global-operational")
    note(host, root["id"], [{"type": "action", "id": hidden["action_id"]}])
    assert [item["id"] for item in items(host, root["id"], "action", ctx=acting)] == [action["action_id"]]


class PermissiveAssociatedRead(PolicyResolver):
    """A synthetic policy that permits the new read for every caller, so only the Task domain guard can refuse it."""

    def resolve(self, context, descriptor, targets, uow):
        if descriptor.name == "tasks_associated_records":
            return PolicyDecision("allow", "synthetic_permissive")
        return super().resolve(context, descriptor, targets, uow)


def test_the_registered_descriptor_applies_the_task_global_guard(host):
    global_task = host.data("tasks_create", {"title": "Global story", "scope": "global"}, host.coordinator(None))["task"]
    # The ordinary policy is made permissive for this read only: the registered descriptor still enters the Task read,
    # whose explicit Global rule is the same guard tasks_get uses.
    host.app.policy = PermissiveAssociatedRead(host.registry, host.agents, rules=PolicyRules())
    arguments = {"task_id": global_task["id"], "record_type": "action", "scope": "global"}
    outsider = InvocationContext(Principal(STRANGER, "human"), allowed_business_ids=frozenset({BUSINESS}),
                                 global_capabilities=frozenset({"read_global_context"}), source="dashboard")
    assert host.error("tasks_associated_records", arguments, outsider) == "forbidden"
    # The ordinary read refuses the same caller too (earlier, at policy, before the Task domain guard can see it).
    assert host.error("tasks_get", {"task_id": global_task["id"], "scope": "global"}, outsider) in {"forbidden", "policy_denied"}
    capability_less = host.agent(business=None)
    assert host.error("tasks_associated_records", arguments, capability_less) == "forbidden"
    assert host.error("tasks_get", {"task_id": global_task["id"], "scope": "global"}, capability_less) == "forbidden"
    for ctx in (host.owner(business=None), host.coordinator(None)):
        assert host.read("tasks_associated_records", arguments, ctx) == {"items": [], "next_cursor": None}
        assert host.read("tasks_get", {"task_id": global_task["id"], "scope": "global"}, ctx)["task"]["id"] == global_task["id"]


def test_the_read_is_an_ordinary_task_catalog_read(host):
    entry = host.registry.metadata("tasks_associated_records")
    descriptor = host.registry.get("tasks_associated_records")
    assert entry.effect_kind == "read" and entry.supports_signoff is False
    assert descriptor.scope_kinds == frozenset({"business", "global"}) and descriptor.global_capability == "read_global_context"
    assert set(descriptor.input_schema["required"]) == {"task_id", "record_type"}
    assert set(descriptor.input_schema["properties"]["record_type"]["enum"]) == {"action", "signoff"}
    assert host.error("tasks_associated_records", {"task_id": "missing", "record_type": "action"}) == "not_found"
    assert host.error("tasks_associated_records", {"record_type": "action"}) == "validation_error"


def test_an_assembly_without_the_record_domain_refuses_rather_than_claiming_an_empty_history(tmp_path):
    host = OperationalHost(tmp_path, records=False)
    root = host.create_task("Story")
    assert host.error("tasks_associated_records", {"task_id": root["id"], "record_type": "action"}) == "operation_unavailable"


# -- paging, cycles and depth -------------------------------------------------------------------------------------------

def test_paging_is_stable_and_the_cursor_binds_the_story_type_and_option(host):
    root = host.create_task("Paged")
    child = host.create_task("Child", parent_id=root["id"])
    expected = [host.record_action(f"Result {index}", task_id=(root if index % 2 else child)["id"])["action_id"]
                for index in range(5)]
    first = associated(host, root["id"], "action", limit=2)
    second = associated(host, root["id"], "action", limit=2, cursor=first["next_cursor"])
    third = associated(host, root["id"], "action", limit=2, cursor=second["next_cursor"])
    assert first["next_cursor"] and second["next_cursor"] and third["next_cursor"] is None
    collected = [item["id"] for page in (first, second, third) for item in page["items"]]
    assert collected == sorted(expected) and len(set(collected)) == len(expected)
    # A cursor continues only the exact root, record type, descendant option and namespace it was issued for.
    assert host.error("tasks_associated_records", {"task_id": root["id"], "record_type": "signoff",
                                                   "cursor": first["next_cursor"]}) == "validation_error"
    assert host.error("tasks_associated_records", {"task_id": child["id"], "record_type": "action",
                                                   "cursor": first["next_cursor"]}) == "validation_error"
    assert host.error("tasks_associated_records", {"task_id": root["id"], "record_type": "action",
                                                   "include_descendants": False,
                                                   "cursor": first["next_cursor"]}) == "validation_error"
    assert host.error("tasks_associated_records", {"task_id": root["id"], "record_type": "action",
                                                   "cursor": "not-a-cursor"}) == "validation_error"


def test_deep_branches_are_not_capped_and_a_corrupt_cycle_terminates(host):
    root = host.create_task("Deep")
    parent = root
    for index in range(60):
        parent = host.create_task(f"Level {index}", parent_id=parent["id"])
    deepest = host.record_action("Deep result", task_id=parent["id"])
    assert deepest["action_id"] in ids(host, root["id"], "action")
    # A corrupt stored cycle (which the live triggers prevent) is traversed once and returns.
    cycle_root = host.create_task("Cycle root")
    cycle_child = host.create_task("Cycle child", parent_id=cycle_root["id"])
    cycle_result = host.record_action("Cycle result", task_id=cycle_child["id"])
    with host.database.transaction() as uow:
        uow.connection.execute("DROP TRIGGER tasks_parent_update")
        uow.connection.execute("UPDATE tasks SET parent_id=? WHERE business_id=? AND id=?",
                               (cycle_child["id"], BUSINESS, cycle_root["id"]))
    assert [item["id"] for item in items(host, cycle_root["id"], "action")] == [cycle_result["action_id"]]


def test_signoff_expected_targets_require_the_matching_task_scope(host):
    same = "00000000-0000-4000-8000-0000000000fd"
    business_task = host.create_task("Business release", id=same)
    global_task = host.data("tasks_create", {"title": "Global release", "scope": "global", "id": same}, host.coordinator(None))["task"]
    host.agents.overrides["tasks_update"] = "signoff"
    business_signoff = host.stage("tasks_update", {"task_id": business_task["id"], "changes": {"title": "Business release"}},
                                  expected_revision=business_task["revision"])
    global_signoff = host.stage("tasks_update", {"task_id": global_task["id"], "changes": {"title": "Global release"}},
                                ctx=host.coordinator(None), expected_revision=global_task["revision"])
    host.agents.overrides.clear()
    assert [item["id"] for item in items(host, business_task["id"], "signoff")] == [business_signoff]
    assert [item["id"] for item in items(host, global_task["id"], "signoff", ctx=host.coordinator(None),
                                         scope="global")] == [global_signoff]


@pytest.mark.parametrize("record_type", ["action", "signoff"])
def test_duplicate_routes_combine_task_ids_in_canonical_order(host, record_type):
    root = host.create_task("Combined")
    child = host.create_task("Child", parent_id=root["id"])
    if record_type == "action":
        record = host.record_action("Combined result", task_id=root["id"])
        identity = record["action_id"]
        concern(host, {"type": "action", "id": identity}, {"type": "task", "id": child["id"]})
        note(host, child["id"], [{"type": "action", "id": identity}])
    else:
        host.agents.overrides["tasks_update"] = "signoff"
        identity = host.stage("tasks_update", {"task_id": child["id"], "changes": {"title": "Combined release"}},
                              expected_revision=child["revision"])
        host.agents.overrides.clear()
        concern(host, {"type": "signoff", "id": identity}, {"type": "task", "id": root["id"]})
    found = items(host, root["id"], record_type)
    assert [item["id"] for item in found] == [identity]
    assert found[0]["task_ids"] == sorted([root["id"], child["id"]])


def test_concerns_in_either_direction_are_the_only_relationship_roles_that_link(host):
    root = host.create_task("Relations")
    action = host.record_action("Related result")
    concern(host, {"type": "action", "id": action["action_id"]}, {"type": "task", "id": root["id"]}, role="part_of")
    concern(host, {"type": "action", "id": action["action_id"]}, {"type": "task", "id": root["id"]}, role="mentions")
    assert items(host, root["id"], "action") == []
    concern(host, {"type": "action", "id": action["action_id"]}, {"type": "task", "id": root["id"]})
    assert [item["id"] for item in items(host, root["id"], "action")] == [action["action_id"]]