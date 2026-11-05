"""The durable Context input queue: admission, one order for GENERAL and Action inputs, controls and driver transitions."""

from __future__ import annotations

import pytest

from operational_support import (AGENT, AGENT2, BUSINESS, COORDINATOR, HUMAN, OTHER, OTHER_AGENT, OperationalHost, WORKER)
from qorqut_context.integration import queue as context_queue
from qorqut_context.integration.queue import transitions
from qorqut_context.integration.queue.queue import RESERVED_PREFIX, general_arguments
from qorqut_operations.contracts import DomainError

META = {"kind": "note", "title": "A note", "origin": "agent"}


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def submit(host, path="foundation/notes/a.md", ctx=None, content="Body", operation="add", **extra):
    arguments = {"operation": operation, "path": path, **({"content": content, "metadata": META} if operation != "remove" else {}), **extra}
    return host.data("context_queue_submit", arguments, ctx or host.agent())["input"]


def drive(host, name, *, input_id, **kwargs):
    if name in ("record_published", "record_discarded"):
        kwargs.setdefault("outcomes", host.outcomes)
    with host.database.transaction() as uow:
        return getattr(transitions, name)(uow, driver_id=kwargs.pop("driver_id", WORKER), input_id=input_id, **kwargs)


def row(host, input_id):
    return host.query("SELECT * FROM context_inputs WHERE id=?", input_id)[0]


def test_a_general_source_is_admitted_with_its_owner_and_submitter_and_never_waits(host):
    queued = submit(host)
    assert queued["state"] == "queued" and queued["input_kind"] == "general" and queued["scope"] == {"kind": "business", "business_id": BUSINESS}
    assert queued["submitted_by"] == AGENT and queued["attempt"] == 0 and queued["context_event_id"] is None and queued["summary"] == {"path": "foundation/notes/a.md"}
    stored = row(host, queued["id"])
    assert stored["business_id"] == BUSINESS and stored["payload_json"] == {
        "operation": "add", "path": "foundation/notes/a.md", "content": "Body", "metadata": META, "submitted_by": AGENT}
    removal = submit(host, "foundation/notes/a.md", operation="remove")
    assert row(host, removal["id"])["payload_json"] == {"operation": "remove", "path": "foundation/notes/a.md", "content": None,
                                                        "metadata": None, "submitted_by": AGENT}
    assert removal["sequence"] > queued["sequence"]
    glob = submit(host, ctx=host.owner(None))
    assert glob["scope"] == {"kind": "global"} and row(host, glob["id"])["business_id"] is None


def test_source_admission_validates_the_envelope_and_reserves_the_action_namespace(host):
    bad = ({"operation": "add", "path": RESERVED_PREFIX + "x.md", "content": "c", "metadata": META},
           {"operation": "add", "path": "foundation/a.md", "content": "c", "metadata": {**META, "kind": "accepted_action"}},
           {"operation": "add", "path": "foundation/a.md", "content": "c", "metadata": {**META, "origin": "controlled_caller"}},
           {"operation": "add", "path": "foundation/a.md", "content": "c"},
           {"operation": "add", "path": "foundation/a.md", "content": "c", "metadata": {**META, "extra": "x"}},
           {"operation": "remove", "path": "foundation/a.md", "content": "c"},
           {"operation": "add", "path": "foundation/../etc/a.md", "content": "c", "metadata": META},
           {"operation": "add", "path": "elsewhere/a.md", "content": "c", "metadata": META},
           {"operation": "add", "path": "foundation/a.py", "content": "c", "metadata": META},
           {"operation": "edit", "path": "foundation/.git/a.md", "content": "c"})
    for arguments in bad:
        assert host.error("context_queue_submit", arguments, host.agent()) in {"validation_error"}, arguments
    assert host.count("context_inputs") == 0
    with pytest.raises(DomainError):
        general_arguments("add", "foundation/a.md", "x" * 11, META, 10)
    assert general_arguments("edit", "foundation/a.md", "ok", None, 10)["metadata"] is None


