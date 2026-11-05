"""Recorded observations, the record's local activity page and record resolution: append-only, bounded, scoped, never leaking."""

from __future__ import annotations

import socket
from dataclasses import replace

import pytest

from crm_support import CRM_COLUMNS, STAGES, CRMHost, ref_of
from operational_support import AGENT, AGENT2, BUSINESS, COORDINATOR, HUMAN, OTHER
from qorqut_operations.application.plans import PlanBuilder
from qorqut_operations.contracts import DomainError, new_id, plain

SOURCE = {"kind": "email", "provider": "fastmail", "account_key": "sales", "thread_id": "T-1", "message_id": "M-1",
          "url": "https://mail.example.test/m/1", "title": "Proposal v2"}


@pytest.fixture
def host(tmp_path):
    return CRMHost(tmp_path)


def record(host, task, key="k1", ctx=None, **extra):
    arguments = {"task_id": task["id"], "kind": "email", "summary": "Sent the proposal", "body": "Hello,\nproposal attached.",
                 "occurred_at": "2026-10-11T10:00:00Z", "source": SOURCE, "event_key": key, **extra}
    return host.data("crm_activity_record", arguments, ctx or host.agent(AGENT))


def local(host, task, ctx=None, **args):
    with host.database.read() as uow:
        return host.crm.local_activity(ctx or host.owner(), task["id"], args, uow)


def ids(page):
    return [item["id"] for item in page["items"]]


# -- recording -----------------------------------------------------------------------------------------------------------------

def test_an_observation_is_recorded_once_with_its_source_and_never_acts(host, monkeypatch):
    task = host.create_task("Acme", task_type="customer")

    def forbidden(*args, **kwargs):
        raise AssertionError("recording must not touch the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    revision = host.task(task["id"])["revision"]
    first = record(host, task)
    event = first["event"]
    assert first["duplicate"] is False and event["kind"] == "email" and event["origin"] == "recorded"
    assert event["occurred_at"] == "2026-10-11T10:00:00.000000Z" and event["recorded_at"] and event["actor"]["id"] == AGENT
    assert event["id"] == f"email:business:{BUSINESS}:{event['source']['id']}" and event["body"] == "Hello,\nproposal attached."
    assert event["source"]["label"] == "Proposal v2" and event["source"]["url"] == SOURCE["url"] and event["source"]["reference"] == SOURCE
    # A retry (same key, same content) is the original; the Task, its thread and its revision are untouched.
    again = record(host, task)
    assert again["duplicate"] is True and again["event"] == event and host.count("crm_activity_events") == 1
    assert host.task(task["id"])["revision"] == revision and host.count("thread_messages", "item_type='task'") == 0
    assert host.count("inbox_events") == 0 and host.count("signoffs") == 0
    # The same key with different content is a conflict, never an overwrite; another Task may reuse the key.
    assert host.error("crm_activity_record", {"task_id": task["id"], "kind": "email", "summary": "Different", "body": "x",
                                              "occurred_at": "2026-10-11T10:00:00Z", "source": SOURCE, "event_key": "k1"}, host.agent(AGENT)) == "conflict"
    other = host.create_task("Other")
    assert record(host, other)["duplicate"] is False and host.count("crm_activity_events") == 2


def test_corrections_are_new_events_that_name_an_earlier_one_of_the_same_task(host):
    task, other = host.create_task("Acme"), host.create_task("Other")
    wrong = record(host, task, "wrong")["event"]
    foreign = record(host, other, "foreign")["event"]
    fixed = record(host, task, "fixed", summary="Corrected summary", corrects_event_id=wrong["source"]["id"])["event"]
    assert fixed["change"] == {"kind": "correction", "corrects_event_id": wrong["source"]["id"], "corrects": wrong["id"]}
    assert host.query("SELECT summary FROM crm_activity_events WHERE id=?", wrong["source"]["id"])[0]["summary"] == "Sent the proposal"
    for bad in (foreign["source"]["id"], new_id()):
        assert host.error("crm_activity_record", {"task_id": task["id"], "kind": "email", "summary": "x", "body": "y", "occurred_at": "2026-10-11T10:00:00Z",
                                                  "source": SOURCE, "event_key": "bad-" + bad[:4], "corrects_event_id": bad}, host.agent(AGENT)) == "validation_error"
    with host.database.transaction() as uow:
        for statement in ("UPDATE crm_activity_events SET summary='rewritten'", "DELETE FROM crm_activity_events",
                          "UPDATE crm_activity_events SET occurred_at='2020-01-01T00:00:00.000000Z'"):
            with pytest.raises(Exception):
                uow.connection.execute(statement)
        with pytest.raises(Exception):
            uow.connection.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
        with pytest.raises(Exception):      # even outside the engine, one key names one event per Task
            uow.connection.execute("INSERT INTO crm_activity_events(business_id,id,task_id,event_key,kind,summary,body,occurred_at,recorded_at,recorded_by,source_json,content_hash) "
                                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (BUSINESS, new_id(), task["id"], "wrong", "email", "s", "b", "2026-10-11T10:00:00.000000Z",
                                                                         "2026-10-12T12:00:00.000000Z", HUMAN, "{}", "h"))


def test_sources_times_and_text_are_validated_as_data_only(host):
    task = host.create_task("Acme")
    base = {"task_id": task["id"], "kind": "message", "summary": "s", "body": "b", "occurred_at": "2026-10-11T10:00:00Z", "event_key": "v"}

    def code(**changes):
        return host.error("crm_activity_record", {**base, "source": {"kind": "external"}, **changes}, host.agent(AGENT))
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:pw@mail.example.test/x", "https://exa mple.test", "//no-scheme.test",
                "https://" + "a" * 4001, "ftp://host/x"):
        assert code(source={"kind": "external", "url": url}) == "validation_error", url
    for source in ({"kind": "carrier-pigeon"}, {}, {"kind": "email", "message_id": "x" * 501}, {"kind": "email", "title": "bad\x00title"}, {"kind": "email", "extra": 1}):
        assert code(source=source) == "validation_error", source
    for when in ("2026-10-11T10:00:00", "yesterday", "2099-01-01T00:00:00Z", ""):
        assert code(occurred_at=when) == "validation_error", when
    for changes in ({"summary": "  "}, {"summary": "x" * 501}, {"body": "b" * 20001}, {"event_key": "k" * 301}, {"event_key": "bad\nkey"}, {"kind": "call"}):
        assert code(**changes) == "validation_error", changes
    zoned = host.data("crm_activity_record", {**base, "source": {"kind": "manual"}, "occurred_at": "2026-10-11T12:00:00+02:00"}, host.agent(AGENT))
    assert zoned["event"]["occurred_at"] == "2026-10-11T10:00:00.000000Z"
    assert host.count("crm_activity_events") == 1


