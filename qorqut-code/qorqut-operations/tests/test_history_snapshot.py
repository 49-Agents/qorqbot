"""Focused synthetic checks for the operational-history snapshot engine, with no host and no Context.

Each test builds a temporary explicit instance, a real Operations store with synthetic registrations and rows, the location
and area adapters a host would supply, and its own private history repository. No real data, provider, source-repository
ref or system history ref is touched.
"""
import json
import os
import subprocess

import pytest

from history_snapshot_support import (ACME_AGENT, ALLOWED_GIT, BETA_AGENT, GIT_ENV, GLOBAL_AGENT, REF, SECRET, Layout, blob,
                                      build, commits, domain_contribution, everything, failure, git, history_over,
                                      registered_areas, rest_classified, tracked_reads, tree, write)
from qorqut_operations.contracts import DomainError
from qorqut_operations.history_exports import (BusinessHeadquarters, ExportCatalog, GlobalAgentFiles, GlobalFiles, History,
                                               _capture, _state)


@pytest.fixture
def env(tmp_path):
    context = build(tmp_path)
    yield context
    context.database.close()


@pytest.fixture
def complete(tmp_path):
    """The same instance with every actual table classified, so ``complete`` can be true."""
    context = build(tmp_path)
    context.catalog = ExportCatalog(rest_classified(context.database))
    context.history = context.another()
    yield context
    context.database.close()


def test_construction_is_inert_and_initialization_is_explicit_private_and_local(env):
    assert not env.runtime.joinpath("history.git").exists() and not any(env.runtime.glob("history*"))
    status = env.history.status()
    assert status["state"] == "uninitialized" and status["initialized"] is False
    for operation in (env.history.snapshot, env.history.validate):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code == "history_uninitialized"
    assert not env.repository.exists()

    report = env.history.initialize()
    assert report["created"] is True and report["state"] == "ready" and report["commit"] is None
    assert report["repository"] == str(env.layout.history) == str(env.repository)
    assert env.repository.stat().st_mode & 0o777 == 0o700
    assert git(env, "rev-parse", "--is-bare-repository").strip() == b"true"
    assert git(env, "symbolic-ref", "HEAD").strip().decode() == REF
    assert not git(env, "remote").strip() and not (env.repository / "hooks").exists()
    assert env.history.initialize()["created"] is False
    assert not [path for path in env.runtime.iterdir() if path.name.startswith(".history-init-")]

    before = sorted(path.name for path in env.runtime.iterdir())
    validated = env.history.validate()
    assert validated["valid"] is True and sorted(path.name for path in env.runtime.iterdir()) == before


def test_initialization_refuses_incompatible_locations_untouched(env, tmp_path):
    env.history.initialize()
    git(env, "remote", "add", "origin", "https://example.invalid/never-contacted.git")
    for operation in (env.history.initialize, env.history.validate, env.history.snapshot):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code == "history_incompatible"
    assert env.history.status()["state"] == "failed"
    git(env, "remote", "remove", "origin")
    assert env.history.snapshot()["changed"] is True

    for kind in ("directory", "plain_repository", "file", "symlink"):
        other = build(tmp_path / kind, seed=False)
        target = other.repository
        if kind == "directory":
            target.mkdir()
            (target / "precious.txt").write_text("keep")
        elif kind == "plain_repository":
            subprocess.run(["git", "init", "--bare", "-q", str(target)], check=True, env=GIT_ENV)
        elif kind == "file":
            target.write_text("not a repository")
        else:
            (tmp_path / "elsewhere").mkdir()
            target.symlink_to(tmp_path / "elsewhere")
        with pytest.raises(DomainError) as error:
            other.history.initialize()
        assert error.value.code == ("history_unavailable" if kind == "symlink" else "history_incompatible")
        assert os.path.lexists(target) and (kind != "directory" or (target / "precious.txt").read_text() == "keep")
        other.database.close()


def test_construction_never_touches_the_database_and_a_missing_operations_root_is_unavailable(env, tmp_path):
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError("construction must not touch the database")

    history_over(Exploding(), env.layout, catalog=env.catalog, areas=registered_areas, clock=env.clock)
    nowhere = Layout(tmp_path / "nowhere")
    absent = history_over(env.database, nowhere, catalog=env.catalog, areas=registered_areas, clock=env.clock)
    for operation in (absent.initialize, absent.snapshot, absent.validate):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code == "history_unavailable"
    assert not (tmp_path / "nowhere").exists()
    assert absent.status()["state"] == "unavailable"


