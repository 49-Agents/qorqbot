from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from bazarlyk.release_worker.errors import ReleaseError
from bazarlyk.release_worker.gitops import merge_component, prepare_merge, reconcile_merge
from bazarlyk.release_worker.manifest import plan_manifest
from bazarlyk.release_worker.worker import ReleaseWorker
from test_release_worker import git_repo, manifest, run


def merged_source(tmp_path):
    repo, head = git_repo(tmp_path)
    original = plan_manifest(manifest(repo))["components"][0]
    merged = merge_component(original, confirm=True)
    return repo, head, merged["merge_commit"]


def test_new_engine_plan_verifies_existing_merge_and_deploys(tmp_path):
    repo, head, target = merged_source(tmp_path)
    value = manifest(repo, category="engine", release_id="corrected-recipe")
    component = value["components"][0]
    component["tests"] = []
    receipt = tmp_path / "receipt.json"
    component["deployment_recipe"] = {
        "host": "localhost", "service": "fixture.service",
        "release_root": str(tmp_path / "releases"), "health": [],
        "backup": str(tmp_path / "backups"), "configuration": [],
        "receipt_path": str(receipt),
        "deploy_command": [sys.executable, "-c",
            "import json,sys; from pathlib import Path; "
            "from bazarlyk.release_worker.adapters import _tree_digest; "
            "release=Path(sys.argv[2]); "
            "Path(sys.argv[1]).write_text(json.dumps(dict(schema_version=1,"
            "operation='deploy',outcome='succeeded',component='component',"
            "release_id=release.name,release=str(release),plan_digest=sys.argv[3],"
            "source_commit=sys.argv[4],artifact_digest=_tree_digest(release))))",
            str(receipt), "{release}", "{plan_digest}", "{source_commit}"],
        "health_command": [sys.executable, "-c", "print('healthy')", "{release}"],
        "inspect_command": [sys.executable, "-c", "print('fixture runtime')", "{release}"],
    }
    planned = plan_manifest(value)
    worker = ReleaseWorker(planned, records_root=tmp_path / "records")
    worker.check()
    worker.build()
    worker.stage()
    with pytest.raises(ReleaseError, match="recorded merge authorization"):
        worker.merge(confirm=True)
    for scope in ("merge", "deploy"):
        evidence = tmp_path / f"{scope}.json"
        evidence.write_text(json.dumps({"scope": scope,
            "plan_digest": planned["plan_digest"], "evidence": "fixture owner approval"}))
        worker.authorize(scope, evidence)
    result = worker.merge(confirm=True)["results"]["component"]
    assert result["already_merged"] is True
    assert result["merged"] is False
    assert result["merge_commit"] == target
    assert reconcile_merge(planned["components"][0], result)["already_succeeded"] is True
    worker.deploy(confirm=True)
    assert worker.status()["steps"]["deploy:component"]["status"] == "succeeded"
    assert json.loads(receipt.read_text())["source_commit"] == head
    assert run("git", "ls-remote", "origin", "refs/heads/main", cwd=repo).split()[0] == target


def test_already_merged_source_cannot_drop_later_target_content(tmp_path):
    repo, _, target = merged_source(tmp_path)
    run("git", "checkout", "-b", "later", target, cwd=repo)
    (repo / "later").write_text("preserve newer work\n")
    run("git", "add", "later", cwd=repo)
    run("git", "commit", "-m", "later work", cwd=repo)
    run("git", "push", "origin", "HEAD:main", cwd=repo)
    run("git", "checkout", "feature/release", cwd=repo)
    component = plan_manifest(manifest(repo))["components"][0]
    with pytest.raises(ReleaseError, match="differs from current target"):
        prepare_merge(component, confirm=True)


@pytest.mark.parametrize("drift", ["target", "remote_source", "local_source", "dirty"])
def test_existing_merge_still_rejects_changed_inputs(tmp_path, drift):
    repo, head, target = merged_source(tmp_path)
    component = plan_manifest(manifest(repo))["components"][0]
    intent = prepare_merge(component, confirm=True)
    if drift == "dirty":
        (repo / "uncommitted").write_text("preserve\n")
    elif drift == "local_source":
        run("git", "commit", "--allow-empty", "-m", "local change", cwd=repo)
    else:
        parent = target if drift == "target" else head
        commit = run("git", "commit-tree", f"{parent}^{{tree}}", "-p", parent,
            "-m", "remote changed", cwd=repo)
        branch = "main" if drift == "target" else "feature/release"
        run("git", "push", "origin", f"{commit}:refs/heads/{branch}", cwd=repo)
    with pytest.raises(ReleaseError):
        merge_component(component, confirm=True, intent=intent)


def test_unmerged_source_cannot_claim_existing_merge(tmp_path):
    repo, _ = git_repo(tmp_path)
    component = plan_manifest(manifest(repo))["components"][0]
    intent = prepare_merge(component, confirm=True)
    intent.update(already_merged=True, merge_commit=component["base_commit"])
    with pytest.raises(ReleaseError, match="not the frozen target content"):
        merge_component(component, confirm=True, intent=intent)
