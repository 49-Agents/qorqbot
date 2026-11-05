"""Browser turns admit only current active Global agents; a synthetic store, no browser, model or service."""

from datetime import timedelta
import json

import pytest

from bazarlyk.browser.turns import BrowserTurnQueue
from qorqut_operations.contracts import DomainError, new_id
from browser_turn_support import setup_store


GLOBAL_ROLES = ("ordubashy", "global-worker", "learning-applier")
BLOCKED_REASON = "requesting agent is not an active Global agent"


def _queue(start):
    """A queue whose clock advances one millisecond per read, so FIFO order follows creation, never random IDs."""
    state = {"at": start}

    def clock():
        state["at"] += timedelta(milliseconds=1)
        return state["at"].isoformat(timespec="microseconds").replace("+00:00", "Z")
    return BrowserTurnQueue(clock=clock)


def _principal(uow, kind, now, *, active=1):
    identity = new_id()
    uow.repository("principals").insert({"id": identity, "kind": kind, "display_name": kind + "-" + identity[:8],
        "active": active, "created_at": now, "updated_at": now})
    return identity


def _profile(role="ordubashy", *, status="active", scope=None):
    return {"status": status, "role": {"id": role, "version": "1.0.0"},
            "scope": scope or {"kind": "ecosystem", "global_capabilities": ["read_global_context"]}}


def _agent(uow, tmp_path, now, profile=None, *, home=None, row_status="active", active=1):
    identity = _principal(uow, "agent", now, active=active)
    stored = profile if isinstance(profile, str) else json.dumps(profile or _profile())
    uow.connection.execute(
        "INSERT INTO agent_profiles(agent_id,principal_id,home_business_id,namespace,name,workspace,status,"
        "profile_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (identity, identity, home, home or "ecosystem", identity, str(tmp_path / identity), row_status, stored,
         now, now))
    return identity


def _ineligible(uow, tmp_path, now, business_id):
    """Each cohort the current Global predicate must refuse, by label; row status alone looks active in most."""
    return {
        "business home": _agent(uow, tmp_path, now, home=business_id),
        "unsupported role": _agent(uow, tmp_path, now, _profile("business-agent")),
        "retired Founder role": _agent(uow, tmp_path, now, _profile("founder")),
        "inactive principal": _agent(uow, tmp_path, now, active=0),
        "inactive profile row": _agent(uow, tmp_path, now, row_status="inactive"),
        "retired profile row": _agent(uow, tmp_path, now, row_status="retired"),
        "profile status not active": _agent(uow, tmp_path, now, _profile(status="retired")),
        "business scope id": _agent(uow, tmp_path, now,
                                    _profile(scope={"kind": "ecosystem", "business_id": business_id})),
        "profile not an object": _agent(uow, tmp_path, now, "[]"),
        "role not an object": _agent(uow, tmp_path, now,
                                     {"status": "active", "role": "ordubashy", "scope": {"kind": "ecosystem"}}),
        "scope not an object": _agent(uow, tmp_path, now,
                                      {"status": "active", "role": {"id": "ordubashy"}, "scope": "ecosystem"}),
    }


def _agent_turn(queue, uow, key, business_id, agent, *, coordinator=False):
    requester = f"coordinator:{agent}:{new_id()}" if coordinator else "native:" + agent
    return queue.acquire(uow, browser_key=key, business_id=business_id, parent_principal_id=agent,
                         requester_kind="agent", requester_id=requester), requester


def _owner_turn(queue, uow, key, business_id, owner):
    return queue.acquire(uow, browser_key=key, business_id=business_id, parent_principal_id=owner,
                         requester_kind="owner", requester_id=owner)


def _row(uow, table, identity, column="id"):
    found = uow.connection.execute(f"SELECT * FROM {table} WHERE {column}=?", (identity,)).fetchone()
    return None if found is None else dict(found)


def _assignment(database, store, parent_id, business_id, title):
    with database.transaction() as uow:
        saved = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id, "record_ids": {}, "source_ids": {}},
            objective=title, plan={"steps": ["open", "verify"]},
            allowed_operations=["browser_use"], resource_bounds={"max_turns": 3, "max_runtime_seconds": 120})
        return store.claim(uow, saved["id"], actor_principal_id=parent_id, dispatcher_ready=True)


