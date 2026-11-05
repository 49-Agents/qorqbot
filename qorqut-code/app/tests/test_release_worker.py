from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import pytest

from bazarlyk.release_worker.adapters import ADAPTER_TYPES, Adapter, SSHExecutor, SubprocessExecutor, _tree_digest, executor_for_component
from bazarlyk.release_worker.errors import ReconciliationRequired, ReleaseError, UncertainRemoteEffect, UnsupportedAdapter
from bazarlyk.release_worker.io import InstallationLock, RecordStore, digest, redact
from bazarlyk.release_worker.manifest import load_plan, plan_manifest
from bazarlyk.release_worker.cli import main as cli_main
from bazarlyk.release_worker.gitops import cleanup_component, prepare_cleanup, prepare_merge, reconcile_cleanup, reconcile_merge
from bazarlyk.release_worker.worker import ReleaseWorker


def run(*args: str, cwd: Path) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    run("git", "init", "-b", "main", str(repo), cwd=tmp_path)
    run("git", "config", "user.name", "Release Test", cwd=repo)
    run("git", "config", "user.email", "release@example.invalid", cwd=repo)
    (repo / "README").write_text("base\n")
    run("git", "add", ".", cwd=repo)
    run("git", "commit", "-m", "base", cwd=repo)
    run("git", "clone", "--bare", str(repo), str(remote), cwd=tmp_path)
    run("git", "remote", "add", "origin", str(remote), cwd=repo)
    run("git", "push", "-u", "origin", "main", cwd=repo)
    run("git", "checkout", "-b", "feature/release", cwd=repo)
    (repo / "change").write_text("feature\n")
    run("git", "add", "change", cwd=repo)
    run("git", "commit", "-m", "feature", cwd=repo)
    run("git", "push", "-u", "origin", "feature/release", cwd=repo)
    return repo, run("git", "rev-parse", "HEAD", cwd=repo)


def manifest(repo: Path, *, category: str = "documentation", release_id: str = "test-release") -> dict:
    installation = {"id": "fixture"}
    components = [
        {
            "id": "component",
            "category": category,
            "repository": str(repo),
            "source_ref": "feature/release",
            "target_branch": "main",
            "tests": ["fixture"],
        }
    ]
    return {
        "schema_version": 1,
        "release_id": release_id,
        "installation": installation,
        "components": components,
    }


def test_plan_freezes_exact_source_and_dependency_order(tmp_path: Path):
    repo, head = git_repo(tmp_path)
    value = manifest(repo)
    value["components"].append(
        {
            "id": "later",
            "category": "documentation",
            "repository": str(repo),
            "source_ref": "feature/release",
            "target_branch": "main",
            "dependencies": ["component"],
        }
    )
    planned = plan_manifest(value)
    assert planned["execution_order"] == ["component", "later"]
    assert planned["components"][0]["source_commit"] == head
    assert len(planned["plan_digest"]) == len("sha256:") + 64