def test_replaced_runtime_root_is_refused_not_followed_before_or_during_an_operation(env, tmp_path):
    env.history.initialize()
    first = env.history.snapshot()

    # Replaced before the operation: the host's path check refuses the linked location and nothing follows it.
    moved = tmp_path / "moved-runtime"
    decoy = tmp_path / "decoy-runtime"
    decoy.mkdir()
    env.runtime.rename(moved)
    env.runtime.symlink_to(decoy)
    for operation in (env.history.snapshot, env.history.validate, env.history.initialize):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code in {"history_unavailable", "history_incompatible"}
    assert not list(decoy.iterdir()) and env.history.status()["state"] == "unavailable"
    env.runtime.unlink()
    moved.rename(env.runtime)
    assert env.history.snapshot()["changed"] is False

    # Replaced DURING a snapshot, after every path was validated: the per-command guard refuses the swapped location
    # even though the link points at the real repository (a realpath() approach would have followed it silently).
    write(env, "files/notes/plan.md", "changed so a commit is needed")

    def swap(name, command, kwargs):
        if name == "ls-tree" and not swap.done:
            swap.done = True
            env.runtime.rename(moved)
            env.runtime.symlink_to(moved)
    swap.done = False
    env.runner.intercepts.append(swap)
    with pytest.raises(DomainError) as error:
        env.history.snapshot()
    env.runner.intercepts.clear()
    assert error.value.code == "history_incompatible"
    env.runtime.unlink()
    moved.rename(env.runtime)
    assert git(env, "rev-parse", REF).decode().strip() == first["commit"] and len(commits(env)) == 1
    assert env.history.snapshot()["changed"] is True


def test_status_never_raises_on_git_trouble_and_reports_the_unobservable_ref(env):
    env.history.initialize()
    first = env.history.snapshot()

    def missing_git(name, command, kwargs):
        raise FileNotFoundError("git")
    env.runner.intercepts.append(missing_git)
    status = env.history.status()
    env.runner.intercepts.clear()
    assert status["initialized"] is True and status["commit"] is None
    assert status["observation"]["ref_error"] == "history_git_failed" and status["observation"]["consistent"] is False
    assert status["last_attempt"]["commit"] == first["commit"]
    assert env.history.status()["observation"]["consistent"] is True


