"""Local-provenance independent-engine lifecycle and privileged artifact placement.

Synthetic temporary repositories and fake executors only: no real sudo, systemd,
provider, remote or original-installation path is ever touched. The privileged
commands are recorded, then run as the test user with the sudo prefix removed.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import types

import pytest

from bazarlyk.release_worker import artifact_placement, gitops, local_integration
from bazarlyk.release_worker.adapters import SubprocessExecutor
from bazarlyk.release_worker.errors import ReconciliationRequired, ReleaseError
from bazarlyk.release_worker.manifest import plan_manifest
from bazarlyk.release_worker.worker import ReleaseWorker
from test_release_worker import git_repo, manifest as remote_manifest

DEPLOY = (
    "import json,sys; from pathlib import Path; "
    "from bazarlyk.release_worker.adapters import _tree_digest; "
    "release=Path(sys.argv[2]); "
    "Path(sys.argv[1]).write_text(json.dumps(dict(schema_version=1,"
    "operation='deploy',outcome='succeeded',component='engine',"
    "release_id=release.name,release=str(release),plan_digest=sys.argv[3],"
    "source_commit=sys.argv[4],artifact_digest=_tree_digest(release))))"
)
WRONG_PLAN_DEPLOY = DEPLOY.replace("plan_digest=sys.argv[3]", "plan_digest='sha256:other'")
INTEGRATION = "refs/heads/release/integration"
STEPS = ("check", "build", "stage", "merge", "deploy")


def git(repo, *args):
    return subprocess.check_output(["git", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                                    "-C", str(repo), *args], text=True).strip()


def commit_file(repo, name, text, message):
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def world(tmp_path):
    """An independent engine repository whose fix was really merged (no-ff) into release/integration."""

    w = types.SimpleNamespace(root=tmp_path, workspace=tmp_path / "workspace", releases=tmp_path / "releases",
                              locks=tmp_path / "locks", receipt=tmp_path / "receipt.json")
    w.workspace.mkdir()
    w.releases.mkdir()
    w.releases.chmod(0o755)
    w.repo = w.workspace / "engine"
    w.repo.mkdir()
    git(w.repo, "init", "-q", "-b", "release/integration")
    git(w.repo, "config", "user.name", "Engine Test")
    git(w.repo, "config", "user.email", "engine@example.invalid")
    commit_file(w.repo, "engine_email/__init__.py", "VERSION = 1\n", "base")
    (w.repo / "deploy").mkdir()
    script = w.repo / "deploy/run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    git(w.repo, "add", "deploy/run.sh")
    w.base = commit_file(w.repo, "README.md", "engine\n", "docs")
    for protected in ("main", "release/base", "qorqut-tables"):
        git(w.repo, "branch", protected, w.base)
    git(w.repo, "checkout", "-q", "-b", "work/engine-fix")
    w.source = commit_file(w.repo, "engine_email/__init__.py", "VERSION = 2\n", "fix")
    git(w.repo, "checkout", "-q", "release/integration")
    git(w.repo, "merge", "-q", "--no-ff", "-m", "Merge the engine fix", "work/engine-fix")
    w.merge = git(w.repo, "rev-parse", "HEAD")
    git(w.repo, "checkout", "-q", "work/engine-fix")
    return w


def engine_manifest(w, *, sudo=False, release_id="engine-local-1", deploy=DEPLOY):
    recipe = {
        "host": "localhost", "service": "fixture-engine.service", "release_root": str(w.releases),
        "health": [], "backup": str(w.root / "backups"), "configuration": [],
        "receipt_path": str(w.receipt),
        "deploy_command": [sys.executable, "-c", deploy, str(w.receipt), "{release}", "{plan_digest}", "{source_commit}"],
        "health_command": [sys.executable, "-c", "print('healthy')", "{release}"],
        "inspect_command": [sys.executable, "-c", "print('fixture runtime')", "{release}"],
    }
    if sudo:
        recipe["sudo_artifact_placement"] = True
    return {
        "schema_version": 1, "release_id": release_id,
        # The lock must not live below the (root-owned) release root.
        "installation": {"id": "engine-" + w.root.name, "host": "localhost", "lock_path": str(w.locks / "engine.lock")},
        "components": [{
            "id": "engine", "category": "engine", "repository": str(w.repo),
            "source_ref": "refs/heads/work/engine-fix", "source_commit": w.source,
            "target_branch": "release/integration",
            "local_integration": {"workspace_root": str(w.workspace), "integrated_ref": INTEGRATION},
            "tests": [], "deployment_recipe": recipe,
        }],
    }


class FakeExecutor(SubprocessExecutor):
    """Records direct argv; `sudo -n` is stripped and the command runs as the test user."""

    def __init__(self, *, forbid_copy_tree=False, copy_hook=None):
        self.commands = []
        self.forbid_copy_tree = forbid_copy_tree
        self.copy_hook = copy_hook

    @property
    def sudo_commands(self):
        return [command for command in self.commands if command[:2] == ["sudo", "-n"]]

    def run(self, argv, *, check=True):
        self.commands.append(list(argv))
        if argv[:2] == ["sudo", "-n"]:
            command = list(argv[2:])
            if command[0] == "/usr/bin/cp" and self.copy_hook:
                return self.copy_hook(command)
            return super().run(command, check=check)
        return super().run(argv, check=check)

    def copy_tree(self, source, destination):
        if self.forbid_copy_tree:
            raise AssertionError("privileged placement must not use the unprivileged copy")
        super().copy_tree(source, destination)


@pytest.fixture
def trusted_owner(monkeypatch):
    # The synthetic release root is owned by the test user, standing in for root.
    monkeypatch.setattr(artifact_placement, "ROOT_UID", os.getuid())


@pytest.fixture
def no_origin(monkeypatch):
    """Any remote-backed gitops command (fetch/push/ls-remote/worktree/branch) fails the test."""

    def forbidden(*args, **kwargs):
        raise AssertionError(f"remote-backed Git operation attempted: {args}")

    monkeypatch.setattr(gitops, "git", forbidden)
    monkeypatch.setattr(gitops, "subprocess", types.SimpleNamespace(run=forbidden))


def snapshot(repo):
    base = Path(repo) / ".git"
    return {str(path.relative_to(base)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(base.rglob("*")) if path.is_file()}


def authorize(worker, planned, tmp_path, *scopes):
    worker.initialize()
    for scope in scopes:
        evidence = tmp_path / f"{scope}-authorization.json"
        evidence.write_text(json.dumps({"scope": scope, "plan_digest": planned["plan_digest"],
                                        "evidence": "synthetic owner approval for the test"}))
        worker.authorize(scope, evidence)


def call(worker, step):
    return getattr(worker, step)(confirm=True) if step in {"merge", "deploy"} else getattr(worker, step)()


def started(tmp_path, *, sudo=False, executor=None, **kwargs):
    w = world(tmp_path)
    planned = plan_manifest(engine_manifest(w, sudo=sudo, **kwargs))
    executor = executor or FakeExecutor(forbid_copy_tree=sudo)
    worker = ReleaseWorker(planned, records_root=tmp_path / "records", executor=executor)
    authorize(worker, planned, tmp_path, "merge", "deploy", "cleanup")
    return w, planned, worker, executor


def release_of(w):
    return w.releases / "engine-local-1"


# --- planning and the full lifecycle -------------------------------------------------------------

def test_plan_freezes_the_real_integration_identity(tmp_path):
    w = world(tmp_path)
    component = plan_manifest(engine_manifest(w))["components"][0]
    tree = git(w.repo, "rev-parse", w.source + "^{tree}")
    assert w.merge != w.source and git(w.repo, "rev-parse", w.merge + "^{tree}") == tree
    assert component["base_commit"] == w.merge
    assert component["source_commit"] == w.source
    assert component["source_tree"] == component["target_tree"] == tree
    assert component["checked_out_branch"] == "work/engine-fix"
    assert (component["target_branch"], component["provenance"], component["remote_verified"],
            component["git_mutated"], component["dirty"]) == ("release/integration", "local-integrated", False, False, False)
    assert component["repository"] == str(w.repo)


def test_local_engine_lifecycle_never_uses_origin_or_mutates_git(tmp_path, no_origin):
    w = world(tmp_path)
    refs, files = git(w.repo, "show-ref"), snapshot(w.repo)
    planned = plan_manifest(engine_manifest(w))
    executor = FakeExecutor()
    worker = ReleaseWorker(planned, records_root=tmp_path / "records", executor=executor)
    worker.check()
    worker.build()
    worker.stage()
    with pytest.raises(ReleaseError, match="recorded owner authorization"):
        worker.merge(confirm=False)
    with pytest.raises(ReleaseError, match="no current recorded merge authorization"):
        worker.merge(confirm=True)
    authorize(worker, planned, tmp_path, "merge", "deploy")
    result = worker.merge(confirm=True)["results"]["engine"]
    tree = git(w.repo, "rev-parse", w.source + "^{tree}")
    assert result == {
        "merged": False, "already_integrated": True, "provenance": "local-integrated",
        "remote_verified": False, "git_mutated": False, "target": "release/integration",
        "integrated_ref": INTEGRATION, "source_branch": "work/engine-fix", "source_commit": w.source,
        "source_tree": tree, "base_commit": w.merge, "target_tree": tree, "merge_commit": w.merge,
    }
    with pytest.raises(ReleaseError, match="deploy requires explicit confirmation"):
        worker.deploy(confirm=False)
    worker.deploy(confirm=True)
    status = worker.status()["steps"]
    assert {key: value["status"] for key, value in status.items() if key.endswith(":engine")} == {
        f"{step}:engine": "succeeded" for step in STEPS}
    assert json.loads(w.receipt.read_text())["source_commit"] == w.source
    assert (release_of(w) / "engine_email/__init__.py").read_text() == "VERSION = 2\n"
    assert not executor.sudo_commands
    assert ["mkdir", "-p", str(release_of(w))] in executor.commands
    assert git(w.repo, "show-ref") == refs and snapshot(w.repo) == files
    assert {"main", "release/base", "qorqut-tables"} <= set(git(w.repo, "for-each-ref", "--format=%(refname:short)").splitlines())


def test_manifest_without_the_field_still_verifies_against_origin(tmp_path, monkeypatch):
    repo, _ = git_repo(tmp_path)
    component = plan_manifest(remote_manifest(repo))["components"][0]
    assert "local_integration" not in component and "provenance" not in component
    seen = []
    real = gitops.git
    monkeypatch.setattr(gitops, "git", lambda repo, *args, **kwargs: (seen.append(args), real(repo, *args, **kwargs))[1])
    intent = gitops.prepare_merge(component, confirm=True)
    assert ("fetch", "origin", "main", "feature/release") in seen
    assert "provenance" not in intent and intent["target"] == "main"


def test_the_declared_target_is_frozen_with_the_plan(tmp_path, no_origin):
    w = world(tmp_path)
    git(w.repo, "branch", "other/integration", w.merge)
    component = plan_manifest(engine_manifest(w))["components"][0]
    assert (component["integrated_ref"], component["target_branch"]) == (INTEGRATION, "release/integration")
    retargeted = copy.deepcopy(component)
    retargeted["local_integration"]["integrated_ref"] = "refs/heads/other/integration"
    retargeted["target_branch"] = "other/integration"
    for step in (local_integration.verify_frozen, local_integration.merge_intent):
        with pytest.raises(ReleaseError, match="local integrated_ref changed after planning"):
            step(retargeted)


def test_plan_digest_binds_the_placement_flag(tmp_path):
    w = world(tmp_path)
    plain = plan_manifest(engine_manifest(w))
    privileged = plan_manifest(engine_manifest(w, sudo=True))
    assert plain["plan_digest"] != privileged["plan_digest"]
    assert privileged["components"][0]["deployment_recipe"]["sudo_artifact_placement"] is True


# --- planning rejections -----------------------------------------------------------------------

def unintegrated(w, m):
    w.new = commit_file(w.repo, "extra.txt", "not integrated\n", "unintegrated work")
    m["components"][0]["source_commit"] = w.new


def different_tree(w, m):
    git(w.repo, "checkout", "-q", "release/integration")
    commit_file(w.repo, "later.txt", "later integration work\n", "later")
    git(w.repo, "checkout", "-q", "work/engine-fix")


def dirty(w, m):
    (w.repo / "README.md").write_text("dirty\n")


def untracked(w, m):
    (w.repo / "scratch").write_text("untracked\n")


def wrong_sha(w, m):
    m["components"][0]["source_commit"] = w.base


def remote_present(w, m):
    git(w.repo, "remote", "add", "origin", str(w.root / "nowhere.git"))


def alternates(w, m):
    (w.repo / ".git/objects/info/alternates").write_text(str(w.root / "other-objects") + "\n")


def symlinked_repository(w, m):
    (w.workspace / "link").symlink_to(w.repo)
    m["components"][0]["repository"] = str(w.workspace / "link")


def symlinked_git_dir(w, m):
    (w.repo / ".git").rename(w.workspace / "gitdata")
    (w.repo / ".git").symlink_to(w.workspace / "gitdata")


def common_dir_outside_workspace(w, m):
    outside = w.root / "outside"
    outside.mkdir()
    shutil.move(str(w.repo), str(outside / "engine"))
    git(outside / "engine", "checkout", "-q", "release/integration")
    git(outside / "engine", "worktree", "add", "-q", str(w.repo), "work/engine-fix")


def on_integration(w, m):
    git(w.repo, "checkout", "-q", "release/integration")
    m["components"][0].update(source_ref="HEAD", source_commit=w.merge)


def source_ref_elsewhere(w, m):
    git(w.repo, "branch", "work/elsewhere", w.base)
    m["components"][0]["source_ref"] = "refs/heads/work/elsewhere"


def source_ref_is_the_target(w, m):
    m["components"][0]["source_ref"] = INTEGRATION


def source_ref_missing(w, m):
    m["components"][0]["source_ref"] = "refs/heads/work/does-not-exist"


def source_ref_protected(name):
    def mutate(w, m):
        m["components"][0]["source_ref"] = name
    return mutate


def component_field(**changes):
    def mutate(w, m):
        for key, value in changes.items():
            if value is KeyError:
                m["components"][0].pop(key, None)
            else:
                m["components"][0][key] = value
    return mutate


def declaration(**changes):
    def mutate(w, m):
        selection = m["components"][0]["local_integration"]
        for key, value in changes.items():
            if value is KeyError:
                selection.pop(key)
            else:
                selection[key] = value
    return mutate


def recipe_field(**changes):
    def mutate(w, m):
        for key, value in changes.items():
            if value is KeyError:
                m["components"][0]["deployment_recipe"].pop(key, None)
            else:
                m["components"][0]["deployment_recipe"][key] = value
    return mutate


def installation_host(value):
    def mutate(w, m):
        if value is KeyError:
            m["installation"].pop("host")
        else:
            m["installation"]["host"] = value
    return mutate


def without_local_integration(w, m):
    m["components"][0].pop("local_integration")


def outside_workspace(w, m):
    other = w.root / "elsewhere"
    other.mkdir()
    m["components"][0]["local_integration"]["workspace_root"] = str(other)


REJECTIONS = [
    ("unintegrated", unintegrated, "already merged into local integration"),
    ("different tree", different_tree, "content differs from the selected source"),
    ("dirty", dirty, "must be clean"),
    ("untracked", untracked, "must be clean"),
    ("wrong sha", wrong_sha, "frozen commit"),
    ("short sha", component_field(source_commit=" abc"), "explicit full source_commit"),
    ("missing sha", component_field(source_commit=KeyError), "explicit full source_commit"),
    ("remote", remote_present, "remotes"),
    ("alternates", alternates, "independent object stores"),
    ("symlinked repository", symlinked_repository, "symbolic links"),
    ("symlinked .git", symlinked_git_dir, "symbolic links"),
    ("common dir outside workspace", common_dir_outside_workspace, "inside the selected independent workspace"),
    ("source branch is integration", on_integration, "must differ from release/integration"),
    ("source ref elsewhere", source_ref_elsewhere, "no longer resolves"),
    ("source ref missing", source_ref_missing, "Local source verification failed"),
    ("source ref is the target", source_ref_is_the_target, "must differ from release/integration"),
    ("source ref protected", source_ref_protected("main"), "dedicated source branch"),
    ("source ref protected base", source_ref_protected("refs/heads/release/base"), "dedicated source branch"),
    ("source ref history", source_ref_protected("qorqut-tables"), "dedicated source branch"),
    ("source ref option", source_ref_protected("--detach"), "dedicated source branch"),
    ("source ref malformed", source_ref_protected("refs/heads/work/.."), "dedicated source branch"),
    ("outside workspace", outside_workspace, "inside the selected independent workspace"),
    ("filesystem root workspace", declaration(workspace_root="/"), "filesystem root"),
    ("relative workspace", declaration(workspace_root="workspace"), "absolute"),
    ("parent traversal", declaration(workspace_root="/tmp/../tmp"), "absolute and normalized"),
    ("unnormalized repository", lambda w, m: m["components"][0].update(repository=str(w.workspace) + "//engine"),
     "normalized absolute path"),
    ("non-string workspace", declaration(workspace_root=7), "normalized absolute path"),
    ("integrated ref main", declaration(integrated_ref="refs/heads/main"), "protected branch main"),
    ("integrated ref master", declaration(integrated_ref="refs/heads/master"), "protected branch master"),
    ("integrated ref base", declaration(integrated_ref="refs/heads/base"), "protected branch base"),
    ("integrated ref nested base", declaration(integrated_ref="refs/heads/release/base"), "protected branch release/base"),
    ("integrated ref history", declaration(integrated_ref="refs/heads/qorqut-tables"), "protected branch qorqut-tables"),
    ("integrated ref origin", declaration(integrated_ref="refs/remotes/origin/release/integration"),
     "integrated_ref must be a full refs/heads/<branch> ref"),
    ("integrated ref short", declaration(integrated_ref="release/integration"), "integrated_ref must be a full refs/heads/<branch> ref"),
    ("integrated ref malformed", declaration(integrated_ref="refs/heads/release/.."), "not a valid Git branch ref"),
    ("integrated ref lock suffix", declaration(integrated_ref="refs/heads/release/integration.lock"), "not a valid Git branch ref"),
    ("integrated ref not a string", declaration(integrated_ref=7), "integrated_ref must be a full refs/heads/<branch> ref"),
    ("extra key", declaration(fallback="origin"), "exactly workspace_root and integrated_ref"),
    ("missing key", declaration(workspace_root=KeyError), "exactly workspace_root and integrated_ref"),
    ("not an object", component_field(local_integration="refs/heads/release/integration"), "exactly workspace_root"),
    ("null", component_field(local_integration=None), "exactly workspace_root"),
    ("documentation category", component_field(category="documentation"), "only supported for the engine category"),
    ("outreach category", component_field(category="outreach"), "only supported for the engine category"),
    ("plugin category", component_field(category="plugin"), "only supported for the engine category"),
    ("other adapter", component_field(adapter="documentation"), "only supported for the engine category"),
    ("main target", component_field(target_branch="main"), "requires target_branch release/integration"),
    ("default target", component_field(target_branch=KeyError), "requires target_branch release/integration"),
    ("target differs from the integrated ref", component_field(target_branch="release/other"),
     "requires target_branch release/integration"),
    ("unchanged", component_field(unchanged=True), "unchanged"),
    ("placement not a boolean", recipe_field(sudo_artifact_placement="yes"), "must be a boolean"),
    ("placement as integer", recipe_field(sudo_artifact_placement=1), "must be a boolean"),
    ("placement without local integration", lambda w, m: (without_local_integration(w, m), recipe_field(sudo_artifact_placement=True)(w, m)),
     "only supported with local_integration"),
    ("placement false without local integration", lambda w, m: (without_local_integration(w, m), recipe_field(sudo_artifact_placement=False)(w, m)),
     "only supported with local_integration"),
    ("placement with default installation host", lambda w, m: (recipe_field(sudo_artifact_placement=True)(w, m), installation_host(KeyError)(w, m)),
     "requires installation.host and the recipe host to be localhost"),
    ("placement with remote installation host", lambda w, m: (recipe_field(sudo_artifact_placement=True)(w, m), installation_host("engine-host")(w, m)),
     "requires installation.host and the recipe host to be localhost"),
    ("placement with remote recipe host", recipe_field(sudo_artifact_placement=True, host="engine-host"),
     "requires installation.host and the recipe host to be localhost"),
    ("placement with default recipe host", recipe_field(sudo_artifact_placement=True, host=KeyError),
     "requires installation.host and the recipe host to be localhost"),
]


@pytest.mark.parametrize("mutate,match", [(row[1], row[2]) for row in REJECTIONS], ids=[row[0] for row in REJECTIONS])
def test_planning_rejects_unsafe_local_declarations(tmp_path, mutate, match):
    w = world(tmp_path)
    value = engine_manifest(w)
    mutate(w, value)
    with pytest.raises(ReleaseError, match=match):
        plan_manifest(value)


def test_placement_flag_is_rejected_on_a_non_engine_component(tmp_path):
    repo, _ = git_repo(tmp_path)
    value = remote_manifest(repo)
    value["installation"]["host"] = "localhost"
    value["components"][0]["deployment_recipe"] = {"host": "localhost", "sudo_artifact_placement": True}
    with pytest.raises(ReleaseError, match="only supported with local_integration"):
        plan_manifest(value)


# --- drift after planning ----------------------------------------------------------------------

def tip_advanced_same_tree(w):
    git(w.repo, "checkout", "-q", "release/integration")
    git(w.repo, "commit", "-q", "--allow-empty", "-m", "advance with identical content")
    git(w.repo, "checkout", "-q", "work/engine-fix")


def tip_content_changed(w):
    git(w.repo, "checkout", "-q", "release/integration")
    commit_file(w.repo, "later.txt", "x\n", "later")
    git(w.repo, "checkout", "-q", "work/engine-fix")


def head_moved(w):
    commit_file(w.repo, "more.txt", "x\n", "more source work")


def branch_switched(w):
    git(w.repo, "checkout", "-q", "-b", "work/other-name")


def detached(w):
    git(w.repo, "checkout", "-q", "--detach")


def remote_added(w):
    git(w.repo, "remote", "add", "origin", str(w.root / "nowhere.git"))


def dirty_after(w):
    (w.repo / "README.md").write_text("dirty after planning\n")


def untracked_after(w):
    (w.repo / "scratch").write_text("untracked\n")


def alternates_after(w):
    (w.repo / ".git/objects/info/alternates").write_text(str(w.root / "other-objects") + "\n")


def integration_ref_deleted(w):
    git(w.repo, "branch", "-q", "-m", "release/integration", "release/integration-renamed")


DRIFTS = {
    "tip advanced with identical content": tip_advanced_same_tree, "tip content changed": tip_content_changed,
    "head moved": head_moved, "branch switched": branch_switched, "detached": detached, "remote added": remote_added,
    "dirty": dirty_after, "untracked": untracked_after, "alternates": alternates_after,
    "integration ref removed": integration_ref_deleted,
}


@pytest.mark.parametrize("step", STEPS)
@pytest.mark.parametrize("drift", ["tip advanced with identical content", "dirty", "head moved"])
def test_every_step_revalidates_the_frozen_source(tmp_path, step, drift):
    w, planned, worker, executor = started(tmp_path, sudo=True)
    for earlier in STEPS[:STEPS.index(step)]:
        call(worker, earlier)
    DRIFTS[drift](w)
    with pytest.raises(ReleaseError):
        call(worker, step)
    assert not executor.sudo_commands and not release_of(w).exists() and not w.receipt.exists()


@pytest.mark.parametrize("drift", DRIFTS)
def test_check_rejects_each_kind_of_drift(tmp_path, drift):
    w, planned, worker, _ = started(tmp_path)
    DRIFTS[drift](w)
    with pytest.raises(ReleaseError):
        worker.check()
    assert worker.status()["steps"].get("check:engine", {}).get("status") != "succeeded"


def test_drift_after_the_merge_step_still_blocks_deploy(tmp_path, trusted_owner):
    w, planned, worker, executor = started(tmp_path, sudo=True)
    for step in STEPS[:4]:
        call(worker, step)
    tip_advanced_same_tree(w)
    with pytest.raises(ReleaseError, match="base_commit changed after planning; create a new plan"):
        worker.deploy(confirm=True)
    assert not executor.sudo_commands and not release_of(w).exists()


# --- merge, resume and cleanup -------------------------------------------------------------------

def test_resumed_local_merge_is_reconciled_read_only_and_truthfully(tmp_path, monkeypatch, no_origin):
    w, planned, worker, _ = started(tmp_path)
    for step in ("check", "build", "stage"):
        call(worker, step)
    real = local_integration.merge_result
    monkeypatch.setattr(local_integration, "merge_result",
                        lambda component, intent: (_ for _ in ()).throw(ReleaseError("interrupted after intent was recorded")))
    with pytest.raises(ReleaseError, match="interrupted"):
        worker.merge(confirm=True)
    assert worker.status()["steps"]["merge:engine"]["after"] == {"external_effect": "unknown"}
    with pytest.raises(ReconciliationRequired):
        worker.merge(confirm=True)
    monkeypatch.setattr(local_integration, "merge_result", real)
    refs = git(w.repo, "show-ref")
    step = worker.resume()["steps"]["merge:engine"]
    assert step["status"] == "succeeded"
    assert step["after"] == {**local_integration.merge_intent(planned["components"][0]),
                             "already_succeeded": True, "safe_to_retry": False, "external_effect": False}
    assert step["after"]["merged"] is False and step["after"]["remote_verified"] is False and step["after"]["git_mutated"] is False
    assert git(w.repo, "show-ref") == refs
    assert worker.merge(confirm=True)["results"]["engine"] == step["after"]


def test_resumed_local_merge_refuses_drift_and_never_retries(tmp_path, monkeypatch, no_origin):
    w, planned, worker, _ = started(tmp_path)
    for step in ("check", "build", "stage"):
        call(worker, step)
    real = local_integration.merge_result
    monkeypatch.setattr(local_integration, "merge_result",
                        lambda component, intent: (_ for _ in ()).throw(ReleaseError("interrupted")))
    with pytest.raises(ReleaseError):
        worker.merge(confirm=True)
    monkeypatch.setattr(local_integration, "merge_result", real)
    tip_advanced_same_tree(w)
    with pytest.raises(ReconciliationRequired, match="does not match the frozen plan"):
        worker.resume()
    assert worker.status()["steps"]["merge:engine"]["status"] == "failed"


TAMPERED = {
    "merge commit": ("merge_commit", "0" * 40), "source commit": ("source_commit", "1" * 40),
    "base commit": ("base_commit", "2" * 40), "target tree": ("target_tree", "3" * 40),
    "source tree": ("source_tree", "4" * 40), "merged": ("merged", True), "not integrated": ("already_integrated", False),
    "git mutated": ("git_mutated", True), "remote verified": ("remote_verified", True),
    "provenance": ("provenance", "remote"), "target": ("target", "main"), "branch": ("source_branch", "main"),
    "extra key": ("fetched", True),
}


@pytest.mark.parametrize("key,value", TAMPERED.values(), ids=list(TAMPERED))
def test_the_complete_local_intent_is_validated(tmp_path, no_origin, key, value):
    w = world(tmp_path)
    component = plan_manifest(engine_manifest(w))["components"][0]
    intent = local_integration.merge_intent(component)
    assert gitops.merge_component(component, confirm=True, intent=copy.deepcopy(intent)) == intent
    tampered = {**intent, key: value}
    with pytest.raises(ReleaseError, match="no longer matches"):
        gitops.merge_component(component, confirm=True, intent=tampered)
    observed = gitops.reconcile_merge(component, tampered)
    assert observed["safe_to_retry"] is False and "already_succeeded" not in observed
    missing = {name: item for name, item in intent.items() if name != "merge_commit"}
    with pytest.raises(ReleaseError, match="no longer matches"):
        gitops.merge_component(component, confirm=True, intent=missing)


def test_remote_backed_components_refuse_local_evidence_before_any_fetch(tmp_path, no_origin):
    w = world(tmp_path)
    component = plan_manifest(engine_manifest(w))["components"][0]
    intent = local_integration.merge_intent(component)
    remote_component = {key: item for key, item in component.items() if key != "local_integration"}
    with pytest.raises(ReleaseError, match="remote-backed"):
        gitops.merge_component(remote_component, confirm=True, intent=intent)
    with pytest.raises(ReleaseError, match="remote-backed"):
        gitops.reconcile_merge(remote_component, intent)


def test_local_cleanup_is_unsupported_before_any_remote_or_ref_operation(tmp_path, no_origin):
    w, planned, worker, executor = started(tmp_path)
    component = planned["components"][0]
    refs = git(w.repo, "show-ref")
    fake_intent = {"worktree": str(w.repo), "task_directory": str(w.workspace), "common_repo": str(w.repo),
                   "branch": "work/engine-fix", "head": w.source, "remote_head": w.source, "target": "release/integration"}
    with pytest.raises(ReleaseError, match="cleanup is unsupported for local-integrated sources"):
        gitops.prepare_cleanup(component, confirm=True)
    with pytest.raises(ReleaseError, match="cleanup is unsupported"):
        gitops.cleanup_component(component, confirm=True)
    with pytest.raises(ReleaseError, match="cleanup is unsupported"):
        gitops.cleanup_component(component, confirm=True, intent=fake_intent)
    with pytest.raises(ReleaseError, match="cleanup is unsupported"):
        gitops.reconcile_cleanup(component, fake_intent)
    with pytest.raises(ReleaseError, match="cleanup is unsupported"):
        worker.cleanup(confirm=True)
    worker.store.step_started("cleanup:engine", before={"component_ids": ["engine"], "cleanup_intent": fake_intent,
                                                         "plan_digest": planned["plan_digest"]})
    with pytest.raises(ReconciliationRequired, match="cleanup is unsupported"):
        worker.resume()
    assert git(w.repo, "show-ref") == refs and w.repo.exists() and not executor.sudo_commands


# --- privileged artifact placement ---------------------------------------------------------------

def test_privileged_placement_uses_only_the_three_exact_commands(tmp_path, trusted_owner, no_origin):
    w, planned, worker, executor = started(tmp_path, sudo=True)
    for step in STEPS[:4]:
        call(worker, step)
    assert not executor.sudo_commands
    worker.deploy(confirm=True)
    artifact = tmp_path / "records/engine-local-1/artifacts/engine"
    release = release_of(w)
    assert executor.sudo_commands == [
        ["sudo", "-n", "/usr/bin/mkdir", "--mode=0755", "--", str(release)],
        ["sudo", "-n", "/usr/bin/cp", "--recursive", "--no-dereference", "--preserve=mode,timestamps",
         "--no-preserve=ownership", "--", f"{artifact}/.", f"{release}/"],
        ["sudo", "-n", "/usr/bin/chmod", "0755", "--", str(release)],
    ]
    assert ["mkdir", "-p", str(release)] not in executor.commands
    positions = [executor.commands.index(command) for command in executor.sudo_commands]
    recipe_at = next(index for index, command in enumerate(executor.commands) if command[:3] == [sys.executable, "-c", DEPLOY])
    assert positions == sorted(positions) and positions[-1] < recipe_at
    assert (release / "deploy/run.sh").stat().st_mode & 0o111
    # The private (0700) staging root's mode is restored to the deliberate public mode.
    assert (release.stat().st_mode & 0o7777, artifact.stat().st_mode & 0o7777) == (0o755, 0o700)
    assert worker.status()["steps"]["deploy:engine"]["status"] == "succeeded"
    assert json.loads(w.receipt.read_text())["artifact_digest"] == worker.status()["steps"]["stage:engine"]["after"]["tree_digest"]


def tree_state(root):
    """Mode, mtime and content of every entry below ``root`` (the root itself excluded)."""

    return {str(path.relative_to(root)): (path.lstat().st_mode, path.lstat().st_mtime_ns,
                                          path.read_bytes() if path.is_file() else None)
            for path in sorted(root.rglob("*"))}


def test_real_copy_replaces_the_root_mode_and_only_the_root_is_restored(tmp_path):
    # Real mkdir/cp/chmod in a temporary tree, as the test user: no sudo prefix is run.
    executor, artifact, root = place_fixture(tmp_path)
    artifact.chmod(0o700)
    (artifact / "pkg").chmod(0o750)
    (artifact / "pkg/module.py").chmod(0o640)
    (artifact / "tool.sh").write_text("#!/bin/sh\n")
    (artifact / "tool.sh").chmod(0o755)
    before_children, before_root = tree_state(artifact), artifact.stat().st_mode
    release = root / "release-1"
    create, copy, restore = (command[2:] for command in artifact_placement.commands(artifact, release))
    subprocess.run(create, check=True)
    assert release.stat().st_mode & 0o7777 == 0o755
    subprocess.run(copy, check=True)
    # GNU cp applies the private staging root's mode to the new root ...
    assert release.stat().st_mode & 0o7777 == 0o700
    subprocess.run(restore, check=True)
    assert release.stat().st_mode & 0o7777 == 0o755
    # ... while every child keeps its copied mode and content, and the source is untouched.
    assert {name: (mode, data) for name, (mode, _, data) in tree_state(release).items()} == \
        {name: (mode, data) for name, (mode, _, data) in before_children.items()}
    assert tree_state(artifact) == before_children and artifact.stat().st_mode == before_root
    assert (release / "pkg").stat().st_mode & 0o7777 == 0o750 and (release / "pkg/module.py").stat().st_mode & 0o7777 == 0o640


def test_unflagged_plan_keeps_the_unprivileged_copy(tmp_path):
    w, planned, worker, executor = started(tmp_path, sudo=False)
    for step in STEPS[:4]:
        call(worker, step)
    worker.deploy(confirm=True)
    assert not executor.sudo_commands
    assert ["test", "!", "-e", str(release_of(w))] in executor.commands


def staged(tmp_path, *, sudo=True, executor=None, **kwargs):
    w, planned, worker, executor = started(tmp_path, sudo=sudo, executor=executor, **kwargs)
    for step in STEPS[:4]:
        call(worker, step)
    return w, planned, worker, executor


def assert_untouched_destination(w, executor):
    assert not executor.sudo_commands
    assert not release_of(w).exists() and not w.receipt.exists()


def test_existing_destination_is_never_overwritten(tmp_path, trusted_owner):
    w, planned, worker, executor = staged(tmp_path)
    release_of(w).mkdir()
    (release_of(w) / "keep").write_text("existing\n")
    with pytest.raises(ReleaseError, match="release already exists"):
        worker.deploy(confirm=True)
    assert not executor.sudo_commands and (release_of(w) / "keep").read_text() == "existing\n" and not w.receipt.exists()


def test_dangling_symlink_destination_counts_as_existing(tmp_path, trusted_owner):
    w, planned, worker, executor = staged(tmp_path)
    release_of(w).symlink_to(tmp_path / "missing-target")
    with pytest.raises(ReleaseError, match="release already exists"):
        worker.deploy(confirm=True)
    assert not executor.sudo_commands and release_of(w).is_symlink() and not (tmp_path / "missing-target").exists()


@pytest.mark.skipif(os.getuid() == 0, reason="the synthetic root must differ from the real root")
def test_release_root_must_be_root_owned(tmp_path):
    w, planned, worker, executor = staged(tmp_path)
    with pytest.raises(ReleaseError, match="root-owned and not group/world writable"):
        worker.deploy(confirm=True)
    assert_untouched_destination(w, executor)


@pytest.mark.parametrize("mode", [0o775, 0o757])
def test_release_root_must_not_be_group_or_world_writable(tmp_path, trusted_owner, mode):
    w, planned, worker, executor = staged(tmp_path)
    w.releases.chmod(mode)
    with pytest.raises(ReleaseError, match="not group/world writable"):
        worker.deploy(confirm=True)
    assert_untouched_destination(w, executor)


def test_release_root_must_be_an_existing_normal_directory(tmp_path, trusted_owner):
    w, planned, worker, executor = staged(tmp_path)
    w.releases.rmdir()
    with pytest.raises(ReleaseError, match="release root cannot be inspected"):
        worker.deploy(confirm=True)
    assert not executor.sudo_commands and not w.receipt.exists()


def place_fixture(tmp_path):
    root = tmp_path / "releases"
    root.mkdir(exist_ok=True)
    root.chmod(0o755)
    artifact = tmp_path / "records" / "artifact"
    (artifact / "pkg").mkdir(parents=True)
    (artifact / "pkg/module.py").write_text("x = 1\n")
    return FakeExecutor(forbid_copy_tree=True), artifact, root


def test_placement_accepts_a_regular_artifact(tmp_path, trusted_owner):
    executor, artifact, root = place_fixture(tmp_path)
    release = artifact_placement.place(executor, artifact, str(root), "release-1")
    assert release == root / "release-1" and (release / "pkg/module.py").read_text() == "x = 1\n"
    assert len(executor.sudo_commands) == 3


def make_symlink(path):
    (path / "pkg/link").symlink_to("module.py")


def make_fifo(path):
    os.mkfifo(path / "pkg/queue")


def make_setuid(path):
    (path / "pkg/module.py").chmod(0o4755)


def make_setgid_file(path):
    (path / "pkg/module.py").chmod(0o2755)


def make_setgid_directory(path):
    (path / "pkg").chmod(0o2755)


def make_dangling_symlink(path):
    (path / "dangling").symlink_to("/nonexistent/target")


@pytest.mark.parametrize("corrupt,match", [
    (make_symlink, "non-regular"), (make_fifo, "non-regular"), (make_dangling_symlink, "non-regular"),
    (make_setuid, "setuid/setgid"), (make_setgid_file, "setuid/setgid"), (make_setgid_directory, "setuid/setgid"),
], ids=lambda value: getattr(value, "__name__", value))
def test_placement_rejects_special_staged_entries_before_any_command(tmp_path, trusted_owner, corrupt, match):
    executor, artifact, root = place_fixture(tmp_path)
    corrupt(artifact)
    with pytest.raises(ReleaseError, match=match):
        artifact_placement.place(executor, artifact, str(root), "release-1")
    assert executor.commands == [] and not (root / "release-1").exists()


def test_placement_rejects_a_symlinked_artifact_or_ancestor(tmp_path, trusted_owner):
    executor, artifact, root = place_fixture(tmp_path)
    link = tmp_path / "linked-artifact"
    link.symlink_to(artifact)
    with pytest.raises(ReleaseError, match="symbolic links"):
        artifact_placement.place(executor, link, str(root), "release-1")
    with pytest.raises(ReleaseError, match="symbolic links"):
        artifact_placement.place(executor, link / "pkg", str(root), "release-1")
    with pytest.raises(ReleaseError, match="absolute and normalized"):
        artifact_placement.place(executor, Path("relative/artifact"), str(root), "release-1")
    assert executor.commands == []


@pytest.mark.parametrize("make_root,match", [
    (lambda tmp: "releases-relative", "absolute and normalized"),
    (lambda tmp: str(tmp / "releases" / ".." / "releases"), "absolute and normalized"),
    (lambda tmp: str(tmp / "releases") + "/", "normalized absolute path"),
    (lambda tmp: "/", "filesystem root"),
    (lambda tmp: str(tmp / "alias"), "symbolic links"),
    (lambda tmp: str(tmp / "alias" / "child"), "symbolic links"),
    (lambda tmp: str(tmp / "plain-file"), "existing normal directory"),
    (lambda tmp: str(tmp / "missing"), "cannot be inspected"),
    (lambda tmp: 7, "normalized absolute path"),
], ids=["relative", "dotdot", "trailing slash", "filesystem root", "symlink", "symlink ancestor",
        "regular file", "missing", "not a string"])
def test_placement_validates_the_release_root(tmp_path, trusted_owner, make_root, match):
    executor, artifact, root = place_fixture(tmp_path)
    (tmp_path / "alias").symlink_to(root)
    (tmp_path / "plain-file").write_text("x")
    with pytest.raises(ReleaseError, match=match):
        artifact_placement.place(executor, artifact, make_root(tmp_path), "release-1")
    assert executor.commands == []


@pytest.mark.parametrize("release_id", ["", "../escape", "Upper", "a/b", "-x", None, "x" * 97, ".hidden"])
def test_placement_validates_the_release_id(tmp_path, trusted_owner, release_id):
    executor, artifact, root = place_fixture(tmp_path)
    with pytest.raises(ReleaseError, match="validated release id"):
        artifact_placement.place(executor, artifact, str(root), release_id)
    assert executor.commands == [] and sorted(path.name for path in root.iterdir()) == []


def test_placement_is_local_only(tmp_path, trusted_owner):
    executor, artifact, root = place_fixture(tmp_path)
    executor.host = "engine-host"
    with pytest.raises(ReleaseError, match="only supported for the local executor"):
        artifact_placement.place(executor, artifact, str(root), "release-1")
    assert executor.commands == []


def test_destination_digest_mismatch_blocks_the_recipe_and_keeps_the_partial_release(tmp_path, trusted_owner):
    def tampering_copy(command):
        subprocess.run(command, check=True)
        (release_of(w) / "injected.py").write_text("print('not staged')\n")
        return ""

    executor = FakeExecutor(forbid_copy_tree=True, copy_hook=tampering_copy)
    w, planned, worker, executor = staged(tmp_path, executor=executor)
    with pytest.raises(ReleaseError, match="artifact transfer changed"):
        worker.deploy(confirm=True)
    assert len(executor.sudo_commands) == 3
    assert not w.receipt.exists()
    assert not any(command[:3] == [sys.executable, "-c", DEPLOY] for command in executor.commands)
    assert (release_of(w) / "injected.py").exists() and (release_of(w) / "engine_email/__init__.py").exists()
    with pytest.raises(ReconciliationRequired):
        worker.deploy(confirm=True)
    assert len(executor.sudo_commands) == 3


def test_mismatched_receipt_fails_the_deploy_without_cleanup(tmp_path, trusted_owner):
    w, planned, worker, executor = staged(tmp_path, deploy=WRONG_PLAN_DEPLOY)
    with pytest.raises(ReleaseError, match="effect receipt is not bound to this deploy action"):
        worker.deploy(confirm=True)
    assert worker.status()["steps"]["deploy:engine"]["status"] == "failed"
    assert (release_of(w) / "engine_email/__init__.py").exists()
    with pytest.raises(ReconciliationRequired):
        worker.deploy(confirm=True)
    with pytest.raises(ReconciliationRequired, match="postconditions are not verified"):
        worker.resume()
    assert len(executor.sudo_commands) == 3


def test_interrupted_copy_is_never_retried_blindly(tmp_path, trusted_owner):
    def interrupted_copy(command):
        (release_of(w) / "engine_email").mkdir()
        raise ReleaseError("command failed (130): sudo -n /usr/bin/cp")

    executor = FakeExecutor(forbid_copy_tree=True, copy_hook=interrupted_copy)
    w, planned, worker, executor = staged(tmp_path, executor=executor)
    with pytest.raises(ReleaseError, match="failed or was interrupted; the new directory was not removed or retried"):
        worker.deploy(confirm=True)
    step = worker.status()["steps"]["deploy:engine"]
    assert step["status"] == "failed" and step["after"] == {"external_effect": "unknown"}
    assert len(executor.sudo_commands) == 2
    with pytest.raises(ReconciliationRequired):
        worker.deploy(confirm=True)
    with pytest.raises(ReconciliationRequired, match="postconditions are not verified"):
        worker.resume()
    assert len(executor.sudo_commands) == 2 and release_of(w).is_dir() and not w.receipt.exists()


def test_failed_privileged_mkdir_attempts_no_copy(tmp_path, trusted_owner):
    class FailingMkdir(FakeExecutor):
        def run(self, argv, *, check=True):
            if argv[:3] == ["sudo", "-n", "/usr/bin/mkdir"]:
                self.commands.append(list(argv))
                raise ReleaseError("command failed (1): sudo -n /usr/bin/mkdir")
            return super().run(argv, check=check)

    w, planned, worker, executor = staged(tmp_path, executor=FailingMkdir(forbid_copy_tree=True))
    with pytest.raises(ReleaseError, match="no copy was attempted and nothing was removed"):
        worker.deploy(confirm=True)
    assert [command[2] for command in executor.sudo_commands] == ["/usr/bin/mkdir"]
    assert not release_of(w).exists() and not w.receipt.exists()


def test_failed_mode_restore_blocks_the_recipe_without_cleanup_or_retry(tmp_path, trusted_owner):
    class FailingChmod(FakeExecutor):
        def run(self, argv, *, check=True):
            if argv[:3] == ["sudo", "-n", "/usr/bin/chmod"]:
                self.commands.append(list(argv))
                raise ReleaseError("command failed (1): sudo -n /usr/bin/chmod")
            return super().run(argv, check=check)

    w, planned, worker, executor = staged(tmp_path, executor=FailingChmod(forbid_copy_tree=True))
    with pytest.raises(ReleaseError, match="privileged mode restore of .* failed or was interrupted"):
        worker.deploy(confirm=True)
    assert [command[2] for command in executor.sudo_commands] == ["/usr/bin/mkdir", "/usr/bin/cp", "/usr/bin/chmod"]
    assert not w.receipt.exists() and not any(command[:3] == [sys.executable, "-c", DEPLOY] for command in executor.commands)
    assert (release_of(w) / "engine_email/__init__.py").exists()
    with pytest.raises(ReconciliationRequired):
        worker.deploy(confirm=True)
    assert len(executor.sudo_commands) == 3


def test_reconcile_accepts_only_receipt_artifact_and_health_evidence(tmp_path, trusted_owner):
    marker = tmp_path / "health-ready"
    w = world(tmp_path)
    value = engine_manifest(w, sudo=True)
    value["components"][0]["deployment_recipe"]["health_command"] = [
        sys.executable, "-c",
        "import sys; from pathlib import Path; m=Path(sys.argv[1]); "
        "sys.exit(0) if m.exists() else (m.write_text('x'), sys.exit(1))", str(marker), "{release}"]
    planned = plan_manifest(value)
    executor = FakeExecutor(forbid_copy_tree=True)
    worker = ReleaseWorker(planned, records_root=tmp_path / "records", executor=executor)
    authorize(worker, planned, tmp_path, "merge", "deploy")
    for step in STEPS[:4]:
        call(worker, step)
    with pytest.raises(ReleaseError, match="command failed"):
        worker.deploy(confirm=True)
    assert len(executor.sudo_commands) == 3 and w.receipt.exists()
    record = worker.resume()
    assert record["steps"]["deploy:engine"]["status"] == "succeeded"
    assert record["steps"]["deploy:engine"]["after"]["reason"] == "engine receipt, health and identity postconditions verified"
    assert len(executor.sudo_commands) == 3
