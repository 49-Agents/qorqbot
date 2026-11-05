"""Acceptance case: a permanent worker's one shared current assignment, the manager's control of its subscriptions, and the Stop
checks that keep both honest.

One synthetic host with two businesses, the configured Ordubashy, two permanent workers and Learning Applier (a global agent that
is no worker). It proves:

* current work: an absent assignment reads as revision 0 and reads or restarts never create, clear or replace it; a write is
  task-only, text-only or both, at most 100 whitespace-separated words, and replaces a Task reference only with
  ``replace_current`` (the previous Task is never completed); the configured Ordubashy follows the Task newly taken up;
* writers: a worker writes only from its current registered native session (a stale or superseded one is refused) and with the
  exact revision; the owner or Ordubashy updates a worker by ``agent_id`` without a session; no other worker or agent does, and
  two workers never touch each other's assignment;
* readers: the authored text is the team's to read, an unreadable Task's title and links are withheld, and old session item lists
  are no second assignment; the non-worker contract (session lists, revision from 1, 300-character fields) is unchanged;
* subscriptions: the owner or Ordubashy lists, adds and removes a worker's source subscriptions and Task/Action follows with the
  source authorized as the worker; nobody else may; mandatory delivery is unaffected;
* Stop checks: finished-work and review corrections use the worker's assignment, once per episode.

Synthetic only: a tmp instance and rows the test appends; no model, provider, network, native process or operational data."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain
from qorqut_operations.discussion import ThreadItem, follow_state

from bazarlyk.composition.locations import agent_workspace_of
from bazarlyk.transport.mcp_presentation import ACTIVITY_AGENT_FIELDS, _activity_summary
from host_foundation_support import (build_instance, create_registered_global, create_worker, owner_global_context,
                                     register_sources, select_coordinator, set_agent_overrides)

SECRET = "Confidential launch plan"
# Reading a Task reference and the worker's own inbox are explicit grants; the worker role itself carries neither.
GRANTS = {"tasks_get": "allow", "inbox_list": "allow"}


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    coordinator = select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    alpha, beta = create_worker(instance, "alpha"), create_worker(instance, "beta")
    applier = create_registered_global(instance, "learning-applier")
    for worker in (alpha, beta):
        set_agent_overrides(instance, worker, GRANTS, [first, second])
    instance.runtime.freeze_registry()
    return SimpleNamespace(instance=instance, runtime=instance.runtime, database=instance.database, tmp=tmp_path, first=first,
                           second=second, coordinator=coordinator, alpha=alpha, beta=beta, applier=applier,
                           owner=replace(owner_global_context(instance), allowed_business_ids=frozenset({first, second})))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


def agent(host, identity, business=None, registration=None):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business,
                                                    session_registration_id=registration)


def open_session(host, identity):
    """A registered native session of ``identity`` and the MCP context that carries its registration."""
    registration, session_id = new_id(), new_id()
    with host.database.transaction() as uow:
        uow.repository("machine_registrations").insert({
            "id": registration, "principal_id": identity, "workspace": str(host.tmp / registration),
            "service_url": "http://127.0.0.1:9", "profile_revision": 1})
    assert host.runtime.activity.register_session(agent(host, identity), registration, session_id, "codex")
    return SimpleNamespace(id=session_id, registration=registration, ctx=agent(host, identity, registration=registration))


def own_owner(host, session):
    """The session becomes the agent's native inbox owner, as a verified binding makes it."""
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO native_session_owners(agent_id,consumer_id,binding_json,generation,desired_state) VALUES(?,?,?,?,'running') "
            "ON CONFLICT(agent_id) DO UPDATE SET consumer_id=excluded.consumer_id,generation=generation+1",
            (session.ctx.principal_id, session.id, json.dumps({"owner": "synthetic"}), 1))


def item(business=None, task=None, summary=None, blocked=None):
    value = {"business_id": business}
    for name, text in (("task_id", task), ("summary", summary), ("blocked_reason", blocked)):
        if text is not None:
            value[name] = text
    return value