def test_recording_needs_comment_authority_in_the_tasks_own_scope(host):
    task = host.create_task("Acme")
    host.grants[AGENT2] = {"tasks_comment": "deny"}
    assert host.error("crm_activity_record", {"task_id": task["id"], "kind": "email", "summary": "s", "body": "b", "occurred_at": "2026-10-11T10:00:00Z",
                                              "source": SOURCE, "event_key": "x"}, host.agent(AGENT2)) == "policy_denied"
    elsewhere = host.agent(AGENT, business=OTHER)
    assert host.error("crm_activity_record", {"task_id": task["id"], "kind": "email", "summary": "s", "body": "b", "occurred_at": "2026-10-11T10:00:00Z",
                                              "source": SOURCE, "event_key": "x"}, elsewhere) == "not_found"
    host.grants[AGENT] = {"crm_activity_record": "signoff"}
    staged = host.stage("crm_activity_record", {"task_id": task["id"], "kind": "development", "summary": "Deployed", "body": "Release 2",
                                                "occurred_at": "2026-10-11T11:00:00Z", "source": {"kind": "manual"}, "event_key": "dev-1"}, host.agent(AGENT))
    assert host.count("crm_activity_events") == 0
    assert host.apply_approved(staged)["state"] == "applied" and host.count("crm_activity_events") == 1
    assert host.count("crm_activity_events") == 1 and host.query("SELECT recorded_by FROM crm_activity_events")[0]["recorded_by"] == AGENT


def test_a_staged_observation_for_a_vanished_task_is_refused_at_approval(host):
    task = host.create_task("Acme")
    host.grants[AGENT] = {"crm_activity_record": "signoff"}
    staged = host.stage("crm_activity_record", {"task_id": task["id"], "kind": "email", "summary": "s", "body": "b", "occurred_at": "2026-10-11T10:00:00Z",
                                                "source": SOURCE, "event_key": "gone"}, host.agent(AGENT))
    host.data("tasks_update", {"task_id": task["id"], "changes": {"title": "Changed"}}, expected_revision=task["revision"])
    assert host.approve(staged).status == "error" and host.count("crm_activity_events") == 0


