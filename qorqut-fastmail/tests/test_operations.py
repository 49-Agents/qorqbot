"""The ``fastmail_*`` operations on a composed host with the provider selected and a fake engine: account access without any
business mailbox, grants, sender identity, durable at-most-once execution and receipt recovery. Everything is synthetic."""

import json
import base64
import hashlib

import pytest

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from fake_engine import FakeEngine
from fastmail_host import CAPABILITIES, agent_context, build, call, approved_job, make_agent, send_arguments
from host_foundation_support import revoke_capabilities


@pytest.fixture
def host(tmp_path):
    return build(tmp_path)


def jobs(host):
    with host.instance.database.read() as uow:
        return [row for row in uow.connection.execute("SELECT * FROM jobs WHERE kind='fastmail.account_mutation'")]


def run(host, job):
    return host.runtime.background.run_one(job)


def state(host, job):
    with host.instance.database.read() as uow:
        return uow.repository("jobs", business_id=None).get(job, required=True)["state"]


# -- account access with no business mailbox -------------------------------------------------------------------------

def test_the_account_is_read_without_any_business_mailbox_table_or_setting(host):
    with host.instance.database.read() as uow:
        tables = {row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not [name for name in tables if name.startswith(("email_", "mailbox", "business_email"))]
    ctx = agent_context(host, host.ordubashy)
    capabilities = call(host, ctx, "fastmail_capabilities_get", {})
    assert capabilities.status == "ok" and capabilities.data["account_id"] == "acct-test"
    assert [item["id"] for item in call(host, ctx, "fastmail_identities_list", {}).data["identities"]] == ["id-1", "id-2", "id-3"]
    assert call(host, ctx, "fastmail_folders_list", {}).data["folders"][0]["id"] == "inbox"
    assert call(host, ctx, "fastmail_messages_search", {"q": "hello", "limit": 5}).data["query"] == {"q": "hello", "limit": "5"}
    assert call(host, ctx, "fastmail_message_get", {"message_id": "m-1"}).data["expected_state"] == "s1"
    assert call(host, ctx, "fastmail_thread_get", {"thread_id": "t-1"}).data["thread_id"] == "t-1"
    assert call(host, ctx, "fastmail_message_get", {"message_id": "absent"}).error["code"] == "not_found"


def test_attachment_reads_are_ranged_and_bounded(host):
    ctx = agent_context(host, host.ordubashy)
    total = len(host.engine.attachment)
    first = call(host, ctx, "fastmail_attachment_get", {"attachment_id": "a-1", "message_id": "m-1", "limit": 1000})
    assert first.status == "ok" and first.data["total"] == total and first.data["next_offset"] == 1000
    assert base64.b64decode(first.data["content_base64"]) == host.engine.attachment[:1000]
    last = call(host, ctx, "fastmail_attachment_get", {"attachment_id": "a-1", "message_id": "m-1", "offset": total - 5})
    assert last.data["next_offset"] == total
    assert call(host, ctx, "fastmail_attachment_get", {"attachment_id": "a-1", "message_id": "m-1", "limit": 6 * 1024 * 1024}).status == "error"
    assert call(host, ctx, "fastmail_messages_search", {"limit": 101}).status == "error"


def test_the_owner_may_read_and_the_engine_failure_is_unavailable_not_a_crash(host):
    assert call(host, host.owner_context, "fastmail_folders_list", {}).status == "ok"
    host.engine.token = "rotated" * 6                       # the engine now refuses this host's token
    refused = call(host, agent_context(host, host.ordubashy), "fastmail_folders_list", {})
    assert refused.status == "error" and refused.error["code"] == "external_auth_failed"


# -- the configured account is enforced on every account-specific read ----------------------------------------------

ACCOUNT_READS = [
    ("fastmail_identities_list", {}),
    ("fastmail_folders_list", {}),
    ("fastmail_messages_search", {"q": "hello", "limit": 5}),
    ("fastmail_message_get", {"message_id": "m-1"}),
    ("fastmail_thread_get", {"thread_id": "t-1"}),
    ("fastmail_attachment_get", {"attachment_id": "a-1", "message_id": "m-1"}),
    ("fastmail_receipt_get", {"request_id": "r-1"}),
]


def test_a_mismatched_configured_account_returns_no_account_payload(tmp_path):
    host = build(tmp_path, account_id="configured-elsewhere")            # the engine serves acct-test
    ctx = agent_context(host, host.ordubashy)
    assert call(host, ctx, "fastmail_capabilities_get", {}).error["code"] == "account_mismatch"
    for name, arguments in ACCOUNT_READS:
        result = call(host, ctx, name, arguments)
        assert result.status == "error" and result.error["code"] == "account_mismatch", (name, result.to_dict())
        assert not result.data, name
    assert {path.rstrip("/") for _, path in host.engine.requests} == {"/fastmail"}      # only the capability check reached the engine
    assert jobs(host) == [] and host.engine.posts == []


def test_a_matching_configured_account_and_an_unpinned_host_read_normally(tmp_path):
    for config in ({"account_id": "acct-test"}, {}):
        root = tmp_path / ("pinned" if config else "open")
        root.mkdir()
        host = build(root, **config)
        ctx = agent_context(host, host.ordubashy)
        for name, arguments in ACCOUNT_READS[:5]:
            assert call(host, ctx, name, arguments).status == "ok", (config, name)
        attachment = call(host, ctx, "fastmail_attachment_get", {"attachment_id": "a-1", "message_id": "m-1", "limit": 10})
        assert attachment.status == "ok" and attachment.data["next_offset"] == 10
        assert call(host, ctx, "fastmail_receipt_get", {"request_id": "r-1"}).error["code"] == "not_found"


def test_a_job_receipt_is_still_reported_while_the_engine_cannot_be_asked(tmp_path):
    host = build(tmp_path, account_id="acct-test")
    ctx = agent_context(host, host.ordubashy)
    request = approved_job(host, call(host, ctx, "fastmail_folder_create", {"account_id": "acct-test", "name": "Receipts"}))
    seen = []

    def unavailable(http_request):                          # every read now gets a 503, like an engine that cannot be asked
        seen.append(http_request.url.path.rstrip("/"))
        return host.engine.respond(503, {"error": "unavailable"})

    host.engine.handle = unavailable
    reported = call(host, ctx, "fastmail_receipt_get", {"request_id": request})
    assert seen == ["/fastmail"]                            # the capability check failed; no receipt request was made
    assert reported.status == "ok" and reported.data["receipt"] is None and reported.data["job"]["operation"] == "fastmail_folder_create"
    assert call(host, ctx, "fastmail_receipt_get", {"request_id": "unknown"}).error["code"] == "operation_unavailable"


# -- grants ---------------------------------------------------------------------------------------------------------

def test_access_needs_the_capability_and_an_authorized_global_agent(host):
    assert call(host, agent_context(host, host.worker), "fastmail_folders_list", {}).status == "ok"       # a granted global worker
    plain = call(host, agent_context(host, host.plain_worker), "fastmail_folders_list", {})
    assert plain.status == "error" and plain.error["code"] == "forbidden"
    applier = make_agent(host.instance, "learning-applier", "applier", CAPABILITIES)                      # right capability, wrong role
    denied = call(host, agent_context(host, applier), "fastmail_folders_list", {})
    assert denied.status == "error" and denied.error["code"] == "forbidden"


def test_an_explicit_tool_denial_is_honored_even_for_a_granted_agent(host):
    from host_foundation_support import set_agent_overrides
    set_agent_overrides(host.instance, host.worker, {"fastmail_send": "deny"}, {host.instance.business})
    ctx = agent_context(host, host.worker)
    assert call(host, ctx, "fastmail_send", send_arguments()).error["code"] == "policy_denied"
    assert call(host, ctx, "fastmail_folders_list", {}).status == "ok"
    assert jobs(host) == [] and host.engine.posts == []


# -- sender identity ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("changes,code", [
    ({"identity_id": "no-such-identity"}, "sender_identity_denied"),
    ({"from_address": "billing@example.test"}, "sender_identity_mismatch"),              # id-1 is owner@, not billing@
    ({"identity_id": "id-3", "from_address": "bcc@example.test"}, "sender_identity_denied"),   # automatic Bcc identity
    ({"account_id": "other-account"}, "account_mismatch"),
])
def test_a_wrong_sender_or_account_is_denied_before_a_job_exists(host, changes, code):
    ctx = agent_context(host, host.ordubashy)
    result = call(host, ctx, "fastmail_send", send_arguments(**changes))
    assert result.status == "error" and result.error["code"] == code, result.to_dict()
    assert jobs(host) == [] and host.engine.posts == []


