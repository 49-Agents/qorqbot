"""Ordubashy's real role, host policy and Context queue on disposable instances."""

import json

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from context_completion_support import Runner
from context_edit_support import build
from host_foundation_support import create_worker
from test_context_supervised_edit import arguments_of, plan_of, put_node, put_note


@pytest.fixture
def host(tmp_path):
    return build(tmp_path)


@pytest.mark.parametrize("business", [None, "first"])
def test_configured_ordubashy_upload_preserves_scope_source_and_idempotency(host, business):
    agent = host.agent(business)
    args = {"body": "Exact owner evidence.\n", "title": "Owner evidence"}
    result = host.data(agent, "context_upload", args, key="same-upload")
    assert host.data(agent, "context_upload", args, key="same-upload") == result
    assert host.count("context_inputs") == host.count("context_source_versions") == 1
    version = host.query("SELECT * FROM context_source_versions")[0]
    assert version["business_id"] == (host.ids[business] if business else None)
    item = host.query("SELECT * FROM context_inputs")[0]
    assert item["submitted_by"] == agent.principal_id
    host.restart_driver(Runner())
    published = host.step()
    assert published["status"] == "published"
    source = json.loads(host.context.snapshot().files[version["source_path"]])
    assert source["body"] == args["body"]
    assert host.context.snapshot().events[published["context_event_id"]]["submitted_by"] == agent.principal_id


def test_global_upload_requires_the_current_configured_identity_and_policy(host):
    agent_id = host.agent_id()
    agent = host.agent(None)
    host.set_overrides(agent_id, {"context_upload": "deny"})
    assert host.error(host.agent(None), "context_upload", {"body": "Denied"}) == "policy_denied"
    host.set_overrides(agent_id, {})
    worker = create_worker(host.instance, "uploader")
    host.set_overrides(worker, {"context_upload": "allow"})
    assert host.error(host.as_agent(worker), "context_upload", {"body": "Not coordinator"}) == "forbidden"
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    assert host.error(agent, "context_upload", {"body": "No longer configured"}) == "forbidden"
    assert host.count("context_inputs") == 0
    assert host.upload(host.human(None), "Owner still permitted")["job"]["state"] == "queued"


def test_global_upload_rechecks_configured_identity_for_a_prepared_plan(host):
    agent = host.agent(None)
    documents = host.runtime.domains.documents.filing_operations
    with host.database.read() as uow:
        plan = documents.prepare("context_upload", agent, {"body": "Prepared evidence"},
                                 InvocationOptions(idempotency_key=new_id()), uow)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    with host.database.read() as uow, pytest.raises(DomainError) as error:
        documents.validate_targets(agent, plan, uow)
    assert error.value.code == "forbidden"
    assert host.count("context_inputs") == 0


def test_ordubashy_previews_and_publishes_a_direct_global_note_without_a_model_or_queue(host):
    agent = host.agent(None)
    plan = plan_of(host, put_node("p0001", "Alice"), put_note("n0001", "Alice prefers email.", ("node", "p0001")))
    arguments = arguments_of(plan, "edit1")
    preview = host.data(agent, "context_edit", {**arguments, "preview_only": True})
    assert preview["status"] == "ready" and host.head == plan["expected_revision"]
    assert host.inputs() == [] and host.context.pending() is None
    receipt = host.data(agent, "context_edit", arguments)
    assert receipt["status"] == "published" and receipt["model"] is None
    snapshot = host.context.snapshot()
    assert snapshot.notes["n0001"]["text"] == "Alice prefers email."
    assert snapshot.events["edit1"]["record_context"]["scope"] == {"kind": "global"}
    assert host.inputs() == [] and host.context.pending() is None
    host.no_model()


def test_context_module_without_host_identity_port_keeps_global_upload_owner_only(host):
    host.runtime.domains.documents.filing_operations.global_upload_agent = None
    assert host.error(host.agent(None), "context_upload", {"body": "No host authority"}) == "forbidden"
    assert host.upload(host.human(None), "Owner evidence")["job"]["state"] == "queued"


@pytest.mark.parametrize("business", [None, "first"])
def test_ordubashy_can_inspect_and_retry_the_same_blocked_instruction_in_scope(host, business):
    agent = host.agent(business)
    host.data(agent, "context_instruct", {"text": "Alice leads the pilot.", "people": ["Alice"]})
    runner = Runner()
    runner.fail = RuntimeError("temporary failure")
    host.restart_driver(runner)
    assert host.step()["status"] == "input_blocked"
    listing = host.read(agent, "context_queue_list")
    assert listing["queue_blocked"] and len(listing["items"]) == 1
    item = listing["items"][0]
    detail = host.read(agent, "context_queue_get", {"input_id": item["id"]})["input"]
    assert detail["state"] == "blocked"
    other = host.agent("second")
    assert host.read(other, "context_queue_list")["blocked_input_id"] is None
    assert host.error(other, "context_queue_get", {"input_id": item["id"]}) == "not_found"
    control = {"input_id": item["id"], "expected_state": "blocked"}
    assert host.error(other, "context_queue_retry", control) == "not_found"
    assert host.error(agent, "context_queue_discard", control) == "policy_denied"
    assert host.error(agent, "context_queue_submit", {
        "operation": "add", "path": "foundation/extra.txt", "content": "Unapproved bypass"}) == "policy_denied"
    before = host.query("SELECT * FROM context_inputs")[0]
    result = host.data(agent, "context_queue_retry", control, key="same-retry")
    assert host.data(agent, "context_queue_retry", control, key="same-retry") == result
    runner.fail = None
    assert host.step()["status"] == "published"
    after = host.query("SELECT * FROM context_inputs")[0]
    assert after["id"] == before["id"] and after["context_event_id"] == before["context_event_id"]
    assert host.count("context_inputs") == 1
    assert host.read(agent, "context_queue_get", {"input_id": item["id"]})["input"]["state"] == "published"