def update(host, ctx, expected, *, session=None, **arguments):
    body = {"scope": "global", "expected_work_revision": expected, **arguments}
    if session is not None:
        body["session_id"] = session.id
    return host.instance.invoke(ctx, "agent_work_update", body)


def roster(host, ctx, **arguments):
    data = ok(host.instance.invoke(ctx, "agents_activity_list", {"scope": "global", **arguments}))
    return {row["agent_id"]: row for row in data["agents"]}


def assignment(host, identity, reader=None):
    return roster(host, reader or host.owner)[identity]["current_work"]


def task(host, business, title, **extra):
    return ok(host.instance.invoke(host.instance.human(business), "tasks_create", {"title": title, **extra}))["task"]


def status(host, business, identifier):
    return ok(host.instance.invoke(host.instance.human(business), "tasks_get", {"task_id": identifier}))["task"]["status"]


def following(host, business, identifier, identity):
    with host.database.read() as uow:
        return follow_state(uow, ThreadItem(business, "task", identifier), identity)["following"]


def table(host, name):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(f"SELECT * FROM {name}")]


# -- current work ----------------------------------------------------------------------------------------------------------

def test_a_worker_has_one_durable_assignment_replaced_only_by_an_explicit_write(host):
    session = open_session(host, host.alpha)
    empty = assignment(host, host.alpha)
    assert empty == {"revision": 0, "item": None, "item_key": None, "updated_at": None, "reviewed_at": None, "stale": False}
    assert table(host, "agent_current_work") == []                       # reading created nothing
    launch, review = task(host, host.first, "Launch page"), task(host, host.second, "Review copy")

    first = ok(update(host, session.ctx, 0, session=session, items=[item(host.first, launch["id"])]))
    assert (first["work_revision"], first["agent_id"], first["session_id"]) == (1, host.alpha, session.id)
    current = assignment(host, host.alpha)
    assert current["revision"] == 1 and current["stale"] is False and current["item_key"] and len(current["item_key"]) == 16
    assert (current["item"]["task_id"], current["item"]["title"], current["item"]["summary"]) == (launch["id"], "Launch page", "")
    assert following(host, host.first, launch["id"], host.coordinator)    # the configured Ordubashy follows the newly taken Task

    # The same Task with added text is the same assignment; another Task is a switch and needs replace_current.
    ok(update(host, session.ctx, 1, session=session, items=[item(host.first, launch["id"], "Drafting the hero")]))
    switch = update(host, session.ctx, 2, session=session, items=[item(host.second, review["id"])])
    assert code(switch) == "replace_current_required" and assignment(host, host.alpha)["item"]["task_id"] == launch["id"]
    ok(update(host, session.ctx, 2, session=session, items=[item(host.second, review["id"])], replace_current=True))
    assert assignment(host, host.alpha)["item"]["task_id"] == review["id"]
    assert status(host, host.first, launch["id"]) == status(host, host.second, review["id"]) == "open"   # nothing was completed
    assert following(host, host.first, launch["id"], host.coordinator) and following(host, host.second, review["id"], host.coordinator)

    # A text-only assignment replaces a Task reference the same way; clearing needs no flag and keeps every follow.
    assert code(update(host, session.ctx, 3, session=session, items=[item(None, None, "Waiting for the owner")])) == "replace_current_required"
    ok(update(host, session.ctx, 3, session=session, items=[item(None, None, "Waiting for the owner")], replace_current=True))
    cleared = ok(update(host, session.ctx, 4, session=session, items=[]))
    after = assignment(host, host.alpha)
    assert cleared["work_revision"] == 5 and after["revision"] == 5 and after["item"] is None and after["item_key"] is None
    assert after["updated_at"] and following(host, host.second, review["id"], host.coordinator)

    # A restart is a new native session; reads, the sampler and the old session list change nothing.
    restarted = open_session(host, host.alpha)
    host.runtime.activity.sample()
    again = roster(host, host.owner)[host.alpha]
    assert again["current_work"] == after and all(view["items"] == [] for view in again["sessions"])
    assert len(table(host, "agent_current_work")) == 1


