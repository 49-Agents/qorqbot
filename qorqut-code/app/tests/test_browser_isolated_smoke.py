"""Real isolated-desktop smoke: real Chrome, TigerVNC, systemd network namespace and the selected proxy.

Opt-in (``QORQUT_ISOLATED_SMOKE=1``) because it needs passwordless sudo, Chrome, Xtigervnc, openbox,
a built browser runtime (bundled Node 24, locked packages) and proxy credentials in a private file. It builds a temporary root with a
blank profile, an unused display and unused ports, and runs task-prefixed transient *system* units.
Assertion-only tests elsewhere do not stand in for this: these tests observe the kernel's answers.

    QORQUT_ISOLATED_SMOKE=1 \\
    QORQUT_ISOLATED_SMOKE_CREDENTIALS=/private/credentials.json      # {"username": ..., "password": ...}
    QORQUT_ISOLATED_SMOKE_RUNTIME=/path/to/built/tools/browser   # node/, node_modules/, relay.cjs, run-agent-browser
    [QORQUT_ISOLATED_SMOKE_PROXY=http://IP:PORT  QORQUT_ISOLATED_SMOKE_EXIT_IP=IP]
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import ProxyHandler, build_opener

import pytest

from bazarlyk.browser import config, control
from bazarlyk.browser.isolated import check, deployment as deploy_module
from qorqut_operations.contracts import DomainError
import isolated_smoke_support as support

pytestmark = pytest.mark.skipif(os.environ.get("QORQUT_ISOLATED_SMOKE") != "1", reason="real isolated-desktop smoke is opt-in")
PROXY = os.environ.get("QORQUT_ISOLATED_SMOKE_PROXY", "http://188.220.114.215:12323")
EXIT_IP = os.environ.get("QORQUT_ISOLATED_SMOKE_EXIT_IP", "188.220.114.215")
RUNTIME = os.environ.get("QORQUT_ISOLATED_SMOKE_RUNTIME", "/nonexistent-runtime")
GLOBAL_PATTERNS = ("Xtigervnc :20", "Xtigervnc :21", "Xtigervnc :22", "remote-debugging-port=9225",
                   "remote-debugging-port=9226", "remote-debugging-port=9230")


def global_processes():
    found = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if any(pattern in command for pattern in GLOBAL_PATTERNS) or command.startswith("tmux: server"):
            found[int(entry.name)] = stat[19]
    return found


def processes_matching(*needles):
    """Live processes whose command line holds any needle (shells and this test process excluded)."""
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            try:
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except (OSError, IndexError):
                continue
            if state == "Z" or command.startswith(("/bin/bash", "bash ", "sudo ", "timeout ")) or "pytest" in command:
                continue
            if any(needle in command for needle in needles):
                found.append((int(entry.name), command[:200]))
    return found


def components(base, *, gateway=True):
    """Every process a smoke fixture can leave behind: its Chrome, display, relay, and (optionally) gateway/session."""
    base = str(base)
    needles = ["--user-data-dir=" + base, "--database=" + base, "Xtigervnc :39", RUNTIME + "/relay.cjs",
               "isolated.session --deployment " + base]
    if gateway:
        needles.append("isolated.gateway --deployment " + base)
    return processes_matching(*needles)


class Cdp:
    """Minimal CDP client used through the host loopback bridge only."""

    def __init__(self, port):
        from websockets.sync.client import connect
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}/json/version", timeout=5) as response:
            url = json.load(response)["webSocketDebuggerUrl"]
        self.socket, self.next = connect(url, open_timeout=5, max_size=8 << 20), 0

    def call(self, method, params=None, session=None):
        self.next += 1
        message = {"id": self.next, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.socket.send(json.dumps(message))
        while True:
            reply = json.loads(self.socket.recv(timeout=30))
            if reply.get("id") == self.next:
                if "error" in reply:
                    raise RuntimeError(reply["error"].get("message", "cdp error"))
                return reply.get("result", {})

    def page_text(self, url, timeout=45):
        target = self.call("Target.createTarget", {"url": url})["targetId"]
        session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        deadline = time.monotonic() + timeout
        text = ""
        while time.monotonic() < deadline:
            value = self.call("Runtime.evaluate", {"expression": "document.body ? document.body.innerText : ''"}, session)
            text = value.get("result", {}).get("value", "") or ""
            if text.strip():
                break
            time.sleep(0.5)
        self.call("Target.closeTarget", {"targetId": target})
        return text.strip()

    def observed_ip(self, url="https://api.ipify.org/?format=json", attempts=4):
        """The address a page reports. The provider sometimes answers one of Chrome's two speculative CONNECTs
        with a 404, which surfaces as ERR_TUNNEL_CONNECTION_FAILED on a first load; retry only that error."""
        text = ""
        for _ in range(attempts):
            text = self.page_text(url)
            if "ERR_TUNNEL_CONNECTION_FAILED" not in text:
                break
            time.sleep(2)
        return text

    def close(self):
        self.socket.close()


PROBE = r"""
import errno, json, socket
def attempt(family, kind, address, send=False):
    try:
        with socket.socket(family, kind) as s:
            s.settimeout(2)
            s.connect(address)
            if send:
                s.send(b"x")
        return "connected"
    except OSError as e:
        return errno.errorcode.get(e.errno, type(e).__name__)
