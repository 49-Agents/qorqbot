"""Phase A item 4: runtime protocol, LocalProcessRuntime, schemas, receipts, idempotency."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time

import pytest

import qorqut_agentic.tools as tools_module
from qorqut_agentic import AgenticError
from qorqut_agentic.runtime import LocalProcessRuntime
from tests.conftest import ATTR, PY, SimulatedCrash, crash_at, git, import_example, make_layer, reopen

ACTOR = {"actor_id": "tester", "actor_kind": "agent"}
WORKFLOW_PKG = "example-project-workflow"
NORMALIZE = f"{WORKFLOW_PKG}/normalize-results"


class CountingRuntime(LocalProcessRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return super().execute(request)


def runtime(**kwargs):
    return CountingRuntime(PY, **kwargs)


def tool_layer(tmp_path, rt=None, **kwargs):
    (tmp_path / "root").mkdir()
    git(str(tmp_path / "root"), "init", "-q", "-b", "agentic")
    layer = make_layer(tmp_path, runtimes={"python": rt} if rt else {}, **kwargs)
    import_example(layer, WORKFLOW_PKG)
    return layer


def add_tool(layer, tool_id, script, *, input_schema=None, output_schema=None, limits=None, runtime_alias="python",
             extra=None):
    """Create a one-tool package ``t-<tool_id>`` through the normal authoring API."""
    definition = {
        "schema_version": 1, "runtime": runtime_alias, "code": f"{tool_id}.py",
        "input_schema": input_schema if input_schema is not None else {"type": "object"},
        "output_schema": output_schema if output_schema is not None else True,
    }
    if limits:
        definition["limits"] = limits
    definition.update(extra or {})
    manifest = (f"schema_version: 1\nid: t-{tool_id}\nname: t\ndescription: d\nentries:\n"
                f"  - {{id: {tool_id}, kind: tool, description: d, when_to_use: w, path: {tool_id}.json}}\n")
    layer.authoring.apply(ATTR, [
        {"op": "create", "path": f"plugins/t-{tool_id}/agentic.yaml", "content": manifest},
        {"op": "create", "path": f"plugins/t-{tool_id}/{tool_id}.json", "content": json.dumps(definition)},
        {"op": "create", "path": f"plugins/t-{tool_id}/{tool_id}.py", "content": textwrap.dedent(script)},
    ])
    return f"t-{tool_id}/{tool_id}"


def invoke(layer, ref, arguments, invocation_id, **kwargs):
    return layer.tools.invoke(ref, arguments, invocation_id, ACTOR, **kwargs)["receipt"]


def test_example_tool_runs_with_helper_resources_schemas_and_a_receipt(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    assert rt.calls == 0  # importing and reading the catalog never executes anything
    layer.catalog.list()
    layer.catalog.read_entry(NORMALIZE)
    assert rt.calls == 0
    entry = layer.catalog.read_entry(NORMALIZE)
    receipt = invoke(layer, NORMALIZE, {"rows": [{"name": " Gamma ", "score": 1.005}, {"name": "B", "score": 3}], "digits": 1}, "inv-1")
    assert receipt["status"] == "succeeded" and receipt["effect_state"] == "completed"
    assert receipt["output"] == {"rows": [{"name": "gamma-ray", "score": 1.0}, {"name": "b", "score": 3}], "count": 2}
    assert receipt["definition_revision"] == entry["entry"]["revision"]
    assert receipt["code_revision"] == entry["entry"]["code_revision"]  # the revision actually staged and run
    assert receipt["actor"] == ACTOR and receipt["started_at"] <= receipt["ended_at"]
    assert receipt["limits"]["timeout_seconds"] == 10
    assert layer.tools.receipt("inv-1")["receipt"]["output"] == receipt["output"]
    assert {r["path"] for r in entry["resources"]} == {
        "tools/normalize_results.py", "tools/lib/__init__.py", "tools/lib/helpers.py", "tools/data/aliases.json"}


def test_definition_tests_are_explicit_fixture_invocations(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    report = layer.tools.test(NORMALIZE, ACTOR)
    assert report["passed"] and [c["name"] for c in report["cases"]][0].startswith("trims") and rt.calls == 1
    assert report["cases"][1]["actual"]["status"] == "rejected"  # the schema rejected it before dispatch
    custom = layer.tools.test(NORMALIZE, ACTOR, cases=[
        {"name": "wrong expectation", "input": {"rows": []}, "expect": {"status": "succeeded", "output": {"rows": [], "count": 1}}}])
    assert not custom["passed"] and custom["cases"][0]["actual"]["output"] == {"rows": [], "count": 0}
    assert layer.tools.receipt(report["cases"][0]["invocation_id"])["receipt"]["purpose"] == "test"
    verifier = layer.tools.test(f"{WORKFLOW_PKG}/check-landing-page", ACTOR)
    assert verifier["passed"]


def test_invalid_arguments_are_rejected_before_dispatch(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    with pytest.raises(AgenticError) as caught:
        invoke(layer, NORMALIZE, {"rows": [{"name": "x"}]}, "bad-1")
    assert caught.value.code == "validation_failed" and caught.value.details["issues"]
    assert rt.calls == 0
    with pytest.raises(AgenticError) as caught:
        layer.tools.receipt("bad-1")
    assert caught.value.code == "not_found"  # nothing was dispatched, so there is no receipt


def test_repeated_invocation_id_returns_the_receipt_and_never_repeats_the_effect(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    effect_file = tmp_path / "effects.log"
    ref = add_tool(layer, "send", """
        import json, sys
        request = json.load(sys.stdin)
        with open(request["effect_file"], "a") as handle:
            handle.write("sent\\n")
        json.dump({"ok": True}, sys.stdout)
        """, input_schema={"type": "object", "required": ["effect_file"]})
    first = invoke(layer, ref, {"effect_file": str(effect_file)}, "send-1")
    again = invoke(layer, ref, {"effect_file": str(effect_file)}, "send-1")
    reopened = reopen(layer, runtimes={"python": rt})
    third = invoke(reopened, ref, {"effect_file": str(effect_file)}, "send-1")
    assert effect_file.read_text() == "sent\n"  # exactly one effect
    assert (first["replayed"], again["replayed"], third["replayed"]) == (False, True, True)
    assert again["output"] == first["output"] and again["started_at"] == first["started_at"]
    with pytest.raises(AgenticError) as caught:
        invoke(layer, ref, {"effect_file": str(effect_file) + "2"}, "send-1")
    assert caught.value.code == "operation_conflict"
    assert effect_file.read_text() == "sent\n"


@pytest.mark.parametrize("script, code, limits", [
    ("import sys; sys.stderr.write('boom'); sys.exit(3)", "nonzero_exit", None),
    ("print('not json')", "invalid_output", None),
    ("print('{}'); print('{}')", "invalid_output", None),
    ("print('NaN')", "invalid_output", None),
    ("pass", "invalid_output", None),
    ("import json, sys; json.dump({'n': 'x'}, sys.stdout)", "output_schema_violation", None),
    ("import time; time.sleep(30)", "timeout", {"timeout_seconds": 0.5}),
    ("import sys; sys.stdout.write('x' * 10_000_000)", "output_overflow", {"max_output_bytes": 1000}),
])
def test_failures_are_reported_honestly(tmp_path, script, code, limits):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    ref = add_tool(layer, "bad", script + "\n", output_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
                   limits=limits)
    started = time.monotonic()
    receipt = invoke(layer, ref, {}, "bad-run")
    assert receipt["status"] == "failed" and receipt["failure_code"] == code and receipt["output"] is None
    assert receipt["effect_state"] == "possible"  # it started, so an external effect cannot be ruled out
    assert time.monotonic() - started < 10
    if code == "nonzero_exit":
        assert receipt["exit_code"] == 3 and receipt["diagnostics"]["stderr"] == "boom"
    if code == "output_overflow":
        assert len(receipt["diagnostics"]["stdout_excerpt"]) <= 1024
    # a failed receipt is final: retrying the same ID does not run the tool again
    calls = rt.calls
    assert invoke(layer, ref, {}, "bad-run")["replayed"] and rt.calls == calls


def test_timeout_terminates_the_whole_process_group(tmp_path):
    layer = tool_layer(tmp_path, runtime())
    marker = tmp_path / "grandchild-survived"
    ref = add_tool(layer, "spawny", f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time, pathlib; time.sleep(1.5); pathlib.Path({str(marker)!r}).write_text('x')"])
        time.sleep(30)
        """, limits={"timeout_seconds": 0.5})
    assert invoke(layer, ref, {}, "spawny-1")["failure_code"] == "timeout"
    time.sleep(2.5)
    assert not marker.exists()


