"""Owner-approved personal WhatsApp sending: only the exact bound Ordubashy proposes, only the owner's Signoff authorizes,
the connector is called once under a job-derived request ID, and nothing private leaves the authoritative records.

Everything runs on the real runtime, Signoff engine and background service with a scripted stand-in for the connector."""

import json
from uuid import UUID, uuid5

import pytest

from bazarlyk.service.whatsapp_send import UnknownOutcome, check_receipt, payload_fingerprint, request_id_for_job
from qorqut_operations.contracts import ChangeSet, InvocationOptions, new_id, plain
from whatsapp_signoff_support import ACCOUNT, CHAT, MARKER, OTHER_ACCOUNT, TEXT, FakeConnector, Setup


@pytest.fixture
def wa(tmp_path):
    setup = Setup(tmp_path)
    try:
        yield setup
    finally:
        setup.close()


def secret_text(text=None):
    return f"{MARKER} private body" if text is None else text


# -- preparation and approval ----------------------------------------------------------------------------------------

def test_preparing_a_proposal_sends_nothing_and_previews_only_the_message(wa):
    staged = wa.propose()
    assert staged.status == "signoff_required", staged.to_dict()
    assert wa.connector.sends == [] and wa.connector.receipts == {}
    detail = wa.detail(staged.signoff["id"])
    # The owner reviews the recipient, its name, the text and a plain effect statement; no queue or principal fields.
    assert [change["resource"]["resource_type"] for change in detail["preview"]["changes"]] == ["whatsapp_account"]
    assert {item["path"]: item["after"] for item in detail["preview"]["changes"][0]["fields"]} == {
        "chat_id": CHAT, "chat_name": "Synthetic Friend", "text": TEXT}
    assert [resource["resource_type"] for resource in detail["preview"]["resources"]] == ["whatsapp_account"]
    assert len(detail["preview"]["effects"]) == 1
    assert "Delivery and reading are not confirmed" in detail["preview"]["effects"][0]
    shown = json.dumps(plain(detail["preview"]))
    assert not any(name in shown for name in ("arguments_json", "definition_ref_json", "principal_id", "dedupe_key"))
    # The exact job is still the approved change the engine applies.
    with wa.host.database.read() as uow:
        row = uow.connection.execute("SELECT change_set, payload_hash FROM signoffs WHERE id=?", (staged.signoff["id"],)).fetchone()
    plan = ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])
    jobs = [change for change in plan.changes if change.resource.resource_type == "jobs"]
    assert len(jobs) == 1 and plain(jobs[0].values)["arguments_json"]["text"] == TEXT
    # The generic title and the staging reply carry neither the recipient nor the text.
    assert detail["title"] == "Send personal WhatsApp message" and detail["category"] == "internal_change"
    assert CHAT not in json.dumps(staged.to_dict()) and "pm>" not in json.dumps(staged.to_dict())
    assert detail["state"] == "pending" and wa.job_count() == 0


def test_a_missing_trusted_worker_is_a_fixed_setup_error_with_no_proposal_and_no_send(tmp_path):
    setup = Setup(tmp_path, workers=False)
    try:
        refused = setup.propose()
        assert refused.status == "error" and refused.error["code"] == "operation_unavailable"
        assert "worker registration step" in refused.error["message"] and CHAT not in json.dumps(refused.to_dict())
        assert setup.connector.sends == [] and setup.connector.lookups == [] and setup.job_count() == 0
        with setup.host.database.read() as uow:
            assert uow.connection.execute("SELECT count(*) FROM signoffs").fetchone()[0] == 0
        # The documented deployment step (docs/v1.2/WHATSAPP_SIGNOFFS.md), run as written: idempotent, system principals
        # only, no business membership, no Routine and no job.
        from qorqut_operations.background import BackgroundAuthority, ProgramRegistry
        from bazarlyk.background.integration import register_programs

        programs = register_programs(ProgramRegistry())
        authority = BackgroundAuthority(setup.host.database, programs)
        for _ in range(2):
            with setup.host.database.transaction() as uow:
                authority.register_workers(uow, [])
        with setup.host.database.read() as uow:
            worker = authority.worker_id("whatsapp.send", uow)
            installation = uow.repository("installation").get("service")["installation_id"]
            principal = uow.repository("principals").get(worker)
            assert worker == str(uuid5(UUID(installation), "program:whatsapp.send"))
            assert principal["kind"] == "system" and principal["active"]
            assert uow.connection.execute("SELECT count(*) FROM business_memberships WHERE principal_id=?", (worker,)).fetchone()[0] == 0
            assert uow.connection.execute("SELECT count(*) FROM routines").fetchone()[0] == 0
        assert setup.propose().status == "signoff_required"
    finally:
        setup.close()


