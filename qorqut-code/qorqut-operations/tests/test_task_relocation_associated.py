"""Story associated records across Task relocations: a moved Story keeps what it had, in the scope each association was
established in, and never borrows a same-ID record of the scope it moved to.

Synthetic data only. The engine is real (Tasks, Actions, Signoffs, discussion, relocation, the item resolver and the policy);
the host supplies the same ports a composed host does. Time is stepped explicitly: an association belongs to the stay of the
Task whose half-open interval holds the instant it was established, so every step here moves the clock."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from qorqut_operations.contracts import InvocationContext, PolicyDecision, Principal, new_id, plain
from qorqut_operations.policy import PolicyResolver, PolicyRules
from qorqut_operations.tasks import relocation
from operational_support import AGENT, BUSINESS, HUMAN, OTHER, OTHER_AGENT, STRANGER, OperationalHost
from test_task_associated_records import concern, note, shared_action
from test_task_relocation import THIRD, move, snapshot


@pytest.fixture
def host(tmp_path):
    host = OperationalHost(tmp_path)
    with host.database.transaction() as uow:
        uow.repository("businesses").insert({"id": THIRD, "slug": "third", "name": "third", "workspace": str(tmp_path / "third"),
                                             "hq_path": str(tmp_path / "third" / "hq")})
        uow.repository("business_memberships", business_id=THIRD).insert({"principal_id": HUMAN, "is_owner": True})
    return host


def shared_with(host, text, task_id, *businesses):
    """A Global-stored, explicitly shared business-operational Action with bare Task evidence, by a coordinator that reaches
    the third project this fixture adds too."""
    coordinator = host.coordinator(None)
    ctx = replace(coordinator, allowed_business_ids=coordinator.allowed_business_ids | {THIRD})
    return host.record_action(text, ctx=ctx, category="business-operational", business_ids=list(businesses), evidence={"task": task_id})


def owner_of(host, business):
    """The installation owner reading ``business``, which may be the third project this fixture adds."""
    ctx = host.owner(business)
    return replace(ctx, allowed_business_ids=ctx.allowed_business_ids | {THIRD})


def associated(host, task_id, record_type, ctx=None, **arguments):
    ctx = ctx or host.owner()
    scope = {"scope": "global"} if ctx.business_id is None else {}
    return host.invoke("tasks_associated_records", {"task_id": task_id, "record_type": record_type, **scope, **arguments}, ctx)


def items(host, task_id, record_type, ctx=None, **arguments):
    result = associated(host, task_id, record_type, ctx, **arguments)
    assert result.status == "ok", result.error
    return plain(result.data)["items"]


def ids(host, task_id, record_type, ctx=None, **arguments):
    return [item["id"] for item in items(host, task_id, record_type, ctx, **arguments)]


def story(host, ctx=None):
    root = host.create_task("Funding story", ctx)
    child = host.create_task("Funding child", ctx, parent_id=root["id"])
    grandchild = host.create_task("Funding grandchild", ctx, parent_id=child["id"])
    return root, child, grandchild


def applied_signoff(host, task, *, ctx=None, business=BUSINESS, title="Reviewed title"):
    """One decided Signoff whose frozen expected target is exactly this Task."""
    host.agents.overrides["tasks_update"] = "signoff"
    identity = host.stage("tasks_update", {"task_id": task["id"], "changes": {"title": title}}, ctx,
                          expected_revision=host.task(task["id"], business)["revision"])
    assert host.apply_approved(identity, host.owner(business))["state"] == "applied"
    host.agents.overrides.clear()
    return identity


def stored(host):
    """The immutable records, whole: Actions, their revisions and Signoffs."""
    return {name: host.query(f"SELECT * FROM {name} ORDER BY rowid") for name in ("action_logs", "action_revisions", "signoffs")}


def messages(host):
    """Every discussion payload with its identity, but not the scope columns a relocation is allowed to change."""
    return host.query("SELECT id, item_type, item_id, kind, body, summary, refs_json, author_id, created_at FROM thread_messages ORDER BY id")


# -- the four reproductions and the preserved destination records --------------------------------------------------------

def test_a_moved_story_keeps_the_actions_of_every_scope_it_was_in(host):
    root, child, grandchild = story(host)
    before = host.record_action("Recorded in the source", ctx=host.agent(AGENT), task_id=grandchild["id"])
    on_root = host.record_action("Recorded on the root", ctx=host.agent(AGENT), task_id=root["id"])
    assert ids(host, root["id"], "action") == sorted([before["action_id"], on_root["action_id"]])
    host.later(seconds=1)
    stored_before, payloads = stored(host), messages(host)
    move(host, BUSINESS, OTHER, [root["id"]])
    host.later(seconds=1)
    destination = host.owner(OTHER)
    after = host.record_action("Recorded in the destination", ctx=host.agent(OTHER_AGENT, OTHER), task_id=grandchild["id"])
    found = {item["id"]: item for item in items(host, root["id"], "action", destination)}
    assert set(found) == {before["action_id"], on_root["action_id"], after["action_id"]}
    # Each record keeps its true owning scope and names the Story Task whose evidence it carries.
    assert found[before["action_id"]]["business_id"] == BUSINESS and found[before["action_id"]]["task_ids"] == [grandchild["id"]]
    assert found[on_root["action_id"]]["task_ids"] == [root["id"]]
    assert found[after["action_id"]]["business_id"] == OTHER and found[after["action_id"]]["task_ids"] == [grandchild["id"]]
    assert found[before["action_id"]]["label"] == "Recorded in the source"
    # Without descendants only the root's own association is read, old and new alike.
    assert ids(host, root["id"], "action", destination, include_descendants=False) == [on_root["action_id"]]
    # A read writes nothing, and no stored record, evidence or message was rewritten by the move or the reads.
    now = stored(host)
    assert now["action_logs"][:2] == stored_before["action_logs"] and now["action_revisions"][:2] == stored_before["action_revisions"]
    assert now["signoffs"] == stored_before["signoffs"] and messages(host) == payloads
    reading = snapshot(host)
    items(host, root["id"], "action", destination)
    assert snapshot(host) == reading


def test_a_decided_signoff_target_stays_with_the_moved_story(host):
    root, child, _ = story(host)
    decided = applied_signoff(host, child)
    assert ids(host, root["id"], "signoff") == [decided]
    host.later(seconds=1)
    kept = stored(host)["signoffs"]
    move(host, BUSINESS, OTHER, [root["id"]])
    host.later(seconds=1)
    destination = host.owner(OTHER)
    found = items(host, root["id"], "signoff", destination)
    assert [item["id"] for item in found] == [decided] and found[0]["business_id"] == BUSINESS
    assert found[0]["task_ids"] == [child["id"]] and found[0]["state"] == "applied"
    # A Signoff proposed in the destination is the destination's own and joins the old one.
    later = applied_signoff(host, child, ctx=host.agent(AGENT, OTHER), business=OTHER)
    found = items(host, root["id"], "signoff", destination)
    assert {item["id"] for item in found} == {decided, later} and {item["id"]: item["business_id"] for item in found}[later] == OTHER
    assert stored(host)["signoffs"][:1] == kept
    # The root-only read sees neither, because both name the child.
    assert ids(host, root["id"], "signoff", destination, include_descendants=False) == []


def test_a_discussion_reference_is_read_in_the_scope_its_message_was_written_in(host):
    root = host.create_task("Story citing records")
    separate = host.create_task("Separate decision target")
    decided = applied_signoff(host, separate)
    original = host.record_action("Original source action")
    bare = original["legacy"]["id"]
    # A same-ID Action of the destination, explicitly linked to it: the decoy a bare ID must never reach.
    row = host.query("SELECT * FROM action_logs WHERE action_key=?", original["action_id"])[0]
    row.update(business_id=OTHER, action_key="business/" + OTHER + "/" + bare, action_text="Destination decoy")
    with host.database.transaction() as uow:
        uow.repository("action_logs", business_id=OTHER).insert(row)
        uow.connection.execute("INSERT INTO action_businesses(action_key,business_id,created_at) VALUES (?,?,?)",
                               (row["action_key"], OTHER, host.stamp()))
    note(host, root["id"], [{"type": "action", "id": bare}, {"type": "signoff", "id": decided}])
    assert ids(host, root["id"], "action") == [original["action_id"]]
    host.later(seconds=1)
    payloads = messages(host)
    move(host, BUSINESS, OTHER, [root["id"]])
    pinned = host.query("SELECT reference_namespace FROM thread_messages WHERE item_id=?", root["id"])
    assert pinned and all(entry["reference_namespace"] == "business:" + BUSINESS for entry in pinned)
    destination = host.owner(OTHER)
    found = items(host, root["id"], "action", destination)
    assert [item["id"] for item in found] == [original["action_id"]] and found[0]["label"] == "Original source action"
    assert found[0]["business_id"] == BUSINESS and found[0]["task_ids"] == [root["id"]]
    assert ids(host, root["id"], "signoff", destination) == [decided]
    # A message written afterwards in the destination names the destination's own record by the same bare ID.
    host.later(seconds=1)
    note(host, root["id"], [{"type": "action", "id": bare}], business=OTHER)
    assert ids(host, root["id"], "action", destination) == sorted([original["action_id"], row["action_key"]])
    # A canonical Action ID keeps its own identity wherever the message was written.
    note(host, root["id"], [{"type": "action", "id": original["action_id"]}], business=OTHER)
    assert ids(host, root["id"], "action", destination) == sorted([original["action_id"], row["action_key"]])
    assert [entry for entry in messages(host) if entry["id"] in {value["id"] for value in payloads}] == payloads


def test_an_unknown_or_unavailable_origin_never_falls_back_to_the_destination(host):
    root = host.create_task("Story with bad origins")
    action = host.record_action("Source action")
    bare = action["legacy"]["id"]
    row = host.query("SELECT * FROM action_logs WHERE action_key=?", action["action_id"])[0]
    row.update(business_id=OTHER, action_key="business/" + OTHER + "/" + bare, action_text="Destination decoy")
    with host.database.transaction() as uow:
        uow.repository("action_logs", business_id=OTHER).insert(row)
        uow.connection.execute("INSERT INTO action_businesses(action_key,business_id,created_at) VALUES (?,?,?)",
                               (row["action_key"], OTHER, host.stamp()))
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [root["id"]])
    destination = host.owner(OTHER)
    # Storage admits only ``global`` and ``business:<id>``; a project that does not exist is an origin that is unavailable.
    for namespace in ("business:00000000-0000-4000-8000-0000000000ff",):
        with host.database.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,summary,refs_json,author_id,recorder_id,"
                "attribution_json,created_at,reference_namespace) VALUES (?,?,'task',?,'comment','A note',NULL,?,?,?, '{\"kind\":\"self\"}', 'now',?)",
                (OTHER, new_id(), root["id"], json.dumps([{"type": "action", "id": bare}]), AGENT, AGENT, namespace))
        assert ids(host, root["id"], "action", destination) == [], namespace
    # Global is a real origin: the same bare ID is looked for in Global storage, where there is none.
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,summary,refs_json,author_id,recorder_id,"
            "attribution_json,created_at,reference_namespace) VALUES (?,?,'task',?,'comment','A note',NULL,?,?,?, '{\"kind\":\"self\"}', 'now','global')",
            (OTHER, new_id(), root["id"], json.dumps([{"type": "action", "id": bare}]), AGENT, AGENT))
    assert ids(host, root["id"], "action", destination) == []


# -- the scopes of a Task that moved more than once -----------------------------------------------------------------------

def test_every_stay_of_a_task_that_left_global_and_moved_again_contributes(host):
    owner = host.owner(None)
    container = host.create_task("Global container", owner, task_type="story")
    kid = host.create_task("Kid", owner, parent_id=container["id"])
    grandkid = host.create_task("Grandkid", owner, parent_id=kid["id"])
    recorder = host.coordinator(None)
    first = [host.record_action(f"Global {name}", ctx=recorder, category="global-operational", task_id=task["id"])["action_id"]
             for name, task in (("grandkid", grandkid), ("kid", kid))]
    assert all(identity.startswith("global/") for identity in first)
    host.later(seconds=1)
    move(host, None, BUSINESS, [kid["id"]], ctx=owner)
    host.later(seconds=1)
    second = [host.record_action(f"Project {name}", ctx=host.agent(AGENT), task_id=task["id"])["action_id"]
              for name, task in (("grandkid", grandkid), ("kid", kid))]
    found = items(host, kid["id"], "action", host.owner(BUSINESS))
    assert {item["id"] for item in found} == {*first, *second}
    assert {item["id"]: item["business_id"] for item in found} == {**{identity: None for identity in first},
                                                                  **{identity: BUSINESS for identity in second}}
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [kid["id"]])
    host.later(seconds=1)
    third = [host.record_action(f"Elsewhere {name}", ctx=host.agent(OTHER_AGENT, OTHER), task_id=task["id"])["action_id"]
             for name, task in (("grandkid", grandkid), ("kid", kid))]
    destination = host.owner(OTHER)
    found = items(host, kid["id"], "action", destination)
    assert {item["id"] for item in found} == {*first, *second, *third}
    assert {item["id"]: item["task_ids"] for item in found}[first[0]] == [grandkid["id"]]
    # Without descendants only the Actions naming the root of the moved tree remain, in all three scopes.
    assert set(ids(host, kid["id"], "action", destination, include_descendants=False)) == {first[1], second[1], third[1]}
    # The container that stayed in Global reads none of it.
    assert ids(host, container["id"], "action", host.owner(None)) == []


# -- lifetimes: a bare ID reused by another Task, and evidence established after a Task left --------------------------------

def test_another_task_with_the_same_id_is_a_different_lifetime(host):
    shared = "00000000-0000-4000-8000-0000000000d1"
    arriving = host.create_task("Arriving Task", id=shared)
    host.later(seconds=1)       # a Task is identified across moves by its id and its creation time: the lifetimes must differ
    host.create_task("Resident Task", id=shared, ctx=host.owner(OTHER))
    source = host.record_action("Source work", ctx=host.agent(AGENT), task_id=shared)
    source_sign = applied_signoff(host, arriving)
    resident_work = host.record_action("Resident work", ctx=host.agent(OTHER_AGENT, OTHER), task_id=shared)
    host.later(seconds=1)
    # The resident leaves, which frees the ID; the arriving Task takes its place under the same bare ID.
    move(host, OTHER, THIRD, [shared], ctx=host.owner(OTHER))
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [shared])
    host.later(seconds=1)
    after_arrival = host.record_action("Arrived work", ctx=host.agent(OTHER_AGENT, OTHER), task_id=shared)
    # The arriving Task: its own source records and what happened after it arrived - not the resident's earlier work.
    assert set(ids(host, shared, "action", host.owner(OTHER))) == {source["action_id"], after_arrival["action_id"]}
    assert ids(host, shared, "signoff", host.owner(OTHER)) == [source_sign]
    # The resident Task, now elsewhere, keeps exactly its own stay in the scope it left and nothing of the newcomer's.
    assert ids(host, shared, "action", owner_of(host, THIRD)) == [resident_work["action_id"]]


def test_evidence_established_after_a_task_left_does_not_name_it(host):
    shared = "00000000-0000-4000-8000-0000000000d2"
    host.create_task("Leaving Task", id=shared)
    kept = host.record_action("While it was here", task_id=shared)
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [shared])
    host.later(seconds=1)
    # A new Task takes the freed ID in the source; evidence recorded there now names that Task, even when its editable
    # occurrence time claims an earlier day.
    host.create_task("Newcomer", id=shared)
    late = host.record_action("About the newcomer", task_id=shared, real_time="2026-10-12T08")
    assert ids(host, shared, "action", host.owner(BUSINESS)) == [late["action_id"]]
    assert ids(host, shared, "action", host.owner(OTHER)) == [kept["action_id"]]


def test_a_text_correction_keeps_the_evidence_and_an_evidence_correction_starts_it_again(host):
    root, child, _ = story(host)
    unrelated = host.create_task("Unrelated Task")
    text_only = host.record_action("First wording", ctx=host.agent(AGENT), task_id=child["id"])
    repointed_in = host.record_action("Points elsewhere", ctx=host.agent(AGENT), task_id=unrelated["id"])
    repointed_out = host.record_action("Points here", ctx=host.agent(AGENT), task_id=child["id"])
    host.later(seconds=1)
    corrected = host.data("actions_correct", {"action_id": text_only["action_id"], "text": "Second wording"}, host.agent(AGENT),
                          expected_revision=1)["action"]
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [root["id"]])
    host.later(seconds=1)
    # After the move, in the scope the Actions still live in: reword one, re-point the other two.
    host.data("actions_correct", {"action_id": text_only["action_id"], "text": "Third wording"}, host.agent(AGENT),
              expected_revision=corrected["revision"])
    host.data("actions_correct", {"action_id": repointed_in["action_id"], "evidence": {"task": child["id"]}}, host.agent(AGENT),
              expected_revision=1)
    host.data("actions_correct", {"action_id": repointed_out["action_id"], "evidence": {"task": unrelated["id"]}}, host.agent(AGENT),
              expected_revision=1)
    assert host.count("action_revisions", "action_key=?", text_only["action_id"]) == 3
    # The reworded Action still names the Task it named when the Task was in the source; the Action re-pointed to the Task only
    # after it left cannot be proven to name that lifetime, and the one re-pointed away no longer names it.
    assert ids(host, root["id"], "action", host.owner(OTHER)) == [text_only["action_id"]]
    assert host.read("tasks_associated_records", {"task_id": root["id"], "record_type": "action"}, host.owner(OTHER))["items"][0][
        "label"] == "Third wording"


def test_an_action_without_a_complete_revision_history_proves_nothing(host):
    root, child, _ = story(host)
    action = host.record_action("History lost", ctx=host.agent(AGENT), task_id=child["id"])
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [root["id"]])
    destination = host.owner(OTHER)
    assert ids(host, root["id"], "action", destination) == [action["action_id"]]
    # An Action row whose revision count outruns its stored revisions cannot show when its evidence was established.
    with host.database.transaction() as uow:
        uow.connection.execute("DROP TRIGGER action_revisions_keep")
        uow.connection.execute("DELETE FROM action_revisions WHERE action_key=?", (action["action_id"],))
    assert ids(host, root["id"], "action", destination) == []


# -- deduplication and paging when old and current associations both contribute -------------------------------------------

def test_old_and_current_associations_deduplicate_and_page_together(host):
    root, child, _ = story(host)
    source_child = host.record_action("Source on the child", ctx=host.agent(AGENT), task_id=child["id"])
    source_root = host.record_action("Source on the root", ctx=host.agent(AGENT), task_id=root["id"])
    # Two routes to the same old record: its evidence (child) and a message on the root that cites it.
    note(host, root["id"], [{"type": "action", "id": source_child["action_id"]}])
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [root["id"]])
    host.later(seconds=1)
    destination_child = host.record_action("Destination on the child", ctx=host.agent(OTHER_AGENT, OTHER), task_id=child["id"])
    destination_root = host.record_action("Destination on the root", ctx=host.agent(OTHER_AGENT, OTHER), task_id=root["id"])
    note(host, root["id"], [{"type": "action", "id": destination_child["action_id"]}], business=OTHER)
    ctx = host.owner(OTHER)
    everything = items(host, root["id"], "action", ctx)
    expected = sorted([source_child["action_id"], source_root["action_id"], destination_child["action_id"], destination_root["action_id"]])
    assert [item["id"] for item in everything] == expected
    found = {item["id"]: item["task_ids"] for item in everything}
    assert found[source_child["action_id"]] == sorted([child["id"], root["id"]])
    assert found[destination_child["action_id"]] == sorted([child["id"], root["id"]])
    assert found[source_root["action_id"]] == [root["id"]] and found[destination_root["action_id"]] == [root["id"]]
    paged, cursor = [], None
    while True:
        page = associated(host, root["id"], "action", ctx, limit=1, **({"cursor": cursor} if cursor else {}))
        assert page.status == "ok", page.error
        assert len(page.data["items"]) <= 1
        paged += [item["id"] for item in page.data["items"]]
        cursor = page.data["next_cursor"]
        if not cursor:
            break
    assert paged == expected


# -- the caller's current authority in the origin ---------------------------------------------------------------------------

class DeniedSourceReads(PolicyResolver):
    """The ordinary policy, except that Action and Signoff item reads are denied in one scope."""

    scope = BUSINESS

    def resolve(self, context, descriptor, targets, uow):
        if descriptor.name in {"actions_get", "signoffs_get"} and context.business_id == self.scope:
            return PolicyDecision("deny", "synthetic_denial")
        return super().resolve(context, descriptor, targets, uow)


def build_moved_story(host):
    root, child, _ = story(host)
    old_action = host.record_action("Old action", ctx=host.agent(AGENT), task_id=child["id"])
    old_signoff = applied_signoff(host, child)
    note(host, root["id"], [{"type": "action", "id": old_action["action_id"]}])
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [root["id"]])
    host.later(seconds=1)
    new_action = host.record_action("New action", ctx=host.agent(OTHER_AGENT, OTHER), task_id=child["id"])
    return root, old_action, old_signoff, new_action


def test_a_denied_read_in_the_origin_omits_the_record_without_a_trace(host):
    root, old_action, old_signoff, new_action = build_moved_story(host)
    destination = host.owner(OTHER)
    assert set(ids(host, root["id"], "action", destination)) == {old_action["action_id"], new_action["action_id"]}
    assert ids(host, root["id"], "signoff", destination) == [old_signoff]
    host.app.policy = DeniedSourceReads(host.registry, host.agents, rules=PolicyRules())
    actions = associated(host, root["id"], "action", destination)
    signoffs = associated(host, root["id"], "signoff", destination)
    assert actions.status == "ok" and [item["id"] for item in actions.data["items"]] == [new_action["action_id"]]
    assert signoffs.status == "ok" and not signoffs.data["items"]
    shown = json.dumps([actions.data, signoffs.data], default=str)
    assert old_action["action_id"] not in shown and old_signoff not in shown and "Old action" not in shown


def test_a_revoked_source_membership_omits_the_old_records_but_not_the_destination_ones(host):
    root, old_action, old_signoff, new_action = build_moved_story(host)
    with host.database.transaction() as uow:
        uow.repository("business_memberships", business_id=OTHER).insert({"principal_id": STRANGER})
    both = InvocationContext(Principal(STRANGER, "human"), business_id=OTHER, allowed_business_ids=frozenset({BUSINESS, OTHER}),
                             source="dashboard")
    only_destination = InvocationContext(Principal(STRANGER, "human"), business_id=OTHER, allowed_business_ids=frozenset({OTHER}),
                                         source="dashboard")
    assert set(ids(host, root["id"], "action", both)) == {old_action["action_id"], new_action["action_id"]}
    assert ids(host, root["id"], "signoff", both) == [old_signoff]
    actions = associated(host, root["id"], "action", only_destination)
    signoffs = associated(host, root["id"], "signoff", only_destination)
    assert actions.status == "ok" and [item["id"] for item in actions.data["items"]] == [new_action["action_id"]]
    assert signoffs.status == "ok" and not signoffs.data["items"]
    shown = json.dumps([actions.data, signoffs.data], default=str)
    assert old_action["action_id"] not in shown and old_signoff not in shown and BUSINESS not in shown


# -- the owner-only shared Global inference, across a move ------------------------------------------------------------------

def test_a_shared_global_action_follows_a_move_only_when_the_original_association_is_proven(host):
    root, child, _ = story(host)
    ambiguous_id = "00000000-0000-4000-8000-0000000000d3"
    host.create_task("Ambiguous Story", id=ambiguous_id)
    host.data("tasks_create", {"title": "Global namesake", "scope": "global", "id": ambiguous_id}, host.coordinator(None))
    proven = shared_action(host, "Shared with the source", child["id"])
    elsewhere = shared_action(host, "Shared with the destination only", child["id"], businesses=(OTHER,))
    colliding = shared_action(host, "Ambiguous when recorded", ambiguous_id)
    assert ids(host, root["id"], "action") == [proven["action_id"]] and ids(host, ambiguous_id, "action") == []
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [root["id"], ambiguous_id])
    host.later(seconds=1)
    after = shared_action(host, "Shared with the destination afterwards", child["id"], businesses=(OTHER,))
    found = items(host, root["id"], "action", host.owner(OTHER))
    # The Action shared with the source keeps its Global identity and storage; the one shared with the destination before the
    # Task arrived names a Task the destination did not have, and the ambiguous one stays ambiguous.
    assert [item["id"] for item in found] == sorted([proven["action_id"], after["action_id"]])
    assert {item["id"]: item["business_id"] for item in found} == {proven["action_id"]: None, after["action_id"]: None}
    assert {proven["action_id"], after["action_id"]} == {item["id"] for item in found}
    assert elsewhere["action_id"] not in ids(host, root["id"], "action", host.owner(OTHER))
    assert ids(host, ambiguous_id, "action", host.owner(OTHER)) == []
    # The path stays owner-only: no other reader gains the association by a move.
    for reader in (host.agent(OTHER_AGENT, OTHER), host.coordinator(OTHER)):
        assert items(host, root["id"], "action", reader) == []


def test_a_record_read_in_two_origins_is_returned_once_with_the_tasks_of_the_origins_that_may_read_it(host):
    root, child, _ = story(host)
    source_only = shared_action(host, "Shared with the source only", child["id"])
    both = shared_action(host, "Shared with both", child["id"], businesses=(BUSINESS, OTHER))
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [root["id"]])
    host.later(seconds=1)
    # The destination cites both Global records from a message on the root: a second origin and a second Story Task.
    note(host, root["id"], [{"type": "action", "id": source_only["action_id"]}, {"type": "action", "id": both["action_id"]}],
         business=OTHER)
    found = {item["id"]: item for item in items(host, root["id"], "action", host.owner(OTHER))}
    assert sorted(found) == sorted([source_only["action_id"], both["action_id"]])
    # Readable only where it was shared when the association was established: that origin's Task alone names it.
    assert found[source_only["action_id"]]["task_ids"] == [child["id"]]
    # Readable in both origins: one item, every Story Task that establishes it, and its true Global storage.
    assert found[both["action_id"]]["task_ids"] == sorted([child["id"], root["id"]])
    assert all(item["business_id"] is None for item in found.values())


# -- the moved Story's old scope --------------------------------------------------------------------------------------------

def test_the_old_scope_names_the_new_place_and_discloses_nothing_to_a_caller_who_may_not_read_it(host):
    ctx = host.owner()
    root, _, _ = story(host)
    move(host, BUSINESS, OTHER, [root["id"]])
    moved = associated(host, root["id"], "signoff", ctx)
    assert moved.status == "error" and moved.error["code"] == "record_moved"
    assert moved.error["details"]["task"] == {"type": "task", "id": root["id"], "business_id": OTHER}
    assert moved.error["details"]["destination"]["id"] == OTHER
    # A caller with no reach into the destination gets the plain refusal with no location detail.
    outsider = InvocationContext(Principal(STRANGER, "human"), business_id=BUSINESS, allowed_business_ids=frozenset({BUSINESS}),
                                 global_capabilities=frozenset(), source="dashboard")
    refused = associated(host, root["id"], "signoff", outsider)
    assert refused.status == "error" and refused.error["code"] != "record_moved" and not refused.error.get("details")
    # A Task that never existed in the old scope stays an ordinary not_found.
    absent = associated(host, "never-existed", "signoff", ctx)
    assert absent.status == "error" and absent.error["code"] == "not_found"


# -- a journey the audit proves, or no inference at all -------------------------------------------------------------------

def test_a_shared_global_action_follows_every_hop_of_a_journey_by_the_tasks_own_creation(host):
    task = host.create_task("Travelling story")
    first = shared_action(host, "Shared with the first stay", task["id"])
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [task["id"]])
    host.later(seconds=1)
    second = shared_action(host, "Shared during the second stay", task["id"], businesses=(OTHER,))
    host.later(seconds=1)
    move(host, OTHER, THIRD, [task["id"]], ctx=owner_of(host, OTHER))
    host.later(seconds=1)
    # Shared with a scope the Task had already left: its evidence was not established during that stay.
    late = shared_action(host, "Shared with a scope it had left", task["id"], businesses=(OTHER,))
    third = shared_with(host, "Shared during the third stay", task["id"], THIRD)
    expected = sorted([first["action_id"], second["action_id"], third["action_id"]])
    assert ids(host, task["id"], "action", owner_of(host, THIRD)) == expected
    host.later(seconds=1)
    move(host, THIRD, BUSINESS, [task["id"]], ctx=owner_of(host, THIRD))
    assert ids(host, task["id"], "action", owner_of(host, BUSINESS)) == expected
    assert late["action_id"] not in expected
    # Still owner-only after any number of moves.
    for reader in (host.agent(AGENT), host.coordinator(BUSINESS)):
        assert items(host, task["id"], "action", reader) == []


def arrival(host, identity):
    """A Task reaching OTHER after a different, earlier Task of the same bare ID lived there and left.

    Returns every record the scenarios need: the arriving Task's own work (inferred from evidence, and cited from a discussion
    written before the move), the destination work it did after arriving, an explicitly related Action, and the records of
    the earlier resident - which must never be read as the arriving Task's."""
    arriving = host.create_task("Arriving task", id=identity)
    found = {"inferred": host.record_action("Source work inferred from evidence", ctx=host.agent(AGENT), task_id=identity)["action_id"],
             "cited": host.record_action("Source work cited in a discussion", ctx=host.agent(AGENT), task_id=identity)["action_id"],
             "signed_cited": applied_signoff(host, arriving, title="Cited decision")}
    found["signed_inferred"] = applied_signoff(host, arriving, title="Inferred decision")
    note(host, identity, [{"type": "action", "id": found["cited"]}, {"type": "signoff", "id": found["signed_cited"]}])
    host.later(seconds=1)
    resident = host.create_task("Earlier resident", id=identity, ctx=host.owner(OTHER))
    found["decoy"] = host.record_action("The resident's work", ctx=host.agent(OTHER_AGENT, OTHER), task_id=identity)["action_id"]
    found["decoy_signoff"] = applied_signoff(host, resident, ctx=host.agent(OTHER_AGENT, OTHER), business=OTHER)
    host.later(seconds=1)
    move(host, OTHER, THIRD, [identity], ctx=owner_of(host, OTHER))
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [identity])
    host.later(seconds=1)
    found["arrived"] = host.record_action("Work after arriving", ctx=host.agent(OTHER_AGENT, OTHER), task_id=identity)["action_id"]
    found["related"] = host.record_action("Explicitly related work", ctx=host.agent(OTHER_AGENT, OTHER))["action_id"]
    concern(host, {"type": "task", "id": identity}, {"type": "action", "id": found["related"]}, business=OTHER)
    return found


