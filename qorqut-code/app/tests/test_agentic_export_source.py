"""Real importer capture from verified, unversioned export snapshots."""
import json
from pathlib import Path

import pytest

from agentic_installation_support import snapshot
from bazarlyk.agentic.installation import SOURCES
from bazarlyk.agentic import inventory_sources, ImportFailure
from bazarlyk.agentic.__main__ import dispatch
from bazarlyk.source_snapshot import validate_export


@pytest.fixture(scope="module")
def export(tmp_path_factory):
    root = snapshot(tmp_path_factory.mktemp("export"))
    manifest, digest = validate_export(root)
    components = {c["name"]: c for c in manifest["components"]}
    roots = {key: str(root / components[name]["destination"]) for key, name in SOURCES.items() if name in components}
    return root, roots, components, digest


def test_verified_export_preserves_provenance_without_consulting_git(export, monkeypatch):
    root, roots, components, manifest_digest = export
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("Export import consulted Git"))
    inventory = dispatch({"operation": "inventory_sources", "arguments": {"source_roots": roots,
        "selected_extensions": ["email-management", "agent-terminal-control"], "export_root": str(root)}})
    assert inventory["coverage"]["complete"], inventory["diagnostics"]
    for source in inventory["sources"]:
        component = components[SOURCES[source["id"]]]
        assert source["clean"] is None and source["provenance_kind"] == "committed_export"
        assert source["commit"] == component["commit"] and source["tree"] == component["tree"]
        assert source["snapshot_sha256"] == component["snapshot_sha256"]
        assert source["source_manifest_sha256"] == manifest_digest
        assert source["files"] and source["snapshot_digest"]


def test_source_root_must_be_exact_component_destination(export):
    root, roots, _, _ = export
    with pytest.raises(ImportFailure, match="does not match"):
        inventory_sources({**roots, "host": str(Path(roots["host"]) / "app")}, [], export_root=root)


def test_changed_export_is_rejected_instead_of_rebaselined(export):
    root, roots, _, _ = export
    path = Path(roots["host"]) / "app/bazarlyk/agentic/assets/toolkit.md"
    before = path.read_bytes()
    try:
        path.write_bytes(before + b"\nChanged after export\n")
        with pytest.raises(ImportFailure, match="snapshot changed"):
            inventory_sources(roots, [], export_root=root)
    finally:
        path.write_bytes(before)


def test_explicit_custom_snapshot_can_be_preserved_in_the_private_binding(export, tmp_path):
    from bazarlyk.agentic import build_import_plan
    from bazarlyk.agentic.installation import initialize_installation, open_installation, prepare_installation
    from bazarlyk.instance_paths import InstancePaths
    root, roots, _, _ = export
    custom = tmp_path / "explicit-custom"
    custom.mkdir()
    (custom / "SKILL.md").write_text("---\nname: custom-check\ndescription: Explicit synthetic custom procedure\n---\nSynthetic instructions.\n")
    inventory = inventory_sources(roots, ["email-management", "agent-terminal-control"],
        custom_sources=[{"id": "example", "root": str(custom), "owner": "content", "kind": "skill", "enabled": True}], export_root=root)
    assert inventory["coverage"]["complete"], inventory["diagnostics"]
    source = next(row for row in inventory["sources"] if row["id"] == "custom-example")
    assert source["commit"] is None and source["clean"] is None
    prepared = prepare_installation(root, ())
    prepared.update(inventory=inventory, plan=build_import_plan(inventory))
    paths = InstancePaths(tmp_path / "instance", {})
    initialize_installation(paths, prepared)
    _, view = open_installation(paths, ())
    assert "imported-custom-example" in view.selection({})["package_ids"]
