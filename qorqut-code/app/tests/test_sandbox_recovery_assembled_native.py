"""Opt-in assembled recovery acceptance with real service writer controllers."""
import hashlib
import json
import os
from pathlib import Path
import stat
import secrets
import socket
import subprocess
import time
import shutil
from contextlib import contextmanager

import httpx
import pytest

from qorqut_operations.contracts import new_id
from bazarlyk.sandbox.config import BusinessPaths
from qorqut_operations.storage.admission import InstallationRecoveryLock
from bazarlyk.sandbox.recovery_admin import RecoveryAdminConfig, client_request
from bazarlyk.service.main import assemble, load_terminal_broker
from bazarlyk.storage.database import Database
from test_sandbox_network_runtime import assigned_proxy
from test_sandbox_quiescence_native import _capture_pause_samples
from test_sandbox_service_native import native_agent_browser_cli, synthetic_form_interaction
from test_service_transports import ServiceFixture
from test_agent_configuration import REPO, PATHS

RESTIC = os.environ.get("RESTIC_TEST_BINARY")
WORKSTATION = os.environ.get("QORQUT_TEST_WORKSTATION_IMAGE", "qorqut-business-sandbox-workstation@sha256:8cb359f1de8240b517fc8c01da76a214473dd0041c147c5ff6e05321d4d89f12")
BROWSER = os.environ.get("QORQUT_TEST_BROWSER_IMAGE", "qorqut-business-sandbox-browser@sha256:0a2f2c97bd4df60f80723a916e28523ecd7982eb5a28050931b9884444988f70")
pytestmark = pytest.mark.skipif(not (RESTIC and os.environ.get("QORQUT_NATIVE_ASSEMBLED_RECOVERY") == "1"),
    reason="set RESTIC_TEST_BINARY and QORQUT_NATIVE_ASSEMBLED_RECOVERY=1")


def _config(tmp_path, root):
    private = tmp_path / "recovery-private"; private.mkdir(mode=0o700)
    password = private / "password"; password.write_text(secrets.token_urlsafe(32) + "\n"); password.chmod(0o600)
    binary = Path(RESTIC)
    value = {"schema_version": 1, "businesses_root": str(root), "socket_path": str(private / "recovery.sock"),
        "repository": str(private / "repository"), "password_file": str(password), "authority_staging": str(private / "authority"),
        "restic": {"executable": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "command_timeout_seconds": 120},
        "retention": {"daily": 2, "weekly": 2}, "owner_uid": os.getuid(), "initialize_repository": True}
    path = private / "recovery.json"; path.write_text(json.dumps(value)); path.chmod(0o600)
    return path, RecoveryAdminConfig.load(path, businesses_root=root)


def _isolated_broker(tmp_path):
    """Map only the exact read-only legacy inventory argv to a unique native socket."""
    socket_name = "assembled-recovery-" + new_id()
    wrapper = tmp_path / "tmux-inventory"
    wrapper.write_text("#!/bin/sh\n[ \"$#\" -eq 7 ] && [ \"$1\" = -L ] && [ \"$2\" = bazarlyk ] && [ \"$3\" = -f ] && [ \"$4\" = /dev/null ] && [ \"$5\" = list-sessions ] && [ \"$6\" = -F ] && [ \"$7\" = '#{session_id}\t#{session_name}' ] || exit 64\nexec /usr/bin/tmux -L '" + socket_name + "' -f /dev/null list-sessions -F \"$7\"\n")
    wrapper.chmod(0o700)
    broker = load_terminal_broker(PATHS)
    broker.TMUX = str(wrapper)
    return broker


def _free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _host_config(root, business_id, proxy, preview_port, viewer_port):
    (root / ".bazarlyk/secrets/browser-proxy").mkdir(parents=True, exist_ok=True, mode=0o700)
    credential = root / ".bazarlyk/secrets/browser-proxy" / (business_id + ".json")
    credential.write_text(json.dumps({"username": proxy.username, "password": proxy.password})); credential.chmod(0o600)
    browser = root / ".bazarlyk/browsers" / business_id
    host = {"schema_version": 1, "workstation_image": WORKSTATION, "browser_image": BROWSER,
        "limits": {"agent_memory_mb": 768, "browser_memory_mb": 512, "gateway_memory_mb": 256, "cpus": 1.5, "pids": 512, "disk_gb": 8},
        "agent_api_port": 1148, "enabled": True}
    (root / ".bazarlyk/sandbox-host.json").write_text(json.dumps(host)); (root / ".bazarlyk/sandbox-host.json").chmod(0o600)
    preview = {"schema_version": 1, "bind_address": "127.0.0.1", "owner_addresses": ["127.0.0.1"],
               "first_port": preview_port, "last_port": preview_port, "viewer_port": viewer_port}
    (root / ".bazarlyk/preview-host.json").write_text(json.dumps(preview)); (root / ".bazarlyk/preview-host.json").chmod(0o600)
    return {"browser": {"enabled": True, "proxy_server": f"{proxy.protocol}://{proxy.server_name}:{proxy.port}",
        "cdp_port": 19400, "user_data_dir": str(browser / "profile"), "downloads_dir": str(browser / "downloads"),
        "credentials_ref": "browser-proxy/" + business_id, "generation": business_id}}


