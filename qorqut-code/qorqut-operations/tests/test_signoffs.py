"""The extracted Signoff service against fake typed host ports: real engine, synthetic domain, nothing external."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib

import pytest

from qorqut_operations import signoffs
from qorqut_operations.contracts import ChangeSet, DomainError, new_id, plain
from qorqut_operations.signoffs import ABSENT, SignoffPorts, SignoffService
from qorqut_operations.signoffs.ports import checked_narrowing
from qorqut_operations.storage import ApplyReceipts, AssemblyError, StorageContribution
from signoff_support import (COORDINATOR, DOCUMENT, HOST_SCHEMA, OTHER_WORKER, FakeExecution, FakePeers, FakeReferences,
                             FakeTasks, SyntheticHost)
from support import AGENT, BUSINESS, HUMAN, OTHER, Demo, database_at


@pytest.fixture
def host(tmp_path):
    return SyntheticHost(tmp_path)


def staged(host, **kwargs):
    result = host.stage(**kwargs)
    assert result.status == "signoff_required", result.error
    return host.row(result.signoff["id"])


def code(result):
    assert result.status == "error", result
    return result.error["code"]


# -- 1. a restricted principal stages, the authorized owner approves, the engine applies exactly once ----------------------

def test_restricted_agent_proposal_is_applied_once_after_a_reviewed_owner_approval(host):
    row = staged(host)
    assert row["state"] == "pending" and row["proposer_principal_id"] == AGENT and host.items() == []
    # The proposer can neither decide nor read-write its own approval: owner decisions are not agent operations.
    assert code(host.decide(row, ctx=host.agent())) == "forbidden"
    registry = host.catalog.registry
    assert registry.metadata("signoffs_review").owner_only and registry.metadata("signoffs_retry_apply").owner_only
    assert code(host.app.invoke(host.agent(), "signoffs_review", {}, {"idempotency_key": "x"})) == "operation_unavailable"
    assert host.row(row["id"])["state"] == "pending" and host.items() == []

    # Every part of the reviewed identity is bound: a stale hash, proposal revision or status revision decides nothing.
    for bad in ({"payload_hash": "0" * 64}, {"proposal_revision": 2}, {"expected_status_revision": 9}):
        assert code(host.decide(row, **bad)) == "stale_signoff"
    assert host.row(row["id"])["state"] == "pending" and host.items() == []

    approved = host.decide(row, key="approve-once")
    assert approved.status == "accepted" and approved.data["signoff"]["state"] == "approved"
    assert host.items() == []                       # approval authorizes; it does not itself apply
    assert host.count("apply_receipts") == 1

    applied = host.service.run_one(row["id"])
    assert applied["state"] == "applied" and [item["id"] for item in host.items()] == ["item-1"]
    assert host.items()[0]["created_by"] == AGENT   # the saved v1 proposal ran as its proposer
    assert host.service.run_one(row["id"]) is None   # nothing left to claim
    assert host.service.run_pending() == 0 and len(host.items()) == 1
    stored = host.row(row["id"])
    assert stored["state"] == "applied" and stored["decision_json"]["by"] == HUMAN
    assert stored["apply_result_json"]["data"]["records"] == ["item-1"]

    # A replay of the very same decision is the saved result, not a second application.
    replay = host.decide(row, key="approve-once")
    assert replay.status in {"accepted", "ok"} and replay.data["replayed"] is True
    assert len(host.items()) == 1 and host.count("apply_receipts") == 1
    # A different decision on an already decided proposal is truthfully stale.
    assert code(host.decide(host.row(row["id"]), "reject", feedback="changed my mind")) == "stale_signoff"


def test_concurrent_application_claims_apply_exactly_once(host):
    row = staged(host)
    assert host.decide(row).status == "accepted"
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: host.service.run_one(row["id"]), range(4)))
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert len(host.items()) == 1 and host.row(row["id"])["state"] == "applied"


def test_stale_target_changed_grants_and_unauthorized_reviewers_are_truthful(host):
    # A target that changed after staging conflicts instead of applying the saved proposal.
    row = staged(host, arguments={"id": "item-stale", "title": "Stale"})
    with host.database.transaction(business_id=BUSINESS) as uow:
        uow.repository("items").insert({"id": "item-stale", "title": "Someone else created it first"})
    outcome = host.decide(row)
    assert code(outcome) == "stale_signoff" and outcome.error["details"]["cause"]["code"] == "revision_conflict"
    assert host.row(row["id"])["state"] == "conflicted"
    assert [item["title"] for item in host.items()] == ["Someone else created it first"]

    # Grants changed before the decision: the proposer's current policy no longer allows the saved proposal.
    row = staged(host, arguments={"id": "item-grant", "title": "Grant"})
    host.agents.overrides = {"items_create": "deny"}
    outcome = host.decide(row)
    assert code(outcome) == "stale_signoff" and outcome.error["details"]["cause"]["code"] == "policy_denied"
    assert host.row(row["id"])["state"] == "conflicted"
    host.agents.overrides = {}

    # Grants changed after approval but before execution: the approved work does not run and says why.
    row = staged(host, arguments={"id": "item-after", "title": "After"})
    assert host.decide(row).status == "accepted"
    host.agents.revision = 2        # a bare profile revision bump is not an authority change in v1
    host.agents.overrides = {"items_create": "deny"}
    assert host.service.run_one(row["id"]) is None
    failed = host.row(row["id"])
    assert failed["state"] == "conflicted" and failed["error_json"]["code"] == "policy_denied"
    assert failed["error_json"]["effect_status"] == "none"
    assert [item["id"] for item in host.items()] == ["item-stale"]
    host.agents.revision, host.agents.overrides = 1, {}

    # Only the current business owner may decide: a non-owner member, an agent, another business, a stranger.
    row = staged(host, arguments={"id": "item-owner", "title": "Owner"})
    receipts = host.count("apply_receipts")
    assert code(host.decide(row, ctx=host.stranger())) == "forbidden"
    assert code(host.decide(row, ctx=host.agent(OTHER_WORKER))) == "forbidden"
    assert code(host.decide(row, ctx=host.coordinator())) == "forbidden"
    other = replace(host.owner(), business_id=OTHER, allowed_business_ids=frozenset({OTHER}))
    assert code(host.decide(row, ctx=other)) == "not_found"
    assert host.row(row["id"])["state"] == "pending" and host.count("apply_receipts") == receipts
    with host.database.transaction() as uow:
        uow.repository("business_memberships", business_id=BUSINESS).update(HUMAN, {"is_owner": False}, expected_revision=1)
    assert code(host.decide(row)) == "forbidden"    # ownership is read when deciding, not remembered from staging
    assert host.row(row["id"])["state"] == "pending"


def test_reject_records_the_owner_message_and_never_applies(host):
    row = staged(host)
    assert code(host.decide(row, "reject")) == "message_required"     # no one eligible has spoken yet
    outcome = host.decide(row, "reject", feedback="Not now")
    assert outcome.status == "ok" and outcome.data["signoff"]["decision"]["feedback"] == "Not now"
    assert host.row(row["id"])["state"] == "rejected" and host.items() == []
    assert host.service.run_one(row["id"]) is None and host.count("apply_receipts") == 0


def test_file_proposal_is_applied_through_the_receipt_executor(host):
    arguments = {"document_id": DOCUMENT, "text": "After\n"}
    row = staged(host, operation="plans_set", arguments=arguments)
    path = host.root / "PLAN.md"
    assert path.read_bytes() == b"Before\n" and row["state"] == "pending"
    assert host.decide(row).status == "accepted"
    assert path.read_bytes() == b"Before\n"          # nothing is written until the application claim runs
    assert host.service.run_one(row["id"])["state"] == "applied"
    assert path.read_bytes() == b"After\n"
    with host.database.read() as uow:
        receipt = ApplyReceipts(uow).get(row["id"])
        document = uow.repository("documents", business_id=BUSINESS).get(DOCUMENT)
        assert receipt["state"] == "applied" and receipt["signoff_id"] == row["id"]
        assert document["content_hash"] == hashlib.sha256(b"After\n").hexdigest() and document["availability"] == "ready"
        assert uow.repository("resource_claims").list() == []
    assert host.service.run_one(row["id"]) is None and path.read_bytes() == b"After\n"

    # The managed file changed after staging: approval conflicts and the owner's bytes are preserved.
    second = staged(host, operation="plans_set", arguments={"document_id": DOCUMENT, "text": "Third\n"})
    path.write_bytes(b"Edited elsewhere\n")
    assert code(host.decide(second)) == "stale_signoff"
    assert host.row(second["id"])["state"] == "conflicted" and path.read_bytes() == b"Edited elsewhere\n"


def test_file_proposal_changed_after_approval_fails_without_replacing_the_file(host):
    row = staged(host, operation="plans_set", arguments={"document_id": DOCUMENT, "text": "After\n"})
    assert host.decide(row).status == "accepted"
    (host.root / "PLAN.md").write_bytes(b"Owner edit\n")
    assert host.service.run_one(row["id"]) is None
    stored = host.row(row["id"])
    # v1 keeps the owner's bytes and reports an uncertain, non-retryable failure rather than guessing.
    assert stored["state"] == "failed" and (host.root / "PLAN.md").read_bytes() == b"Owner edit\n"
    assert stored["error_json"]["code"] == "revision_conflict" and stored["error_json"]["retryable"] is False
    assert stored["error_json"]["effect_status"] == "partial_or_uncertain"


def test_declared_platform_staging_callbacks_run_in_the_staging_transaction_without_sending(host):
    # A declared platform-staging port: staging_values before the row, staged after it, transition on each decision.
    row = staged(host, operation="email_send", arguments={"id": "mail-1", "submission": "email.send"})
    assert row["category"] == "platform_action" and row["execution_job_id"]
    assert host.platform.calls == ["staging_values", "staged"]
    assert host.items() == []                         # staging sends and applies nothing
    assert host.decide(row).status == "accepted"
    assert host.platform.calls[-1] == "transition"
    assert host.service.run_one(row["id"])["state"] == "applied"
    assert host.platform.calls[-1] == "transition" and [item["id"] for item in host.items()] == ["mail-1"]

    # A failing staged callback rolls back the proposal, the callback's own writes and the saved receipt together.
    before, saved, history = host.count("signoffs"), host.count("idempotency_receipts"), host.count("demo_history")
    host.platform.fail_staged = True
    failed = host.stage(operation="email_send", arguments={"id": "mail-2", "submission": "email.send"}, key="mail-two")
    assert code(failed) == "operation_failed"
    assert host.platform.calls[-2:] == ["staging_values", "staged"]          # both callbacks did run ...
    assert host.count("signoffs") == before and host.count("idempotency_receipts") == saved
    assert host.count("demo_history") == history                              # ... and their own writes rolled back


def test_platform_actions_are_unavailable_when_the_domain_is_declared_absent(tmp_path):
    host = SyntheticHost(tmp_path, platform=False)   # platform=False declares the port ABSENT
    failed = host.stage(operation="email_send", arguments={"id": "mail-1", "submission": "email.send"})
    assert code(failed) == "operation_unavailable" and host.count("signoffs") == 0 and host.items() == []
    # Ordinary proposals, which never needed the domain, behave exactly as before.
    assert host.decide(staged(host)).status == "accepted"
    assert host.service.run_pending() == 1 and [item["id"] for item in host.items()] == ["item-1"]


@pytest.mark.parametrize("applied", [False, True])
def test_retained_platform_signoffs_stay_readable_after_removing_the_domain(host, applied):
    row = staged(host, operation="email_send", arguments={"id": "old-mail", "submission": "email.send"})
    if applied:
        assert host.decide(row).status == "accepted"
        assert host.service.run_one(row["id"])["state"] == "applied"
        row = host.row(row["id"])
    host.service.ports = replace(host.service.ports, platform=ABSENT)
    before = host.row(row["id"])
    result = host.app.invoke(host.owner(), "signoffs_get", {"signoff_id": row["id"]})
    assert result.status == "ok", result.error
    saved = result.data["signoff"]
    assert plain(saved["preview"]) == row["preview_json"] and saved["state"] == row["state"]
    assert saved["actionable"] is False and saved["platform"]["execution"]["historical"]
    assert host.app.invoke(host.owner(), "signoffs_list", {}).status == "ok"
    assert host.service.notifications.overview(host.owner()) is not None
    assert host.row(row["id"]) == before
    if not applied:
        assert code(host.decide(row)) == "operation_unavailable"
        assert host.row(row["id"]) == before and host.items() == []


# -- 2. standing authority, input responses, delegated answers and rejection --------------------------------------------------

def test_no_operation_gets_standing_mail_authority_for_any_agent_including_the_coordinator(host):
    # The engine has no mail-control authority: the configured coordinator's proposal waits for an owner decision like
    # any other agent's, and the engine asks no peer port to decide that.
    ctx = host.coordinator()
    pending = host.stage(operation="email_send", arguments={"id": "mail-1", "submission": "email.send"}, ctx=ctx)
    assert pending.status == "signoff_required"
    assert host.row(pending.signoff["id"])["state"] == "pending" and host.items() == []
    other = host.stage(operation="email_send", arguments={"id": "mail-2", "submission": "email.send"},
                       ctx=host.agent(OTHER_WORKER))
    assert other.status == "signoff_required"
    assert [call for call in host.peers.calls if call[0] != "ordubashy"] == []
    assert not hasattr(host.service, "standing_authority_current") and not hasattr(host.service, "authorize_mail_control")


def test_a_recorded_standing_authority_the_engine_no_longer_issues_is_never_executed(host):
    row = staged(host, operation="email_send", arguments={"id": "mail-1", "submission": "email.send"})
    plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
    with host.database.transaction() as uow:
        ApplyReceipts(uow).create(plan, receipt_id=row["id"], signoff_id=row["id"])
        host.service._update(row, {"state": "approved", "decision_json": {
            "kind": "approve", "by": AGENT, "at": host.now, "authority": "marketing_business_execution",
            "message_id": None}}, uow)
    # The decision is not an owner approval and nothing re-verifies it, so execution is refused and explained.
    assert host.service.run_one(row["id"]) is None
    stored = host.row(row["id"])
    assert stored["state"] == "conflicted" and "no longer exists" in stored["error_json"]["message"]
    assert host.items() == []


FORM = {"title": "Which plan?", "explanation": "Pick one", "kind": "question", "selection_mode": "single",
        "choices": [{"id": "a", "label": "Alpha", "explanation": "First"}, {"id": "b", "label": "Beta", "explanation": "Second"}]}


def create_input(host, form=None, ctx=None, key="ask-1", **extra):
    result = host.app.invoke(ctx or host.agent(), "signoffs_input_create",
                             {"request_key": key, "form": form or FORM, **extra}, {"idempotency_key": new_id()})
    assert result.status == "ok", result.error
    return host.row(result.data["signoff"]["id"])


def test_input_responses_authorize_nothing_and_cannot_be_approved(host):
    action = staged(host)
    ask = create_input(host)
    assert ask["category"] == "input_request" and ask["state"] == "pending" and ask["execution_job_id"] is None
    # An input request is answered, never approved, rejected or retried.
    assert code(host.decide(ask)) == "invalid_transition"
    assert code(host.decide(ask, "reject", feedback="no")) == "invalid_transition"
    response = host.service.respond(host.owner(), ask["id"], proposal_revision=ask["proposal_revision"],
        payload_hash=ask["payload_hash"], expected_status_revision=ask["status_revision"], idempotency_key="answer",
        notes="Approve everything", selected_choices=["a"])
    assert response.status == "ok" and response.data["signoff"]["state"] == "answered"
    # The answer, even one that says "approve", changes nothing about the pending action and applies nothing.
    assert host.row(action["id"])["state"] == "pending" and host.count("apply_receipts") == 0
    assert host.service.run_pending() == 0 and host.items() == []
    # Using respond on a prepared action is the wrong tool, and a repeated answer is a replay or stale.
    action_response = host.service.respond(host.owner(), action["id"], proposal_revision=1,
        payload_hash=action["payload_hash"], expected_status_revision=1, idempotency_key="wrong", notes="yes")
    assert code(action_response) == "invalid_transition"
    again = host.service.respond(host.owner(), ask["id"], proposal_revision=1, payload_hash=ask["payload_hash"],
        expected_status_revision=ask["status_revision"], idempotency_key="answer", notes="Approve everything",
        selected_choices=["a"])
    assert again.data["replayed"] is True
    changed = host.service.respond(host.owner(), ask["id"], proposal_revision=1, payload_hash=ask["payload_hash"],
        expected_status_revision=ask["status_revision"], idempotency_key="other", notes="Different")
    assert code(changed) == "stale_signoff"
    # Invalid choices are refused.
    second = create_input(host, key="ask-2")
    bad = host.service.respond(host.owner(), second["id"], proposal_revision=1, payload_hash=second["payload_hash"],
        expected_status_revision=1, idempotency_key="bad", notes="", selected_choices=["zzz"])
    assert code(bad) == "validation_error"


def test_only_the_proposer_cancels_an_input_request_and_delegated_answers_keep_their_boundaries(host):
    ask = create_input(host, dict(FORM, response_policy="management_allowed"))
    assert host.count("host_routing", f"signoff_id='{ask['id']}'") == 1   # the management port routed it
    stranger_cancel = host.app.invoke(host.agent(OTHER_WORKER), "signoffs_input_cancel", {"signoff_id": ask["id"]},
                                      {"idempotency_key": "c1"})
    assert code(stranger_cancel) in {"forbidden", "not_found"}

    def delegated(ctx, row, **extra):
        return host.app.invoke(ctx, "signoffs_input_answer_delegated", {"signoff_id": row["id"],
            "proposal_revision": row["proposal_revision"], "payload_hash": row["payload_hash"],
            "expected_status_revision": row["status_revision"], "notes": "Alpha", "selected_choices": ["a"], **extra},
            {"idempotency_key": new_id()})
    # Only the configured coordinator may answer, and only a question routed to management.
    assert code(delegated(host.agent(), ask)) in {"forbidden", "policy_denied"}
    answered = delegated(host.coordinator(), ask)
    assert answered.status == "accepted" or answered.status == "ok", answered.error
    stored = host.row(ask["id"])
    assert stored["state"] == "answered" and stored["decision_json"]["by"] == COORDINATOR
    assert host.count("apply_receipts") == 0

    # Owner-only questions and prepared actions are never delegable, and the answer cannot approve an action.
    owner_only = create_input(host, dict(FORM, response_policy="owner_only"), key="ask-2")
    assert code(delegated(host.coordinator(), owner_only)) == "forbidden"
    action = staged(host)
    assert code(delegated(host.coordinator(), action)) == "forbidden"
    assert host.row(action["id"])["state"] == "pending" and host.items() == []

    # The proposer may withdraw its own pending request.
    mine = create_input(host, key="ask-3")
    withdrawn = host.app.invoke(host.agent(), "signoffs_input_cancel", {"signoff_id": mine["id"]}, {"idempotency_key": "w1"})
    assert withdrawn.status == "ok", withdrawn.error
    assert host.row(mine["id"])["state"] == "cancelled"


def test_rejection_message_rule_ignores_ordinary_discussion(host):
    row = staged(host)
    comment = host.app.invoke(host.agent(), "signoffs_comment", {"signoff_id": row["id"], "text": "I approve this"},
                              {"idempotency_key": new_id()})
    assert comment.status == "ok", comment.error
    assert host.row(row["id"])["state"] == "pending" and host.row(row["id"])["decision_json"] is None
    assert host.service.run_pending() == 0 and host.count("apply_receipts") == 0
    # The proposer talking is not a formal decision and does not satisfy the rejection-message requirement.
    assert code(host.decide(row, "reject")) == "message_required"
    assert code(host.decide(row, "reject", feedback="   ")) == "message_required"
    listing = host.app.invoke(host.owner(), "signoffs_notes_list", {"signoff_id": row["id"]})
    assert listing.data["rejection_requires_message"] is True
    # The configured Ordubashy speaking on the signoff does satisfy it, for the owner's later rejection.
    spoken = host.app.invoke(host.coordinator(), "signoffs_comment", {"signoff_id": row["id"], "text": "Looks wrong"},
                             {"idempotency_key": new_id()})
    assert spoken.status == "ok", spoken.error
    assert host.app.invoke(host.owner(), "signoffs_notes_list", {"signoff_id": row["id"]}
                           ).data["rejection_requires_message"] is False
    assert host.decide(row, "reject").status == "ok" and host.row(row["id"])["state"] == "rejected"


def test_delegated_rejection_is_ordubashy_only_and_needs_a_message(host):
    row = staged(host)
    args = {"signoff_id": row["id"], "proposal_revision": 1, "payload_hash": row["payload_hash"],
            "expected_status_revision": 1}
    assert code(host.app.invoke(host.agent(OTHER_WORKER), "signoffs_reject", args, {"idempotency_key": "r1"})
                ) in {"forbidden", "policy_denied"}
    assert code(host.app.invoke(host.coordinator(), "signoffs_reject", args, {"idempotency_key": "r2"})
                ) == "message_required"
    rejected = host.app.invoke(host.coordinator(), "signoffs_reject", {**args, "feedback": "Not safe"}, {"idempotency_key": "r3"})
    assert rejected.status == "ok", rejected.error
    assert host.row(row["id"])["state"] == "rejected" and host.items() == []
    # Reject is not approval: it never creates a receipt and an input request cannot be rejected this way.
    ask = create_input(host)
    assert code(host.app.invoke(host.coordinator(), "signoffs_reject", {"signoff_id": ask["id"], "proposal_revision": 1,
        "payload_hash": ask["payload_hash"], "expected_status_revision": 1, "feedback": "no"}, {"idempotency_key": "r4"})
        ) == "invalid_transition"


# -- 3. typed port assembly -------------------------------------------------------------------------------------------

def ports(**overrides):
    values = dict(execution=FakeExecution(), peers=FakePeers(), tasks=FakeTasks(), references=FakeReferences(),
                  platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT)
    values.update(overrides)
    return SignoffPorts(**values)


@pytest.mark.parametrize("name", ["execution", "peers", "tasks", "references"])
def test_a_missing_required_port_fails_at_assembly(name):
    for missing in (None, ABSENT):
        with pytest.raises(AssemblyError, match="Required Signoff port missing: " + name):
            ports(**{name: missing})


@pytest.mark.parametrize("name", ["platform", "email", "management", "focus"])
def test_an_optional_domain_must_be_declared_and_complete(name):
    with pytest.raises(AssemblyError, match="must be declared"):
        ports(**{name: None})
    with pytest.raises(AssemblyError, match="Signoff port " + name):
        ports(**{name: object()})
    assert getattr(ports(), name) is ABSENT


class CoordinatorOnly:
    @staticmethod
    def configured_ordubashy(uow, *, require_codex):
        return None


def test_the_peers_port_is_only_the_configured_coordinator_lookup():
    assert ports(peers=CoordinatorOnly()).peers.configured_ordubashy(None, require_codex=False) is None
    with pytest.raises(AssemblyError, match="Signoff port peers"):
        ports(peers=object())


def test_incomplete_required_ports_and_untyped_references_are_rejected():
    class Incomplete:
        execution_states = ()
    with pytest.raises(AssemblyError, match="execution"):
        ports(execution=Incomplete())
    with pytest.raises(AssemblyError, match="ReferenceTarget"):
        ports(references=type("Refs", (), {"targets": {"task": "tasks_get"}, "task_namespace": staticmethod(lambda c, a: "")})())
    with pytest.raises(AssemblyError, match="SignoffPorts"):
        SignoffService(None, object(), locks=None)


class HideFrom:
    """A host narrowing: ``items_create`` proposals are readable only by the listed principals."""

    def __init__(self, *allowed):
        self.allowed = allowed

    def narrow(self, context, uow):
        marks = ",".join("?" for _ in self.allowed)
        return f" AND (s.operation_name <> ? OR ? IN ({marks}))", ["items_create", context.principal_id, *self.allowed]


def test_a_host_narrowing_removes_a_proposal_from_every_reader_but_never_adds_one(tmp_path):
    host = SyntheticHost(tmp_path, narrowing=HideFrom(HUMAN))
    row = staged(host)
    owner = host.owner()
    args = {"signoff_id": row["id"]}
    assert host.app.invoke(owner, "signoffs_get", args).status == "ok"
    assert [item["id"] for item in host.app.invoke(owner, "signoffs_list", {}).data["items"]] == [row["id"]]
    assert host.app.invoke(owner, "signoffs_notes_list", args).status == "ok"
    with host.database.read() as uow:
        assert [item["id"] for item in host.service.notifications._rows(owner, uow)[2]] == [row["id"]]
    # The proposer and the coordinator are not in the allowed set: the proposal does not exist for them anywhere.
    for context in (host.agent(), host.coordinator()):
        assert code(host.app.invoke(context, "signoffs_get", args)) == "not_found"
        assert code(host.app.invoke(context, "signoffs_notes_list", args)) == "not_found"
        assert code(host.app.invoke(context, "signoffs_comment", {**args, "text": "hello"}, {"idempotency_key": new_id()})
                    ) == "not_found"
        assert list(host.app.invoke(context, "signoffs_list", {}).data["items"]) == []
    # Narrowing never grants: a reader the engine already refuses stays refused even when the host allows them.
    assert code(host.app.invoke(host.stranger(), "signoffs_get", args)) in {"forbidden", "policy_denied", "not_found"}
    assert host.row(row["id"])["state"] == "pending" and host.items() == []


def test_owner_attention_also_honors_a_host_narrowing(tmp_path):
    host = SyntheticHost(tmp_path, narrowing=HideFrom())
    row = staged(host)
    with host.database.read() as uow:
        assert host.service.notifications._rows(host.owner(), uow)[2] == []
    assert code(host.app.invoke(host.owner(), "signoffs_get", {"signoff_id": row["id"]})) == "not_found"
    assert host.row(row["id"])["state"] == "pending"


def test_without_a_host_narrowing_visibility_is_unchanged(host):
    assert host.ports.narrowing is ABSENT
    row = staged(host)
    for context in (host.owner(), host.agent()):
        assert host.app.invoke(context, "signoffs_get", {"signoff_id": row["id"]}).status == "ok"


@pytest.mark.parametrize("sql,params", [
    (" OR 1=1", []), (" AND (1=1) OR (1=1)", []), (" AND (1=1)) OR (1=1", []), (" AND (1=1) ; DROP TABLE signoffs", []),
    (" AND (s.id = ?)", []), (" AND (1=1)", [1]), (" AND (s.id = 'x' -- )", []), (" AND (1=1 /* )", []),
    ("", ["unused"]), (" AND (", []), (" AND ((1=1)", []), ("AND (1=1)", []), (None, []), (" AND (1=1)", "x")])
def test_a_malformed_narrowing_is_refused_before_it_can_widen_visibility(sql, params):
    with pytest.raises(AssemblyError):
        checked_narrowing(sql, params)


def test_a_well_formed_narrowing_is_accepted_and_literals_are_not_counted():
    assert checked_narrowing("", []) == ("", [])
    assert checked_narrowing(" AND (s.operation_name <> ? OR (? = ?))", ("a", "b", "c"))[1] == ["a", "b", "c"]
    assert checked_narrowing(" AND (s.state <> 'what?(')", [])[1] == []


def test_a_narrowing_that_returns_a_widening_fragment_exposes_nothing(tmp_path):
    class Widening:
        def narrow(self, context, uow):
            return " OR 1=1", []
    host = SyntheticHost(tmp_path, narrowing=Widening())
    row = host.stage()
    with pytest.raises(AssemblyError):
        with host.database.read() as uow:
            host.service._visibility(host.owner(), uow)
    assert row.status == "signoff_required"


def test_an_incomplete_narrowing_port_fails_at_assembly():
    with pytest.raises(AssemblyError, match="Signoff port narrowing"):
        ports(narrowing=object())
    with pytest.raises(AssemblyError, match="must be declared"):
        ports(narrowing=None)
    assert ports().narrowing is ABSENT


def test_service_must_be_bound_once_to_its_own_application(host, tmp_path):
    with pytest.raises(AssemblyError, match="already bound"):
        host.service.bind(host.app)
    fresh = SignoffService(host.database, host.ports, locks=host.locks)
    with pytest.raises(AssemblyError, match="not bound"):
        fresh.app
    with pytest.raises(AssemblyError, match="this database"):
        fresh.bind(host.app)          # the application names a different stager
    with pytest.raises(AssemblyError, match="LockPaths"):
        SignoffService(host.database, host.ports, locks=tmp_path)


def test_required_host_tables_must_exist_and_be_specified(tmp_path):
    (tmp_path / "a").mkdir()
    with pytest.raises(AssemblyError, match="inbox_changes"):    # the table does not exist in the composed schema
        database_at(tmp_path / "a", demo=Demo(), extra=(signoffs.storage_contribution(),))
    (tmp_path / "b").mkdir()
    with pytest.raises(AssemblyError, match="inbox_changes"):    # it exists, but nobody specified how to write it
        database_at(tmp_path / "b", demo=Demo(), extra=(StorageContribution("host", schema=HOST_SCHEMA),
                                                          signoffs.storage_contribution()))


def test_unavailable_optional_domains_make_only_their_operations_unavailable(tmp_path):
    host = SyntheticHost(tmp_path, management=False, focus=False)
    names = {descriptor.name for descriptor in host.service.descriptors()}
    assert "signoffs_input_answer_delegated" not in names and "signoffs_get" in names
    ask = host.app.invoke(host.agent(), "signoffs_input_create", {"request_key": "k", "form": FORM},
                          {"idempotency_key": "one"})
    assert ask.status == "ok"                    # without the management domain nothing auto-routes
    with_focus = host.app.invoke(host.agent(), "signoffs_input_create", {"request_key": "k2", "form": FORM,
        "focus": {"card": "x"}}, {"idempotency_key": "two"})
    assert code(with_focus) == "validation_error"   # the focus argument does not exist without the domain


# -- 4. notifications and ordering --------------------------------------------------------------------------------------

def test_owner_attention_is_durable_and_follows_the_eastern_summary_and_snooze_rules(host):
    ask = create_input(host, dict(FORM, response_policy="owner_only"), attention={
        "notify": True, "urgent": True, "reason": "Blocked on you"})
    owner = host.owner()
    overview = host.service.notifications.overview(owner)
    business = [item for item in overview["businesses"] if item["id"] == BUSINESS][0]
    assert business["signoffs"] == 1 and business["tasks"] == host.tasks.count and business["urgent"] == 1
    assert host.tasks.calls and host.tasks.calls[0][1] == HUMAN
    # An urgent notification is due at once, then hourly; before 08:00 Eastern no summary is created.
    host.service.notifications.tick()
    deliveries = host.service.notifications.deliveries(owner)["items"]
    assert [d["kind"] for d in deliveries] == ["individual"] and deliveries[0]["items"][0]["id"] == ask["id"]
    host.service.notifications.tick()
    assert len(host.service.notifications.deliveries(owner)["items"]) == 1   # not delivered twice in the same hour
    # Snoozing is bounded to 30 days and withdraws the current alert.
    with pytest.raises(DomainError, match="30 days"):
        host.service.notifications.snooze(owner, ask["id"], "2026-12-01T00:00:00Z")
    with pytest.raises(DomainError, match="timezone"):
        host.service.notifications.snooze(owner, ask["id"], "2026-10-13T00:00:00")
    snoozed = host.service.notifications.snooze(owner, ask["id"], "2026-10-12T14:00:00Z")
    assert snoozed["snoozed_until"].startswith("2026-10-12T14:00:00")
    assert host.service.notifications.deliveries(owner)["items"] == []
    # Humans only: an agent has no owner attention.
    with pytest.raises(DomainError):
        host.service.notifications.overview(host.agent())


# -- 4. the optional host outcome read model -------------------------------------------------------------------------------

def test_a_host_outcome_is_added_to_the_list_and_detail_of_a_visible_signoff_only(tmp_path):
    host = SyntheticHost(tmp_path, narrowing=HideFrom(HUMAN))
    row = staged(host)
    calls = []

    def outcome(found, uow, *, detail):
        calls.append((found["id"], detail))
        return {"stage": "queued", "job_id": "job-1"} if detail else {"stage": "queued"}

    host.ports.execution.outcome = outcome
    owner = host.owner()
    detail = host.app.invoke(owner, "signoffs_get", {"signoff_id": row["id"]}).data["signoff"]
    assert detail["outcome"] == {"stage": "queued", "job_id": "job-1"} and calls == [(row["id"], True)]
    # A list row carries only the bounded summary the host chose for it.
    listed = host.app.invoke(owner, "signoffs_list", {}).data["items"]
    assert [item["id"] for item in listed] == [row["id"]] and listed[0]["outcome"] == {"stage": "queued"}
    assert calls == [(row["id"], True), (row["id"], False)]
    # A reader the engine's visibility removes never reaches it, in a detail read or a listing.
    assert code(host.app.invoke(host.coordinator(), "signoffs_get", {"signoff_id": row["id"]})) == "not_found"
    assert host.app.invoke(host.coordinator(), "signoffs_list", {}).data["items"] == ()
    assert calls == [(row["id"], True), (row["id"], False)]


def test_a_missing_or_empty_host_outcome_leaves_the_signoff_representation_unchanged(host):
    row = staged(host)
    args = {"signoff_id": row["id"]}
    before = host.app.invoke(host.owner(), "signoffs_get", args).data["signoff"]
    assert "outcome" not in before
    host.ports.execution.outcome = lambda found, uow, **_: None
    assert host.app.invoke(host.owner(), "signoffs_get", args).data["signoff"] == before