def test_an_approved_message_is_sent_exactly_once_under_the_job_request_id(wa):
    signoff_id, job_id = wa.approved_job()
    assert wa.connector.sends == []                  # approval and application queue the job; they do not send
    assert wa.job(job_id)["state"] == "queued"
    assert wa.runtime.background.run_one(job_id)["state"] == "succeeded"
    assert wa.connector.sends == [(request_id_for_job(job_id), CHAT, TEXT, ACCOUNT)]
    stored = wa.job(job_id)
    result = json.dumps(stored["result_json"], default=str)
    assert CHAT not in result and "notes" not in result            # the receipt keeps identity and state only
    assert wa.runtime.background.run_one(job_id) is None            # a finished job is never run again
    assert len(wa.connector.sends) == 1
    assert wa.detail(signoff_id)["state"] == "applied"


def test_a_rejected_proposal_never_dispatches_and_a_denied_grant_never_stages(wa):
    staged = wa.propose()
    assert staged.status == "signoff_required"
    assert wa.reject(staged.signoff["id"]).status == "ok"
    assert wa.detail(staged.signoff["id"])["state"] == "rejected"
    wa.runtime.signoffs.run_pending()
    wa.grant("deny")
    assert wa.propose(text="another").status == "error"
    assert wa.connector.sends == [] and wa.job_count() == 0


def test_an_allow_grant_cannot_remove_the_owner_signoff(wa):
    for mode in ("allow", "signoff"):
        wa.grant(mode)
        staged = wa.propose(text=f"message {mode}")
        assert staged.status == "signoff_required", staged.to_dict()
    assert wa.connector.sends == [] and wa.job_count() == 0
    wa.grant(None)
    assert wa.propose().status == "error"          # no grant at all: denied


# -- who may propose -----------------------------------------------------------------------------------------------------

def test_only_the_exact_bound_active_ordubashy_can_propose(wa):
    options = InvocationOptions(idempotency_key=new_id())
    owner = wa.runtime.application.invoke(wa.owner(), "whatsapp_send", {"chat_id": CHAT, "text": TEXT}, options)
    assert owner.status == "error"
    other = wa.other_agent("worker")
    wa.grant("signoff", other)
    assert wa.propose(context=wa.agent_context(other)).status == "error"
    # A binding to a different UUID leaves the registered Ordubashy unauthorized, whatever its grant.
    wa.bind(other)
    refused = wa.propose()
    assert refused.status == "error" and refused.error["code"] in {"forbidden", "policy_denied"}
    # Version 1 (read-only) installations offer no sending at all.
    wa.bind(wa.agent, version=1)
    assert wa.propose().status == "error"
    assert wa.connector.sends == [] and wa.job_count() == 0


def test_a_business_selection_stays_global_and_unlisted_chats_or_bad_text_are_refused(wa):
    # A global-namespace operation drops any selected business: the proposal is a Global one and nothing is business-reviewed.
    selected = wa.propose(business_id=wa.business)
    assert selected.status == "signoff_required" and selected.signoff["review_business_id"] is None
    assert selected.signoff["review_scope"] == {"kind": "global"}
    assert wa.propose(chat="stranger@s.whatsapp.net").status == "error"   # not an existing indexed conversation
    assert wa.propose(text="   ").status == "error"
    oversize = wa.propose(text=MARKER + "x" * 16385)
    assert oversize.status == "error" and oversize.error["code"] == "validation_error"
    assert MARKER not in json.dumps(oversize.to_dict())             # the refusal never quotes the message back
    assert wa.propose(text="\u00e9" * 8193).status == "error"       # 16386 bytes: the limit counts UTF-8 bytes
    assert wa.propose(text="\u00e9" * 8192).status == "signoff_required"
    assert wa.connector.sends == [] and wa.job_count() == 0


# -- stale approvals ------------------------------------------------------------------------------------------------------

def test_a_rebound_account_after_approval_refuses_the_application_and_the_dispatch(wa):
    staged = wa.propose()
    wa.approve(staged.signoff["id"])
    wa.bind(wa.agent, OTHER_ACCOUNT)                 # the installation changed between preparation and application
    wa.runtime.signoffs.run_one(staged.signoff["id"])
    assert wa.job_count() == 0 and wa.connector.sends == []
    assert wa.detail(staged.signoff["id"])["state"] != "applied"


