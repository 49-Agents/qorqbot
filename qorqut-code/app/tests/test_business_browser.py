from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from bazarlyk.browser import config, control, service
from qorqut_operations.contracts import DomainError, Principal
from foundation_support import BUSINESS, OTHER, AGENT, context, database_at
from test_domain_documents import setup_documents, invoke


def configure(db, root, business_id, **changes):
    with db.transaction() as uow:
        repo = uow.repository("businesses")
        row = repo.get(business_id, required=True)
        value = {**row["config_json"], "browser": {"enabled": True, "proxy_server": "http://proxy.example:8080", **changes}}
        config.prepare_config(value, row["config_json"], root=root, business_id=business_id,
                              principal_kind="human", uow=uow)
        repo.update(business_id, {"config_json": value}, expected_revision=row["revision"])
        return value["browser"]


def test_allocation_is_unique_and_identity_survives_rename(tmp_path, monkeypatch):
    db = database_at(tmp_path)
    monkeypatch.setattr(config, "port_available", lambda port: port != config.PORTS.start)
    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(lambda biz: configure(db, tmp_path, biz), (BUSINESS, OTHER)))
    assert first["cdp_port"] != second["cdp_port"]
    assert first["cdp_port"] != config.PORTS.start
    assert first["user_data_dir"] != second["user_data_dir"]
    assert first["downloads_dir"] != second["downloads_dir"]
    with db.transaction() as uow:
        uow.repository("businesses").update(BUSINESS, {"name": "Renamed"}, expected_revision=2)
    changed = configure(db, tmp_path, BUSINESS, proxy_server="https://new.example:443")
    assert changed["generation"] != first["generation"]
    assert {k: changed[k] for k in config.GENERATED - {"generation"}} == {k: first[k] for k in config.GENERATED - {"generation"}}
    assert configure(db, tmp_path, BUSINESS, proxy_server="https://new.example:443") == changed


@pytest.mark.parametrize("proxy", [None, "", "direct://", "http://host", "http://u:secret@host:80",
    "http://host:80/path", "http://host:80?password=secret", "http://host:80#x", "http://host:80\n", 4])
def test_bad_proxy_rejected(proxy):
    with pytest.raises(DomainError):
        config.validate_proxy(proxy)


def test_generic_config_edits_preserve_browser_and_reject_identity_changes(tmp_path):
    db, app, _ = setup_documents(tmp_path)
    initial = configure(db, tmp_path, BUSINESS)
    result = invoke(app, "business_config_update", {"config": {"name": "New name", "stage": "validating", "timezone": "UTC"}}, revision=2)
    assert result.status == "ok", result.to_dict()
    with db.read() as uow:
        row = uow.repository("businesses").get(BUSINESS)
        assert row["config_json"]["browser"] == initial
    for changes in ({"cdp_port": 9225}, {"user_data_dir": "/tmp/shared"}, {"password": "never-store"},
                    {"credentials_ref": "browser-proxy/" + OTHER}, {"generation": "invented"}):
        result = invoke(app, "business_config_update", {"config": {"browser": {**initial, **changes}}}, revision=3)
        assert result.status == "error", result.to_dict()
    with db.read() as uow, pytest.raises(DomainError, match="require the owner"):
        config.prepare_config({"browser": {"enabled": False}}, row["config_json"], root=tmp_path,
                              business_id=BUSINESS, principal_kind="agent", uow=uow)
    result = invoke(app, "business_config_update", {"config": {"browser": None}}, revision=3)
    assert result.status == "error"