def test_stderr_over_host_limit_is_bounded_and_fails(tmp_path):
    layer = tool_layer(tmp_path, runtime(max_stderr_bytes=100))
    ref = add_tool(layer, "chatty", """
        import json, sys
        sys.stderr.write("e" * 100000)
        json.dump({}, sys.stdout)
        """)
    receipt = invoke(layer, ref, {}, "chatty-1")
    assert receipt["failure_code"] == "output_overflow" and len(receipt["diagnostics"]["stderr"]) == 100
    assert receipt["diagnostics"]["stderr_truncated"] and receipt["limits"]["max_stderr_bytes"] == 100


def test_host_maxima_override_larger_definition_limits(tmp_path):
    layer = tool_layer(tmp_path, runtime(max_timeout_seconds=0.5))
    ref = add_tool(layer, "slow", "import time; time.sleep(30)\n", limits={"timeout_seconds": 3000})
    receipt = invoke(layer, ref, {}, "slow-1")
    assert receipt["failure_code"] == "timeout" and receipt["limits"]["timeout_seconds"] == 0.5


def test_child_environment_is_explicit_and_scratch_is_fresh_every_time(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "do-not-leak")
    layer = tool_layer(tmp_path, runtime(env={"ALLOWED": "yes"}))
    ref = add_tool(layer, "envcheck", """
        import json, os, sys
        leftover = os.path.exists("leftover.txt")
        open("leftover.txt", "w").write("x")
        json.dump({"env": sorted(os.environ), "leftover": leftover, "cwd_is_scratch": os.getcwd() == os.environ["QORQUT_AGENTIC_SCRATCH_DIR"]}, sys.stdout)
        """)
    first, second = invoke(layer, ref, {}, "env-1"), invoke(layer, ref, {}, "env-2")
    # LC_CTYPE is set by the interpreter itself (locale coercion), not inherited from the caller
    assert set(first["output"]["env"]) - {"LC_CTYPE"} == {"ALLOWED", "QORQUT_AGENTIC_PACKAGE_DIR", "QORQUT_AGENTIC_SCRATCH_DIR"}
    assert first["output"]["leftover"] is False and second["output"]["leftover"] is False
    assert first["output"]["cwd_is_scratch"]
    assert not list((layer.state / "scratch" / "invocations").iterdir())  # cleaned up afterwards
    assert not (layer.root / "leftover.txt").exists()