def test_a_task_whose_bounded_history_overflows_gains_no_inferred_records_from_any_lifetime(host, monkeypatch):
    identity = "00000000-0000-4000-8000-0000000000f1"
    found = arrival(host, identity)
    reader = host.owner(OTHER)
    # With the audit intact the Task reads its own stays and never the earlier resident's records.
    assert set(ids(host, identity, "action", reader)) == {found[name] for name in ("inferred", "cited", "arrived", "related")}
    assert set(ids(host, identity, "signoff", reader)) == {found["signed_cited"], found["signed_inferred"]}
    before = snapshot(host)
    # Two recorded moves of this id exceed a limit of one: the same branch as 1,001 moves, without making them.
    monkeypatch.setattr(relocation, "MAXIMUM_STEPS", 1)
    # Nothing is inferred - not from the source, not from the destination, and not from the earlier resident - but the explicit
    # associations (a typed discussion reference, a ``concerns`` relationship) are unaffected.
    assert set(ids(host, identity, "action", reader)) == {found["cited"], found["related"]}
    assert ids(host, identity, "signoff", reader) == [found["signed_cited"]]
    assert found["decoy"] not in ids(host, identity, "action", reader) and found["decoy_signoff"] not in ids(host, identity, "signoff", reader)
    assert snapshot(host) == before


