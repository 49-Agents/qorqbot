"""Waiting native inputs of a recipient that stops qualifying are settled as undelivered (Manager assignment 2463).

Contract: ``NativeBackend.run_once`` settles every ``waiting`` input whose registered recipient is no longer an operating
supported global, although the observer no longer sees that recipient. The pass is maintenance, never delivery: nothing is
probed, bound, typed or retargeted for it, its native session and final capture are untouched, only ``waiting`` rows move
(to ``undelivered`` with ``agent_inactive``), each input keeps its identity, session, source, type, body and attribution,
and inbox events stay pending. Eligibility is checked again under the agent's lock in the write transaction, and closed
writes settle nothing. Startup recovery still makes a claimed input ``uncertain``.

Real: the stock host assembly and the native Chat graph of ``native_chat_support`` with its scripted pane. Synthetic: the tmp
instance, the stored-row edits and the directly inserted inputs. No agent, terminal client, model, provider or live data."""

import pytest

from qorqut_operations.contracts import DomainError, new_id, utc_now

from bazarlyk.native_chat import store
from bazarlyk.native_chat.contracts import (STATE_FAILED, STATE_SUBMITTED, STATE_SUBMITTING, STATE_UNCERTAIN, STATE_WAITING,
                                            TYPE_DIRECT, InputRequest)
from native_chat_support import Native, rows

IDENTITY = ("id", "agent_id", "session_id", "origin", "input_type", "source_key", "attempt", "request_hash", "principal_id",
            "body", "sensitive", "steer", "marker", "metadata_json", "sequence", "created_at")


def cohorts(native):
    """Each way the registered recipient stops being an operating supported global."""
    agent = native.agent
    return {
        "principal_inactive": ("UPDATE principals SET active=0 WHERE id=?", (agent,)),
        "row_retired": ("UPDATE agent_profiles SET status='retired' WHERE agent_id=?", (agent,)),
        "profile_inactive": ("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.status','inactive') WHERE agent_id=?",
                             (agent,)),
        "unsupported_role": ("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.role.id','business-agent') "
                             "WHERE agent_id=?", (agent,)),
        "business_home": ("UPDATE agent_profiles SET home_business_id=?, namespace=? WHERE agent_id=?",
                          (native.instance.business, native.instance.business, agent)),
    }


def disqualify(native, name):
    sql, parameters = cohorts(native)[name]
    with native.runtime.database.transaction() as uow:
        uow.connection.execute(sql, parameters)


def requalify(native):
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (native.agent,))


def inputs(native):
    return {row["id"]: row for row in rows(native.runtime, "SELECT * FROM native_inputs WHERE agent_id=?", (native.agent,))}


def events(native):
    return rows(native.runtime, "SELECT input_id, state, reason FROM native_input_events ORDER BY rowid")


def session_row(native):
    return rows(native.runtime, "SELECT * FROM native_sessions WHERE agent_id=?", (native.agent,))


def watched(native):
    """Record every probe, binding and adapter call the backend makes, by agent."""
    calls = []
    probe, reconcile = native.probe.observe, native.registry.reconcile
    native.probe.observe = lambda profile: (calls.append(("probe", profile["agent_id"])), probe(profile))[1]
    native.registry.reconcile = lambda profile, observed: (calls.append(("reconcile", profile["agent_id"])),
                                                            reconcile(profile, observed))[1]
    for adapter in native.arbiter.adapters.values():
        submit = adapter.submit
        adapter.submit = lambda profile, *args, _submit=submit, **kwargs: (calls.append(("submit", profile["agent_id"])),
                                                                          _submit(profile, *args, **kwargs))[1]
    return calls


def pending_event(native):
    """One unacknowledged inbox delivery for the recipient: the obligation a nudge only announces."""
    with native.runtime.database.transaction() as uow:
        event = uow.connection.execute(
            "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
            "VALUES(NULL,'tasks',?,?,'created','Pending work',?)", (new_id(), native.owner, utc_now())).lastrowid
        uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", (native.agent, event))
    return event


def admitted(native):
    """A waiting Chat input and a waiting inbox nudge, both admitted while the recipient qualified."""
    native.publish()
    pin = native.pin()
    chat = native.chat.send(native.owner, native.agent, "Please summarize", "chat-key-000001", pin)["input"]
    nudge = native.arbiter.admit_nudge(native.agent, "nudge-key-000001", "Inbox has new work.", wait=True, expected_binding=pin,
                                       metadata={"nudge_kind": "inbox"})
    assert chat["state"] == STATE_WAITING and nudge.state == STATE_WAITING
    return pin