def test_one_monotonic_order_covers_general_and_action_inputs(host):
    first = submit(host)
    action = host.record_action("An action in the middle")
    second = submit(host, "foundation/notes/b.md")
    other = host.record_action("Another namespace", ctx=host.agent(OTHER_AGENT, OTHER))
    sequence = [item["sequence"] for item in host.query("SELECT sequence FROM context_inputs ORDER BY sequence")]
    assert sequence == sorted(set(sequence)) and len(sequence) == 4
    kinds = [item["input_kind"] for item in host.query("SELECT input_kind FROM context_inputs ORDER BY sequence")]
    assert kinds == ["general", "action", "general", "action"]
    head = drive_head(host)
    assert head["id"] == first["id"]                               # the oldest unfinished input, across every namespace
    with pytest.raises(DomainError):
        drive(host, "start_attempt", input_id=second["id"], context_event_id="ab12c")      # only the oldest may start
    del action, other


def drive_head(host):
    with host.database.read() as uow:
        return transitions.head(uow, driver_id=WORKER)


def test_reads_are_bound_to_the_callers_namespace(host):
    mine = submit(host)
    theirs = submit(host, ctx=host.agent(OTHER_AGENT, OTHER))
    glob = submit(host, ctx=host.owner(None))
    assert [item["id"] for item in host.read("context_queue_list", {}, host.agent())["items"]] == [mine["id"]]
    assert [item["id"] for item in host.read("context_queue_list", {}, host.agent(OTHER_AGENT, OTHER))["items"]] == [theirs["id"]]
    assert [item["id"] for item in host.read("context_queue_list", {}, host.owner(None))["items"]] == [glob["id"]]
    assert host.error("context_queue_get", {"input_id": theirs["id"]}, host.agent()) == "not_found"
    assert host.error("context_queue_get", {"input_id": glob["id"]}, host.agent()) == "not_found"
    detail = host.read("context_queue_get", {"input_id": mine["id"]}, host.agent())["input"]
    assert detail["payload"]["path"] == "foundation/notes/a.md" and detail["attempts"] == [] and detail["controls"] == []


def test_queue_blocked_is_the_only_cross_namespace_fact(host):
    mine = submit(host)
    theirs = submit(host, ctx=host.agent(OTHER_AGENT, OTHER))
    assert host.read("context_queue_list", {}, host.agent())["queue_blocked"] is False
    drive(host, "start_attempt", input_id=mine["id"], context_event_id="ab12c")
    drive(host, "record_blocked", input_id=mine["id"], error={"code": "context_failed", "message": "Context refused"}, ordubashy_id=COORDINATOR)
    own = host.read("context_queue_list", {}, host.agent())
    other = host.read("context_queue_list", {}, host.agent(OTHER_AGENT, OTHER))
    assert own["queue_blocked"] and own["blocked_input_id"] == mine["id"]
    assert other["queue_blocked"] and other["blocked_input_id"] is None and [item["id"] for item in other["items"]] == [theirs["id"]]
    # Reads do not leak the other namespace's state, and controls cannot reach across.
    assert host.error("context_queue_retry", {"input_id": mine["id"], "expected_state": "blocked"}, host.agent(OTHER_AGENT, OTHER)) == "not_found"


def test_driver_transitions_are_system_only_and_keep_the_donor_states(host):
    first = submit(host)
    for name, kwargs in (("start_attempt", {"context_event_id": "ab12c"}),
                         ("record_blocked", {"error": {"code": "x", "message": "y"}, "ordubashy_id": COORDINATOR})):
        for impostor in (AGENT, HUMAN, COORDINATOR, "missing"):
            with pytest.raises(DomainError) as refused:
                drive(host, name, input_id=first["id"], driver_id=impostor, **kwargs)
            assert refused.value.code == "forbidden"
    started = drive(host, "start_attempt", input_id=first["id"], context_event_id="ab12c")
    assert started["state"] == "processing" and started["attempt"] == 1 and started["context_event_id"] == "ab12c"
    assert started["attempts"][0]["cause"] == "queue" and started["attempts"][0]["outcome"] is None
    with pytest.raises(DomainError):
        drive(host, "start_attempt", input_id=first["id"], context_event_id="zz99z")             # already processing
    published = drive(host, "record_published", input_id=first["id"], ordubashy_id=COORDINATOR,
                      result={"context_event_id": "ab12c", "published_commit": "c0ffee"},
                      handoff_note={"disposition": "prepared", "text": "Please look at the new note"})
    assert published["state"] == "published" and published["result"]["published_commit"] == "c0ffee"
    assert published["attempts"][0]["outcome"] == "published"
    notice = [(r["agent_id"], r["event_type"], r["required"], r["business_id"]) for r in host.deliveries(source_type="context_inputs")]
    assert notice == [(COORDINATOR, "context.handoff_published", 1, None)]
    with pytest.raises(Exception):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE context_inputs SET state='queued' WHERE id=?", (first["id"],))        # final
    with pytest.raises(Exception):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE context_inputs SET payload_json='{}' WHERE id=?", (first["id"],))      # immutable payload