def test_first_snapshot_versions_exact_rows_and_registered_global_business_and_agent_areas_only(env):
    state = {}
    included = {
        "files/notes/plan.md": "global note", "files/assets/logo.bin": b"\x00\x01\xff",
        f"agents/{GLOBAL_AGENT}/files/research/finding.md": "global agent research",
        f"projects/acme/agents/{ACME_AGENT}/files/notes/lead.md": "acme agent note",
        f"projects/beta/agents/{BETA_AGENT}/files/data/table.csv": "a,b\n1,2\n",
        "projects/acme/hq/strategy.md": "acme hq", "projects/beta/hq/notes/offer.md": "beta hq",
    }
    outside = {
        "context/published.md": "shared context", f"agents/{GLOBAL_AGENT}/transcripts/session.jsonl": "{}",
        f"agents/{GLOBAL_AGENT}/CLAUDE.md": "generated instructions", f"agents/{GLOBAL_AGENT}/notes.md": "agent root note",
        f"projects/acme/agents/{ACME_AGENT}/transcripts/session.jsonl": "{}",
        f"projects/acme/agents/{ACME_AGENT}/.claude/settings.json": "provider configuration",
        f"projects/acme/agents/{ACME_AGENT}/CLAUDE.md": "generated", "projects/acme/code/app.py": "business code",
        "projects/ghost/hq/unregistered.md": "x", f"projects/beta/agents/{ACME_AGENT}/files/wrong-business.md": "x",
        "agents/ffffffff-0000-4000-8000-000000000000/files/unregistered.md": "x", ".runtime/private.txt": "runtime",
        "qorqut-roles/agent-roles/role.md": "roles", "README.md": "top level",
    }
    for relative, content in {**included, **outside}.items():
        write(env, relative, content)
    env.history.initialize()

    with tracked_reads(env, state):
        env.runner.intercepts.append(lambda name, command, kwargs: pytest.fail("Git ran inside a read unit")
                                     if state["open"] else None)
        summary = env.history.snapshot()
    assert state["count"] == 1 and state["open"] is False  # rows and areas came from ONE read unit, closed before Git
    assert summary["changed"] and summary["files_complete"] and summary["retryable"] is False
    assert summary["complete"] is False and summary["coverage_complete"] is False  # engine tables are unclassified here
    assert summary["previous_commit"] is None and summary["commit"] == git(env, "rev-parse", REF).decode().strip()
    assert summary["files"] == {"included": len(included), "bytes": sum(
        len(v if isinstance(v, bytes) else v.encode()) for v in included.values())}
    assert summary["areas"] == sorted(summary["areas"]) and len(summary["areas"]) == 6

    files = tree(env)
    database = {path for path in files if path.startswith("database/")}
    assert {path for path in files if path not in database} == set(included)
    assert database >= {"database/manifest.json", "database/work_notes.jsonl"}
    assert all(path.startswith(("database/work_items/", "database/work_notes", "database/manifest")) for path in database)
    assert blob(env, "files/assets/logo.bin") == b"\x00\x01\xff"

    # Selected rows only: safe fields, the declared nested projection, ordinary discussion; no private channel or secrets.
    items = [json.loads(line) for path in sorted(database) if path.startswith("database/work_items/")
             for line in blob(env, path).splitlines()]
    assert sorted(item["id"] for item in items) == ["item-1", "item-2"]
    first = next(item for item in items if item["id"] == "item-1")
    assert first == {"id": "item-1", "scope": "acme", "title": "Review the offer", "state": "open", "revision": 1,
                     "detail_json": {"label": "pricing", "pinned": True}}
    notes = [json.loads(line) for line in blob(env, "database/work_notes.jsonl").splitlines()]
    assert [note["id"] for note in notes] == [1, 3]
    assert SECRET.encode() not in everything(env) and b"private message" not in everything(env)
    manifest = json.loads(blob(env, "database/manifest.json"))
    assert manifest["coverage_complete"] is False and "event_log" in manifest["exclusions"]
    assert manifest["tables"]["work_items"]["omitted_columns"] == ["secret_note"]

    assert len(commits(env)) == 1
    assert git(env, "for-each-ref", "--format=%(refname)").split() == [REF.encode()]
    assert git(env, "log", "-1", "--format=%an <%ae>").strip() == b"Qorqut History <history@localhost>"
    assert {name for name, _, _ in env.runner.calls} <= ALLOWED_GIT
    assert not any("history-status" in path or "history-files" in path or "history.lock" in path for path in files)
    stored = env.history.status()
    assert stored["state"] == "incomplete" and stored["commit"] == summary["commit"]
    assert stored["last_attempt"]["outcome"] == "incomplete" and stored["last_complete"] is None
    assert stored["observation"]["consistent"] is True


def test_complete_requires_both_file_capture_and_table_coverage(complete):
    write(complete, "files/notes/plan.md", "note")
    complete.history.initialize()
    summary = complete.history.snapshot()
    assert summary["complete"] is True and summary["coverage_complete"] and summary["files_complete"]
    status = complete.history.status()
    assert status["state"] == "ok" and status["last_complete"] == {"at": summary["captured_at"], "commit": summary["commit"]}
    assert status["last_attempt"]["outcome"] == "complete"


def test_unclassified_tables_are_incomplete_coverage_that_never_dumps_them(env):
    env.history.initialize()
    first = env.history.snapshot()
    assert first["complete"] is False and first["coverage_complete"] is False
    missing = first["coverage"]["missing"]
    assert "principals" in missing and "work_items" not in missing and first["coverage"]["missing_total"] == len(missing)
    exported = [path for path in tree(env) if path.startswith("database/")]
    assert not any("principals" in path or "agent_profiles" in path for path in exported)
    assert env.history.status()["last_complete"] is None

    # Classification (composition) completes coverage; the same files then also become the last complete snapshot.
    classified = env.another(catalog=ExportCatalog(rest_classified(env.database)))
    second = classified.snapshot()
    assert second["complete"] is True and classified.status()["last_complete"]["commit"] == second["commit"]


