"""Controlled graph edits as metadata of the one Context queue, over the synthetic Operations host (the Operations engine is the host here; the queue is Context's).

Everything real is the engine: the queue, its ``transitions`` and public operations, the ``context_graph_edits`` contribution
and its database invariants, and the prepared-plan records that apply an edit. The only test code is the stand-in for the
host adapter (``prepare_edit``): a prepared operation that calls ``admit`` with a plan and its canonical text. No Context
library, model or provider is involved; the engine never interprets the plan."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from operational_support import AGENT, BUSINESS, COORDINATOR, OTHER, OperationalHost, WORKER
from qorqut_context.integration import graph_edits as edits
from qorqut_operations.application.plans import PlanBuilder, apply_records
from qorqut_context.integration.filing import FilingOutcomes
from qorqut_context.integration.queue import transitions
from qorqut_operations.contracts import DomainError, new_id

BASE = "a" * 40
EVENT = "ed1t5"
META = {"kind": "note", "title": "A note", "origin": "agent"}
PLAN = {"format": "qorqut-graph-edit-v1", "expected_revision": BASE, "rationale": "Fix a label",
        "operations": [{"op": "remove_node", "id": "abcde", "explanation": "Obsolete", "citations": ["self"]}]}
TARGETS = [{"kind": "node", "id": "abcde", "operation": "remove", "ownership": {"state": "unknown"}}]
ATTRIBUTION = {"principal_id": AGENT, "principal_kind": "agent", "delegation": None}
BINDING, ASSIGNMENT = "00000000-0000-4000-8000-0000000000b1", "00000000-0000-4000-8000-0000000000a1"
TRIPLET = {"binding_id": BINDING, "assignment_id": ASSIGNMENT, "generation": 3}


@pytest.fixture
def host(tmp_path):
    return OperationalHost(tmp_path)


def text_of(plan) -> str:
    """The canonical text a host would supply (the Context library's encoding is two-space indented, sorted, newline-ended)."""
    return json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def digest_of(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def prepare_edit(host, *, plan=PLAN, ctx=None, event=EVENT, content=None, digest=None, attribution=None, targets=TARGETS,
                 business_id="same", limit=None, apply=True):
    ctx = ctx or host.agent()
    content = text_of(plan) if content is None else content
    digest = digest_of(content) if digest is None else digest
    attribution = attribution or {"principal_id": ctx.principal_id, "principal_kind": ctx.principal.kind,
                                  "delegation": edits.delegation_of(ctx)}
    with host.database.read() as uow:
        builder = PlanBuilder(ctx, "edit_test", {}, "Graph edit", clock=host.clock)
        admission = edits.admit(builder, uow, plan=plan, content=content, plan_digest=digest, reserved_event_id=event,
                                attribution=attribution, targets=targets,
                                business_id=ctx.business_id if business_id == "same" else business_id, max_content_bytes=limit)
        change_set = builder.build()
    if apply:
        with host.database.transaction() as uow:
            apply_records(ctx, change_set, uow)
    return admission, change_set


def drive(host, name, input_id, **kwargs):
    if name in ("record_published", "record_discarded"):
        kwargs["outcomes"] = host.outcomes
    with host.database.transaction() as uow:
        return getattr(transitions, name)(uow, driver_id=WORKER, input_id=input_id, **kwargs)


def load(host, input_id):
    with host.database.read() as uow:
        return edits.load(uow, input_id)


def counts(host):
    return host.count("context_graph_edits"), host.count("context_inputs")


@contextlib.contextmanager
def rejected(message=""):
    """The storage layer maps a database refusal to a DomainError; its cause is the database's own message."""
    with pytest.raises(DomainError) as error:
        yield
    cause = error.value.__cause__
    assert isinstance(cause, sqlite3.IntegrityError) and message in str(cause), repr(error.value)


def refused(host, sql, parameters=(), message="Controlled graph-edit paths"):
    with rejected(message):
        with host.database.transaction() as uow:
            uow.connection.execute(sql, parameters)


def raw_input(input_id, payload, *, operation="add", submitted_by=AGENT, kind="general"):
    return ("INSERT INTO context_inputs(id,business_id,input_kind,operation,payload_json,submitted_by,created_at) VALUES (?,?,?,?,?,?,?)",
            (input_id, BUSINESS, kind, operation, json.dumps(payload), submitted_by, "now"))


def edit_payload(event=EVENT, **changes):
    return {"operation": "add", "path": f"foundation/edits/{event}.txt", "content": text_of(PLAN),
            "metadata": dict(edits.METADATA), "submitted_by": AGENT, **changes}


def raw_binding(input_id, event=EVENT, **changes):
    row = {"input_id": input_id, "reserved_event_id": event, "plan_digest": digest_of(text_of(PLAN)), "expected_revision": BASE,
           "plan_json": json.dumps(PLAN), "attribution_json": json.dumps(ATTRIBUTION),
           "targets_json": json.dumps(TARGETS), **changes}
    return ("INSERT INTO context_graph_edits(" + ",".join(row) + ") VALUES (" + ",".join("?" * len(row)) + ")", tuple(row.values()))


def raw_pair(host, input_id, *, binding=None, payload=None, **input_options):
    """Insert a raw binding and then a raw input in one transaction, the way a prepared plan applies them."""
    with host.database.transaction() as uow:
        uow.connection.execute(*raw_binding(input_id, **(binding or {})))
        uow.connection.execute(*raw_input(input_id, payload or edit_payload(), **input_options))


# -- admission ----------------------------------------------------------------------------------------------------------

def test_an_edit_is_one_binding_and_one_general_input_with_the_original_submitter(host):
    admission, change_set = prepare_edit(host)
    assert [change.resource.resource_type for change in change_set.changes] == ["context_graph_edits", "context_inputs"]
    binding, stored = host.query("SELECT * FROM context_graph_edits")[0], host.query("SELECT * FROM context_inputs")[0]
    assert (binding["input_id"], binding["reserved_event_id"], binding["plan_digest"], binding["expected_revision"]) == (
        stored["id"], EVENT, digest_of(text_of(PLAN)), BASE) == (admission.input_id, admission.reserved_event_id, admission.plan_digest, BASE)
    assert binding["plan_json"] == PLAN and binding["targets_json"] == TARGETS
    assert binding["attribution_json"] == ATTRIBUTION
    assert (stored["input_kind"], stored["operation"], stored["state"], stored["business_id"], stored["submitted_by"]) == (
        "general", "add", "queued", BUSINESS, AGENT)
    assert stored["context_event_id"] is None and stored["attempt"] == 0 and stored["item_id"] is None
    assert stored["payload_json"] == {"operation": "add", "path": f"foundation/edits/{EVENT}.txt", "content": text_of(PLAN),
                                      "metadata": {"kind": "graph_edit", "title": "Explicit graph edit", "origin": "controlled_caller"},
                                      "submitted_by": AGENT}
    verified = load(host, stored["id"])
    assert (verified["plan"], verified["content"], verified["reserved_event_id"], verified["submitted_by"]) == (PLAN, text_of(PLAN), EVENT, AGENT)


def test_every_read_of_the_input_exposes_the_same_execution_facts(host):
    prepare_edit(host)
    ordinary = host.data("context_queue_submit", {"operation": "add", "path": "foundation/notes/a.md", "content": "Body", "metadata": META},
                         host.agent())["input"]
    facts = {"kind": "deterministic_graph_edit", "plan_digest": digest_of(text_of(PLAN)), "expected_revision": BASE, "reserved_event_id": EVENT}
    listed = host.read("context_queue_list", {}, host.agent())["items"]
    edit = next(item for item in listed if item["id"] != ordinary["id"])
    assert edit["execution"] == facts and edit["summary"] == {"path": f"foundation/edits/{EVENT}.txt"}
    assert next(item for item in listed if item["id"] == ordinary["id"])["execution"] is None and ordinary["execution"] is None
    got = host.read("context_queue_get", {"input_id": edit["id"]}, host.agent())["input"]
    assert got["execution"] == facts and got["state"] == "queued" and got["attempts"] == [] and got["error"] is None
    assert host.read("context_queue_get", {"input_id": ordinary["id"]}, host.agent())["input"]["execution"] is None


def test_admission_refuses_anything_that_is_not_exactly_the_plan_its_digest_and_its_caller(host):
    content = text_of(PLAN)
    bad = (dict(content=content.replace("Fix a label", "Fix another label")),                       # text is not the plan
           dict(digest="b" * 64), dict(digest="not-a-digest"),
           dict(plan={**PLAN, "expected_revision": "b" * 40}, content=content),                      # base is not the text's
           dict(plan={**PLAN, "expected_revision": "xyz"}, content=text_of({**PLAN, "expected_revision": "xyz"})),
           dict(event="TOOLONG"), dict(event="ab"), dict(event="AB12C"),
           dict(attribution={"principal_id": "someone-else", "principal_kind": "agent"}),
           dict(attribution={"principal_id": AGENT, "principal_kind": "human", "delegation": None}),
           dict(attribution={"principal_id": AGENT}), dict(attribution={"principal_id": AGENT, "principal_kind": "agent"}),
           dict(targets={"kind": "node"}), dict(targets=[{"kind": "node"}]),
           dict(business_id=OTHER), dict(business_id=None), dict(limit=10), dict(content="  "))
    for options in bad:
        with pytest.raises(DomainError) as error:
            prepare_edit(host, **options)
        assert error.value.code in {"validation_error", "forbidden"}, options
    assert counts(host) == (0, 0)


def test_a_reserved_event_id_must_be_unused_at_admission_and_at_apply(host):
    prepare_edit(host)
    with pytest.raises(DomainError) as error:
        prepare_edit(host, plan={**PLAN, "rationale": "Another"})
    assert error.value.code == "conflict" and error.value.details == {"reserved_event_id": EVENT}
    # Two plans prepared at the same time with the same reservation: the second one's apply leaves neither of its rows.
    _, first = prepare_edit(host, event="aaaaa", apply=False)
    _, second = prepare_edit(host, event="aaaaa", plan={**PLAN, "rationale": "Racing"}, apply=False)
    ctx = host.agent()
    with host.database.transaction() as uow:
        apply_records(ctx, first, uow)
    before = counts(host)
    with rejected("UNIQUE"):
        with host.database.transaction() as uow:
            apply_records(ctx, second, uow)
    assert counts(host) == before
    with host.database.read() as uow:
        assert edits.reserved_event_ids(uow) == {EVENT, "aaaaa"} and EVENT in edits.taken_event_ids(uow)


def test_a_used_queue_event_id_cannot_be_reserved(host):
    ordinary = host.data("context_queue_submit", {"operation": "add", "path": "foundation/notes/a.md", "content": "Body", "metadata": META},
                         host.agent())["input"]
    drive(host, "start_attempt", ordinary["id"], context_event_id="used1")
    with pytest.raises(DomainError) as error:
        prepare_edit(host, event="used1")
    assert error.value.code == "conflict"
    refused(host, *raw_binding(new_id(), event="used1"), message="before its own new input")        # and the database agrees
    assert host.count("context_graph_edits") == 0


# -- the reserved path and the database invariants ----------------------------------------------------------------------------

def test_a_public_submission_cannot_name_the_reserved_path_or_its_metadata(host):
    path = f"foundation/edits/{EVENT}.txt"
    for operation in ("add", "edit", "remove"):
        arguments = {"operation": operation, "path": path, **({"content": text_of(PLAN), "metadata": META} if operation != "remove" else {})}
        result = host.invoke("context_queue_submit", arguments, host.agent())
        assert result.status == "error" and result.error["code"] == "validation_error" and "foundation/edits/" in result.error["message"]
        for ctx in (host.owner(), host.owner(None)):
            assert host.invoke("context_queue_submit", arguments, ctx).status == "error"
    spoofed = host.invoke("context_queue_submit", {"operation": "add", "path": "foundation/notes/a.md", "content": text_of(PLAN),
                                                   "metadata": dict(edits.METADATA)}, host.agent())
    assert spoofed.status == "error" and "official Action input mode" in spoofed.error["message"]
    assert host.count("context_inputs") == 0


def test_an_ordinary_input_with_edit_shaped_text_is_not_a_graph_edit(host):
    item = host.data("context_queue_submit", {"operation": "add", "path": "foundation/notes/plan.md", "content": text_of(PLAN),
                                              "metadata": META}, host.agent())["input"]
    assert item["execution"] is None and host.count("context_graph_edits") == 0
    with host.database.read() as uow:
        assert edits.load(uow, item["id"]) is None


def test_the_reserved_path_holds_only_the_input_of_a_matching_binding(host):
    refused(host, *raw_input(new_id(), edit_payload()))                                              # no binding at all
    stranger = new_id()
    with rejected():                                                      # a binding for another input
        with host.database.transaction() as uow:
            uow.connection.execute(*raw_binding(new_id()))
            uow.connection.execute(*raw_input(stranger, edit_payload()))
    assert counts(host) == (0, 0)
    attacks = {
        "path": edit_payload(path="foundation/edits/other.txt"), "extra field": {**edit_payload(), "extra": 1},
        "metadata kind": edit_payload(metadata={**edits.METADATA, "kind": "note"}),
        "metadata extra": edit_payload(metadata={**edits.METADATA, "origin_ref": "x"}),
        "metadata title": edit_payload(metadata={**edits.METADATA, "title": "Other"}),
        "metadata origin": edit_payload(metadata={**edits.METADATA, "origin": "agent"}),
        "submitter": edit_payload(submitted_by=COORDINATOR)}
    for name, payload in attacks.items():
        input_id = new_id()
        with rejected("graph-edit"):
            raw_pair(host, input_id, payload=payload)
        assert counts(host) == (0, 0), name
    for options in ({"operation": "edit"}, {"operation": "remove"}, {"submitted_by": COORDINATOR}):
        with rejected():
            raw_pair(host, new_id(), **options)
    with rejected():                                                      # attribution of someone else
        raw_pair(host, new_id(), binding={"attribution_json": json.dumps({**ATTRIBUTION, "principal_id": COORDINATOR})})
    assert counts(host) == (0, 0)


def test_a_binding_needs_a_new_input_in_the_same_transaction(host):
    with rejected():                                                      # the deferred foreign key
        with host.database.transaction() as uow:
            uow.connection.execute(*raw_binding(new_id()))
    existing = host.data("context_queue_submit", {"operation": "add", "path": "foundation/notes/a.md", "content": "Body", "metadata": META},
                         host.agent())["input"]
    refused(host, *raw_binding(existing["id"]), message="before its own new input")                 # not for an input that exists
    action_binding = new_id()
    with rejected():                                                      # nor for a non-GENERAL input
        with host.database.transaction() as uow:
            uow.connection.execute(*raw_binding(action_binding))
            uow.connection.execute("INSERT INTO context_inputs(id,business_id,input_kind,item_id,revision,operation,payload_json,"
                                   "submitted_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                   (action_binding, BUSINESS, "action", "x", 1, "add", json.dumps({}), AGENT, "now"))
    assert host.count("context_graph_edits") == 0


def test_the_binding_is_immutable_and_never_deleted(host):
    admission, _ = prepare_edit(host)
    for column, value in (("reserved_event_id", "zzzzz"), ("plan_digest", "c" * 64), ("expected_revision", "c" * 40),
                          ("plan_json", "{}"), ("attribution_json", "{}"), ("targets_json", "[]")):
        refused(host, f"UPDATE context_graph_edits SET {column}=? WHERE input_id=?", (value, admission.input_id),
                message="graph-edit binding is immutable")
    refused(host, "DELETE FROM context_graph_edits WHERE input_id=?", (admission.input_id,), message="retained")
    refused(host, "UPDATE context_inputs SET payload_json=? WHERE id=?", (json.dumps(edit_payload(event="other")), admission.input_id),
            message="immutable")
    refused(host, "DELETE FROM context_inputs WHERE id=?", (admission.input_id,), message="retained")
    assert load(host, admission.input_id)["plan"] == PLAN


def test_a_raw_binding_must_describe_one_consistent_plan(host):
    for changes in ({"expected_revision": "b" * 40}, {"plan_digest": "xyz"}, {"reserved_event_id": "UPPER"},
                    {"plan_json": "[]"}, {"targets_json": "{}"}, {"attribution_json": "[]"}):
        with rejected():
            raw_pair(host, new_id(), binding=changes)
    assert counts(host) == (0, 0)


# -- the caller's closed delegation -------------------------------------------------------------------------------------

def delegated(host, **changes):
    values = dict(delegation_binding_id=BINDING, delegation_assignment_id=ASSIGNMENT, delegation_generation=3)
    return replace(host.agent(), **{**values, **changes})


def test_a_delegated_caller_is_recorded_as_exactly_its_authenticated_triplet(host):
    ctx = delegated(host)
    assert edits.delegation_of(ctx) == TRIPLET and edits.delegation_of(host.agent()) is None
    admission, _ = prepare_edit(host, ctx=ctx)
    stored = host.query("SELECT attribution_json FROM context_graph_edits")[0]["attribution_json"]
    assert stored == {"principal_id": AGENT, "principal_kind": "agent", "delegation": TRIPLET}
    assert load(host, admission.input_id)["attribution"] == stored
    # Identities only: no credential, hash, lease or decision can be part of the frozen value.
    assert set(stored["delegation"]) == {"binding_id", "assignment_id", "generation"}


def test_the_attribution_must_carry_the_delegation_the_caller_was_authenticated_with(host):
    direct, ctx = host.agent(), delegated(host)
    base = {"principal_id": AGENT, "principal_kind": "agent"}
    wrong = (
        dict(ctx=ctx, attribution={**base, "delegation": None}),                                           # delegated, recorded direct
        dict(ctx=direct, attribution={**base, "delegation": TRIPLET}),                                      # direct, recorded delegated
        dict(ctx=ctx, attribution={**base, "delegation": {**TRIPLET, "generation": 4}}),                    # another generation
        dict(ctx=ctx, attribution={**base, "delegation": {**TRIPLET, "binding_id": ASSIGNMENT}}),
        dict(ctx=ctx, attribution={**base, "delegation": {**TRIPLET, "assignment_id": BINDING}}),
    )
    for options in wrong:
        with pytest.raises(DomainError) as error:
            prepare_edit(host, **options)
        assert error.value.code == "forbidden", options
    malformed = (None, {}, [], {**TRIPLET, "credential_hash": "h"}, {"binding_id": BINDING, "assignment_id": ASSIGNMENT},
                 {**TRIPLET, "generation": 0}, {**TRIPLET, "generation": True}, {**TRIPLET, "generation": "3"},
                 {**TRIPLET, "generation": 3.0}, {**TRIPLET, "binding_id": "not-a-uuid"}, {**TRIPLET, "assignment_id": None},
                 {**TRIPLET, "binding_id": BINDING.upper()})
    for value in malformed:
        attribution = {**base, "delegation": value}
        if value is None:
            attribution.pop("delegation")                                  # the key itself is required, as null for a direct caller
        with pytest.raises(DomainError) as error:
            prepare_edit(host, ctx=ctx, attribution=attribution)
        assert error.value.code == "validation_error", value
    with pytest.raises(DomainError):                                       # a delegated value cannot sit on a human principal
        edits.check_delegation({"principal_kind": "human", "delegation": TRIPLET})
    assert counts(host) == (0, 0)


def test_the_database_and_every_read_refuse_a_binding_without_a_closed_delegation(host):
    for name, attribution in {
        "missing": {"principal_id": AGENT, "principal_kind": "agent"},
        "extra key": {**ATTRIBUTION, "delegation": {**TRIPLET, "lease_id": BINDING}},
        "missing key": {**ATTRIBUTION, "delegation": {"binding_id": BINDING, "generation": 3}},
        "generation zero": {**ATTRIBUTION, "delegation": {**TRIPLET, "generation": 0}},
        "generation text": {**ATTRIBUTION, "delegation": {**TRIPLET, "generation": "3"}},
        "short id": {**ATTRIBUTION, "delegation": {**TRIPLET, "binding_id": "abc"}},
        "non hex id": {**ATTRIBUTION, "delegation": {**TRIPLET, "assignment_id": "z" * 36}},
        "array": {**ATTRIBUTION, "delegation": [TRIPLET]},
        "human": {**ATTRIBUTION, "principal_kind": "human", "delegation": TRIPLET},
    }.items():
        with rejected("closed delegation"):
            raw_pair(host, new_id(), binding={"attribution_json": json.dumps(attribution)})
        assert counts(host) == (0, 0), name
    delegated_attribution = {**ATTRIBUTION, "delegation": TRIPLET}
    input_id = new_id()
    raw_pair(host, input_id, binding={"attribution_json": json.dumps(delegated_attribution)})
    assert load(host, input_id)["attribution"] == delegated_attribution
    # A stored binding whose delegation was damaged (the immutable row can only be rewritten with its trigger dropped) is
    # an integrity failure on read, never a direct caller.
    with host.database.transaction() as uow:
        uow.connection.execute("DROP TRIGGER context_graph_edits_immutable")
        uow.connection.execute("UPDATE context_graph_edits SET attribution_json=json_remove(attribution_json, ?) WHERE input_id=?",
                               ("$.delegation", input_id))
    with pytest.raises(DomainError) as error:
        load(host, input_id)
    assert error.value.code == edits.INTEGRITY


# -- reads ----------------------------------------------------------------------------------------------------------------

def test_a_read_verifies_the_text_digest_and_never_infers_a_binding(host):
    admission, _ = prepare_edit(host)
    # The immutable payload can only be changed with the database triggers dropped; a read still notices.
    with host.database.transaction() as uow:
        uow.connection.execute("DROP TRIGGER context_inputs_immutable_input")
        uow.connection.execute("UPDATE context_inputs SET payload_json=json_set(payload_json,'$.content',?) WHERE id=?",
                               (text_of({**PLAN, "rationale": "Changed"}), admission.input_id))
    with pytest.raises(DomainError) as error:
        load(host, admission.input_id)
    assert error.value.code == edits.INTEGRITY
    read = host.invoke("context_queue_get", {"input_id": admission.input_id}, host.agent())
    assert read.status == "error" and read.error["code"] == edits.INTEGRITY


def test_a_stored_edit_input_is_never_read_as_ordinary_without_the_contribution(host):
    admission, _ = prepare_edit(host)
    from qorqut_context.integration.queue import ContextQueueOperations
    bare = ContextQueueOperations(outcomes=FilingOutcomes(), clock=host.clock)
    with host.database.read() as uow:
        row = uow.connection.execute("SELECT * FROM context_inputs WHERE id=?", (admission.input_id,)).fetchone()
        with pytest.raises(DomainError) as error:
            bare.executions(uow, row)
    assert error.value.code == "composition_error"


# -- the queue lifecycle -------------------------------------------------------------------------------------------------------

def test_the_input_keeps_a_null_event_until_its_first_attempt_which_carries_the_reservation(host):
    admission, _ = prepare_edit(host)
    first = host.query("SELECT context_event_id, attempt FROM context_inputs")[0]
    assert (first["context_event_id"], first["attempt"]) == (None, 0)
    with rejected("reserved Context event ID"):
        drive(host, "start_attempt", admission.input_id, context_event_id="other")
    stored = host.query("SELECT state, attempt, context_event_id FROM context_inputs")[0]
    assert (stored["state"], stored["attempt"], stored["context_event_id"]) == ("queued", 0, None)
    started = drive(host, "start_attempt", admission.input_id, context_event_id=EVENT)
    assert (started["state"], started["attempt"], started["context_event_id"]) == ("processing", 1, EVENT)
    assert host.count("context_input_attempts", "context_event_id=?", EVENT) == 1


def test_no_other_input_can_start_an_attempt_with_a_reserved_event_id(host):
    prepare_edit(host)
    ordinary = host.data("context_queue_submit", {"operation": "add", "path": "foundation/notes/a.md", "content": "Body", "metadata": META},
                         host.agent())["input"]
    # The ordinary input is not the head; the database refuses the reserved ID whichever transition names it.
    with rejected("reserved Context event ID"):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE context_inputs SET context_event_id=? WHERE id=?", (EVENT, ordinary["id"]))
    with rejected("reserved Context event ID"):
        with host.database.transaction() as uow:
            uow.connection.execute("INSERT INTO context_input_attempts(input_id,attempt,context_event_id,cause,started_at) VALUES (?,?,?,?,?)",
                                   (ordinary["id"], 1, EVENT, "queue", "now"))
    with host.database.read() as uow:
        assert EVENT in edits.reserved_event_ids(uow) and EVENT not in transitions.used_event_ids(uow, driver_id=WORKER)


def test_an_unstarted_edit_is_discarded_at_once_and_leaves_no_event(host):
    admission, _ = prepare_edit(host)
    result = host.data("context_queue_discard", {"input_id": admission.input_id, "expected_state": "queued"}, host.agent())["result"]
    assert result["state"] == "discarded"
    stored = host.query("SELECT state, context_event_id, attempt, result_json FROM context_inputs")[0]
    assert (stored["state"], stored["context_event_id"], stored["attempt"]) == ("discarded", None, 0)
    assert stored["result_json"]["reason"] == "never_started"
    assert host.count("context_input_attempts") == 0 and host.count("context_completions") == 0
    assert host.count("context_graph_edits") == 1                                                    # the binding stays as history


def test_publication_blocking_retry_and_discard_use_the_ordinary_transitions_and_record_no_filing(host):
    admission, _ = prepare_edit(host)
    drive(host, "start_attempt", admission.input_id, context_event_id=EVENT)
    blocked = drive(host, "record_blocked", admission.input_id, ordubashy_id=COORDINATOR,
                    error={"code": "authority_revoked", "message": "The caller no longer holds the grant", "retryable": True})
    assert blocked["state"] == "blocked" and blocked["error"]["code"] == "authority_revoked"
    assert host.read("context_queue_get", {"input_id": admission.input_id}, host.agent())["input"]["execution"]["reserved_event_id"] == EVENT
    host.data("context_queue_retry", {"input_id": admission.input_id, "expected_state": "blocked"}, host.agent())
    assert drive(host, "start_attempt", admission.input_id, context_event_id=EVENT)["attempt"] == 2          # the same ID again
    done = drive(host, "record_published", admission.input_id, ordubashy_id=COORDINATOR,
                 result={"context_event_id": EVENT, "published_commit": "c0ffee"}, handoff_note={"disposition": "empty", "text": ""})
    assert done["state"] == "published" and "execution" not in done                                 # transitions return the internal view
    assert host.count("context_completions") == 0 and host.count("context_filings") == 0
    assert host.read("context_queue_get", {"input_id": admission.input_id}, host.agent())["input"]["execution"]["plan_digest"] == digest_of(text_of(PLAN))


def test_a_blocked_edit_can_be_discarded_and_keeps_its_reservation_closed(host):
    admission, _ = prepare_edit(host)
    drive(host, "start_attempt", admission.input_id, context_event_id=EVENT)
    drive(host, "record_blocked", admission.input_id, ordubashy_id=COORDINATOR, error={"code": "stale_base", "message": "Base moved"})
    host.data("context_queue_discard", {"input_id": admission.input_id, "expected_state": "blocked"}, host.agent())
    drive(host, "record_discarded", admission.input_id, result={"context_event_id": EVENT, "context": "no_draft", "mechanical": True})
    assert host.query("SELECT state FROM context_inputs")[0]["state"] == "discarded"
    with pytest.raises(DomainError):                                                                 # the ID is never reused
        prepare_edit(host, plan={**PLAN, "rationale": "Reviewed again"})


def test_existing_ordinary_and_action_inputs_keep_their_path(host):
    ordinary = host.data("context_queue_submit", {"operation": "add", "path": "foundation/notes/a.md", "content": "Body", "metadata": META},
                         host.agent())["input"]
    action = host.record_action()
    assert ordinary["state"] == "queued" and ordinary["execution"] is None
    rows = host.query("SELECT input_kind, state FROM context_inputs ORDER BY sequence")
    assert [row["input_kind"] for row in rows] == ["general", "action"]
    assert [item["execution"] for item in host.read("context_queue_list", {}, host.agent())["items"]] == [None, None]
    assert action is not None


def test_a_database_without_the_contribution_is_a_composition_error_never_an_ordinary_input(host):
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    bare.execute("CREATE TABLE context_inputs (id TEXT, input_kind TEXT, payload_json TEXT)")
    bare.execute("INSERT INTO context_inputs VALUES ('only', 'general', '{\"path\": \"foundation/notes/a.txt\"}')")
    unit = type("Unit", (), {"connection": bare})()
    for read in (lambda: edits.load(unit, "only"), lambda: edits.reserved_event_ids(unit), lambda: edits.ExecutionFacts()(unit, {"id": "only"})):
        with pytest.raises(DomainError) as refused:
            read()
        assert refused.value.code == edits.COMPOSITION
    assert not hasattr(edits, "composed")