def test_inspect_is_read_only_and_does_not_create_a_record(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    planned = plan_manifest(manifest(repo))
    records = tmp_path / "records"
    worker = ReleaseWorker(planned, records_root=records)

    observed = worker.inspect()

    assert observed["component"]["adapter"] == "documentation"
    assert not (records / planned["release_id"] / "record.json").exists()
    assert not (records / planned["release_id"] / "events.jsonl").exists()


def test_cleanup_rejects_worktree_that_changed_branch_after_planning(tmp_path: Path):
    worktree, component = _dedicated_merged_worktree(tmp_path)
    intent = prepare_cleanup(component, confirm=True)
    run("git", "checkout", "-b", "feature/unrelated", cwd=worktree)
    (worktree / "unrelated").write_text("must remain\n")
    run("git", "add", "unrelated", cwd=worktree)
    run("git", "commit", "-m", "unrelated task", cwd=worktree)

    with pytest.raises(ReleaseError, match="branch changed"):
        cleanup_component(component, confirm=True, intent=intent)
    assert worktree.exists()
    assert (worktree / "unrelated").read_text() == "must remain\n"


def test_installation_lock_is_canonical_across_record_roots(tmp_path: Path):
    config = {"id": "fixture", "release_root": str(tmp_path / "destination")}
    first = InstallationLock(config, "fixture")
    second = InstallationLock(config, "fixture")
    with first:
        with pytest.raises(ReleaseError, match="already running"):
            second.__enter__()


def test_local_and_ssh_lock_protocols_share_the_destination_lock(tmp_path: Path):
    class LocalSSHExecutor(SSHExecutor):
        def run(self, argv: list[str], *, check: bool = True) -> str:
            owner = getattr(self._owner_context, "value", None)
            command = self._encoded_remote(argv, owner=owner) if owner else None
            result = subprocess.run(
                ["bash", "-c", command] if command else argv,
                text=True,
                capture_output=True,
                check=False,
            )
            if check and result.returncode:
                raise ReleaseError(result.stderr.strip() or "remote fixture command failed")
            return result.stdout.strip()

    config = {"id": "fixture", "release_root": str(tmp_path / "destination")}
    local = InstallationLock(config, "fixture")
    remote = InstallationLock(config, "fixture", executor=LocalSSHExecutor("fixture-host"))
    with local:
        with pytest.raises(ReleaseError):
            remote.__enter__()
    with remote:
        with pytest.raises(ReleaseError):
            local.__enter__()


def test_ssh_active_command_blocks_lease_takeover(tmp_path: Path):
    class LocalSSHExecutor(SSHExecutor):
        def run(self, argv: list[str], *, check: bool = True) -> str:
            result = subprocess.run(argv, text=True, capture_output=True, check=False)
            if check and result.returncode:
                raise ReleaseError(result.stderr.strip() or "remote fixture command failed")
            return result.stdout.strip()

    config = {"id": "fixture", "release_root": str(tmp_path / "destination")}
    executor = LocalSSHExecutor("fixture-host")
    first = InstallationLock(config, "fixture", executor=executor)
    second = InstallationLock(config, "fixture", executor=executor)
    with first:
        with first.command_scope("remote-mutation"):
            with pytest.raises(ReleaseError):
                second.__enter__()


def test_ssh_command_wrapper_records_destination_process_identity(tmp_path: Path):
    class LocalSSHExecutor(SSHExecutor):
        def run(self, argv: list[str], *, check: bool = True) -> str:
            owner = getattr(self._owner_context, "value", None)
            command = self._encoded_remote(argv, owner=owner) if owner else None
            result = subprocess.run(
                ["bash", "-c", command] if command else argv,
                text=True,
                capture_output=True,
                check=False,
            )
            if check and result.returncode:
                raise ReleaseError(result.stderr.strip() or "remote fixture command failed")
            return result.stdout.strip()

    config = {"id": "fixture", "host": "fixture-host", "release_root": str(tmp_path / "destination")}
    executor = LocalSSHExecutor("fixture-host")
    lock = InstallationLock(config, "fixture", executor=executor)
    with lock:
        with lock.command_scope("remote-write"):
            assert lock._command_id is not None
            with executor.owner_context(lock.remote_path + ".d", lock.owner_token, lock._command_id):
                assert executor.run(["python3", "-c", "print('wrapped')"]) == "wrapped"
            active = json.loads(lock.owner_path.read_text())["active_command"]
            assert active["pid"] > 0
            assert active["host"] == socket.gethostname()
            assert active["process_start"]
            assert lock._refresh_remote() is True
            assert json.loads(lock.owner_path.read_text())["active_command"]["id"] == active["id"]


def test_remote_step_assigns_each_launch_identity_and_transfers_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("#!/bin/sh\nif [ \"$1\" = \"--\" ]; then shift; fi\nshift\nexec /bin/sh -c \"$1\"\n")
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_text("payload\n")
    destination = tmp_path / "destination" / "release"
    config = {"id": "fixture", "host": "fixture-host", "release_root": str(tmp_path / "locks")}
    executor = SSHExecutor("fixture-host")
    lock = InstallationLock(config, "fixture", executor=executor)
    guarded = lock.guarded(executor)

    with lock:
        with lock.hold_scope("release-step"):
            assert guarded.run(["python3", "-c", "print('first')"]) == "first"
            assert guarded.run(["python3", "-c", "print('second')"]) == "second"
            guarded.copy_tree(source, destination)

    assert (destination / "payload.txt").read_text() == "payload\n"


def test_interrupted_artifact_extraction_retains_running_destination_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--\" ]; then shift; fi\n"
        "shift\n"
        "if echo \"$1\" | grep -q 'os.execvp'; then\n"
        "  payload=${1##* }\n"
        "  argv0=$(python3 -c 'import base64,json,sys; value=json.loads(base64.urlsafe_b64decode(sys.argv[1])); print(value.get(\"argv\", [\"\"])[0] if isinstance(value,dict) else \"\" )' \"$payload\" 2>/dev/null || true)\n"
        "  if [ \"$argv0\" = \"tar\" ]; then /bin/sh -c \"$1\" & exit 255; fi\n"
        "fi\n"
        "exec /bin/sh -c \"$1\"\n"
    )
    fake_ssh.chmod(0o755)
    fake_tar = tmp_path / "tar"
    fake_tar.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in *\" -xf - \"*) sleep 2;; esac\n"
        "exec /bin/tar \"$@\"\n"
    )
    fake_tar.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    source = tmp_path / "source"
    source.mkdir()
    (source / "payload.txt").write_text("payload\n")
    destination = tmp_path / "destination" / "release"
    config = {"id": "fixture", "host": "fixture-host", "release_root": str(tmp_path / "locks")}
    executor = SSHExecutor("fixture-host")
    lock = InstallationLock(config, "fixture", executor=executor)

    try:
        with lock:
            with pytest.raises(UncertainRemoteEffect):
                with lock.hold_scope("release-step"):
                    lock.guarded(executor).copy_tree(source, destination)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                active = json.loads(lock.owner_path.read_text()).get("active_command", {})
                if active.get("state") == "running" and active.get("pid"):
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("extraction did not register a destination process")
            assert lock._retain_on_exit is True
            assert lock.lock_directory.exists()
    finally:
        lock._retain_on_exit = False
        lock.__exit__(None, None, None)


def test_ssh_live_command_blocks_takeover_after_active_grace_expires(tmp_path: Path):
    class LocalSSHExecutor(SSHExecutor):
        def run(self, argv: list[str], *, check: bool = True) -> str:
            owner = getattr(self._owner_context, "value", None)
            command = self._encoded_remote(argv, owner=owner) if owner else None
            result = subprocess.run(
                ["bash", "-c", command] if command else argv,
                text=True,
                capture_output=True,
                check=False,
            )
            if check and result.returncode:
                raise ReleaseError(result.stderr.strip() or "remote fixture command failed")
            return result.stdout.strip()

    config = {"id": "fixture", "host": "fixture-host", "release_root": str(tmp_path / "destination")}
    executor = LocalSSHExecutor("fixture-host")
    first = InstallationLock(config, "fixture", executor=executor)
    second = InstallationLock(config, "fixture", executor=executor)
    finished = threading.Event()
    with first:
        with first.command_scope("remote-sleep"):
            assert first._command_id is not None

            def run_remote_command() -> None:
                with executor.owner_context(first.remote_path + ".d", first.owner_token, first._command_id):
                    executor.run(["python3", "-c", "import time; time.sleep(2)"])
                finished.set()

            thread = threading.Thread(target=run_remote_command)
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                active = json.loads(first.owner_path.read_text()).get("active_command", {})
                if active.get("pid"):
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("remote command did not register its process")
            owner = json.loads(first.owner_path.read_text())
            owner["active_command"]["expires_at"] = 0
            first.owner_path.write_text(json.dumps(owner))
            with pytest.raises(ReleaseError):
                second.__enter__()
            thread.join(timeout=5)
            assert finished.is_set()


