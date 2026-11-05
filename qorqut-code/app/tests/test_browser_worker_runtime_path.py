"""New browser worker path coverage: wait/resume, fencing, and action intent."""

import json
import shutil
import subprocess
import time
from dataclasses import replace
from types import SimpleNamespace
from datetime import datetime, timedelta
from pathlib import Path

import anyio

import pytest

from bazarlyk.application.catalog import builtin_registry
from bazarlyk.browser.operations import BROWSER_OPERATION, register_browser_operation
from bazarlyk.browser.turns import BrowserTurnQueue
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.contracts import InvocationContext, Principal, new_id
from bazarlyk.transport.auth import AuthenticatedPrincipal
from bazarlyk.transport.auth import Authentication
from bazarlyk.bootstrap import browser
from bazarlyk.browser.access import BusinessBrowserAccess
from test_browser_turn_integration import _port
from bazarlyk.transport.mcp import MCPTransport
from test_operational_delegation_store import setup_store


def _running(database, store, parent_id, business_id, title):
    with database.transaction() as uow:
        saved = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id, "browser_key": "business:" + business_id,
                   "record_ids": {}, "source_ids": {}}, objective=title,
            plan={"steps": ["acquire", "act", "release"]}, allowed_operations=["browser_use"],
            resource_bounds={"max_turns": 3, "max_runtime_seconds": 120})
        return store.claim(uow, saved["id"], actor_principal_id=parent_id, dispatcher_ready=True)


def _configured_running(database, store, parent_id, title):
    with database.transaction() as uow:
        saved = store.create(uow, parent_principal_id=parent_id, business_id=None,
            scope={"kind": "global", "allowed_business_ids": [], "browser_key": "configured:" + parent_id,
                   "record_ids": {}, "source_ids": {}}, objective=title,
            plan={"steps": ["acquire", "act", "release"]}, allowed_operations=["browser_use"],
            resource_bounds={"max_turns": 3, "max_runtime_seconds": 120})
        return store.claim(uow, saved["id"], actor_principal_id=parent_id, dispatcher_ready=True)


def _configured_runtime(tmp_path, database, store, parent_id):
    workspace = tmp_path / "parent"
    runtime_dir = tmp_path / "browser-runtime"
    (workspace / ".bazarlyk").mkdir(parents=True)
    (runtime_dir / "node_modules/agent-browser").mkdir(parents=True)
    (runtime_dir / "node_modules/.bin").mkdir(parents=True)
    (runtime_dir / "node_modules/agent-browser/package.json").write_text(json.dumps({
        "name": "agent-browser", "version": browser.CLI_VERSION}))
    executable = runtime_dir / "node_modules/.bin/agent-browser"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (workspace / ".bazarlyk/browser.json").write_text(json.dumps({
        "schema_version": 1, "cdp_endpoint": "http://127.0.0.1:45678",
        "runtime_dir": str(runtime_dir)}))
    runtime = SimpleNamespace(root=tmp_path, workspace=workspace, database=database,
        browser_turns=BrowserTurnQueue(clock=store.clock))
    return runtime