def test_a_missing_runtime_is_an_error_with_no_receipt(tmp_path):
    layer = tool_layer(tmp_path)  # no runtime registered
    with pytest.raises(AgenticError) as caught:
        invoke(layer, NORMALIZE, {"rows": []}, "no-runtime")
    assert caught.value.code == "runtime_unavailable" and caught.value.details["runtime"] == "python"
    with pytest.raises(AgenticError):
        layer.tools.receipt("no-runtime")
    # registering one later lets the same ID run: nothing was ever dispatched
    other = reopen(layer, runtimes={"python": runtime()})
    assert invoke(other, NORMALIZE, {"rows": []}, "no-runtime")["status"] == "succeeded"
    # a runtime object that does not serve the alias reports spawn_failed, still honestly
    stray = reopen(layer, runtimes={"python": LocalProcessRuntime({"other": sys.executable})})
    receipt = invoke(stray, NORMALIZE, {"rows": []}, "stray-1")
    assert receipt["failure_code"] == "spawn_failed" and receipt["effect_state"] == "not_started"


def test_runtime_exceptions_close_the_receipt(tmp_path):
    class Exploding:
        def execute(self, request):
            raise RuntimeError("backend down")

    layer = tool_layer(tmp_path, Exploding())
    receipt = invoke(layer, NORMALIZE, {"rows": []}, "boom-1")
    assert receipt["failure_code"] == "runtime_error" and "backend down" in receipt["diagnostics"]["message"]