def test_uncertain_remote_result_retains_active_lock_for_reconciliation(tmp_path: Path):
    class LocalSSHExecutor(SSHExecutor):
        def run(self, argv: list[str], *, check: bool = True) -> str:
            owner = getattr(self._owner_context, "value", None)
            command = self._encoded_remote(argv, owner=owner) if owner else None
            result = subprocess.run(
                ["bash", "-c", command] if command else argv,
                text=True,
                capture_output=True,
                check=False,
            )
            if check and result.returncode:
                raise ReleaseError(result.stderr.strip() or "remote fixture command failed")
            return result.stdout.strip()

    config = {"id": "fixture", "host": "fixture-host", "release_root": str(tmp_path / "destination")}
    executor = LocalSSHExecutor("fixture-host")
    first = InstallationLock(config, "fixture", executor=executor)
    second = InstallationLock(config, "fixture", executor=LocalSSHExecutor("fixture-host"))
    with pytest.raises(UncertainRemoteEffect):
        with first:
            with first.command_scope("remote-mutation"):
                owner = json.loads(first.owner_path.read_text())
                owner["active_command"].update(
                    {"state": "running", "pid": os.getpid(), "host": socket.gethostname()}
                )
                first.owner_path.write_text(json.dumps(owner))
                raise UncertainRemoteEffect("SSH response lost")

    owner = json.loads(first.owner_path.read_text())
    assert owner["active_command"]["id"] == first._command_id
    with pytest.raises(ReleaseError):
        second.__enter__()

    # The test owns a deliberately retained lock; release it only after the
    # retention assertion so the temporary destination cannot affect others.
    first._retain_on_exit = False
    first.__exit__(None, None, None)


def test_uncertain_setup_failure_cancels_pending_launch_before_release(tmp_path: Path):
    class SetupFailureSSHExecutor(SSHExecutor):
        def __init__(self, host: str):
            super().__init__(host)
            self.delayed_launch: str | None = None

        def run(self, argv: list[str], *, check: bool = True) -> str:
            owner = getattr(self._owner_context, "value", None)
            if owner and argv == ["python3", "-c", "print('delayed')"]:
                self.delayed_launch = self._encoded_remote(argv, owner=owner)
                raise UncertainRemoteEffect("SSH setup failed before launch")
            command = self._encoded_remote(argv, owner=owner) if owner else None
            result = subprocess.run(
                ["bash", "-c", command] if command else argv,
                text=True,
                capture_output=True,
                check=False,
            )
            if check and result.returncode:
                raise ReleaseError(result.stderr.strip() or "remote fixture command failed")
            return result.stdout.strip()

    config = {"id": "fixture", "host": "fixture-host", "release_root": str(tmp_path / "destination")}
    executor = SetupFailureSSHExecutor("fixture-host")
    lock = InstallationLock(config, "fixture", executor=executor)
    guarded = lock.guarded(executor)
    with lock:
        with pytest.raises(UncertainRemoteEffect):
            with lock.command_scope("remote-mutation"):
                guarded.run(["python3", "-c", "print('delayed')"])
        assert executor.delayed_launch is not None
        assert "active_command" not in json.loads(lock.owner_path.read_text())

        delayed = subprocess.run(
            ["bash", "-c", executor.delayed_launch],
            text=True,
            capture_output=True,
            check=False,
        )
        assert delayed.returncode != 0
        assert "delayed" not in delayed.stdout


def test_ssh_transport_failure_is_classified_as_uncertain(monkeypatch: pytest.MonkeyPatch):
    class Result:
        returncode = 255
        stdout = ""
        stderr = "connection reset"

    monkeypatch.setattr("bazarlyk.release_worker.adapters.subprocess.run", lambda *args, **kwargs: Result())
    with pytest.raises(UncertainRemoteEffect, match="transport failed"):
        SSHExecutor("fixture-host").run(["true"])


def test_interrupted_lock_owner_can_be_reclaimed_but_live_owner_cannot(tmp_path: Path):
    config = {"id": "fixture", "release_root": str(tmp_path / "destination")}
    stale = InstallationLock(config, "fixture")
    stale.lock_directory.mkdir(parents=True)
    stale.owner_path.write_text(json.dumps({
        "protocol": 1,
        "owner": "interrupted",
        "pid": 999999,
        "host": socket.gethostname(),
        "heartbeat_at": 0,
        "lease_seconds": 1,
    }))
    with InstallationLock(config, "fixture"):
        assert stale.lock_directory.exists()

    live = InstallationLock(config, "fixture")
    live.lock_directory.mkdir(parents=True)
    live.owner_path.write_text(json.dumps({
        "protocol": 1,
        "owner": "live",
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "heartbeat_at": 0,
        "lease_seconds": 1,
    }))
    with pytest.raises(ReleaseError, match="still live"):
        InstallationLock(config, "fixture").__enter__()


def test_expired_pending_launch_is_cancelled_before_reacquisition(tmp_path: Path):
    class LocalSSHExecutor(SSHExecutor):
        def run(self, argv: list[str], *, check: bool = True) -> str:
            owner = getattr(self._owner_context, "value", None)
            command = self._encoded_remote(argv, owner=owner) if owner else None
            result = subprocess.run(
                ["bash", "-c", command] if command else argv,
                text=True,
                capture_output=True,
                check=False,
            )
            if check and result.returncode:
                raise ReleaseError(result.stderr.strip() or "remote fixture command failed")
            return result.stdout.strip()

    config = {"id": "fixture", "host": "fixture-host", "release_root": str(tmp_path / "destination")}
    executor = LocalSSHExecutor("fixture-host")
    first = InstallationLock(config, "fixture", executor=executor)
    second = InstallationLock(config, "fixture", executor=LocalSSHExecutor("fixture-host"))
    with first:
        first._stop_heartbeat()
        owner = json.loads(first.owner_path.read_text())
        owner["heartbeat_at"] = 0
        owner["active_command"] = {
            "id": "pending-launch",
            "state": "pending_launch",
            "started_at": 0,
            "expires_at": 0,
        }
        first.owner_path.write_text(json.dumps(owner))
        with second:
            assert second.owner_path.exists()


