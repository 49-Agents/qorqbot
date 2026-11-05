"""Combined facade/JSON contracts, also runnable against an installed wheel.

Set QORQUT_AGENTIC_TEST_CONSOLE to the installed console entry point and clear
pytest's pythonpath setting to exercise the wheel outside the source checkout.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import qorqut_agentic
from qorqut_agentic import AgenticError, AgenticLayer
from qorqut_agentic.runtime import LocalProcessRuntime
from tests.conftest import ATTR, EXAMPLES, GIT_USER, git

ACTOR = {"actor_id": "integration-owner", "actor_kind": "owner"}
SELECTION = {"package_ids": ["example-project-workflow"]}
REF = "example-project-workflow/landing-page"
TOOL = "example-project-workflow/normalize-results"


def process_env():
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONPATH", None)
    if not os.environ.get("QORQUT_AGENTIC_TEST_CONSOLE"):
        env["PYTHONPATH"] = str(Path(qorqut_agentic.__file__).resolve().parent.parent)
    return env


class PublicClient:
    def __init__(self, base, mode):
        self.base, self.mode = base, mode
        self.root, self.state = base / "root", base / "state"
        self.config = base / "runtime.json"
        self.config.write_text(json.dumps({"local_process": {
            "interpreters": {"python": sys.executable}, "env": {}}}))

    def call(self, operation, *, runtime=True, **arguments):
        if self.mode == "cli":
            console = os.environ.get("QORQUT_AGENTIC_TEST_CONSOLE")
            command = [console] if console else [sys.executable, "-m", "qorqut_agentic"]
            command += ["--root", str(self.root), "--state", str(self.state)]
            if runtime:
                command += ["--runtime-config", str(self.config)]
            process = subprocess.run(command, input=json.dumps({"operation": operation, "arguments": arguments}),
                                     text=True, capture_output=True, cwd=self.base, env=process_env(), timeout=30)
            assert process.stderr == ""
            result = json.loads(process.stdout)
            assert process.returncode == (0 if result["ok"] else 1)
            return result
        runtimes = {"python": LocalProcessRuntime({"python": sys.executable}, env={})} if runtime else {}
        try:
            if operation == "lifecycle.initialize":
                layer = AgenticLayer.initialize(self.root, self.state, runtimes=runtimes, **arguments)
                result = {"root": str(layer.root), "state": str(layer.state),
                          "recovery_required": layer.recovery_required}
            else:
                # Reopen on every call, using only the public facade.
                layer = AgenticLayer.open(self.root, self.state, runtimes=runtimes)
                assert layer.workflows is layer.workflows
                component, method = operation.split(".")
                result = getattr(getattr(layer, component), method)(**arguments)
            return {"ok": True, "result": result}
        except AgenticError as error:
            return {"ok": False, "error": error.to_dict()}

    def ok(self, operation, **arguments):
        result = self.call(operation, **arguments)
        assert result["ok"], result
        return result["result"]


@pytest.fixture(params=["facade", "cli"])
def client(tmp_path, request):
    client = PublicClient(tmp_path, request.param)
    client.ok("lifecycle.initialize", git_user=GIT_USER)
    assert git(client.root, "branch", "--show-current").stdout.strip() == "agentic"
    client.ok("authoring.import_package", attribution=ATTR, package_id="example-project-workflow",
              snapshot={"directory": str(EXAMPLES / "example-project-workflow")})
    return client


def ready(run):
    return {step["step_id"] for step in run["ready_steps"]}


def prepare_assembly(client):
    started = client.ok("workflows.start", ref=REF, actor=ACTOR, selection=SELECTION, operation_id="start")
    run = started["run"]
    rid = run["run_id"]
    assert run["state"] == "waiting_arguments" and ready(run) == set()
    assert {item["name"] for item in run["argument_requests"]} == {"product_name", "audience"}
    assert run["arguments"]["tone"]["value"] == "plain"
    assert run["arguments"]["tone"]["confirmed"] is False
    assert client.ok("workflows.start", ref=REF, actor=ACTOR, selection=SELECTION, operation_id="start") == started
    unconfirmed = client.call("workflows.supply_arguments", run_id=rid, actor=ACTOR, operation_id="bad-confirmation",
                             arguments={"audience": {"value": "Designers", "source": "synthetic-owner"}})
    assert unconfirmed["ok"] is False
    run = client.ok("workflows.supply_arguments", run_id=rid, actor=ACTOR, operation_id="inputs", arguments={
        "product_name": {"value": "Example", "source": "synthetic-owner"},
        "audience": {"value": "Designers", "source": "synthetic-owner", "confirmed": True}})["run"]
    assert ready(run) == {"brief"}
    assert client.ok("workflows.start_step", run_id=rid, step_id="brief", actor=ACTOR,
                     operation_id="begin-brief")["run"]["steps"][0]["state"] == "active"
    run = client.ok("workflows.complete_step", run_id=rid, step_id="brief", actor=ACTOR,
                    operation_id="brief", completion_note="Synthetic brief prepared")["run"]
    assert run["steps"][0]["verification"] == "declared"
    assert ready(run) == {"collect-assets"}
    # The independent step can finish while draft-copy awaits a derived input.
    run = client.ok("workflows.complete_step", run_id=rid, step_id="collect-assets", actor=ACTOR,
                    operation_id="assets", output={"images": []})["run"]
    assert ready(run) == set()
    assert {item["name"] for item in run["argument_requests"]} == {"primary_keyword"}
    run = client.ok("workflows.supply_arguments", run_id=rid, actor=ACTOR, operation_id="derive", arguments={
        "primary_keyword": {"value": "example", "source": "synthetic-brief", "note": "From the accepted brief"}})["run"]
    assert ready(run) == {"draft-copy"}
    run = client.ok("workflows.complete_step", run_id=rid, step_id="draft-copy", actor=ACTOR,
                    operation_id="copy", output={"headline": "Example"})["run"]
    assert ready(run) == set()
    assert {item["name"] for item in run["argument_requests"]} == {"launch_date"}
    client.ok("workflows.supply_arguments", run_id=rid, actor=ACTOR, operation_id="date", arguments={
        "launch_date": {"value": "2026-10-26", "source": "synthetic-owner"}})
    return rid


def test_public_workflow_completion_reopen_and_retry(client):
    rid = prepare_assembly(client)
    arguments = dict(run_id=rid, step_id="assemble", actor=ACTOR, operation_id="assemble",
                     completion_note="Synthetic outline", output={"title": "Example", "sections": ["hero", "proof", "cta"]})
    completed = client.ok("workflows.complete_step", **arguments)
    assert completed["run"]["state"] == "completed"
    step = completed["run"]["steps"][-1]
    assert step["verification"] == "verified" and len(step["attempts"]) == 1
    assert client.ok("workflows.complete_step", **arguments) == completed
    assert client.ok("workflows.get", run_id=rid)["run"] == completed["run"]
    assert client.ok("workflows.list", state="completed")["total"] == 1
    assert AgenticLayer.open(client.root, client.state).workflows.get(rid)["run"] == completed["run"]


def test_public_blocked_verification_persists_and_replays_error(client):
    rid = prepare_assembly(client)
    args = dict(run_id=rid, step_id="assemble", actor=ACTOR, operation_id="missing-runtime",
                output={"title": "Example", "sections": ["hero", "proof", "cta"]})
    blocked = client.call("workflows.complete_step", runtime=False, **args)
    assert blocked["ok"] is False
    error = blocked["error"]
    assert error["code"] == "verification_blocked"
    assert error["details"]["reason"] == "runtime_unavailable"
    assert error["details"]["attempt_recorded"] is True
    run = client.ok("workflows.get", run_id=rid)["run"]
    assert run == error["details"]["run"] and run["state"] == "running"
    attempt = run["steps"][-1]["attempts"][0]
    assert attempt["status"] == "blocked" and attempt["block_reason"] == "runtime_unavailable"
    # Registering a runtime on retry must not erase or execute the blocked attempt.
    assert client.call("workflows.complete_step", **args) == blocked
    assert len(client.ok("workflows.get", run_id=rid)["run"]["steps"][-1]["attempts"]) == 1
    args["operation_id"] = "retry-with-runtime"
    completed = client.ok("workflows.complete_step", **args)["run"]
    assert completed["state"] == "completed" and len(completed["steps"][-1]["attempts"]) == 2


def test_public_tool_receipt_and_reconcile_forward_actor_and_selection(client):
    invoked = client.ok("tools.invoke", ref=TOOL, arguments={"rows": [{"name": " Gamma ", "score": 1}]},
                        invocation_id="success", actor=ACTOR, selection=SELECTION)["receipt"]
    assert invoked["status"] == "succeeded" and invoked["output"]["rows"][0]["name"] == "gamma-ray"
    assert client.ok("tools.receipt", invocation_id="success", actor=ACTOR, selection=SELECTION)["receipt"] == invoked
    # Stop an isolated runtime after the durable in-flight receipt is reserved.
    code = """