def test_event_ids_are_five_lowercase_characters_and_never_reissued(host):
    first, second = submit(host), submit(host, "foundation/notes/b.md")
    for bad in ("AB12C", "ab12", "ab12cd", "ab-2c", ""):
        with pytest.raises(Exception):
            drive(host, "start_attempt", input_id=first["id"], context_event_id=bad)
    drive(host, "start_attempt", input_id=first["id"], context_event_id="ab12c")
    drive(host, "record_published", input_id=first["id"], ordubashy_id=COORDINATOR, result={"context_event_id": "ab12c", "published_commit": "c0ffee"},
          handoff_note={"disposition": "empty", "text": ""})
    assert host.deliveries(source_type="context_inputs") == []              # an explicit empty handoff notifies nobody
    with pytest.raises(DomainError) as collision:
        drive(host, "start_attempt", input_id=second["id"], context_event_id="ab12c")
    assert collision.value.code == "id_collision"
    with host.database.read() as uow:
        assert transitions.used_event_ids(uow, driver_id=WORKER) == {"ab12c"}
    with pytest.raises(DomainError):
        drive(host, "record_published", input_id=second["id"], ordubashy_id=COORDINATOR, result={"context_event_id": "ab12c", "published_commit": "c"},
              handoff_note={"disposition": "empty", "text": ""})


def test_a_blocked_head_notifies_once_and_restarts_only_after_a_retry_request(host):
    first, second = submit(host), submit(host, "foundation/notes/b.md")
    drive(host, "start_attempt", input_id=first["id"], context_event_id="ab12c")
    blocked = drive(host, "record_blocked", input_id=first["id"], error={"code": "context_failed", "message": "refused", "retryable": True},
                    ordubashy_id=COORDINATOR)
    assert blocked["state"] == "blocked" and blocked["error"]["code"] == "context_failed" and blocked["attempts"][0]["outcome"] == "blocked"
    assert [(r["event_type"], r["required"]) for r in host.deliveries(COORDINATOR, source_type="context_inputs")] == [("context.input_blocked", 1)]
    with pytest.raises(DomainError):
        drive(host, "record_blocked", input_id=first["id"], error={"code": "x", "message": "y"}, ordubashy_id=COORDINATOR)   # not processing
    with pytest.raises(DomainError):
        drive(host, "start_attempt", input_id=first["id"], context_event_id="de34f")           # no retry request yet
    with pytest.raises(DomainError):
        drive(host, "start_attempt", input_id=second["id"], context_event_id="de34f")          # later inputs wait behind the block
    retried = host.data("context_queue_retry", {"input_id": first["id"], "expected_state": "blocked", "rationale": "Context is back"}, host.agent())["result"]
    assert retried == {"input_id": first["id"], "control_id": retried["control_id"], "intent": "retry", "status": "pending", "state": "blocked"}
    assert host.error("context_queue_retry", {"input_id": first["id"], "expected_state": "blocked"}, host.agent()) == "control_pending"
    again = drive(host, "start_attempt", input_id=first["id"], context_event_id="de34f")
    assert again["attempt"] == 2 and again["context_event_id"] == "de34f" and again["attempts"][1]["cause"] == "retry"
    assert again["controls"][0]["status"] == "applied" and again["attempts"][0]["context_event_id"] == "ab12c"       # histories are kept


