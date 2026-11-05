"""Focused local-dispatch contracts; no Codex process or provider is launched."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from bazarlyk.agents.operational_delegation import OperationalDelegation, OperationalDelegationOperations
from bazarlyk.bootstrap.client import auth_headers, load_registration
from bazarlyk.bootstrap.delegated_runtime import (
    DelegatedRuntime, supported_cli_version, worker_command, worker_registration,
)
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, Principal, new_id
from bazarlyk.inbox.service import InboxService
from test_operational_delegation_store import setup_store


def _dispatch_fixture(tmp_path, monkeypatch, *, max_turns=5, max_runtime_seconds=30):
    database, store, parent_id, business_id, now = setup_store(tmp_path)
    store.clock = lambda: now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    registration_id, consumer_id = new_id(), new_id()
    workspace = tmp_path / "parent"
    workspace.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    with database.transaction() as uow:
        uow.repository("machine_registrations").insert({"id": registration_id,
            "principal_id": parent_id, "workspace": str(workspace), "service_url": "http://127.0.0.1:9000",
            "profile_revision": 1, "projection_json": {},
            "compatibility_json": {"durable_delegation_enabled": True},
            "created_at": store.clock(), "updated_at": store.clock()})
        saved = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id, "record_ids": {}, "source_ids": {}},
            objective="Complete one approved operational task", plan={"steps": ["review", "complete"]},
            allowed_operations=["tasks_get"],
            resource_bounds={"max_turns": max_turns, "max_runtime_seconds": max_runtime_seconds})
        uow.connection.execute("INSERT INTO inbox_watchers(agent_id,consumer_id,last_check_at,lease_until) "
            "VALUES(?,?,?,?)", (parent_id, consumer_id, store.clock(),
                (now + timedelta(minutes=15)).isoformat(timespec="microseconds").replace("+00:00", "Z")))
    app = type("App", (), {"database": database, "current_context": staticmethod(lambda context, _uow: context)})()
    inbox = InboxService(app, None, clock=lambda: now)
    operations = OperationalDelegationOperations(store)
    operations.application, operations.inbox = app, inbox
    context = InvocationContext(Principal(parent_id, "agent", "Astra"), profile_revision=1,
        global_capabilities=frozenset({"manage_delegated_assignments"}),
        role_artifact_ref=ArtifactRef("ordubashy", "1.34.0", "sha256:" + "a" * 64),
        session_registration_id=registration_id)
    return database, store, operations, context, registration_id, consumer_id, workspace, saved


def test_worker_registration_is_private_and_outside_parent_project(tmp_path):
    parent_id, assignment_id, registration_id = new_id(), new_id(), new_id()
    state_root = tmp_path / "private-state"
    parent_workspace = tmp_path / "parent-project"
    parent_workspace.mkdir()
    token = "worker-token-" + "x" * 48
    claim = {"assignment": {"id": assignment_id}, "worker_registration_id": registration_id,
        "worker_workspace": str(state_root / parent_id / assignment_id),
        "service_url": "http://127.0.0.1:9000", "worker_token": token}
    parent = {"agent_id": parent_id, "workspace": str(parent_workspace),
        "service_url": "http://127.0.0.1:9000"}

    registration, path = worker_registration(claim, parent, state_root=state_root)

    loaded = load_registration(path)
    assert registration == loaded
    assert Path(registration["workspace"]).is_relative_to(state_root)
    assert not Path(registration["workspace"]).is_relative_to(parent_workspace)
    credential = Path(registration["workspace"]) / registration["credential_file"]
    assert credential.stat().st_mode & 0o777 == 0o600
    assert auth_headers(loaded)["Authorization"] == "Bearer " + token
    assert token not in path.read_text()
    assert loaded["durable_delegation_enabled"] is False


def test_worker_registration_rejects_substituted_private_workspace(tmp_path):
    parent_id, assignment_id, registration_id = new_id(), new_id(), new_id()
    state_root = tmp_path / "private-state"
    parent_workspace = tmp_path / "parent-project"
    parent_workspace.mkdir()
    claim = {"assignment": {"id": assignment_id}, "worker_registration_id": registration_id,
        "worker_workspace": str(state_root / new_id() / assignment_id),
        "service_url": "http://127.0.0.1:9000", "worker_token": "worker-token-" + "x" * 48}
    parent = {"agent_id": parent_id, "workspace": str(parent_workspace),
        "service_url": "http://127.0.0.1:9000"}

    with pytest.raises(DomainError, match="escaped the private runtime state root"):
        worker_registration(claim, parent, state_root=state_root)


def test_worker_command_resumes_with_only_scoped_mcp_and_no_local_tools(tmp_path):
    workspace = tmp_path / "isolated-worker"
    registration_path = workspace / ".bazarlyk/registration.json"
    registration = {"registration_id": new_id(), "agent_id": new_id(),
        "workspace": str(workspace), "service_url": "http://127.0.0.1:9000",
        "credential_file": ".bazarlyk/credential"}
    assignment = {"selected_model": "gpt-6-luna", "reasoning_effort": "low"}

    assert supported_cli_version("codex-cli 0.155.1")
    assert supported_cli_version("codex-cli 0.159.3")
    assert supported_cli_version("codex-cli 0.159.2")
    assert supported_cli_version("codex-cli 0.159.4")
    assert not supported_cli_version("codex-cli 0.159.3-beta")
    assert supported_cli_version("codex-cli 0.154.9")
    assert supported_cli_version("codex-cli 0.156.0")
    command = worker_command(assignment, registration, registration_path)
    overrides = [tomllib.loads(value) for value in command if value.startswith((
        "features=", "mcp_servers.", "permissions.", "default_permissions=", "web_search="))]
    config = {}
    for override in overrides:
        for key, value in override.items():
            config[key] = value
    assert "--ignore-user-config" in command and "--skip-git-repo-check" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert config["features"] == {"apps": False, "plugins": False, "code_mode": False,
        "multi_agent": False, "browser_use": False, "shell_tool": False}
    assert config["web_search"] == "disabled"
    assert set(config["mcp_servers"]) == {"bazarlyk"}
    assert config["mcp_servers"]["bazarlyk"]["url"] == "http://127.0.0.1:9000/mcp"
    assert "mcp_headers" in config["mcp_servers"]["bazarlyk"]["http_headers_helper"]
    assert config["mcp_servers"]["bazarlyk"]["required"] is True
    permissions = config["permissions"]["qorqut-delegated-worker"]
    assert permissions["filesystem"]["/"] == "deny"
    assert permissions["filesystem"][str(workspace / ".bazarlyk/credential")] == "deny"
    assert permissions["network"]["enabled"] is False

    resumed = worker_command(assignment, registration, registration_path,
        thread_id="0199a213-81c0-7800-8aa1-bbab2a035a53")
    assert resumed[1:5] == ["exec", "resume", "--json", "0199a213-81c0-7800-8aa1-bbab2a035a53"]
    assert "--sandbox" not in resumed
    assert "--ignore-user-config" in resumed


def test_dispatch_requires_opted_in_registration_and_persists_turn_usage(tmp_path, monkeypatch):
    database, store, operations, context, registration_id, consumer_id, workspace, saved = _dispatch_fixture(
        tmp_path, monkeypatch, max_turns=1)
    result = operations.claim_next_for_dispatch(context, registration_id, consumer_id)
    assert result["assignment"]["id"] == saved["id"]
    assert Path(result["worker_workspace"]).is_relative_to(tmp_path / "state")
    assert not Path(result["worker_workspace"]).is_relative_to(workspace)

    thread_id = new_id()
    polled = operations.poll_worker(context, registration_id, consumer_id, saved["id"],
        result["generation"], runtime_thread_id=thread_id, turn_number=1,
        turn_usage={"input_tokens": 20, "cached_input_tokens": 4, "output_tokens": 8,
                    "reasoning_output_tokens": 2})

    assert polled["assignment"]["runtime_thread_id"] == thread_id
    assert polled["assignment"]["state"] == "blocked"
    assert polled["assignment"]["worker_usage"] == {
        "source": "codex_cli_turn_completed", "turns": 1, "input_tokens": 20,
        "cached_input_tokens": 4, "output_tokens": 8, "reasoning_output_tokens": 2}
    with database.read() as uow:
        events = uow.connection.execute("SELECT event_type FROM operational_assignment_events "
            "WHERE assignment_id=? ORDER BY sequence", (saved["id"],)).fetchall()
        assert [row["event_type"] for row in events].count("worker_turn_completed") == 1
        assert uow.connection.execute("SELECT count(*) FROM inbox_events WHERE source_type='operational_assignments' "
            "AND source_id=?", (saved["id"],)).fetchone()[0] == 1


def test_runtime_budget_excludes_durable_wait_time(tmp_path):
    database, store, parent_id, business_id, now = setup_store(tmp_path)
    store.clock = lambda: now.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with database.transaction() as uow:
        saved = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id, "record_ids": {}, "source_ids": {}},
            objective="Wait safely for an assigned reply", plan={"steps": ["wait", "continue"]},
            allowed_operations=["tasks_get"], resource_bounds={"max_runtime_seconds": 30})
        running = store.claim(uow, saved["id"], actor_principal_id=parent_id, lease_seconds=300)
    checkpoint_time = now + timedelta(seconds=12)
    store.clock = lambda: checkpoint_time.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with database.transaction() as uow:
        waiting = store.checkpoint(uow, saved["id"], actor_principal_id=parent_id,
            generation=running["execution_generation"], lease_id=running["lease_id"],
            expected_revision=running["revision"], checkpoint={"finished": ["wait"]},
            next_step="Continue after the matching reply", state="waiting",
            wake_condition={"kind": "message_thread", "thread_id": new_id()})
    assert waiting["runtime_used_seconds"] == 12
    resumed_time = checkpoint_time + timedelta(days=2)
    store.clock = lambda: resumed_time.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with database.transaction() as uow:
        queued = store.control(uow, saved["id"], parent_principal_id=parent_id,
            expected_revision=waiting["revision"], action="resume")
        next_run = store.claim(uow, saved["id"], actor_principal_id=parent_id)
    assert queued["runtime_used_seconds"] == 12
    assert next_run["runtime_used_seconds"] == 12
    assert next_run["execution_started_at"] == store.clock()


def test_worker_process_saves_thread_then_completes_without_parent_hooks(tmp_path):
    parent_workspace = tmp_path / "parent"
    parent_workspace.mkdir()
    parent = {"agent_id": new_id(), "registration_id": new_id(),
        "workspace": str(parent_workspace), "service_url": "http://127.0.0.1:9000",
        "credential_file": ".bazarlyk/credential", "durable_delegation_enabled": True}
    assignment = {"id": new_id(), "state": "running", "business_id": new_id(),
        "scope_json": {"kind": "business"}, "objective": "Finish a bounded test handoff",
        "plan_json": {"steps": ["inspect", "finish"]}, "plan_revision": 1,
        "allowed_operations_json": ["tasks_get"], "constraints_json": {},
        "internal_recipients_json": [], "completion_evidence_json": [], "escalation_rules_json": {},
        "checkpoint_json": {}, "next_step": "inspect", "resource_bounds_json": {
            "max_turns": 2, "max_runtime_seconds": 60}, "worker_usage": {"turns": 0},
        "runtime_used_seconds": 0, "runtime_thread_id": None,
        "selected_model": "gpt-6-luna", "reasoning_effort": "low"}
    generation, worker_registration_id, thread_id = 3, new_id(), new_id()
    state_root = tmp_path / "state" / "bazarlyk" / "delegated-workers"
    claim = {"assignment": assignment, "generation": generation, "worker_registration_id": worker_registration_id,
        "worker_workspace": str(state_root / parent["agent_id"] / assignment["id"]),
        "service_url": parent["service_url"], "worker_token": "scoped-" + "t" * 48, "events": []}
    turns = []

    class Client:
        def post(self, route, body):
            turns.append((route, dict(body)))
            current = dict(assignment)
            if body.get("runtime_thread_id"):
                current["runtime_thread_id"] = body["runtime_thread_id"]
            if body.get("turn_usage") is not None:
                current["state"] = "completed"
                current["worker_usage"] = {"turns": 1, "input_tokens": 12,
                    "cached_input_tokens": 2, "output_tokens": 4, "reasoning_output_tokens": 1}
            return {"assignment": current, "events": []}

    invoked = []
    script = (
        "import json,sys; "
        f"rows=[{{'type':'thread.started','thread_id':{thread_id!r}}},"
        "{'type':'turn.completed','usage':{'input_tokens':12,'cached_input_tokens':2,"
        "'output_tokens':4,'reasoning_output_tokens':1}}]; "
        "[sys.stdout.write(json.dumps(row)+'\\n') for row in rows]; sys.stdout.flush()"
    )

    def fake_popen(command, **kwargs):
        invoked.extend(command)
        return subprocess.Popen([sys.executable, "-c", script], **kwargs)

    runtime = DelegatedRuntime(parent, new_id(), alive=lambda: True, client=Client(),
        executable="codex", state_root=state_root, popen=fake_popen)
    runtime._execute(claim)

    assert len(turns) == 2
    assert all(route == "/api/agent/delegation/poll" for route, _ in turns)
    assert turns[0][1]["runtime_thread_id"] == thread_id
    assert turns[1][1]["turn_usage"]["input_tokens"] == 12
    assert "--ignore-user-config" in invoked and "--sandbox" in invoked
    assert str(parent_workspace) not in " ".join(invoked)
    assert not (Path(claim["worker_workspace"]) / ".bazarlyk/credential").exists()
    assert not (Path(claim["worker_workspace"]) / ".bazarlyk/registration.json").exists()


def test_worker_failure_is_reported_without_exposing_cli_output(tmp_path):
    parent_workspace = tmp_path / "parent"
    parent_workspace.mkdir()
    parent = {"agent_id": new_id(), "registration_id": new_id(),
        "workspace": str(parent_workspace), "service_url": "http://127.0.0.1:9000",
        "credential_file": ".bazarlyk/credential", "durable_delegation_enabled": True}
    assignment = {"id": new_id(), "state": "running", "business_id": None,
        "scope_json": {"kind": "ecosystem"}, "objective": "Fail safely",
        "plan_json": {"steps": ["run"]}, "plan_revision": 1,
        "allowed_operations_json": ["tasks_get"], "constraints_json": {},
        "internal_recipients_json": [], "completion_evidence_json": [], "escalation_rules_json": {},
        "checkpoint_json": {}, "next_step": None, "resource_bounds_json": {
            "max_turns": 2, "max_runtime_seconds": 60}, "worker_usage": {"turns": 0},
        "runtime_used_seconds": 0, "runtime_thread_id": None,
        "selected_model": "gpt-6-luna", "reasoning_effort": "low"}
    claim = {"assignment": assignment, "generation": 1, "worker_registration_id": new_id(),
        "worker_workspace": str(tmp_path / "state" / "bazarlyk" / "delegated-workers" /
            parent["agent_id"] / assignment["id"]), "service_url": parent["service_url"],
        "worker_token": "scoped-" + "t" * 48, "events": []}
    calls = []

    class Client:
        def post(self, route, body):
            calls.append((route, dict(body)))
            return {"assignment": assignment, "events": []}

    def failing_popen(_command, **kwargs):
        return subprocess.Popen([sys.executable, "-c", "import sys; sys.stderr.write('secret transcript'); sys.exit(2)"], **kwargs)

    runtime = DelegatedRuntime(parent, new_id(), alive=lambda: True, client=Client(),
        executable="codex", state_root=tmp_path / "state" / "bazarlyk" / "delegated-workers",
        popen=failing_popen)
    runtime._execute(claim)

    assert len(calls) == 1 and calls[0][0] == "/api/agent/delegation/fail"
    assert "secret transcript" not in json.dumps(calls)
    assert "reason" in calls[0][1] and "secret transcript" not in calls[0][1]["reason"]


def test_dispatcher_claims_before_parent_watcher_starts_notification_poll(tmp_path):
    parent = {"agent_id": new_id(), "registration_id": new_id(), "workspace": str(tmp_path),
        "service_url": "http://127.0.0.1:9000", "credential_file": ".bazarlyk/credential",
        "durable_delegation_enabled": True}
    claims = []

    class Client:
        def post(self, route, body):
            claims.append((route, body))
            return {"assignment": None, "reason": None}

    runtime = DelegatedRuntime(parent, new_id(), alive=lambda: True, client=Client(),
        executable="codex", state_root=tmp_path / "state", poll_interval=0.01)
    runtime.compatible = lambda: True

    assert runtime.start()
    try:
        assert claims and claims[0][0] == "/api/agent/delegation/claim"
    finally:
        runtime.stop()


def test_poll_turn_usage_is_idempotent_for_a_retried_request(tmp_path, monkeypatch):
    database, store, operations, context, registration_id, consumer_id, _workspace, saved = _dispatch_fixture(
        tmp_path, monkeypatch, max_turns=2)
    claim = operations.claim_next_for_dispatch(context, registration_id, consumer_id)
    kwargs = {"runtime_thread_id": new_id(), "turn_number": 1,
        "turn_usage": {"input_tokens": 10, "output_tokens": 3}}

    first = operations.poll_worker(context, registration_id, consumer_id, saved["id"],
        claim["generation"], **kwargs)
    retried = operations.poll_worker(context, registration_id, consumer_id, saved["id"],
        claim["generation"], **kwargs)

    assert first["assignment"]["worker_usage"]["turns"] == 1
    assert retried["assignment"]["worker_usage"]["turns"] == 1
    with database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM operational_assignment_events "
            "WHERE assignment_id=? AND event_type='worker_turn_completed'", (saved["id"],)).fetchone()[0] == 1


def test_unopted_registration_cannot_claim_assignment(tmp_path, monkeypatch):
    database, _store, operations, context, registration_id, consumer_id, _workspace, _saved = _dispatch_fixture(
        tmp_path, monkeypatch)
    with database.transaction() as uow:
        registration = uow.repository("machine_registrations").get(registration_id)
        uow.repository("machine_registrations").update(registration_id,
            {"compatibility_json": {"durable_delegation_enabled": False}},
            expected_revision=registration["revision"])

    with pytest.raises(DomainError, match="does not enable durable delegation"):
        operations.claim_next_for_dispatch(context, registration_id, consumer_id)


def test_unavailable_thread_recreates_from_checkpoint_without_parent_turn(tmp_path):
    parent_workspace = tmp_path / "parent"
    parent_workspace.mkdir()
    parent = {"agent_id": new_id(), "registration_id": new_id(),
        "workspace": str(parent_workspace), "service_url": "http://127.0.0.1:9000",
        "credential_file": ".bazarlyk/credential", "durable_delegation_enabled": True}
    old_thread, fresh_thread = new_id(), new_id()
    assignment = {"id": new_id(), "state": "running", "business_id": None,
        "scope_json": {"kind": "ecosystem"}, "objective": "Resume from the saved checkpoint",
        "plan_json": {"steps": ["continue"]}, "plan_revision": 1,
        "allowed_operations_json": ["tasks_get"], "constraints_json": {},
        "internal_recipients_json": [], "completion_evidence_json": [], "escalation_rules_json": {},
        "checkpoint_json": {"completed": ["setup"]}, "next_step": "continue",
        "resource_bounds_json": {"max_turns": 2, "max_runtime_seconds": 60},
        "worker_usage": {"turns": 0}, "runtime_used_seconds": 0,
        "runtime_thread_id": old_thread, "selected_model": "gpt-6-luna", "reasoning_effort": "low"}
    claim = {"assignment": assignment, "generation": 2, "worker_registration_id": new_id(),
        "worker_workspace": str(tmp_path / "state" / "bazarlyk" / "delegated-workers" /
            parent["agent_id"] / assignment["id"]), "service_url": parent["service_url"],
        "worker_token": "scoped-" + "t" * 48, "events": []}
    calls, commands = [], []

    class Client:
        current = dict(assignment)

        def post(self, route, body):
            calls.append((route, dict(body)))
            if body.get("reset_runtime_thread"):
                self.current = {**self.current, "runtime_thread_id": None}
            if body.get("runtime_thread_id"):
                self.current = {**self.current, "runtime_thread_id": body["runtime_thread_id"]}
            if body.get("turn_usage") is not None:
                self.current = {**self.current, "state": "completed",
                    "worker_usage": {"turns": 1, "input_tokens": 5, "cached_input_tokens": 0,
                        "output_tokens": 2, "reasoning_output_tokens": 0}}
            return {"assignment": dict(self.current), "events": []}

    script = (
        "import json,sys; "
        f"rows=[{{'type':'thread.started','thread_id':{fresh_thread!r}}},"
        "{'type':'turn.completed','usage':{'input_tokens':5,'output_tokens':2}}]; "
        "[sys.stdout.write(json.dumps(row)+'\\n') for row in rows]; sys.stdout.flush()"
    )

    def fake_popen(command, **kwargs):
        commands.append(list(command))
        code = "import sys; sys.exit(1)" if len(commands) == 1 else script
        return subprocess.Popen([sys.executable, "-c", code], **kwargs)

    state_root = tmp_path / "state" / "bazarlyk" / "delegated-workers"
    runtime = DelegatedRuntime(parent, new_id(), alive=lambda: True, client=Client(),
        executable="codex", state_root=state_root, popen=fake_popen)
    runtime._execute(claim)

    assert commands[0][1:4] == ["exec", "resume", "--json"]
    assert commands[1][1] == "exec" and "resume" not in commands[1]
    assert any(body.get("reset_runtime_thread") is True for _route, body in calls)
    assert not any(route.endswith("/fail") for route, _body in calls)
    assert calls[-1][1]["runtime_thread_id"] == fresh_thread
    assert calls[-1][1]["turn_number"] == 1


def test_worker_handoff_explains_revision_bound_completion():
    from bazarlyk.bootstrap.delegated_runtime import _prompt
    prompt = _prompt({"id": new_id(), "plan_revision": 1}, [])
    assert "call delegated_assignment_get" in prompt
    assert "expected_revision from that read" in prompt
    assert "result containing a nonempty summary" in prompt
    assert "identical payload and key" in prompt
    assert "return immediately" in prompt
