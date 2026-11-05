"""Native JSON helper, real module/runtime, and a synthetic authenticated MCP peer."""
import io
import json
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from qorqut_agentic import AgenticLayer
from qorqut_agentic.runtime import LocalProcessRuntime

from bazarlyk.agentic import client as native
from bazarlyk.bootstrap.mcp_reconnect import ReconnectBridge

ATTR = {"actor_id": "fixture", "actor_kind": "owner", "reason": "synthetic setup"}
OTHER = {"actor_id": str(uuid4()), "actor_kind": "agent"}


class Peer:
    def __init__(self, fixture):
        self.fixture = fixture
        self.requests = []
        self.denied_projects = set()
        self.allowed_packages = ["sample"]
        self.manage = False
        self.failure = None
        self.override = {}

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        assert request.headers["authorization"] == "Bearer " + "x" * 40
        method = body["method"]
        if method == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "test-session"}, json={
                "jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
        if method.startswith("notifications/"):
            return httpx.Response(202)
        if method == "tools/list":
            result = {"tools": [{"name": "agentic_access", "annotations": {"readOnlyHint": True}}]}
        else:
            assert method == "tools/call" and body["params"]["name"] == "agentic_access"
            args = body["params"]["arguments"]
            if self.failure == "rpc":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                    "error": {"code": -32000, "message": "SECRET transport error"}})
            if self.failure == "tool":
                result = {"isError": True, "content": [{"type": "text", "text": "SECRET"}]}
            elif self.failure == "denied" or args.get("project_id") in self.denied_projects:
                result = {"isError": True, "content": [{"type": "text", "text": json.dumps({
                    "status": "error", "error": {"code": "forbidden", "message": "SECRET denied"}})}]}
            else:
                value = {"operation": args["operation"], "actor": self.fixture.actor,
                    "selection": {"package_ids": self.allowed_packages, "project_id": args.get("project_id"),
                                  "work_type": args.get("work_type")},
                    "root": str(self.fixture.layer.root), "state": str(self.fixture.layer.state),
                    "instance_id": self.fixture.layer.instance_id, "can_manage": self.manage,
                    "profile_revision": 1, "registration_id": self.fixture.registration_id, "attention_required": False, **self.override}
                result = {"isError": False, "content": [{"type": "text", "text": json.dumps({"status": "ok", "data": value})}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def fixture(tmp_path):
    actor = {"actor_id": str(uuid4()), "actor_kind": "agent"}
    layer = AgenticLayer.initialize(tmp_path / "root", tmp_path / "state",
        git_user={"name": "Synthetic", "email": "synthetic@example.invalid"})
    manifest = {"schema_version": 1, "id": "sample", "name": "Sample", "description": "Synthetic sample",
        "entries": [
            {"id": "echo", "kind": "tool", "description": "Echo", "when_to_use": "Test", "path": "echo.json"},
            {"id": "verify", "kind": "tool", "description": "Verify", "when_to_use": "Test", "path": "verify.json"},
            {"id": "flow", "kind": "workflow", "description": "Flow", "when_to_use": "Test", "path": "flow.json"}]}
    tool = {"schema_version": 1, "runtime": "python", "code": "echo.py",
            "input_schema": {"type": "object"}, "output_schema": {"type": "object"}}
    workflow = {"schema_version": 1, "id": "flow", "description": "Synthetic workflow",
                "steps": [{"id": "step", "instructions": "Do synthetic work", "verifier": "sample/verify"}]}
    files = {"agentic.yaml": json.dumps(manifest), "echo.json": json.dumps(tool),
        "verify.json": json.dumps({**tool, "code": "verify.py"}), "flow.json": json.dumps(workflow),
        "echo.py": "import json,os,sys\nv=json.load(sys.stdin)\nprint(json.dumps({'input':v,'secret':os.environ.get('NATIVE_SECRET'),'python':sys.executable}))\n",
        "verify.py": "import json,sys\nv=json.load(sys.stdin)\nprint(json.dumps({'passed':bool(v['output'].get('passed')),'feedback':'synthetic verdict'}))\n"}
    layer.authoring.apply(ATTR, [{"op": "create", "path": "packages/sample/" + path, "content": body}
                                 for path, body in files.items()])
    workspace = tmp_path / "native"
    (workspace / ".bazarlyk").mkdir(parents=True)
    registration_id = str(uuid4())
    path = workspace / ".bazarlyk/registration.json"
    path.write_text(json.dumps({"schema_version": 1, "registration_id": registration_id, "agent_id": actor["actor_id"],
        "workspace": str(workspace), "service_url": "http://synthetic.invalid", "credential_file": ".bazarlyk/credential"}))
    credential = path.with_name("credential")
    credential.write_text("x" * 40); credential.chmod(0o600)
    fixture = SimpleNamespace(layer=layer, actor=actor, registration_id=registration_id, registration=path)
    fixture.peer = Peer(fixture)
    return fixture


def call(fixture, operation, arguments=None, context=None, *, raw=None):
    request = {"operation": operation, "arguments": arguments or {}}
    if context is not None:
        request["context"] = context
    transport = httpx.Client(transport=httpx.MockTransport(fixture.peer), trust_env=False)
    bridge = ReconnectBridge(fixture.registration, client=transport)
    output = io.BytesIO()
    code = native.run(fixture.registration, stream=io.BytesIO(raw if raw is not None else json.dumps(request).encode()),
                      output=output, bridge=bridge)
    value = json.loads(output.getvalue())
    assert code == (0 if value["ok"] else 1)
    return value


def test_real_local_tool_has_current_interpreter_empty_environment_and_own_receipt(fixture, monkeypatch):
    monkeypatch.setenv("NATIVE_SECRET", "must never reach tool")
    result = call(fixture, "tools.invoke", {"ref": "sample/echo", "arguments": {"hello": "world"}, "invocation_id": "echo-1"})
    assert result["ok"], result
    receipt = result["result"]["receipt"]
    assert receipt["status"] == "succeeded" and receipt["actor"] == fixture.actor
    assert receipt["output"] == {"input": {"hello": "world"}, "secret": None, "python": os.path.normpath(sys.executable)}
    assert [request["method"] for request in fixture.peer.requests] == ["initialize", "notifications/initialized", "tools/call"]
    replay = call(fixture, "tools.invoke", {"ref": "sample/echo", "arguments": {"hello": "world"}, "invocation_id": "echo-1"})
    assert replay["result"]["receipt"]["replayed"] is True
    fixture.peer.allowed_packages = []
    assert call(fixture, "tools.receipt", {"invocation_id": "echo-1"})["ok"]
    assert not call(fixture, "tools.invoke", {"ref": "sample/echo", "arguments": {}, "invocation_id": "echo-2"})["ok"]
    assert fixture.layer.tools.receipt("echo-1")["receipt"]["output"]["secret"] is None


@pytest.mark.parametrize("field", sorted(native._OVERRIDES))
def test_request_cannot_override_native_authority(fixture, field):
    result = call(fixture, "catalog.list", {field: "forged"})
    assert result["error"]["code"] == "invalid_request" and fixture.peer.requests == []


@pytest.mark.parametrize("payload", [
    {"operation": "lifecycle.initialize", "arguments": {}},
    {"operation": "lifecycle.open", "arguments": {}},
    {"operation": "catalog.list", "arguments": {}, "actor": "forged"},
    {"operation": "catalog.list", "arguments": {}, "context": {"root": "forged"}},
    {"operation": "authoring.apply", "arguments": {"attribution": {"reason": "test", "actor_id": "forged"}}},
])
def test_invalid_request_fails_before_mcp(fixture, payload):
    assert call(fixture, None, raw=json.dumps(payload).encode())["error"]["code"] == "invalid_request"
    assert fixture.peer.requests == []


@pytest.mark.parametrize("failure", ["rpc", "tool", "denied"])
def test_refused_access_never_opens_module_or_echoes_credentials(fixture, failure, monkeypatch):
    fixture.peer.failure = failure
    monkeypatch.setattr(native.AgenticLayer, "open", lambda *a, **k: pytest.fail("opened before successful access"))
    result = call(fixture, "catalog.list")
    assert not result["ok"] and "SECRET" not in json.dumps(result) and "x" * 40 not in json.dumps(result)


@pytest.mark.parametrize("override", [{"actor": OTHER}, {"registration_id": "wrong"}, {"instance_id": "inst_" + "0" * 32},
                                     {"root": "relative"}, {"selection": {"package_ids": None}}])
def test_wrong_binding_fails_closed(fixture, override):
    fixture.peer.override = override
    assert not call(fixture, "catalog.list")["ok"]


def test_attribution_is_canonical_and_management_is_explicit(fixture):
    args = {"attribution": {"reason": "Direct owner instruction", "source_refs": ["owner:synthetic"]},
            "changes": [{"op": "create", "path": "notes/test.md", "content": "hello"}]}
    assert not call(fixture, "authoring.apply", args)["ok"]
    assert not (fixture.layer.root / "notes/test.md").exists()
    fixture.peer.manage = True
    result = call(fixture, "authoring.apply", args)
    assert result["ok"], result
    change = result["result"]["change"]
    assert change["attribution"]["actor_id"] == fixture.actor["actor_id"]
    assert change["attribution"]["trust"] == "host_authenticated"


def test_scan_is_management_only_and_never_claims_to_know_the_writer(fixture):
    (fixture.layer.root / "observed.md").write_text("Synthetic direct filesystem edit")
    assert not call(fixture, "changes.scan")["ok"]
    fixture.peer.manage = True
    before = len(fixture.peer.requests)
    for attribution in (None, {"reason": "claiming a writer"}):
        denied = call(fixture, "changes.scan", {"attribution": attribution})
        assert denied["error"]["code"] == "invalid_request" and len(fixture.peer.requests) == before
    scanned = call(fixture, "changes.scan")
    assert scanned["ok"], scanned
    change = fixture.layer.changes.list()["changes"][-1]
    assert change["paths"] == ["observed.md"]
    assert change["attribution"]["actor_id"] == "external/unknown"
    assert change["attribution"]["actor_kind"] == "external"
    assert change["attribution"]["trust"] == "external_unknown"


def start(fixture, *, context=None, operation_id="start"):
    result = call(fixture, "workflows.start", {"ref": "sample/flow", "operation_id": operation_id}, context)
    assert result["ok"], result
    return result["result"]["run"]


def test_workflow_real_verifier_and_blocked_attempt(fixture):
    run = start(fixture)
    args = {"run_id": run["run_id"], "step_id": "step", "operation_id": "complete", "output": {"passed": True}}
    completed = call(fixture, "workflows.complete_step", args)
    assert completed["ok"], completed
    assert completed["result"]["run"]["state"] == "completed"
    assert completed["result"]["run"]["steps"][0]["verification"] == "verified"
    run = start(fixture, operation_id="other-start")
    definition = fixture.layer.root / "packages/sample/verify.json"
    data = json.loads(definition.read_text()); data["runtime"] = "unregistered"
    definition.write_text(json.dumps(data))
    args.update(run_id=run["run_id"], operation_id="blocked")
    result = call(fixture, "workflows.complete_step", args)
    assert result["error"]["code"] == "verification_blocked"
    assert result["error"]["details"]["attempt_recorded"]
    assert fixture.layer.workflows.get(run["run_id"])["run"]["steps"][0]["verification"] == "blocked"


def test_progress_checks_only_frozen_package_and_relevant_verifier(fixture):
    import hashlib
    layer = fixture.layer
    path = layer.root / "packages/sample/flow.json"
    flow = json.loads(path.read_text())
    flow["steps"] = [{"id": "plain", "instructions": "Frozen plain instruction"},
                     {"id": "checked", "instructions": "Frozen verified instruction", "depends_on": ["plain"],
                      "verifier": "verifiers/check"}]
    manifest = {"schema_version": 1, "id": "verifiers", "name": "Verifiers", "description": "Synthetic",
                "entries": [{"id": "check", "kind": "tool", "description": "Check", "when_to_use": "Verify", "path": "check.json"}]}
    layer.authoring.apply(ATTR, [
        {"op": "create", "path": "packages/verifiers/agentic.yaml", "content": json.dumps(manifest)},
        {"op": "create", "path": "packages/verifiers/check.json", "content": (layer.root / "packages/sample/verify.json").read_text()},
        {"op": "create", "path": "packages/verifiers/verify.py", "content": (layer.root / "packages/sample/verify.py").read_text()},
        {"op": "edit", "path": "packages/sample/flow.json", "content": json.dumps(flow),
         "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}])
    fixture.peer.allowed_packages = ["sample", "verifiers", "unrelated"]
    run = start(fixture)
    # An edit of the current definition never rewrites the saved instructions.
    current = path.read_bytes()
    flow["steps"][0]["instructions"] = "New instruction for new runs"
    layer.authoring.apply(ATTR, [{"op": "edit", "path": "packages/sample/flow.json",
        "content": json.dumps(flow), "expected_sha256": hashlib.sha256(current).hexdigest()}])
    fixture.peer.allowed_packages = ["sample"]
    args = {"run_id": run["run_id"], "step_id": "plain", "operation_id": "plain-complete"}
    completed = call(fixture, "workflows.complete_step", args)
    assert completed["ok"], completed
    assert completed["result"]["run"]["steps"][0]["instructions"] == "Frozen plain instruction"
    # Removing an unrelated original package did not block the plain step;
    # removing the actual next verifier's package blocks that execution.
    denied = call(fixture, "workflows.complete_step", {"run_id": run["run_id"], "step_id": "checked",
        "operation_id": "denied-check", "output": {"passed": True}})
    assert denied["error"]["code"] == "unavailable"
    assert fixture.layer.workflows.get(run["run_id"])["run"]["steps"][1]["attempts"] == []


def test_main_executes_one_real_tool_over_mocked_authenticated_mcp(fixture, monkeypatch):
    output = io.BytesIO()
    request = {"operation": "tools.invoke", "arguments": {"ref": "sample/echo", "arguments": {"cli": True},
                                                            "invocation_id": "from-cli"}}
    monkeypatch.setattr(native.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())))
    monkeypatch.setattr(native.sys, "stdout", SimpleNamespace(buffer=output))
    factory = native.ReconnectBridge
    monkeypatch.setattr(native, "ReconnectBridge", lambda path: factory(path,
        client=httpx.Client(transport=httpx.MockTransport(fixture.peer), trust_env=False)))
    assert native.main(["--registration", str(fixture.registration)]) == 0
    result = json.loads(output.getvalue())
    assert result["result"]["receipt"]["output"]["input"] == {"cli": True}
    assert result["result"]["receipt"]["actor"] == fixture.actor


def test_main_rejects_runtime_flags_without_echoing_values(fixture, monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(native.sys, "stdout", output)
    assert native.main(["--registration", str(fixture.registration), "--runtime", "SECRET"]) == 1
    result = json.loads(output.getvalue())
    assert result["error"]["code"] == "invalid_request" and "SECRET" not in output.getvalue()
    assert fixture.peer.requests == []


def test_actor_filtered_histories_remain_available_after_revocation(fixture):
    fixture.layer.workflows.start("sample/flow", OTHER, operation_id="foreign")
    revoked_project = str(uuid4())
    revoked = start(fixture, context={"project_id": revoked_project}, operation_id="revoked")
    visible = start(fixture, operation_id="visible")
    fixture.peer.denied_projects.add(revoked_project)
    result = call(fixture, "workflows.list", {"limit": 1}, {"project_id": revoked_project})
    assert result["ok"], result
    assert result["result"]["total"] == 1
    assert [r["run_id"] for r in result["result"]["runs"]] == [revoked["run_id"]]
    assert call(fixture, "workflows.list", {"limit": 1, "offset": 1}, {"project_id": revoked_project})["result"]["runs"] == []
    own = call(fixture, "workflows.list")["result"]
    assert own["total"] == 2 and {r["run_id"] for r in own["runs"]} == {visible["run_id"], revoked["run_id"]}
    assert call(fixture, "workflows.get", {"run_id": revoked["run_id"]}, {"project_id": revoked_project})["ok"]
    assert call(fixture, "workflows.resume", {"run_id": revoked["run_id"], "operation_id": "resume"})["error"]["code"] == "forbidden"
    for operation in ("supply_arguments", "start_step", "complete_step"):
        assert call(fixture, "workflows." + operation, {"run_id": revoked["run_id"], "operation_id": operation})["error"]["code"] == "forbidden"
    assert call(fixture, "workflows.pause", {"run_id": revoked["run_id"], "operation_id": "pause"})["ok"]
    assert call(fixture, "workflows.cancel", {"run_id": revoked["run_id"], "operation_id": "cancel"})["ok"]


def test_package_revocation_and_cross_actor_never_allow_progress(fixture):
    run = start(fixture)
    foreign = fixture.layer.workflows.start("sample/flow", OTHER, operation_id="foreign")["run"]
    fixture.peer.manage = True
    for operation in ("get", "pause", "cancel", "resume"):
        assert not call(fixture, "workflows." + operation,
                        {"run_id": foreign["run_id"], **({} if operation == "get" else {"operation_id": "no-takeover"})})["ok"]
    fixture.peer.allowed_packages = []
    assert call(fixture, "workflows.get", {"run_id": run["run_id"]})["ok"]
    assert not call(fixture, "workflows.complete_step", {"run_id": run["run_id"], "step_id": "step", "operation_id": "no"})["ok"]
    assert call(fixture, "workflows.pause", {"run_id": run["run_id"], "operation_id": "pause"})["ok"]
    own = call(fixture, "workflows.list")["result"]
    assert own["total"] == 1 and own["runs"][0]["run_id"] == run["run_id"]


@pytest.mark.parametrize("operation", ["get", "pause", "cancel", "resume", "supply_arguments", "start_step", "complete_step"])
def test_explicit_contradictory_context_never_substitutes_for_saved_context(fixture, operation):
    project = str(uuid4())
    run = start(fixture, context={"project_id": project, "work_type": "review"})
    arguments = {"run_id": run["run_id"], "operation_id": "conflicting"}
    if operation == "get":
        arguments.pop("operation_id")
    result = call(fixture, "workflows." + operation, arguments, {"project_id": str(uuid4())})
    assert result["error"]["code"] == "invalid_request"
    result = call(fixture, "workflows." + operation, arguments, {"work_type": "unrelated"})
    assert result["error"]["code"] == "invalid_request"


def test_foreign_tool_receipts_are_never_visible_even_to_manager(fixture):
    layer = AgenticLayer.open(fixture.layer.root, fixture.layer.state,
                             runtimes={"python": LocalProcessRuntime({"python": sys.executable}, env={})})
    layer.tools.invoke("sample/echo", {}, "foreign-receipt", OTHER)
    fixture.peer.manage = True
    assert not call(fixture, "tools.receipt", {"invocation_id": "foreign-receipt"})["ok"]
    assert call(fixture, "tools.reconcile", {"invocation_id": "foreign-receipt", "resolution": "no_effect_confirmed", "note": "test"})["error"]["code"] == "unavailable"


def test_own_interrupted_receipt_remains_reconcilable_after_revocation(fixture, monkeypatch):
    class Interrupted(BaseException):
        pass
    attempts = []
    def interrupted(runtime, request):
        attempts.append(request)
        raise Interrupted("synthetic process interruption")
    monkeypatch.setattr(LocalProcessRuntime, "execute", interrupted)
    with pytest.raises(Interrupted):
        call(fixture, "tools.invoke", {"ref": "sample/echo", "arguments": {}, "invocation_id": "uncertain"})
    assert fixture.layer.tools.receipt("uncertain")["receipt"]["status"] == "in_flight"
    fixture.peer.allowed_packages = []
    result = call(fixture, "tools.reconcile", {"invocation_id": "uncertain", "resolution": "no_effect_confirmed",
                                               "note": "Synthetic runtime did not start a process"})
    assert result["ok"] and result["result"]["receipt"]["status"] == "reconciled"
    assert result["result"]["receipt"]["reconciliation"]["actor"] == fixture.actor
    assert len(attempts) == 1


def test_custom_fixture_test_runs_locally(fixture):
    result = call(fixture, "tools.test", {"ref": "sample/echo", "cases": [
        {"name": "synthetic echo", "input": {}, "expect": {"status": "succeeded"}}]})
    assert result["ok"] and result["result"]["passed"]
    receipt = fixture.layer.tools.receipt(result["result"]["cases"][0]["invocation_id"])["receipt"]
    assert receipt["actor"] == fixture.actor and receipt["purpose"] == "test"


@pytest.mark.parametrize("raw", [b'[]', b'{} {}', b'\xff', b'{"operation":"catalog.list","arguments":{},"arguments":{}}',
                                 b'{"operation":"catalog.list","arguments":{"limit":NaN}}'])
def test_invalid_json_is_bounded_and_never_looks_up_access(fixture, raw):
    assert call(fixture, None, raw=raw)["error"]["code"] == "invalid_request"
    assert fixture.peer.requests == []


def test_input_bound_and_unavailable_catalog_disclosure(fixture, monkeypatch):
    monkeypatch.setattr(native, "MAX_INPUT_BYTES", 5)
    assert call(fixture, None, raw=b"x" * 6)["error"]["code"] == "limit_exceeded"
    assert fixture.peer.requests == []
    monkeypatch.setattr(native, "MAX_INPUT_BYTES", 1000)
    assert call(fixture, "catalog.list", {"include_unavailable": True})["error"]["code"] == "invalid_request"


def test_no_retry_after_uncertain_local_effect(fixture, monkeypatch):
    fixture.peer.manage = True
    original = native.dispatch
    attempts = []
    def lose_reply(layer, request):
        result = original(layer, request)
        attempts.append(result)
        raise RuntimeError("SECRET lost reply")
    monkeypatch.setattr(native, "dispatch", lose_reply)
    result = call(fixture, "authoring.apply", {"attribution": {"reason": "owner instructed"},
        "changes": [{"op": "create", "path": "notes/once.md", "content": "one"}]})
    assert not result["ok"] and result["error"]["details"]["outcome_unknown"]
    assert len(attempts) == 1 and "SECRET" not in json.dumps(result)
    assert (fixture.layer.root / "notes/once.md").read_text() == "one"
    assert fixture.layer.changes.list()["changes"][-1]["files"][0]["path"] == "notes/once.md"


def test_bounded_output_after_success_is_not_reported_as_unexecuted(fixture, monkeypatch):
    monkeypatch.setattr(native, "MAX_OUTPUT_BYTES", 100)
    result = call(fixture, "catalog.list")
    assert result["error"]["code"] == "limit_exceeded"
    assert result["error"]["details"]["operation_completed"] is True


def _proposal():
    from uuid import uuid4
    scope = {"description": "synthetic", "project_ids": [], "work_types": [], "workflow_refs": [],
             "conditions": [], "exceptions": []}
    source = {"provider": "transcript", "kind": "session", "record_id": "synthetic-session", "revision": "1",
              "content_hash": "sha256:" + "0" * 64, "origin_event_id": None, "episode_id": None,
              "actor_id": None, "actor_kind": None, "occurred_at": None,
              "locator": {"kind": "synthetic"}, "scope": dict(scope),
              "provenance": {"producer_id": None, "original_refs": [], "overseer_origin": False},
              "availability": "available", "namespace": {"kind": "global", "business_id": None}}
    return {"schema_version": 1, "id": str(uuid4()), "extraction_run_id": "synthetic-run",
            "summary": "Synthetic proposal", "change_direction": "Add a synthetic rule",
            "observed_fact": "The owner said something", "interpretation": "It is a rule",
            "scope": dict(scope), "target_refs": [], "origin_type": "decision",
            "sources": [{"ref": source, "role": "supporting"}],
            "created_at": "2026-01-01T00:00:00Z", "created_by": "fixture"}


def test_native_client_resolves_allowlisted_nested_overseer_operations(fixture):
    fixture.peer.manage = True
    read = call(fixture, "overseer.proposals.list", {"limit": 5})
    assert read["ok"] and read["result"]["items"] == [], read
    record = _proposal()
    fixture.peer.manage = True
    created = call(fixture, "overseer.proposals.record", {"record": record, "operation_id": "proposal-1"})
    assert created["ok"], created
    replay = call(fixture, "overseer.proposals.record", {"record": record, "operation_id": "proposal-1"})
    assert replay["ok"] and replay["result"] == created["result"]
    changed = call(fixture, "overseer.proposals.record",
                   {"record": {**record, "summary": "Different"}, "operation_id": "proposal-1"})
    assert changed["error"]["code"] == "operation_conflict"
    # Access is still checked before the local module opens: a denied mutation writes nothing.
    with fixture.layer._ctx.store.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM proposed_deltas").fetchone()[0] == 1
    fixture.peer.manage = False
    denied = call(fixture, "overseer.proposals.record", {"record": _proposal(), "operation_id": "proposal-2"})
    assert not denied["ok"] and denied["error"]["code"] in {"forbidden", "unavailable"}
    with fixture.layer._ctx.store.reader() as conn:
        assert conn.execute("SELECT COUNT(*) FROM proposed_deltas").fetchone()[0] == 1


def test_nested_overseer_mutation_marks_an_uncertain_local_effect(fixture, monkeypatch):
    fixture.peer.manage = True
    real_dispatch = native.dispatch

    def crash(layer, request):
        raise RuntimeError("synthetic local crash after dispatch")

    monkeypatch.setattr(native, "dispatch", crash)
    result = call(fixture, "overseer.proposals.record", {"record": _proposal(), "operation_id": "proposal-crash"})
    assert result["error"]["code"] == "internal_error"
    assert result["error"]["details"]["outcome_unknown"] is True
    monkeypatch.setattr(native, "dispatch", real_dispatch)
    read = call(fixture, "overseer.proposals.list", {"limit": 5})
    assert read["ok"]


def test_bounded_workflow_evidence_checks_receipt_with_long_event_history(fixture):
    from bazarlyk.agentic.attention import workflow_evidence
    from qorqut_operations.contracts import DomainError
    selection = {'package_ids': ['sample']}
    result = fixture.layer.workflows.start('sample/flow', fixture.actor, selection=selection, operation_id='evidence-start')
    identity = result['run']['run_id']
    with fixture.layer.workflows.ctx.store.transaction() as connection:
        connection.executemany('INSERT INTO run_events(run_id,ordinal,at,type,actor,payload) VALUES(?,?,?,?,?,?)',
            ((identity, ordinal, '2026-01-01T00:00:00Z', 'synthetic', json.dumps(fixture.actor), '{}')
             for ordinal in range(2, 5002)))
    evidence = workflow_evidence(fixture.layer, 'workflows.start', 'evidence-start', identity, fixture.actor)
    assert evidence['run_id'] == identity and evidence['ordinal'] == 1 and evidence['current_ordinal'] == 5001 and evidence['state'] == 'running'
    with pytest.raises(DomainError) as error:
        workflow_evidence(fixture.layer, 'workflows.pause', 'evidence-start', identity, fixture.actor)
    assert error.value.code == 'workflow_unavailable'
    with pytest.raises(DomainError):
        workflow_evidence(fixture.layer, 'workflows.start', 'missing', identity, fixture.actor)


def test_preflight_refuses_a_previously_reserved_start(fixture):
    from bazarlyk.agentic.attention import WorkflowAttention
    from qorqut_operations.contracts import DomainError
    fixture.layer.workflows.start('sample/flow', fixture.actor, selection={'package_ids': ['sample']}, operation_id='old-start')
    app = SimpleNamespace(ordubashy_attention=object())
    runtime = SimpleNamespace(application=app, agentic_layer=fixture.layer)
    service = WorkflowAttention(runtime, key=b'x' * 32)
    service.caller = lambda ctx, consumer, generation, uow: ctx
    args = {'scope': 'global', 'operation': 'workflows.start', 'operation_id': 'old-start',
            'consumer_id': 'session', 'binding_generation': 1}
    with pytest.raises(DomainError) as error:
        service.context(SimpleNamespace(principal_id=fixture.actor['actor_id']), args, None, object())
    assert error.value.code == 'workflow_context_unavailable'
