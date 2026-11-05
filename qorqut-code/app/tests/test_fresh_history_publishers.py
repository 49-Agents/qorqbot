"""Both retained history publishers over the repository the fresh initializer provisions, with real Git and synthetic data only.

An instance is made by the actual ``bootstrap.initialize`` (the neutral ``qorqut-instance`` HEAD with one empty bootstrap commit),
its stock runtime is opened over it, and then:

* the real table publisher (``TableHistory`` run as the registered ``tables.version`` / ``tables.global-version`` jobs) makes its
  first publication for a Project table and a Global table, survives a Git-success / SQL-receipt-failure and its retry, and
  publishes a later revision on top of its own ref;
* the real nightly publisher's Git stage (``GitVersioner.version``) makes its first publication from the bootstrap ``HEAD``, survives
  a crash before and after its ref update, and is a no-op for unchanged content. The rest of ``NightlyMaintenance`` (the quiesced backup
  capture, exports, retention, scheduling) is a separate prerequisite chain with its own tests (``test_nightly_capture_host.py``)
  and is deliberately not part of this proof; the generation here is a small synthetic one.

Throughout, the neutral ``HEAD`` never moves, one publisher never moves the other's ref, the owner's real index (with a staged
sentinel) is byte-identical, and private sentinel files inside ``operations/`` appear in neither history. A repository with an
unborn ``HEAD`` is the negative control for why the bootstrap commit exists."""

import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id
from qorqut_operations.tables import HISTORY_REF as TABLES_REF

from bazarlyk.background.integration import attach_background
from bazarlyk.bootstrap import initialize as init
from bazarlyk.extensions import installed_selection
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.service.runtime import build_runtime
from bazarlyk.storage.versioning import HISTORY_REF as NIGHTLY_REF, GitVersioner
from test_initialize_instance import no_environment, prepare

NEUTRAL_REF = "refs/heads/qorqut-instance"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
COLUMNS = [{"id": "name", "name": "Name", "type": "text"}, {"id": "amount", "name": "Amount", "type": "number"}]
# Private files below operations/ that no history may ever carry: untracked, and (one) staged in the owner's real index.
SENTINELS = {"PRIVATE-STAGED.txt": b"staged by the owner, never part of any history\n",
             "PRIVATE-UNTRACKED.txt": b"untracked private file\n",
             "projects/alpha/code/secret.py": b"TOKEN = 'code of a project repository'\n",
             "agents/helper/.bazarlyk/credential": b"agent credential\n",
             ".runtime/private.bin": b"runtime state\n"}
PRIVATE_MARKERS = ("PRIVATE-", "secret.py", "credential", ".runtime", ".env", "sqlite3")