PREFLIGHT = {"code": "context_unreadable", "message": "Context could not be checked", "retryable": True, "phase": "preflight"}


def test_a_preflight_failure_blocks_a_first_queued_head_without_an_event_id(host):
    first, second = submit(host), submit(host, "foundation/notes/b.md")
    for impostor in (AGENT, HUMAN, COORDINATOR, "missing"):
        with pytest.raises(DomainError) as refused:
            drive(host, "record_preflight_failure", input_id=first["id"], driver_id=impostor, error=PREFLIGHT, ordubashy_id=COORDINATOR)
        assert refused.value.code == "forbidden"
    with pytest.raises(DomainError) as later:
        drive(host, "record_preflight_failure", input_id=second["id"], error=PREFLIGHT, ordubashy_id=COORDINATOR)       # not the oldest
    assert later.value.code == "invalid_transition"
    with pytest.raises(DomainError):
        drive(host, "record_preflight_failure", input_id=first["id"], error=PREFLIGHT, ordubashy_id=AGENT)               # not the coordinator
    assert row(host, first["id"])["state"] == "queued" and host.deliveries(source_type="context_inputs") == []

    blocked = drive(host, "record_preflight_failure", input_id=first["id"], error=PREFLIGHT, ordubashy_id=COORDINATOR)
    assert blocked["state"] == "blocked" and blocked["attempt"] == 1 and blocked["context_event_id"] is None
    assert blocked["error"]["code"] == "context_unreadable" and blocked["error"]["details"] == {"phase": "preflight"}
    attempt = blocked["attempts"][0]
    assert (attempt["cause"], attempt["context_event_id"], attempt["outcome"], attempt["control_id"]) == ("queue", None, "blocked", None)
    assert attempt["finished_at"] and attempt["error"]["code"] == "context_unreadable"
    notices = host.query("SELECT event_type, summary FROM inbox_events WHERE source_type='context_inputs'")
    assert [n["event_type"] for n in notices] == ["context.input_blocked"] and "(event" not in notices[0]["summary"]
    with host.database.read() as uow:
        assert transitions.used_event_ids(uow, driver_id=WORKER) == set()                  # no event ID was manufactured
    with pytest.raises(DomainError):
        drive(host, "start_attempt", input_id=second["id"], context_event_id="ab12c")      # later inputs wait behind the block
    with pytest.raises(DomainError):
        drive(host, "record_preflight_failure", input_id=first["id"], error=PREFLIGHT, ordubashy_id=COORDINATOR)       # needs a retry
    assert len(host.query("SELECT 1 FROM inbox_events WHERE source_type='context_inputs'")) == 1


def test_a_blocked_head_retries_into_a_preflight_failure_keeping_any_prior_event_id(host):
    item = submit(host)
    retry = lambda: host.data("context_queue_retry", {"input_id": item["id"], "expected_state": "blocked"}, host.agent())
    drive(host, "record_preflight_failure", input_id=item["id"], error=PREFLIGHT, ordubashy_id=COORDINATOR)
    retry()
    again = drive(host, "record_preflight_failure", input_id=item["id"], error=PREFLIGHT, ordubashy_id=COORDINATOR)
    assert again["state"] == "blocked" and again["attempt"] == 2 and again["context_event_id"] is None
    assert [(a["cause"], a["outcome"], a["context_event_id"]) for a in again["attempts"]] == [("queue", "blocked", None), ("retry", "blocked", None)]
    assert again["controls"][0]["status"] == "applied" and again["controls"][0]["result"] == {"outcome": "preflight_failed", "attempt": 2}

    retry()                                                                                 # an attempt that did allocate an event ID
    drive(host, "start_attempt", input_id=item["id"], context_event_id="ab12c")
    drive(host, "record_blocked", input_id=item["id"], error={"code": "x", "message": "y"}, ordubashy_id=COORDINATOR)
    retry()
    kept = drive(host, "record_preflight_failure", input_id=item["id"], error=PREFLIGHT, ordubashy_id=COORDINATOR)
    assert kept["attempt"] == 4 and kept["context_event_id"] == "ab12c"                     # the prior ID is preserved, never replaced
    assert kept["attempts"][3]["cause"] == "retry" and kept["attempts"][3]["context_event_id"] == "ab12c"
    summaries = [n["summary"] for n in host.query("SELECT summary FROM inbox_events WHERE source_type='context_inputs' ORDER BY id")]
    assert len(summaries) == 4 and "(event" not in summaries[0] and "(event ab12c)" in summaries[3]       # one notice per attempt
    with host.database.read() as uow:
        assert transitions.used_event_ids(uow, driver_id=WORKER) == {"ab12c"}
    retry()
    drive(host, "start_attempt", input_id=item["id"], context_event_id="ab12c")
    with pytest.raises(DomainError) as processing:                                          # a processing head is not preflighted
        drive(host, "record_preflight_failure", input_id=item["id"], error=PREFLIGHT, ordubashy_id=COORDINATOR)
    assert processing.value.code == "invalid_transition"