def test_repeat_unchanged_then_changed_executable_row_and_deleted_history(complete):
    note = write(complete, "files/notes/plan.md", "first")
    keep = write(complete, "projects/acme/hq/keep.md", "stays")
    complete.history.initialize()
    first = complete.history.snapshot()

    again = complete.history.snapshot()
    assert again["changed"] is False and again["commit"] == first["commit"] and again["tree"] == first["tree"]
    assert len(commits(complete)) == 1

    note.write_text("second")
    changed = complete.history.snapshot()
    assert changed["changed"] and changed["previous_commit"] == first["commit"]
    assert git(complete, "diff-tree", "-r", "--name-only", first["commit"], changed["commit"]).split() == [b"files/notes/plan.md"]

    with complete.database.transaction() as uow:
        uow.connection.execute("UPDATE work_items SET state = 'done', revision = 2 WHERE id = 'item-1'")
    recorded = complete.history.snapshot()
    delta = git(complete, "diff-tree", "-r", "--name-only", changed["commit"], recorded["commit"]).split()
    assert recorded["changed"] and delta and all(path.startswith(b"database/") for path in delta)
    assert any(b"work_items" in path for path in delta)  # one sharded row file, not the whole table

    keep.chmod(0o755)
    assert tree(complete, complete.history.snapshot()["commit"])["projects/acme/hq/keep.md"][0] == "100755"

    note.unlink()
    deleted = complete.history.snapshot()
    assert "files/notes/plan.md" not in tree(complete, deleted["commit"])
    assert blob(complete, "files/notes/plan.md", first["commit"]) == b"first"
    assert blob(complete, "files/notes/plan.md", changed["commit"]) == b"second"
    assert len(commits(complete)) == 5


def test_boundaries_skip_private_nested_linked_and_special_material_and_report_it(env, tmp_path):
    outside = tmp_path / "outside"
    (outside / "hq").mkdir(parents=True)
    (outside / "outside.txt").write_text("TOPSECRET outside the authored areas")
    (outside / "hq" / "leak.md").write_text("TOPSECRET through a linked business folder")
    included = {"files/ok.md": "fine", "files/env-notes.md": "mentions .env but is ordinary",
                "files/data/table.csv": "a,b\n1,2\n", "files/assets/pic.png": b"\x89PNG\r\n"}
    private = {
        "files/research/project/.git/HEAD": "ref: refs/heads/main", "files/research/project/readme.md": "TOPSECRET",
        "files/mirror.git/HEAD": "ref", "files/mirror.git/objects/x": "TOPSECRET", "files/mirror.git/refs/x": "x",
        "files/tools/.runtime/state": "TOPSECRET", "files/.env": "TOPSECRET=1", "files/app/.env.local": "TOPSECRET=1",
        "files/web/node_modules/pkg/index.js": "TOPSECRET", "files/.venv/lib/x.py": "TOPSECRET",
        "files/py/pyvenv.cfg": "home = /usr", "files/py/lib/site.py": "TOPSECRET",
        "files/agent/.claude/settings.json": "TOPSECRET", "files/agent/.codex/config.toml": "TOPSECRET",
        "files/agent/.mcp.json": "TOPSECRET", "files/build/__pycache__/m.pyc": "TOPSECRET",
        "files/tmp/.cache/blob": "TOPSECRET", "files/keys/id_rsa": "TOPSECRET", "files/keys/deploy.pem": "TOPSECRET",
        "files/keys/credentials.json": "TOPSECRET", "files/keys/.ssh/config": "TOPSECRET",
        "files/notes-key.txt": "-----BEGIN OPENSSH PRIVATE KEY-----\nTOPSECRET\n-----END OPENSSH PRIVATE KEY-----\n",
        "files/.git ": "TOPSECRET", "files/GIT~1": "TOPSECRET",
    }
    for relative, content in {**included, **private}.items():
        write(env, relative, content)
    os.symlink(outside / "outside.txt", env.operations / "files/linked-file.md")
    os.symlink(outside, env.operations / "files/linked-directory")
    os.symlink(env.operations / "files/ok.md", env.operations / "files/linked-inside.md")
    os.mkfifo(env.operations / "files/pipe")
    (env.operations / "projects").mkdir()
    os.symlink(outside, env.operations / "projects/acme")
    env.history.initialize()

    summary = env.history.snapshot()
    assert summary["files_complete"] and summary["incomplete_total"] == 0
    expected = {
        "files/research/project": "nested_git", "files/mirror.git": "nested_git", "files/tools/.runtime": "runtime",
        "files/.env": "env_file", "files/app/.env.local": "env_file", "files/web/node_modules": "node_modules",
        "files/.venv": "virtualenv", "files/py": "virtualenv", "files/agent/.claude": "provider_config",
        "files/agent/.codex": "provider_config", "files/agent/.mcp.json": "provider_config",
        "files/build/__pycache__": "cache", "files/tmp/.cache": "cache", "files/keys/id_rsa": "private_key",
        "files/keys/deploy.pem": "private_key", "files/keys/credentials.json": "credentials",
        "files/keys/.ssh": "credentials", "files/notes-key.txt": "private_key", "files/linked-file.md": "symlink",
        "files/linked-directory": "symlink", "files/linked-inside.md": "symlink", "files/pipe": "special_file",
        "files/.git ": "unsafe_name", "files/GIT~1": "unsafe_name",
        "business_hq:acme": "symlink",  # the linked business folder cannot be resolved; none of it is read
        "business_agent_files:acme:" + ACME_AGENT: "symlink",
    }
    assert {item["path"]: item["reason"] for item in summary["exclusions"]} == expected
    assert summary["exclusions_total"] == len(expected)
    files = tree(env)
    assert {path for path in files if not path.startswith("database/")} == set(included)
    assert b"TOPSECRET" not in everything(env)