def _blocked(uow, turn_id):
    row = _row(uow, "browser_turns", turn_id)
    return (row["state"] == "blocked" and row["reason"] == BLOCKED_REASON and row["lease_id"] is None
            and row["lease_until"] is None and row["released_at"] is not None and row["revision"] == 2)


def test_queued_ineligible_agents_are_blocked_and_the_next_global_promotes(tmp_path):
    database, store, _parent, business_id, start = setup_store(tmp_path)
    queue, now, key = _queue(start), store.clock(), "business:" + business_id
    with database.transaction() as uow:
        owner = _principal(uow, "human", now)
        cohorts = _ineligible(uow, tmp_path, now, business_id)
        eligible = _agent(uow, tmp_path, now, _profile("global-worker"))
        held = _owner_turn(queue, uow, key, business_id, owner)
        queued = {label: _agent_turn(queue, uow, key, business_id, agent, coordinator=index % 2 == 1)[0]
                  for index, (label, agent) in enumerate(cohorts.items())}
        following, _ = _agent_turn(queue, uow, key, business_id, eligible)
        assert held["state"] == "owning"
        assert {turn["state"] for turn in queued.values()} == {"queued"} and following["state"] == "queued"
        queue.release(uow, held["id"], parent_principal_id=owner, requester_id=owner)
        blocked = {label: _blocked(uow, turn["id"]) for label, turn in queued.items()}
        promoted = _row(uow, "browser_turns", following["id"])
        actions = uow.connection.execute("SELECT count(*) FROM browser_turn_actions").fetchone()[0]
    assert blocked == {label: True for label in cohorts}
    assert promoted["state"] == "owning" and promoted["lease_id"] is not None and promoted["reason"] is None
    assert actions == 0


def test_an_ineligible_agent_on_a_free_browser_is_blocked_at_once(tmp_path):
    database, store, _parent, business_id, start = setup_store(tmp_path)
    queue, now = _queue(start), store.clock()
    with database.transaction() as uow:
        cohorts = _ineligible(uow, tmp_path, now, business_id)
        results = {}
        for index, (label, agent) in enumerate(cohorts.items()):
            turn, _ = _agent_turn(queue, uow, f"free-{index}", business_id, agent)
            results[label] = (turn["state"], turn["lease_id"], turn["reason"])
        eligible = _agent(uow, tmp_path, now, _profile("global-worker"))
        after, _ = _agent_turn(queue, uow, "free-0", business_id, eligible)
        owners = uow.connection.execute("SELECT count(*) FROM browser_turns WHERE state='owning'").fetchone()[0]
    assert results == {label: ("blocked", None, BLOCKED_REASON) for label in cohorts}
    assert after["state"] == "owning" and owners == 1


def test_every_active_global_role_still_acquires_and_acts(tmp_path):
    database, store, _parent, business_id, start = setup_store(tmp_path)
    queue, now = _queue(start), store.clock()
    with database.transaction() as uow:
        settled = {}
        for index, role in enumerate(GLOBAL_ROLES):
            agent = _agent(uow, tmp_path, now, _profile(role))
            turn, requester = _agent_turn(queue, uow, f"global-{role}", business_id, agent, coordinator=index % 2 == 0)
            assert turn["state"] == "owning"
            action_id = new_id()
            intent, fresh = queue.begin_action(uow, turn["id"], parent_principal_id=agent, requester_id=requester,
                generation=None, lease_id=turn["lease_id"], action_id=action_id, command=["snapshot"])
            assert fresh and intent["state"] == "pending"
            settled[role] = queue.settle_action(uow, turn["id"], parent_principal_id=agent, requester_id=requester,
                generation=None, lease_id=turn["lease_id"], action_id=action_id, state="succeeded",
                outcome={"exit_code": 0})["state"]
    assert settled == {role: "succeeded" for role in GLOBAL_ROLES}