def test_credentials_fixed_business_path_private_and_not_in_argv(tmp_path):
    db = database_at(tmp_path)
    target = configure(db, tmp_path, BUSINESS)
    with pytest.raises(DomainError):
        service.upstream_url(tmp_path, BUSINESS, target)
    path = tmp_path / ".bazarlyk/secrets/browser-proxy" / (BUSINESS + ".json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"username": "u@tenant", "password": "p:secret"}))
    path.chmod(0o644)
    with pytest.raises(DomainError):
        service.upstream_url(tmp_path, BUSINESS, target)
    path.chmod(0o600)
    assert service.upstream_url(tmp_path, BUSINESS, target) == "http://u%40tenant:p%3Asecret@proxy.example:8080"
    command = service.chrome_command("chrome", target, 12345)
    assert "--proxy-bypass-list=<-loopback>" in command
    assert "--no-sandbox" not in command
    assert not any("secret" in arg or "proxy.example" in arg for arg in command)
    with pytest.raises(DomainError):
        service.upstream_url(tmp_path, OTHER, target)


def test_ready_does_not_accept_arbitrary_or_stale_cdp(tmp_path):
    with pytest.raises(DomainError):
        control.ready_target(tmp_path, {"generation": "g"})
    (tmp_path / "ready.json").write_text('{"generation":"old"}')
    with pytest.raises(DomainError):
        control.ready_target(tmp_path, {"generation": "g"})


@pytest.mark.skipif(not os.environ.get("QORQUT_TEST_CHROME"), reason="explicit disposable Chrome test only")
def test_real_business_chromes_proxy_cookie_separation_and_restart(tmp_path, capfd):
    import base64
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import time
    from bazarlyk.bootstrap import browser

    db = database_at(tmp_path)
    runtime = Path(__file__).resolve().parents[1] / "tools/browser"
    executable = browser.runtime_executable(str(runtime))
    servers, threads, stops, futures = [], [], [], []
    traffic = []
    direct_hits = []
    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            direct_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"DIRECT")
        def log_message(self, *_):
            pass
    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
    origin_thread.start()
    servers.append(origin)
    threads.append(origin_thread)
    # A local origin avoids HTTPS-upgrade policy and also checks Chrome's
    # implicit loopback bypass really is removed. It must receive zero requests.
    site = f"http://127.0.0.1:{origin.server_port}"
    pool = ThreadPoolExecutor(2)
    states, instances = [], []
    try:
        for business_id, tenant in ((BUSINESS, "alpha"), (OTHER, "beta")):
            def handler(identity):
                class Proxy(BaseHTTPRequestHandler):
                    def do_GET(self):
                        traffic.append((identity, "GET", self.path))
                        expected = "Basic " + base64.b64encode((identity + ":secret").encode()).decode()
                        if self.headers.get("Proxy-Authorization") != expected:
                            self.send_response(407)
                            self.send_header("Proxy-Authenticate", 'Basic realm="fixture"')
                            self.end_headers()
                            return
                        assert self.headers.get("Authorization") is None
                        body = ("<html><body>" + identity + "</body></html>").encode()
                        self.send_response(200)
                        if self.path.endswith("/set"):
                            self.send_header("Set-Cookie", "tenant=" + identity + "; Path=/; Max-Age=3600")
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    def do_CONNECT(self):
                        traffic.append((identity, "CONNECT", self.path))
                        self.send_error(502)  # No external requests in this test.
                    def log_message(self, *_):
                        pass
                return Proxy
            proxy = ThreadingHTTPServer(("127.0.0.1", 0), handler(tenant))
            servers.append(proxy)
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
            conf = configure(db, tmp_path, business_id, proxy_server=f"http://127.0.0.1:{proxy.server_port}")
            credentials = tmp_path / ".bazarlyk/secrets/browser-proxy" / (business_id + ".json")
            credentials.parent.mkdir(parents=True, exist_ok=True)
            credentials.write_text(json.dumps({"username": tenant, "password": "secret"}))
            credentials.chmod(0o600)
            stop = threading.Event()
            stops.append(stop)
            futures.append(pool.submit(service.serve, db, tmp_path, business_id, runtime,
                                       os.environ["QORQUT_TEST_CHROME"], stop=stop))
            workspace = tmp_path / ("agent-" + tenant)
            workspace.mkdir()
            states.append((business_id, tenant, conf, workspace))

        def ready(business_id, conf, future):
            deadline = time.monotonic() + 40
            failure = None
            while time.monotonic() < deadline:
                if future.done():
                    future.result()
                    pytest.fail("Browser service exited before readiness")
                try:
                    return control.ready_target(tmp_path / ".bazarlyk/browsers" / business_id, conf)
                except DomainError as exc:
                    failure = repr(exc.__cause__)
                    time.sleep(0.1)
            pytest.fail("Managed test browser did not become ready: " + str(failure))

        for state, future in zip(states, futures):
            business_id, tenant, conf, workspace = state
            target = ready(business_id, conf, future)
            instances.append(target["instance"])
            def command(args):
                return browser.run(workspace, args, target=(target["cdp_endpoint"], executable), instance=target["instance"])
            assert command(["attach"]) == 0
            assert command(["goto", site + "/check"]) == 0, traffic[-10:]
            capfd.readouterr()
            assert command(["eval", "() => document.cookie", "--raw"]) == 0
            assert "tenant=" not in capfd.readouterr().out
            assert command(["goto", site + "/set"]) == 0
            assert command(["eval", "() => document.cookie", "--raw"]) == 0
            assert "tenant=" + tenant in capfd.readouterr().out
            assert command(["detach"]) == 0
            assert control.ready_target(tmp_path / ".bazarlyk/browsers" / business_id, conf) == target

        # Stop only alpha; beta remains healthy. Restart alpha with same profile.
        stops[0].set()
        futures[0].result(timeout=30)
        business_id, tenant, conf, workspace = states[0]
        stops[0] = threading.Event()
        futures[0] = pool.submit(service.serve, db, tmp_path, business_id, runtime,
                                 os.environ["QORQUT_TEST_CHROME"], stop=stops[0])
        restarted = ready(business_id, conf, futures[0])
        assert restarted["instance"] != instances[0]
        assert restarted["cdp_endpoint"] == f"http://127.0.0.1:{conf['cdp_port']}"
        args = {"target": (restarted["cdp_endpoint"], executable), "instance": restarted["instance"]}
        assert browser.run(workspace, ["attach"], **args) == 0
        assert browser.run(workspace, ["goto", site + "/check"], **args) == 0, traffic[-10:]
        assert browser.run(workspace, ["eval", "() => document.cookie", "--raw"], **args) == 0
        assert "tenant=alpha" in capfd.readouterr().out
        assert browser.run(workspace, ["detach"], **args) == 0
        assert not futures[1].done()
        # Killing alpha's actual upstream must not reach the direct local origin.
        servers[1].shutdown()
        servers[1].server_close()
        assert browser.run(workspace, ["attach"], **args) == 0
        probe = ("async page => { let response; try { response = await page.goto("
                 + json.dumps(site + "/upstream-down") + ", {timeout: 5000}); } "
                 'catch { return "expected-proxy-network-failure"; } '
                 'if (response.status() < 400) throw new Error("Unexpected direct success"); '
                 'return "expected-proxy-http-failure"; }')
        assert browser.run(workspace, ["run-code", probe], **args) == 0
        assert "expected-proxy-" in capfd.readouterr().out
        assert browser.run(workspace, ["detach"], **args) == 0
        assert not direct_hits
        configure(db, tmp_path, OTHER, enabled=False)
        futures[1].result(timeout=30)
    finally:
        for stop in stops:
            stop.set()
        pool.shutdown(wait=True)
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