def test_text_forms_and_the_hundred_word_limit(host):
    session = open_session(host, host.alpha)
    launch = task(host, host.first, "Launch page")
    words = lambda count, word="w": " ".join([word] * count)            # noqa: E731
    revision = 0

    def write(*items, expect=None, **extra):
        nonlocal revision
        result = update(host, session.ctx, revision, session=session, items=list(items), replace_current=True, **extra)
        if expect:
            assert code(result) == expect
            return result
        revision = ok(result)["work_revision"]
        return result

    write(item(host.first, launch["id"]))                                          # task only
    write(item(None, None, "Only text"))                                            # text only
    write(item(host.first, launch["id"], "Both"))                                   # both
    write(item(host.first, launch["id"], "  Padded text  "))
    assert assignment(host, host.alpha)["item"]["summary"] == "Padded text"
    write(item(host.first, None, words(60), words(40, "why")))                      # exactly 100 words, summary and reason together
    write(item(host.first, None, words(60), words(41, "why")), expect="validation_error")
    write(item(host.first, None, "tab\tand\nnewline " + words(97)))                 # any whitespace separates words: 100
    write(item(host.first, None, "tab\tand\nnewline " + words(98)), expect="validation_error")
    write(item(host.first, None, words(90, "longerword")))                           # 989 characters: no old 300-character cap for a worker
    write(item(host.first), expect="validation_error")                              # neither a Task nor text
    write(item(host.first, None, "  \n "), expect="validation_error")
    write(item(host.first, None, None, "Waiting on legal"))                         # a blocked reason is text
    write(item(host.first, "missing-task"), expect="not_found")
    write(item(host.first, None, "one"), item(host.first, None, "two"), expect="validation_error")  # at most one item


def test_revisions_and_native_sessions_guard_every_write(host):
    old, new = open_session(host, host.alpha), open_session(host, host.alpha)
    ok(update(host, old.ctx, 0, session=old, items=[item(None, None, "First")]))
    # Two writers with the same revision: only the first wins, and the loser learns the current revision.
    stale = update(host, old.ctx, 0, session=old, items=[item(None, None, "Second")])
    assert code(stale) == "revision_conflict" and stale.error["current_revision"] == 1
    assert assignment(host, host.alpha)["item"]["summary"] == "First"
    # A worker writes from its own session only, and a session another one superseded is refused.
    assert code(update(host, old.ctx, 1, items=[])) == "validation_error"
    assert code(update(host, old.ctx, 1, session=SimpleNamespace(id=new_id()), items=[])) == "not_found"
    foreign = open_session(host, host.beta)
    assert code(update(host, old.ctx, 1, session=foreign, items=[])) == "not_found"
    assert code(update(host, agent(host, host.alpha, registration=foreign.registration), 1, session=old, items=[])) == "forbidden"
    own_owner(host, new)
    assert code(update(host, old.ctx, 1, session=old, items=[])) == "inbox_session_superseded"
    ok(update(host, new.ctx, 1, session=new, items=[item(None, None, "From the current session")]))
    assert assignment(host, host.alpha)["revision"] == 2


