"""Native callers use their own configured Chrome lease-free; no turn gates a command."""
import json
import subprocess
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from bazarlyk.bootstrap import browser
from bazarlyk.browser.access import BusinessBrowserAccess
from qorqut_operations.contracts import DomainError, Principal, new_id
from bazarlyk.transport.auth import AuthenticatedPrincipal
from test_browser_worker_runtime_path import _configured_runtime, _configured_running
from test_operational_delegation_store import setup_store


@pytest.fixture
def env(tmp_path, monkeypatch):
    database, store, parent, _business, now = setup_store(tmp_path)
    database.delegation = store
    runtime = _configured_runtime(tmp_path, database, store, parent)
    calls = []

    def run(workspace, command, **kwargs):
        calls.append((workspace, command))
        return 0, '{"ok":true}', ''
    monkeypatch.setattr(browser, "run", run)
    return SimpleNamespace(database=database, store=store, parent=parent, runtime=runtime, calls=calls,
        access=BusinessBrowserAccess(runtime), later=lambda: (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        actor=AuthenticatedPrincipal(Principal(parent, "agent"), registration_id=new_id(), credential_id=new_id()))


def tool(e, action, actor=None, **body):
    return e.access.tool(actor or e.actor, action, body)


def count(e, table):
    with e.database.read() as uow:
        return uow.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def stuck_turn(e, consumer):
    """The live deadlock shape: an owning native turn whose command ended uncertain."""
    queue = e.runtime.browser_turns
    owner = dict(parent_principal_id=e.parent, requester_id="native:" + e.parent + ":" + consumer, generation=None)
    with e.database.transaction() as uow:
        turn = queue.acquire(uow, browser_key="configured:" + e.parent, business_id=None, requester_kind="agent", **owner)
        owner["lease_id"] = turn["lease_id"]
        queue.begin_action(uow, turn["id"], **owner, action_id="utc-activate-0122", command=["eval", "() => document.title"])
        queue.settle_action(uow, turn["id"], **owner, action_id="utc-activate-0122", state="uncertain",
                            outcome={"error": "timed out"}, error="Browser command timed out")
    queue.clock = e.later  # lease and command fence are both long expired
    return turn


def test_stuck_configured_turn_no_longer_blocks_native_calls(env):
    e = env
    consumer = new_id()
    turn = stuck_turn(e, consumer)
    receipt = tool(e, "acquire", protocol="handles", consumer_id=consumer)
    handle = receipt["handle"]
    assert receipt == {"status": "owning", "handle": handle}
    assert tool(e, "command", handle=handle, command=["observe", "overview"], action_id="next")["exit_code"] == 0
    assert tool(e, "command", consumer_id=consumer, command=["observe", "overview"])["exit_code"] == 0
    assert tool(e, "status", handle=handle) == {"status": "owning"}
    assert tool(e, "reconcile", handle=handle, action_id="utc-activate-0122") == {"status": "owning"}
    assert tool(e, "release", handle=handle) == {"status": "released"}
    assert tool(e, "command", handle=handle, command=["observe", "overview"])["exit_code"] == 0
    assert len(e.calls) == 3 and e.calls[0][0] == e.calls[1][0] == e.calls[2][0]
    with e.database.transaction() as uow:
        assert uow.connection.execute("SELECT state FROM browser_turns WHERE id=?", (turn["id"],)).fetchone()[0] == "owning"
        # Startup recovery releases the leftover row; the action stays as history.
        assert e.runtime.browser_turns.recover(uow) == 1
        assert uow.connection.execute("SELECT state FROM browser_turns WHERE id=?", (turn["id"],)).fetchone()[0] == "released"
        assert uow.connection.execute("SELECT state FROM browser_turn_actions").fetchone()[0] == "uncertain"


def test_leftover_native_turn_does_not_block_configured_workers(env):
    e = env
    stuck_turn(e, new_id())
    assignment = _configured_running(e.database, e.store, e.parent, "configured worker")
    worker = replace(e.actor, delegation_binding_id=new_id(), delegation_assignment_id=assignment["id"],
                     delegation_generation=assignment["execution_generation"])
    assert tool(e, "acquire", worker, protocol="handles")["status"] == "owning"


def test_timeout_returns_error_and_next_command_runs(env, monkeypatch):
    e = env
    outcomes = [browser.BrowserTimeout("Browser command timed out"), (0, "after", "")]

    def run(workspace, command, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(browser, "run", run)
    handle = tool(e, "acquire", protocol="handles", consumer_id=new_id())["handle"]
    with pytest.raises(DomainError, match="inspect the page before repeating a state-changing action") as timed_out:
        tool(e, "command", handle=handle, command=["click", "e1"], action_id="first")
    assert timed_out.value.code == "browser_timeout"
    assert tool(e, "command", handle=handle, command=["click", "e1"], action_id="first")["stdout"] == "after"
    assert count(e, "browser_turns") == count(e, "browser_turn_actions") == 0


def test_runner_reports_a_killed_cli_as_browser_timeout(tmp_path, monkeypatch):
    def expire(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 45)
    monkeypatch.setattr(browser.subprocess, "run", expire)
    with pytest.raises(browser.BrowserTimeout):
        browser.run(tmp_path, ["click", "e1"], target=("http://127.0.0.1:9", Path("/bin/true")), capture=True)


def test_two_native_consumers_run_without_waiting(env, monkeypatch):
    e = env
    first = tool(e, "acquire", protocol="handles", consumer_id=new_id())["handle"]
    second = tool(e, "acquire", protocol="handles", consumer_id=new_id())["handle"]
    started, finish = threading.Event(), threading.Event()

    def run(workspace, command, **kwargs):
        e.calls.append((workspace, command))
        if command[0] == "goto":
            started.set()
            assert finish.wait(5)
        return 0, command[0], ""
    monkeypatch.setattr(browser, "run", run)
    results = []
    slow = threading.Thread(target=lambda: results.append(
        tool(e, "command", handle=first, command=["goto", "https://example.com"])))
    slow.start()
    assert started.wait(5)
    # The second session runs while the first command is still in flight.
    assert tool(e, "command", handle=second, command=["observe", "overview"])["stdout"] == "observe"
    finish.set()
    slow.join(5)
    assert results[0]["stdout"] == "goto" and first != second
    assert e.calls[0][0] != e.calls[1][0]


def test_foreign_principal_cannot_reach_another_agents_configured_browser(env):
    e = env
    handle = tool(e, "acquire", protocol="handles", consumer_id=new_id())["handle"]
    stranger = replace(e.actor, principal=Principal(new_id(), "agent"))
    with pytest.raises(DomainError, match="unavailable for this caller"):
        tool(e, "command", stranger, handle=handle, command=["observe", "overview"])
    with pytest.raises(DomainError) as selected:
        tool(e, "command", stranger, browser_key="configured:" + e.parent, command=["observe", "overview"])
    assert selected.value.code == "forbidden"
    with pytest.raises(DomainError) as own:
        tool(e, "command", stranger, command=["observe", "overview"])
    assert own.value.code == "operation_unavailable"
    same_agent = replace(e.actor, credential_id=new_id(), registration_id=new_id())
    assert tool(e, "command", same_agent, handle=handle, command=["observe", "overview"])["exit_code"] == 0
    assert len(e.calls) == 1


def test_native_artifacts_are_read_through_the_same_handle(env, monkeypatch):
    e = env

    def run(workspace, command, **kwargs):
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "screen.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return 0, "", ""
    monkeypatch.setattr(browser, "run", run)
    handle = tool(e, "acquire", protocol="handles", consumer_id=new_id())["handle"]
    artifact = tool(e, "command", handle=handle, command=["screenshot", "screen.png"])["artifacts"][0]["artifact_handle"]
    assert tool(e, "artifact", handle=handle, artifact_handle=artifact)["_mcp_images"][0]["mime_type"] == "image/png"
    stranger = replace(e.actor, principal=Principal(new_id(), "agent"))
    with pytest.raises(DomainError):
        tool(e, "artifact", stranger, handle=e.access.native_handle(stranger.principal.id, "default"),
             artifact_handle=artifact)


def test_handles_issued_before_lease_free_stay_usable_by_their_agent(env):
    e = env
    turn = stuck_turn(e, new_id())
    with e.database.transaction() as uow:
        uow.connection.execute("INSERT INTO browser_handles (id,turn_id,binding_json,arguments_json) VALUES (?,?,?,?)",
                               ("b0123456789ab", turn["id"], json.dumps({"principal_id": e.parent}), "{}"))
    assert tool(e, "command", handle="b0123456789ab", command=["observe", "overview"])["exit_code"] == 0
    stranger = replace(e.actor, principal=Principal(new_id(), "agent"))
    with pytest.raises(DomainError, match="unavailable for this caller"):
        tool(e, "command", stranger, handle="b0123456789ab", command=["observe", "overview"])


def test_main_session_configured_control_is_lease_free(env):
    e = env
    stuck_turn(e, "default")
    principal = e.actor.principal
    for consumer in (new_id(), new_id()):
        acquired = e.access.configured_control(principal, "acquire", consumer_id=consumer)
        assert acquired["turn"]["id"] is None
        assert acquired["target"]["cdp_endpoint"] == "http://127.0.0.1:45678"
    assert e.access.configured_control(principal, "renew", consumer_id=consumer)["turn"]["state"] == "owning"
    assert e.access.configured_control(principal, "release", consumer_id=consumer)["turn"]["state"] == "released"
    assert count(e, "browser_turns") == 1
