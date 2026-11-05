from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile

import pytest

from bazarlyk.bootstrap.preparation import prepare_claude_agent
from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.storage import Database
from bazarlyk.storage.exports import build_exports, publish_exports
from bazarlyk.storage.recovery import BackupStore
from bazarlyk.storage.receipts import ApplyReceipts
from bazarlyk.storage.versioning import HISTORY_REF, NightlyMaintenance, public_path
from foundation_support import BUSINESS, HUMAN, context, task_plan
from test_agent_configuration import Fixture
from test_domain_documents import invoke, setup_documents


def configured(tmp_path):
    root = tmp_path / "businesses"
    db, app, domains = setup_documents(root)
    task = invoke(app, "tasks_create", {"title": "Retain customer evidence"})
    assert task.status == "ok", task.to_dict()
    document = invoke(app, "notes_create", {"title": "Evidence", "body": "Exact authored wording."}).data["document"]
    (root / "first/hq/.env").write_text("EXAMPLE_KEY=private-example-value\n")
    (root / "first/code").mkdir()
    (root / "first/code/not-owned.txt").write_text("Do not capture the business code repository")
    return root, db, app, document


def test_exports_are_deterministic_partitioned_and_exclude_private_records(tmp_path):
    root, db, _, _ = configured(tmp_path)
    snapshot = root / ".snapshot.sqlite3"
    db.backup(snapshot)
    first = build_exports(snapshot, root)
    assert first == build_exports(snapshot, root)
    task = json.loads(first.files["first/hq/exports/tasks.json"])
    assert task["generated"] and task["authority"] == "sqlite" and task["scope"] == BUSINESS
    assert task["records"][0]["title"] == "Retain customer evidence"
    assert json.loads(first.files["second/hq/exports/tasks.json"])["records"] == []
    for forbidden in ("credentials", "sessions", "signoffs", "receipts", "jobs", "private-example-value"):
        assert not any(forbidden in name for name in first.files)
        assert forbidden.encode() not in b"".join(first.files.values())
    assert publish_exports(first, root)["changed"]
    assert publish_exports(first, root)["changed"] == []


def test_backup_restores_exact_data_and_files_but_both_authority_gates_stay_closed(tmp_path):
    root, db, _, document = configured(tmp_path)
    plan = task_plan(context(), {"id": "recoverable", "title": "Pending exact application"})
    with db.transaction() as uow:
        pending = ApplyReceipts(uow).create(plan)
    store, identity = BackupStore(db, root), new_id()
    metadata = store.create(identity)
    assert store.verify(identity) == metadata
    assert store.create(identity) == metadata
    archive = store.destination / identity / "authored/sources.tar"
    with tarfile.open(archive) as saved:
        names = saved.getnames()
        assert "first/hq/.env" in names
        assert "first/code/not-owned.txt" not in names
        assert all("exports/" not in name for name in names)
    destination = tmp_path / "restored"
    result = store.restore(identity, destination)
    assert result["state"] == "closed" and result["activation_root"] == str(root)
    restored = Database(result["database"]).initialize()
    with restored.read() as uow:
        assert uow.repository("maintenance_state").get("service")["state"] == "closed"
        assert uow.repository("businesses").get(BUSINESS)["hq_path"] == str(root / "first/hq")
        receipt = ApplyReceipts(uow).get(pending["id"])
        assert receipt["change_set"] == plan.canonical and receipt["state"] == "prepared"
    assert json.loads((destination / ".bazarlyk/authority.json").read_text())["state"] == "maintenance"
    assert (destination / "first/hq" / document["path"]).read_bytes() == (root / "first/hq" / document["path"]).read_bytes()
    assert (destination / "first/hq/.env").read_text() == "EXAMPLE_KEY=private-example-value\n"
    assert (store.destination / identity).stat().st_mode & 0o077 == 0
    assert archive.stat().st_mode & 0o077 == 0
    with pytest.raises(DomainError, match="empty"):
        store.restore(identity, destination)