proxy = ("PROXY_HOST", PROXY_PORT)
out = {
  "interfaces": [l.split(":")[0].strip() for l in open("/proc/net/dev").read().splitlines()[2:]],
  "routes": [l for l in open("/proc/net/route").read().splitlines()[1:] if l.strip()],
  "http": attempt(socket.AF_INET, socket.SOCK_STREAM, ("1.1.1.1", 80)),
  "https": attempt(socket.AF_INET, socket.SOCK_STREAM, ("1.1.1.1", 443)),
  "proxy_direct": attempt(socket.AF_INET, socket.SOCK_STREAM, proxy),
  "dns_udp": attempt(socket.AF_INET, socket.SOCK_DGRAM, ("8.8.8.8", 53), True),
  "dns_tcp": attempt(socket.AF_INET, socket.SOCK_STREAM, ("8.8.8.8", 53)),
  "stun_udp": attempt(socket.AF_INET, socket.SOCK_DGRAM, ("74.125.250.129", 19302), True),
  "quic_udp": attempt(socket.AF_INET, socket.SOCK_DGRAM, ("1.1.1.1", 443), True),
  "ipv6_tcp": attempt(socket.AF_INET6, socket.SOCK_STREAM, ("2606:4700:4700::1111", 443)),
  "ipv6_udp": attempt(socket.AF_INET6, socket.SOCK_DGRAM, ("2606:4700:4700::1111", 443), True),
}
try:
    socket.getaddrinfo("example.com", 443)
    out["resolver"] = "resolved"
except OSError as e:
    out["resolver"] = "failed"
