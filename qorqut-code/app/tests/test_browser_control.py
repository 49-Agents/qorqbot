from contextlib import contextmanager
import json
from pathlib import Path
import os
import subprocess
import sys
import sysconfig
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import time
import venv

import pytest

from bazarlyk.bootstrap import browser
from bazarlyk.bootstrap.preparation import build_projection
import test_agent_configuration
from instance_support import PATHS
from test_agent_configuration import Fixture

ROLE_SOURCE = PATHS.component("roles")

OBSERVATION_SNAPSHOT = '''- main "Settings" [ref=e2]:
  - heading "Settings" [level=1] [ref=e3]
  - form "Profile" [ref=e4]:
    - generic [ref=e5]:
      - text: Name
      - textbox "Name" [ref=e6]: Alice
    - generic [ref=e7]:
      - text: Opt
      - checkbox "Opt" [checked] [ref=e8]
    - combobox "Plan" [ref=e9]:
      - option "Pro" [selected]
    - button "Save" [ref=e10]
    - paragraph [ref=e11]: "Status: ready"
  - table "Prospects" [ref=e12]:
    - rowgroup [ref=e13]:
      - row [ref=e14]:
        - columnheader "Name" [ref=e15]
        - columnheader "Status" [ref=e16]
      - row [ref=e17]:
        - cell "Bob" [ref=e18]
        - cell "Pending" [ref=e19]
'''


def _observation_output(capfd):
    output = capfd.readouterr().out.strip().splitlines()
    assert len(output) == 1
    return json.loads(output[0]), output[0]