def test_backup_captures_cost_sidecar_separately_and_graph_restore_keeps_live_costs(tmp_path):
    root = tmp_path / "businesses"
    root.mkdir()
    db = Database.for_root(root).initialize()
    data_dir = root / ".contextbase"
    data_dir.mkdir(mode=0o700)
    graph = data_dir / "state.sqlite3"
    with sqlite3.connect(graph) as connection:
        connection.execute("CREATE TABLE graph_state (value TEXT)")
        connection.execute("INSERT INTO graph_state VALUES ('graph')")
    costs = data_dir / "costs.sqlite3"
    cost_connection = sqlite3.connect(costs)
    cost_connection.execute("PRAGMA journal_mode=WAL")
    cost_connection.execute("CREATE TABLE cost_events (event_id TEXT PRIMARY KEY, known_usd TEXT)")
    cost_connection.execute("INSERT INTO cost_events VALUES ('incurred-before-backup', '0.00000003')")
    cost_connection.commit()

    store, identity = BackupStore(db, root), new_id()
    store.context_base_path = graph
    metadata = store.create(identity)
    assert metadata["context_costs"]["checksum"]
    saved_costs = store.destination / identity / "costs.sqlite3"
    assert saved_costs.stat().st_mode & 0o077 == 0
    assert store.verify(identity)["context_costs"] == metadata["context_costs"]
    with sqlite3.connect(saved_costs) as connection:
        assert connection.execute("SELECT event_id FROM cost_events").fetchall() == [("incurred-before-backup",)]

    graph.unlink()
    graphless_identity = new_id()
    graphless = store.create(graphless_identity)
    assert "context_base" not in graphless and "context_costs" in graphless

    # A later graph recovery must not touch the independently growing live ledger.
    cost_connection.execute("INSERT INTO cost_events VALUES ('incurred-after-backup', '0.00000004')")
    cost_connection.commit()
    destination = tmp_path / "cost-ledger-rehearsal"
    store.restore(identity, destination)
    assert cost_connection.execute("SELECT COUNT(*) FROM cost_events").fetchone()[0] == 2
    with sqlite3.connect(destination / "costs.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM cost_events").fetchone()[0] == 1

    cost_connection.close()
    costs.unlink()
    missing_identity = new_id()
    missing = store.create(missing_identity)
    assert "context_costs" not in missing
    assert not (store.destination / missing_identity / "costs.sqlite3").exists()
    costs.write_text("not a SQLite database")
    broken_identity = new_id()
    with pytest.raises(DomainError, match="cost sidecar"):
        store.create(broken_identity)
    assert not (store.destination / broken_identity).exists()


def test_required_catalog_and_bootstrap_bytes_and_credentials_are_private_backup_inputs(tmp_path):
    fixture = Fixture(tmp_path)
    principal = fixture.create()
    prepare_claude_agent(fixture.resolver, principal, endpoint="http://127.0.0.1:19991/mcp", claude_version="2.1.263")
    store, identity = BackupStore(fixture.db, fixture.root), new_id()
    store.create(identity)
    with tarfile.open(store.destination / identity / "authored/sources.tar") as archive:
        names = archive.getnames()
        assert any(name.startswith(".bazarlyk/artifacts/roles/") and name.endswith("role.yaml") for name in names)
        assert any(name.endswith("/.bazarlyk/credential") for name in names)
        assert any(name.endswith("/.bazarlyk/registration.json") for name in names)
        assert any("/.claude/skills/" in name for name in names)
    with fixture.db.read() as uow:
        registration = uow.repository("machine_registrations").list()[0]
    (Path(registration["workspace"]) / ".claude/settings.json").write_text("{}")
    with pytest.raises(DomainError, match="bootstrap"):
        store.create(new_id())


def test_root_and_hq_retirement_archives_are_private_complete_and_restorable(tmp_path):
    root, db, _, _ = configured(tmp_path)
    migration = new_id()
    archived = {
        f".bazarlyk/legacy/{migration}/routines/legacy.yaml": b"private root archive marker\n",
        f"first/hq/.bazarlyk/legacy/{migration}/agents/retired/PLAN.md": b"private archived plan marker\n",
        f"first/hq/.bazarlyk/legacy/{migration}/agents/retired/.bazarlyk/credential": b"private archived credential marker\n",
    }
    for relative, body in archived.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    store, identity = BackupStore(db, root), new_id()
    store.create(identity)
    with tarfile.open(store.destination / identity / "authored/sources.tar") as archive:
        for relative, body in archived.items():
            assert archive.extractfile(relative).read() == body
            assert not public_path(relative, [{"slug": "first", "hq": "first/hq"}])
    bundle = build_exports(store.destination / identity / "state.sqlite3", root)
    assert not any(".bazarlyk/legacy/" in name for name in bundle.files)
    assert all(body not in b"".join(bundle.files.values()) for body in archived.values())
    destination = tmp_path / "restored"
    assert store.restore(identity, destination)["state"] == "closed"
    assert all((destination / relative).read_bytes() == body for relative, body in archived.items())