print(json.dumps(out))
"""


def probe_from(pid=None):
    host, _, port = PROXY.split("//")[1].partition(":")
    code = PROBE.replace('"PROXY_HOST"', repr(host)).replace("PROXY_PORT", port)
    argv = ["/usr/bin/python3", "-c", code]
    if pid is not None:
        argv = ["sudo", "-n", "nsenter", "-t", str(pid), "-n", *argv]
    return json.loads(subprocess.run(argv, capture_output=True, text=True, timeout=60, check=True).stdout)


@pytest.fixture
def smoke():
    credentials = os.environ.get("QORQUT_ISOLATED_SMOKE_CREDENTIALS")
    runtime = os.environ.get("QORQUT_ISOLATED_SMOKE_RUNTIME")
    if not credentials or not runtime:
        pytest.skip("QORQUT_ISOLATED_SMOKE_CREDENTIALS and QORQUT_ISOLATED_SMOKE_RUNTIME are required")
    assert (Path(runtime) / "node/bin/node").is_file() and (Path(runtime) / "run-agent-browser").is_file(), "the runtime is not built"
    base = Path(tempfile.mkdtemp(prefix="t49-smoke-base-"))
    base.chmod(0o700)
    fixture = support.Smoke(base, credentials, proxy=PROXY, exit_ip=EXIT_IP, runtime=runtime)
    before = global_processes()
    fixture.database = fixture.build()
    try:
        yield fixture
    finally:
        fixture.cleanup()
        assert global_processes() == before, "global browser processes or tmux changed during the smoke"
        assert not components(base), "orphan processes remain"


def bring_up(smoke, timeout=120):
    smoke.start_gateway()
    assert smoke.wait(lambda: (smoke.state_dir / "gateway.json").exists(), 30), smoke.journal(smoke.gateway_unit)
    smoke.start_session()
    assert smoke.wait(lambda: (smoke.state_dir / "ready.json").exists(), timeout), smoke.journal(smoke.session_unit)
    return json.loads((smoke.state_dir / "ready.json").read_text())


def assert_stopped_clean(smoke):
    for unit in (smoke.session_unit, smoke.gateway_unit):
        assert smoke.state(unit).get("ActiveState") in {"inactive", "failed"}, unit
    for name in ("ready.json", "session.json", "gateway.json"):
        assert not (smoke.state_dir / name).exists(), name
    assert not any(smoke.runtime_dir.glob("*.sock")) if smoke.runtime_dir.exists() else True
    assert not components(smoke.base), "orphan processes remain"


def saved_config(smoke):
    return smoke.saved(smoke.database)


def unit_exit(smoke, unit, timeout=90):
    assert smoke.wait(lambda: smoke.state(unit).get("ActiveState") in {"inactive", "failed"}, timeout), smoke.journal(unit)
    return int(smoke.state(unit)["ExecMainStatus"])


def expect_exit(smoke, unit, *codes):
    code = unit_exit(smoke, unit)
    assert code in codes, f"{unit} exited {code}, expected {codes}\n" + smoke.journal(unit, 15)


# --- the real enforced boundary -------------------------------------------------------------------------


def test_real_chrome_exits_through_the_proxy_and_the_namespace_has_no_other_path(smoke):
    receipt = bring_up(smoke)
    try:
        # The namespace: loopback only, no route, and no packet can reach any address directly.
        inside = probe_from(receipt["chrome_pid"])
        assert inside["interfaces"] == ["lo"] and inside["routes"] == []
        assert inside["resolver"] == "failed"
        for name in ("http", "https", "proxy_direct", "dns_udp", "dns_tcp", "stun_udp", "quic_udp", "ipv6_tcp", "ipv6_udp"):
            assert inside[name] in {"ENETUNREACH", "EAFNOSUPPORT", "EADDRNOTAVAIL"}, (name, inside[name])
        # Control: the host itself can reach the same proxy, so the refusals above are the namespace's doing.
        outside = probe_from()
        assert outside["proxy_direct"] == "connected" and "lo" in outside["interfaces"] and len(outside["interfaces"]) > 1
        # Chrome inside it is in that namespace and observes the assigned exit IP, not the host's.
        cdp = Cdp(saved_config(smoke)["cdp_port"])
        try:
            text = cdp.observed_ip()
        finally:
            cdp.close()
        assert text.startswith("{"), text + "\n--- gateway ---\n" + smoke.journal(smoke.gateway_unit, 10)
        observed = json.loads(text)["ip"]
        assert observed == EXIT_IP
        host_ip = subprocess.run(["curl", "-s", "--max-time", "15", "https://api.ipify.org"], capture_output=True, text=True).stdout.strip()
        assert host_ip and host_ip != observed
        # Host side: readiness receipt, CDP through the gateway, the VNC banner and the last exit check.
        with smoke.database.read() as uow:
            row = uow.repository("businesses").get(support.BUSINESS)
        report = check.verify(deploy_module.load(smoke.deployment_path), row)
        assert report["ready"] and report["vnc"]["banner"].startswith("RFB 003.") and report["exit_check"]["result"] == "ok"
        assert report["exit_check"]["observed_exit_ip"] == EXIT_IP
        assert not any(term in json.dumps(receipt) for term in ("password", "username"))
    finally:
        smoke.stop(smoke.session_unit)
        smoke.stop(smoke.gateway_unit)
    assert_stopped_clean(smoke)


def test_scoped_launcher_attaches_checks_and_detaches_through_the_bundled_runtime(smoke, tmp_path, monkeypatch):
    """The launcher's own command runner drives the disposable browser via the wrapper and its bundled Node."""
    from bazarlyk.bootstrap import browser as launcher
    receipt = bring_up(smoke)
    try:
        executable = launcher.runtime_executable(str(smoke.tools))
        assert executable == smoke.tools / "run-agent-browser"
        bundled = os.path.realpath(smoke.tools / "node/bin/node")
        # The relay of the running browser and the launcher both use the bundled Node, not the host's.
        assert os.path.realpath(f"/proc/{receipt['relay_pid']}/exe") == bundled
        decoy = tmp_path / "decoy"
        decoy.mkdir()
        (decoy / "node").write_text(f"#!/bin/sh\necho used >> {tmp_path}/decoy-used\nexit 9\n")
        (decoy / "node").chmod(0o755)
        monkeypatch.setenv("PATH", f"{decoy}:{os.environ['PATH']}")
        version = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=30)
        assert version.returncode == 0 and version.stdout.strip() == "agent-browser 0.38.1", version.stderr
        endpoint = f"http://127.0.0.1:{saved_config(smoke)['cdp_port']}"
        workspace = tmp_path / "launcher-workspace"
        workspace.mkdir()

        def run(*arguments):
            code, output, errors = launcher.run(workspace, list(arguments), target=(endpoint, executable),
                                                instance=receipt["instance"], capture=True)
            assert code == 0, (arguments, output, errors)
            return output
        run("attach")
        assert "about:blank" in run("tab-list")
        run("goto", "data:text/html,<title>bundled-node-24</title>")
        assert "bundled-node-24" in run("eval", "() => document.title")
        run("detach")
        assert not (tmp_path / "decoy-used").exists()
        # Detach never owns the browser: it is still served after the launcher session ended.
        Cdp(saved_config(smoke)["cdp_port"]).close()
    finally:
        smoke.stop(smoke.session_unit)
        smoke.stop(smoke.gateway_unit)