def test_included_files_keep_complete_bytes_and_git_environment_cannot_alter_them(env, monkeypatch, tmp_path):
    import random
    random_bytes = random.Random(7).randbytes(3 * _capture.CHUNK + 12345)
    samples = {
        "files/data/binary.bin": bytes(range(256)) * 3 + b"\x00\x00", "files/data/crlf.txt": b"one\r\ntwo\r\nno newline",
        "files/data/not-utf8.txt": b"\xff\xfe broken \xc3", "files/data/empty": b"", "files/assets/large.dat": random_bytes,
        "files/data/ünïcode name.md": "ünï",
    }
    for relative, content in samples.items():
        write(env, relative, content)
    write(env, "projects/beta/hq/run.sh", "#!/bin/sh\n", mode=0o755)
    hostile = tmp_path / "user-gitconfig"
    hostile.write_text("[core]\n\tautocrlf = true\n\thooksPath = /nonexistent\n[user]\n\tname = Someone Else\n")
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "--bare", "-q", str(other)], check=True, env=GIT_ENV)
    for key, value in {"GIT_CONFIG_GLOBAL": str(hostile), "GIT_DIR": str(other), "GIT_INDEX_FILE": str(tmp_path / "x"),
                       "GIT_AUTHOR_NAME": "Someone Else", "GIT_WORK_TREE": str(tmp_path), "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/x",
                       "GIT_NAMESPACE": "elsewhere"}.items():
        monkeypatch.setenv(key, value)
    env.history.initialize()
    hook = env.repository / "hooks" / "reference-transaction"
    hook.parent.mkdir()
    hook.write_text(f'#!/bin/sh\ntouch {tmp_path / "hook-ran"}\n')
    hook.chmod(0o755)

    env.history.snapshot()
    for relative, content in samples.items():
        assert blob(env, relative) == (content if isinstance(content, bytes) else content.encode()), relative
    assert tree(env)["projects/beta/hq/run.sh"][0] == "100755"
    assert not (tmp_path / "hook-ran").exists()
    assert git(env, "log", "-1", "--format=%an").strip() == b"Qorqut History"
    assert not subprocess.run(["git", "--git-dir", str(other), "rev-parse", "--verify", "-q", "HEAD"],
                              env=GIT_ENV, capture_output=True).stdout
    allowed_environment = {"GIT_DIR", "GIT_INDEX_FILE", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT",
                           "GIT_OPTIONAL_LOCKS", "GIT_NO_REPLACE_OBJECTS", "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME",
                           "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"}
    for _, command, kwargs in env.runner.calls:
        assert not any(key.startswith("GIT_") and key not in allowed_environment for key in kwargs["env"])
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull and "core.hooksPath=/dev/null" in command
        assert "protocol.allow=never" in command and "fetch" not in command and "push" not in command


def test_concurrently_changed_file_is_incomplete_retryable_and_keeps_saved_version(complete):
    live = write(complete, "files/notes/live.txt", "saved version")
    write(complete, "files/notes/stable.md", "unchanged")
    complete.history.initialize()
    first = complete.history.snapshot()

    live.write_text("new version being written")
    write(complete, "files/notes/added.md", "captured meanwhile")

    def mutate(name, command, kwargs):
        stream = kwargs.get("stdin")
        if name == "hash-object" and hasattr(stream, "fileno") and mutate.once and os.readlink(
                f"/proc/self/fd/{stream.fileno()}") == os.path.realpath(live):
            mutate.once = False
            live.write_text("a third version, written while the snapshot read it")
    mutate.once = True
    complete.runner.intercepts.append(mutate)
    partial = complete.history.snapshot()
    complete.runner.intercepts.clear()

    assert partial["complete"] is False and partial["retryable"] is True and partial["coverage_complete"] is True
    assert partial["incomplete"] == [{"path": "files/notes/live.txt", "reason": "changed_during_capture"}]
    assert blob(complete, "files/notes/live.txt") == b"saved version"
    assert blob(complete, "files/notes/added.md") == b"captured meanwhile"
    status = complete.history.status()
    assert status["state"] == "incomplete" and status["last_complete"]["commit"] == first["commit"]
    assert status["last_attempt"]["outcome"] == "incomplete" and status["last_snapshot"]["incomplete_total"] == 1

    resolved = complete.history.snapshot()
    assert resolved["complete"] and blob(complete, "files/notes/live.txt") == b"a third version, written while the snapshot read it"
    assert complete.history.status()["state"] == "ok" and len(commits(complete)) == 3


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions are not enforced for root")
def test_unreadable_directory_keeps_its_saved_files_instead_of_recording_deletion(complete):
    write(complete, "files/vault/one.md", "one")
    write(complete, "files/open/two.md", "two")
    complete.history.initialize()
    complete.history.snapshot()
    vault = complete.operations / "files/vault"
    vault.chmod(0)
    try:
        summary = complete.history.snapshot()
    finally:
        vault.chmod(0o755)
    assert summary["incomplete"] == [{"path": "files/vault", "reason": "unreadable"}] and not summary["complete"]
    assert summary["changed"] is False and blob(complete, "files/vault/one.md") == b"one"
    assert complete.history.snapshot()["complete"]