def test_the_owner_or_ordubashy_updates_a_worker_without_a_session_and_nobody_else_does(host):
    session = open_session(host, host.alpha)
    ordubashy = agent(host, host.coordinator)
    before = [dict(row) for row in table(host, "agent_activity")]
    managed = ok(update(host, ordubashy, 0, agent_id=host.alpha, items=[item(None, None, "Set by Ordubashy")]))
    assert (managed["agent_id"], managed["session_id"], managed["work_revision"]) == (host.alpha, None, 1)
    assert [dict(row) for row in table(host, "agent_activity")] == before         # no worker session was impersonated
    ok(update(host, host.owner, 1, agent_id=host.alpha, items=[item(None, None, "Set by the owner")]))
    ok(update(host, session.ctx, 2, session=session, agent_id=host.alpha, items=[item(None, None, "By itself")]))
    assert assignment(host, host.alpha)["item"]["summary"] == "By itself"

    assert code(update(host, ordubashy, 3, agent_id=host.alpha, session=session, items=[])) == "validation_error"
    other = open_session(host, host.beta)
    for ctx in (other.ctx, agent(host, host.applier)):                              # no worker or agent administers a worker
        assert code(update(host, ctx, 3, agent_id=host.alpha, items=[item(None, None, "Hijack")])) == "forbidden"
    assert code(update(host, ordubashy, 0, agent_id=host.applier, items=[item(None, None, "Not a worker")])) == "forbidden"
    assert code(update(host, host.owner, 0, items=[item(None, None, "No target")])) == "forbidden"
    assert assignment(host, host.alpha)["item"]["summary"] == "By itself"


def test_two_workers_keep_independent_assignments_and_restart_persistence(host):
    alpha, beta = open_session(host, host.alpha), open_session(host, host.beta)
    ok(update(host, alpha.ctx, 0, session=alpha, items=[item(None, None, "Alpha work")]))
    ok(update(host, beta.ctx, 0, session=beta, items=[item(None, None, "Beta work", "Beta blocked")]))
    ok(update(host, alpha.ctx, 1, session=alpha, items=[]))
    team = roster(host, host.owner)
    assert (team[host.alpha]["current_work"]["revision"], team[host.alpha]["current_work"]["item"]) == (2, None)
    assert (team[host.beta]["current_work"]["revision"], team[host.beta]["current_work"]["item"]["summary"]) == (1, "Beta work")
    assert team[host.beta]["current_work"]["item"]["blocked"] is True
    assert {row["agent_id"] for row in table(host, "agent_current_work")} == {host.alpha, host.beta}
    # The non-worker row has no assignment at all, and its legacy session contract is exactly as before.
    applier = open_session(host, host.applier)
    assert "current_work" not in team[host.applier] and team[host.applier]["sessions"] == []
    assert code(update(host, applier.ctx, 0, session=applier, items=[item(host.first, None, "x")])) == "revision_conflict"
    assert code(update(host, applier.ctx, 1, items=[item(host.first, None, "x")])) == "validation_error"
    assert code(update(host, applier.ctx, 1, session=applier, items=[item(host.first, None, "x" * 301)])) == "validation_error"
    ok(update(host, applier.ctx, 1, session=applier, items=[item(host.first, None, "x" * 300)]))
    legacy = roster(host, host.owner)[host.applier]
    assert [view["items"][0]["summary"] for view in legacy["sessions"]] == ["x" * 300] and "current_work" not in legacy
    assert code(update(host, applier.ctx, 2, session=applier, agent_id=host.alpha, items=[])) == "forbidden"


# -- readers ---------------------------------------------------------------------------------------------------------------