def test_waiting_worker_retains_fifo_row_and_dispatcher_resumes_after_release(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    database.delegation = store
    first = _running(database, store, parent_id, business_id, "first")
    second = _running(database, store, parent_id, business_id, "second")
    queue = BrowserTurnQueue(clock=store.clock)
    with database.transaction() as uow:
        first_turn = queue.acquire(uow, browser_key="business:" + business_id, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=first["id"],
            assignment_id=first["id"], generation=first["execution_generation"])
        second_turn = queue.acquire(uow, browser_key="business:" + business_id, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=second["id"],
            assignment_id=second["id"], generation=second["execution_generation"])
        waited, waiting_assignment = queue.wait(uow, second_turn["id"], parent_principal_id=parent_id,
            requester_id=second["id"], generation=second["execution_generation"], lease_id=None,
            checkpoint={"phase": "queued"}, next_action="Resume when Chrome is free")
        assert waited["state"] == "queued" and waited["position"] == 1
        released = queue.release(uow, first_turn["id"], parent_principal_id=parent_id,
            requester_id=first["id"], generation=first["execution_generation"], lease_id=first_turn["lease_id"])
        resumed = store.claim(uow, second["id"], actor_principal_id=parent_id, dispatcher_ready=True)
        owned = queue.acquire(uow, browser_key="business:" + business_id, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=second["id"],
            assignment_id=second["id"], generation=resumed["execution_generation"])
        rebound = uow.connection.execute("SELECT execution_generation,state FROM browser_turns WHERE id=?",
                                          (second_turn["id"],)).fetchone()
    assert waiting_assignment["state"] == "waiting"
    assert released["state"] == "released"
    assert resumed["state"] == "running"
    assert rebound["execution_generation"] == resumed["execution_generation"]
    assert owned["state"] == "owning"


def test_woken_browser_assignment_keeps_reserved_fifo_place_until_dispatcher_claim(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    database.delegation = store
    first = _running(database, store, parent_id, business_id, "first")
    second = _running(database, store, parent_id, business_id, "second")
    third = _running(database, store, parent_id, business_id, "third")
    queue = BrowserTurnQueue(clock=store.clock)
    key = "business:" + business_id
    with database.transaction() as uow:
        first_turn = queue.acquire(uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=first["id"],
            assignment_id=first["id"], generation=first["execution_generation"])
        second_turn = queue.acquire(uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=second["id"],
            assignment_id=second["id"], generation=second["execution_generation"])
        queue.wait(uow, second_turn["id"], parent_principal_id=parent_id, requester_id=second["id"],
            generation=second["execution_generation"], lease_id=None, checkpoint={"phase": "queued"},
            next_action="Resume when Chrome is free")
        queue.release(uow, first_turn["id"], parent_principal_id=parent_id, requester_id=first["id"],
            generation=first["execution_generation"], lease_id=first_turn["lease_id"])
        third_turn = queue.acquire(uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=third["id"],
            assignment_id=third["id"], generation=third["execution_generation"])
        reserved = uow.connection.execute("SELECT state FROM browser_turns WHERE id=?", (second_turn["id"],)).fetchone()
        claimed = store.claim(uow, second["id"], actor_principal_id=parent_id, dispatcher_ready=True)
        resumed = queue.acquire(uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=second["id"],
            assignment_id=second["id"], generation=claimed["execution_generation"])
        third_state = uow.connection.execute("SELECT state FROM browser_turns WHERE id=?", (third_turn["id"],)).fetchone()[0]
    assert third_turn["state"] == "queued"
    assert reserved["state"] == "queued"
    assert claimed["state"] == "running"
    assert resumed["state"] == "owning"
    assert third_state == "queued"


def test_promotion_gap_does_not_let_later_acquisition_steal_woken_turn(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    database.delegation = store
    first = _running(database, store, parent_id, business_id, "gap owner")
    waiting = _running(database, store, parent_id, business_id, "gap waiter")
    later = _running(database, store, parent_id, business_id, "gap later")
    queue = BrowserTurnQueue(clock=store.clock)
    key = "business:" + business_id
    with database.transaction() as uow:
        owner = queue.acquire(uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=first["id"],
            assignment_id=first["id"], generation=first["execution_generation"])
        waiter = queue.acquire(uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=waiting["id"],
            assignment_id=waiting["id"], generation=waiting["execution_generation"])
        queue.wait(uow, waiter["id"], parent_principal_id=parent_id, requester_id=waiting["id"],
            generation=waiting["execution_generation"], lease_id=None, checkpoint={"step": "wait"},
            next_action="Resume automatically")
        queue.release(uow, owner["id"], parent_principal_id=parent_id, requester_id=first["id"],
            generation=first["execution_generation"], lease_id=owner["lease_id"])
        # This acquisition is deliberately in the gap after wakeup and before
        # the dispatcher claims the reserved assignment.
        later_turn = queue.acquire(uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=later["id"],
            assignment_id=later["id"], generation=later["execution_generation"])
        reserved = uow.connection.execute("SELECT state FROM browser_turns WHERE id=?", (waiter["id"],)).fetchone()[0]
        later_state = uow.connection.execute("SELECT state FROM browser_turns WHERE id=?", (later_turn["id"],)).fetchone()[0]
    assert reserved == "queued"
    assert later_state == "queued"


def test_configured_worker_uses_persisted_target_resolver_and_decoded_checkpoint(tmp_path):
    database, store, parent_id, _business_id, _ = setup_store(tmp_path)
    database.delegation = store
    runtime = _configured_runtime(tmp_path, database, store, parent_id)
    access = BusinessBrowserAccess(runtime)
    assignment = _configured_running(database, store, parent_id, "configured target")
    actor = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"),
        registration_id=new_id(), delegation_binding_id=new_id(),
        delegation_assignment_id=assignment["id"], delegation_generation=assignment["execution_generation"])
    first = access.worker(actor, "acquire", {"action": "acquire", "browser_key": "configured:" + parent_id})
    assert first["status"] == "owning"
    assert first["target"]["cdp_endpoint"] == "http://127.0.0.1:45678"
    access.worker(actor, "release", {"action": "release", "browser_key": "configured:" + parent_id,
        "turn_id": first["turn"]["id"], "lease_id": first["turn"]["lease_id"]})


def test_worker_handler_decodes_repository_checkpoint_on_queued_acquisition(tmp_path):
    database, store, parent_id, _business_id, _ = setup_store(tmp_path)
    database.delegation = store
    runtime = _configured_runtime(tmp_path, database, store, parent_id)
    access = BusinessBrowserAccess(runtime)
    first = _configured_running(database, store, parent_id, "current browser task")
    second = _configured_running(database, store, parent_id, "queued browser task")
    actor1 = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"), registration_id=new_id(),
        delegation_binding_id=new_id(), delegation_assignment_id=first["id"],
        delegation_generation=first["execution_generation"])
    actor2 = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"), registration_id=new_id(),
        delegation_binding_id=new_id(), delegation_assignment_id=second["id"],
        delegation_generation=second["execution_generation"])
    key = "configured:" + parent_id
    owned = access.worker(actor1, "acquire", {"action": "acquire", "browser_key": key})
    waiting = access.worker(actor2, "acquire", {"action": "acquire", "browser_key": key})
    assert owned["status"] == "owning"
    assert waiting["status"] == "waiting"
    with database.read() as uow:
        saved = uow.repository("operational_assignments").get(second["id"], required=True)
    assert isinstance(saved["checkpoint_json"], dict)