def test_a_first_preflight_failure_can_be_discarded_without_any_event(host):
    item, later = submit(host), submit(host, "foundation/notes/b.md")
    drive(host, "record_preflight_failure", input_id=item["id"], error=PREFLIGHT, ordubashy_id=COORDINATOR)
    requested = host.data("context_queue_discard", {"input_id": item["id"], "expected_state": "blocked"}, host.agent())["result"]
    assert requested["status"] == "pending" and requested["state"] == "discard_requested"
    closed = drive(host, "record_discarded", input_id=item["id"], result={"context_event_id": None, "context": "no_event"})
    assert closed["state"] == "discarded" and closed["controls"][0]["status"] == "applied" and closed["context_event_id"] is None
    assert drive(host, "start_attempt", input_id=later["id"], context_event_id="ab12c")["state"] == "processing"


def test_discard_controls_follow_the_input_state(host):
    queued = submit(host)
    unstarted = host.data("context_queue_discard", {"input_id": queued["id"], "expected_state": "queued"}, host.agent())["result"]
    assert unstarted["status"] == "applied" and unstarted["state"] == "discarded"
    final = row(host, queued["id"])
    assert final["state"] == "discarded" and final["result_json"]["reason"] == "never_started"
    assert host.error("context_queue_discard", {"input_id": queued["id"], "expected_state": "discarded"}, host.agent()) == "invalid_transition"
    blocked_input = submit(host, "foundation/notes/b.md")
    drive(host, "start_attempt", input_id=blocked_input["id"], context_event_id="ab12c")
    assert host.error("context_queue_discard", {"input_id": blocked_input["id"], "expected_state": "processing"}, host.agent()) == "invalid_transition"
    drive(host, "record_blocked", input_id=blocked_input["id"], error={"code": "x", "message": "y"}, ordubashy_id=COORDINATOR)
    assert host.error("context_queue_discard", {"input_id": blocked_input["id"], "expected_state": "queued"}, host.agent()) == "state_conflict"
    requested = host.data("context_queue_discard", {"input_id": blocked_input["id"], "expected_state": "blocked"}, host.agent())["result"]
    assert requested["status"] == "pending" and requested["state"] == "discard_requested"
    drive(host, "record_discard_failure", input_id=blocked_input["id"], error={"code": "context_down", "message": "try later"})
    closed = drive(host, "record_discarded", input_id=blocked_input["id"], result={"mechanical": True})
    assert closed["state"] == "discarded" and closed["controls"][0]["status"] == "applied" and closed["attempts"][0]["outcome"] == "blocked"


def test_a_discard_that_finds_the_event_published_closes_as_published(host):
    item = submit(host)
    drive(host, "start_attempt", input_id=item["id"], context_event_id="ab12c")
    drive(host, "record_blocked", input_id=item["id"], error={"code": "x", "message": "y"}, ordubashy_id=COORDINATOR)
    host.data("context_queue_discard", {"input_id": item["id"], "expected_state": "blocked"}, host.agent())
    done = drive(host, "record_published", input_id=item["id"], ordubashy_id=COORDINATOR,
                 result={"context_event_id": "ab12c", "published_commit": "c0ffee"}, handoff_note={"disposition": "empty", "text": ""})
    assert done["state"] == "published" and done["controls"][0]["status"] == "rejected" and done["controls"][0]["result"]["outcome"] == "already_published"