@pytest.mark.parametrize("damage", ["changed", "missing", "link"])
def test_file_drift_never_publishes_a_complete_generation(tmp_path, damage):
    root, db, _, document = configured(tmp_path)
    store, first, failed = BackupStore(db, root), new_id(), new_id()
    store.create(first)
    target = root / "first/hq" / document["path"]
    if damage == "changed":
        target.write_text("Concurrent unsynchronized edit")
    else:
        target.unlink()
        if damage == "link":
            target.symlink_to(root / "first/hq/.env")
    with pytest.raises(DomainError):
        store.create(failed)
    assert not (store.destination / failed).exists()
    assert not list(store.destination.glob(".incomplete-*"))
    assert store.verify(first)["state"] == "complete"
    with db.read() as uow:
        assert uow.repository("backup_runs").get(failed)["state"] == "failed"


def test_claimed_file_work_blocks_backups_and_archive_tampering_blocks_restore(tmp_path):
    root, db, _, _ = configured(tmp_path)
    store, identity = BackupStore(db, root), new_id()
    store.create(identity)
    with db.transaction() as uow:
        receipts = ApplyReceipts(uow)
        pending = receipts.create(task_plan(context(), {"id": "claimed"}))
        receipts.claim_resources(pending["id"])
    with pytest.raises(DomainError, match="claimed"):
        store.create(new_id())
    archive = store.destination / identity / "authored/sources.tar"
    with archive.open("r+b") as stream:
        stream.seek(520)
        stream.write(b"tamper")
    with pytest.raises(DomainError):
        store.restore(identity, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_retention_keeps_latest_seven_days_and_four_distinct_weeks(tmp_path):
    root, db, _, _ = configured(tmp_path)
    date = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
    now = [date]
    store = BackupStore(db, root, clock=lambda: now[0].isoformat())
    ids = {}
    for offset in (35, 28, 21, 14, 7, 6, 5, 4, 3, 2, 1, 0):
        now[0] = date - timedelta(days=offset)
        ids[offset] = new_id()
        store.create(ids[offset])
    assert set(store.prune()) == {ids[index] for index in (35, 28, 7)}
    assert {path.name for path in store.destination.iterdir()} == {identity for offset, identity in ids.items() if offset not in (35, 28, 7)}


class Control:
    def __init__(self, identity=None):
        self.job = {"id": identity or new_id()}
        self.business_id = None
        self.cancelled = False

    def check(self):
        if self.cancelled:
            raise DomainError("cancelled", "Stopped by owner")

    def remaining_seconds(self):
        return 1800


class RecordingGit:
    """Only the external Git boundary is recorded; storage and recovery stay real."""
    def __init__(self, root):
        self.root, self.calls, self.history = root, [], None
        self.crash_after_ref = False
        self.checked_out_history = False

    def __call__(self, command, **kwargs):
        arguments = command[command.index("-C") + 2:]
        self.calls.append((arguments, {**kwargs, "env": dict(kwargs["env"])}))
        output, code = b"", 0
        if arguments == ["rev-parse", "--show-toplevel"]:
            output = str(self.root).encode()
        elif arguments[0] == "worktree" and self.checked_out_history:
            output = ("worktree /elsewhere\0branch " + HISTORY_REF + "\0\0").encode()
        elif arguments == ["rev-parse", "--verify", HISTORY_REF]:
            output, code = (self.history.encode(), 0) if self.history else (b"", 128)
        elif arguments == ["rev-parse", "--verify", "HEAD"]:
            output = b"a" * 40
        elif arguments[0] == "rev-parse":
            output = b"b" * 40
        elif arguments[0] == "ls-tree":
            output = b"unrelated.txt\0first/code/source.py\0first/hq/notes/deleted.md\0"
        elif arguments[0] == "hash-object":
            output = hashlib.sha1(kwargs["input"]).hexdigest().encode()
        elif arguments[0] == "write-tree":
            output = b"c" * 40
        elif arguments[0] == "commit-tree":
            output = b"d" * 40
        elif arguments[0] == "update-ref":
            assert arguments[1] == HISTORY_REF
            self.history = arguments[2]
            if self.crash_after_ref:
                self.crash_after_ref = False
                raise SystemExit("simulated process exit after ref update")
        return subprocess.CompletedProcess(command, code, stdout=output, stderr=b"")


def test_nightly_without_git_is_idempotent_and_does_not_create_a_second_backup(tmp_path):
    root, db, _, _ = configured(tmp_path)
    runner = RecordingGit(root)
    maintenance = NightlyMaintenance(db, root, git_runner=runner)
    control = Control()
    first = maintenance.run(control, {})
    assert first.outputs == () and first.data["versioning"] == {"enabled": False}
    assert maintenance.run(control, {}).data == first.data
    assert runner.calls == []
    assert len(list(maintenance.backups.destination.iterdir())) == 1
    with pytest.raises(DomainError):
        maintenance.run(control, {"command": "anything"})


def test_nightly_export_recovery_uses_the_saved_snapshot_not_newer_live_data(tmp_path, monkeypatch):
    root, db, app, _ = configured(tmp_path)
    maintenance, control = NightlyMaintenance(db, root), Control()
    from bazarlyk.storage import versioning
    original = versioning.publish_exports

    def interrupted(bundle, root):
        original(bundle, root)
        raise SystemExit("interrupt after exports")

    monkeypatch.setattr(versioning, "publish_exports", interrupted)
    with pytest.raises(SystemExit):
        maintenance.run(control, {})
    assert invoke(app, "tasks_create", {"title": "Created after the snapshot"}).status == "ok"
    monkeypatch.setattr(versioning, "publish_exports", original)
    maintenance.run(control, {})
    tasks = json.loads((root / "first/hq/exports/tasks.json").read_text())["records"]
    assert [task["title"] for task in tasks] == ["Retain customer evidence"]


def test_git_boundary_preserves_unrelated_index_and_recovers_ref_update_without_duplicate_commit(tmp_path):
    root, db, _, _ = configured(tmp_path)
    (root / ".git").mkdir()
    index = root / ".git/index"
    index.write_bytes(b"unrelated user staging")
    runner = RecordingGit(root)
    runner.crash_after_ref = True
    maintenance, control = NightlyMaintenance(db, root, git_enabled=True, git_runner=runner), Control()
    with pytest.raises(SystemExit):
        maintenance.run(control, {})
    result = maintenance.run(control, {})
    assert result.data["versioning"]["ref"] == HISTORY_REF
    assert index.read_bytes() == b"unrelated user staging"
    arguments = [row[0] for row in runner.calls]
    assert sum(row[0] == "commit-tree" for row in arguments) == 1
    assert sum(row[0] == "update-ref" for row in arguments) == 1
    assert all(row[0] not in {"push", "commit", "checkout", "reset", "init"} for row in arguments)
    changed = [row[-1] for row in arguments if row[0] == "update-index"]
    assert "first/hq/notes/deleted.md" in changed
    assert "unrelated.txt" not in changed and "first/code/source.py" not in changed
    assert not any(".env" in path or ".bazarlyk" in path or "sessions" in path for path in changed)
    for command, options in runner.calls:
        if command[0] in {"update-index", "write-tree", "read-tree"}:
            assert options["env"]["GIT_INDEX_FILE"] != str(index)


def test_changed_recovery_configuration_cannot_enable_new_git_effects(tmp_path, monkeypatch):
    root, db, _, _ = configured(tmp_path)
    first, control = NightlyMaintenance(db, root), Control()
    monkeypatch.setattr(first.backups, "create", lambda *args, **kwargs: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        first.run(control, {})
    runner = RecordingGit(root)
    with pytest.raises(DomainError, match="original configuration"):
        NightlyMaintenance(db, root, git_enabled=True, git_runner=runner).run(control, {})
    assert runner.calls == []


def test_versioning_does_not_advance_a_checked_out_history_branch(tmp_path):
    root, db, _, _ = configured(tmp_path)
    runner = RecordingGit(root)
    runner.checked_out_history = True
    maintenance = NightlyMaintenance(db, root, git_enabled=True, git_runner=runner)
    with pytest.raises(DomainError, match="checked out"):
        maintenance.run(Control(), {})
    assert all(command[0] not in {"commit-tree", "update-ref"} for command, _ in runner.calls)
    assert len(list(maintenance.backups.destination.iterdir())) == 1


def test_real_git_versions_ignored_business_data_without_touching_head_or_staging(tmp_path):
    root, db, _, document = configured(tmp_path)
    assert root.is_relative_to(tmp_path) and root != Path("/absolute/path/to/businesses")
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0")

    def git(*arguments):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *arguments],
            env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20).stdout

    # This disposable fixture is the only place this test initializes a repository.
    git("init", "--initial-branch=fixture-main")
    git("config", "--local", "user.name", "Disposable Fixture")
    git("config", "--local", "user.email", "fixture@localhost")
    git("config", "--local", "commit.gpgSign", "false")
    (root / ".gitignore").write_text("/*/\n!/context/\n!/learnings/\n!/routines/\n.env\n.env.*\n")
    (root / "README.md").write_text("Initial shared instructions\n")
    unrelated = root / "unrelated.txt"
    unrelated.write_text("Previously committed unrelated work\n")
    git("add", "--", ".gitignore", "README.md", "unrelated.txt")
    git("commit", "--no-verify", "-m", "Disposable initial history")

    unrelated.write_text("Unrelated staged bytes\n")
    (root / "unrelated-new.txt").write_text("New unrelated staged file\n")
    git("add", "--", "unrelated.txt", "unrelated-new.txt")
    unrelated.write_text("Unrelated unstaged bytes\n")
    (root / "README.md").write_text("Current authored shared instructions\n")
    head, branch = git("rev-parse", "HEAD"), git("symbolic-ref", "HEAD")
    index_path, head_path = root / ".git/index", root / ".git/HEAD"
    index_bytes, head_bytes = index_path.read_bytes(), head_path.read_bytes()
    staged = git("show", ":unrelated.txt"), git("show", ":unrelated-new.txt")
    authored_path = "first/hq/" + document["path"]
    git("check-ignore", "--quiet", "--", authored_path)
    git("check-ignore", "--quiet", "--", "first/hq/exports/tasks.json")
    git("check-ignore", "--quiet", "--", "exports/businesses.json")
    assert git("remote") == b""

    maintenance, control = NightlyMaintenance(db, root, git_enabled=True), Control()
    result = maintenance.run(control, {})
    history = git("rev-parse", HISTORY_REF)
    assert result.data["versioning"]["commit"] == history.decode().strip()
    assert git("rev-list", "--count", HISTORY_REF, "^" + head.decode().strip()) == b"1\n"
    assert git("show", HISTORY_REF + ":" + authored_path) == (root / authored_path).read_bytes()
    tasks = json.loads(git("show", HISTORY_REF + ":first/hq/exports/tasks.json"))
    assert tasks["generated"] and tasks["authority"] == "sqlite"
    assert tasks["records"][0]["title"] == "Retain customer evidence"
    assert json.loads(git("show", HISTORY_REF + ":exports/manifest.json"))["generated"]
    assert git("show", HISTORY_REF + ":README.md") == b"Current authored shared instructions\n"
    assert git("show", HISTORY_REF + ":unrelated.txt") == b"Previously committed unrelated work\n"
    names = git("ls-tree", "-r", "--name-only", "-z", HISTORY_REF).decode().split("\0")
    assert not any(name.startswith(".bazarlyk/") or ".env" in name or "/code/" in name for name in names)
    assert "unrelated-new.txt" not in names and "state.sqlite3" not in names

    assert maintenance.run(control, {}).data == result.data
    unchanged = maintenance.run(Control(), {})
    assert unchanged.data["versioning"]["changed"] is False
    assert git("rev-parse", HISTORY_REF) == history
    assert git("rev-list", "--count", HISTORY_REF, "^" + head.decode().strip()) == b"1\n"
    assert git("rev-parse", "HEAD") == head and git("symbolic-ref", "HEAD") == branch
    assert head_path.read_bytes() == head_bytes and index_path.read_bytes() == index_bytes
    assert (git("show", ":unrelated.txt"), git("show", ":unrelated-new.txt")) == staged
    assert unrelated.read_text() == "Unrelated unstaged bytes\n"
    assert (root / "unrelated-new.txt").read_text() == "New unrelated staged file\n"
    assert git("remote") == b""


def test_public_paths_exclude_private_runtime_code_and_cache_artifacts():
    business = [{"slug": "first", "hq": "first/hq"}]
    for name in (".bazarlyk/state.sqlite3", "first/code/main.py", "first/hq/.env", "first/hq/.env.backup",
                 "first/hq/agents/business-agent/.bazarlyk/credential", "first/hq/agents/business-agent/sessions/one.jsonl",
                 "first/hq/agents/business-agent/work/main.py", "first/hq/context/email/mailbox/thread/message.md",
                 "routines/logs/one.log", "unrelated.txt"):
        assert not public_path(name, business), name
    for name in ("files/instructions/agent-foundation.md", "first/hq/context/product.md", "first/hq/exports/tasks.json",
                 "first/hq/agents/business-agent/notes/one.md", "first/hq/agents/business-agent/.claude/skills/bazarlyk-work/SKILL.md"):
        assert public_path(name, business), name