# -- local activity ---------------------------------------------------------------------------------------------------------------

def test_local_activity_merges_the_records_own_sources_newest_first(host):
    task = host.create_task("Acme", task_type="customer")
    table = host.crm_table()
    host.later(seconds=1)
    record(host, task, "early", occurred_at="2026-10-01T09:00:00Z", summary="Early mail")
    host.later(seconds=1)
    host.data("tasks_comment", {"task_id": task["id"], "text": "Called them"}, host.agent(AGENT), expected_revision=task["revision"])
    host.later(seconds=1)
    host.add(table, task, cells={"value": 1}, stage_id="lead")
    host.later(seconds=1)
    record(host, task, "dev", kind="development", occurred_at="2026-10-02T09:00:00Z", summary="Shipped", source={"kind": "manual"})
    page = local(host, task)
    # Newest first by the time each thing happened: the CRM change and the comment (now), then the two backdated observations.
    assert [item["origin"] for item in page["items"]][2:] == ["recorded", "recorded"]
    assert sorted(item["origin"] for item in page["items"][:2]) == ["crm_membership", "own"] and page["next_cursor"] is None and page["as_of"]
    assert [item["summary"] for item in page["items"]][2:] == ["Shipped", "Early mail"]
    times = [item["occurred_at"] for item in page["items"]]
    assert times == sorted(times, reverse=True)
    by_origin = {item["origin"]: item for item in page["items"] if item["origin"] != "recorded"}
    assert by_origin["own"]["body"] == "Called them" and by_origin["own"]["source"]["id"] == task["id"]
    membership = by_origin["crm_membership"]
    assert membership["change"]["operation"] == "added" and membership["source"] == {"type": "table", "id": table["id"], "business_id": BUSINESS, "label": "Pipeline"}
    assert len({item["id"] for item in page["items"]}) == 4


def test_activity_pages_are_stable_with_ties_and_the_ingestion_cutoff_holds(host):
    task = host.create_task("Acme")
    for number in range(5):
        record(host, task, f"tie-{number}", occurred_at="2026-10-11T10:00:00Z")           # five events with one timestamp
    host.later(seconds=1)
    first = local(host, task, limit=2)
    assert len(first["items"]) == 2 and first["next_cursor"]
    # A late record, even one dated far in the past, never enters a browsing session that already fixed its cutoff.
    host.later(seconds=1)
    record(host, task, "late", occurred_at="2026-09-01T00:00:00Z")
    record(host, task, "late-new", occurred_at="2026-10-11T11:30:00Z")
    seen, page = ids(first), first
    while page["next_cursor"]:
        page = local(host, task, limit=2, cursor=page["next_cursor"])
        assert page["as_of"] == first["as_of"]
        seen += ids(page)
    assert len(seen) == len(set(seen)) == 5
    fresh = local(host, task)
    assert len(fresh["items"]) == 7 and fresh["as_of"] > first["as_of"]
    # Restating a different cutoff against a cursor, a foreign cursor and a garbled cursor are all refused.
    for args in ({"cursor": first["next_cursor"], "as_of": "2026-01-01T00:00:00.000000Z"}, {"cursor": "bogus"}):
        with pytest.raises(DomainError):
            local(host, task, **args)
    other = host.create_task("Other")
    with pytest.raises(DomainError) as moved:
        local(host, other, cursor=first["next_cursor"])
    assert moved.value.code == "validation_error"
    assert len(local(host, task, limit=500)["items"]) == 7 and len(local(host, task, as_of=first["as_of"])["items"]) == 5


