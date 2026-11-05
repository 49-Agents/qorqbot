"""New live adapter coverage: MCP-shaped worker calls, Chrome, wait, resume."""

import json
import shutil
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from bazarlyk.browser.access import BusinessBrowserAccess
from bazarlyk.browser.turns import BrowserTurnQueue
from qorqut_operations.contracts import Principal
from bazarlyk.transport.auth import AuthenticatedPrincipal
from test_browser_turn_integration import _FormHandler, _port, _assignment
from test_operational_delegation_store import setup_store


@pytest.mark.integration
def test_live_worker_acquires_chrome_waits_releases_and_dispatches_resume(tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    runtime_dir = Path(__file__).resolve().parents[1] / "tools/browser"
    executable = runtime_dir / "node_modules/.bin/agent-browser"
    if chrome is None or not executable.is_file():
        pytest.skip("disposable Chrome and the locked agent-browser runtime are required")

    database, store, parent_id, business_id, _ = setup_store(tmp_path)
    database.delegation = store
    first = _assignment(database, store, parent_id, business_id, "live first")
    second = _assignment(database, store, parent_id, business_id, "live waiter")
    browser_port, page_port = _port(), _port()
    page_server = ThreadingHTTPServer(("127.0.0.1", page_port), _FormHandler)
    chrome_profile = tmp_path / "chrome-profile"
    chrome_process = subprocess.Popen([
        chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={browser_port}",
        f"--user-data-dir={chrome_profile}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=page_server.serve_forever, daemon=True).start()
    try:
        endpoint = f"http://127.0.0.1:{browser_port}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                import urllib.request
                with urllib.request.urlopen(endpoint + "/json/version", timeout=0.5) as response:
                    if json.loads(response.read()).get("Browser"):
                        break
            except (OSError, ValueError):
                time.sleep(0.1)
        else:
            pytest.fail("disposable Chrome did not expose CDP")

        runtime = type("Runtime", (), {"root": tmp_path, "workspace": runtime_dir.parent,
            "database": database, "browser_turns": BrowserTurnQueue(clock=store.clock)})()
        access = BusinessBrowserAccess(runtime)
        access._worker_target = lambda _key, _token, _parent: {
            "cdp_endpoint": endpoint, "runtime_dir": str(runtime_dir), "instance": "disposable-chrome"}
        actor1 = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"), registration_id="r1",
            delegation_binding_id="b1", delegation_assignment_id=first["id"],
            delegation_generation=first["execution_generation"])
        actor2 = AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"), registration_id="r2",
            delegation_binding_id="b2", delegation_assignment_id=second["id"],
            delegation_generation=second["execution_generation"])
        key = "business:" + business_id
        first_turn = access.worker(actor1, "acquire", {"action": "acquire", "browser_key": key})
        waiting = access.worker(actor2, "acquire", {"action": "acquire", "browser_key": key})
        assert first_turn["status"] == "owning" and waiting["status"] == "waiting"

        common = {"action": "command", "browser_key": key, "turn_id": first_turn["turn"]["id"],
            "lease_id": first_turn["turn"]["lease_id"], "turn_token": first_turn["turn_token"],
            "action_id": "live-goto"}
        worked = access.worker(actor1, "command", {**common,
            "command": ["goto", f"http://127.0.0.1:{page_port}/"]})
        assert worked["exit_code"] == 0 and worked["action"]["state"] == "succeeded"
        access.worker(actor1, "release", {"action": "release", "browser_key": key,
            "turn_id": first_turn["turn"]["id"], "lease_id": first_turn["turn"]["lease_id"],
            "turn_token": first_turn["turn_token"]})

        # Release promotion wakes the durable assignment. The dispatcher claims
        # it and creates a new generation; the resumed worker then acquires the
        # same queued row without model/Astra polling.
        with database.transaction() as uow:
            resumed = store.claim(uow, second["id"], actor_principal_id=parent_id, dispatcher_ready=True)
        actor2 = AuthenticatedPrincipal(actor2.principal, registration_id="r2", delegation_binding_id="b2",
            delegation_assignment_id=second["id"], delegation_generation=resumed["execution_generation"])
        second_turn = access.worker(actor2, "acquire", {"action": "acquire", "browser_key": key})
        assert second_turn["status"] == "owning" and second_turn["turn"]["id"] == waiting["turn"]["id"]
        resumed_work = access.worker(actor2, "command", {"action": "command", "browser_key": key,
            "turn_id": second_turn["turn"]["id"], "lease_id": second_turn["turn"]["lease_id"],
            "turn_token": second_turn["turn_token"], "action_id": "live-resume",
            "command": ["eval", "() => document.title"]})
        assert resumed_work["exit_code"] == 0
        access.worker(actor2, "release", {"action": "release", "browser_key": key,
            "turn_id": second_turn["turn"]["id"], "lease_id": second_turn["turn"]["lease_id"],
            "turn_token": second_turn["turn_token"]})
    finally:
        page_server.shutdown()
        page_server.server_close()
        chrome_process.terminate()
        try:
            chrome_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome_process.kill()
            chrome_process.wait(timeout=5)