def test_completed_and_cancelled_workers_release_browser_turns_and_capacity(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    database.delegation = store
    completed = _running(database, store, parent_id, business_id, "completed worker")
    cancelled = _running(database, store, parent_id, business_id, "cancelled worker")
    queue = BrowserTurnQueue(clock=store.clock)
    key = "business:" + business_id

    with database.transaction() as uow:
        completed_turn = queue.acquire(
            uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment",
            requester_id=completed["id"], assignment_id=completed["id"],
            generation=completed["execution_generation"])
        completed = store.checkpoint(
            uow, completed["id"], actor_principal_id=parent_id,
            generation=completed["execution_generation"], lease_id=completed["lease_id"],
            expected_revision=completed["revision"], checkpoint={"step": "done"},
            state="completed", result={"summary": "Browser work completed"})
        completed_turn_state = uow.connection.execute(
            "SELECT state FROM browser_turns WHERE id=?", (completed_turn["id"],)).fetchone()[0]
        running_after_completion = uow.connection.execute(
            "SELECT count(*) FROM operational_assignments WHERE parent_principal_id=? AND state='running'",
            (parent_id,)).fetchone()[0]

        cancelled_turn = queue.acquire(
            uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment",
            requester_id=cancelled["id"], assignment_id=cancelled["id"],
            generation=cancelled["execution_generation"])
        cancelled = store.control(
            uow, cancelled["id"], parent_principal_id=parent_id,
            expected_revision=cancelled["revision"], action="cancel")
        cancelled_turn_state = uow.connection.execute(
            "SELECT state FROM browser_turns WHERE id=?", (cancelled_turn["id"],)).fetchone()[0]
        running_after_cancellation = uow.connection.execute(
            "SELECT count(*) FROM operational_assignments WHERE parent_principal_id=? AND state='running'",
            (parent_id,)).fetchone()[0]

    assert completed["state"] == "completed"
    assert completed_turn_state == "released"
    assert running_after_completion == 1
    assert cancelled["state"] == "cancelled"
    assert cancelled_turn_state == "released"
    assert running_after_cancellation == 0


def test_real_dispatcher_reauthenticates_woken_worker_and_worker_handler_resumes(tmp_path, monkeypatch):
    from test_delegated_runtime_driver import _dispatch_fixture
    database, store, operations, context, registration_id, consumer_id, _workspace, _ = _dispatch_fixture(tmp_path, monkeypatch)
    database.delegation = store
    with database.transaction() as uow:
        saved = uow.repository("operational_assignments").get(_["id"])
        uow.repository("operational_assignments").update(saved["id"], {
            "scope_json": {"kind": "global", "allowed_business_ids": [], "browser_key": "configured:" + context.principal_id,
                           "record_ids": {}, "source_ids": {}}, "allowed_operations_json": ["browser_use"]},
            expected_revision=saved["revision"])
        second = store.create(uow, parent_principal_id=context.principal_id, business_id=None,
            scope={"kind": "global", "allowed_business_ids": [], "browser_key": "configured:" + context.principal_id,
                   "record_ids": {}, "source_ids": {}}, objective="second configured browser task",
            plan={"steps": ["acquire", "act"]}, allowed_operations=["browser_use"],
            resource_bounds={"max_turns": 3, "max_runtime_seconds": 120})
    first_claim = operations.claim_next_for_dispatch(context, registration_id, consumer_id)
    second_claim = operations.claim_next_for_dispatch(context, registration_id, consumer_id)
    runtime = _configured_runtime(tmp_path, database, store, context.principal_id)
    access = BusinessBrowserAccess(runtime)
    clock = lambda: datetime.fromisoformat(store.clock().replace("Z", "+00:00")).timestamp()
    authentication = Authentication(database, tmp_path, signing_key=b"k" * 48, clock=clock)
    actor1 = authentication.agent("Bearer " + first_claim["worker_token"])
    actor2 = authentication.agent("Bearer " + second_claim["worker_token"])
    first_turn = access.worker(actor1, "acquire", {"action": "acquire", "browser_key": "configured:" + context.principal_id})
    waiting = access.worker(actor2, "acquire", {"action": "acquire", "browser_key": "configured:" + context.principal_id})
    assert first_turn["status"] == "owning" and waiting["status"] == "waiting"
    access.worker(actor1, "release", {"action": "release", "browser_key": "configured:" + context.principal_id,
        "turn_id": first_turn["turn"]["id"], "lease_id": first_turn["turn"]["lease_id"]})
    resumed_claim = operations.claim_next_for_dispatch(context, registration_id, consumer_id)
    resumed_actor = authentication.agent("Bearer " + resumed_claim["worker_token"])
    resumed = access.worker(resumed_actor, "acquire", {"action": "acquire", "browser_key": "configured:" + context.principal_id})
    assert resumed_claim["assignment"]["id"] == second_claim["assignment"]["id"]
    assert resumed["status"] == "owning" and resumed["turn"]["id"] == waiting["turn"]["id"]


@pytest.mark.integration
def test_live_chrome_uses_real_target_dispatcher_and_waiter_resume(tmp_path, monkeypatch):
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    runtime_dir = Path(__file__).resolve().parents[1] / "tools/browser"
    executable = runtime_dir / "node_modules/.bin/agent-browser"
    if chrome is None or not executable.is_file():
        pytest.skip("disposable Chrome and the locked agent-browser runtime are required")
    from test_delegated_runtime_driver import _dispatch_fixture
    database, store, operations, context, registration_id, consumer_id, parent_workspace, saved = _dispatch_fixture(tmp_path, monkeypatch)
    database.delegation = store
    with database.transaction() as uow:
        current = uow.repository("operational_assignments").get(saved["id"])
        uow.repository("operational_assignments").update(current["id"], {
            "scope_json": {"kind": "global", "allowed_business_ids": [], "browser_key": "configured:" + context.principal_id,
                           "record_ids": {}, "source_ids": {}}, "allowed_operations_json": ["browser_use"]},
            expected_revision=current["revision"])
        operations.store.create(uow, parent_principal_id=context.principal_id, business_id=None,
            scope={"kind": "global", "allowed_business_ids": [], "browser_key": "configured:" + context.principal_id,
                   "record_ids": {}, "source_ids": {}}, objective="live waiting Chrome task",
            plan={"steps": ["acquire", "inspect"]}, allowed_operations=["browser_use"],
            resource_bounds={"max_turns": 3, "max_runtime_seconds": 120})
    first_claim = operations.claim_next_for_dispatch(context, registration_id, consumer_id)
    second_claim = operations.claim_next_for_dispatch(context, registration_id, consumer_id)
    browser_port = _port()
    chrome_profile = tmp_path / "chrome-profile"
    chrome_process = subprocess.Popen([
        chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={browser_port}",
        f"--user-data-dir={chrome_profile}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        endpoint = f"http://127.0.0.1:{browser_port}"
        import urllib.request
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(endpoint + "/json/version", timeout=0.5) as response:
                    if json.loads(response.read()).get("Browser"):
                        break
            except (OSError, ValueError):
                time.sleep(0.1)
        else:
            pytest.fail("disposable Chrome did not expose CDP")
        (parent_workspace / ".bazarlyk").mkdir(parents=True, exist_ok=True)
        (parent_workspace / ".bazarlyk/browser.json").write_text(json.dumps({
            "schema_version": 1, "cdp_endpoint": endpoint, "runtime_dir": str(runtime_dir)}))
        runtime = SimpleNamespace(root=tmp_path, workspace=parent_workspace, database=database,
            browser_turns=BrowserTurnQueue(clock=store.clock))
        access = BusinessBrowserAccess(runtime)
        clock = lambda: datetime.fromisoformat(store.clock().replace("Z", "+00:00")).timestamp()
        authentication = Authentication(database, tmp_path, signing_key=b"k" * 48, clock=clock)
        actor1 = authentication.agent("Bearer " + first_claim["worker_token"])
        actor2 = authentication.agent("Bearer " + second_claim["worker_token"])
        key = "configured:" + context.principal_id
        first_turn = access.worker(actor1, "acquire", {"action": "acquire", "browser_key": key})
        waiting = access.worker(actor2, "acquire", {"action": "acquire", "browser_key": key})
        assert first_turn["status"] == "owning" and waiting["status"] == "waiting"
        first_work = access.worker(actor1, "command", {"action": "command", "browser_key": key,
            "turn_id": first_turn["turn"]["id"], "lease_id": first_turn["turn"]["lease_id"],
            "turn_token": first_turn["turn_token"], "action_id": new_id(), "command": ["goto", "about:blank"]})
        assert first_work["exit_code"] == 0
        access.worker(actor1, "release", {"action": "release", "browser_key": key,
            "turn_id": first_turn["turn"]["id"], "lease_id": first_turn["turn"]["lease_id"],
            "turn_token": first_turn["turn_token"]})
        resumed_claim = operations.claim_next_for_dispatch(context, registration_id, consumer_id)
        resumed_actor = authentication.agent("Bearer " + resumed_claim["worker_token"])
        resumed = access.worker(resumed_actor, "acquire", {"action": "acquire", "browser_key": key})
        resumed_work = access.worker(resumed_actor, "command", {"action": "command", "browser_key": key,
            "turn_id": resumed["turn"]["id"], "lease_id": resumed["turn"]["lease_id"],
            "turn_token": resumed["turn_token"], "action_id": new_id(), "command": ["eval", "() => document.title"]})
        assert resumed_claim["assignment"]["id"] == second_claim["assignment"]["id"]
        assert resumed["turn"]["id"] == waiting["turn"]["id"] and resumed_work["exit_code"] == 0
        access.worker(resumed_actor, "release", {"action": "release", "browser_key": key,
            "turn_id": resumed["turn"]["id"], "lease_id": resumed["turn"]["lease_id"],
            "turn_token": resumed["turn_token"]})
    finally:
        chrome_process.terminate()
        try:
            chrome_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome_process.kill()
            chrome_process.wait(timeout=5)


def test_expiry_does_not_promote_waiter_until_delayed_command_settles_and_delivers_artifacts(tmp_path, monkeypatch):
    database, store, parent_id, _business_id, start = setup_store(tmp_path)
    database.delegation = store
    current = [start]
    runtime = _configured_runtime(tmp_path, database, store, parent_id)
    runtime.browser_turns = BrowserTurnQueue(clock=lambda: current[0], lease_seconds=30)
    access = BusinessBrowserAccess(runtime)
    first = _configured_running(database, store, parent_id, "delayed command")
    second = _configured_running(database, store, parent_id, "waiting command")
    actor1 = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"), registration_id=new_id(),
        delegation_binding_id=new_id(), delegation_assignment_id=first["id"],
        delegation_generation=first["execution_generation"])
    actor2 = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"), registration_id=new_id(),
        delegation_binding_id=new_id(), delegation_assignment_id=second["id"],
        delegation_generation=second["execution_generation"])
    first_turn = access.worker(actor1, "acquire", {"action": "acquire", "browser_key": "configured:" + parent_id})
    waiting = access.worker(actor2, "acquire", {"action": "acquire", "browser_key": "configured:" + parent_id})
    assert waiting["status"] == "waiting"
    observed = {}

    def delayed_run(workspace, _command, *, target, instance, capture):
        with database.read() as uow:
            observed["state_during_command"] = uow.connection.execute(
                "SELECT state FROM browser_turns WHERE id=?", (waiting["turn"]["id"],)).fetchone()[0]
        (workspace / "screen.png").parent.mkdir(parents=True, exist_ok=True)
        (workspace / "screen.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        current[0] = start + timedelta(seconds=31)
        return 0, "done", ""

    monkeypatch.setattr(browser, "run", delayed_run)
    result = access.worker(actor1, "command", {"action": "command", "browser_key": "configured:" + parent_id,
        "turn_id": first_turn["turn"]["id"], "lease_id": first_turn["turn"]["lease_id"],
        "turn_token": "a" * 64, "action_id": "delayed-action", "command": ["screenshot", "screen.png"]})
    assert observed["state_during_command"] == "queued"
    assert result["action"]["state"] == "succeeded"
    assert result["artifacts"][0]["mime_type"] == "image/png"
    with database.read() as uow:
        waiting_row = uow.connection.execute("SELECT browser_turns.state,operational_assignments.wake_condition_json FROM browser_turns JOIN operational_assignments ON operational_assignments.id=browser_turns.assignment_id WHERE browser_turns.id=?", (waiting["turn"]["id"],)).fetchone()
    assert waiting_row[0] == "queued"
    assert json.loads(waiting_row[1])["kind"] == "browser_turn_available"


def test_uncertain_command_stays_blocked_past_fence_until_new_generation_reconciles(tmp_path):
    database, store, parent_id, business_id, start = setup_store(tmp_path)
    database.delegation = store
    first = _running(database, store, parent_id, business_id, "uncertain command")
    second = _running(database, store, parent_id, business_id, "after recovery")
    queue = BrowserTurnQueue(clock=store.clock, lease_seconds=30)
    key = "business:" + business_id
    late = start + timedelta(seconds=61)
    with database.transaction() as uow:
        first_turn = queue.acquire(uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=first["id"],
            assignment_id=first["id"], generation=first["execution_generation"])
        second_turn = queue.acquire(uow, browser_key=key, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=second["id"],
            assignment_id=second["id"], generation=second["execution_generation"])
        queue.wait(uow, second_turn["id"], parent_principal_id=parent_id, requester_id=second["id"],
            generation=second["execution_generation"], lease_id=None, checkpoint={"phase": "queued"},
            next_action="Resume after command reconciliation")
        intent, fresh = queue.begin_action(uow, first_turn["id"], parent_principal_id=parent_id,
            requester_id=first["id"], generation=first["execution_generation"], lease_id=first_turn["lease_id"],
            action_id="cross-fence-action", command=["click", "e1"])
        uncertain = queue.settle_action(uow, first_turn["id"], parent_principal_id=parent_id,
            requester_id=first["id"], generation=first["execution_generation"], lease_id=first_turn["lease_id"],
            action_id="cross-fence-action", state="uncertain", outcome={"timeout": True},
            error="command crossed the fence deadline", now=start + timedelta(seconds=1))
        queue.recover(uow, now=late)
        held = uow.connection.execute("SELECT state,command_id FROM browser_turns WHERE id=?",
                                      (first_turn["id"],)).fetchone()
        assert fresh and intent["state"] == "pending"
        assert uncertain["state"] == "uncertain"
        assert held["state"] == "owning" and held["command_id"] == "cross-fence-action"
        paused = store.control(uow, first["id"], parent_principal_id=parent_id,
            expected_revision=uow.repository("operational_assignments").get(first["id"])["revision"], action="pause")
        resumed = store.control(uow, first["id"], parent_principal_id=parent_id,
            expected_revision=paused["revision"], action="resume")
        current = store.claim(uow, first["id"], actor_principal_id=parent_id, dispatcher_ready=True)
        recovered = queue.reconcile_action(uow, first_turn["id"], parent_principal_id=parent_id,
            requester_id=first["id"], generation=current["execution_generation"], lease_id=current["lease_id"],
            action_id="cross-fence-action", outcome={"resolved": "stopped"}, now=late)
        waiting = uow.connection.execute("SELECT state FROM operational_assignments WHERE id=?",
                                         (second["id"],)).fetchone()[0]
        assert current["execution_generation"] > first["execution_generation"]
        assert recovered["state"] == "reconciled"
        assert waiting == "queued"


def test_delayed_worker_command_crossing_fence_remains_blocked_until_recovery(tmp_path, monkeypatch):
    database, store, parent_id, _business_id, start = setup_store(tmp_path)
    database.delegation = store
    current = [start]
    runtime = _configured_runtime(tmp_path, database, store, parent_id)
    runtime.browser_turns = BrowserTurnQueue(clock=lambda: current[0], lease_seconds=30)
    access = BusinessBrowserAccess(runtime)
    first = _configured_running(database, store, parent_id, "delayed fence command")
    second = _configured_running(database, store, parent_id, "protected waiter")
    actor1 = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"), registration_id=new_id(),
        delegation_binding_id=new_id(), delegation_assignment_id=first["id"],
        delegation_generation=first["execution_generation"])
    actor2 = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"), registration_id=new_id(),
        delegation_binding_id=new_id(), delegation_assignment_id=second["id"],
        delegation_generation=second["execution_generation"])
    key = "configured:" + parent_id
    owner = access.worker(actor1, "acquire", {"action": "acquire", "browser_key": key})
    waiting = access.worker(actor2, "acquire", {"action": "acquire", "browser_key": key})

    def delayed_run(_workspace, _command, *, target, instance, capture):
        current[0] = start + timedelta(seconds=61)
        return 0, "completed after fence", ""

    monkeypatch.setattr(browser, "run", delayed_run)
    result = access.worker(actor1, "command", {"action": "command", "browser_key": key,
        "turn_id": owner["turn"]["id"], "lease_id": owner["turn"]["lease_id"],
        "turn_token": "c" * 64, "action_id": "crossing-fence-live-command", "command": ["click", "e1"]})
    assert result["outcome"] == "uncertain"
    with database.transaction() as uow:
        runtime.browser_turns.recover(uow, now=current[0])
        held = uow.connection.execute("SELECT state,command_id FROM browser_turns WHERE id=?",
                                      (owner["turn"]["id"],)).fetchone()
        waiting_state = uow.connection.execute("SELECT state FROM browser_turns WHERE id=?",
                                                (waiting["turn"]["id"],)).fetchone()[0]
    assert held["state"] == "owning" and held["command_id"] == "crossing-fence-live-command"
    assert waiting_state == "queued"