def test_authored_text_is_the_teams_but_an_unreadable_task_stays_withheld(host):
    secret = task(host, host.first, SECRET)
    session = open_session(host, host.alpha)
    ok(update(host, session.ctx, 0, session=session, items=[item(host.first, secret["id"], "Public status", "Waiting on legal")]))
    set_agent_overrides(host.instance, host.beta, {"tasks_get": "deny"}, [host.first, host.second])
    readers = {"owner": host.owner, "ordubashy": agent(host, host.coordinator), "denied worker": agent(host, host.beta)}
    for name, reader in readers.items():
        data = ok(host.instance.invoke(reader, "agents_activity_list", {"scope": "global"}))
        view = {row["agent_id"]: row for row in data["agents"]}[host.alpha]["current_work"]["item"]
        assert (view["summary"], view["blocked_reason"], view["blocked"]) == ("Public status", "Waiting on legal", True), name
        if name == "denied worker":
            assert (view["title"], view["task_id"], view["business_id"], view["task_status"]) == (None, None, None, None)
            assert SECRET not in json.dumps(data)
        else:
            assert (view["title"], view["task_id"], view["business_id"]) == (SECRET, secret["id"], host.first)
    # The team consumers keep the status too: Ordubashy's startup summary, the business briefing and the compact MCP view.
    principal = Principal(host.coordinator, "agent")
    startup = host.runtime.activity.startup(principal, host.runtime.resolver.resolve_agent_config(principal).data)
    assert "Public status" in startup and host.alpha in startup
    briefed = ok(host.instance.invoke(host.instance.human(host.first), "business_briefing", {}))["agents"]["items"]
    assert [row["agent_id"] for row in briefed] == [host.alpha] and briefed[0]["work"] == ["Public status (blocked)"]
    assert "current_work" in ACTIVITY_AGENT_FIELDS
    listing = ok(host.instance.invoke(host.owner, "agents_activity_list", {"scope": "global"}))
    compact = {row["agent_id"]: row for row in _activity_summary(listing)["agents"]}[host.alpha]["current_work"]
    assert compact["item_key"] and compact["item"]["summary"] == "Public status" and compact["revision"] == 1

    # A business briefing is one business's: another business's worker (its source is redacted for this reader) and a worker on
    # no business are team status, never this business's work, while the global roster keeps their public descriptions.
    def briefed_work(business):
        rows = ok(host.instance.invoke(host.instance.human(business), "business_briefing", {}))["agents"]["items"]
        return {row["agent_id"]: row["work"] for row in rows}

    def team_summary():
        return host.runtime.activity.startup(principal, host.runtime.resolver.resolve_agent_config(principal).data)

    elsewhere = task(host, host.second, "Second business plan")
    ok(update(host, host.owner, 0, agent_id=host.beta, items=[item(host.second, elsewhere["id"], "Second business status")]))
    assert briefed_work(host.first) == {host.alpha: ["Public status (blocked)"]}
    assert briefed_work(host.second) == {host.beta: ["Second business status"]}
    assert assignment(host, host.beta)["item"]["summary"] == "Second business status"
    ok(update(host, host.owner, 1, agent_id=host.beta, replace_current=True, items=[item(None, None, "Global review")]))
    assert briefed_work(host.first) == {host.alpha: ["Public status (blocked)"]} and briefed_work(host.second) == {}
    assert assignment(host, host.beta)["item"]["summary"] == "Global review" and "Global review" in team_summary()

    # A description made only of a blocked reason is still the description, never "Task in progress".
    ok(update(host, host.owner, 2, agent_id=host.beta, items=[item(host.first, None, None, "Waiting on the lawyer")]))
    assert briefed_work(host.first)[host.beta] == ["Waiting on the lawyer (blocked)"]
    assert "Waiting on the lawyer (blocked)" in team_summary() and "Task in progress" not in team_summary()


def test_an_assignment_goes_stale_two_hours_after_it_was_last_reviewed(host):
    session = open_session(host, host.alpha)
    ok(update(host, session.ctx, 0, session=session, items=[item(None, None, "Long task")]))
    assert assignment(host, host.alpha)["stale"] is False
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_current_work SET work_reviewed_at='2026-01-01T00:00:00+00:00' WHERE agent_id=?", (host.alpha,))
    assert assignment(host, host.alpha)["stale"] is True
    ok(update(host, session.ctx, 1, session=session, items=[item(None, None, "Long task")]))   # an unchanged update is a review
    assert assignment(host, host.alpha)["stale"] is False