def test_local_activity_is_authorized_in_the_tasks_scope_and_filters_foreign_developments(host):
    owner = host.owner(business=None)
    secret = host.data("tasks_create", {"title": "Global secret", "scope": "global"}, owner)["task"]
    business_table = host.crm_table()
    global_table = host.data("tables_create", {"title": "G", "table_kind": "crm", "columns": CRM_COLUMNS, "crm_config": STAGES, "scope": "global"}, owner)["table"]
    host.add(business_table, secret)
    host.data("crm_entries_add", {"table_id": global_table["id"], "scope": "global", "record": ref_of(secret)}, owner, expected_revision=1)
    record_ = host.data("crm_activity_record", {"task_id": secret["id"], "scope": "global", "kind": "email", "summary": "Global mail", "body": "b",
                                                "occurred_at": "2026-10-11T10:00:00Z", "source": {"kind": "manual"}, "event_key": "g"}, host.coordinator())
    with host.database.read() as uow:
        full = host.crm.local_activity(owner, secret["id"], {"scope": "global"}, uow)
        assert sorted(item["origin"] for item in full["items"]) == ["crm_membership", "crm_membership", "recorded"]
        with pytest.raises(DomainError) as denied:      # the project agent cannot read the Global Task at all
            host.crm.local_activity(host.agent(AGENT), secret["id"], {"scope": "global"}, uow)
        assert denied.value.code in {"forbidden", "policy_denied", "scope_required"}
        with pytest.raises(DomainError):
            host.crm.local_activity(host.agent(AGENT), secret["id"], {}, uow)       # and a business view never reaches a Global id
    visible = host.create_task("Visible")
    host.add(host.get(business_table), visible)
    host.grants[AGENT2] = {"tables_get": "deny"}
    with host.database.read() as uow:
        mine = host.crm.local_activity(host.agent(AGENT), visible["id"], {}, uow)
        blind = host.crm.local_activity(host.agent(AGENT2), visible["id"], {}, uow)
    assert [item["origin"] for item in mine["items"]] == ["crm_membership"] and blind["items"] == []
    assert record_["event"]["actor"]["id"] == COORDINATOR


# -- recording is a shared-history write: the Task-comment rule, authority and channel -------------------------------------------

def test_recording_is_held_to_the_task_comment_channel_rule(host):
    owner = host.owner(business=None)
    task = host.data("tasks_create", {"title": "Global co", "scope": "global"}, owner)["task"]
    args = {"task_id": task["id"], "scope": "global", "kind": "message", "summary": "Team note", "body": "Team note",
            "source": {"kind": "manual"}, "event_key": "owner-1", "occurred_at": "2026-10-11T10:00:00Z"}
    said = host.invoke("tasks_comment", {"task_id": task["id"], "text": "Team note", "scope": "global"}, owner, expected_revision=task["revision"])
    assert said.status == "error" and said.error["code"] == "channel_required"
    # the installation owner's shared-record write is refused the same way, at every stage, and writes nothing
    refused = host.invoke("crm_activity_record", args, owner)
    assert refused.status == "error" and refused.error["code"] == "channel_required"
    assert refused.error["details"]["refused_operation"] == "tasks_comment" and refused.error["details"]["operation"] == "owner_conversation_send"
    assert host.count("crm_activity_events") == 0
    with host.database.read() as uow:
        planned = host.crm.activity.prepare_record(host.coordinator(), {**args, "event_key": "by-coordinator"},
                                                   type("Options", (), {"expected_revision": None})(), uow)
        for stage in (host.crm.activity.validate_record, host.crm.activity.apply_record):
            with pytest.raises(DomainError) as late:
                stage(owner, planned, uow)
            assert late.value.code == "channel_required"
    # an authorized non-owner records it, attributed to its authenticated recorder; a retry returns the original
    first = host.data("crm_activity_record", {**args, "event_key": "agent-1"}, host.coordinator())
    assert first["event"]["actor"]["id"] == COORDINATOR and first["duplicate"] is False
    assert host.data("crm_activity_record", {**args, "event_key": "agent-1"}, host.coordinator())["duplicate"] is True
    # the project-Task exception of tasks_comment applies unchanged: an owner may comment on, and so record into, a project Task
    project = host.create_task("Project co")
    project_args = {"task_id": project["id"], "kind": "message", "summary": "Owner note", "body": "Owner note", "source": {"kind": "manual"},
                    "event_key": "owner-p", "occurred_at": "2026-10-11T10:00:00Z"}
    assert host.invoke("tasks_comment", {"task_id": project["id"], "text": "Owner note"}, host.owner(), expected_revision=project["revision"]).status == "ok"
    mine = host.data("crm_activity_record", project_args, host.owner())
    assert mine["event"]["actor"]["id"] == HUMAN
    assert host.count("crm_activity_events") == 2