def synthetic_move(host, task_id, source, destination, created_at, *, at):
    """One recorded move written straight to the append-only audit: the break a damaged or foreign history would have."""
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO task_moves(move_id,task_id,source_business_id,destination_business_id,task_created_at,previous_parent_id,"
            "revision_before,revision_after,actor_id,moved_at) VALUES (?,?,?,?,?,NULL,1,2,?,?)",
            (new_id(), task_id, source, destination, created_at, HUMAN, at))


@pytest.mark.parametrize("source,destination", [
    pytest.param(THIRD, BUSINESS, id="chain"),     # the next recorded move starts somewhere the Task had not been
    pytest.param(OTHER, THIRD, id="ending"),       # the recorded journey ends where the Task is not
])
def test_a_discontinuous_journey_gains_no_inferred_records_but_keeps_its_explicit_ones(host, source, destination):
    identity = "00000000-0000-4000-8000-0000000000f2"
    found = arrival(host, identity)
    reader = host.owner(OTHER)
    created = host.query("SELECT created_at FROM tasks WHERE business_id=? AND id=?", OTHER, identity)[0]["created_at"]
    assert found["inferred"] in ids(host, identity, "action", reader)
    host.later(seconds=1)
    synthetic_move(host, identity, source, destination, created, at=host.stamp())
    assert set(ids(host, identity, "action", reader)) == {found["cited"], found["related"]}
    assert ids(host, identity, "signoff", reader) == [found["signed_cited"]]
    # Never as a plain Task either: no unbounded current-scope read of the destination's own records for it.
    assert found["arrived"] not in ids(host, identity, "action", reader) and found["decoy"] not in ids(host, identity, "action", reader)