def test_an_alias_is_just_another_identity_and_addresses_compare_case_insensitively(host):
    ctx = agent_context(host, host.ordubashy)
    sent = call(host, ctx, "fastmail_send", send_arguments(identity_id="id-2", from_address="Billing@Example.TEST"))
    assert run(host, approved_job(host, sent))["state"] == "succeeded"
    [post] = host.engine.posts
    assert post["identity_id"] == "id-2" and post["account_id"] == "acct-test" and post["action"] == "send"
    assert post["request_id"] == approved_job(host, sent) and "business_id" not in post and "mailbox" not in post


def test_reply_forward_and_draft_use_the_same_identity_check(host):
    ctx = agent_context(host, host.ordubashy)
    reply = call(host, ctx, "fastmail_reply", send_arguments(reply_message_id="m-1"))
    forward = call(host, ctx, "fastmail_forward", send_arguments(forward_message_id="m-1"))
    draft = call(host, ctx, "fastmail_draft_create", send_arguments())
    for result in (reply, forward, draft):
        assert run(host, approved_job(host, result))["state"] == "succeeded"
    assert [post["action"] for post in host.engine.posts] == ["send", "send", "create_draft"]
    assert host.engine.posts[0]["reply_message_id"] == "m-1" and host.engine.posts[1]["forward_message_id"] == "m-1"
    assert call(host, ctx, "fastmail_reply", send_arguments()).status == "error"                  # a reply names its source
    bad = call(host, ctx, "fastmail_draft_create", send_arguments(reply_message_id="m-1", forward_message_id="m-1"))
    assert bad.status == "error" and bad.error["code"] == "validation_error"
    wrong = call(host, ctx, "fastmail_draft_update", send_arguments(message_id="d-1", expected_state="s", from_address="x@example.test"))
    assert wrong.error["code"] == "sender_identity_mismatch"