def test_registered_operations_are_the_only_public_surface_and_policy_applies(host):
    names = {entry.name for entry in context_queue.QUEUE_ENTRIES}
    assert names == {"context_queue_list", "context_queue_get", "context_queue_submit", "context_queue_retry", "context_queue_discard"}
    host.agents.overrides["context_queue_submit"] = "signoff"
    signoff_id = host.stage("context_queue_submit", {"operation": "add", "path": "foundation/notes/p.md", "content": "Staged", "metadata": META})
    assert host.count("context_inputs") == 0
    assert host.apply_approved(signoff_id)["state"] == "applied"
    [queued] = host.query("SELECT * FROM context_inputs")
    assert queued["state"] == "queued" and queued["submitted_by"] == AGENT and queued["payload_json"]["content"] == "Staged"
    host.agents.overrides["context_queue_discard"] = "deny"
    assert host.error("context_queue_discard", {"input_id": queued["id"], "expected_state": "queued"}, host.agent()) in {"forbidden", "policy_denied"}


def test_an_action_input_is_immutable_attributed_and_owned_by_the_action_scope(host):
    action = host.record_action("Queued evidence")
    [queued] = host.query("SELECT * FROM context_inputs")
    assert queued["business_id"] == BUSINESS and queued["submitted_by"] == AGENT and queued["item_id"] == action["action_id"]
    visible = host.read("context_queue_list", {}, host.agent())["items"]
    assert visible[0]["summary"] == {"action_ref": action["action_id"], "revision_ref": action["action_id"] + ":1"}
    assert host.read("context_queue_list", {}, host.agent(OTHER_AGENT, OTHER))["items"] == []
    assert host.error("context_queue_get", {"input_id": queued["id"]}, host.agent(OTHER_AGENT, OTHER)) == "not_found"
    with pytest.raises(Exception):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE context_inputs SET business_id=NULL WHERE id=?", (queued["id"],))
    with pytest.raises(Exception):          # an input points only at an existing accepted revision in its own scope
        with host.database.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO context_inputs(id,business_id,input_kind,item_id,revision,operation,payload_json,submitted_by,created_at) "
                "VALUES('11111111-2222-4333-8444-555555555555',?,'action','business/x/y',1,'add','{}',?,'now')", (OTHER, AGENT))
    assert context_queue.OPERATIONS == ("add", "edit", "remove")


def test_an_action_input_must_match_its_stored_revision_in_ownership_operation_and_submitter(host, monkeypatch):
    def tampered(change):
        def enqueue(uow, *, action_id, revision, operation, payload):
            stored = uow.connection.execute("SELECT * FROM action_revisions WHERE action_key=? AND revision=?", (action_id, revision)).fetchone()
            fields = {"business_id": stored["business_id"], "operation": operation, "submitted_by": stored["recorded_by"], **change}
            uow.connection.execute(
                "INSERT INTO context_inputs(id,business_id,input_kind,item_id,revision,operation,payload_json,submitted_by,created_at) "
                "VALUES('11111111-2222-4333-8444-555555555556',?,'action',?,?,?,'{}',?,'now')",
                (fields["business_id"], action_id, revision, fields["operation"], fields["submitted_by"]))
            return "11111111-2222-4333-8444-555555555556"
        return enqueue

    for change in ({"business_id": OTHER}, {"submitted_by": AGENT2}, {"operation": "edit"}):
        monkeypatch.setattr("qorqut_context.integration.queue.queue.enqueue_action", tampered(change))
        try:
            result = host.invoke("actions_record", {"text": "Forged ownership"}, host.agent())
            assert result.status == "error", change
        except Exception:
            pass
        assert host.count("context_inputs") == 0 and host.count("action_revisions") == 0, change
    monkeypatch.setattr("qorqut_context.integration.queue.queue.enqueue_action", tampered({}))
    assert host.invoke("actions_record", {"text": "Matching input"}, host.agent()).status == "ok"
    assert host.count("context_inputs") == 1