def test_recording_needs_the_task_comment_decision_and_a_saved_result_needs_it_again(host):
    task = host.create_task("Acme")
    agent = host.agent(AGENT)
    arguments = {"task_id": task["id"], "kind": "message", "summary": "Called", "body": "Left a voicemail", "source": {"kind": "manual"},
                 "event_key": "k", "occurred_at": "2026-10-11T10:00:00Z"}
    host.grants[AGENT] = {"tasks_comment": "deny", "crm_activity_record": "allow"}
    assert host.error("crm_activity_record", arguments, agent) == "policy_denied"        # an allow on the new name never opens it
    host.grants[AGENT] = {"tasks_comment": "signoff", "crm_activity_record": "allow"}
    staged = host.stage("crm_activity_record", arguments, agent)
    assert host.count("crm_activity_events") == 0
    host.grants[AGENT] = {"tasks_comment": "deny"}                               # the authority changes before it is applied
    approved = host.approve(staged)
    if approved.status == "accepted":
        host.service.run_one(staged)
    assert host.count("crm_activity_events") == 0 and host.row(staged)["state"] != "applied"
    host.grants[AGENT] = {"tasks_comment": "signoff"}
    assert host.apply_approved(host.stage("crm_activity_record", arguments, agent))["state"] == "applied"
    event = host.query("SELECT recorded_by FROM crm_activity_events")[0]
    assert event["recorded_by"] == AGENT and host.count("crm_activity_events") == 1
    # a saved result is served while the decision stands, and refused once it no longer does
    host.grants[AGENT] = {}
    key = new_id()
    other = {**arguments, "event_key": "k2"}
    first = host.invoke("crm_activity_record", other, agent, idempotency_key=key)
    assert first.status == "ok" and plain(host.invoke("crm_activity_record", other, agent, idempotency_key=key).data) == plain(first.data)
    host.grants[AGENT] = {"tasks_comment": "deny"}
    refused = host.invoke("crm_activity_record", other, agent, idempotency_key=key)
    assert refused.status == "error" and refused.error["code"] == "policy_denied" and host.count("crm_activity_events") == 2


# -- the source Table's discussion read decides what its developments show ----------------------------------------------------------

def test_a_membership_development_needs_the_tables_discussion_read_in_its_own_scope(host):
    table = host.crm_table("Pipeline Secret")
    task = host.create_task("Visible")
    host.data("tasks_comment", {"task_id": task["id"], "text": "Own note"}, expected_revision=task["revision"])
    host.add(table, task)
    peer = host.agent(AGENT2)

    def developments(ctx):
        return [item for item in local(host, task, ctx)["items"] if item["origin"] == "crm_membership"]

    assert len(developments(peer)) == 1
    host.grants[AGENT2] = {"tables_thread_get": "deny"}              # the Table is readable, its discussion is not
    assert host.read("tables_get", {"table_id": table["id"]}, peer)["table"]["id"] == table["id"]
    assert host.error("tables_thread_get", {"table_id": table["id"]}, peer) == "policy_denied"
    hidden = local(host, task, peer)
    assert not developments(peer) and "Pipeline Secret" not in repr(hidden) and "crm_membership" not in repr(hidden)
    assert any(item["origin"] == "own" for item in hidden["items"])  # the Task's own history is still complete
    host.grants[AGENT2] = {}
    assert len(developments(peer)) == 1                             # reauthorized: shown again on the next read
    # a development of another scope's Table needs the reader's access to that scope, checked now
    elsewhere = host.crm_table("Other Pipeline", host.owner(business=OTHER))
    host.data("crm_entries_add", {"table_id": elsewhere["id"], "record": ref_of(task)}, host.owner(business=OTHER), expected_revision=1)
    coordinator = host.coordinator(business=BUSINESS)
    assert len(developments(coordinator)) == 2
    reduced = replace(coordinator, allowed_business_ids=frozenset({BUSINESS}))
    shown = local(host, task, reduced)
    assert len(developments(reduced)) == 1 and "Other Pipeline" not in repr(shown)
    host.grants[COORDINATOR] = {"tables_thread_get": "deny"}
    assert developments(coordinator) == []


# -- a record's developments are scanned completely, in bounded batches --------------------------------------------------------

def bulk_memberships(host, task, template, count):
    """Synthetic: ``count`` more CRM tables of the template's project, each holding the Task with its membership development."""
    ctx = host.owner(business=template["business_id"])
    entry = host.query("SELECT * FROM crm_entries WHERE table_id=?", template["id"])[0]
    with host.database.transaction() as uow:
        tables = uow.repository("data_tables", business_id=template["business_id"])
        entries = uow.repository("crm_entries", business_id=template["business_id"])
        row = tables.get(template["id"])
        for number in range(count):
            table_id = new_id()
            tables.insert({**row, "id": table_id, "title": f"Synthetic CRM {number}"})
            added = entries.insert({**entry, "id": new_id(), "table_id": table_id})
            host.crm.threads.record_membership(ctx, uow, None, added)