def test_eligibility_lost_while_queued_blocks_only_that_entry(tmp_path):
    database, store, _parent, business_id, start = setup_store(tmp_path)
    queue, now, key = _queue(start), store.clock(), "business:" + business_id
    with database.transaction() as uow:
        owner = _principal(uow, "human", now)
        lost, kept = _agent(uow, tmp_path, now, _profile("ordubashy")), _agent(uow, tmp_path, now, _profile("global-worker"))
        held = _owner_turn(queue, uow, key, business_id, owner)
        first, _ = _agent_turn(queue, uow, key, business_id, lost, coordinator=True)
        second, _ = _agent_turn(queue, uow, key, business_id, kept)
        assert first["state"] == second["state"] == "queued"
        uow.connection.execute("UPDATE principals SET active=0,updated_at=? WHERE id=?", (now, lost))
        queue.release(uow, held["id"], parent_principal_id=owner, requester_id=owner)
        assert _blocked(uow, first["id"])
        assert _row(uow, "browser_turns", second["id"])["state"] == "owning"


def _lose(uow, agent, mode, business_id, now):
    profile = json.loads(_row(uow, "agent_profiles", agent, "agent_id")["profile_json"])
    if mode == "principal inactive":
        uow.connection.execute("UPDATE principals SET active=0,updated_at=? WHERE id=?", (now, agent))
    elif mode == "profile row inactive":
        uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (agent,))
    elif mode == "business home":
        uow.connection.execute("UPDATE agent_profiles SET home_business_id=?,namespace=? WHERE agent_id=?",
                               (business_id, business_id, agent))
    else:
        changed = {"profile status retired": {**profile, "status": "retired"},
                   "role unsupported": {**profile, "role": {"id": "business-agent", "version": "1.0.0"}},
                   "business scope id": {**profile, "scope": {**profile["scope"], "business_id": business_id}}}[mode]
        uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (json.dumps(changed), agent))


LOSS_MODES = ("principal inactive", "profile row inactive", "business home", "profile status retired",
              "role unsupported", "business scope id")


def test_eligibility_lost_after_acquisition_refuses_new_actions_and_replays_recorded_ones(tmp_path):
    database, store, _parent, business_id, start = setup_store(tmp_path)
    queue, now = _queue(start), store.clock()
    with database.transaction() as uow:
        outcomes = {}
        for index, mode in enumerate(LOSS_MODES):
            agent = _agent(uow, tmp_path, now, _profile(GLOBAL_ROLES[index % len(GLOBAL_ROLES)]))
            turn, requester = _agent_turn(queue, uow, f"lost-{index}", business_id, agent, coordinator=index % 2 == 1)
            handle = {"parent_principal_id": agent, "requester_id": requester, "generation": None,
                      "lease_id": turn["lease_id"]}
            recorded = new_id()
            queue.begin_action(uow, turn["id"], **handle, action_id=recorded, command=["snapshot"])
            queue.settle_action(uow, turn["id"], **handle, action_id=recorded, state="succeeded", outcome={"exit_code": 0})
            _lose(uow, agent, mode, business_id, now)
            refused = new_id()
            with pytest.raises(DomainError) as error:
                queue.begin_action(uow, turn["id"], **handle, action_id=refused, command=["snapshot"])
            replay, fresh = queue.begin_action(uow, turn["id"], **handle, action_id=recorded, command=["snapshot"])
            current = _row(uow, "browser_turns", turn["id"])
            outcomes[mode] = (error.value.code, _row(uow, "browser_turn_actions", refused, "action_id"),
                              current["state"], current["lease_id"] == turn["lease_id"], current["command_id"],
                              fresh, replay["state"])
    assert outcomes == {mode: ("forbidden", None, "owning", True, None, False, "succeeded") for mode in LOSS_MODES}