def test_expired_operator_is_fenced_after_another_operator_reclaims_lock(tmp_path: Path):
    config = {"id": "fixture", "release_root": str(tmp_path / "destination")}
    first = InstallationLock(config, "fixture")
    second = InstallationLock(config, "fixture")
    with first:
        first._stop_heartbeat()
        first.owner_path.write_text(json.dumps({
            "protocol": 1,
            "owner": first.owner_token,
            "pid": 999999,
            "host": socket.gethostname(),
            "heartbeat_at": 0,
            "lease_seconds": 1,
        }))
        with second:
            with pytest.raises(ReleaseError, match="ownership was lost"):
                first.guarded(SubprocessExecutor()).run(["true"])


def test_ssh_entry_digest_executes_for_regular_files(tmp_path: Path):
    class LocalSSHExecutor(SSHExecutor):
        def run(self, argv: list[str], *, check: bool = True) -> str:
            result = subprocess.run(argv, text=True, capture_output=True, check=False)
            if check and result.returncode:
                raise ReleaseError(result.stderr.strip() or "remote fixture command failed")
            return result.stdout.strip()

    payload = tmp_path / "payload"
    payload.write_text("payload\n")
    assert LocalSSHExecutor("fixture-host").entry_digest(payload, "payload") == SubprocessExecutor().entry_digest(payload, "payload")


def test_merge_groups_components_sharing_repository_source_and_target(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo)
    value["components"].append(
        {
            "id": "second",
            "category": "documentation",
            "repository": str(repo),
            "source_ref": "feature/release",
            "target_branch": "main",
        }
    )
    planned = plan_manifest(value)
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")
    units = worker._units("merge")
    assert len(units) == 1
    assert {item["id"] for item in units[0][1]} == {"component", "second"}


def test_cleanup_deduplicates_components_sharing_a_worktree(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo)
    value["components"].append(
        {"id": "second", "category": "documentation", "repository": str(repo), "source_ref": "feature/release", "target_branch": "main"}
    )
    worker = ReleaseWorker(plan_manifest(value), records_root=tmp_path / "records")
    units = worker._units("cleanup")
    assert len(units) == 1
    assert {item["id"] for item in units[0][1]} == {"component", "second"}