def test_a_patch_edits_the_one_item_by_its_current_key(host):
    session = open_session(host, host.alpha)
    ok(update(host, session.ctx, 0, session=session, patch=[{"action": "add", "item": item(None, None, "Added")}]))
    key = assignment(host, host.alpha)["item_key"]
    assert code(update(host, session.ctx, 1, session=session, patch=[{"action": "add", "item": item(None, None, "Second")}])) == "validation_error"
    assert code(update(host, session.ctx, 1, session=session, patch=[{"action": "replace", "item_key": "0" * 16,
                                                                       "item": item(None, None, "x")}])) == "work_item_unavailable"
    ok(update(host, session.ctx, 1, session=session, patch=[{"action": "replace", "item_key": key, "item": item(None, None, "Edited")}]))
    assert assignment(host, host.alpha)["item"]["summary"] == "Edited"
    assert code(update(host, session.ctx, 2, session=session, patch=[{"action": "remove", "item_key": key}])) == "work_item_unavailable"
    ok(update(host, agent(host, host.coordinator), 2, agent_id=host.alpha,
              patch=[{"action": "remove", "item_key": assignment(host, host.alpha)["item_key"]}]))
    assert assignment(host, host.alpha)["item"] is None


# -- subscriptions ---------------------------------------------------------------------------------------------------------

def subscribe(host, ctx, **arguments):
    return host.instance.invoke(ctx, "inbox_subscribe", arguments)


def listed(host, ctx, **arguments):
    return ok(host.instance.invoke(ctx, "inbox_subscriptions_list", arguments))["items"]


def test_the_owner_or_ordubashy_manages_a_workers_task_and_action_follows_as_that_worker(host):
    ordubashy, global_ordubashy = agent(host, host.coordinator, host.first), agent(host, host.coordinator)
    watched = task(host, host.first, "Watched")
    action = ok(host.instance.invoke(host.owner, "actions_record", {"text": "Published the spring update", "category": "global-operational"}))["action"]
    assert ok(subscribe(host, ordubashy, agent_id=host.alpha, source_type="tasks", source_id=watched["id"]))["following"] is True
    # The source is authorized as the worker: the real worker role holds no Action read, so the manager's request is refused until
    # the owner explicitly grants it.
    assert code(subscribe(host, host.owner, agent_id=host.alpha, source_type="actions", source_id=action["action_id"])) == "not_found"
    set_agent_overrides(host.instance, host.alpha, {**GRANTS, "actions_get": "allow"}, [host.first, host.second])
    assert ok(subscribe(host, host.owner, agent_id=host.alpha, source_type="actions", source_id=action["action_id"]))["following"] is True
    assert following(host, host.first, watched["id"], host.alpha) and not following(host, host.first, watched["id"], host.beta)

    for reader in (agent(host, host.alpha), global_ordubashy, host.owner):
        rows = listed(host, reader, **({} if reader is not global_ordubashy and reader is not host.owner else {"agent_id": host.alpha}))
        assert {(row["source_type"], row["source_id"]) for row in rows} == {("tasks", watched["id"]), ("actions", action["action_id"])}
        assert {row["agent_id"] for row in rows} == {host.alpha}
    assert listed(host, agent(host, host.beta)) == []

    # Removal goes through the same sticky unfollow, by source; one selector only.
    for arguments in ({}, {"subscription_id": new_id(), "source_type": "tasks", "source_id": watched["id"]}, {"source_type": "tasks"}):
        assert code(host.instance.invoke(ordubashy, "inbox_unsubscribe", {"agent_id": host.alpha, **arguments})) == "validation_error"
    ok(host.instance.invoke(ordubashy, "inbox_unsubscribe", {"agent_id": host.alpha, "source_type": "tasks", "source_id": watched["id"]}))
    ok(host.instance.invoke(global_ordubashy, "inbox_unsubscribe",
                            {"agent_id": host.alpha, "source_type": "actions", "source_id": action["action_id"]}))
    assert not following(host, host.first, watched["id"], host.alpha) and listed(host, global_ordubashy, agent_id=host.alpha) == []