def test_artifact_handle_delivers_png_and_paginates_text_within_turn(tmp_path, monkeypatch):
    database, store, parent_id, _business_id, _ = setup_store(tmp_path)
    database.delegation = store
    runtime = _configured_runtime(tmp_path, database, store, parent_id)
    access = BusinessBrowserAccess(runtime)
    assignment = _configured_running(database, store, parent_id, "artifact delivery")
    actor = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"), registration_id=new_id(),
        delegation_binding_id=new_id(), delegation_assignment_id=assignment["id"],
        delegation_generation=assignment["execution_generation"])
    turn = access.worker(actor, "acquire", {"action": "acquire", "browser_key": "configured:" + parent_id})

    def write_artifacts(workspace, _command, *, target, instance, capture):
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "screen.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (workspace / "notes.txt").write_text("0123456789")
        return 0, "", ""

    monkeypatch.setattr(browser, "run", write_artifacts)
    result = access.worker(actor, "command", {"action": "command", "browser_key": "configured:" + parent_id,
        "turn_id": turn["turn"]["id"], "lease_id": turn["turn"]["lease_id"],
        "turn_token": "b" * 64, "action_id": "artifact-action", "command": ["screenshot", "screen.png"]})
    by_type = {item["mime_type"]: item for item in result["artifacts"]}
    image = access.worker(actor, "artifact", {"action": "artifact", "browser_key": "configured:" + parent_id,
        "turn_id": turn["turn"]["id"], "lease_id": turn["turn"]["lease_id"],
        "artifact_handle": by_type["image/png"]["artifact_handle"]})
    text_page = access.worker(actor, "artifact", {"action": "artifact", "browser_key": "configured:" + parent_id,
        "turn_id": turn["turn"]["id"], "lease_id": turn["turn"]["lease_id"],
        "artifact_handle": by_type["text/plain"]["artifact_handle"], "offset": 2, "limit": 3})
    assert image["encoding"] == "base64" and image["content_base64"]
    assert text_page["content"] == "234" and text_page["next_offset"] == 5