def test_session_refuses_a_routed_network_namespace_and_starts_nothing(smoke):
    smoke.start_gateway()
    assert smoke.wait(lambda: (smoke.state_dir / "gateway.json").exists(), 30)
    smoke.start_session(private_network=False)
    expect_exit(smoke, smoke.session_unit, 78)
    assert not (smoke.state_dir / "ready.json").exists()
    assert not components(smoke.base, gateway=False)
    assert "loopback-only network namespace" in smoke.journal(smoke.session_unit)


def test_session_requires_the_live_gateway(smoke):
    smoke.start_session(bind=False)  # no gateway unit exists at all
    expect_exit(smoke, smoke.session_unit, 1)
    assert not (smoke.state_dir / "ready.json").exists()
    assert "gateway" in smoke.journal(smoke.session_unit)
    assert not components(smoke.base)


# --- broken upstream: transport, authentication, wrong exit ---------------------------------------------


def test_broken_upstream_authentication_starts_no_browser_and_leaks_nothing(smoke):
    good = json.loads(Path(os.environ["QORQUT_ISOLATED_SMOKE_CREDENTIALS"]).read_text())
    path = smoke.root / ".bazarlyk/secrets/browser-proxy" / (support.BUSINESS + "-isolated.json")
    path.write_text(json.dumps({"username": good["username"], "password": "definitely-not-the-password"}))
    smoke.start_gateway()
    assert smoke.wait(lambda: (smoke.state_dir / "gateway.json").exists(), 30)
    smoke.start_session()
    expect_exit(smoke, smoke.session_unit, 1)
    assert not (smoke.state_dir / "ready.json").exists()
    assert not components(smoke.base, gateway=False)
    journal = smoke.journal(smoke.session_unit) + smoke.journal(smoke.gateway_unit)
    assert "definitely-not-the-password" not in journal and good["password"] not in journal and good["username"] not in journal
    last = json.loads((smoke.state_dir / "recheck.json").read_text())
    assert last["result"] == "failed" and last["observed_exit_ip"] is None


def test_a_different_exit_ip_is_fatal_and_needs_operator_repair(smoke):
    smoke.assign(smoke.database, expected_exit_ip="188.220.114.216", proxy_server=PROXY)
    smoke.start_gateway()
    assert smoke.wait(lambda: (smoke.state_dir / "gateway.json").exists(), 30)
    smoke.start_session()
    expect_exit(smoke, smoke.session_unit, 78)
    assert not (smoke.state_dir / "ready.json").exists()
    last = json.loads((smoke.state_dir / "recheck.json").read_text())
    assert last["result"] == "mismatch" and last["observed_exit_ip"] == EXIT_IP