def walk(host, task, ctx, limit):
    """Every page of a record's activity: ``(membership ids, own and recorded ids, page sizes)``."""
    seen, other, sizes, cursor = [], [], [], None
    while True:
        page = local(host, task, ctx, limit=limit, **({"cursor": cursor} if cursor else {}))
        sizes.append(len(page["items"]))
        seen += [item["id"] for item in page["items"] if item["origin"] == "crm_membership"]
        other += [item["id"] for item in page["items"] if item["origin"] != "crm_membership"]
        cursor = page["next_cursor"]
        if not cursor:
            return seen, other, sizes


def test_every_authorized_membership_development_is_reachable_beyond_two_hundred_tables(host):
    first = host.crm_table("First")
    task = host.create_task("Popular")
    host.add(first, task)
    bulk_memberships(host, task, host.get(first), 200)
    assert host.count("crm_entries") == 201
    seen, other, sizes = walk(host, task, host.owner(), 50)
    assert len(seen) == len(set(seen)) == 201 and max(sizes) <= 50 and sizes == [50, 50, 50, 50, 1]
    # one more page-size does not change what exists: the total is stable however it is paged
    assert len(walk(host, task, host.owner(), 7)[0]) == 201


def test_inaccessible_tables_hide_neither_later_sources_nor_the_continuation(host):
    task = host.create_task("Shared record")
    old = host.crm_table("Old readable")
    host.add(old, task)
    host.later(seconds=1)
    foreign = host.crm_table("Foreign template", host.owner(business=OTHER))
    host.data("crm_entries_add", {"table_id": foreign["id"], "record": ref_of(task)}, host.owner(business=OTHER), expected_revision=1)
    bulk_memberships(host, task, host.get(foreign, host.owner(business=OTHER)), 229)     # 230 newer developments the agent cannot read
    host.later(seconds=1)
    new = host.crm_table("New readable")
    host.add(new, task)
    agent = host.agent(AGENT)
    seen, other, sizes = walk(host, task, agent, 2)
    assert len(seen) == len(set(seen)) == 2 and max(sizes) <= 2 and 1 not in sizes[:-1] and not any("Foreign" in item for item in seen)
    page = local(host, task, agent, limit=100)
    assert [item["source"]["label"] for item in page["items"] if item["origin"] == "crm_membership"] == ["New readable", "Old readable"]
    assert "Synthetic CRM" not in repr(page) and "Foreign template" not in repr(page)
    # the owner (who can read both projects) sees all 232 developments, paged exactly
    assert len(walk(host, task, host.owner(), 40)[0]) == 232


# -- record resolution ---------------------------------------------------------------------------------------------------------------

def test_resolve_record_is_the_full_task_under_current_task_authority(host):
    task = host.create_task("Acme", task_type="customer", customer_profile={"company": "Acme Ltd"})
    owner = host.owner()
    with host.database.read() as uow:
        resolved = host.crm.resolve_record(owner, ref_of(task), uow)
        assert resolved == host.crm.tasks.read("tasks_get", owner, {"task_id": task["id"]}, uow)["task"]
        assert resolved["customer_profile"]["company"] == "Acme Ltd" and "notes" in resolved
        builder = PlanBuilder(owner, "crm_entries_add", {}, "t", namespace="business:" + BUSINESS)
        host.crm.resolve_record(owner, ref_of(task), uow, builder)
        pinned = [target for target in builder.targets.values() if target.resource.resource_type == "tasks"]
        assert len(pinned) == 1 and pinned[0].resource.resource_id == task["id"] and pinned[0].revision == task["revision"]
        for ctx, reference in ((host.agent(AGENT, business=OTHER), ref_of(task)), (host.agent(AGENT), {**ref_of(task), "id": "missing"})):
            with pytest.raises(DomainError) as hidden:
                host.crm.resolve_record(ctx, reference, uow)
            assert hidden.value.code in {"forbidden", "not_found", "policy_denied"}
    host.grants[AGENT2] = {"tasks_get": "deny"}
    with host.database.read() as uow:
        with pytest.raises(DomainError) as revoked:
            host.crm.resolve_record(host.agent(AGENT2), ref_of(task), uow)
        assert revoked.value.code == "policy_denied"