def test_stat_cache_cannot_hide_a_same_size_change_with_restored_times(env, monkeypatch):
    monkeypatch.setattr(_capture, "SETTLE_NS", 0)
    path = write(env, "files/data/value.txt", "aaaa")
    env.history.initialize()
    env.history.snapshot()
    before = path.stat()
    assert env.history.snapshot()["changed"] is False
    path.write_text("bbbb")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size and path.stat().st_mtime_ns == before.st_mtime_ns
    assert env.history.snapshot()["changed"] is True
    assert blob(env, "files/data/value.txt") == b"bbbb"


def test_git_failure_preserves_the_last_complete_snapshot_and_accepted_rows(complete):
    write(complete, "files/notes/plan.md", "saved")
    complete.history.initialize()
    first = complete.history.snapshot()
    assert first["complete"]

    with complete.database.transaction() as uow:
        uow.connection.execute("INSERT INTO work_items(id, scope, title, state) VALUES ('item-3','global','Accepted','open')")
    write(complete, "files/notes/plan.md", "edited")
    complete.runner.intercepts.append(lambda name, command, kwargs: failure() if name == "commit-tree" else None)
    with pytest.raises(DomainError) as error:
        complete.history.snapshot()
    assert error.value.code == "history_git_failed" and error.value.retryable is True
    assert git(complete, "rev-parse", REF).decode().strip() == first["commit"] and len(commits(complete)) == 1

    status = complete.history.status()
    assert status["state"] == "failed" and status["consecutive_failures"] == 1
    assert status["error"]["code"] == "history_git_failed" and status["commit"] == first["commit"]
    assert status["last_attempt"]["outcome"] == "failed" and status["last_attempt"]["error"]["code"] == "history_git_failed"
    assert status["last_complete"]["commit"] == first["commit"]  # the latest known complete snapshot is preserved
    assert status["observation"]["consistent"] is True
    with complete.database.read() as uow:
        assert uow.connection.execute("SELECT count(*) FROM work_items WHERE id = 'item-3'").fetchone()[0] == 1

    def missing_git(name, command, kwargs):
        raise FileNotFoundError("git")
    complete.runner.intercepts[:] = [missing_git]
    with pytest.raises(DomainError) as error:
        complete.history.snapshot()
    assert error.value.code == "history_git_failed" and complete.history.status()["consecutive_failures"] == 2

    complete.runner.intercepts.clear()
    recovered = complete.history.snapshot()
    assert recovered["changed"] and blob(complete, "files/notes/plan.md") == b"edited"
    status = complete.history.status()
    assert status["state"] == "ok" and status["consecutive_failures"] == 0 and status["error"] is None
    assert status["last_complete"]["commit"] == recovered["commit"]


