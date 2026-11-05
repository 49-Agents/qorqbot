from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import shutil
import stat

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.sandbox.recovery import FullRecoveryStore, ResticBinary
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
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def restic_binary():
    value = os.environ.get("RESTIC_TEST_BINARY")
    if not value:
        pytest.skip("set RESTIC_TEST_BINARY to a checked restic 0.19.1 binary for encrypted recovery integration tests")
    return ResticBinary(value, digest(value))


def recovery_fixture(tmp_path, restic_binary, *, clock=lambda: datetime.now(timezone.utc).isoformat()):
    root = tmp_path / "businesses"
    (root / "first/hq/notes").mkdir(parents=True)
    db = Database(root / ".bazarlyk/state.sqlite3").initialize()
    owner, business = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"
    with db.transaction() as uow:
        uow.repository("principals").insert({"id": owner, "kind": "human", "display_name": "Owner"})
        uow.repository("businesses").insert({"id": business, "slug": "first", "name": "First",
                                               "workspace": str(root / "first"), "hq_path": str(root / "first/hq")})
        uow.repository("business_memberships", business_id=business).insert({"principal_id": owner, "is_owner": True})
    document = {"path": "notes/evidence.md"}
    (root / "first/hq" / document["path"]).write_text("authoritative data\n")
    (root / "CLAUDE.md").write_text("registered root context\n")
    code = root / "first/code/repository/main"
    code.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(code)], check=True)
    subprocess.run(["git", "-C", str(code), "config", "user.email", "recovery@example.test"], check=True)
    subprocess.run(["git", "-C", str(code), "config", "user.name", "Recovery test"], check=True)
    (code / "tracked.txt").write_text("committed source\n")
    subprocess.run(["git", "-C", str(code), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(code), "commit", "-qm", "initial"], check=True)
    worktree = root / "first/code/repository/worktrees/dirty"
    subprocess.run(["git", "-C", str(code), "worktree", "add", "-qb", "dirty-work", str(worktree)], check=True)
    (worktree / "dirty.txt").write_text("uncommitted work\n")
    outside = tmp_path / "outside-secret"
    outside.write_text("must never be followed\n")
    (code / "linked-directory").symlink_to(outside)
    sandbox = root / ".bazarlyk/sandboxes/11111111-1111-4111-8111-111111111111/home"
    (sandbox / ".config/gh").mkdir(parents=True)
    (sandbox / ".config/gh/hosts.yml").write_text("oauth_token: private-home-canary\n")
    (sandbox / "Downloads").mkdir()
    (sandbox / "Downloads/report.csv").write_text("browser download\n")
    browser = root / ".bazarlyk/browsers/22222222-2222-4222-8222-222222222222/Default"
    browser.mkdir(parents=True)
    (browser / "Cookies").write_text("private-browser-canary\n")
    proxy = root / ".bazarlyk/secrets/browser-proxy"
    proxy.mkdir(parents=True)
    (proxy / "22222222-2222-4222-8222-222222222222.json").write_text('{"password":"private-proxy-canary"}\n')
    (root / ".bazarlyk/sandbox-host.json").write_text('{"credential":"private-control-canary"}\n')
    preview_host = root / ".bazarlyk/preview-host.json"
    preview_host.write_text('{"schema_version":1,"bind_address":"127.0.0.1","owner_addresses":["127.0.0.1"],"first_port":20000,"last_port":20002,"private_canary":"private-preview-canary"}\n')
    preview_host.chmod(0o600)
    (root / "first/hq/.env").write_text("BUSINESS_PRIVATE=private-business-canary\n")
    (root / ".bazarlyk/sandboxes/11111111-1111-4111-8111-111111111111/reconcile.lock").write_text("ephemeral\n")
    password = tmp_path / "restic-password"
    password.write_text("correct horse battery staple\n")
    password.chmod(0o600)
    store = FullRecoveryStore(db, root, repository=tmp_path / "encrypted-repository", password_file=password,
                              authority_staging=tmp_path / "authority-staging", restic=restic_binary,
                              initialize_repository=True, clock=clock)
    return store, root, db, document, password