def _git_state(root):
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(root)], check=True)
    for target in (root,):
        subprocess.run(["git", "-C", str(target), "config", "user.email", "fixture@example.test"], check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "Fixture"], check=True)
    (root / "ROOT.md").write_text("root metadata\n")
    subprocess.run(["git", "-C", str(root), "add", "ROOT.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "root"], check=True)
    hq, seed = root / "widget/hq", root.parent / "hq-seed"
    hq.rename(seed)
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-qb", "hq-work", str(hq)], check=True)
    shutil.copytree(seed, hq, dirs_exist_ok=True)
    subprocess.run(["git", "-C", str(hq), "add", "."], check=True)
    subprocess.run(["git", "-C", str(hq), "commit", "-qm", "hq"], check=True)
    (hq / "recovery-dirty.txt").write_text("dirty hq\n")
    code = root / "widget/code/repository/main"; code.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(code)], check=True)
    subprocess.run(["git", "-C", str(code), "config", "user.email", "fixture@example.test"], check=True)
    subprocess.run(["git", "-C", str(code), "config", "user.name", "Fixture"], check=True)
    (code / "tracked.txt").write_text("tracked\n")
    subprocess.run(["git", "-C", str(code), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(code), "commit", "-qm", "code"], check=True)
    dirty = root / "widget/code/repository/worktrees/dirty"
    subprocess.run(["git", "-C", str(code), "worktree", "add", "-qb", "dirty-work", str(dirty)], check=True)
    (dirty / "dirty.txt").write_text("dirty code\n")


def _threads(runtime):
    return (runtime.background._thread, runtime.signoffs._thread, runtime.email_history._thread,
            runtime.sandbox.worker._thread, runtime.previews._thread,
            runtime.previews.browser_viewers._thread, runtime.recovery_admin._thread)


def _assert_stopped(runtime, ownership, config):
    assert all(thread is None or not thread.is_alive() for thread in _threads(runtime))
    assert not runtime.previews._servers
    assert runtime.previews.browser_viewers._server is None
    assert not runtime.sandbox.listeners._servers
    assert not config.socket_path.exists()
    assert runtime.sandbox.driver._inventory("container", ownership) == []
    assert runtime.sandbox.driver._inventory("network", ownership) == []


def _stop_controllers(runtime):
    """Require the real controllers to drain, including any bounded stop retry."""
    try:
        runtime.stop()
    finally:
        failures = []
        for controller in (runtime.recovery_admin, runtime.previews, runtime.sandbox,
                           runtime.background, runtime.signoffs, runtime.email_history):
            try:
                controller.stop(timeout=60)
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup("Assembled fixture controllers did not drain", failures)
    assert all(thread is None or not thread.is_alive() for thread in _threads(runtime))


def _stop_and_remove(runtime, ownership, business, paths):
    # Never remove containers beneath an undrained controller that could still
    # recreate a writer. Stop retries above preserve a failed inventory.
    _stop_controllers(runtime)
    try:
        runtime.sandbox.driver._drain(ownership, runtime.sandbox.driver._inventory("container", ownership),
                                      business=business, paths=paths)
    finally:
        # A failed orderly Chrome close can leave containers alive. Preserve
        # their containment and fixture credentials for exact operator cleanup.
        if any(row.get("State", {}).get("Running") or row.get("State", {}).get("Paused")
               for row in runtime.sandbox.driver._inventory("container", ownership)):
            raise RuntimeError("Native fixture containers remain; preserving network containment")
        for row in runtime.sandbox.driver._inventory("network", ownership):
            runtime.sandbox.network.remove_bridge(row["Id"])
            runtime.sandbox.docker.call("network", "rm", row["Id"], timeout=180)


