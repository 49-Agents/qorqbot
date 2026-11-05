"""Serialized native proof; opt in only after Docker/Chrome fixture queues are clear."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import stat
import threading
import time

import pytest

from qorqut_operations.contracts import new_id
from bazarlyk.sandbox.config import BusinessPaths
from bazarlyk.sandbox.docker import Docker
from bazarlyk.sandbox.quiescence import DockerFreezer, DockerSandboxWriters
from qorqut_operations.storage.admission import InstallationRecoveryLock, RecoveryAdmission
from qorqut_operations.storage.quiescence import KnownZeroWriters, QuiescenceProof, RecoveryQuiescer
from bazarlyk.sandbox.recovery import ResticBinary
from bazarlyk.sandbox.recovery_admin import (RecoveryAdminConfig,
    RecoveryAdministration, client_request)
from bazarlyk.sandbox.runtime import DockerEnvironmentDriver
from bazarlyk.sandbox.state import SandboxStateStore
from bazarlyk.service.runtime import Runtime
from bazarlyk.storage import Database
from test_sandbox_recovery import root_git_linked_hq_fixture

RESTIC = os.environ.get("RESTIC_TEST_BINARY")


@pytest.fixture
def restic_binary():
    if not RESTIC:
        pytest.skip("RESTIC_TEST_BINARY is required")
    return ResticBinary(RESTIC, hashlib.sha256(Path(RESTIC).read_bytes()).hexdigest())


pytestmark = pytest.mark.skipif(not (RESTIC and os.environ.get("QORQUT_NATIVE_RECOVERY") == "1"),
    reason="serialized native recovery proof needs RESTIC_TEST_BINARY and QORQUT_NATIVE_RECOVERY=1")


class FixtureKnownZeroHostWriters:
    """Test-only explicit known-zero host controller; never a production inventory."""

    def inventory_and_freeze(self, *, timeout):
        return QuiescenceProof("native_fixture_known_zero_host", "frozen")

    def thaw(self):
        pass


def _barrier(database, lock_root, ownership_root, docker):
    writers = DockerSandboxWriters(database, ownership_root, freezer=DockerFreezer(docker))
    return RecoveryQuiescer(
        database,
        admission=RecoveryAdmission(),
        installation_lock=InstallationRecoveryLock(lock_root),
        host_writers=FixtureKnownZeroHostWriters(),
        background=KnownZeroWriters("background"),
        signoffs=KnownZeroWriters("signoffs"), email_history=KnownZeroWriters("email_history"),
        browser_writers=KnownZeroWriters("browser"),
        sandbox_writers=KnownZeroWriters("sandbox"),
        container_writers=writers,
    )


def _runtime(database, root, barrier, workspace):
    """Build the actual Runtime recovery entrypoint without starting services."""
    return Runtime(database, root, workspace, None, None, None, None, None, None,
                   None, None, None, None, recovery_quiescer=barrier)


def _admin_config(tmp_path, root, store, restic_binary):
    private = tmp_path / "recovery-admin-private"
    private.mkdir(mode=0o700)
    password = store.password_file
    value = {
        "schema_version": 1,
        "businesses_root": str(root),
        "socket_path": str(private / "recovery.sock"),
        "repository": str(store.repository),
        "password_file": str(password),
        "authority_staging": str(store.authority_staging),
        "restic": {"executable": str(restic_binary.executable), "sha256": restic_binary.sha256,
                   "command_timeout_seconds": 120},
        "retention": {"daily": 2, "weekly": 2},
        "owner_uid": os.getuid(),
        "initialize_repository": True,
    }
    source = private / "recovery-admin.json"
    source.write_text(json.dumps(value))
    source.chmod(0o600)
    return RecoveryAdminConfig.load(source, businesses_root=root)


def _paused_canary_samples(docker, name, ownership, canary, done, samples):
    """Observe only actual Docker-paused intervals; no quiescence wrapper is used."""
    while not done.is_set():
        current = docker.owned("container", name, ownership)
        if current and current["State"].get("Paused"):
            samples.append(canary.read_bytes())
        time.sleep(0.01)


def _capture_pause_samples(docker, name, ownership, canary, operation):
    done, samples = threading.Event(), []
    observer = threading.Thread(target=_paused_canary_samples,
                                args=(docker, name, ownership, canary, done, samples), daemon=True)
    observer.start()
    try:
        result = operation()
    finally:
        done.set()
        observer.join(timeout=5)
        assert not observer.is_alive()
    assert samples, "the actual freezer never exposed a paused container interval"
    assert all(value == samples[0] for value in samples), "raw writer changed while Docker reported it paused"
    return result, samples[0]


def test_native_uds_capture_verify_and_offline_original_restore_with_actual_quiescer(tmp_path, restic_binary):
    """Exercise Runtime/UDS recovery with explicit test-only host controller inventories.

    This manually constructed Runtime is not production service assemble().
    """
    store, root, database, _, _ = root_git_linked_hq_fixture(tmp_path, restic_binary)
    business = "00000000-0000-4000-8000-000000000002"
    with database.transaction() as uow:
        SandboxStateStore().insert_intent(uow, business)
        row = dict(uow.repository("businesses").get(business))
    paths = BusinessPaths.registered(root, row)
    ownership = DockerEnvironmentDriver.ownership(paths)
    docker = Docker(command=("sudo", "-n", "docker"))
    image = "sha256:8cb359f1de8240b517fc8c01da76a214473dd0041c147c5ff6e05321d4d89f12"
    name = "qorqut-native-recovery-uds-" + new_id()[:8]
    canary = root / "first/code/raw-canary"
    # A modest untracked payload keeps the encrypted capture long enough for the
    # observer to read the actual Docker-paused interval without broad test I/O.
    (root / "first/code/repository/main/capture-payload.bin").write_bytes(os.urandom(1024 * 1024))
    command = ("while :; do date +%s%N >> /capture/raw-canary; sleep 0.02; done")
    created_container = False
    controller = None
    try:
        docker.call("run", "--detach", "--name", name, "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--network", "none", "--user", "1000:1000",
            "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5", "--pids-limit", "64",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m,mode=1777",
            *sum((("--label", key + "=" + value) for key, value in ownership.labels.items()), ()),
            "--mount", f"type=bind,src={root / 'first/code'},dst=/capture", image, "/bin/sh", "-c", command,
            timeout=60)
        created_container = True
        for _ in range(100):
            if canary.exists() and canary.stat().st_size:
                break
            time.sleep(0.02)
        assert canary.exists() and canary.stat().st_size

        barrier = _barrier(database, root, root, docker)
        runtime = _runtime(database, root, barrier, tmp_path)
        config = _admin_config(tmp_path, root, store, restic_binary)
        controller = RecoveryAdministration(database, config,
                                            lambda: runtime.recovery_quiescence(timeout=60))
        controller.start()
        assert stat.S_IMODE(config.socket_path.stat().st_mode) == 0o600
        generation = new_id()
        created, frozen_canary = _capture_pause_samples(
            docker, name, ownership, canary,
            lambda: client_request(config, {"action": "create", "generation_id": generation}, timeout=180),
        )
        assert created["ok"] is True
        assert created["result"]["generation_id"] == generation
        verified = client_request(config, {"action": "verify", "generation_id": generation}, timeout=180)
        assert verified["ok"] is True and verified["result"]["generation_id"] == generation
        # The prior gate was absent, therefore capture returns it to absent and
        # reopens admission only after its actual freezer has thawed the writer.
        with database.read() as uow:
            assert uow.repository("maintenance_state").get("service") is None
        assert barrier.admission._closed is False
        current = docker.owned("container", name, ownership)
        assert current and current["State"]["Running"] and not current["State"]["Paused"]
        resumed = canary.stat().st_size
        time.sleep(0.1)
        assert canary.stat().st_size > resumed
        controller.stop(timeout=10)
        controller = None

        # Keep the raw writer alive on the retired bind mount.  The second real
        # quiescer uses the retired authority and lock so it never recreates the
        # deliberately absent original root before restore_original_root checks it.
        retired = tmp_path / "retired-live-businesses"
        root.rename(retired)
        retired_database = Database(retired / ".bazarlyk/state.sqlite3").open_existing()
        restore_barrier = _barrier(retired_database, retired, root, docker)
        restore_runtime = _runtime(retired_database, retired, restore_barrier, tmp_path)
        retired_canary = retired / "first/code/raw-canary"
        restored, restore_frozen_canary = _capture_pause_samples(
            docker, name, ownership, retired_canary,
            lambda: store.restore_original_root(
                generation, quiescence=restore_runtime.recovery_quiescence(timeout=60)),
        )
        assert restored["state"] == "closed" and restored["destination"] == str(root)
        assert frozen_canary == (root / "first/code/raw-canary").read_bytes()
        # _capture_pause_samples already proved every read while Docker reported
        # pause matched restore_frozen_canary.  It is expected to change after thaw.
        assert restore_frozen_canary
        retired_resumed = retired_canary.stat().st_size
        time.sleep(0.1)
        assert retired_canary.stat().st_size > retired_resumed
        restored_database = Database(root / ".bazarlyk/state.sqlite3").initialize()
        with restored_database.read() as uow:
            assert uow.repository("maintenance_state").get("service")["state"] == "closed"
            assert uow.repository("businesses").get(business)["hq_path"] == str(root / "first/hq")
        hq = root / "first/hq"
        listed = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"], check=True,
                                text=True, capture_output=True).stdout
        assert "worktree " + str(hq) in listed and "branch refs/heads/hq-work" in listed
        status = subprocess.run(["git", "-C", str(hq), "status", "--porcelain"], check=True,
                                text=True, capture_output=True).stdout
        assert " M notes/evidence.md\n" in status and "?? linked-dirty.txt\n" in status
        assert (root / "first/code/repository/worktrees/dirty/dirty.txt").read_text() == "uncommitted work\n"
        assert (root / ".bazarlyk/sandboxes/11111111-1111-4111-8111-111111111111/home/.config/gh/hosts.yml").read_text().endswith("private-home-canary\n")
        assert (root / ".bazarlyk/browsers/22222222-2222-4222-8222-222222222222/Default/Cookies").read_text() == "private-browser-canary\n"
        assert "private-preview-canary" in (root / ".bazarlyk/preview-host.json").read_text()
        # The restore barrier did thaw the retained raw writer and restored the
        # retired authority's previous gate state after the original path became closed.
        assert restore_barrier.admission._closed is False
        with retired_database.read() as uow:
            assert uow.repository("maintenance_state").get("service") is None
        current = docker.owned("container", name, ownership)
        assert current and not current["State"]["Paused"]
    finally:
        if controller is not None:
            controller.stop(timeout=10)
        if created_container:
            docker.call("rm", "--force", name, timeout=60)
            assert docker.owned("container", name, ownership) is None