def test_interrupted_invocation_is_indeterminate_until_reconciled(tmp_path, monkeypatch):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    crash_at(layer, "after_receipt_recorded")
    with pytest.raises(SimulatedCrash):
        invoke(layer, NORMALIZE, {"rows": []}, "crash-1")
    reopened = reopen(layer, runtimes={"python": rt})
    # the "runner" is this very process, which is alive, so the call is still in flight
    assert reopened.tools.receipt("crash-1")["receipt"]["status"] == "in_flight"
    monkeypatch.setattr(tools_module, "_runner_alive", lambda token: False)  # the process that owned it died
    receipt = reopened.tools.receipt("crash-1")["receipt"]
    assert receipt["status"] == "indeterminate" and receipt["effect_state"] == "unknown"
    calls = rt.calls
    again = invoke(reopened, NORMALIZE, {"rows": []}, "crash-1")
    assert again["status"] == "indeterminate" and again["replayed"] and rt.calls == calls  # never silently re-run
    reconciled = reopened.tools.reconcile("crash-1", "no_effect_confirmed", "checked the mailbox: nothing was sent", ACTOR)["receipt"]
    assert reconciled["status"] == "reconciled" and reconciled["reconciliation"]["resolution"] == "no_effect_confirmed"
    assert invoke(reopened, NORMALIZE, {"rows": []}, "crash-1")["status"] == "reconciled" and rt.calls == calls
    assert invoke(reopened, NORMALIZE, {"rows": []}, "crash-2")["status"] == "succeeded"  # a new ID is a new attempt
    with pytest.raises(AgenticError) as caught:
        reopened.tools.reconcile("crash-2", "effect_confirmed", "n/a", ACTOR)
    assert caught.value.code == "state_conflict"