def test_owner_and_assignment_queue_paths_are_unchanged(tmp_path):
    database, store, parent_id, business_id, start = setup_store(tmp_path)
    queue, now, key = _queue(start), store.clock(), "business:" + business_id
    # The assignment parent from setup_store has no Global role: the assignment contract never consulted the predicate.
    first = _assignment(database, store, parent_id, business_id, "First browser task")
    second = _assignment(database, store, parent_id, business_id, "Second browser task")
    with database.transaction() as uow:
        owner = _principal(uow, "human", now)
        retired = _agent(uow, tmp_path, now, _profile("business-agent"))
        worker = queue.acquire(uow, browser_key=key, business_id=business_id, parent_principal_id=parent_id,
            assignment_id=first["id"], generation=first["execution_generation"])
        blocked_agent, _ = _agent_turn(queue, uow, key, business_id, retired)
        waiting_owner = _owner_turn(queue, uow, key, business_id, owner)
        assert worker["state"] == "owning" and waiting_owner["state"] == "queued"
        queue.release(uow, worker["id"], parent_principal_id=parent_id, requester_id=first["id"],
            generation=first["execution_generation"])
        assert _blocked(uow, blocked_agent["id"])
        assert _row(uow, "browser_turns", waiting_owner["id"])["state"] == "owning"
        queued_worker = queue.acquire(uow, browser_key=key, business_id=business_id, parent_principal_id=parent_id,
            assignment_id=second["id"], generation=second["execution_generation"])
        assert queued_worker["state"] == "queued"
        queue.release(uow, waiting_owner["id"], parent_principal_id=owner, requester_id=owner)
        assert _row(uow, "browser_turns", queued_worker["id"])["state"] == "owning"


def test_unsettled_agent_action_reconciliation_is_unchanged_after_eligibility_loss(tmp_path):
    database, store, _parent, business_id, start = setup_store(tmp_path)
    queue, now = _queue(start), store.clock()
    with database.transaction() as uow:
        coordinated = _agent(uow, tmp_path, now, _profile("ordubashy"))
        native = _agent(uow, tmp_path, now, _profile("global-worker"))
        waiting_retired = _agent(uow, tmp_path, now, _profile("business-agent"))
        waiting_global = _agent(uow, tmp_path, now, _profile("learning-applier"))
        turns = {}
        for name, agent, coordinator in (("coordinated", coordinated, True), ("native", native, False)):
            turn, requester = _agent_turn(queue, uow, "reconcile-" + name, business_id, agent, coordinator=coordinator)
            handle = {"parent_principal_id": agent, "requester_id": requester, "generation": None,
                      "lease_id": turn["lease_id"]}
            action_id = new_id()
            queue.begin_action(uow, turn["id"], **handle, action_id=action_id, command=["snapshot"])
            queue.settle_action(uow, turn["id"], **handle, action_id=action_id, state="uncertain",
                outcome={"error": "connection lost"}, error="connection lost")
            turns[name] = (turn, handle, action_id)
        blocked_agent, _ = _agent_turn(queue, uow, "reconcile-coordinated", business_id, waiting_retired)
        following, _ = _agent_turn(queue, uow, "reconcile-coordinated", business_id, waiting_global)
        for agent in (coordinated, native):
            uow.connection.execute("UPDATE principals SET active=0,updated_at=? WHERE id=?", (now, agent))

        turn, handle, action_id = turns["coordinated"]
        # An unsettled owning turn is neither released nor transferred because its agent lost eligibility.
        with pytest.raises(DomainError) as busy:
            queue.release(uow, turn["id"], parent_principal_id=handle["parent_principal_id"],
                          requester_id=handle["requester_id"])
        assert busy.value.code == "browser_busy"
        assert _row(uow, "browser_turns", turn["id"])["command_id"] == action_id
        reconciled = queue.reconcile_action(uow, turn["id"], **handle, action_id=action_id,
                                            outcome={"status": "reconciled"})
        assert reconciled["state"] == "reconciled"
        assert _row(uow, "browser_turns", turn["id"])["state"] == "released"
        assert _blocked(uow, blocked_agent["id"])
        assert _row(uow, "browser_turns", following["id"])["state"] == "owning"

        turn, handle, action_id = turns["native"]
        with pytest.raises(DomainError) as native_error:
            queue.reconcile_action(uow, turn["id"], **handle, action_id=action_id, outcome={"status": "reconciled"})
        assert native_error.value.code == "browser_reconcile"
        native_turn = _row(uow, "browser_turns", turn["id"])
        assert native_turn["state"] == "owning" and native_turn["command_id"] == action_id
        assert _row(uow, "browser_turn_actions", action_id, "action_id")["state"] == "uncertain"