import os, sys
from qorqut_agentic import AgenticLayer
class Interrupted:
    def execute(self, request):
        os._exit(17)
layer = AgenticLayer.open(sys.argv[1], sys.argv[2], runtimes={'python': Interrupted()})
layer.tools.invoke(sys.argv[3], {'rows': []}, 'interrupted',
                   {'actor_id': 'integration-owner', 'actor_kind': 'owner'})
"""
    crashed = subprocess.run([sys.executable, "-c", code, str(client.root), str(client.state), TOOL],
                             cwd=client.base, env=process_env(), capture_output=True, timeout=30)
    assert crashed.returncode == 17 and crashed.stdout == crashed.stderr == b""
    for operation in ("tools.receipt", "tools.reconcile"):
        args = {"invocation_id": "interrupted"}
        if operation == "tools.reconcile":
            args.update(resolution="no_effect_confirmed", note="Isolated runtime exited before executing code")
        for actor, selection in (({"actor_id": "other", "actor_kind": "owner"}, SELECTION),
                                 (ACTOR, {"package_ids": []})):
            denied = client.call(operation, actor=actor, selection=selection, **args)
            assert denied["error"]["code"] == "unavailable"
    receipt = client.ok("tools.receipt", invocation_id="interrupted", actor=ACTOR, selection=SELECTION)["receipt"]
    assert receipt["status"] == "indeterminate"
    result = client.ok("tools.reconcile", invocation_id="interrupted", actor=ACTOR, selection=SELECTION,
                       resolution="no_effect_confirmed", note="Isolated runtime exited before executing code")["receipt"]
    assert result["status"] == "reconciled" and result["effect_state"] == "not_started"
    assert client.ok("tools.receipt", invocation_id="interrupted", actor=ACTOR, selection=SELECTION)["receipt"] == result


@pytest.mark.parametrize("kind", ["layer", "path", "string"])
def test_git_fixture_uses_explicit_authoring_root(layer, kind):
    value = {"layer": layer, "path": layer.root, "string": str(layer.root)}[kind]
    assert git(value, "rev-parse", "--show-toplevel").stdout.strip() == str(layer.root)