@pytest.mark.parametrize("name", ["principal_inactive", "row_retired", "profile_inactive", "unsupported_role", "business_home"])
def test_waiting_inputs_of_a_recipient_that_stops_qualifying_become_undelivered_and_nothing_else_moves(tmp_path, name):
    native = Native(tmp_path)
    admitted(native)
    event = pending_event(native)
    native.backend.undeliver_ineligible()                         # a qualifying recipient keeps its queue
    before, session = inputs(native), session_row(native)
    assert {row["state"] for row in before.values()} == {STATE_WAITING}
    disqualify(native, name)
    with native.runtime.database.read() as uow:
        assert native.agent not in {row["agent_id"] for row in store.observed_agents(uow.connection, native.runtime.paths)}
    calls = watched(native)
    native.pass_()
    after = inputs(native)
    assert {(row["state"], row["reason"]) for row in after.values()} == {("undelivered", "agent_inactive")}
    for identity, row in after.items():
        assert {key: row[key] for key in IDENTITY} == {key: before[identity][key] for key in IDENTITY}
    assert {row["body"] for row in after.values()} == {"Please summarize", "Inbox has new work."}
    assert calls == [] and native.pane.typed() == [] and native.queued == []
    # Maintenance only: the session, its final capture and the inbox obligation stay exactly as they were.
    assert session_row(native) == session
    assert rows(native.runtime, "SELECT acknowledged_at, outcome FROM inbox_deliveries WHERE agent_id=? AND event_id=?",
                (native.agent, event)) == [{"acknowledged_at": None, "outcome": None}]
    settled = events(native)
    native.pass_(2)                                               # idempotent
    assert inputs(native) == after and events(native) == settled and session_row(native) == session
    # The settled inputs stay readable as history.
    with native.runtime.database.read() as uow:
        assert {row["id"] for row in store.recent_inputs(uow.connection, native.agent)} == set(before)


def test_eligibility_is_rechecked_under_the_agent_lock_so_a_restored_recipient_keeps_its_queue(tmp_path):
    native = Native(tmp_path)
    admitted(native)
    disqualify(native, "principal_inactive")
    locks = native.backend.locks

    def restored_meanwhile(agent_id):
        requalify(native)                                          # the recipient qualifies again before the write
        return locks(agent_id)

    native.backend.locks = restored_meanwhile
    native.backend.undeliver_ineligible()
    assert {row["state"] for row in inputs(native).values()} == {STATE_WAITING}


def test_only_waiting_inputs_move_and_startup_recovery_keeps_its_own_outcome(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.pin()
    session = native.registry.current(native.agent)

    def insert(key, *moves):
        request = InputRequest(native.agent, TYPE_DIRECT, key, f"Text {key}")
        with native.runtime.database.transaction() as uow:
            row = store.insert_input(uow.connection, session, request, store.request_hash(request), state=STATE_WAITING)
            states = (STATE_WAITING, *moves)
            for previous, state in zip(states, states[1:]):
                assert store.settle_input(uow.connection, row["id"], state, "synthetic", {}, (previous,))
        return row["id"]

    ids = {"submitted": insert("submitted-key", STATE_SUBMITTING, STATE_SUBMITTED),
           "uncertain": insert("uncertain-key", STATE_SUBMITTING, STATE_UNCERTAIN),
           "failed": insert("failed-key", STATE_SUBMITTING, STATE_FAILED),
           "submitting": insert("submitting-key", STATE_SUBMITTING),
           "waiting": insert("waiting-key")}
    before = inputs(native)
    disqualify(native, "unsupported_role")
    native.build()                                                  # a restarted backend: startup recovery runs first
    native.pass_()
    after = inputs(native)
    assert {name: (after[identity]["state"], after[identity]["reason"]) for name, identity in ids.items()} == {
        "submitted": ("submitted", "synthetic"), "uncertain": ("uncertain", "synthetic"), "failed": ("failed", "synthetic"),
        "submitting": ("uncertain", "service_restarted"), "waiting": ("undelivered", "agent_inactive")}
    for name in ("submitted", "uncertain", "failed"):
        assert after[ids[name]] == before[ids[name]]
    assert native.pane.typed() == [] and native.queued == []


def test_closed_writes_settle_nothing_until_they_reopen(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.pin()
    native.pass_()                                                  # startup recovery done while writes are open
    sent = native.chat.send(native.owner, native.agent, "Please summarize", "chat-key-000001", native.pin())["input"]
    assert sent["state"] == STATE_WAITING
    disqualify(native, "row_retired")

    def maintenance(state):
        with native.runtime.database.transaction() as uow:
            uow.connection.execute("INSERT INTO maintenance_state(id,state,created_at,updated_at) VALUES('service',?,?,?) "
                                   "ON CONFLICT(id) DO UPDATE SET state=excluded.state", (state, utc_now(), utc_now()))

    maintenance("closed")
    for run in (native.pass_, native.backend.undeliver_ineligible):   # the pass, and its own write gate
        with pytest.raises(DomainError) as paused:
            run()
        assert paused.value.code == "maintenance"
    assert [(row["state"], row["reason"]) for row in inputs(native).values()] == [(STATE_WAITING, None)]
    maintenance("open")
    native.pass_()
    assert [(row["state"], row["reason"]) for row in inputs(native).values()] == [("undelivered", "agent_inactive")]
    assert native.pane.typed() == []