def test_attachments_are_bounded_checked_and_renamed_for_the_engine(host):
    ctx = agent_context(host, host.ordubashy)
    content = b"synthetic attachment"
    good = {"filename": "note.txt", "content_type": "text/plain", "content_base64": base64.b64encode(content).decode(),
            "sha256": hashlib.sha256(content).hexdigest()}
    assert run(host, approved_job(host, call(host, ctx, "fastmail_send", send_arguments(attachments=[good]))))["state"] == "succeeded"
    [post] = host.engine.posts
    assert post["attachments"] == [{"filename": "note.txt", "content_type": "text/plain", "sha256": good["sha256"],
                                    "data_base64": good["content_base64"]}]
    for bad in ({**good, "sha256": "0" * 64}, {**good, "content_base64": "not base64!!"}):
        result = call(host, ctx, "fastmail_send", send_arguments(attachments=[bad]))
        assert result.status == "error" and result.error["code"] == "validation_error"
    big = base64.b64encode(b"x" * 800_000).decode()
    huge = [{**good, "content_base64": big, "sha256": hashlib.sha256(b"x" * 800_000).hexdigest()}] * 2
    assert call(host, ctx, "fastmail_send", send_arguments(attachments=huge)).status == "error"
    assert len(host.engine.posts) == 1


