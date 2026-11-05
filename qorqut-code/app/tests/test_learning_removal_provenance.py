"""Verified provenance of the installed host sources and the selected extension implementations (phase B).

The first-wave slice refused every selected extension and every non-Git installed source. These cases prove the replacement:
the installed tree must be byte-identical to the verified export (never an mtime or a stale digest), a selected factory's
loaded package must be its exported component's own source, and an unrelated installed package, a mutated or substituted
file, an untracked addition, a symlink or a stale selection is refused. A prepared installation additionally needs the real
startup contract (the running interpreter is its own environment).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile

import pytest
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.database import Database

from bazarlyk.bootstrap import learning_removal as removal
from bazarlyk.bootstrap import learning_removal_provenance as provenance
from bazarlyk.bootstrap.learning_removal import prepare_migration
from bazarlyk.extensions import select
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.source_snapshot import component_digest
from test_learning_removal_migration import (COMPONENTS, _remove_temporary_trees, _release_reference, make_paths,
                                             make_predecessor, release_for)

__all__ = ["_remove_temporary_trees"]

ENTRIES = [{"factory": "qorqut_content.extension:create", "config": {"schema_version": 1, "enabled": False}}]


def reference_for(export: Path, **extra) -> dict:
    """Recompute every component snapshot after a deliberate change to an export and return its release reference."""
    manifest = json.loads((export / "SOURCE-MANIFEST.json").read_text())
    for component in manifest["components"]:
        component["snapshot_sha256"] = component_digest(export, component, manifest["components"])
    (export / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    reference = _release_reference(None, export, manifest)
    reference.update(extra)
    return reference


def installed_paths(tmp_path, paths, release) -> InstancePaths:
    """Plain (non-Git) installed copies of every exported host component, as a prepared ``src`` tree holds them."""
    export, root = Path(release["export"]), tmp_path / "installed"
    components = {}
    for name in COMPONENTS:
        shutil.copytree(export / name, root / name)
        components[name] = root / name
    return InstancePaths(paths.instance_root, components)


def plan_with(paths, release):
    return prepare_migration(paths, preparation={"release": release, "steps": [], "files": []})


def test_a_non_git_installed_source_must_equal_the_validated_export(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    release = release_for(paths)
    installed = installed_paths(tmp_path, paths, release)
    plan = plan_with(installed, release)
    app = plan["release_evidence"]["components"]["app"]
    assert plan["gates"]["ready"] is True and app["installed_sha256"] and app["subpath"] == ""
    assert plan["release_evidence"]["extension_sources"] == {"packages": {}, "installation": None}

    # A mutated file, an untracked addition, a deleted file, a changed mode or a symlink never reaches a plan.
    target = installed.component("app") / "bazarlyk" / "__init__.py"
    original = target.read_bytes()
    target.write_bytes(original + b"\n# mutated\n")
    with pytest.raises(DomainError) as refused:
        plan_with(installed, release)
    assert refused.value.code == "operation_conflict"
    target.write_bytes(original)
    assert plan_with(installed, release)["release_evidence"] == plan["release_evidence"]

    extra = installed.component("app") / "bazarlyk" / "untracked_drift.py"
    extra.write_text("print('drift')\n")
    with pytest.raises(DomainError) as refused:
        plan_with(installed, release)
    assert refused.value.code == "operation_conflict"
    extra.unlink()

    target.rename(target.with_name("moved.py"))
    with pytest.raises(DomainError):
        plan_with(installed, release)
    target.with_name("moved.py").rename(target)

    os.chmod(target, 0o755)
    with pytest.raises(DomainError):
        plan_with(installed, release)
    os.chmod(target, 0o644)

    link = installed.component("app") / "bazarlyk" / "link.py"
    link.symlink_to(target.name)
    with pytest.raises(DomainError):
        plan_with(installed, release)
    link.unlink()
    assert plan_with(installed, release)["release_evidence"] == plan["release_evidence"]


def test_a_substituted_or_symlinked_installed_root_is_refused(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    release = release_for(paths)
    installed = installed_paths(tmp_path, paths, release)
    # The dashboard destination presented as the app: a substituted component is a different tree.
    swapped = {**release, "components": {**release["components"], "app": "dashboard", "dashboard": "app"}}
    with pytest.raises(DomainError) as refused:
        plan_with(installed, swapped)
    assert refused.value.code == "operation_conflict"

    real = installed.component("operations_engine")
    moved = real.with_name("operations_engine_real")
    real.rename(moved)
    real.symlink_to(moved)
    with pytest.raises(DomainError) as refused:
        plan_with(installed, release)
    assert refused.value.code == "invalid_configuration"
    real.unlink()
    moved.rename(real)
    assert plan_with(installed, release)["gates"]["ready"] is True


def test_a_component_subpath_binds_the_installed_directory_to_its_exported_subtree(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    release = release_for(paths)
    export = Path(release["export"])
    # The real prepared layout: the host is the ``qorqut-code`` component and ``app`` is a subdirectory of it.
    (export / "host").mkdir()
    shutil.move(export / "app", export / "host" / "app")
    manifest = json.loads((export / "SOURCE-MANIFEST.json").read_text())
    for component in manifest["components"]:
        if component["name"] == "app":
            component["name"], component["destination"] = "host", "host"
    (export / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    release = reference_for(export)
    release["components"]["app"] = "host:app"
    installed_root = tmp_path / "installed"
    shutil.copytree(export / "host" / "app", installed_root / "app")
    components = {name: export / name for name in COMPONENTS if name != "app"}
    components["app"] = installed_root / "app"
    installed = InstancePaths(paths.instance_root, components)
    plan = plan_with(installed, release)
    entry = plan["release_evidence"]["components"]["host:app"]
    assert entry["subpath"] == "app" and entry["installed_sha256"]

    mutated = installed_root / "app" / "bazarlyk" / "__init__.py"
    mutated.write_bytes(mutated.read_bytes() + b"\n# mutated\n")
    with pytest.raises(DomainError) as refused:
        plan_with(installed, release)
    assert refused.value.code == "operation_conflict"
    wrong = {**release, "components": {**release["components"], "app": "host:tests"}}
    with pytest.raises(DomainError):
        plan_with(installed, wrong)
    for unsafe in ("host:../x", "host:/abs", "host:a/../b", "host:.git"):
        with pytest.raises(DomainError) as invalid:
            plan_with(installed, {**release, "components": {**release["components"], "app": unsafe}})
        assert invalid.value.code == "invalid_configuration"


# -- selected extension implementations -----------------------------------------------------------------------------

def extension_instance(tmp_path):
    pytest.importorskip("qorqut_content.extension")
    selection = select(ENTRIES)
    paths = make_paths(tmp_path)
    Database(paths.database, assembly=removal._transition_for(selection).predecessor).initialize_target()
    return paths, selection


def test_a_selected_factory_needs_its_exported_component_and_nothing_else(tmp_path):
    paths, selection = extension_instance(tmp_path)
    release = release_for(paths, selection, entries=ENTRIES)
    export = Path(release["export"])

    def plan(reference):
        return prepare_migration(paths, extensions=selection, preparation={"release": reference, "steps": [], "files": []})

    first = plan(release)
    evidence = first["release_evidence"]["extension_sources"]["packages"]["qorqut_content"]
    assert evidence["component"] == "qorqut-content" and evidence["files"] > 0

    # Every selected factory package needs exactly one explicit component reference.
    for broken in ({k: v for k, v in release.items() if k != "extension_components"},
                   {**release, "extension_components": {}},
                   {**release, "extension_components": {"qorqut_content": "qorqut-content", "qorqut_paper": "qorqut-content"}}):
        with pytest.raises(DomainError) as refused:
            plan(broken)
        assert refused.value.code == "invalid_configuration"
    with pytest.raises(DomainError) as refused:
        plan({**release, "extension_components": {"qorqut_content": "not-a-component"}})
    assert refused.value.code == "invalid_configuration"

    # An unrelated installed package cannot satisfy the factory: the host component has no such package source.
    with pytest.raises(DomainError) as refused:
        plan({**release, "extension_components": {"qorqut_content": "app"}})
    assert refused.value.code == "operation_conflict"

    # The exported component's own source must be what is loaded: a mutated exported module is a different package.
    module = export / "extensions" / "qorqut-content" / "qorqut_content" / "extension.py"
    original = module.read_bytes()
    module.write_bytes(original + b"\n# the exported source differs from the loaded package\n")
    changed = reference_for(export, extension_components=release["extension_components"])
    with pytest.raises(DomainError) as refused:
        plan(changed)
    assert refused.value.code == "operation_conflict"
    module.write_bytes(original)

    # A lookalike: another component that carries a same-named package with different content is refused too.
    lookalike = export / "extensions" / "lookalike" / "qorqut_content"
    shutil.copytree(export / "extensions" / "qorqut-content" / "qorqut_content", lookalike)
    (lookalike / "extension.py").write_bytes(original + b"\n# lookalike\n")
    manifest = json.loads((export / "SOURCE-MANIFEST.json").read_text())
    manifest["components"].append({"name": "lookalike", "destination": "extensions/lookalike", "commit": "1" * 40,
                                   "tree": "2" * 40, "snapshot_sha256": "0" * 64})
    (export / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    lookalike_reference = reference_for(export, extension_components={"qorqut_content": "lookalike"})
    with pytest.raises(DomainError) as refused:
        plan(lookalike_reference)
    assert refused.value.code == "operation_conflict"
    # The honest reference still verifies after all of that (nothing is cached by an mtime or a remembered result).
    honest = reference_for(export, extension_components=release["extension_components"])
    assert plan(honest)["release_evidence"]["extension_sources"] == first["release_evidence"]["extension_sources"]


def test_extension_components_without_a_selected_extension_are_refused(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    release = release_for(paths)
    with pytest.raises(DomainError) as refused:
        plan_with(paths, {**release, "extension_components": {"qorqut_content": "app"}})
    assert refused.value.code == "invalid_configuration"


# -- prepared installation: wheel contents and the startup contract ---------------------------------------------------

def make_wheel(path: Path, files: dict) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def test_installed_files_must_equal_the_receipt_wheel_bytes(tmp_path):
    wheel = make_wheel(tmp_path / "demo-1.0-py3-none-any.whl", {
        "demo/__init__.py": b"VALUE = 1\n", "demo/data.json": b"{}\n", "demo-1.0.dist-info/METADATA": b"Name: demo\n",
        "demo/__pycache__/cache.pyc": b"x"})
    members = provenance.wheel_members(wheel)
    assert sorted(members) == ["demo/__init__.py", "demo/data.json"]
    site = tmp_path / "site"
    (site / "demo").mkdir(parents=True)
    for name in members:
        (site / name).write_bytes(zipfile.ZipFile(wheel).read(name))
    assert provenance.wheel_matches_installed(members, site) == []
    (site / "demo" / "__init__.py").write_bytes(b"VALUE = 2\n")
    assert provenance.wheel_matches_installed(members, site) == ["demo/__init__.py"]
    (site / "demo" / "__init__.py").write_bytes(b"VALUE = 1\n")
    (site / "demo" / "data.json").unlink()
    assert provenance.wheel_matches_installed(members, site) == ["demo/data.json"]
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}\n")
    (site / "demo" / "data.json").symlink_to(outside)
    assert provenance.wheel_matches_installed(members, site) == ["demo/data.json"]
    with pytest.raises(DomainError):
        provenance.wheel_members(tmp_path / "missing.whl")


def test_a_prepared_installation_needs_the_running_environment_to_be_its_own(tmp_path):
    paths, _transition = make_predecessor(tmp_path)
    release = release_for(paths)
    export = Path(release["export"])
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    shutil.copytree(export, prepared / "src")
    manifest = json.loads((prepared / "src" / "SOURCE-MANIFEST.json").read_text())
    manifest_sha = hashlib.sha256((prepared / "src" / "SOURCE-MANIFEST.json").read_bytes()).hexdigest()
    receipt = {"format": 1, "state": "prepared-not-configured", "source_manifest_sha256": manifest_sha,
               "source_components": manifest["components"], "wheels": [], "environments": {"venv": {"first_party": []}}}
    (prepared / "PREPARATION-RECEIPT.json").write_text(json.dumps(receipt))
    assert provenance.prepared_root(prepared / "src") == prepared and provenance.prepared_root(export) is None
    reference = {**release, "export": str(prepared / "src"), "manifest": str(prepared / "src" / "SOURCE-MANIFEST.json"),
                 "selection": str(prepared / "src" / "selection.json")}
    with pytest.raises(DomainError) as refused:
        plan_with(paths, reference)
    assert refused.value.code == "operation_conflict" and "prepared environment" in refused.value.message

    # A receipt that does not describe the verified export is refused before anything else is asked of the interpreter.
    receipt["source_manifest_sha256"] = "0" * 64
    (prepared / "PREPARATION-RECEIPT.json").write_text(json.dumps(receipt))
    with pytest.raises(DomainError) as refused:
        plan_with(paths, reference)
    assert refused.value.code == "operation_conflict" and "does not describe" in refused.value.message