def test_assembled_runtime_uds_capture_and_original_path_restore(tmp_path):
    fixture = ServiceFixture(tmp_path)
    root, database, business_id = fixture.fixture.root, fixture.fixture.db, fixture.fixture.business_id
    _git_state(root)
    proxy = assigned_proxy()  # Explicit operator-selected compatibility check; values never enter output.
    preview_port = _free_port()
    viewer_port = _free_port()
    while viewer_port == preview_port:
        viewer_port = _free_port()
    browser_config = _host_config(root, business_id, proxy, preview_port, viewer_port)
    with database.transaction() as uow:
        row = uow.repository("businesses").get(business_id, required=True)
        uow.repository("businesses").update(business_id, {"config_json": browser_config}, expected_revision=row["revision"])
    config_path, config = _config(tmp_path, root)
    app = assemble(root, REPO, paths=PATHS, origins=["http://127.0.0.1:1049"], endpoint="http://127.0.0.1:1049/mcp",
                   database=database, broker=_isolated_broker(tmp_path), recovery_config=config_path,
                   model_client_factory=lambda _business: (_ for _ in ()).throw(AssertionError("native recovery must not invoke a model")), nightly_maintenance=None)
    runtime = app.state.runtime
    with database.read() as uow:
        business = dict(uow.repository("businesses").get(business_id, required=True))
    paths = BusinessPaths.registered(root, business)
    owner = runtime.sandbox.driver.ownership(paths)
    retired = tmp_path / "retired-businesses"
    credential_relative = Path(".bazarlyk/secrets/browser-proxy") / (business_id + ".json")
    credential_hash = hashlib.sha256((root / credential_relative).read_bytes()).hexdigest()
    stopped = False
    try:
        # Explicit adoption of this disposable existing business uses the real
        # assembled host-terminal/browser inventory before committing intent.
        assert runtime.sandbox.administration.status(business_id) is None
        startup_deadline = time.monotonic() + 180
        result = runtime.sandbox.administration.ensure(business_id)
        attempts = 1
        containers = {row["Id"] for row in runtime.sandbox.driver._inventory("container", owner)}
        while result["state"] != "ready" and time.monotonic() < startup_deadline:
            # Exercise the production worker's durable retry contract under a
            # cold/loaded host. Conflicts/config errors never become a retry;
            # neither timeouts nor the existing generation may be relaxed.
            assert result["state"] == "failed"
            assert result["error_code"] in {"environment_unavailable", "environment_unhealthy"}
            assert len(containers) == 3
            results = runtime.sandbox.worker.run_once()
            attempts += len(results)
            result = runtime.sandbox.administration.status(business_id)
            assert {row["Id"] for row in runtime.sandbox.driver._inventory("container", owner)} == containers
            assert result["desired_generation"] == 1
            if result["state"] != "ready":
                time.sleep(.5)
        print("NATIVE_STARTUP_ATTEMPTS", attempts)
        assert result["state"] == "ready"
        assert time.monotonic() < startup_deadline
        runtime.previews.registry.register(business_id, 18123)
        runtime.start()
        runtime.previews.reconcile()
        viewers = runtime.previews.browser_viewers
        viewers.reconcile()
        assert all(thread is not None and thread.is_alive() for thread in _threads(runtime))
        assert runtime.previews._servers and all(server.running for _, _, server in runtime.previews._servers.values())
        assert viewers._server.running
        viewer_url = viewers.link(business_id)
        assert viewer_url == f"http://127.0.0.1:{viewer_port}/{business_id}/"
        with httpx.Client(trust_env=False, timeout=30, headers={"Origin": f"http://127.0.0.1:{viewer_port}"}) as client:
            assert client.get(viewer_url).status_code == 200
            assert client.post(viewer_url + "api/acquire").json() == {"acquired": True}
            synthetic_form_interaction(runtime.sandbox, owner, business_id)
            assert client.post(viewer_url + "api/release").json() == {"released": True}
        native_agent_browser_cli(runtime.sandbox.docker, result["environment_id"], owner,
                                 paths.workspace / "hq/agents/growth")
        home_canary = paths.home / ".recovery-home-canary"
        profile_canary = paths.browser / "profile/.recovery-profile-canary"
        home_canary.write_text("persistent home\n")
        profile_canary.write_text("persistent profile file\n")
        (paths.workspace / "code/repository/main/capture-payload.bin").write_bytes(os.urandom(1024 * 1024))
        with database.read() as uow:
            registrations = {row[0] for row in uow.connection.execute("SELECT id FROM machine_registrations")}
        assert set(fixture.registrations.values()) <= registrations
        canary = paths.workspace / "code/raw-recovery-canary"
        runtime.sandbox.docker.call("exec", "--detach", result["environment_id"], "/bin/sh", "-c", 'while :; do date +%s%N >> "$1"; sleep .02; done', "sh", str(canary), timeout=60)
        for _ in range(100):
            if canary.exists() and canary.stat().st_size: break
            time.sleep(.02)
        assert canary.exists() and canary.stat().st_size
        generation = new_id()
        created, frozen = _capture_pause_samples(runtime.sandbox.docker, result["environment_id"], owner, canary,
            lambda: client_request(config, {"action": "create", "generation_id": generation}, timeout=180))
        assert created["ok"] is True
        verified = client_request(config, {"action": "verify", "generation_id": generation}, timeout=180)
        assert verified["ok"] is True
        assert all(thread is not None and thread.is_alive() for thread in _threads(runtime))
        runtime.previews.reconcile()
        viewers.reconcile()
        assert runtime.previews._servers and all(server.running for _, _, server in runtime.previews._servers.values())
        assert viewers._server.running and viewers.link(business_id) == viewer_url
        before = canary.stat().st_size
        for _ in range(100):
            if canary.stat().st_size > before:
                break
            time.sleep(.02)
        assert canary.stat().st_size > before
        with database.read() as uow:
            assert uow.repository("maintenance_state").get("service") is None
        store = runtime.recovery_admin._store
        assert store is not None
        _stop_controllers(runtime)
        admin = runtime.sandbox.administration
        current = admin.status(business_id)
        admin.request(business_id, "stop", expected_revision=current["revision"])
        stopped_row = runtime.sandbox.lifecycle.ensure_business_environment(business_id)
        assert stopped_row["state"] == stopped_row["desired_state"] == "stopped"
        admin.rollback_to_host(business_id, expected_revision=stopped_row["revision"])
        assert admin.status(business_id) is None
        assert home_canary.read_text() == "persistent home\n"
        assert profile_canary.read_text() == "persistent profile file\n"
        _assert_stopped(runtime, owner, config)
        stopped = True
        root.rename(retired)

        @contextmanager
        def offline_quiescence():
            # This is an offline proof about the actual assembled controllers,
            # not a synthetic zero-inventory controller. Keep the retired lock
            # held without recreating the original path before restore checks it.
            with InstallationRecoveryLock(retired).exclusive(timeout=10):
                _assert_stopped(runtime, owner, config)
                yield
                _assert_stopped(runtime, owner, config)

        restored = store.restore_original_root(generation, quiescence=offline_quiescence())
        assert restored["state"] == "closed" and restored["destination"] == str(root)
        restored_database = Database(root / ".bazarlyk/state.sqlite3").open_existing()
        with restored_database.read() as uow:
            assert uow.repository("maintenance_state").get("service")["state"] == "closed"
            assert uow.repository("businesses").get(business_id)["hq_path"] == str(paths.workspace / "hq")
            assert {row[0] for row in uow.connection.execute("SELECT id FROM machine_registrations")} == registrations
            profiles = uow.repository("agent_profiles", business_id=business_id)
            assert profiles.get(fixture.agent.id) is not None
            assert profiles.get(fixture.manager.id) is not None
        assert json.loads((root / ".bazarlyk/authority.json").read_text())["state"] == "maintenance"
        assert canary.read_bytes() == frozen
        assert home_canary.read_text() == "persistent home\n"
        assert profile_canary.read_text() == "persistent profile file\n"
        assert hashlib.sha256((root / credential_relative).read_bytes()).hexdigest() == credential_hash
        assert stat.S_IMODE((root / credential_relative).stat().st_mode) == 0o600
        assert json.loads((root / ".bazarlyk/preview-host.json").read_text())["viewer_port"] == viewer_port
        listed = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"],
                                capture_output=True, text=True, check=True).stdout
        assert "worktree " + str(paths.workspace / "hq") in listed and "branch refs/heads/hq-work" in listed
        for checkout, dirty_file, contents in (
            (paths.workspace / "hq", "recovery-dirty.txt", "dirty hq\n"),
            (paths.workspace / "code/repository/worktrees/dirty", "dirty.txt", "dirty code\n"),
        ):
            assert (checkout / dirty_file).read_text() == contents
            status = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain"],
                                    capture_output=True, text=True, check=True).stdout
            assert "?? " + dirty_file + "\n" in status
    finally:
        if not stopped:
            _stop_and_remove(runtime, owner, business, paths)
        _assert_stopped(runtime, owner, config)
        # Preserve all private fixture state if shutdown/absence proof fails.
        # An operator can then reconcile the exact retained environment.
        for selected_root in (root, retired):
            control = selected_root / paths.control.relative_to(root)
            for gateway in control.glob("generation-*/gateway.json"):
                gateway.unlink()
            (selected_root / credential_relative).unlink(missing_ok=True)
        shutil.rmtree(config.password_file.parent)
