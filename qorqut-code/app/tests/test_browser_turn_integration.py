"""New end-to-end browser-turn coverage with disposable Chrome."""

import json
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bazarlyk.browser.access import BusinessBrowserAccess
from bazarlyk.browser.turns import BrowserTurnQueue
from qorqut_operations.contracts import Principal
from bazarlyk.transport.auth import AuthenticatedPrincipal
from test_operational_delegation_store import setup_store


class _FormHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b"<!doctype html><form><input id='name'><button id='save' type='button'>Save</button>"
                b"<output id='result'></output></form><script>"
                b"save.onclick=()=>{result.textContent=name.value;document.body.dataset.saved='yes'}"
                b"</script>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _assignment(database, store, parent_id, business_id, title):
    with database.transaction() as uow:
        saved = store.create(uow, parent_principal_id=parent_id, business_id=business_id,
            scope={"kind": "business", "business_id": business_id, "record_ids": {}, "source_ids": {}},
            objective=title, plan={"steps": ["open", "save"]}, allowed_operations=["browser_use"],
            resource_bounds={"max_turns": 6, "max_runtime_seconds": 300})
        return store.claim(uow, saved["id"], actor_principal_id=parent_id, dispatcher_ready=True)


def _actor(parent_id, assignment):
    return AuthenticatedPrincipal(Principal(parent_id, "agent", "Astra"),
        registration_id="registration", delegation_binding_id="binding",
        delegation_assignment_id=assignment["id"], delegation_generation=assignment["execution_generation"])


@pytest.mark.integration
def test_durable_workers_use_chrome_turn_then_promote_waiter(tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    runtime_dir = Path(__file__).resolve().parents[1] / "tools/browser"
    executable = runtime_dir / "node_modules/.bin/agent-browser"
    if chrome is None or not executable.is_file():
        pytest.skip("disposable Chrome and the locked agent-browser runtime are required")

    database, store, parent_id, business_id, _now = setup_store(tmp_path)
    first = _assignment(database, store, parent_id, business_id, "Fill the first form")
    second = _assignment(database, store, parent_id, business_id, "Fill the waiting form")
    browser_port, page_port = _port(), _port()
    page_server = ThreadingHTTPServer(("127.0.0.1", page_port), _FormHandler)
    page_server.timeout = 0.2
    chrome_profile = tmp_path / "chrome-profile"
    chrome_process = subprocess.Popen([
        chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={browser_port}",
        f"--user-data-dir={chrome_profile}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page_server_thread = threading.Thread(target=page_server.serve_forever, daemon=True)
    page_server_thread.start()
    try:
        deadline = time.monotonic() + 15
        endpoint = f"http://127.0.0.1:{browser_port}"
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
        access._worker_target = lambda _business_id, _token, _parent_id: {
            "cdp_endpoint": endpoint, "runtime_dir": str(runtime_dir), "instance": "disposable-chrome"}
        first_actor, second_actor = _actor(parent_id, first), _actor(parent_id, second)

        first_turn = access.worker(first_actor, "acquire", {"action": "acquire", "business_id": business_id})
        waiting = access.worker(second_actor, "acquire", {"action": "acquire", "business_id": business_id})
        assert first_turn["status"] == "owning"
        assert waiting["status"] == "waiting"

        common = {"action": "command", "business_id": business_id,
            "turn_id": first_turn["turn"]["id"], "lease_id": first_turn["turn"]["lease_id"],
            "turn_token": first_turn["turn_token"]}
        command = access.worker(first_actor, "command", {**common,
            "command": ["goto", f"http://127.0.0.1:{page_port}/"],
            "checkpoint": {"tab": "form", "completed": ["open"]},
            "next_action": "fill and save the form"})
        assert command["exit_code"] == 0
        filled = access.worker(first_actor, "command", {**common,
            "command": ["fill", "#name", "Luna"], "checkpoint": {"completed": ["fill"]}})
        assert filled["exit_code"] == 0
        saved = access.worker(first_actor, "command", {**common,
            "command": ["click", "#save"], "checkpoint": {"completed": ["save"]}})
        assert saved["exit_code"] == 0
        verified = access.worker(first_actor, "command", {**common,
            "command": ["eval", "() => document.body.dataset.saved"],
            "checkpoint": {"completed": ["verify"]}})
        assert verified["exit_code"] == 0 and "yes" in verified["stdout"]
        released = access.worker(first_actor, "release", {
            "action": "release", "business_id": business_id, "turn_id": first_turn["turn"]["id"],
            "lease_id": first_turn["turn"]["lease_id"], "turn_token": first_turn["turn_token"]})
        assert released["turn"]["state"] == "released"

        second_turn = access.worker(second_actor, "acquire", {"action": "acquire", "business_id": business_id})
        assert second_turn["status"] == "owning"
        assert second_turn["turn"]["id"] == waiting["turn"]["id"]
        status = access.worker(second_actor, "status", {
            "action": "status", "business_id": business_id, "turn_id": second_turn["turn"]["id"]})
        assert status["turn"]["state"] == "owning"
        resumed_work = access.worker(second_actor, "command", {
            "action": "command", "business_id": business_id, "turn_id": second_turn["turn"]["id"],
            "lease_id": second_turn["turn"]["lease_id"], "turn_token": second_turn["turn_token"],
            "command": ["goto", f"http://127.0.0.1:{page_port}/"],
            "checkpoint": {"completed": ["resume-and-open"]}})
        assert resumed_work["exit_code"] == 0
        access.worker(second_actor, "release", {
            "action": "release", "business_id": business_id, "turn_id": second_turn["turn"]["id"],
            "lease_id": second_turn["turn"]["lease_id"], "turn_token": second_turn["turn_token"]})
    finally:
        page_server.shutdown()
        page_server.server_close()
        chrome_process.terminate()
        try:
            chrome_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            chrome_process.kill()
            chrome_process.wait(timeout=5)