def test_the_retired_dashboard_deployment_adapter_and_pinned_runtime_inputs_are_refused(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    assert "qorqut-dashboard" not in ADAPTER_TYPES
    for category in ("core", "dashboard"):
        with pytest.raises(ReleaseError, match="unsupported component category"):
            plan_manifest(manifest(repo, category=category))
    value = manifest(repo)
    value["components"][0]["adapter"] = "qorqut-dashboard"
    with pytest.raises(UnsupportedAdapter, match="not registered"):
        plan_manifest(value)
    value = manifest(repo)
    value["components"][0]["unchanged"] = True
    with pytest.raises(ReleaseError, match="unchanged is not supported"):
        plan_manifest(value)


def test_build_outputs_are_copied_into_the_staged_artifact(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo)
    value["components"][0]["tests"] = []
    value["components"][0]["build"] = [["python3", "-c", "from pathlib import Path; Path('dist').mkdir(); Path('dist/generated').write_text('built')"]]
    value["components"][0]["build_outputs"] = ["dist"]
    planned = plan_manifest(value)
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")
    worker.check()
    worker.build()
    worker.stage()
    assert (worker.artifact_root / "component" / "dist" / "generated").read_text() == "built"


def test_plan_persistence_keeps_digest_bound_executable_configuration(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    planned = plan_manifest(manifest(repo))
    planned["installation"]["credential_file"] = "/run/secrets/release-worker"
    planned["installation_digest"] = digest(planned["installation"])
    unsigned = dict(planned)
    unsigned.pop("plan_digest")
    planned["plan_digest"] = digest(unsigned)
    store = RecordStore(tmp_path / "records", planned["release_id"])
    store.create(planned)
    loaded = load_plan(store.plan_path)
    assert loaded["installation"]["credential_file"] == "/run/secrets/release-worker"


def test_redaction_covers_argument_pairs_and_bearer_headers():
    safe = redact({"command": ["publish", "--token", "secret-value", "-H", "Authorization: Bearer abc123"]})
    assert safe["command"] == ["publish", "--token", "<redacted>", "-H", "Authorization: Bearer <redacted>"]

    cookie = redact(["curl", "-H", "Cookie: session=session-secret; theme=dark"])
    assert "session-secret" not in json.dumps(cookie)
    assert cookie[-1] == "Cookie: <redacted>"


def test_release_check_command_is_executed_and_failing_notes_are_rejected(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo)
    value["checks"] = [{"command": ["false"]}]
    planned = plan_manifest(value)
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")
    with pytest.raises(ReleaseError):
        worker.check()
    assert worker.status()["steps"]["check:release"]["status"] == "failed"

    value["checks"] = ["false"]
    with pytest.raises(ReleaseError, match="explicitly"):
        plan_manifest(value)


def test_planning_rejects_literal_credentials_but_allows_credential_file_references(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo)
    value["checks"] = [{"command": ["publisher", "--token", "literal-secret"]}]
    with pytest.raises(ReleaseError, match="literal credential"):
        plan_manifest(value)

    value["checks"] = [{"command": ["publisher", "--token-file", "/run/secrets/publisher-token"]}]
    planned = plan_manifest(value)
    assert planned["checks"][0]["command"][-1] == "/run/secrets/publisher-token"

    value["checks"] = [{"command": ["curl", "-H", "Cookie: session=session-secret"]}]
    with pytest.raises(ReleaseError, match="literal authorization header"):
        plan_manifest(value)


def test_artifact_digest_includes_mode_and_symlink_identity(tmp_path: Path):
    root = tmp_path / "artifact"
    root.mkdir()
    target = root / "payload"
    target.write_text("same\n")
    first = _tree_digest(root)
    target.chmod(0o755)
    second = _tree_digest(root)
    link = root / "link"
    link.symlink_to("payload")
    third = _tree_digest(root)
    assert first != second
    assert second != third


def test_merge_intent_reconciles_remote_ancestry_after_interruption(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    component = plan_manifest(manifest(repo))["components"][0]

    intent = prepare_merge(component, confirm=True)
    before_push = reconcile_merge(component, intent)
    assert before_push["safe_to_retry"] is True

    run("git", "push", "origin", f"{intent['merge_commit']}:refs/heads/main", cwd=repo)

    after_push = reconcile_merge(component, intent)
    assert after_push["already_succeeded"] is True
    assert after_push["remote_target"] == intent["merge_commit"]


def _dedicated_merged_worktree(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    primary = tmp_path / "primary"
    remote = tmp_path / "remote.git"
    run("git", "init", "-b", "main", str(primary), cwd=tmp_path)
    run("git", "config", "user.name", "Release Test", cwd=primary)
    run("git", "config", "user.email", "release@example.invalid", cwd=primary)
    (primary / "README").write_text("base\n")
    run("git", "add", "README", cwd=primary)
    run("git", "commit", "-m", "base", cwd=primary)
    run("git", "clone", "--bare", str(primary), str(remote), cwd=tmp_path)
    run("git", "remote", "add", "origin", str(remote), cwd=primary)
    run("git", "push", "-u", "origin", "main", cwd=primary)
    worktree = tmp_path / "cleanup-task" / "repo"
    run("git", "worktree", "add", "-b", "feature/cleanup", str(worktree), "origin/main", cwd=primary)
    (worktree / "change").write_text("cleanup\n")
    run("git", "add", "change", cwd=worktree)
    run("git", "commit", "-m", "cleanup feature", cwd=worktree)
    run("git", "push", "-u", "origin", "feature/cleanup", cwd=worktree)
    run("git", "merge", "--no-ff", "feature/cleanup", "-m", "merge cleanup", cwd=primary)
    run("git", "push", "origin", "main", cwd=primary)
    return worktree, {
        "id": "cleanup",
        "repository": str(worktree),
        "checked_out_branch": "feature/cleanup",
        "target_branch": "main",
    }


def test_cleanup_removes_worktree_local_branch_remote_branch_and_task_directory(tmp_path: Path):
    worktree, component = _dedicated_merged_worktree(tmp_path)
    intent = prepare_cleanup(component, confirm=True)

    partial = reconcile_cleanup(component, intent)
    assert partial["safe_to_retry"] is True
    phases: list[str] = []
    result = cleanup_component(component, confirm=True, intent=intent, phase_callback=lambda name, value: phases.append(name))

    assert result["cleaned"] is True
    assert phases == ["worktree_removed", "local_branch_removed", "remote_branch_removed", "task_directory_removed"]
    assert not worktree.exists()
    assert not Path(intent["task_directory"]).exists()
    local = subprocess.run(["git", "-C", intent["common_repo"], "show-ref", "--verify", "--quiet", "refs/heads/feature/cleanup"], check=False)
    remote = subprocess.run(["git", "-C", intent["common_repo"], "ls-remote", "--exit-code", "origin", "refs/heads/feature/cleanup"], capture_output=True, check=False)
    assert local.returncode != 0
    assert remote.returncode == 2


def test_cleanup_refuses_to_delete_remote_branch_with_newer_tip(tmp_path: Path):
    worktree, component = _dedicated_merged_worktree(tmp_path)
    intent = prepare_cleanup(component, confirm=True)
    contributor = tmp_path / "contributor"
    run("git", "clone", str(tmp_path / "remote.git"), str(contributor), cwd=tmp_path)
    run("git", "config", "user.name", "Other Contributor", cwd=contributor)
    run("git", "config", "user.email", "other@example.invalid", cwd=contributor)
    run("git", "checkout", "-b", "feature/cleanup", "--track", "origin/feature/cleanup", cwd=contributor)
    (contributor / "newer").write_text("must not be deleted\n")
    run("git", "add", "newer", cwd=contributor)
    run("git", "commit", "-m", "newer contributor work", cwd=contributor)
    newer = run("git", "rev-parse", "HEAD", cwd=contributor)
    run("git", "push", "origin", "feature/cleanup", cwd=contributor)

    with pytest.raises(ReleaseError, match="remote branch tip changed"):
        cleanup_component(component, confirm=True, intent=intent)
    remote = subprocess.run(
        ["git", "-C", intent["common_repo"], "ls-remote", "--exit-code", "origin", "refs/heads/feature/cleanup"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert remote.returncode == 0
    assert remote.stdout.split()[0] == newer


def test_cleanup_defers_shared_task_directory_until_all_repositories_are_clean(tmp_path: Path):
    first_worktree, first_component = _dedicated_merged_worktree(tmp_path)
    second_root = tmp_path / "second"
    second_root.mkdir()
    second_repo, _ = git_repo(second_root)
    second_worktree = tmp_path / "cleanup-task" / "second-repo"
    run("git", "worktree", "add", "-b", "feature/cleanup-two", str(second_worktree), "origin/main", cwd=second_repo)
    (second_worktree / "change-two").write_text("cleanup two\n")
    run("git", "add", "change-two", cwd=second_worktree)
    run("git", "commit", "-m", "cleanup second", cwd=second_worktree)
    run("git", "push", "-u", "origin", "feature/cleanup-two", cwd=second_worktree)
    run("git", "checkout", "main", cwd=second_repo)
    run("git", "merge", "--no-ff", "feature/cleanup-two", "-m", "merge cleanup two", cwd=second_repo)
    run("git", "push", "origin", "main", cwd=second_repo)
    second_component = {
        "id": "cleanup-two",
        "repository": str(second_worktree),
        "checked_out_branch": "feature/cleanup-two",
        "target_branch": "main",
    }

    first_intent = prepare_cleanup(first_component, confirm=True)
    second_intent = prepare_cleanup(second_component, confirm=True)
    first_result = cleanup_component(first_component, confirm=True, intent=first_intent)
    assert first_result["task_directory_pending"] is True
    assert Path(first_intent["task_directory"]).exists()
    second_result = cleanup_component(second_component, confirm=True, intent=second_intent)
    assert second_result["cleaned"] is True
    assert not Path(first_intent["task_directory"]).exists()


def _bare_backed_merged_worktree(tmp_path: Path, *, tracking_refspec: bool = True) -> tuple[Path, Path, dict[str, str]]:
    """Mirror Bazarlyk's <workspace>/SEO-tooling.git with worktrees/<task>/SEO-tooling."""
    seed = tmp_path / "seed"
    remote = tmp_path / "remote.git"
    run("git", "init", "-b", "main", str(seed), cwd=tmp_path)
    run("git", "config", "user.name", "Release Test", cwd=seed)
    run("git", "config", "user.email", "release@example.invalid", cwd=seed)
    (seed / "README").write_text("base\n")
    run("git", "add", "README", cwd=seed)
    run("git", "commit", "-m", "base", cwd=seed)
    run("git", "clone", "--bare", str(seed), str(remote), cwd=tmp_path)
    bare = tmp_path / "workspace" / "SEO-tooling.git"
    run("git", "clone", "--bare", str(remote), str(bare), cwd=tmp_path)
    run("git", "config", "user.name", "Release Test", cwd=bare)
    run("git", "config", "user.email", "release@example.invalid", cwd=bare)
    start = "main"
    if tracking_refspec:
        run("git", "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=bare)
        run("git", "fetch", "origin", cwd=bare)
        start = "origin/main"
    worktree = tmp_path / "workspace" / "worktrees" / "bare-task" / "SEO-tooling"
    run("git", "worktree", "add", "-b", "feature/bare-cleanup", str(worktree), start, cwd=bare)
    (worktree / "change").write_text("cleanup\n")
    run("git", "add", "change", cwd=worktree)
    run("git", "commit", "-m", "bare cleanup feature", cwd=worktree)
    run("git", "push", "-u", "origin", "feature/bare-cleanup", cwd=worktree)
    # Merge through another clone so the bare repository's local main (its HEAD) stays stale.
    merger = tmp_path / "merger"
    run("git", "clone", str(remote), str(merger), cwd=tmp_path)
    run("git", "config", "user.name", "Release Test", cwd=merger)
    run("git", "config", "user.email", "release@example.invalid", cwd=merger)
    run("git", "merge", "--no-ff", "origin/feature/bare-cleanup", "-m", "merge bare cleanup", cwd=merger)
    run("git", "push", "origin", "main", cwd=merger)
    return bare, worktree, {
        "id": "bare-cleanup",
        "repository": str(worktree),
        "checked_out_branch": "feature/bare-cleanup",
        "target_branch": "main",
    }


@pytest.mark.parametrize("tracking_refspec", [True, False], ids=["tracking-refspec", "plain-bare-clone"])
def test_cleanup_removes_worktree_and_branches_of_a_bare_repository(tmp_path: Path, tracking_refspec: bool):
    bare, worktree, component = _bare_backed_merged_worktree(tmp_path, tracking_refspec=tracking_refspec)
    head = run("git", "rev-parse", "HEAD", cwd=worktree)
    stale_main = run("git", "rev-parse", "refs/heads/main", cwd=bare)
    # `git branch -d` in the bare repository would compare against this stale HEAD and refuse.
    assert subprocess.run(["git", "-C", str(bare), "merge-base", "--is-ancestor", head, stale_main], check=False).returncode == 1

    intent = prepare_cleanup(component, confirm=True)
    assert intent["common_repo"] == str(bare.resolve())
    assert reconcile_cleanup(component, intent)["safe_to_retry"] is True
    phases: list[str] = []
    result = cleanup_component(component, confirm=True, intent=intent, phase_callback=lambda name, value: phases.append(name))

    assert result["cleaned"] is True
    assert phases == ["worktree_removed", "local_branch_removed", "remote_branch_removed", "task_directory_removed"]
    assert not worktree.exists()
    assert not Path(intent["task_directory"]).exists()
    assert run("git", "rev-parse", "--is-bare-repository", cwd=bare) == "true"
    assert run("git", "worktree", "list", "--porcelain", cwd=bare).splitlines()[0] == f"worktree {bare.resolve()}"
    assert str(worktree) not in run("git", "worktree", "list", "--porcelain", cwd=bare)
    local = subprocess.run(["git", "-C", str(bare), "show-ref", "--verify", "--quiet", "refs/heads/feature/bare-cleanup"], check=False)
    remote = subprocess.run(["git", "-C", str(bare), "ls-remote", "--exit-code", "origin", "refs/heads/feature/bare-cleanup"], capture_output=True, check=False)
    assert local.returncode != 0
    assert remote.returncode == 2
    assert "branch.feature/bare-cleanup." not in run("git", "config", "--list", "--local", cwd=bare)
    assert run("git", "rev-parse", "refs/heads/main", cwd=bare) == stale_main
    assert reconcile_cleanup(component, intent)["already_succeeded"] is True


def test_bare_repository_cleanup_keeps_a_local_branch_that_moved_after_approval(tmp_path: Path):
    bare, worktree, component = _bare_backed_merged_worktree(tmp_path)
    intent = prepare_cleanup(component, confirm=True)
    run("git", "commit", "--allow-empty", "-m", "late unmerged work", cwd=worktree)
    late = run("git", "rev-parse", "HEAD", cwd=worktree)
    run("git", "worktree", "remove", str(worktree), cwd=bare)

    with pytest.raises(ReleaseError, match="local branch feature/bare-cleanup moved"):
        cleanup_component(component, confirm=True, intent=intent)
    assert run("git", "rev-parse", "refs/heads/feature/bare-cleanup", cwd=bare) == late
    remote = run("git", "ls-remote", "--exit-code", "origin", "refs/heads/feature/bare-cleanup", cwd=bare)
    assert remote.split()[0] == intent["head"]
    assert reconcile_cleanup(component, intent)["safe_to_retry"] is False


def test_resumed_bare_cleanup_removes_upstream_config_left_by_an_interrupted_branch_deletion(tmp_path: Path):
    bare, worktree, component = _bare_backed_merged_worktree(tmp_path)
    intent = prepare_cleanup(component, confirm=True)
    # Simulate a stop after `update-ref -d` and before the upstream section is removed.
    run("git", "worktree", "remove", str(worktree), cwd=bare)
    run("git", "update-ref", "-d", "refs/heads/feature/bare-cleanup", intent["head"], cwd=bare)
    assert "branch.feature/bare-cleanup.merge" in run("git", "config", "--list", "--local", cwd=bare)

    result = cleanup_component(component, confirm=True, intent=intent)

    assert result["cleaned"] is True
    assert "branch.feature/bare-cleanup." not in run("git", "config", "--list", "--local", cwd=bare)
    remote = subprocess.run(["git", "-C", str(bare), "ls-remote", "--exit-code", "origin", "refs/heads/feature/bare-cleanup"], capture_output=True, check=False)
    assert remote.returncode == 2
    assert reconcile_cleanup(component, intent)["already_succeeded"] is True


def test_cleanup_refuses_to_remove_the_bare_repository_itself(tmp_path: Path):
    bare, _, component = _bare_backed_merged_worktree(tmp_path)
    run("git", "symbolic-ref", "HEAD", "refs/heads/feature/bare-cleanup", cwd=bare)
    component = {**component, "repository": str(bare)}

    with pytest.raises(ReleaseError, match="refusing to remove the primary checkout or bare repository"):
        prepare_cleanup(component, confirm=True)
    assert run("git", "rev-parse", "--is-bare-repository", cwd=bare) == "true"


def test_unsupported_engine_fails_during_planning(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo, category="engine")
    with pytest.raises(UnsupportedAdapter, match="deployment_recipe"):
        plan_manifest(value)


def test_required_migrations_are_rejected_before_planning_effects(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo)
    value["migration"] = {"required": True, "reviewed": True}
    with pytest.raises(UnsupportedAdapter, match="required migrations"):
        plan_manifest(value)


def test_selected_package_adoption_without_receipt_recipe_is_unsupported(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo, category="role")
    value["adoption"] = [{"component": "component", "agents": ["fixture-agent"]}]
    with pytest.raises(UnsupportedAdapter, match="adoption_recipe"):
        plan_manifest(value)


def test_dirty_source_and_system_history_target_are_rejected(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo)
    value["components"][0]["target_branch"] = "qorqut-tables"
    with pytest.raises(ReleaseError, match="protected/non-task ref"):
        plan_manifest(value)
    value["components"][0]["target_branch"] = "main"
    planned = plan_manifest(value)
    (repo / "unrelated").write_text("do not sweep\n")
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")
    with pytest.raises(ReleaseError, match="dirty"):
        worker.check()


def test_record_is_redacted_hash_bound_and_plan_is_immutable(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    planned = plan_manifest(manifest(repo))
    store = RecordStore(tmp_path / "records", planned["release_id"])
    store.create(planned)
    store.append("command", record_patch={"observations": [{"token": "do-not-store"}]}, output="password=secret-value")
    loaded = store.load()
    assert loaded["observations"][0]["token"] == "<redacted>"
    assert "secret-value" not in store.events_path.read_text()
    original = store.plan_path.read_text()
    with pytest.raises(ReleaseError, match="different digest"):
        store.create({**planned, "plan_digest": "sha256:" + "0" * 64})
    assert store.plan_path.read_text() == original


def test_approval_must_bind_plan_and_confirmation_does_not_create_it(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    planned = plan_manifest(manifest(repo))
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")
    worker.initialize()
    with pytest.raises(ReleaseError, match="recorded merge authorization"):
        worker.merge(confirm=True)
    evidence = tmp_path / "approval.json"
    evidence.write_text(json.dumps({"scope": "merge", "plan_digest": planned["plan_digest"], "evidence": "owner task 1"}))
    worker.authorize("merge", evidence)
    assert worker.status()["approvals"]["merge"]["plan_digest"] == planned["plan_digest"]


def test_merge_requires_exact_check_and_build_evidence(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    planned = plan_manifest(manifest(repo))
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")
    worker.initialize()
    evidence = tmp_path / "approval.json"
    evidence.write_text(json.dumps({"scope": "merge", "plan_digest": planned["plan_digest"], "evidence": "fixture owner instruction"}))
    worker.authorize("merge", evidence)
    with pytest.raises(ReleaseError, match="check evidence"):
        worker.merge(confirm=True)


def test_installation_override_must_match_frozen_digest(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    planned = plan_manifest(manifest(repo))
    changed = dict(planned["installation"], service="another.service")
    with pytest.raises(ReleaseError, match="does not match the frozen plan"):
        ReleaseWorker(planned, records_root=tmp_path / "records", installation_config=changed)


def test_engine_destination_uses_component_host_not_installation_host(tmp_path: Path):
    component = {
        "id": "engine",
        "category": "engine",
        "deployment_recipe": {"host": "engine-host"},
        "recovery_recipe": {"command": ["recover"]},
    }
    selected = executor_for_component(component, {"host": "dashboard-host"})
    assert isinstance(selected, SSHExecutor)
    assert selected.host == "engine-host"
    recovery_selected = executor_for_component(component, {"host": "dashboard-host"}, action="recover")
    assert isinstance(recovery_selected, SSHExecutor)
    assert recovery_selected.host == "engine-host"


def test_worker_locks_each_component_destination_in_deterministic_order(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo)
    value["installation"].update(
        host="dashboard-host",
        release_root=str(tmp_path / "dashboard-releases"),
    )
    value["components"][0]["deployment_recipe"] = {
        "host": "engine-host",
        "release_root": str(tmp_path / "engine-releases"),
    }
    planned = plan_manifest(value)
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")

    locks = worker._destination_locks({"deploy"})
    keys = list(locks.locks)
    engine_lock = locks.for_component("component", "deploy")

    assert keys == sorted(keys)
    assert len(locks.locks) == 2
    assert getattr(engine_lock.executor, "host") == "engine-host"
    assert engine_lock.remote_path == str(tmp_path / "engine-releases" / ".release-worker" / "fixture.lock")
    assert engine_lock is not locks.primary


def test_adoption_destination_uses_adoption_recipe_host(tmp_path: Path):
    component = {"id": "role", "category": "role", "adoption_recipe": {"host": "package-host"}}
    selected = executor_for_component(component, {"host": "dashboard-host"}, action="adopt")
    assert isinstance(selected, SSHExecutor)
    assert selected.host == "package-host"


def test_cli_rejects_conflicting_plan_without_overwriting_original(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo, _ = git_repo(tmp_path)
    manifest_path = tmp_path / "release.json"
    value = manifest(repo)
    manifest_path.write_text(json.dumps(value))
    records = tmp_path / "records"
    assert cli_main(["plan", "--manifest", str(manifest_path), "--records-root", str(records)]) == 0
    plan_path = records / "test-release" / "plan.json"
    original = plan_path.read_bytes()
    value["components"][0]["source_ref"] = "main"
    manifest_path.write_text(json.dumps(value))
    assert cli_main(["plan", "--manifest", str(manifest_path), "--records-root", str(records)]) == 2
    assert plan_path.read_bytes() == original
    capsys.readouterr()


def test_interrupted_step_requires_action_specific_reconciliation(tmp_path: Path):
    repo, _ = git_repo(tmp_path)
    planned = plan_manifest(manifest(repo))
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")
    worker.initialize()
    worker.store.step_started("stage:component", before={"effect": "none"})
    with pytest.raises(ReconciliationRequired, match="cannot inspect"):
        worker.resume()


def test_installation_lock_prevents_competing_mutation(tmp_path: Path):
    first = InstallationLock(tmp_path / "records", "fixture")
    second = InstallationLock(tmp_path / "records", "fixture")
    with first:
        with pytest.raises(ReleaseError, match="already running"):
            second.__enter__()


def test_coordinated_failure_reconciles_before_safe_resume(tmp_path: Path, monkeypatch):
    repo, _ = git_repo(tmp_path)
    value = manifest(repo, release_id="coordinated-fixture")
    value["components"].append(
        {
            "id": "second",
            "category": "documentation",
            "repository": str(repo),
            "source_ref": "feature/release",
            "target_branch": "main",
            "dependencies": ["component"],
        }
    )
    planned = plan_manifest(value)

    class Flaky(Adapter):
        name = "documentation"
        categories = frozenset({"documentation"})
        runtime = True
        calls = 0

        def deploy(self, component, context):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("fixture effect returned no receipt")
            return {"deployed": True, "component": component["id"]}

        def reconcile(self, component, context, action, before):
            return {"safe_to_retry": True, "observed": "fixture remained on previous version"}

    monkeypatch.setitem(ADAPTER_TYPES, "documentation", Flaky)
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")
    worker.initialize()
    for item in planned["components"]:
        before = {"source_commit": item["source_commit"], "plan_digest": planned["plan_digest"]}
        for action in ("check", "build", "stage", "merge"):
            step_id = f"{action}:{item['id']}"
            worker.store.step_started(step_id, before=before)
            worker.store.step_finished(step_id, after={"fixture": True})
    evidence = tmp_path / "deploy-approval.json"
    evidence.write_text(json.dumps({"scope": "deploy", "plan_digest": planned["plan_digest"], "evidence": "fixture owner instruction"}))
    worker.authorize("deploy", evidence)
    with pytest.raises(RuntimeError, match="no receipt"):
        worker.deploy(confirm=True)
    assert worker.status()["steps"]["deploy:component"]["after"]["external_effect"] == "unknown"
    assert worker.resume()["steps"]["deploy:component"]["status"] == "reconciled"
    result = worker.deploy(confirm=True)
    assert result["results"]["component"]["deployed"] is True
    assert result["results"]["second"]["deployed"] is True
    assert Flaky.calls == 3


def test_library_uses_source_lifecycle_without_runtime_or_adoption(tmp_path):
    from bazarlyk.release_worker.adapters import adapter_for
    repo, _ = git_repo(tmp_path)
    value = manifest(repo, category="library")
    value["components"][0]["tests"] = []
    plan = plan_manifest(value)
    worker = ReleaseWorker(plan, records_root=tmp_path / "records")
    worker.initialize()
    worker.check()
    worker.build()
    worker.stage()
    adapter = adapter_for(plan["components"][0])
    assert adapter.runtime is False
    assert adapter.deploy(plan["components"][0], worker._context())["deployed"] is False
    assert adapter.adopt(plan["components"][0], worker._context())["adopted"] is False
    assert (worker.artifact_root / "component" / "change").read_text() == "feature\n"