def run_git(operations, *arguments):
    """The test's own Git over the operations root: no ambient or global configuration."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", str(operations), *arguments], env=environment, capture_output=True, text=True)


def git(operations, *arguments, check=True):
    result = run_git(operations, *arguments)
    assert not check or result.returncode == 0, result.stderr
    return result.stdout.strip()


def is_ancestor(operations, older, newer):
    return run_git(operations, "merge-base", "--is-ancestor", older, newer).returncode == 0


def ref(operations, name):
    return git(operations, "rev-parse", "--verify", "--quiet", name, check=False) or None


def names(operations, revision):
    return git(operations, "ls-tree", "-r", "--name-only", revision).split("\n") if git(operations, "ls-tree", "-r", revision) else []


class Fresh:
    """One instance made by the actual initializer, with the owner's sentinels placed beside its (empty) history."""

    def __init__(self, tmp_path, *, inside_checkout=False):
        self.prepared, self.instance = prepare(tmp_path), tmp_path / "instance"
        self.checkout = None
        if inside_checkout:
            # The new instance is placed inside another repository (the owner's source checkout, with commits and a staged file).
            self.checkout, self.instance = tmp_path / "checkout", tmp_path / "checkout" / "instance"
            self.checkout.mkdir()
            git(self.checkout, "init", "--quiet", "--initial-branch=trunk")
            (self.checkout / "tracked.txt").write_text("tracked\n")
            git(self.checkout, "add", "tracked.txt")
            git(self.checkout, "-c", "user.name=Owner", "-c", "user.email=owner@example.test", "commit", "--quiet", "-m", "owner work")
            (self.checkout / "staged.txt").write_text("staged\n")
            git(self.checkout, "add", "staged.txt")
            self.checkout_state = self.checkout_snapshot()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(init, "require_installed", no_environment)
            self.record = init.initialize(self.prepared, self.instance, owner_name="Ada")
        self.operations = self.instance / "operations"
        self.bootstrap = self.record["history_repository"]["bootstrap_commit"]
        assert ref(self.operations, "HEAD") == self.bootstrap and git(self.operations, "rev-list", "--count", "HEAD") == "1"
        assert git(self.operations, "rev-parse", "HEAD^{tree}") == EMPTY_TREE
        for relative, body in SENTINELS.items():
            path = self.operations / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        git(self.operations, "add", "-f", "--", "PRIVATE-STAGED.txt")             # the owner's own staging in the real index
        self.index = (self.operations / ".git" / "index").read_bytes()

    def checkout_snapshot(self):
        """Refs, HEAD, index and every object file of the ancestor checkout."""
        objects = sorted(path.relative_to(self.checkout).as_posix() for path in (self.checkout / ".git" / "objects").rglob("*") if path.is_file())
        return (git(self.checkout, "for-each-ref"), git(self.checkout, "rev-parse", "HEAD"), (self.checkout / ".git" / "index").read_bytes(), objects,
                (self.checkout / ".git" / "HEAD").read_bytes())

    def open(self):
        """The stock runtime over the initialized instance, owned and with the registered background programs and workers."""
        paths = InstancePaths(self.instance, {name: Path(location) for name, location in self.record["components"].items()})
        runtime = build_runtime(self.prepared / "src", paths=paths, extensions=installed_selection({"extensions": []}))
        runtime.own_instance()
        attach_background(runtime, runtime.programs)
        runtime.freeze_registry()
        self.runtime, self.paths = runtime, paths
        with runtime.database.transaction() as uow:
            self.project = new_id()
            uow.repository("businesses").insert({"id": self.project, "slug": "alpha", "name": "Alpha",
                                                 "workspace": str(paths.business_root("alpha")), "hq_path": str(paths.business_hq("alpha"))})
            uow.repository("business_memberships", business_id=self.project).insert({"principal_id": self.record["owner_principal_id"], "is_owner": True})
            runtime.authority.register_workers(uow, [self.project])
        return self

    def close(self):
        self.runtime.release_instance()
        self.runtime.domains.shared_context.close()
        self.runtime.database.close()

    def call(self, scope, operation, arguments, revision=None):
        owner = self.record["owner_principal_id"]
        if scope == "project":
            context = InvocationContext(Principal(owner, "human"), business_id=self.project, allowed_business_ids=frozenset({self.project}), source="dashboard")
        else:
            context, arguments = init.owner_context(owner), {"scope": "global", **arguments}
        result = self.runtime.application.invoke(context, operation, arguments, InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
        assert result.status == "ok", result.error
        return result.data

    def run(self, job):
        result = self.runtime.background.run_one(job)
        assert result["state"] == "succeeded", result
        return result

    def versions(self, scope):
        with self.runtime.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(
                "SELECT id, table_id, path, table_revision, git_commit, snapshot_json IS NULL AS released FROM table_versions "
                "WHERE business_id IS ? ORDER BY sequence", (None if scope == "global" else self.project,))]

    def untouched(self):
        """The neutral branch, the owner's staged index and every sentinel are exactly as the owner left them."""
        assert git(self.operations, "symbolic-ref", "HEAD") == NEUTRAL_REF and ref(self.operations, NEUTRAL_REF) == self.bootstrap
        assert (self.operations / ".git" / "index").read_bytes() == self.index
        assert git(self.operations, "diff", "--cached", "--name-only") == "PRIVATE-STAGED.txt"
        assert {relative: (self.operations / relative).read_bytes() for relative in SENTINELS} == SENTINELS