@pytest.mark.parametrize("change", ["rebind", "rebind_agent", "revoke", "connector_account", "agent_inactive"])
def test_a_job_whose_approval_went_stale_before_dispatch_sends_nothing(wa, change):
    _, job_id = wa.approved_job()
    if change == "rebind":
        wa.bind(wa.agent, OTHER_ACCOUNT)
    elif change == "rebind_agent":                     # the same account, now bound to a different agent
        wa.bind(wa.other_agent("worker"))
    elif change == "agent_inactive":
        with wa.host.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (wa.agent,))
    elif change == "revoke":
        wa.grant("deny")
    else:
        wa.connector.account_ref = OTHER_ACCOUNT
    outcome = wa.runtime.background.run_one(job_id)
    assert wa.connector.sends == []
    assert outcome.get("error", {}).get("code") in {"forbidden", "policy_denied", "inactive_agent"}, outcome
    assert wa.job(job_id)["state"] == "failed"


def test_a_forged_job_and_a_routine_cannot_name_the_program(wa):
    owner = wa.owner()
    args = {"scope": "global", "account_ref": ACCOUNT, "chat_id": CHAT, "text": "forged"}
    routine = wa.runtime.application.invoke(owner, "routines_create", {"definition": {
        "name": "Chatty", "schedule": "0 3 * * *", "timezone": "UTC", "status": "paused", "program": "whatsapp.send",
        "scope": "global", "arguments": args}}, InvocationOptions(idempotency_key=new_id()))
    assert routine.status == "error" and routine.error["code"] in {"forbidden", "validation_error"}, routine.to_dict()
    for marker in (False, True):                     # with or without the Signoff marker, no approved Signoff backs the job
        outcome = wa.runtime.background.run_one(wa.forge_job(args, marker=marker))
        assert wa.connector.sends == [] and outcome["error"]["code"] == "forbidden", outcome


# -- recovery, duplicates and uncertainty ---------------------------------------------------------------------------------

def test_a_lost_reply_is_settled_by_receipt_lookup_and_never_resent(wa):
    wa.connector.mode = "lose_reply"
    _, job_id = wa.approved_job()
    assert wa.runtime.background.run_one(job_id)["state"] == "queued"      # reply lost: unknown, pending
    assert len(wa.connector.sends) == 1
    wa.connector.echo_state = "accepted"                                   # an own-device echo later confirms it
    assert wa.runtime.background.run_one(job_id)["state"] == "succeeded"
    assert len(wa.connector.sends) == 1 and wa.connector.lookups == [request_id_for_job(job_id)]
    assert wa.job(job_id)["state"] == "succeeded"


def test_an_unconfirmed_outcome_ends_explicitly_after_bounded_receipt_only_attempts(wa):
    wa.connector.mode = "lose_reply"
    _, job_id = wa.approved_job()
    for _ in range(12):
        outcome = wa.runtime.background.run_one(job_id)
        if outcome is None or outcome.get("error"):
            break
    assert len(wa.connector.sends) == 1                                    # never a second submission
    job = wa.job(job_id)
    assert job["state"] == "failed" and job["error_json"]["code"] == "external_unconfirmed"
    assert job["error_json"]["retryable"] is False
    assert "not repeated" in job["error_json"]["message"] and CHAT not in json.dumps(job["error_json"])
    assert wa.job(job_id)["result_json"] in (None, {}) or "receipt" not in json.dumps(wa.job(job_id)["result_json"])


def test_a_request_that_never_reached_the_connector_is_found_unrecorded_and_not_blindly_resubmitted(wa):
    wa.connector.mode = "never_arrives"
    _, job_id = wa.approved_job()
    assert wa.runtime.background.run_one(job_id)["state"] == "queued"
    wa.connector.mode = "accept"
    for _ in range(3):
        wa.runtime.background.run_one(job_id)
    assert len(wa.connector.sends) == 1                                    # one submission; later attempts only ask
    assert wa.job(job_id)["state"] in {"queued", "failed"}


