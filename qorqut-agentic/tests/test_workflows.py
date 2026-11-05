"""Focused state-machine and frozen-definition tests using disposable stores."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from qorqut_agentic import AgenticError
from qorqut_agentic.errors import VerificationBlockedError
from qorqut_agentic.runtime import ExecutionResult
from qorqut_agentic.workflows import Workflows
from tests.conftest import ATTR, reopen, sha
from tests.test_tools import ACTOR, add_tool, runtime, tool_layer

REF = "example-project-workflow/landing-page"


def runner(tmp_path, rt=None):
    layer = tool_layer(tmp_path, rt)
    return layer, Workflows(layer._ctx, layer.tools)


def fields(**values):
    return {name: {"value": value, "source": "synthetic-owner"} for name, value in values.items()}


def start(wf, **kwargs):
    args = fields(product_name="Acme", audience="owners")
    args["audience"]["confirmed"] = True
    args.update(kwargs.pop("arguments", {}))
    return wf.start(REF, ACTOR, args, **kwargs)["run"]


def ids(run):
    return {step["step_id"] for step in run["ready_steps"]}


def blocked(call, reason):
    with pytest.raises(VerificationBlockedError) as caught:
        call()
    assert caught.value.code == "verification_blocked"
    assert caught.value.details["reason"] == reason
    assert caught.value.details["attempt_recorded"] is True
    return caught.value.details


def test_start_defaults_requests_and_idempotency(tmp_path):
    layer, wf = runner(tmp_path)
    result = wf.start(REF, ACTOR, operation_id="start-once")
    run = result["run"]
    assert run["state"] == "waiting_arguments" and not run["ready_steps"]
    assert run["arguments"]["tone"]["value"] == "plain"
    assert run["arguments"]["tone"]["source"] == "default"
    assert not run["arguments"]["tone"]["confirmed"]
    assert {r["name"] for r in run["argument_requests"]} == {"product_name", "audience"}
    assert next(r for r in run["argument_requests"] if r["name"] == "audience")["proposed_default"] == "small business owners"
    assert wf.start(REF, ACTOR, operation_id="start-once") == result
    with pytest.raises(AgenticError) as error:
        wf.start(REF, ACTOR, fields(product_name="different"), operation_id="start-once")
    assert error.value.code == "operation_conflict"
    restored = reopen(layer)
    other = Workflows(restored._ctx, restored.tools)
    assert other.get(run["run_id"])["run"] == run
    assert wf.list(state="waiting_arguments")["total"] == 1


def test_new_start_id_is_returned_and_selection_is_enforced(tmp_path):
    _, wf = runner(tmp_path)
    assert wf.start(REF, ACTOR)["operation_id"].startswith("op_")
    with pytest.raises(AgenticError) as error:
        wf.start(REF, ACTOR, selection={"package_ids": []})
    assert error.value.code == "unavailable"


def test_instruction_and_definition_snapshot_survive_catalog_edits(tmp_path):
    layer, wf = runner(tmp_path)
    before = start(wf)
    path = "plugins/example-project-workflow/instructions/brief.md"
    original = (layer.root / path).read_bytes()
    layer.authoring.apply(ATTR, [{"op": "edit", "path": path, "expected_sha256": sha(original),
                                  "content": "New instructions for future runs only.\n"}])
    assert wf.get(before["run_id"])["run"] == before
    after = start(wf)
    assert after["steps"][0]["instructions"] == "New instructions for future runs only.\n"
    assert after["steps"][0]["instruction_revision"] != before["steps"][0]["instruction_revision"]


def test_invalid_dependencies_cycles_and_arguments_fail_before_run_creation(tmp_path):
    layer, wf = runner(tmp_path)
    path = "plugins/example-project-workflow/workflows/landing-page.yaml"
    original = (layer.root / path).read_bytes()
    for bad in (original.decode().replace("depends_on: [brief]", "depends_on: [missing]", 1),
                original.decode().replace("instruction_ref: example-project-workflow/brief",
                                          "instruction_ref: example-project-workflow/brief\n    depends_on: [assemble]")):
        with pytest.raises(AgenticError) as error:
            layer.authoring.apply(ATTR, [{"op": "edit", "path": path, "expected_sha256": sha(original), "content": bad}])
        assert error.value.code == "validation_failed"
    with pytest.raises(AgenticError):
        wf.start(REF, ACTOR, fields(tone="unrecognized"))
    with pytest.raises(AgenticError):
        wf.start(REF, ACTOR, fields(audience="owners"))
    with pytest.raises(AgenticError):
        wf.start(REF, ACTOR, fields(primary_keyword="keyword"))
    assert wf.list()["total"] == 0


def test_all_argument_modes_parallel_steps_and_verified_completion(tmp_path):
    rt = runtime()
    layer, wf = runner(tmp_path, rt)
    initial = wf.start(REF, ACTOR)["run"]
    rid = initial["run_id"]
    args = fields(product_name="Acme", audience="owners")
    args["audience"]["confirmed"] = True
    result = wf.supply_arguments(rid, args, ACTOR, "args")
    assert ids(result["run"]) == {"brief"}
    assert wf.supply_arguments(rid, args, ACTOR, "args") == result
    with pytest.raises(AgenticError) as error:
        wf.supply_arguments(rid, fields(product_name="different"), ACTOR, "args")
    assert error.value.code == "operation_conflict"
    assert wf.start_step(rid, "brief", ACTOR, "start-brief")["run"]["steps"][0]["state"] == "active"
    brief = wf.complete_step(rid, "brief", ACTOR, "brief", completion_note="declared by executor", output={})
    assert ids(brief["run"]) == {"collect-assets"}
    assert brief["run"]["steps"][0]["verification"] == "declared"
    assert {r["name"] for r in brief["run"]["argument_requests"]} == {"primary_keyword"}
    with pytest.raises(AgenticError):
        wf.start_step(rid, "draft-copy", ACTOR, "too-early")
    keyword = {"primary_keyword": {"value": "example", "source": "derived", "note": "from the synthetic brief"}}
    assert ids(wf.supply_arguments(rid, keyword, ACTOR, "derive")["run"]) == {"draft-copy", "collect-assets"}
    wf.complete_step(rid, "draft-copy", ACTOR, "copy", output="copy")
    wf.complete_step(rid, "collect-assets", ACTOR, "assets", output=["image"])
    waiting = wf.get(rid)["run"]
    assert not ids(waiting)
    assert {r["name"] for r in waiting["argument_requests"]} == {"launch_date"}
    wf.supply_arguments(rid, fields(launch_date="2026-10-26"), ACTOR, "date")
    output = {"title": "Acme landing page", "sections": ["hero", "proof", "cta"]}
    complete = wf.complete_step(rid, "assemble", ACTOR, "assemble", "complete", output)
    assert complete["run"]["state"] == "completed"
    assert complete["run"]["steps"][-1]["verification"] == "verified"
    assert wf.complete_step(rid, "assemble", ACTOR, "assemble", "complete", output) == complete
    assert rt.calls == 1
    assert Workflows(reopen(layer)._ctx, layer.tools).get(rid)["run"]["state"] == "completed"


def add_workflow(layer, *, verifier=None, arguments=None, steps=None):
    definition = {"schema_version": 1, "id": "flow", "description": "Synthetic workflow",
                  "arguments": arguments or [], "steps": steps or [{"id": "one", "instructions": "Do one thing."}]}
    if verifier:
        definition["steps"][0]["verifier"] = verifier
    manifest = {"schema_version": 1, "id": "test-flow", "name": "Test", "description": "Synthetic",
                "entries": [{"id": "flow", "kind": "workflow", "description": "Test", "when_to_use": "Tests",
                             "path": "flow.json"}]}
    layer.authoring.apply(ATTR, [
        {"op": "create", "path": "plugins/test-flow/agentic.yaml", "content": json.dumps(manifest)},
        {"op": "create", "path": "plugins/test-flow/flow.json", "content": json.dumps(definition)},
    ])
    return "test-flow/flow"


def verification_flow(tmp_path, rt, script="print('{}')", output_schema=True):
    layer, wf = runner(tmp_path, rt)
    verifier = add_tool(layer, "verify", script, input_schema=True, output_schema=output_schema)
    ref = add_workflow(layer, verifier=verifier)
    rid = wf.start(ref, ACTOR)["run"]["run_id"]
    return layer, wf, rid


def test_locked_arguments_keep_provenance_and_independent_locals_remain_editable(tmp_path):
    layer, wf = runner(tmp_path)
    rid = start(wf)["run_id"]
    wf.start_step(rid, "brief", ACTOR, "start")
    original = wf.get(rid)["run"]["arguments"]["product_name"]
    with pytest.raises(AgenticError) as error:
        wf.supply_arguments(rid, fields(product_name="changed"), ACTOR, "change")
    assert error.value.code == "state_conflict"
    wf.supply_arguments(rid, {"product_name": {"value": "Acme", "source": "different", "note": "ignored"}}, ACTOR, "same")
    assert wf.get(rid)["run"]["arguments"]["product_name"] == original
    wf.supply_arguments(rid, fields(launch_date="2026-11-01"), ACTOR, "future")
    wf.supply_arguments(rid, fields(launch_date="2026-12-01"), ACTOR, "future-edit")
    assert wf.get(rid)["run"]["arguments"]["launch_date"]["value"] == "2026-12-01"
    # The locked identity comparison must distinguish JSON true from JSON 1.
    ref = add_workflow(layer, arguments=[{"name": "value", "schema": True, "default": True,
                                         "prompt": "Value?", "acquisition": "default"}])
    other = wf.start(ref, ACTOR)["run"]["run_id"]
    wf.start_step(other, "one", ACTOR, "start")
    with pytest.raises(AgenticError):
        wf.supply_arguments(other, fields(value=1), ACTOR, "numeric")


def test_pause_resume_cancel_and_invalid_transitions(tmp_path):
    layer, wf = runner(tmp_path)
    rid = start(wf)["run_id"]
    paused = wf.pause(rid, ACTOR, "pause")
    assert paused["run"]["state"] == "paused" and not ids(paused["run"])
    assert wf.pause(rid, ACTOR, "pause") == paused
    for call in (lambda: wf.start_step(rid, "brief", ACTOR, "start"),
                 lambda: wf.complete_step(rid, "brief", ACTOR, "done"),
                 lambda: wf.supply_arguments(rid, fields(tone="formal"), ACTOR, "supply")):
        with pytest.raises(AgenticError) as error:
            call()
        assert error.value.code == "state_conflict"
    assert ids(wf.resume(rid, ACTOR, "resume")["run"]) == {"brief"}
    cancelled = wf.cancel(rid, ACTOR, "cancel")["run"]
    assert cancelled["state"] == "cancelled"
    assert wf.list(state="cancelled")["total"] == 1
    with pytest.raises(AgenticError):
        wf.resume(rid, ACTOR, "resume-again")


@pytest.mark.parametrize("reply, status, reason", [
    ({"passed": False, "feedback": "Add required evidence"}, "failed", None),
    ({"passed": "yes", "feedback": "invalid boolean"}, "blocked", "invalid_verdict"),
    ({"passed": True}, "blocked", "invalid_verdict"),
])
def test_verifier_failed_or_invalid_verdict_never_completes(tmp_path, reply, status, reason):
    class Respond:
        def execute(self, request):
            return ExecutionResult("exited", 0, json.dumps(reply).encode())
    _, wf, rid = verification_flow(tmp_path, Respond())
    call = lambda: wf.complete_step(rid, "one", ACTOR, "attempt", "done", {"evidence": 1})
    result = blocked(call, reason) if reason else call()
    step = result["run"]["steps"][0]
    assert step["state"] == "active" and step["verification"] == status
    assert step["attempts"][0]["block_reason"] == reason
    assert step["attempts"][0]["feedback"]
    assert result["run"]["state"] == "running"


@pytest.mark.parametrize("kind, reason", [("missing", "runtime_unavailable"), ("error", "runtime_error"),
                                          ("timeout", "timeout"), ("changed", "revision_conflict")])
def test_verifier_execution_blockers_are_durable(tmp_path, kind, reason):
    class Respond:
        def execute(self, request):
            return ExecutionResult("runtime_error" if kind == "error" else "timeout", message="backend unavailable")
    layer, wf, rid = verification_flow(tmp_path, None if kind == "missing" else Respond())
    if kind == "changed":
        path = "plugins/t-verify/verify.py"
        original = (layer.root / path).read_bytes()
        layer.authoring.apply(ATTR, [{"op": "edit", "path": path, "expected_sha256": sha(original),
                                      "content": original.decode() + "\n# changed"}])
    result = blocked(lambda: wf.complete_step(rid, "one", ACTOR, "attempt"), reason)
    assert result["run"]["steps"][0]["attempts"][0]["block_reason"] == reason
    assert result["run"]["steps"][0]["state"] != "completed"
    assert blocked(lambda: wf.complete_step(rid, "one", ACTOR, "attempt"), reason) == result
    assert wf.get(rid)["run"] == result["run"]


@pytest.mark.parametrize("action", ["pause", "pause-resume", "cancel"])
def test_late_verifier_result_cannot_overwrite_control_changes(tmp_path, action):
    entered, release = threading.Event(), threading.Event()

    class Waiting:
        calls = 0

        def execute(self, request):
            self.calls += 1
            entered.set()
            assert release.wait(timeout=10)
            return ExecutionResult("exited", 0, b'{"passed": true, "feedback": "actual verdict"}')

    rt = Waiting()
    _, wf, rid = verification_flow(tmp_path, rt)
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(wf.complete_step, rid, "one", ACTOR, "late", "done", {})
        assert entered.wait(timeout=5)
        if action == "cancel":
            wf.cancel(rid, ACTOR, "cancel")
        else:
            wf.pause(rid, ACTOR, "pause")
            if action == "pause-resume":
                wf.resume(rid, ACTOR, "resume")
        release.set()
        result = blocked(lambda: job.result(timeout=10), "control_changed")
    assert result["run"]["state"] == {"pause": "paused", "pause-resume": "running", "cancel": "cancelled"}[action]
    step = result["run"]["steps"][0]
    assert step["state"] != "completed"
    assert step["attempts"][0]["block_reason"] == "control_changed"
    assert step["attempts"][0]["details"]["verdict"] is True
    assert step["attempts"][0]["details"]["receipt"]["status"] == "succeeded"
    assert blocked(lambda: wf.complete_step(rid, "one", ACTOR, "late", "done", {}), "control_changed") == result
    assert rt.calls == 1
    if action == "pause":
        wf.resume(rid, ACTOR, "resume")
    if action != "cancel":
        fresh = wf.complete_step(rid, "one", ACTOR, "fresh", "done", {})
        assert fresh["run"]["state"] == "completed"
        assert rt.calls == 2


def test_competing_completion_never_dispatches_a_second_effect(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Waiting:
        calls = 0

        def execute(self, request):
            self.calls += 1
            entered.set()
            assert release.wait(timeout=10)
            return ExecutionResult("exited", 0, b'{"passed": true, "feedback": "ok"}')

    rt = Waiting()
    _, wf, rid = verification_flow(tmp_path, rt)
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(wf.complete_step, rid, "one", ACTOR, "same")
        assert entered.wait(timeout=5)
        with pytest.raises(AgenticError) as error:
            wf.complete_step(rid, "one", ACTOR, "different")
        assert error.value.code == "state_conflict"
        in_flight = wf.complete_step(rid, "one", ACTOR, "same")
        assert in_flight["run"]["steps"][0]["attempts"][0]["status"] == "verifying"
        release.set()
        final = job.result(timeout=10)
    assert final["run"]["state"] == "completed" and rt.calls == 1
    assert wf.complete_step(rid, "one", ACTOR, "same") == final


def test_competing_argument_update_and_step_start_are_serialized(tmp_path):
    layer, wf = runner(tmp_path)
    rid = start(wf)["run_id"]
    barrier = threading.Barrier(2)

    def update():
        barrier.wait(timeout=5)
        try:
            wf.supply_arguments(rid, fields(product_name="Updated"), ACTOR, "update")
            return True
        except AgenticError as error:
            assert error.code == "state_conflict"
            return False

    def begin():
        barrier.wait(timeout=5)
        return wf.start_step(rid, "brief", ACTOR, "start")

    with ThreadPoolExecutor(max_workers=2) as pool:
        update_job, start_job = pool.submit(update), pool.submit(begin)
        updated, _ = update_job.result(timeout=10), start_job.result(timeout=10)
    argument = wf.get(rid)["run"]["arguments"]["product_name"]
    assert argument["locked"]
    assert argument["value"] == ("Updated" if updated else "Acme")


def test_interrupted_verification_uses_known_receipt_after_reopen(tmp_path):
    from tests.conftest import SimulatedCrash

    class Respond:
        calls = 0

        def execute(self, request):
            self.calls += 1
            return ExecutionResult("exited", 0, b'{"passed": true, "feedback": "ok"}')

    rt = Respond()
    layer, wf, rid = verification_flow(tmp_path, rt)
    original = wf._finish_verification

    def crash(*args):
        raise SimulatedCrash()

    wf._finish_verification = crash
    with pytest.raises(SimulatedCrash):
        wf.complete_step(rid, "one", ACTOR, "crashed", "done", {"evidence": True})
    assert rt.calls == 1
    restored = reopen(layer, runtimes={"python": rt})
    recovered = Workflows(restored._ctx, restored.tools)
    result = recovered.complete_step(rid, "one", ACTOR, "crashed", "done", {"evidence": True})
    assert result["run"]["state"] == "completed" and rt.calls == 1


def test_indeterminate_verifier_receipt_requires_reconciliation_not_redispatch(tmp_path, monkeypatch):
    import qorqut_agentic.tools as tools_module
    from tests.conftest import SimulatedCrash, crash_at

    rt = runtime()
    layer, wf, rid = verification_flow(tmp_path, rt)
    crash_at(layer, "after_receipt_recorded")
    with pytest.raises(SimulatedCrash):
        wf.complete_step(rid, "one", ACTOR, "crashed")
    restored = reopen(layer, runtimes={"python": rt})
    monkeypatch.setattr(tools_module, "_runner_alive", lambda _: False)
    recovered = Workflows(restored._ctx, restored.tools)
    result = blocked(lambda: recovered.complete_step(rid, "one", ACTOR, "crashed"), "indeterminate")
    attempt = result["run"]["steps"][0]["attempts"][0]
    assert attempt["block_reason"] == "indeterminate"
    assert rt.calls == 0
    restored.tools.reconcile(attempt["invocation_id"], "no_effect_confirmed", "synthetic reconciliation", ACTOR)
    assert blocked(lambda: recovered.complete_step(rid, "one", ACTOR, "crashed"), "indeterminate") == result
    assert rt.calls == 0


def test_failed_verification_locks_inputs_but_allows_explicit_new_output(tmp_path):
    class Respond:
        def execute(self, request):
            passed = json.loads(request.stdin)["output"].get("complete") is True
            return ExecutionResult("exited", 0, json.dumps({"passed": passed, "feedback": "fix output"}).encode())

    layer, wf = runner(tmp_path, Respond())
    verifier = add_tool(layer, "verify", "print('{}')", input_schema=True)
    ref = add_workflow(layer, verifier=verifier, arguments=[{"name": "flag", "schema": {"type": "boolean"},
                                                          "acquisition": "default", "default": True, "prompt": "Flag?"}])
    rid = wf.start(ref, ACTOR)["run"]["run_id"]
    failed = wf.complete_step(rid, "one", ACTOR, "bad", output={"complete": False})
    assert failed["run"]["arguments"]["flag"]["locked"]
    with pytest.raises(AgenticError):
        wf.supply_arguments(rid, fields(flag=False), ACTOR, "change")
    complete = wf.complete_step(rid, "one", ACTOR, "good", output={"complete": True})
    assert complete["run"]["state"] == "completed"
    assert [a["status"] for a in complete["run"]["steps"][0]["attempts"]] == ["failed", "verified"]


def test_removed_verifier_stays_blocked_in_existing_run(tmp_path):
    layer, wf, rid = verification_flow(tmp_path, runtime())
    (layer.root / "plugins/t-verify/agentic.yaml").unlink()
    result = blocked(lambda: wf.complete_step(rid, "one", ACTOR, "missing-verifier"), "not_found")
    attempt = result["run"]["steps"][0]["attempts"][0]
    assert attempt["block_reason"] == "not_found"
    assert attempt["details"]["verdict"] is None
    assert result["run"]["steps"][0]["state"] == "active"


def test_frozen_definition_and_before_start_local_argument_scope(tmp_path):
    layer, wf = runner(tmp_path)
    ref = add_workflow(layer, arguments=[
        {"name": "later", "schema": {"type": "string"}, "prompt": "Later?", "acquisition": "ask_before_start", "step": "two"},
    ], steps=[{"id": "one", "instructions": "original"}, {"id": "two", "instructions": "second", "depends_on": ["one"]}])
    result = wf.start(ref, ACTOR)
    rid = result["run"]["run_id"]
    assert result["run"]["state"] == "waiting_arguments"
    wf.supply_arguments(rid, fields(later="first value"), ACTOR, "supply")
    wf.start_step(rid, "one", ACTOR, "start-one")
    # It gates start but belongs to step two, so step one does not consume it.
    wf.supply_arguments(rid, fields(later="corrected"), ACTOR, "correct")
    path = "plugins/test-flow/flow.json"
    original = (layer.root / path).read_bytes()
    changed = json.loads(original)
    changed["steps"][0]["instructions"] = "new future instruction"
    layer.authoring.apply(ATTR, [{"op": "edit", "path": path, "expected_sha256": sha(original), "content": json.dumps(changed)}])
    assert wf.get(rid)["run"]["steps"][0]["instructions"] == "original"
    assert wf.start(ref, ACTOR)["run"]["workflow_revision"] != result["run"]["workflow_revision"]
    wf.complete_step(rid, "one", ACTOR, "complete-one")
    wf.start_step(rid, "two", ACTOR, "start-two")
    assert wf.get(rid)["run"]["arguments"]["later"]["locked"]


def test_pause_before_interrupted_verifier_dispatch_prevents_new_effect(tmp_path):
    from tests.conftest import SimulatedCrash, crash_at
    rt = runtime()
    layer, wf, rid = verification_flow(tmp_path, rt)
    crash_at(layer, "after_workflow_attempt_recorded")
    with pytest.raises(SimulatedCrash):
        wf.complete_step(rid, "one", ACTOR, "interrupted")
    wf.pause(rid, ACTOR, "pause")
    restored = reopen(layer, runtimes={"python": rt})
    recovered = Workflows(restored._ctx, restored.tools)
    result = blocked(lambda: recovered.complete_step(rid, "one", ACTOR, "interrupted"), "control_changed")
    assert rt.calls == 0
    assert result["run"]["state"] == "paused"
    assert result["run"]["steps"][0]["attempts"][0]["block_reason"] == "control_changed"


def test_blocked_error_replays_exactly_after_reopen_and_new_control_state(tmp_path):
    layer, wf, rid = verification_flow(tmp_path, None)
    first = blocked(lambda: wf.complete_step(rid, "one", ACTOR, "blocked", "original", {}), "runtime_unavailable")
    assert first["run"] == wf.get(rid)["run"]
    wf.pause(rid, ACTOR, "pause")
    reopened = reopen(layer)
    restored = Workflows(reopened._ctx, reopened.tools)
    replay = blocked(lambda: restored.complete_step(rid, "one", ACTOR, "blocked", "original", {}), "runtime_unavailable")
    assert first == replay
    assert restored.get(rid)["run"]["state"] == "paused"
    assert replay["run"]["state"] == "running"
    with pytest.raises(AgenticError) as error:
        restored.complete_step(rid, "one", ACTOR, "blocked", "changed", {})
    assert error.value.code == "operation_conflict"


def test_deep_verifier_output_is_a_durable_blocked_attempt(tmp_path):
    rt = runtime()
    layer, wf, rid = verification_flow(tmp_path, rt, "print('[' * 20000 + '0' + ']' * 20000)")
    result = blocked(lambda: wf.complete_step(rid, "one", ACTOR, "deep-verifier"), "invalid_output")
    attempt = result["run"]["steps"][0]["attempts"][0]
    assert attempt["status"] == "blocked"
    assert attempt["details"]["receipt"]["status"] == "failed"
    assert "RecursionError" in attempt["details"]["receipt"]["diagnostics"]["message"]
    assert wf.get(rid)["run"]["steps"][0]["verification"] == "blocked"
    restored = reopen(layer, runtimes={"python": rt})
    again = Workflows(restored._ctx, restored.tools)
    assert blocked(lambda: again.complete_step(rid, "one", ACTOR, "deep-verifier"), "invalid_output") == result
    assert rt.calls == 1


def test_pending_recovery_blocks_new_verifier_then_explicit_recovery_allows_retry(tmp_path):
    from tests.conftest import SimulatedCrash, crash_at

    rt = runtime()
    layer, wf, rid = verification_flow(tmp_path, rt, 'print(\'{"passed": true, "feedback": "ok"}\')')
    # Change a different tool so the run's verifier pins remain valid after recovery.
    path = "plugins/example-project-workflow/tools/normalize_results.py"
    original = (layer.root / path).read_bytes()
    crash_at(layer, "before_finalize")
    with pytest.raises(SimulatedCrash):
        layer.authoring.apply(ATTR, [{"op": "edit", "path": path, "expected_sha256": sha(original),
                                      "content": original.decode() + "\n# pending synthetic edit\n"}])
    restored = reopen(layer, runtimes={"python": rt})
    assert restored.recovery_required
    wf = Workflows(restored._ctx, restored.tools)
    result = blocked(lambda: wf.complete_step(rid, "one", ACTOR, "while-pending"), "recovery_required")
    attempt = result["run"]["steps"][0]["attempts"][0]
    assert attempt["status"] == "blocked" and attempt["details"]["receipt"] is None
    assert rt.calls == 0
    with pytest.raises(AgenticError) as error:
        restored.tools.receipt(attempt["invocation_id"])
    assert error.value.code == "not_found"
    restored.recover()
    assert blocked(lambda: wf.complete_step(rid, "one", ACTOR, "while-pending"), "recovery_required") == result
    complete = wf.complete_step(rid, "one", ACTOR, "after-recovery")
    assert complete["run"]["state"] == "completed" and rt.calls == 1