def test_ref_update_is_compare_and_swap_and_snapshots_are_serialized(complete):
    complete.history.initialize()
    first = complete.history.snapshot()
    with complete.database.transaction() as uow:
        uow.connection.execute("INSERT INTO work_items(id, scope, title, state) VALUES ('item-4','global','Triggers','open')")

    def interloper(name, command, kwargs):
        if name == "update-ref":
            interloper.commit = git(complete, "commit-tree", first["tree"], "-p", first["commit"],
                                    data=b"someone else\n").strip().decode()
            git(complete, "update-ref", REF, interloper.commit, first["commit"])
    complete.runner.intercepts.append(interloper)
    with pytest.raises(DomainError) as error:
        complete.history.snapshot()
    complete.runner.intercepts.clear()
    assert error.value.code == "history_conflict" and error.value.retryable is True
    assert git(complete, "rev-parse", REF).decode().strip() == interloper.commit  # the unexpected change is preserved
    status = complete.history.status()
    assert status["state"] == "failed" and status["observation"]["consistent"] is False  # reported honestly
    assert status["observation"]["ref_commit"] == interloper.commit and "note" in status["observation"]

    busy = []

    def overlap(name, command, kwargs):
        if name == "write-tree" and not busy:
            try:
                complete.another().snapshot()
            except DomainError as other:
                busy.append(other)
    complete.runner.intercepts.append(overlap)
    assert complete.history.snapshot()["changed"]
    assert busy[0].code == "history_busy" and busy[0].retryable is True
    assert complete.history.status()["observation"]["consistent"] is True


def test_failing_to_record_advisory_status_cannot_undo_a_commit_and_is_reported_later(complete, monkeypatch):
    write(complete, "files/notes/plan.md", "one")
    complete.history.initialize()
    first = complete.history.snapshot()
    write(complete, "files/notes/plan.md", "two")

    real = _state.write_json

    def failing(runtime, name, value):
        if name == "history-status.json":
            raise OSError("status disk full")
        return real(runtime, name, value)
    monkeypatch.setattr(_state, "write_json", failing)
    second = complete.history.snapshot()
    monkeypatch.setattr(_state, "write_json", real)

    assert second["changed"] and second["status_recorded"] is False
    assert git(complete, "rev-parse", REF).decode().strip() == second["commit"] and len(commits(complete)) == 2
    status = complete.history.status()
    assert status["commit"] == second["commit"] and status["last_complete"]["commit"] == first["commit"]
    assert status["observation"]["consistent"] is False  # live ref is ahead of the recorded status
    assert status["observation"]["recorded_commit"] == first["commit"]

    third = complete.history.snapshot()  # unchanged tree: no commit, status reconciled
    assert third["changed"] is False and third["status_recorded"] is True
    assert complete.history.status()["observation"]["consistent"] is True


def test_composition_and_export_failures_fail_the_snapshot_without_committing(env):
    from qorqut_operations.history_exports import HistoryContribution, TableClassification, TECHNICAL_EXCLUDED
    env.history.initialize()
    first = env.history.snapshot()
    broken = ExportCatalog([HistoryContribution("ghost", classifications=(
        TableClassification("ghost_table", TECHNICAL_EXCLUDED, "does not exist"),))])
    history = env.another(catalog=broken)
    with pytest.raises(DomainError) as error:
        history.snapshot()
    assert error.value.code == "history_composition_invalid"
    assert git(env, "rev-parse", REF).decode().strip() == first["commit"] and len(commits(env)) == 1
    assert history.status()["state"] == "failed"


def test_provider_must_return_valid_typed_areas_or_the_snapshot_fails(env):
    cases = {
        "duplicate": lambda uow: [GlobalFiles(), GlobalFiles()],
        "case_folded_duplicate": lambda uow: [BusinessHeadquarters("Acme"), BusinessHeadquarters("acme")],
        "unsafe_segment": lambda uow: [BusinessHeadquarters("../escape")],
        "unsafe_agent": lambda uow: [GlobalAgentFiles("a/b")],
        "hidden_segment": lambda uow: [GlobalAgentFiles(".hidden")],
        "absolute_path": lambda uow: ["/etc"],
        "untyped": lambda uow: [{"kind": "global_files"}],
        "not_iterable": lambda uow: 7,
        "bad_exclusion": lambda uow: [GlobalFiles(excluded=("../up",))],
        "unnormalized_exclusion": lambda uow: [GlobalFiles(excluded=("a//b",))],
        "absolute_exclusion": lambda uow: [GlobalFiles(excluded=("/tmp",))],
        "string_exclusions": lambda uow: [GlobalFiles(excluded="projection")],
        "duplicate_exclusion": lambda uow: [GlobalFiles(excluded=("a", "a"))],
    }
    env.history.initialize()
    first = env.history.snapshot()
    for name, provider in cases.items():
        history = env.another(areas=provider)
        with pytest.raises(DomainError) as error:
            history.snapshot()
        assert error.value.code == "history_areas_invalid", name
        assert git(env, "rev-parse", REF).decode().strip() == first["commit"], name
    with pytest.raises(TypeError):
        env.another(areas=None)
    with pytest.raises(TypeError):
        env.another(catalog=[domain_contribution()])
    wiring = dict(catalog=env.catalog, areas=registered_areas, clock=env.clock)
    for missing in ("locations", "resolve_areas", "safe_segment"):
        callables = {"locations": lambda: (env.operations, env.repository), "resolve_areas": lambda areas: (),
                     "safe_segment": lambda value: True}
        callables[missing] = str(env.root)  # a path where the host's callable belongs
        with pytest.raises(TypeError):
            History(env.database, **callables, **wiring)