class Control:
    def __init__(self):
        self.job = {"id": new_id()}
        self.business_id = None

    def check(self):
        pass

    def remaining_seconds(self):
        return 120


def generation(tmp_path, name, *, files, exports, businesses=()):
    """A small synthetic nightly generation: its authored source archive, manifest and generated export bundle."""
    directory = tmp_path / name
    (directory / "authored").mkdir(parents=True)
    (directory / "authored" / "manifest.json").write_text(json.dumps({"businesses": list(businesses)}))
    with tarfile.open(directory / "authored" / "sources.tar", "w:") as archive:
        for relative, body in sorted(files.items()):
            member = tarfile.TarInfo(relative)
            member.size, member.mode = len(body), 0o644
            archive.addfile(member, io.BytesIO(body))
    metadata = {"created_at": "2026-10-25T12:00:00Z", "generation_id": name}
    return directory, metadata, SimpleNamespace(files=dict(exports))


# What an authored source archive of the fresh instance can contain: public authored paths, plus the sentinels the policy must drop.
def authored(extra=None):
    return {"README.md": b"Instance notes\n", "files/instructions/agent-foundation.md": b"Shared instructions\n",
            **SENTINELS, ".env": b"KEY=private\n", ".runtime/operations.sqlite3": b"database bytes", **(extra or {})}


class CrashAfter:
    """The real ``subprocess.run`` that stops the process once, right after the named Git subcommand succeeded."""

    def __init__(self, subcommand=None):
        self.subcommand, self.calls = subcommand, []

    def __call__(self, command, **options):
        result = subprocess.run(command, **options)
        subcommand = next(part for index, part in enumerate(command[1:], 1) if command[index - 1] not in ("-c", "-C") and not part.startswith("-"))
        self.calls.append(subcommand)
        if subcommand == self.subcommand:
            self.subcommand = None
            raise SystemExit("simulated process exit after " + subcommand)
        return result