def test_expected_revisions_guard_against_a_changed_tool(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    entry = layer.catalog.read_entry(NORMALIZE)
    code_revision = entry["entry"]["code_revision"]
    ok = invoke(layer, NORMALIZE, {"rows": []}, "rev-1", expected_definition_revision=entry["entry"]["revision"],
                expected_code_revision=code_revision)
    assert ok["status"] == "succeeded" and ok["code_revision"] == code_revision
    helper = "plugins/example-project-workflow/tools/lib/helpers.py"
    current = (layer.root / helper).read_bytes()
    from tests.conftest import sha

    layer.authoring.apply(ATTR, [{"op": "edit", "path": helper, "expected_sha256": sha(current),
                                  "content": current.decode() + "\n# changed\n"}])
    calls = rt.calls
    with pytest.raises(AgenticError) as caught:
        invoke(layer, NORMALIZE, {"rows": []}, "rev-2", expected_code_revision=code_revision)
    assert caught.value.code == "revision_conflict" and caught.value.details["kind"] == "code"
    assert rt.calls == calls
    with pytest.raises(AgenticError):
        layer.tools.receipt("rev-2")
    # without a pin, the changed code runs and the receipt records the revision actually used
    new = invoke(layer, NORMALIZE, {"rows": []}, "rev-3")
    assert new["code_revision"] != code_revision


def test_unsafe_tool_definitions_never_reach_a_runtime(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    base = {"schema_version": 1, "runtime": "python", "code": "x.py", "input_schema": True, "output_schema": True}
    bad_definitions = {
        "shell-like alias": {**base, "runtime": "/bin/sh -c"},
        "parent path": {**base, "code": "../escape.py"},
        "absolute code": {**base, "code": "/bin/sh"},
        "remote ref": {**base, "input_schema": {"$ref": "https://example.invalid/schema.json"}},
        "invalid schema": {**base, "output_schema": {"type": 12}},
        "unknown field": {**base, "command": "rm -rf /"},
    }
    for label, definition in bad_definitions.items():
        manifest = ("schema_version: 1\nid: bad-tool\nname: t\ndescription: d\nentries:\n"
                    "  - {id: t, kind: tool, description: d, when_to_use: w, path: t.json}\n")
        with pytest.raises(AgenticError) as caught:
            layer.authoring.apply(ATTR, [
                {"op": "create", "path": "plugins/bad-tool/agentic.yaml", "content": manifest},
                {"op": "create", "path": "plugins/bad-tool/t.json", "content": json.dumps(definition)},
                {"op": "create", "path": "plugins/bad-tool/x.py", "content": "print(1)"}])
        assert caught.value.code == "validation_failed", label
    assert rt.calls == 0


def test_symlinked_tool_code_is_rejected_when_found_outside_the_toolkit(tmp_path):
    layer = tool_layer(tmp_path, runtime())
    code = layer.root / "plugins/example-project-workflow/tools/normalize_results.py"
    code.unlink()
    code.symlink_to(tmp_path / "elsewhere.py")
    (tmp_path / "elsewhere.py").write_text("print('{}')")
    report = layer.catalog.validate()
    assert any(i["code"] == "symlink_not_allowed" for i in report["issues"])
    with pytest.raises(AgenticError) as caught:
        invoke(layer, NORMALIZE, {"rows": []}, "link-1")
    assert caught.value.code == "not_found"  # the invalid tool is not in the catalog, so it cannot run


def test_local_runtime_requires_explicit_absolute_interpreters():
    from qorqut_agentic.errors import InvalidRequestError

    for interpreters in ({}, {"python": "python3"}, {"python": "/nonexistent/interpreter"}):
        with pytest.raises(InvalidRequestError):
            LocalProcessRuntime(interpreters)
    assert os.path.isabs(sys.executable)


def test_stderr_overflow_terminates_and_bounds_capture(tmp_path):
    layer = tool_layer(tmp_path, runtime())
    ref = add_tool(layer, "stderr-flood", """
        import sys, time
        sys.stderr.write('x' * 100_000)
        sys.stderr.flush()
        time.sleep(30)
    """, limits={"max_stderr_bytes": 123, "timeout_seconds": 3})
    started = time.monotonic()
    receipt = invoke(layer, ref, {}, "stderr-flood")
    assert receipt["failure_code"] == "output_overflow"
    assert len(receipt["diagnostics"]["stderr"]) <= 123
    assert receipt["diagnostics"]["stderr_truncated"]
    assert time.monotonic() - started < 2


def test_competing_same_id_uses_independent_staging(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from qorqut_agentic.runtime import ExecutionResult

    class ReadingRuntime:
        def __init__(self):
            self.calls = 0

        def execute(self, request):
            self.calls += 1
            assert os.path.isfile(request.code_path)
            return ExecutionResult("exited", 0, b'{"rows": [], "count": 0}')

    rt = ReadingRuntime()
    layer = tool_layer(tmp_path, rt)
    barrier = threading.Barrier(2)
    original = layer.tools._stage
    paths = []

    def stage(*args):
        staged = original(*args)
        paths.append(staged[0])
        barrier.wait(timeout=5)
        return staged

    monkeypatch.setattr(layer.tools, "_stage", stage)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(invoke, layer, NORMALIZE, {"rows": []}, "racing") for _ in range(2)]
        receipts = [job.result(timeout=10) for job in jobs]
    assert len(set(paths)) == 2
    assert rt.calls == 1
    assert all(item["status"] in {"in_flight", "succeeded"} for item in receipts)
    assert layer.tools.receipt("racing")["receipt"]["status"] == "succeeded"


def test_invalid_fixture_batch_runs_nothing(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    with pytest.raises(AgenticError) as caught:
        layer.tools.test(NORMALIZE, ACTOR, cases=[
            {"name": "valid", "input": {"rows": []}, "expect": {"status": "succeeded"}},
            {"name": "invalid", "input": {}, "expect": {"status": "invented"}},
        ])
    assert caught.value.code == "invalid_request"
    assert rt.calls == 0


@pytest.mark.parametrize("limits", [
    {"max_timeout_seconds": float("inf")}, {"max_timeout_seconds": float("nan")},
    {"max_output_bytes": 1.5}, {"max_stderr_bytes": True},
])
def test_host_runtime_limits_must_be_finite_and_typed(limits):
    with pytest.raises(AgenticError):
        runtime(**limits)


def test_receipts_check_actor_and_package_before_disclosing_replays(tmp_path):
    layer = tool_layer(tmp_path, runtime())
    invoke(layer, NORMALIZE, {"rows": []}, "owned")
    stranger = {"actor_id": "someone-else", "actor_kind": "agent"}
    checks = [
        lambda: layer.tools.receipt("owned", actor=stranger),
        lambda: layer.tools.receipt("owned", actor=ACTOR, selection={"package_ids": []}),
        lambda: layer.tools.invoke(NORMALIZE, {"rows": ["different"]}, "owned", stranger),
        lambda: layer.tools.invoke(NORMALIZE, {"rows": []}, "owned", ACTOR, selection={"package_ids": []}),
        lambda: layer.tools.reconcile("owned", "no_effect_confirmed", "observed", stranger),
        lambda: layer.tools.reconcile("owned", "no_effect_confirmed", "observed", ACTOR,
                                     selection={"package_ids": []}),
    ]
    for call in checks:
        with pytest.raises(AgenticError) as error:
            call()
        assert error.value.code == "unavailable"
    with pytest.raises(AgenticError) as error:
        layer.tools.invoke(NORMALIZE, {"rows": []}, "owned", ACTOR, expected_code_revision="different")
    assert error.value.code == "operation_conflict"


def test_deleted_tools_do_not_strand_owned_receipt_evidence(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    first = invoke(layer, NORMALIZE, {"rows": []}, "historic")
    manifest = layer.root / "plugins/example-project-workflow/agentic.yaml"
    manifest.unlink()  # simulate an external deletion; reading a receipt is not catalog execution
    assert layer.tools.receipt("historic", actor=ACTOR,
                               selection={"package_ids": [WORKFLOW_PKG]})["receipt"]["output"] == first["output"]
    assert invoke(layer, NORMALIZE, {"rows": []}, "historic")["replayed"]
    assert rt.calls == 1


@pytest.mark.parametrize("result", [
    {"pretend": "success"},
    tools_module.ExecutionResult([], 0),
    tools_module.ExecutionResult("exited", True, b"{}"),
    tools_module.ExecutionResult("exited", 0, b"{}", limits={"bad": object()}),
])
def test_invalid_runtime_result_is_a_final_failed_receipt(tmp_path, result):
    class Broken:
        def execute(self, request):
            return result
    layer = tool_layer(tmp_path, Broken())
    receipt = invoke(layer, NORMALIZE, {"rows": []}, "invalid-runtime")
    assert receipt["status"] == "failed" and receipt["failure_code"] == "runtime_error"


def test_deep_json_output_finishes_failed_receipt_without_reexecution(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    ref = add_tool(layer, "deep-output", "print('[' * 20000 + '0' + ']' * 20000)")
    receipt = invoke(layer, ref, {}, "deep-output")
    assert receipt["status"] == "failed" and receipt["failure_code"] == "invalid_output"
    assert receipt["effect_state"] == "possible" and receipt["ended_at"] is not None
    assert "RecursionError" in receipt["diagnostics"]["message"]
    assert len(receipt["diagnostics"]["stdout_excerpt"]) <= 1024
    assert len(receipt["diagnostics"]["message"]) <= 4096
    assert layer.tools.receipt("deep-output")["receipt"]["status"] == "failed"
    assert invoke(layer, ref, {}, "deep-output")["replayed"]
    assert rt.calls == 1


@pytest.mark.parametrize("phase", ["parse", "schema", "serialization"])
def test_output_processing_exceptions_finalize_receipts(tmp_path, monkeypatch, phase):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    ref = add_tool(layer, "output-processing", 'print(\'{"result": "ok"}\')',
                   output_schema={"type": "object", "title": "output-processing"})

    def fail(*args, **kwargs):
        raise RecursionError("synthetic output processing limit")

    if phase == "parse":
        monkeypatch.setattr(layer.tools, "_parse_output", fail)
    elif phase == "schema":
        original = tools_module.validate_instance

        def validate(value, schema, **kwargs):
            if isinstance(schema, dict) and schema.get("title") == "output-processing":
                fail()
            return original(value, schema, **kwargs)

        monkeypatch.setattr(tools_module, "validate_instance", validate)
    else:
        original = tools_module.dumps

        def dumps(value):
            if isinstance(value, dict) and value == {"result": "ok"}:
                fail()
            return original(value)

        monkeypatch.setattr(tools_module, "dumps", dumps)
    receipt = invoke(layer, ref, {}, "output-processing")
    assert receipt["status"] == "failed" and receipt["failure_code"] == "invalid_output"
    assert "RecursionError" in receipt["diagnostics"]["message"]
    assert invoke(layer, ref, {}, "output-processing")["replayed"]
    assert rt.calls == 1


def test_unrepresentable_output_finishes_failed_receipt(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    ref = add_tool(layer, "surrogate", r'''print(r'"\ud800"')''')
    receipt = invoke(layer, ref, {}, "surrogate")
    assert receipt["status"] == "failed" and receipt["failure_code"] == "invalid_output"
    assert "UnicodeEncodeError" in receipt["diagnostics"]["message"]
    assert rt.calls == 1


def test_pending_authoring_recovery_blocks_new_execution_but_preserves_evidence(tmp_path):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)
    ref = add_tool(layer, "versioned", 'print(\'{"version": 1}\')')
    first = invoke(layer, ref, {}, "old")
    crash_at(layer, "after_receipt_recorded")
    with pytest.raises(SimulatedCrash):
        invoke(layer, ref, {}, "uncertain")
    path = "plugins/t-versioned/versioned.py"
    original = (layer.root / path).read_bytes()
    from tests.conftest import sha
    crash_at(layer, "before_finalize")
    with pytest.raises(SimulatedCrash):
        layer.authoring.apply(ATTR, [{"op": "edit", "path": path, "expected_sha256": sha(original),
                                      "content": 'print(\'{"version": 2}\')'}])
    restored = reopen(layer, runtimes={"python": rt})
    assert restored.recovery_required
    assert restored.tools.receipt("old", actor=ACTOR)["receipt"]["output"] == first["output"]
    assert invoke(restored, ref, {}, "old")["replayed"]
    assert restored.tools.reconcile("uncertain", "no_effect_confirmed", "synthetic evidence", ACTOR)["receipt"]["status"] == "reconciled"
    assert invoke(restored, ref, {}, "uncertain")["replayed"]
    for call in (lambda: invoke(restored, ref, {}, "new"),
                 lambda: restored.tools.test(ref, ACTOR, cases=[
                     {"name": "explicit", "input": {}, "expect": {"status": "succeeded"}}])):
        with pytest.raises(AgenticError) as error:
            call()
        assert error.value.code == "recovery_required"
    with pytest.raises(AgenticError) as error:
        restored.tools.receipt("new")
    assert error.value.code == "not_found"
    assert rt.calls == 1
    with restored._ctx.store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM invocations").fetchone()[0] == 2
    restored.recover()
    assert invoke(restored, ref, {}, "new")["output"] == {"version": 2}
    assert rt.calls == 2


def test_output_processing_preserves_process_crash_baseexception(tmp_path, monkeypatch):
    rt = runtime()
    layer = tool_layer(tmp_path, rt)

    def interrupted(_):
        raise SimulatedCrash("interrupted output processing")

    monkeypatch.setattr(layer.tools, "_parse_output", interrupted)
    with pytest.raises(SimulatedCrash):
        invoke(layer, NORMALIZE, {"rows": []}, "processing-crash")
    receipt = layer.tools.receipt("processing-crash")["receipt"]
    assert receipt["status"] == "in_flight" and rt.calls == 1
    assert invoke(layer, NORMALIZE, {"rows": []}, "processing-crash")["replayed"]
    assert rt.calls == 1