def test_trusted_excluded_subtrees_are_skipped_and_never_carried_forward(env):
    write(env, "files/notes/plan.md", "authored")
    write(env, "files/projection/table.jsonl", "generated from rows")
    write(env, "projects/acme/hq/exports/summary.json", "generated")
    write(env, "projects/acme/hq/notes.md", "authored hq")

    def provider(excluded):
        def areas(uow):
            return [GlobalFiles(excluded=excluded.get("files", ())), BusinessHeadquarters(
                "acme", excluded=excluded.get("acme", ()))]
        return areas

    env.history.initialize()
    plain = env.another(areas=provider({}))
    assert "files/projection/table.jsonl" in tree(env, plain.snapshot()["commit"])

    # The domain starts excluding its generated projections; they leave the tree and stay in older commits.
    excluding = env.another(areas=provider({"files": ("projection",), "acme": ("exports",)}))
    summary = excluding.snapshot()
    files = tree(env, summary["commit"])
    assert "files/projection/table.jsonl" not in files and "projects/acme/hq/exports/summary.json" not in files
    assert "files/notes/plan.md" in files and "projects/acme/hq/notes.md" in files
    reasons = {item["path"]: item["reason"] for item in summary["exclusions"]}
    assert reasons == {"files/projection": "generated_projection", "projects/acme/hq/exports": "generated_projection"}
    assert blob(env, "files/projection/table.jsonl", plain.status()["commit"] + "~1") == b"generated from rows"

    # An unreadable parent keeps saved authored files but must not resurrect the now-excluded content.
    if os.geteuid() != 0:
        write(env, "files/projection/table.jsonl", "generated again")
        (env.operations / "files").chmod(0)
        try:
            blocked = excluding.snapshot()
        finally:
            (env.operations / "files").chmod(0o755)
        assert blocked["incomplete"] == [{"path": "files", "reason": "unreadable"}]
        carried = tree(env, blocked["commit"])
        assert "files/notes/plan.md" in carried and "files/projection/table.jsonl" not in carried
    # Excluded names also stay excluded on carry-forward even when they were somehow saved earlier.
    assert excluding.snapshot()["complete"] is False  # engine tables remain unclassified in this fixture


def test_registered_areas_drive_inclusion_not_folders_or_tags(env):
    write(env, f"projects/acme/agents/{ACME_AGENT}/files/a.md", "registered")
    write(env, "projects/acme/agents/zzzz-unregistered/files/b.md", "folder only")
    write(env, "projects/acme/hq/h.md", "hq")
    env.history.initialize()
    paths = set(tree(env, env.history.snapshot()["commit"]))
    assert f"projects/acme/agents/{ACME_AGENT}/files/a.md" in paths and "projects/acme/hq/h.md" in paths
    assert not any("zzzz-unregistered" in path for path in paths)

    # Deregistering a business agent removes its area from the next tree but never from earlier commits.
    with env.database.transaction() as uow:
        uow.connection.execute("DELETE FROM agent_profiles WHERE agent_id = ?", (ACME_AGENT,))
    after = env.history.snapshot()
    assert f"projects/acme/agents/{ACME_AGENT}/files/a.md" not in tree(env, after["commit"])
    assert blob(env, f"projects/acme/agents/{ACME_AGENT}/files/a.md", after["previous_commit"]) == b"registered"


def test_symlinked_area_ancestors_are_never_followed_even_when_the_link_appears_later(env, tmp_path):
    outside = tmp_path / "outside-agents"
    (outside / ACME_AGENT / "files").mkdir(parents=True)
    (outside / ACME_AGENT / "files" / "leak.md").write_text("TOPSECRET via a linked agents folder")
    write(env, "files/ok.md", "fine")
    env.history.initialize()
    env.history.snapshot()
    shared = env.operations / "projects" / "acme" / "agents"
    shared.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, shared)
    summary = env.history.snapshot()
    assert b"TOPSECRET" not in everything(env)
    assert {"path": f"business_agent_files:acme:{ACME_AGENT}", "reason": "symlink"} in summary["exclusions"]
    assert not any(path.startswith("projects/acme/agents") for path in tree(env, summary["commit"]))