def test_mcp_transport_delivers_browser_png_as_image_content(tmp_path):
    from mcp import types
    from mcp.shared.context import RequestContext
    from mcp.server.lowlevel.server import request_ctx
    from starlette.requests import Request

    class Application:
        @staticmethod
        def namespace(context, descriptor, arguments):
            return "global"

        @staticmethod
        def authorize(context, descriptor, targets, uow, **kwargs):
            return None

    class Access:
        @staticmethod
        def tool(actor, action, body):
            return {"artifact_handle": "browser-artifact:a:b:screen.png", "mime_type": "image/png",
                    "size": 8, "encoding": "base64", "content_base64": "iVBORw0KGgo=",
                    "_mcp_images": [{"data": "iVBORw0KGgo=", "mime_type": "image/png"}]}

    class Caller:
        def inventory(self, actor):
            return [{"canonical_operation": BROWSER_OPERATION, "exposed_aliases": [], "available": True}]

        def check_operation(self, name):
            assert name == BROWSER_OPERATION

        def validate_options(self, actor, name, options):
            return None

        def validate_arguments(self, actor, name, args):
            return None

        def invocation_context(self, actor, *, canonical_name, options):
            return InvocationContext(actor.principal, source="mcp")

    registry = builtin_registry()
    runtime = SimpleNamespace(database=None, registry=registry,
        application=Application(), browser_access=Access(), require_authority=lambda: None)
    # The handler still exercises the actual MCP registration and wire
    # projection; its domain authorization is intentionally minimal here.
    runtime.database = setup_store(tmp_path)[0]
    register_browser_operation(runtime)
    transport = MCPTransport(runtime, authentication=None, origins=[], caller=Caller())
    actor = SimpleNamespace(principal=Principal(new_id(), "agent"), credential_id=new_id(),
        registration_id=new_id(), delegation_assignment_id=new_id(), delegation_generation=1)
    request = Request({"type": "http", "method": "POST", "path": "/mcp", "headers": [],
                       "query_string": b"", "scheme": "http", "server": ("test", 80),
                       "client": ("test", 1), "root_path": "", "app": None})
    request.scope["bazarlyk.auth"] = actor
    context = RequestContext("image-test", None, None, None, request=request)
    message = types.CallToolRequest(params=types.CallToolRequestParams(name=BROWSER_OPERATION,
        arguments={"action": "artifact", "action_id": "image-action", "idempotency_key": "image-request"}))

    async def invoke():
        token = request_ctx.set(context)
        try:
            return await transport.server.request_handlers[types.CallToolRequest](message)
        finally:
            request_ctx.reset(token)

    result = anyio.run(invoke).root
    assert isinstance(result, types.CallToolResult)
    assert isinstance(result.content[0], types.TextContent)
    assert isinstance(result.content[1], types.ImageContent)
    assert result.content[1].mimeType == "image/png"
    assert "content_base64" not in result.content[0].text