def test_full_encrypted_recovery_rehearses_original_bindings_and_preserves_git_home_browser_and_credentials(tmp_path, restic_binary):
    store, root, _, document, _ = recovery_fixture(tmp_path, restic_binary)
    generation, quiescence = new_id(), Quiesced()
    created = store.create(generation, quiescence=quiescence, daily=2, weekly=2)
    assert quiescence.entered and quiescence.exited
    assert created["generation_id"] == generation and created["state"] == "complete"
    assert not (store.authority_staging / generation).exists()
    assert store.verify(generation)["snapshot_id"] == created["snapshot_id"]

    restored = tmp_path / "rehearsal"
    result = store.restore(generation, restored)
    assert result == {"generation_id": generation, "snapshot_id": created["snapshot_id"], "destination": str(restored),
                      "activation_root": str(root), "state": "closed", "scope": "full_installation"}
    restored_db = Database(restored / ".bazarlyk/state.sqlite3").initialize()
    with restored_db.read() as uow:
        assert uow.repository("maintenance_state").get("service")["state"] == "closed"
        assert uow.repository("businesses").list()[0]["hq_path"] == str(root / "first/hq")
    assert json.loads((restored / ".bazarlyk/authority.json").read_text())["state"] == "maintenance"
    assert (restored / "first/hq" / document["path"]).read_bytes() == (root / "first/hq" / document["path"]).read_bytes()
    assert (restored / "first/code/repository/main/.git/HEAD").read_text() == "ref: refs/heads/main\n"
    assert (restored / "first/code/repository/main/.git/worktrees/dirty").is_dir()
    assert (restored / "first/code/repository/worktrees/dirty/dirty.txt").read_text() == "uncommitted work\n"
    assert (restored / "first/code/repository/main/linked-directory").is_symlink()
    assert os.readlink(restored / "first/code/repository/main/linked-directory") == str(tmp_path / "outside-secret")
    assert (restored / ".bazarlyk/sandboxes/11111111-1111-4111-8111-111111111111/home/.config/gh/hosts.yml").read_text().endswith("private-home-canary\n")
    assert (restored / ".bazarlyk/browsers/22222222-2222-4222-8222-222222222222/Default/Cookies").read_text() == "private-browser-canary\n"
    assert "private-proxy-canary" in (restored / ".bazarlyk/secrets/browser-proxy/22222222-2222-4222-8222-222222222222.json").read_text()
    assert "private-control-canary" in (restored / ".bazarlyk/sandbox-host.json").read_text()
    assert "private-preview-canary" in (restored / ".bazarlyk/preview-host.json").read_text()
    assert stat.S_IMODE((restored / ".bazarlyk/preview-host.json").stat().st_mode) == 0o600
    assert "private-business-canary" in (restored / "first/hq/.env").read_text()
    assert not (restored / ".bazarlyk/sandboxes/11111111-1111-4111-8111-111111111111/reconcile.lock").exists()
    with pytest.raises(DomainError, match="empty"):
        store.restore(generation, restored)

    retired = tmp_path / "retired-live-businesses"
    root.rename(retired)
    original_quiescence = Quiesced()
    original = store.restore_original_root(generation, quiescence=original_quiescence)
    assert original_quiescence.entered and original_quiescence.exited
    assert original["destination"] == str(root) and original["state"] == "closed"
    original_db = Database(root / ".bazarlyk/state.sqlite3").initialize()
    with original_db.read() as uow:
        assert uow.repository("maintenance_state").get("service")["state"] == "closed"
        assert uow.repository("businesses").get("00000000-0000-4000-8000-000000000002")["hq_path"] == str(root / "first/hq")
    assert json.loads((root / ".bazarlyk/authority.json").read_text())["state"] == "maintenance"
    restored_main = root / "first/code/repository/main"
    restored_worktree = root / "first/code/repository/worktrees/dirty"
    assert (restored_main / ".git/worktrees/dirty").is_dir()
    listed = subprocess.run(["git", "-C", str(restored_main), "worktree", "list", "--porcelain"], check=True,
                            text=True, capture_output=True).stdout
    assert "worktree " + str(restored_worktree) in listed
    assert subprocess.run(["git", "-C", str(restored_worktree), "status", "--porcelain"], check=True,
                          text=True, capture_output=True).stdout == "?? dirty.txt\n"
    assert (root / ".bazarlyk/sandboxes/11111111-1111-4111-8111-111111111111/home/.config/gh/hosts.yml").read_text().endswith("private-home-canary\n")
    assert (root / ".bazarlyk/browsers/22222222-2222-4222-8222-222222222222/Default/Cookies").read_text() == "private-browser-canary\n"
    assert "private-preview-canary" in (root / ".bazarlyk/preview-host.json").read_text()
    assert stat.S_IMODE((root / ".bazarlyk/preview-host.json").stat().st_mode) == 0o600
    with pytest.raises(DomainError, match="nonempty"):
        store.restore_original_root(generation, quiescence=Quiesced())


