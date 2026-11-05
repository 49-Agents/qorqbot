from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import json
import os
from pathlib import Path
import socket
import stat

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.sandbox.recovery import ResticBinary
from bazarlyk.sandbox.recovery_admin import RecoveryAdminConfig, RecoveryAdministration, client_request, main
from bazarlyk.storage import Database


class Quiesced(AbstractContextManager):
    def __init__(self):
        self.entered = self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exited = True
        return False


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def restic_binary():
    value = os.environ.get("RESTIC_TEST_BINARY")
    if not value:
        pytest.skip("set RESTIC_TEST_BINARY to a checked restic binary for encrypted recovery integration tests")
    return ResticBinary(value, digest(value))


def recovery_database(root):
    (root / "first/hq/notes").mkdir(parents=True)
    database = Database(root / ".bazarlyk/state.sqlite3").initialize()
    owner, business = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": owner, "kind": "human", "display_name": "Owner"})
        uow.repository("businesses").insert({"id": business, "slug": "first", "name": "First",
                                               "workspace": str(root / "first"), "hq_path": str(root / "first/hq")})
        uow.repository("business_memberships", business_id=business).insert({"principal_id": owner, "is_owner": True})
    (root / "first/hq/notes/evidence.md").write_text("authority\n")
    (root / "CLAUDE.md").write_text("registered context\n")
    code = root / "first/code/repository/main"
    code.mkdir(parents=True)
    (code / "dirty.txt").write_text("uncommitted code\n")
    home = root / ".bazarlyk/sandboxes/11111111-1111-4111-8111-111111111111/home"
    home.mkdir(parents=True)
    (home / ".credential").write_text("private canary\n")
    return database


def write_config(tmp_path, root, restic_binary, *, owner_uid=None):
    private = tmp_path / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    config_path = private / "recovery-admin.json"
    value = {
        "schema_version": 1,
        "businesses_root": str(root),
        "socket_path": str(private / "recovery.sock"),
        "repository": str(tmp_path / "encrypted-repository"),
        "password_file": str(tmp_path / "restic-password"),
        "authority_staging": str(tmp_path / "authority-staging"),
        "restic": {"executable": str(restic_binary.executable), "sha256": restic_binary.sha256,
                   "command_timeout_seconds": 120},
        "retention": {"daily": 2, "weekly": 2},
        "owner_uid": os.getuid() if owner_uid is None else owner_uid,
        "initialize_repository": True,
    }
    config_path.write_text(json.dumps(value))
    config_path.chmod(0o600)
    password = Path(value["password_file"])
    password.write_text("correct horse battery staple\n")
    password.chmod(0o600)
    return config_path, RecoveryAdminConfig.load(config_path, businesses_root=root)


def test_uds_create_verify_and_list_use_actual_encrypted_store_and_quiescence(tmp_path, restic_binary, capsys):
    root = tmp_path / "businesses"
    database = recovery_database(root)
    _config_path, config = write_config(tmp_path, root, restic_binary)
    contexts = []

    def quiescence():
        value = Quiesced()
        contexts.append(value)
        return value

    controller = RecoveryAdministration(database, config, quiescence)
    assert not config.socket_path.exists()
    controller.start()
    try:
        assert stat.S_IMODE(config.socket_path.lstat().st_mode) == 0o600
        generation = new_id()
        created = client_request(config, {"action": "create", "generation_id": generation}, timeout=180)
        assert created["ok"] and created["result"]["generation_id"] == generation
        assert contexts and contexts[0].entered and contexts[0].exited
        assert "sources" not in created["result"] and str(config.password_file) not in json.dumps(created)
        verified = client_request(config, {"action": "verify", "generation_id": generation}, timeout=180)
        assert verified["ok"] and verified["result"]["snapshot_id"] == created["result"]["snapshot_id"]
        listed = client_request(config, {"action": "list"}, timeout=180)
        assert listed == {"ok": True, "result": {"snapshots": [{"generation_id": generation,
            "snapshot_id": created["result"]["snapshot_id"], "created_at": created["result"]["created_at"]}]}}
        refused = client_request(config, {"action": "restore", "generation_id": generation})
        assert refused == {"ok": False, "code": "validation_error"}
        assert main(["--businesses-root", str(root), "--config", str(_config_path), "list"]) == 0
        assert json.loads(capsys.readouterr().out) == listed
    finally:
        controller.stop(timeout=5)
    assert not config.socket_path.exists()


def test_rejects_foreign_peer_without_running_recovery(tmp_path, restic_binary):
    root = tmp_path / "businesses"
    database = recovery_database(root)
    _config_path, config = write_config(tmp_path, root, restic_binary, owner_uid=os.getuid() + 1)
    called = []
    controller = RecoveryAdministration(database, config, lambda: called.append(True))
    controller.start()
    try:
        assert client_request(config, {"action": "list"}) == {"ok": False, "code": "forbidden"}
        assert called == []
    finally:
        controller.stop()


def test_private_config_and_socket_descriptors_reject_symlink_and_fifo(tmp_path, restic_binary):
    root = tmp_path / "businesses"
    root.mkdir()
    config_path, config = write_config(tmp_path, root, restic_binary)
    inside_root = root / "recovery-admin.json"
    inside_root.write_text(config_path.read_text())
    inside_root.chmod(0o600)
    with pytest.raises(DomainError, match="outside business mounts"):
        RecoveryAdminConfig.load(inside_root, businesses_root=root)
    link = tmp_path / "linked-config"
    link.symlink_to(config_path)
    with pytest.raises(DomainError, match="symbolic link"):
        RecoveryAdminConfig.load(link, businesses_root=root)
    os.unlink(config_path)
    os.mkfifo(config_path, 0o600)
    with pytest.raises(DomainError):
        RecoveryAdminConfig.load(config_path, businesses_root=root)
    config_path.unlink()
    _config_path, config = write_config(tmp_path, root, restic_binary)
    os.mkfifo(config.socket_path, 0o600)
    controller = RecoveryAdministration(Database(root / ".bazarlyk/state.sqlite3").initialize(), config, Quiesced)
    with pytest.raises(DomainError, match="already exists"):
        controller.start()


def test_existing_socket_is_never_replaced_and_construction_is_inert(tmp_path, restic_binary):
    root = tmp_path / "businesses"
    database = recovery_database(root)
    _config_path, config = write_config(tmp_path, root, restic_binary)
    controller = RecoveryAdministration(database, config, Quiesced)
    assert not config.socket_path.exists()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as existing:
        existing.bind(os.fspath(config.socket_path))
        before = config.socket_path.lstat().st_ino
        with pytest.raises(DomainError, match="already exists"):
            controller.start()
        assert config.socket_path.lstat().st_ino == before