def test_a_subscription_row_is_listed_and_removed_for_the_target_only(host):
    ordubashy = agent(host, host.coordinator, host.first)
    campaign = new_id()
    created = ok(subscribe(host, ordubashy, agent_id=host.alpha, source_type="outreach_events", source_id=campaign))["subscription_id"]
    assert [(row["id"], row["agent_id"]) for row in listed(host, ordubashy, agent_id=host.alpha)] == [(created, host.alpha)]
    assert listed(host, ordubashy) == []                                            # Ordubashy's own list is unchanged
    own = ok(subscribe(host, agent(host, host.beta, host.first), source_type="outreach_events", source_id=campaign))["subscription_id"]
    # A row is removed for its own agent: the manager cannot reach another agent's row through a target.
    assert code(host.instance.invoke(ordubashy, "inbox_unsubscribe", {"agent_id": host.alpha, "subscription_id": own})) == "not_found"
    ok(host.instance.invoke(ordubashy, "inbox_unsubscribe", {"agent_id": host.alpha, "subscription_id": created}))
    assert [row["agent_id"] for row in table(host, "inbox_subscriptions")] == [host.beta]


def test_sources_are_authorized_as_the_worker_and_no_worker_administers_another(host):
    ordubashy = agent(host, host.coordinator, host.first)
    watched = task(host, host.first, "Watched")
    set_agent_overrides(host.instance, host.beta, {"tasks_get": "deny"}, [host.first, host.second])
    refused = subscribe(host, ordubashy, agent_id=host.beta, source_type="tasks", source_id=watched["id"])
    assert code(refused) == "not_found" and not following(host, host.first, watched["id"], host.beta)   # Ordubashy could read it
    # Only the owner or Ordubashy target a worker: not a peer, not itself via a peer, not a non-worker, and no non-worker target.
    for ctx, target in ((agent(host, host.alpha, host.first), host.beta), (agent(host, host.beta, host.first), host.alpha),
                        (agent(host, host.applier, host.first), host.alpha), (ordubashy, host.applier), (ordubashy, new_id())):
        assert code(subscribe(host, ctx, agent_id=target, source_type="tasks", source_id=watched["id"])) == "forbidden"
        assert code(host.instance.invoke(ctx, "inbox_subscriptions_list", {"agent_id": target})) == "forbidden"
        assert code(host.instance.invoke(ctx, "inbox_unsubscribe", {"agent_id": target, "source_type": "tasks",
                                                                     "source_id": watched["id"]})) == "forbidden"
    ok(subscribe(host, agent(host, host.alpha, host.first), agent_id=host.alpha, source_type="tasks", source_id=watched["id"]))
    assert following(host, host.first, watched["id"], host.alpha)                   # naming itself is just the default
    # Acknowledgement is only ever the agent's own.
    assert code(host.instance.invoke(ordubashy, "inbox_acknowledge", {"agent_id": host.alpha, "event_ids": [1]})) == "validation_error"


def test_removing_a_follow_never_removes_mandatory_delivery(host):
    ordubashy = agent(host, host.coordinator, host.first)
    assigned = task(host, host.first, "Assigned to alpha", assignee_ids=[host.alpha])
    ok(subscribe(host, ordubashy, agent_id=host.alpha, source_type="tasks", source_id=assigned["id"]))
    ok(host.instance.invoke(ordubashy, "inbox_unsubscribe", {"agent_id": host.alpha, "source_type": "tasks", "source_id": assigned["id"]}))
    assert not following(host, host.first, assigned["id"], host.alpha)
    ok(host.runtime.application.invoke(host.instance.human(host.first), "tasks_comment", {"task_id": assigned["id"], "text": "Please look"},
                                       InvocationOptions(idempotency_key=new_id(), expected_revision=assigned["revision"])))
    pending = ok(host.instance.invoke(agent(host, host.alpha, host.first), "inbox_list", {}))["items"]
    assert any(row["source_id"] == assigned["id"] for row in pending)               # the assignee still gets the required update