def test_recovery_rejects_wrong_password_tamper_and_non_quiesced_capture(tmp_path, restic_binary):
    store, _, _, _, _ = recovery_fixture(tmp_path, restic_binary)
    generation = new_id()
    with pytest.raises(DomainError, match="quiescence"):
        store.create(generation, quiescence=object())
    store.create(generation, quiescence=Quiesced())
    wrong = tmp_path / "wrong-password"
    wrong.write_text("wrong password\n")
    wrong.chmod(0o600)
    rejected = FullRecoveryStore(store.database, store.root, repository=store.repository, password_file=wrong,
                                 authority_staging=tmp_path / "other-authority", restic=store.restic.binary)
    with pytest.raises(DomainError):
        rejected.verify(generation)
    pack = next(path for path in (store.repository / "data").rglob("*") if path.is_file())
    pack.chmod(0o600)
    with pack.open("r+b") as stream:
        stream.seek(0)
        stream.write(b"x")
    with pytest.raises(DomainError):
        store.verify(generation)
    with pytest.raises(DomainError):
        store.restore(generation, tmp_path / "tampered")


def test_retention_runs_only_after_new_verified_generation(tmp_path, restic_binary):
    moments = [datetime(2026, 9, 1, 12, tzinfo=timezone.utc)]
    store, _, _, _, _ = recovery_fixture(tmp_path, restic_binary, clock=lambda: moments[0].isoformat())
    # Even if an operator shares an encrypted repository across installations,
    # this installation's retention cannot prune another one's generations.
    # Same host, older date: an unscoped forget would remove this snapshot.
    foreign_tag = "qorqut-installation:" + "f" * 64
    foreign = tmp_path / "other-installation"
    foreign.write_text("retained foreign installation evidence")
    generations = []
    for offset in range(3):
        moments[0] = datetime(2026, 9, 1, 12, tzinfo=timezone.utc) + timedelta(days=offset)
        generation = new_id()
        store.create(generation, quiescence=Quiesced(), daily=1, weekly=1)
        generations.append(generation)
        if offset == 0:
            store.restic._run(["backup", "--host", "qorqut-full-installation", "--tag", "qorqut-full-recovery",
                               "--tag", foreign_tag, "--time", "2026-08-01 12:00:00", str(foreign)])
    assert store.verify(generations[-1])["generation_id"] == generations[-1]
    with pytest.raises(DomainError):
        store.verify(generations[0])
    foreign_snapshots = json.loads(store.restic._run(["snapshots", "--tag", "qorqut-full-recovery," + foreign_tag], json_output=True))
    assert len(foreign_snapshots) == 1


def root_git_linked_hq_fixture(tmp_path, restic_binary):
    store, root, database, document, password = recovery_fixture(tmp_path, restic_binary)
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "recovery@example.test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Recovery test"], check=True)
    (root / "ROOT.md").write_text("installation root tracked outside authority inventory\n")
    subprocess.run(["git", "-C", str(root), "add", "ROOT.md", "CLAUDE.md"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "root initial"], check=True)
    hq = root / "first/hq"
    hq_seed = tmp_path / "hq-seed"
    hq.rename(hq_seed)
    subprocess.run(["git", "-C", str(root), "worktree", "add", "-qb", "hq-work", str(hq)], check=True)
    shutil.copytree(hq_seed, hq, dirs_exist_ok=True)
    subprocess.run(["git", "-C", str(hq), "add", "notes/evidence.md"], check=True)
    subprocess.run(["git", "-C", str(hq), "commit", "-qm", "HQ initial"], check=True)
    (hq / "notes/evidence.md").write_text("dirty authoritative data\n")
    (hq / "linked-dirty.txt").write_text("uncommitted HQ work\n")
    return store, root, database, document, password