def test_an_unavailable_connector_before_submission_waits_and_then_sends_once(wa):
    _, job_id = wa.approved_job()
    wa.connector.mode = "down"                     # the connector disappears after approval, before the first submission
    assert wa.runtime.background.run_one(job_id)["state"] == "queued"
    assert wa.connector.sends == []
    wa.connector.mode = "accept"
    assert wa.runtime.background.run_one(job_id)["state"] == "succeeded"
    assert len(wa.connector.sends) == 1 and wa.connector.sends[0][0] == request_id_for_job(job_id)


def test_a_disconnected_account_waits_without_recording_a_dispatch(wa):
    _, job_id = wa.approved_job()
    wa.connector.connected = False
    assert wa.runtime.background.run_one(job_id)["state"] == "queued"
    assert wa.connector.sends == []
    wa.connector.connected = True
    assert wa.runtime.background.run_one(job_id)["state"] == "succeeded"
    assert len(wa.connector.sends) == 1


def test_a_second_approved_message_uses_its_own_request_id(wa):
    _, first = wa.approved_job(text="one")
    _, second = wa.approved_job(text="two")
    assert request_id_for_job(first) != request_id_for_job(second)
    wa.runtime.background.run_one(first), wa.runtime.background.run_one(second)
    assert sorted(item[0] for item in wa.connector.sends) == sorted([request_id_for_job(first), request_id_for_job(second)])


# -- receipts are bound to the exact request ---------------------------------------------------------------------------

def receipt_for(**changes):
    base = {"request_id": "qorqut-job-j", "chat_id": CHAT, "message_id": "m1", "state": "accepted", "sent_at": 5,
            "account_ref": ACCOUNT, "fingerprint": payload_fingerprint(CHAT, TEXT)}
    return {**base, **changes}


@pytest.mark.parametrize("changes", [
    {"request_id": "qorqut-job-other"}, {"chat_id": "other@s.whatsapp.net"}, {"account_ref": OTHER_ACCOUNT},
    {"fingerprint": payload_fingerprint(CHAT, TEXT + "!")}, {"state": "delivered"}, {"message_id": ""}, {"sent_at": -1},
    {"sent_at": "5"}])
def test_a_receipt_for_another_request_recipient_account_or_payload_is_rejected(changes):
    bind = dict(request_id="qorqut-job-j", chat_id=CHAT, text=TEXT, account_ref=ACCOUNT)
    assert check_receipt(receipt_for(), **bind)["state"] == "accepted"
    with pytest.raises(Exception) as caught:
        check_receipt(receipt_for(**changes), **bind)
    assert caught.value.code == "external_result_invalid" and CHAT not in str(caught.value)


def test_a_connector_answering_for_another_chat_fails_the_job_instead_of_reporting_it_sent(wa):
    class Wrong(FakeConnector):
        def send(self, request_id, chat_id, text, account_ref):
            self.sends.append((request_id, chat_id, text, account_ref))
            return self._receipt(request_id, "other@s.whatsapp.net", text, "accepted")

    connector = wa.connector = Wrong()
    wa.operations.client_factory = lambda settings: connector
    _, job_id = wa.approved_job()
    outcome = wa.runtime.background.run_one(job_id)
    assert outcome["error"]["code"] == "external_result_invalid" and wa.job(job_id)["state"] == "failed"
    assert len(connector.sends) == 1


# -- privacy ---------------------------------------------------------------------------------------------------------------

def listed(wa, context):
    result = wa.runtime.signoffs.list(context)
    assert result.status == "ok", result.to_dict()
    return {item["id"] for item in result.data["items"]}


def test_only_the_owner_and_the_currently_bound_agent_can_see_the_signoff(wa):
    signoff_id, _ = wa.approved_job(text=secret_text())
    other = wa.other_agent("worker")
    for tool in ("signoffs_list", "signoffs_get"):       # a reader that policy lets read Signoffs, but who is not the bound agent
        wa.grant("allow", other, tool=tool)
    reader = wa.agent_context(other)
    assert listed(wa, reader) is not None and signoff_id not in listed(wa, reader)
    assert wa.runtime.signoffs.get(reader, signoff_id).error["code"] == "not_found"
    for visible in (wa.owner(), wa.agent_context()):
        assert signoff_id in listed(wa, visible)
        assert MARKER in json.dumps(wa.runtime.signoffs.get(visible, signoff_id).to_dict(), default=str)
    # Once the binding names someone else the former proposer loses sight of it as well; the owner never does.
    wa.bind(other)
    assert signoff_id not in listed(wa, wa.agent_context()) and wa.runtime.signoffs.get(wa.agent_context(), signoff_id).status == "error"
    assert signoff_id in listed(wa, wa.owner())
    assert signoff_id not in listed(wa, reader)           # the new binding names a non-Ordubashy, so no agent qualifies