def test_tables_then_nightly_publish_from_the_fresh_repository_without_moving_each_other(tmp_path):
    fresh = Fresh(tmp_path)
    operations = fresh.operations
    fresh.open()
    try:
        # -- table publisher, first publication: a Global table and a Project table, each a root commit of its own ref ------
        created = fresh.call("global", "tables_create", {"title": "Metrics", "columns": COLUMNS})
        global_table, global_job = created["table"]["id"], created["job_id"]
        history = fresh.runtime.domains.tables.history
        real_commit, receipt = history.commit, {}

        def crash_after_ref_update(version, control):
            receipt["commit"] = real_commit(version, control)
            raise OSError("Simulated stop after the Git ref update, before the SQLite receipt")

        history.commit = crash_after_ref_update
        with pytest.raises(OSError, match="Simulated stop"):
            fresh.runtime.background.run_one(global_job)
        history.commit = real_commit
        first = receipt["commit"]
        assert ref(operations, TABLES_REF) == first and git(operations, "rev-list", "--count", TABLES_REF) == "1"
        assert git(operations, "rev-list", "--parents", "-n", "1", TABLES_REF) == first          # a root commit: not the bootstrap's child
        assert names(operations, TABLES_REF) == [f"files/tables/{global_table}.json"]
        assert fresh.versions("global")[0]["git_commit"] is None                                 # the SQL receipt is what failed
        fresh.untouched()
        assert ref(operations, NIGHTLY_REF) is None

        # -- the retry reuses the exact commit already in Git: no second version, no second commit --------------------
        retry = fresh.call("global", "tables_history_retry", {"version_id": fresh.versions("global")[0]["id"]})
        fresh.run(retry["job_id"])
        assert fresh.versions("global")[0]["git_commit"] == first and fresh.versions("global")[0]["released"]
        assert ref(operations, TABLES_REF) == first and git(operations, "rev-list", "--count", TABLES_REF) == "1"
        fresh.untouched()

        project = fresh.call("project", "tables_create", {"title": "Pipeline", "columns": COLUMNS})
        project_table = project["table"]["id"]
        fresh.run(project["job_id"])
        second = ref(operations, TABLES_REF)
        assert second != first and git(operations, "rev-list", "--parents", "-n", "1", TABLES_REF) == f"{second} {first}"
        assert sorted(names(operations, TABLES_REF)) == sorted([f"files/tables/{global_table}.json", f"projects/alpha/hq/tables/{project_table}.json"])
        assert json.loads(git(operations, "show", f"{TABLES_REF}:projects/alpha/hq/tables/{project_table}.json"))["table"]["title"] == "Pipeline"
        fresh.untouched()
        assert ref(operations, NIGHTLY_REF) is None

        # -- nightly publisher, first publication: its base is the neutral HEAD (the bootstrap commit), its tree only public paths --
        control, state = Control(), {}
        files = authored({"projects/alpha/hq/notes/idea.md": b"A project note\n", "projects/alpha/hq/context/email/private.eml": b"mail\n"})
        businesses = [{"slug": "alpha", "root": "projects/alpha", "hq": "projects/alpha/hq"}]
        source, metadata, bundle = generation(tmp_path, "gen-1", files=files, businesses=businesses,
                                              exports={"exports/manifest.json": b'{"generated": true}\n',
                                                       "projects/alpha/hq/exports/tasks.json": b'{"records": []}\n'})
        crash = CrashAfter("update-ref")
        nightly = GitVersioner(operations, runner=crash)
        with pytest.raises(SystemExit):
            nightly.version(source, metadata, bundle, state, lambda: None, control)
        published = ref(operations, NIGHTLY_REF)
        assert published == state["commit"] and state["base_commit"] == fresh.bootstrap and state["base_ref"] is None and state["changed"]
        assert git(operations, "rev-list", "--parents", "-n", "1", NIGHTLY_REF) == f"{published} {fresh.bootstrap}"
        selected = names(operations, NIGHTLY_REF)
        assert selected == sorted(["README.md", "files/instructions/agent-foundation.md", "exports/manifest.json",
                                   "projects/alpha/hq/exports/tasks.json", "projects/alpha/hq/notes/idea.md"])
        assert not [name for name in selected if any(marker in name for marker in PRIVATE_MARKERS)] and "projects/alpha/hq/context/email/private.eml" not in selected
        assert ref(operations, TABLES_REF) == second                       # the table publisher's ref was not moved
        fresh.untouched()

        # -- retry after the crash: same saved state, the ref already there; no new commit and no second ref update --
        before = crash.calls.count("commit-tree")
        assert nightly.version(source, metadata, bundle, state, lambda: None, control)["commit"] == published
        assert crash.calls.count("commit-tree") == before == 1 and git(operations, "rev-list", "--count", NIGHTLY_REF) == "2"
        # An unchanged later generation: the same tree, so no new commit and the ref stays.
        again, metadata_2, bundle_2 = generation(tmp_path, "gen-2", files=files, businesses=businesses, exports=bundle.files)
        unchanged = GitVersioner(operations).version(again, metadata_2, bundle_2, {}, lambda: None, control)
        assert unchanged == {"enabled": True, "ref": NIGHTLY_REF, "commit": published, "changed": False}
        assert ref(operations, NIGHTLY_REF) == published and ref(operations, TABLES_REF) == second
        fresh.untouched()

        # -- a later table revision still builds on the table ref, not on the nightly ref or the neutral HEAD ----------
        rows = fresh.call("global", "tables_rows_write", {"table_id": global_table, "rows": [{"cells": {"name": "A", "amount": 1.5}}]}, revision=1)
        fresh.run(rows["job_id"])
        third = ref(operations, TABLES_REF)
        assert git(operations, "rev-list", "--parents", "-n", "1", TABLES_REF) == f"{third} {second}"
        assert json.loads(git(operations, "show", f"{TABLES_REF}:files/tables/{global_table}.json"))["rows"][0]["cells_json"]["name"] == "A"
        assert ref(operations, NIGHTLY_REF) == published
        fresh.untouched()
        # Every private sentinel is outside both histories, and neither publisher left an index or worktree entry behind.
        for revision in (TABLES_REF, NIGHTLY_REF):
            assert not [name for name in names(operations, revision) if any(marker in name for marker in PRIVATE_MARKERS)]
        assert git(operations, "worktree", "list", "--porcelain").count("worktree ") == 1 and git(operations, "remote") == ""
    finally:
        fresh.close()