def test_full_recovery_preserves_root_git_and_linked_hq_metadata_at_original_paths(tmp_path, restic_binary):
    store, root, _, _, _ = root_git_linked_hq_fixture(tmp_path, restic_binary)
    generation = new_id()
    store.create(generation, quiescence=Quiesced())
    root.rename(tmp_path / "retired-live-businesses")
    store.restore_original_root(generation, quiescence=Quiesced())

    worktrees = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"], check=True,
                               text=True, capture_output=True).stdout
    hq = root / "first/hq"
    assert "worktree " + str(hq) in worktrees
    assert "branch refs/heads/hq-work" in worktrees
    assert subprocess.run(["git", "-C", str(hq), "branch", "--show-current"], check=True,
                          text=True, capture_output=True).stdout.strip() == "hq-work"
    hq_status = subprocess.run(["git", "-C", str(hq), "status", "--porcelain"], check=True,
                               text=True, capture_output=True).stdout
    assert " M notes/evidence.md\n" in hq_status
    assert "?? linked-dirty.txt\n" in hq_status
    # Root working-tree files outside BackupStore's registered authority
    # inventory are deliberately not represented by the optional Git metadata.
    root_status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], check=True,
                                 text=True, capture_output=True).stdout
    assert " D ROOT.md\n" in root_status


def test_recovery_refuses_an_installation_gitdir_file_outside_the_registered_root(tmp_path, restic_binary):
    store, root, _, _, _ = recovery_fixture(tmp_path, restic_binary)
    (root / ".git").write_text("gitdir: " + str(tmp_path / "outside-common-dir") + "\n")
    with pytest.raises(DomainError, match="Installation Git metadata"):
        store.create(new_id(), quiescence=Quiesced())


def test_recovery_refuses_linked_hq_gitdir_that_lexically_escapes_root_git(tmp_path, restic_binary):
    store, root, _, _, _ = root_git_linked_hq_fixture(tmp_path, restic_binary)
    (root / "first/hq/.git").write_text("gitdir: " + str(root / ".git/../../outside") + "\n")
    with pytest.raises(DomainError, match="points outside"):
        store.create(new_id(), quiescence=Quiesced())


@pytest.mark.parametrize("contents", [b"gitdir: \xff\n", b"gitdir: " + b"x" * 4097 + b"\n"])
def test_recovery_rejects_malformed_or_unbounded_linked_hq_git_metadata(tmp_path, restic_binary, contents):
    store, root, _, _, _ = root_git_linked_hq_fixture(tmp_path, restic_binary)
    (root / "first/hq/.git").write_bytes(contents)
    with pytest.raises(DomainError, match="metadata is invalid"):
        store.create(new_id(), quiescence=Quiesced())


def test_recovery_rejects_linked_hq_common_dir_outside_installation_git(tmp_path, restic_binary):
    store, root, _, _, _ = root_git_linked_hq_fixture(tmp_path, restic_binary)
    gitdir = Path((root / "first/hq/.git").read_text().removeprefix("gitdir: ").strip())
    (gitdir / "commondir").write_text("../../../outside\n")
    with pytest.raises(DomainError, match="common directory points outside"):
        store.create(new_id(), quiescence=Quiesced())


def test_recovery_rejects_symlink_traversal_to_linked_hq_gitdir(tmp_path, restic_binary):
    store, root, _, _, _ = root_git_linked_hq_fixture(tmp_path, restic_binary)
    worktrees = root / ".git/worktrees"
    relocated = tmp_path / "relocated-worktrees"
    worktrees.rename(relocated)
    worktrees.symlink_to(relocated, target_is_directory=True)
    with pytest.raises(DomainError, match="crosses a symbolic link"):
        store.create(new_id(), quiescence=Quiesced())