def test_upstream_transport_break_withdraws_readiness_without_a_host_ip_fallback(smoke):
    receipt = bring_up(smoke)
    try:
        cdp = Cdp(saved_config(smoke)["cdp_port"])
        assert json.loads(cdp.observed_ip())["ip"] == EXIT_IP
        (smoke.runtime_dir / "upstream.sock").unlink()  # the only path to the proxy disappears
        assert smoke.wait(lambda: not (smoke.state_dir / "ready.json").exists(), 60), "readiness stayed usable after transport loss"
        with smoke.database.read() as uow:
            row = uow.repository("businesses").get(support.BUSINESS)
        with pytest.raises(DomainError):
            control.ready_target(smoke.state_dir, config.configured_target(row, smoke.root))
        result = check.recheck(deploy_module.load(smoke.deployment_path), timeout=60)
        assert result["result"] == "failed"
        # Chrome keeps running and may reuse a tunnel it already holds, but a NEW connection has nowhere to go:
        # a host it never contacted fails, and nothing ever reports the host's own address.
        text = cdp.page_text("https://icanhazip.com/", timeout=40)
        cdp.close()
        host_ip = subprocess.run(["curl", "-s", "--max-time", "15", "https://api.ipify.org"], capture_output=True, text=True).stdout.strip()
        assert "ERR_" in text and host_ip not in text and EXIT_IP not in text, text
        assert smoke.state(smoke.session_unit)["ActiveState"] == "active"
    finally:
        smoke.stop(smoke.session_unit)
        smoke.stop(smoke.gateway_unit)
    assert_stopped_clean(smoke)


# --- collisions and refusals: nothing is adopted, killed or deleted -------------------------------------


