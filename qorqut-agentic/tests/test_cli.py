"""Exercise the installed command boundary using only disposable data."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import qorqut_agentic
from qorqut_agentic.cli import MAX_OUTPUT_BYTES, dispatch, execute, load_runtimes
from qorqut_agentic import AgenticError
from tests.conftest import ATTR, EXAMPLES, GIT_USER, sha


def command(tmp_path, request, *extra):
    raw = request if isinstance(request, bytes) else json.dumps(request).encode()
    proc = subprocess.run([sys.executable, "-m", "qorqut_agentic", "--root", str(tmp_path / "root"),
                           "--state", str(tmp_path / "state"), *extra], input=raw, capture_output=True,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                               "PYTHONPATH": str(Path(qorqut_agentic.__file__).resolve().parent.parent)})
    assert proc.stderr == b""
    result = json.loads(proc.stdout)
    assert len(proc.stdout) <= MAX_OUTPUT_BYTES + 1
    assert proc.returncode == (0 if result["ok"] else 1)
    return result


def test_json_lifecycle_authoring_and_feed(tmp_path):
    assert command(tmp_path, {"operation": "catalog.list", "arguments": {}})["error"]["code"] == "invalid_root"
    assert not (tmp_path / "root").exists() and not (tmp_path / "state").exists()
    assert command(tmp_path, {"operation": "lifecycle.initialize", "arguments": {"git_user": GIT_USER}})["ok"]
    imported = command(tmp_path, {"operation": "authoring.import_package", "arguments": {
        "attribution": ATTR, "package_id": "email-composition", "snapshot": {
            "directory": str(EXAMPLES / "email-composition")}}})
    assert imported["ok"] and imported["result"]["change"]["sequence"] == 1
    entry = command(tmp_path, {"operation": "catalog.read_entry", "arguments": {"ref": "email-composition/style"}})
    path, content = entry["result"]["entry"]["root_path"], entry["result"]["body"]
    changed = command(tmp_path, {"operation": "authoring.apply", "arguments": {"attribution": ATTR, "changes": [
        {"op": "edit", "path": path, "expected_sha256": sha(content), "content": content + "No exclamation marks.\n"}]}})
    assert changed["ok"] and changed["result"]["change"]["sequence"] == 2
    feed = command(tmp_path, {"operation": "changes.list", "arguments": {"consumer": "overseer"}})
    assert len(feed["result"]["changes"]) == 2
    assert command(tmp_path, {"operation": "history.status", "arguments": {}})["result"]["head"] is None
    assert command(tmp_path, {"operation": "changes.advance_cursor", "arguments": {"consumer": "overseer", "sequence": 1}})["ok"]
    assert command(tmp_path, {"operation": "changes.cursor", "arguments": {"consumer": "overseer"}})["result"]["cursor"] == 1
    assert command(tmp_path, {"operation": "lifecycle.open", "arguments": {}})["result"]["recovery_required"] is False


@pytest.mark.parametrize("payload", [
    b'{"operation":"catalog.list","arguments":{},"arguments":{}}',
    b'{"operation":"catalog.list","arguments":{"limit":NaN}}',
    b'{} {}', b'\xff', b'[]', b'{}',
    {"operation": "__dict__", "arguments": {}},
    {"operation": "catalog.list", "arguments": {}, "extra": True},
    {"operation": "catalog.list", "arguments": []},
    {"operation": [], "arguments": {}},
    {"operation": "lifecycle.initialize", "arguments": {"runtimes": {"python": "/bin/sh"}}},
    {"operation": "lifecycle.initialize", "arguments": {"git_user": True}},
])
def test_malformed_requests_never_initialize(tmp_path, payload):
    result = command(tmp_path, payload)
    assert result["error"]["code"] == "invalid_request"
    assert not (tmp_path / "root").exists() and not (tmp_path / "state").exists()


@pytest.mark.parametrize("arguments", [{"limit": True}, {"query": []}, {"surprise": 1}, {"kinds": "skill"},
                                       {"include_unavailable": "false"}])
def test_dispatch_rejects_unknown_arguments_and_wrong_types(layer, arguments):
    with pytest.raises(AgenticError) as caught:
        dispatch(layer, {"operation": "catalog.list", "arguments": arguments})
    assert caught.value.code == "invalid_request"


def test_runtime_configuration_is_explicit_not_request_json(layer, tmp_path):
    assert load_runtimes(None) == {}
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps({"local_process": {"interpreters": {"python": sys.executable}, "env": {}}}))
    assert set(load_runtimes(str(config))) == {"python"}
    with pytest.raises(AgenticError):
        execute({"operation": "lifecycle.open", "arguments": {"runtime_config": str(config)}},
                root=str(layer.root), state=str(layer.state))
    # Importing metadata never runs code. Invocation needs an explicit runtime.
    from tests.conftest import import_example
    import_example(layer, "example-project-workflow")
    with pytest.raises(AgenticError) as caught:
        dispatch(layer, {"operation": "tools.invoke", "arguments": {
            "ref": "example-project-workflow/normalize-results", "arguments": {"names": []},
            "invocation_id": "cli-missing-runtime", "actor": {"actor_id": "test", "actor_kind": "owner"}}})
    assert caught.value.code == "runtime_unavailable"


def test_oversized_predispatch_error_never_claims_completion(tmp_path):
    result = command(tmp_path, {"operation": "x" * (MAX_OUTPUT_BYTES + 1), "arguments": {}})
    assert result["error"]["code"] == "invalid_request"
    assert result["error"]["details"]["truncated"] is True
    assert "operation_completed" not in result["error"]["details"]
    assert not (tmp_path / "root").exists() and not (tmp_path / "state").exists()


def run_with_dispatch_result(monkeypatch, tmp_path, dispatch):
    import io
    from types import SimpleNamespace
    import qorqut_agentic.cli as cli

    stdin = io.BytesIO(b'{"operation":"catalog.list","arguments":{}}')
    stdout = io.BytesIO()
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(buffer=stdin))
    monkeypatch.setattr(cli.sys, "stdout", SimpleNamespace(buffer=stdout))
    monkeypatch.setattr(cli, "execute", dispatch)
    status = cli.main(["--root", str(tmp_path / "root"), "--state", str(tmp_path / "state")])
    assert len(stdout.getvalue()) <= MAX_OUTPUT_BYTES + 1
    return status, json.loads(stdout.getvalue())


def test_oversized_success_reports_completed_operation(monkeypatch, tmp_path):
    status, result = run_with_dispatch_result(monkeypatch, tmp_path,
        lambda *_args, **_kwargs: "x" * (MAX_OUTPUT_BYTES + 1))
    assert status == 1 and result["error"]["code"] == "limit_exceeded"
    assert result["error"]["details"]["operation_completed"] is True


@pytest.mark.parametrize("oversized", [False, True])
def test_blocked_verification_error_preserves_recorded_attempt(monkeypatch, tmp_path, oversized):
    from qorqut_agentic.errors import VerificationBlockedError

    def blocked(*_args, **_kwargs):
        raise VerificationBlockedError("verifier execution blocked", details={
            "run": {"run_id": "synthetic-run", "events": "x" * (MAX_OUTPUT_BYTES + 1) if oversized else []},
            "operation_id": "synthetic-operation", "reason": "runtime_unavailable", "attempt_recorded": True})

    status, result = run_with_dispatch_result(monkeypatch, tmp_path, blocked)
    error = result["error"]
    assert status == 1 and error["code"] == "verification_blocked"
    assert error["details"]["attempt_recorded"] is True
    assert "operation_completed" not in error["details"]
    if oversized:
        assert error["details"]["truncated"] is True
        assert "run" not in error["details"]
    else:
        assert error["details"]["run"]["run_id"] == "synthetic-run"
        assert error["details"]["operation_id"] == "synthetic-operation"