def test_a_shared_global_action_proves_nothing_when_the_journey_cannot_be_audited(host, monkeypatch):
    task = host.create_task("Shared story")
    action = shared_action(host, "Original shared result", task["id"])
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [task["id"]])
    host.later(seconds=1)
    move(host, OTHER, THIRD, [task["id"]], ctx=owner_of(host, OTHER))
    reader = owner_of(host, THIRD)
    assert ids(host, task["id"], "action", reader) == [action["action_id"]]
    monkeypatch.setattr(relocation, "MAXIMUM_STEPS", 1)
    assert ids(host, task["id"], "action", reader) == []


def test_another_lifetime_of_the_id_with_no_provable_whereabouts_stops_a_shared_action_from_linking(host):
    identity = "00000000-0000-4000-8000-0000000000f3"
    host.create_task("Shared story", id=identity)
    action = shared_action(host, "Shared with the source", identity)
    host.later(seconds=1)
    # A different Task with the same id lives in a project the Action was never shared with, and its journey is whole.
    host.create_task("Namesake elsewhere", id=identity, ctx=owner_of(host, THIRD))
    host.later(seconds=1)
    move(host, BUSINESS, OTHER, [identity])
    reader = host.owner(OTHER)
    assert ids(host, identity, "action", reader) == [action["action_id"]]
    # The audit then claims the namesake came from the Action's own project and is not where it says it ended: where it was, at
    # the instant the Action was shared, is unknown, so uniqueness cannot be proven and nothing is linked.
    created = host.query("SELECT created_at FROM tasks WHERE business_id=? AND id=?", THIRD, identity)[0]["created_at"]
    synthetic_move(host, identity, BUSINESS, OTHER, created, at=host.stamp())
    assert ids(host, identity, "action", reader) == []