def test_collisions_are_refused_without_touching_the_occupant(smoke):
    cdp_port = saved_config(smoke)["cdp_port"]
    occupant = subprocess.Popen([sys.executable, "-c",
        "import socket,time,sys\nkeep=[]\nfor p in (%d,%d):\n s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',p));s.listen(1);keep.append(s)\nprint('up',flush=True);time.sleep(120)" % (cdp_port, smoke.vnc_port)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert occupant.stdout.readline().strip() == "up"
        smoke.start_gateway()
        expect_exit(smoke, smoke.gateway_unit, 75)
        assert occupant.poll() is None and not (smoke.state_dir / "gateway.json").exists()
    finally:
        occupant.kill()
        occupant.wait()
    lock = Path("/tmp/.X39-lock")
    sleeper = subprocess.Popen(["sleep", "120"])
    try:
        lock.write_text(f"{sleeper.pid:>10}\n")
        smoke.stop(smoke.gateway_unit)
        smoke.start_gateway()
        assert smoke.wait(lambda: (smoke.state_dir / "gateway.json").exists(), 30)
        smoke.start_session()
        expect_exit(smoke, smoke.session_unit, 75)
        assert lock.read_text().strip() == str(sleeper.pid) and sleeper.poll() is None
        assert not (smoke.state_dir / "ready.json").exists()
    finally:
        lock.unlink(missing_ok=True)
        sleeper.kill()
        sleeper.wait()


def test_second_gateway_and_second_session_are_refused_while_the_first_runs(smoke):
    receipt = bring_up(smoke)
    smoke.start_gateway(unit="t49-smoke-gateway-2", runtime_name="t49-smoke/second-gateway")  # its own systemd runtime dir
    expect_exit(smoke, "t49-smoke-gateway-2", 75, 1)
    smoke.start_session(unit="t49-smoke-session-2")
    expect_exit(smoke, "t49-smoke-session-2", 75, 1)
    assert json.loads((smoke.state_dir / "ready.json").read_text())["instance"] == receipt["instance"]
    with smoke.database.read() as uow:
        row = uow.repository("businesses").get(support.BUSINESS)
    assert control.ready_target(smoke.state_dir, config.configured_target(row, smoke.root))["cdp_endpoint"]
    assert smoke.state(smoke.session_unit)["ActiveState"] == "active" and smoke.state(smoke.gateway_unit)["ActiveState"] == "active"


# --- lifecycle ------------------------------------------------------------------------------------------


def test_graceful_restart_keeps_the_profile_and_leaves_no_orphans(smoke):
    bring_up(smoke)
    port = saved_config(smoke)["cdp_port"]
    expires = time.time() + 86400
    cdp = Cdp(port)
    try:
        cdp.call("Storage.setCookies", {"cookies": [{"name": "t49", "value": "persisted", "domain": "example.invalid",
                                                    "path": "/", "expires": expires}]})
    finally:
        cdp.close()
    smoke.stop(smoke.session_unit)
    assert not (smoke.state_dir / "ready.json").exists() and not (smoke.runtime_dir / "cdp.sock").exists()
    assert not components(smoke.base, gateway=False)
    profile = Path(saved_config(smoke)["user_data_dir"])
    assert (profile / "Default").is_dir() and (smoke.state_dir.parent.parent.parent / "smoke-business/hq/browser/.gitignore").read_text() == "*\n"
    smoke.start_session()
    assert smoke.wait(lambda: (smoke.state_dir / "ready.json").exists(), 120), smoke.journal(smoke.session_unit)
    cdp = Cdp(port)
    try:
        cookies = cdp.call("Storage.getCookies")["cookies"]
    finally:
        cdp.close()
    assert any(item["name"] == "t49" and item["value"] == "persisted" for item in cookies)
    smoke.stop(smoke.session_unit)
    smoke.stop(smoke.gateway_unit)
    assert_stopped_clean(smoke)
    assert profile.is_dir()  # shutdown never deletes the persistent profile


def test_unexpected_child_death_and_gateway_death_stop_the_browser(smoke):
    receipt = bring_up(smoke)
    os.kill(receipt["chrome_pid"], 9)
    expect_exit(smoke, smoke.session_unit, 1)
    assert not (smoke.state_dir / "ready.json").exists()
    assert not components(smoke.base, gateway=False)
    smoke.stop(smoke.session_unit)  # clear the failed unit that still references the gateway
    smoke.stop(smoke.gateway_unit)
    receipt = bring_up(smoke)
    support.sudo("systemctl", "kill", "--kill-whom=main", "--signal=SIGKILL", smoke.gateway_unit + ".service")
    expect_exit(smoke, smoke.session_unit, 0, 1, 143)
    assert not (smoke.state_dir / "ready.json").exists()
    assert smoke.wait(lambda: not components(smoke.base, gateway=False), 30)


def test_config_change_stops_both_units_and_stale_receipts_are_rejected(smoke):
    receipt = bring_up(smoke)
    with smoke.database.read() as uow:
        row = uow.repository("businesses").get(support.BUSINESS)
    saved = config.configured_target(row, smoke.root)
    # Tampered copies of the live receipts are rejected against the real processes and namespaces.
    copy = smoke.base / "state-copy"
    for tamper, label in ((lambda s, g: s.update(chrome_identity="0:1"), "chrome identity"),
                          (lambda s, g: g.update(pid=os.getpid()), "gateway pid"),
                          (lambda s, g: s.update(netns=g["netns"]), "same namespace"),
                          (lambda s, g: (s.update(gateway={**s["gateway"], "instance": "other"})), "gateway instance")):
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(smoke.state_dir, copy, ignore=shutil.ignore_patterns("*.lock", "xdg", "*.sock"))
        status, gateway = json.loads((copy / "ready.json").read_text()), json.loads((copy / "gateway.json").read_text())
        tamper(status, gateway)
        (copy / "ready.json").write_text(json.dumps(status))
        (copy / "gateway.json").write_text(json.dumps(gateway))
        with pytest.raises(DomainError):
            control.ready_target(copy, saved)
        assert label
    assert control.ready_target(smoke.state_dir, saved)["generation"] == saved["generation"]
    smoke.assign(smoke.database, enabled=False)
    expect_exit(smoke, smoke.session_unit, 0, 1)
    expect_exit(smoke, smoke.gateway_unit, 0)
    assert_stopped_clean(smoke)