def test_vacation_and_masked_email_need_an_advertised_action(host):
    ctx = agent_context(host, host.ordubashy)
    host.engine.actions.remove("vacation_set")
    assert call(host, ctx, "fastmail_vacation_set", {"account_id": "acct-test", "vacation": {"isEnabled": True}}).error["code"] == "operation_unavailable"
    ok = call(host, ctx, "fastmail_masked_email_set", {"account_id": "acct-test", "masked_email": {"state": "enabled"}})
    assert run(host, approved_job(host, ok))["state"] == "succeeded"


# -- ordinary organizing, receipts and request ids --------------------------------------------------------------------

def test_organize_actions_complete_and_the_receipt_is_readable_by_request_id(host):
    ctx = agent_context(host, host.ordubashy)
    update = call(host, ctx, "fastmail_message_update", {"account_id": "acct-test", "message_id": "m-1", "seen": True,
                                                         "move_to_folder_id": "inbox", "expected_state": "s1"})
    folder = call(host, ctx, "fastmail_folder_create", {"account_id": "acct-test", "name": "Receipts"})
    rename = call(host, ctx, "fastmail_folder_update", {"account_id": "acct-test", "folder_id": "f-1", "name": "Bills", "expected_state": "s2"})
    delete = call(host, ctx, "fastmail_message_delete", {"account_id": "acct-test", "message_id": "m-1", "expected_state": "s1"})
    for result in (update, folder, rename, delete):
        assert run(host, approved_job(host, result))["state"] == "succeeded"
    assert [post["action"] for post in host.engine.posts] == ["message_update", "folder_create", "folder_update", "message_delete"]
    receipt = call(host, ctx, "fastmail_receipt_get", {"request_id": approved_job(host, folder)})
    assert receipt.status == "ok" and receipt.data["receipt"]["state"] == "completed" and receipt.data["receipt"]["account_id"] == "acct-test"
    assert receipt.data["job"]["state"] == "succeeded" and receipt.data["job"]["operation"] == "fastmail_folder_create"
    assert call(host, ctx, "fastmail_receipt_get", {"request_id": "unknown-request"}).error["code"] == "not_found"
    for post in host.engine.posts:
        assert set(post) <= {"account_id", "request_id", "action", "message_id", "seen", "move_to_folder_id", "expected_state",
                             "name", "folder_id"}


# -- durable at-most-once execution ---------------------------------------------------------------------------------

def test_a_response_lost_after_the_engine_recorded_the_send_is_recovered_from_the_receipt_without_resending(host):
    ctx = agent_context(host, host.ordubashy)
    host.engine.mode = "lost_after_record"
    job = approved_job(host, call(host, ctx, "fastmail_send", send_arguments()))
    assert run(host, job) == {"job_id": job, "state": "queued"}                    # unconfirmed: pending, not failed, not retried
    assert len(host.engine.posts) == 1
    assert run(host, job)["state"] == "succeeded"
    assert len(host.engine.posts) == 1                                              # the second run only read the receipt


def test_a_response_lost_before_the_engine_recorded_anything_is_never_blindly_resent(host):
    ctx = agent_context(host, host.ordubashy)
    host.engine.mode = "lost_before_record"
    job = approved_job(host, call(host, ctx, "fastmail_send", send_arguments()))
    for _ in range(7):
        assert run(host, job)["state"] == "queued"
    ended = run(host, job)
    assert ended["error"]["code"] == "external_unconfirmed" and not ended["error"].get("retryable")
    assert state(host, job) == "failed"
    assert len(host.engine.posts) == 1                                              # one attempt only: an uncertain send is never repeated
    seen = call(host, ctx, "fastmail_receipt_get", {"request_id": job})            # the granted caller can see the unconfirmed end
    assert seen.status == "ok" and seen.data["receipt"] is None
    assert seen.data["job"]["state"] == "failed" and seen.data["job"]["error_code"] == "external_unconfirmed"
    assert set(seen.data["job"]) == {"state", "attempts", "operation", "error_code", "created_at", "updated_at"}


