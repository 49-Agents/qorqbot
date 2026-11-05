"""The engine client against the fake engine transport: error mapping, receipts, digests and the no-resend rule."""

import hashlib
import json

import httpx
import pytest
from qorqut_operations.contracts import DomainError

from fake_engine import TOKEN, FakeEngine
from qorqut_fastmail.client import EngineClient, action_digest
from qorqut_fastmail.config import FastmailConfig


@pytest.fixture
def engine():
    return FakeEngine()


def client(engine, tmp_path=None):
    return EngineClient(FastmailConfig("http://127.0.0.1:8787", "/unused", None), client_factory=engine.client_factory, token=TOKEN)


def test_reads_use_the_account_api_with_the_bearer_token(engine):
    api = client(engine)
    assert api.capabilities()["account_id"] == "acct-test"
    assert [item["id"] for item in api.identities()["identities"]] == ["id-1", "id-2", "id-3"]
    assert api.folders()["folders"][0]["id"] == "inbox"
    assert api.search({"q": "hello"})["query"] == {"q": "hello"}
    assert api.message("m-1")["id"] == "m-1" and api.thread("t-1")["thread_id"] == "t-1"
    assert {path.split("/")[1] for _, path in engine.requests} == {"fastmail"}
    with pytest.raises(DomainError) as missing:
        api.message("absent")
    assert missing.value.code == "not_found"


def test_the_digest_is_the_hash_of_the_exact_posted_object(engine):
    api = client(engine)
    payload = {"account_id": "acct-test", "name": "Receipts"}
    receipt = api.mutation("job-1", "folder_create", payload)
    assert receipt["digest"] == action_digest("job-1", "folder_create", payload)
    expected = hashlib.sha256(json.dumps({**payload, "request_id": "job-1", "action": "folder_create"}, sort_keys=True,
                                         ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    assert receipt["digest"] == expected
    # Re-reading is by request id and never posts.
    assert api.receipt("job-1") == receipt and len(engine.posts) == 1
    assert api.receipt("never") is None


def test_a_prepared_receipt_is_accepted_as_202_and_other_failures_keep_their_meaning(engine):
    api = client(engine)
    engine.mode = "prepared"
    assert api.mutation("job-2", "folder_create", {"account_id": "acct-test", "name": "x"})["state"] == "prepared"
    engine.mode = None
    with pytest.raises(DomainError) as denied:
        api.mutation("job-3", "send", {"account_id": "acct-test", "identity_id": "id-1", "from_address": "other@example.test",
                                       "to": ["a@example.test"], "subject": "s", "body": "b"})
    assert denied.value.code == "sender_identity_denied"
    with pytest.raises(DomainError) as other:
        api.mutation("job-4", "folder_create", {"account_id": "another-account", "name": "x"})
    assert other.value.code == "account_mismatch"
    engine.mode = "server_error"
    with pytest.raises(DomainError) as outage:
        api.mutation("job-5", "folder_create", {"account_id": "acct-test", "name": "x"})
    assert outage.value.code == "email_unavailable" and outage.value.retryable
    engine.mode = "lost_before_record"
    with pytest.raises(DomainError) as lost:
        api.mutation("job-6", "folder_create", {"account_id": "acct-test", "name": "x"})
    assert lost.value.code == "email_unavailable"


def test_reads_report_an_unreachable_account_as_unavailable_and_a_wrong_token_as_owner_attention(engine):
    api = client(engine)
    down = EngineClient(FastmailConfig("http://127.0.0.1:8787", "/unused"), token=TOKEN, client_factory=lambda **options: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(502, json={})), base_url=options["base_url"]))
    with pytest.raises(DomainError) as outage:
        down.capabilities()
    assert outage.value.code == "operation_unavailable"
    wrong = EngineClient(FastmailConfig("http://127.0.0.1:8787", "/unused"), client_factory=engine.client_factory, token="w" * 40)
    with pytest.raises(DomainError) as unauthorized:
        wrong.capabilities()
    assert unauthorized.value.code == "external_auth_failed"


def test_the_token_is_read_lazily_once_from_its_private_file(engine, tmp_path):
    file = tmp_path / "token"
    config = FastmailConfig("http://127.0.0.1:8787", file)
    api = EngineClient(config, client_factory=engine.client_factory)
    with pytest.raises(DomainError):
        api.capabilities()                # nothing exists yet: unavailable, nothing was requested
    assert engine.requests == []
    file.write_text(TOKEN + "\n")
    file.chmod(0o600)
    assert api.capabilities()["account_id"] == "acct-test"
    file.unlink()
    assert api.capabilities()["account_id"] == "acct-test"      # cached after the first successful read


def test_attachments_are_ranged_and_the_route_interface_keeps_engine_refusals(engine):
    api = client(engine)
    chunk = api.attachment("a-1", "m-1", offset=10, limit=100)
    assert chunk["content"] == engine.attachment[10:110] and chunk["headers"]["x-attachment-total"] == str(len(engine.attachment))
    assert api.route_change("ensure", {"client_id": "paper"})["ready"] is True
    with pytest.raises(DomainError) as refused:
        api.route_change("ensure", {"refuse": True})
    assert refused.value.code == "route_conflict"
    with pytest.raises(DomainError):
        api.route_change("sideways", {})
    assert api.routes("paper") == []