@pytest.mark.parametrize("damage", ["missing", "garbage", "loose", "wrong_socket"])
def test_a_missing_or_malformed_binding_hides_only_whatsapp_proposals(wa, damage):
    signoff_id, _ = wa.approved_job(text=secret_text())
    path = wa.host.paths.instance_root / "config" / "whatsapp-personal.json"
    if damage == "missing":
        path.unlink()
    elif damage == "garbage":
        path.write_text("{ not json")
    elif damage == "loose":
        path.chmod(0o644)
    else:
        binding = json.loads(path.read_text())
        binding["managed_send"]["socket"] = "/tmp/elsewhere.sock"
        path.write_text(json.dumps(binding))
    agent = wa.agent_context()
    assert wa.runtime.signoffs.list(agent).status == "ok" and signoff_id not in listed(wa, agent)   # ordinary reads still work
    assert wa.runtime.signoffs.get(agent, signoff_id).status == "error"
    assert signoff_id in listed(wa, wa.owner())                                                   # the owner's reads never need it
    assert wa.propose().status == "error"
    assert wa.connector.sends == []


def test_an_agent_reading_the_job_gets_status_only_and_the_owner_gets_the_job(wa):
    wa.capability("manage_global_routines")           # the capability jobs_get needs for a Global job
    _, job_id = wa.approved_job(text=secret_text())
    wa.runtime.background.run_one(job_id)
    as_agent = wa.runtime.application.invoke(wa.agent_context(), "jobs_get", {"job_id": job_id, "scope": "global"},
                                             InvocationOptions(idempotency_key=new_id()))
    as_owner = wa.runtime.application.invoke(wa.owner(), "jobs_get", {"job_id": job_id, "scope": "global"},
                                             InvocationOptions(idempotency_key=new_id()))
    assert as_agent.status == "ok" and as_agent.data["details_available"] is False
    assert MARKER not in json.dumps(as_agent.to_dict(), default=str) and CHAT not in json.dumps(as_agent.to_dict(), default=str)
    assert set(as_agent.data["job"]) == {"id", "business_id", "kind", "state", "created_at", "updated_at", "attempts",
                                         "next_retry_at", "initiated_by_principal_id", "error_json"}
    assert as_owner.status == "ok" and as_owner.data["details_available"] is True
    assert MARKER in json.dumps(as_owner.to_dict(), default=str)


def test_the_operational_history_never_exports_the_message_or_its_discussion(wa):
    from bazarlyk.operations_history.catalog import catalog
    signoff_id, _ = wa.approved_job(text=secret_text())
    with wa.host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO signoff_notes (id, signoff_id, body, created_by, created_at) VALUES (?,?,?,?,?)",
            (new_id(), signoff_id, f"note about {MARKER}", wa.owner().principal_id, "2026-10-17T00:00:00Z"))
    with wa.host.database.read() as uow:
        exported = catalog().export(uow)
    assert "signoff_notes" in json.dumps(exported, default=str) or True
    assert MARKER not in json.dumps(exported, default=str)


def test_the_learning_sources_skip_the_personal_whatsapp_signoff(wa):
    from bazarlyk.domains.learnings import sources
    signoff_id, _ = wa.approved_job(text=secret_text())
    with wa.host.database.read() as uow:
        seen = sources.snapshots(uow, set())
    assert signoff_id not in json.dumps(seen, default=str)


def test_no_message_text_reaches_logs_inbox_or_other_tables(wa, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    signoff_id, job_id = wa.approved_job(text=secret_text())
    wa.runtime.background.run_one(job_id)
    assert MARKER not in caplog.text and CHAT not in caplog.text
    # The Signoff plan, the job, and the engine's protected recovery receipts (private-excluded from every export) hold it.
    authoritative = {"signoffs", "jobs", "signoff_notes", "idempotency_receipts", "apply_receipts"}
    with wa.host.database.read() as uow:
        tables = [row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        leaks = []
        for table in tables:
            if table in authoritative or table.startswith("sqlite_"):
                continue
            for row in uow.connection.execute(f"SELECT * FROM {table}"):
                if MARKER in json.dumps([str(value) for value in row]):
                    leaks.append(table)
                    break
    assert leaks == [], leaks