def test_a_prepared_receipt_stays_pending_until_the_engine_completes_it(host):
    ctx = agent_context(host, host.ordubashy)
    host.engine.mode = "prepared"
    job = approved_job(host, call(host, ctx, "fastmail_send", send_arguments()))
    assert run(host, job)["state"] == "queued" and run(host, job)["state"] == "queued"
    pending = call(host, ctx, "fastmail_receipt_get", {"request_id": job})
    assert pending.data["receipt"]["state"] == "prepared" and pending.data["job"]["state"] == "queued"
    host.engine.complete(job)
    assert run(host, job)["state"] == "succeeded" and len(host.engine.posts) == 1


def test_an_uncertain_receipt_ends_unconfirmed_after_bounded_recovery_and_a_failed_one_ends_failed(host):
    ctx = agent_context(host, host.ordubashy)
    host.engine.mode = "uncertain"
    job = approved_job(host, call(host, ctx, "fastmail_send", send_arguments()))
    for _ in range(7):
        assert run(host, job)["state"] == "queued"
    assert run(host, job)["error"]["code"] == "external_unconfirmed"
    host.engine.mode = "failed"
    failed = approved_job(host, call(host, ctx, "fastmail_folder_create", {"account_id": "acct-test", "name": "Nope"}))
    assert run(host, failed)["error"]["code"] == "external_failed"
    assert len(host.engine.posts) == 2


def test_a_receipt_for_another_request_account_or_action_never_completes_a_job(host):
    ctx = agent_context(host, host.ordubashy)
    host.engine.mode = "lost_after_record"
    job = approved_job(host, call(host, ctx, "fastmail_send", send_arguments()))
    run(host, job)
    good = host.engine.receipts[job]
    for tampered in ({"digest": "0" * 64}, {"account_id": "another"}, {"action": "create_draft"}, {"state": "mystery"}):
        host.engine.receipts[job] = {**good, **tampered}
        assert run(host, job)["error"]["code"] == "external_result_invalid", tampered
        host.runtime.background  # the job is terminal; re-queue the same state for the next case
        with host.instance.database.transaction() as uow:
            row = uow.repository("jobs", business_id=None).get(job, required=True)
            uow.repository("jobs", business_id=None).update(job, {"state": "queued", "error_json": None, "attempts": 1},
                                                            expected_revision=row["revision"])
    assert len(host.engine.posts) == 1


def test_authority_is_rechecked_when_the_job_runs_and_a_revoked_agent_sends_nothing(host):
    ctx = agent_context(host, host.worker)
    job = approved_job(host, call(host, ctx, "fastmail_send", send_arguments()))
    revoke_capabilities(host.instance, host.worker, "manage_fastmail")
    result = run(host, job)
    assert result["error"]["code"] in {"forbidden", "policy_denied"} and state(host, job) == "failed"
    assert host.engine.posts == []


def test_an_unreadable_engine_token_sends_nothing_and_ends_as_a_definite_refusal(host, tmp_path):
    ctx = agent_context(host, host.ordubashy)
    job = approved_job(host, call(host, ctx, "fastmail_send", send_arguments()))
    host.extension.config.admin_token_file.unlink()
    host.extension._client = None
    for _ in range(7):
        assert run(host, job)["state"] == "queued"
    ended = run(host, job)
    assert ended["error"]["code"] == "provider_unavailable" and "nothing was sent" in ended["error"]["message"]
    assert host.engine.posts == []


def test_the_same_idempotency_key_queues_one_job_and_one_request(host):
    ctx = agent_context(host, host.ordubashy)
    first = call(host, ctx, "fastmail_send", send_arguments(), key="same-key")
    again = call(host, ctx, "fastmail_send", send_arguments(), key="same-key")
    assert approved_job(host, first) == approved_job(host, again) and len(jobs(host)) == 1
    run(host, approved_job(host, first))
    assert len(host.engine.posts) == 1