def test_delayed_release_cannot_clear_next_lease_and_action_intent_is_idempotent(tmp_path):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    database.delegation = store
    running = _running(database, store, parent_id, business_id, "fenced")
    queue = BrowserTurnQueue(clock=store.clock)
    with database.transaction() as uow:
        turn = queue.acquire(uow, browser_key="business:" + business_id, business_id=business_id,
            parent_principal_id=parent_id, requester_kind="assignment", requester_id=running["id"],
            assignment_id=running["id"], generation=running["execution_generation"])
        intent, fresh = queue.begin_action(uow, turn["id"], parent_principal_id=parent_id,
            requester_id=running["id"], generation=running["execution_generation"], lease_id=turn["lease_id"],
            action_id="action-1", command=["click", "e1"])
        assert fresh and intent["state"] == "pending"
        settled = queue.settle_action(uow, turn["id"], parent_principal_id=parent_id,
            requester_id=running["id"], generation=running["execution_generation"], lease_id=turn["lease_id"],
            action_id="action-1", state="failed", outcome={"exit_code": 1}, error="browser rejected command")
        again, fresh_again = queue.begin_action(uow, turn["id"], parent_principal_id=parent_id,
            requester_id=running["id"], generation=running["execution_generation"], lease_id=turn["lease_id"],
            action_id="action-1", command=["click", "e1"])
        assert settled["state"] == "failed" and not fresh_again and again["state"] == "failed"
        queue.release(uow, turn["id"], parent_principal_id=parent_id, requester_id=running["id"],
            generation=running["execution_generation"], lease_id=turn["lease_id"])
        queue.release(uow, turn["id"], parent_principal_id=parent_id, requester_id=running["id"],
            generation=running["execution_generation"], lease_id="old-lease")
        assert uow.connection.execute("SELECT state FROM browser_turns WHERE id=?", (turn["id"],)).fetchone()[0] == "released"