def test_unresponsive_title_is_bounded_and_recovers():
    import shutil
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser runtime tests")
    script = r"""
const assert = require('node:assert/strict');
const init = require(process.argv[1]).default;
(async () => {
  let calls = 0, finish;
  const page = { title: () => { calls++; return new Promise(resolve => { finish = resolve; }); } };
  await init({page});
  const started = Date.now();
  const results = await Promise.all([page.title(), page.title()]);
  assert(results.every(value => value.includes('unresponsive')));
  assert(Date.now() - started < 3000);
  assert.equal(calls, 1); // Repeated calls do not leak additional stuck requests.
  finish('recovered');
  await new Promise(resolve => setImmediate(resolve));
  const next = page.title();
  assert.equal(calls, 2);
  finish('healthy title');
  assert.equal(await next, 'healthy title');
  const failing = {title: async () => { throw new Error('closed'); }};
  await init({page: failing});
  assert.equal(await failing.title(), '[title unavailable]');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-e", script, str(Path(browser.__file__).with_name("browser_init.cjs"))],
        check=True, timeout=8, capture_output=True, text=True)


@pytest.fixture
def workspace(tmp_path):
    runtime = tmp_path / "runtime"
    package = runtime / "node_modules/agent-browser/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"name": "agent-browser", "version": browser.CLI_VERSION}))
    executable = runtime / "node_modules/.bin/agent-browser"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    agent = tmp_path / "agent"
    (agent / ".bazarlyk").mkdir(parents=True)
    (agent / ".bazarlyk/browser.json").write_text(json.dumps({
        "schema_version": 1, "cdp_endpoint": "http://127.0.0.1:19225", "runtime_dir": str(runtime)}))
    return agent


@pytest.mark.parametrize("args", [[], ["open"], ["close"], ["kill-all"], ["delete-data"],
    ["attach", "other"], ["attach", "--cdp=http://127.0.0.1:1234"],
    ["snapshot", "--config", "other.json"], ["tab-list", "-s=other"],
    ["tab-list", "--session", "other"], ["detach", "--extension"]])
def test_target_overrides_and_global_actions_rejected(args):
    with pytest.raises(browser.BrowserError):
        browser.validate_arguments(args)


@pytest.mark.parametrize("endpoint", ["https://example.com", "http://localhost:9225",
    "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:bad",
    "http://secret@127.0.0.1:9225", "http://127.0.0.1:9225/path", "ws://127.0.0.1:9225"])
def test_bad_target_never_launches(workspace, endpoint, monkeypatch):
    path = workspace / ".bazarlyk/browser.json"
    target = json.loads(path.read_text())
    target["cdp_endpoint"] = endpoint
    path.write_text(json.dumps(target))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not launch"))
    assert browser.main(str(workspace), ["attach"]) == 2


def test_missing_target_and_wrong_version_fail_closed(workspace):
    path = workspace / ".bazarlyk/browser.json"
    target = json.loads(path.read_text())
    package = Path(target["runtime_dir"]) / "node_modules/agent-browser/package.json"
    package.write_text('{"name":"agent-browser","version":"999.0.0"}')
    assert browser.main(str(workspace), ["attach"]) == 2
    path.unlink()
    assert browser.main(str(workspace), ["attach"]) == 2


def test_session_binding_environment_and_bounded_failure(workspace, monkeypatch):
    calls = []
    monkeypatch.setattr(browser, "preflight", lambda endpoint: None)
    monkeypatch.setenv("PLAYWRIGHT_CLI_SESSION", "someone-else")
    monkeypatch.setenv("PLAYWRIGHT_MCP_CDP_ENDPOINT", "http://127.0.0.1:9999")
    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(subprocess, "run", execute)
    assert browser.run(workspace, ["attach"]) == 7
    assert browser.run(workspace, ["fill", "e1", "literal $(do-not-run)"]) == 7
    assert calls[0][0][4] == calls[1][0][4]
    assert calls[1][0][-1] == "literal $(do-not-run)"
    assert not any(k.startswith("PLAYWRIGHT_") for k in calls[0][1]["env"])
    assert calls[0][1]["timeout"] == 45
    config = json.loads(Path(calls[0][0][2]).read_text())
    assert config == {}
    path = workspace / ".bazarlyk/browser.json"
    target = json.loads(path.read_text())
    target["cdp_endpoint"] = "http://127.0.0.1:19226"
    path.write_text(json.dumps(target))
    browser.run(workspace, ["attach"])
    assert calls[2][0][4] != calls[0][0][4]
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 45)
    monkeypatch.setattr(subprocess, "run", timeout)
    assert browser.main(str(workspace), ["click", "e1"]) == 2


def test_targeted_observation_is_bounded_and_paginates_from_full_evidence(workspace, monkeypatch, capfd):
    calls = []
    def observe(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "data": {"snapshot": OBSERVATION_SNAPSHOT}}), stderr="")
    monkeypatch.setattr(subprocess, "run", observe)

    assert browser.run(workspace, ["observe", "overview", "--budget=1000"]) == 0
    first, encoded = _observation_output(capfd)
    assert len(encoded) <= 1000
    assert first["status"] == "ok"
    assert first["budget"]["maxChars"] == 1000
    assert first["budget"]["chars"] == len(encoded)
    assert first["omitted"] > 0 and first["next"] is not None
    assert {item["ref"] for item in first["items"] if "ref" in item} & {"e6", "e8", "e10"}
    evidence = workspace / first["evidence"]
    assert evidence.read_text() == OBSERVATION_SNAPSHOT
    assert evidence.stat().st_mode & 0o777 == 0o600

    assert browser.run(workspace, ["observe", "overview", "--budget=1000",
                                  "--observation=" + first["observation"],
                                  "--offset=" + str(first["next"])]) == 0
    second, second_encoded = _observation_output(capfd)
    assert len(second_encoded) <= 1000
    assert second["observation"] == first["observation"]
    assert second["offset"] == first["next"]
    assert len(calls) == 1  # Pagination only reads the immutable artifact.


def test_targeted_observation_modes_preserve_refs_and_values(workspace, monkeypatch, capfd):
    calls = []
    def observe(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "data": {"snapshot": OBSERVATION_SNAPSHOT}}), stderr="")
    monkeypatch.setattr(subprocess, "run", observe)

    assert browser.run(workspace, ["observe", "overview"]) == 0
    overview, _ = _observation_output(capfd)
    handle = overview["observation"]

    def reuse(mode, *extra):
        assert browser.run(workspace, ["observe", mode, *extra, "--observation=" + handle]) == 0
        return _observation_output(capfd)[0]

    found = reuse("find", "Name")
    assert {item.get("ref") for item in found["items"]} >= {"e6", "e15"}
    region = reuse("region", "--region=Profile")
    assert {item.get("ref") for item in region["items"]} >= {"e4", "e6", "e10"}
    values = reuse("values")
    value_by_ref = {item["ref"]: item for item in values["items"] if "ref" in item}
    assert value_by_ref["e6"]["value"] == "Alice"
    assert value_by_ref["e8"]["checked"] is True
    assert value_by_ref["e9"]["value"] == "Pro"
    table = reuse("table", "Prospects")
    assert {item.get("ref") for item in table["items"]} >= {"e12", "e15", "e18"}
    assert len(calls) == 1


def test_observation_failure_keeps_complete_error_artifact(workspace, monkeypatch, capfd):
    marker_out, marker_err = "stdout-marker-" + "x" * 1800, "stderr-marker-" + "y" * 1800
    def fail(*_args, **_kwargs):
        return SimpleNamespace(returncode=17, stdout=marker_out, stderr=marker_err)
    monkeypatch.setattr(subprocess, "run", fail)
    assert browser.run(workspace, ["observe", "overview", "--budget=1000"]) == 17
    result, encoded = _observation_output(capfd)
    assert len(encoded) <= 1000
    assert result["status"] == "failed"
    assert result["exit_code"] == 17
    assert result["error_omitted"] is True
    artifact = workspace / result["error_artifact"]
    details = artifact.read_text()
    assert marker_out in details and marker_err in details
    assert artifact.stat().st_mode & 0o777 == 0o600


def test_observation_handle_does_not_follow_symlink(workspace, monkeypatch, capfd):
    def observe(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "data": {"snapshot": OBSERVATION_SNAPSHOT}}), stderr="")
    monkeypatch.setattr(subprocess, "run", observe)
    assert browser.run(workspace, ["observe", "overview"]) == 0
    result, _ = _observation_output(capfd)
    evidence = workspace / result["evidence"]
    canary = evidence.with_name("canary.txt")
    canary.write_text("keep")
    evidence.unlink()
    evidence.symlink_to(canary)
    assert browser.run(workspace, ["observe", "overview", "--observation=" + result["observation"]]) == 2
    failure, encoded = _observation_output(capfd)
    assert len(encoded) <= 4000
    assert failure["status"] == "failed"
    assert canary.read_text() == "keep"


def test_observation_reports_oversized_first_item_without_stuck_next(workspace, monkeypatch, capfd):
    long_snapshot = '- main "' + "x" * 5000 + '" [ref=e1]:\n'
    def observe(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "data": {"snapshot": long_snapshot}}), stderr="")
    monkeypatch.setattr(subprocess, "run", observe)

    assert browser.run(workspace, ["observe", "overview", "--budget=1000"]) == 0
    result, encoded = _observation_output(capfd)
    assert len(encoded) <= 1000
    assert result["status"] == "ok"
    assert result["omitted"] == 1
    assert "next" not in result
    assert result["pagination_stalled"] is True
    assert result["oversized_item"]["ref"] == "e1"
    assert "larger --budget" in result["oversized_item"]["recovery"]
    assert long_snapshot in (workspace / result["evidence"]).read_text()


def test_observation_rejects_unrecognized_nonempty_evidence(workspace, monkeypatch, capfd):
    def observe(command, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"success": True, "data": {"snapshot": "not an accessibility snapshot\n"}}), stderr="")
    monkeypatch.setattr(subprocess, "run", observe)

    assert browser.run(workspace, ["observe", "overview"]) == 2
    result, encoded = _observation_output(capfd)
    assert len(encoded) <= 4000
    assert result["status"] == "failed"
    assert "recognized accessibility snapshot" in result["message"]
    assert "not an accessibility snapshot" in (workspace / result["evidence"]).read_text()


@contextmanager
def cdp_metadata(pages):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/json/list"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(pages).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_empty_initial_tab_is_reported_without_navigation(monkeypatch):
    with cdp_metadata([{"type": "page", "url": ""}]) as endpoint:
        with pytest.raises(browser.BrowserError, match="uninitialized"):
            browser.preflight(endpoint)
    with cdp_metadata([{"type": "page", "url": "about:blank"}]) as endpoint:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        browser.preflight(endpoint)


def test_selected_browser_roles_get_lazy_skill_and_launcher(tmp_path, monkeypatch):
    # Use this task's role source even when the rest of the fixture's packages
    # come from the shared repository map.
    load_sources = test_agent_configuration.load_sources
    def sources(root, **options):
        result = {name: value for name, value in load_sources(root, **options).items()
                  if Path(value[1]).is_dir()}
        for manifest in (ROLE_SOURCE / "roles").glob("*/role.yaml"):
            result[manifest.parent.name] = ("role", ROLE_SOURCE, manifest.relative_to(ROLE_SOURCE).as_posix())
        return result
    monkeypatch.setattr(test_agent_configuration, "load_sources", sources)
    fixture = Fixture(tmp_path)
    # Exercise the prepared shell command with an interpreter outside the source
    # tree, as in production. A .pth supplies the equivalent of an editable install.
    runtime = tmp_path / "external-python"
    venv.EnvBuilder(with_pip=False).create(runtime)
    site = runtime / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    (site / "qorqut-test.pth").write_text(str(Path(__file__).resolve().parents[1]) + "\n"
        + sysconfig.get_path("purelib") + "\n")
    for role in ("ordubashy", "founder", "business-agent", "global-marketing", "learning-applier"):
        scope = {"scope": {"kind": "ecosystem", "allowed_business_ids": [],
                            "global_capabilities": sorted(fixture.ctx.global_capabilities)}} if role in {"founder", "learning-applier"} else {}
        agent = fixture.create() if role == "business-agent" else fixture.create("browser-" + role, role, **scope)
        config = fixture.resolver.resolve_agent_config(agent)
        projection = build_projection(fixture.resolver, config, registration_id="preview",
            endpoint="http://127.0.0.1:9988", interpreter=str(runtime / "bin/python"))
        selected = role in {"ordubashy", "business-agent"}
        assert (".bazarlyk/browser" in projection.files) == selected
        for directory in (".claude/skills", ".agents/skills"):
            assert (f"{directory}/{browser.SKILL_NAME}/SKILL.md" in projection.files) == selected
        servers = {"bazarlyk", "outreach"} if role == "global-marketing" else {"bazarlyk"}
        assert set(json.loads(projection.files[".mcp.json"])["mcpServers"]) == servers
        for name in ("CLAUDE.md", "AGENTS.md", ".codex/config.toml"):
            assert b"Playwright" not in projection.files[name]
        assert "Playwright" not in config.to_dict()["mandatory_context"]["rendered"]
        if selected:
            launcher = tmp_path / "prepared-browser"
            launcher.write_bytes(projection.files[".bazarlyk/browser"])
            help_result = subprocess.run(["sh", str(launcher), "--help"], cwd=tmp_path,
                capture_output=True, text=True, check=True)
            assert "Workspace Chrome control" in help_result.stdout
            ready = fixture.prepare(agent)
            if role == "business-agent":
                continue  # Its role is fixed.
            workspace = Path(ready["workspace"])
            target = workspace / ".bazarlyk/browser.json"
            target.write_text('{"operator":"preserve me"}')
            fixture.update(agent, {"role": fixture.refs["founder"]})
            fixture.prepare(agent)
            assert not (workspace / ".bazarlyk/browser").exists()
            assert target.read_text() == '{"operator":"preserve me"}'


def test_prepared_owner_launcher_keeps_receipt_outside_launcher_file(tmp_path, monkeypatch):
    import shlex
    import uuid
    workspace = tmp_path / "prepared-owner"
    (workspace / ".bazarlyk").mkdir(parents=True)
    runtime = tmp_path / "browser-runtime"
    executable = runtime / "node_modules/.bin/agent-browser"
    executable.parent.mkdir(parents=True)
    (runtime / "node_modules/agent-browser").mkdir(parents=True)
    (runtime / "node_modules/agent-browser/package.json").write_text(
        json.dumps({"name": "agent-browser", "version": browser.CLI_VERSION}))
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    (workspace / ".bazarlyk/browser.json").write_text(json.dumps({
        "schema_version": 1, "cdp_endpoint": "http://127.0.0.1:45678",
        "runtime_dir": str(runtime)}))

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            if body["phase"] == "running":
                result = {"browser_turn": {"turn": {"id": "turn-prepared", "lease_id": "lease-prepared"},
                    "target": {"cdp_endpoint": "http://127.0.0.1:45678", "runtime_dir": str(runtime),
                                "instance": "configured:prepared"}}}
            else:
                result = {}
            payload = json.dumps({"status": "ok", "data": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        python_runtime = tmp_path / "prepared-python"
        venv.EnvBuilder(with_pip=False).create(python_runtime)
        site = python_runtime / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        (site / "qorqut-test.pth").write_text("\n".join(path for path in sys.path if path) + "\n")
        registration = {"schema_version": 1, "registration_id": str(uuid.uuid4()),
            "agent_id": str(uuid.uuid4()), "workspace": str(workspace),
            "service_url": f"http://127.0.0.1:{server.server_port}",
            "credential_file": ".bazarlyk/credential", "capture_transcripts": False,
            "durable_delegation_enabled": False}
        registration_path = workspace / ".bazarlyk/registration.json"
        registration_path.write_text(json.dumps(registration))
        credential = workspace / ".bazarlyk/credential"
        credential.write_text("prepared-launcher-test-credential-0123456789")
        credential.chmod(0o600)
        script = ("from bazarlyk.bootstrap.browser import main; raise SystemExit(main("
                  + repr(str(workspace)) + ", business=False))")
        launcher = workspace / ".bazarlyk/browser"
        launcher.write_text("#!/bin/sh\nexec " + shlex.join([str(python_runtime / "bin/python"), "-I", "-c", script])
            + ' "$@"\n')
        launcher.chmod(0o700)
        consumer = "12345678-1234-4234-8234-123456789abc"
        result = subprocess.run(["sh", str(workspace / ".bazarlyk/browser"), "--consumer-id", consumer,
                                 "click", "e1"], cwd=workspace, capture_output=True, text=True, check=True)
        assert result.returncode == 0
        subprocess.run(["sh", str(workspace / ".bazarlyk/browser"), "--consumer-id", consumer,
                        "click", "e2"], cwd=workspace, capture_output=True, text=True, check=True)
        subprocess.run(["sh", str(workspace / ".bazarlyk/browser"), "--consumer-id", consumer,
                        "detach"], cwd=workspace, capture_output=True, text=True, check=True)
        receipt = workspace / ".bazarlyk/browser-owner" / (consumer + ".json")
        assert json.loads(receipt.read_text()) == {}
        assert (workspace / ".bazarlyk/browser").is_file()
        assert [call["phase"] for call in calls] == ["running", "active", "active", "active", "finished"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.skipif(not os.environ.get("QORQUT_TEST_CHROME"), reason="explicit isolated Chrome test only")
def test_real_cli_on_disposable_chrome(tmp_path, capfd):
    profile = tmp_path / "chrome"
    profile.mkdir()
    process = subprocess.Popen([os.environ["QORQUT_TEST_CHROME"], "--headless=new", "--no-sandbox",
        "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
        "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    workspace = tmp_path / "agent"
    (workspace / ".bazarlyk").mkdir(parents=True)
    attached = False
    try:
        port_file = profile / "DevToolsActivePort"
        for _ in range(100):
            if port_file.exists():
                break
            assert process.poll() is None
            time.sleep(0.1)
        port = int(port_file.read_text().splitlines()[0])
        endpoint = f"http://127.0.0.1:{port}"
        (workspace / ".bazarlyk/browser.json").write_text(json.dumps({"schema_version": 1,
            "cdp_endpoint": endpoint, "runtime_dir": str(Path(__file__).resolve().parents[1] / "tools/browser")}))
        assert browser.run(workspace, ["attach"]) == 0
        attached = True
        assert browser.run(workspace, ["tab-new", "data:text/html,<button onclick='this.textContent=42'>Click me</button>"]) == 0
        assert browser.run(workspace, ["snapshot"]) == 0
        assert browser.run(workspace, ["click", "button"]) == 0
        assert browser.run(workspace, ["eval", "() => document.querySelector('button').textContent"]) == 0
        output = capfd.readouterr().out
        assert "42" in output
        assert browser.run(workspace, ["eval", "() => document.querySelector('button').textContent"]) == 0
        assert capfd.readouterr().out.strip().strip('"') == "42"
        assert browser.run(workspace, ["tab-list"]) == 0
        assert "about:blank" in capfd.readouterr().out
        assert browser.run(workspace, ["detach"]) == 0
        attached = False
        assert process.poll() is None  # Detach must preserve the browser.
        browser.preflight(endpoint)
    finally:
        if attached:
            browser.main(str(workspace), ["detach"])
        process.terminate()  # Only the disposable browser started by this test.
        process.wait(timeout=10)