def test_nightly_then_tables_publish_from_the_fresh_repository_inside_another_checkout(tmp_path):
    """Nightly first (with a crash before its ref update), then tables; the instance lives inside an unrelated repository that
    neither the initializer nor either publisher may use or change."""
    fresh = Fresh(tmp_path, inside_checkout=True)
    operations = fresh.operations
    assert git(operations, "rev-parse", "--show-toplevel") == str(operations) != str(fresh.checkout)
    assert fresh.checkout_snapshot() == fresh.checkout_state
    fresh.open()
    try:
        source, metadata, bundle = generation(tmp_path, "gen-1", files=authored(), exports={"exports/manifest.json": b'{"generated": true}\n'})
        state, crash = {}, CrashAfter("commit-tree")
        with pytest.raises(SystemExit):                                  # the commit object exists, but neither the state nor the ref was saved
            GitVersioner(operations, runner=crash).version(source, metadata, bundle, state, lambda: None, Control())
        assert ref(operations, NIGHTLY_REF) is None and "commit" not in state
        result = GitVersioner(operations).version(source, metadata, bundle, state, lambda: None, Control())
        nightly = ref(operations, NIGHTLY_REF)
        assert result["commit"] == nightly == state["commit"] and git(operations, "rev-list", "--parents", "-n", "1", NIGHTLY_REF) == f"{nightly} {fresh.bootstrap}"
        assert git(operations, "rev-list", "--count", NIGHTLY_REF) == "2"       # the retry made the same commit, not a duplicate chain
        assert ref(operations, TABLES_REF) is None
        fresh.untouched()

        created = fresh.call("global", "tables_create", {"title": "Metrics", "columns": COLUMNS})
        fresh.run(created["job_id"])
        tables = ref(operations, TABLES_REF)
        # The table history is a root of its own: neither the nightly commit nor the bootstrap commit is its ancestor.
        assert git(operations, "rev-list", "--parents", "-n", "1", TABLES_REF) == tables
        assert not is_ancestor(operations, nightly, TABLES_REF) and not is_ancestor(operations, fresh.bootstrap, TABLES_REF)
        assert ref(operations, NIGHTLY_REF) == nightly
        assert names(operations, TABLES_REF) == [f"files/tables/{created['table']['id']}.json"]
        fresh.untouched()
        assert fresh.checkout_snapshot() == fresh.checkout_state          # the surrounding checkout: no ref, object, HEAD or index change
    finally:
        fresh.close()


def test_an_unborn_head_is_why_the_bootstrap_commit_exists(tmp_path):
    """The negative control: the nightly Git stage cannot make its first publication from a repository with no commit, while the
    table publisher's own empty-index start does not need one. This is baseline behavior, not a regression of the relocation."""
    root = tmp_path / "operations"
    root.mkdir(mode=0o700)
    git(root, "init", "--quiet", "--initial-branch=qorqut-instance")
    assert ref(root, "HEAD") is None
    source, metadata, bundle = generation(tmp_path, "gen-1", files=authored(), exports={"exports/manifest.json": b"{}\n"})
    with pytest.raises(DomainError) as unborn:
        GitVersioner(root).version(source, metadata, bundle, {}, lambda: None, Control())
    assert unborn.value.code == "versioning_failed" and ref(root, NIGHTLY_REF) is None and ref(root, "HEAD") is None
