from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from bazarlyk.release_worker.adapters import AdapterContext, ImmutablePackageAdapter, IndependentEngineAdapter, _tree_digest
from bazarlyk.release_worker.errors import ReleaseError, UnsupportedAdapter


class RehearsalExecutor:
    def __init__(self, root: Path, components: list[dict] | None = None):
        self.root = root
        self.commands: list[list[str]] = []
        self.source_commits = {item["id"]: item["source_commit"] for item in components or []}
        self.systemd_output = "ok"

    def run(self, argv: list[str], *, check: bool = True) -> str:
        self.commands.append(argv)
        if argv[:2] == ["systemctl", "show"]:
            return self.systemd_output
        if argv[0] == "publish-package":
            component, agents, artifact, plan_digest = argv[1:5]
            self.root.joinpath("package.receipt").write_text(
                json.dumps({
                    "schema_version": 1,
                    "operation": "adopt",
                    "release_id": "rehearsal-001",
                    "plan_digest": plan_digest,
                    "component": component,
                    "source_commit": self.source_commits[component],
                    "outcome": "succeeded",
                    "release": artifact,
                    "artifact_digest": _tree_digest(Path(artifact)),
                    "agents": sorted(agents.split(",")),
                })
            )
        elif argv[0] == "deploy-engine":
            component, release, plan_digest = argv[1:4]
            self.root.joinpath("engine.receipt").write_text(
                json.dumps({
                    "schema_version": 1,
                    "operation": "deploy",
                    "release_id": "rehearsal-001",
                    "plan_digest": plan_digest,
                    "component": component,
                    "source_commit": self.source_commits[component],
                    "outcome": "succeeded",
                    "release": release,
                    "artifact_digest": _tree_digest(Path(release)),
                })
            )
        elif argv[0] == "sha256sum":
            return f"{hashlib.sha256(Path(argv[1]).read_bytes()).hexdigest()}  {argv[1]}"
        return "ok"

    def write_file(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    def copy_tree(self, source: Path, destination: Path) -> None:
        shutil.copytree(source, destination, dirs_exist_ok=True)

    def tree_digest(self, root: Path) -> str:
        return _tree_digest(root)

    def file_digest(self, path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def read_file(self, path: Path) -> bytes:
        return Path(path).read_bytes()

    def entry_exists(self, path: Path) -> bool:
        return Path(path).exists() or Path(path).is_symlink()


def context(tmp_path: Path, component: dict) -> AdapterContext:
    artifact_root = tmp_path / "artifacts"
    artifact_root.joinpath(component["id"]).mkdir(parents=True)
    artifact_root.joinpath(component["id"], "payload").write_text("immutable\n")
    return AdapterContext(
        plan={"release_id": "rehearsal-001", "plan_digest": "sha256:rehearsal-plan", "components": [component]},
        artifact_root=artifact_root,
        installation_config={},
        executor=RehearsalExecutor(tmp_path, [component]),
        stage_evidence={component["id"]: {"tree_digest": _tree_digest(artifact_root / component["id"])}},
    )


def test_real_package_adapter_requires_and_verifies_publication_receipt(tmp_path: Path):
    component = {
        "id": "fixture-role",
        "category": "role",
        "source_commit": "a" * 40,
        "adoption": {"agents": ["agent-a", "agent-b"]},
        "adoption_recipe": {
            "command": ["publish-package", "{component}", "{agents}", "{artifact}", "{plan_digest}"],
            "receipt_path": str(tmp_path / "package.receipt"),
            "release_root": str(tmp_path / "adopted"),
        },
    }
    ctx = context(tmp_path, component)
    result = ImmutablePackageAdapter().adopt(component, ctx)
    assert result["adopted"] is True
    assert result["receipt_digest"].startswith("sha256:")
    assert any(command[0] == "publish-package" for command in ctx.executor.commands)


def test_package_adoption_rejects_modified_staged_artifact(tmp_path: Path):
    component = {
        "id": "fixture-role",
        "category": "role",
        "source_commit": "a" * 40,
        "adoption": {"agents": ["agent-a"]},
        "adoption_recipe": {
            "command": ["publish-package", "{component}", "{agents}", "{artifact}", "{plan_digest}"],
            "receipt_path": str(tmp_path / "package.receipt"),
            "release_root": str(tmp_path / "adopted"),
        },
    }
    ctx = context(tmp_path, component)
    ctx.artifact_root.joinpath(component["id"], "payload").write_text("tampered\n")
    with pytest.raises(ReleaseError, match="changed after its recorded stage evidence"):
        ImmutablePackageAdapter().adopt(component, ctx)


def test_stale_adoption_receipt_cannot_prove_success(tmp_path: Path):
    component = {
        "id": "fixture-role",
        "category": "role",
        "source_commit": "a" * 40,
        "adoption": {"agents": ["agent-a"]},
        "adoption_recipe": {
            "command": ["publish-package", "{component}", "{agents}", "{artifact}", "{plan_digest}"],
            "receipt_path": str(tmp_path / "package.receipt"),
            "release_root": str(tmp_path / "adopted"),
        },
    }
    ctx = context(tmp_path, component)
    adapter = ImmutablePackageAdapter()
    adapter.adopt(component, ctx)
    receipt = json.loads((tmp_path / "package.receipt").read_text())
    receipt["plan_digest"] = "sha256:old-plan"
    (tmp_path / "package.receipt").write_text(json.dumps(receipt))
    observed = adapter.reconcile(component, ctx, "adopt", {})
    assert observed.get("already_succeeded") is not True
    assert "not bound" in observed["reason"]


def test_real_engine_adapter_copies_artifact_and_verifies_deploy_and_health(tmp_path: Path):
    component = {
        "id": "fixture-engine",
        "category": "engine",
        "source_commit": "b" * 40,
        "deployment_recipe": {
            "service": "fixture-engine.service",
            "release_root": str(tmp_path / "releases"),
            "health": ["fixture"],
            "backup": str(tmp_path / "backups"),
            "configuration": ["FIXTURE_CONFIG"],
            "deploy_command": ["deploy-engine", "{component}", "{release}", "{plan_digest}"],
            "receipt_path": str(tmp_path / "engine.receipt"),
            "health_command": ["check-engine", "{release}"],
            "inspect_command": ["inspect-engine", "{release}"],
        },
    }
    ctx = context(tmp_path, component)
    result = IndependentEngineAdapter().deploy(component, ctx)
    assert result["deployed"] is True
    assert result["health_verified"]["argv"][0] == "check-engine"
    assert (tmp_path / "releases" / "rehearsal-001" / "payload").is_file()


def test_real_engine_adapter_rejects_modified_staged_artifact(tmp_path: Path):
    component = {
        "id": "fixture-engine",
        "category": "engine",
        "source_commit": "f" * 40,
        "deployment_recipe": {
            "service": "fixture-engine.service",
            "release_root": str(tmp_path / "releases"),
            "health": [],
            "backup": str(tmp_path / "backups"),
            "configuration": [],
            "deploy_command": ["deploy-engine", "{component}", "{release}", "{plan_digest}"],
            "receipt_path": str(tmp_path / "engine.receipt"),
            "health_command": ["check-engine", "{release}"],
            "inspect_command": ["inspect-engine", "{release}"],
        },
    }
    ctx = context(tmp_path, component)
    ctx.artifact_root.joinpath(component["id"], "payload").write_text("tampered\n")
    with pytest.raises(ReleaseError, match="changed after its recorded stage evidence"):
        IndependentEngineAdapter().deploy(component, ctx)


def test_runtime_recovery_without_reviewed_recipe_fails_explicitly(tmp_path: Path):
    component = {
        "id": "fixture-engine",
        "category": "engine",
        "source_commit": "c" * 40,
        "deployment_recipe": {
            "service": "fixture-engine.service",
            "release_root": str(tmp_path / "releases"),
            "health": [],
            "backup": str(tmp_path / "backups"),
            "configuration": [],
            "deploy_command": ["deploy-engine", "{component}", "{release}", "{plan_digest}"],
            "receipt_path": str(tmp_path / "engine.receipt"),
            "health_command": ["check-engine", "{release}"],
            "inspect_command": ["inspect-engine", "{release}"],
        },
    }
    ctx = context(tmp_path, component)
    with pytest.raises(UnsupportedAdapter, match="recovery_recipe"):
        IndependentEngineAdapter().recover(component, ctx)