def test_ordubashy_follows_a_new_task_only_where_he_may_read_it(host):
    session = open_session(host, host.alpha)
    set_agent_overrides(host.instance, host.coordinator, {"tasks_get": "deny"}, [host.first, host.second])
    hidden = task(host, host.first, "Hidden from Ordubashy")
    ok(update(host, session.ctx, 0, session=session, items=[item(host.first, hidden["id"])]))
    assert not following(host, host.first, hidden["id"], host.coordinator)          # the write is not blocked by his access


# -- the Stop checks -------------------------------------------------------------------------------------------------------

def native_state(host, identity, session, turn, root, dirty):
    """The hook's small protected observation of the session's turn (the checkpoint's only evidence of it)."""
    path = agent_workspace_of(host.runtime.paths, identity, None, None) / ".bazarlyk/activity" / (session.id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agent_id": identity, "registration_id": session.registration, "session_id": session.id,
                                "runtime": "codex", "state": "idle",
                                "checkpoint": {"protocol": 1, "turn_id": turn, "root_turn_id": root, "dirty_sequence": dirty}}))


def stop(host, session, turn, correct=True):
    return host.runtime.checkpoints.check(session.ctx, session.registration, session.id, turn,
                                          allow_correction=correct, claim_id=new_id() if correct else None)


def test_the_stop_check_uses_the_workers_assignment_and_corrects_once_per_episode(host):
    session = open_session(host, host.alpha)
    root, turn = new_id(), new_id()
    native_state(host, host.alpha, session, turn, root, 3)
    finished = task(host, host.first, "Finished soon")
    # Work was done this turn and the assignment was not reviewed: one review correction, never repeated.
    first = stop(host, session, turn)
    assert [issue["code"] for issue in first["issues"]] == ["work_review_missing"] and first["correction"]
    assert stop(host, session, turn)["correction"] is None
    # A manager's update is not the worker's review; the worker's own is.
    ok(update(host, agent(host, host.coordinator), 0, agent_id=host.alpha, items=[item(host.first, finished["id"])]))
    assert [issue["code"] for issue in stop(host, session, turn)["issues"]] == ["work_review_missing"]
    ok(update(host, session.ctx, 1, session=session, items=[item(host.first, finished["id"])]))
    assert stop(host, session, turn)["issues"] == []
    # The Task finishes while it is still the assignment: one correction naming it, then it resolves once the assignment is cleared.
    ok(host.runtime.application.invoke(host.instance.human(host.first), "tasks_complete", {"task_id": finished["id"]},
                                       InvocationOptions(idempotency_key=new_id(), expected_revision=finished["revision"])))
    done = stop(host, session, turn)
    assert [issue["code"] for issue in done["issues"]] == ["finished_work_listed"] and done["correction"]
    assert stop(host, session, turn)["correction"] is None
    ok(update(host, session.ctx, 2, session=session, items=[]))
    assert stop(host, session, turn)["issues"] == [] and stop(host, session, turn)["outstanding_count"] == 0


def test_a_non_workers_stop_check_still_reads_its_session_list(host):
    set_agent_overrides(host.instance, host.applier, GRANTS, [host.first, host.second])
    session = open_session(host, host.applier)
    root, turn = new_id(), new_id()
    native_state(host, host.applier, session, turn, root, 0)
    finished = task(host, host.first, "Applier task")
    ok(update(host, session.ctx, 1, session=session, items=[item(host.first, finished["id"], "Applier work")]))
    ok(host.runtime.application.invoke(host.instance.human(host.first), "tasks_complete", {"task_id": finished["id"]},
                                       InvocationOptions(idempotency_key=new_id(), expected_revision=finished["revision"])))
    assert [issue["code"] for issue in stop(host, session, turn)["issues"]] == ["finished_work_listed"]
    assert table(host, "agent_current_work") == []