def test_browser_turn_is_a_real_mcp_inventory_operation(tmp_path):
    registry = builtin_registry()
    register_browser_operation(type("Runtime", (), {"registry": registry})())
    descriptor = registry.get(BROWSER_OPERATION)
    assert descriptor.name == "browser_turn"
    assert "mcp" in descriptor.exposure
    assert "command" in descriptor.input_schema["properties"]
    assert "shell" not in json.dumps(plain(descriptor.input_schema))


def test_authenticated_mcp_bridge_reaches_worker_adapter_and_replays_action(tmp_path, monkeypatch):
    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    database.delegation = store
    assignment = _running(database, store, parent_id, business_id, "MCP browser task")
    calls = []

    class Access:
        def tool(self, actor, action, body):
            calls.append((actor.delegation_assignment_id, action, body["action_id"]))
            return {"status": "owning", "action": body["action"]}

    class Application:
        @staticmethod
        def namespace(context, descriptor, arguments):
            return "global"

        @staticmethod
        def authorize(context, descriptor, targets, uow, **kwargs):
            assert context.delegation_assignment_id == assignment["id"]

    class Caller:
        def check_operation(self, name):
            assert name == BROWSER_OPERATION

        def exposes(self, descriptor):
            return "mcp" in descriptor.exposure

        def validate_options(self, actor, name, options):
            return None

        def validate_arguments(self, actor, name, args):
            return None

        def invocation_context(self, actor, *, canonical_name, options):
            return InvocationContext(actor.principal, source="mcp",
                delegation_binding_id=new_id(),
                delegation_assignment_id=actor.delegation_assignment_id,
                delegation_generation=actor.delegation_generation)

    runtime = SimpleNamespace(database=database, registry=builtin_registry(), application=Application(),
        browser_access=Access(), require_authority=lambda: None)
    register_browser_operation(runtime)
    transport = MCPTransport(runtime, authentication=None, origins=[], caller=Caller())
    actor = SimpleNamespace(principal=Principal(parent_id, "agent"), credential_id=new_id(),
        registration_id=new_id(), delegation_assignment_id=assignment["id"],
        delegation_generation=assignment["execution_generation"])
    action_id = new_id()
    first = anyio.run(transport._single_result, BROWSER_OPERATION,
        {"action": "acquire", "action_id": action_id, "idempotency_key": "mcp-request"}, actor)
    second = anyio.run(transport._single_result, BROWSER_OPERATION,
        {"action": "acquire", "action_id": action_id, "idempotency_key": "mcp-request-2"}, actor)
    assert first["status"] == second["status"] == "ok", (first, second)
    assert len(calls) == 2
    assert calls[0][0] == assignment["id"] and calls[0][1] == "acquire"
