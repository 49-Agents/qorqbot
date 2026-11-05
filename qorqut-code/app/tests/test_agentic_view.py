from copy import deepcopy
import base64
import yaml

import pytest

from bazarlyk.agentic.support import digest
from bazarlyk.agentic.view import HostAgenticView
from qorqut_operations.contracts import DomainError
from agentic_projection_support import ATTRIBUTION, native_package, opened_view, skill_body


def chosen(view):
    return {"package_ids": ["imported-host-builtins", "imported-content", "imported-roles"],
            "project_id": None, "work_type": None}


def edit(layer, path, content):
    if path.startswith(("roles/", "operations/", "overseer/")):
        # Fixture owner maintenance of protected built-ins, observed by the engine.
        (layer.root / path).write_text(content)
        layer.changes.scan()
        return
    current = digest((layer.root / path).read_bytes())
    layer.authoring.apply(ATTRIBUTION, [{"op": "edit", "path": path,
        "expected_sha256": current, "content": content}])


def test_current_body_resources_and_root_skill_use_only_public_catalog(tmp_path):
    package = native_package(root=True)
    view, inventory = opened_view(tmp_path, [package])
    immutable = deepcopy(package.files)
    read = view.skill_files("plugin", package, "content-foundations", selection=chosen(view))
    assert read["SKILL.md"] != package.files["SKILL.md"]
    assert read["assets/guide.txt"] == b"Managed resource.\n"
    before = view.catalog_revision
    edit(view.layer, "plugins/imported-content/native/SKILL.md",
         skill_body("bazarlyk-plugin-content-foundations", "Edited now.").decode())
    view.layer.authoring.apply(ATTRIBUTION, [{"op": "create", "path": "plugins/imported-content/native/assets/image.bin",
        "content_base64": base64.b64encode(b"\xff\x00").decode()}])
    assert view.catalog_revision != before
    class PublicLayer:
        catalog = view.layer.catalog
    projection = HostAgenticView(PublicLayer(), inventory).skill_files(
        "plugin", package, "content-foundations", selection=chosen(view))
    assert b"Edited now." in projection["SKILL.md"]
    assert projection["assets/image.bin"] == b"\xff\x00"
    assert package.files == immutable


def test_inventory_is_defensive_and_binding_cannot_be_authored(tmp_path):
    package = native_package()
    view, inventory = opened_view(tmp_path, [package], inactive=True)
    inventory["bindings"][1]["skills"][0]["canonical_entry"] = "other/not-real"
    assert b"Current managed" in view.skill_files("plugin", package, "content-foundations", selection=chosen(view))["SKILL.md"]
    with pytest.raises(DomainError, match="outside the current selection"):
        view.skill_files("plugin", package, "content-foundations", selection={"package_ids": []})
    with pytest.raises(DomainError, match="not declared"):
        view.skill_files("plugin", package, "invented", selection=chosen(view))
    bad = deepcopy(inventory)
    with pytest.raises(DomainError, match="unknown entry|canonical"):
        HostAgenticView(view.layer, bad)


def test_exact_source_resolution_and_ambiguous_symbols(tmp_path):
    view, inventory = opened_view(tmp_path)
    resource = view.resource("host", "app/bazarlyk/agentic/assets/toolkit.md")
    assert resource["encoding"] == "utf-8" and resource["root_path"].endswith("toolkit.md")
    duplicate = deepcopy(next(asset for asset in inventory["assets"] if asset["path"].endswith("toolkit.md")))
    package = next(p for p in inventory["packages"] if p["id"] == duplicate["package_id"])
    original = duplicate["destination"][len(package["path"]) + 1:]
    def extra(asset, suffix, symbol):
        item = deepcopy(asset)
        item["symbol"] = symbol
        relative = original + suffix
        item["destination"] = package["path"] + "/" + relative
        package["files"][relative] = deepcopy(package["files"][original])
        target = view.layer.root / item["destination"]
        target.write_bytes((view.layer.root / asset["destination"]).read_bytes())
        return item
    duplicate = extra(duplicate, ".second", "second")
    view.layer.changes.scan()
    inventory["assets"].append(duplicate)
    exact = HostAgenticView(view.layer, inventory)
    assert exact.resource("host", duplicate["path"]) == resource
    assert exact.resource("host", duplicate["path"], symbol="second")["content"] == resource["content"]
    inventory["assets"].append(extra(duplicate, ".third", None))
    view.layer.changes.scan()
    ambiguous = HostAgenticView(view.layer, inventory)
    with pytest.raises(DomainError, match="no unique"):
        ambiguous.resource("host", duplicate["path"])
    assert ambiguous.resource("host", duplicate["path"], symbol="second")["content"] == resource["content"]
    with pytest.raises((DomainError, ValueError)):
        view.resource("host", "../outside.md")
    with pytest.raises(DomainError):
        view.resource("unknown", "other.md")


def test_content_uses_canonical_adaptation_not_provenance_copy(tmp_path):
    package = native_package()
    view, inventory = opened_view(tmp_path, [package])
    path = "plugins/imported-content/native/provenance-snapshot/skills/content-foundations/SKILL.md"
    view.layer.authoring.apply(ATTRIBUTION, [{"op": "create", "path": path, "content": "UPSTREAM SNAPSHOT"}])
    content = view.skill_files("plugin", package, "content-foundations", selection=chosen(view))
    assert b"Current managed" in content["SKILL.md"]
    assert all(b"UPSTREAM" not in raw for raw in content.values())


def test_concurrent_edit_retries_whole_projection_and_bounds_failure(tmp_path, monkeypatch):
    package = native_package()
    view, _ = opened_view(tmp_path, [package])
    reader = view.layer.catalog.read_resource
    count = 0
    def once(*args, **kwargs):
        nonlocal count
        result = reader(*args, **kwargs)
        count += 1
        if count == 1:
            view.layer.authoring.apply(ATTRIBUTION, [{"op": "edit",
                "path": "plugins/imported-content/native/skills/content-foundations/assets/guide.txt",
                "expected_sha256": digest(b"Managed resource.\n"), "content": "New resource."}])
        return result
    monkeypatch.setattr(view.layer.catalog, "read_resource", once)
    projected = view.skill_files("plugin", package, "content-foundations", selection=chosen(view))
    assert projected["assets/guide.txt"] == b"New resource." and count >= 4
    def forever():
        nonlocal count
        count += 1
        return str(count)
    monkeypatch.setattr(HostAgenticView, "catalog_revision", property(lambda self: forever()))
    with pytest.raises(DomainError) as error:
        view.skill_files("plugin", package, "content-foundations", selection=chosen(view))
    assert error.value.code == "agentic_revision_conflict" and error.value.retryable


def test_consistent_requires_the_configuration_revision(tmp_path):
    view, _ = opened_view(tmp_path)
    called = []
    with pytest.raises(DomainError):
        view.consistent(lambda: called.append(True), revision="stale")
    assert not called


def test_many_nested_skill_reads_share_one_catalog_and_a_fresh_final_check(tmp_path, monkeypatch):
    import qorqut_agentic.catalog_api as api
    package = native_package()
    view, _ = opened_view(tmp_path, [package])
    original = api.load_catalog
    loads = []

    def counted(source):
        loads.append(True)
        return original(source)

    monkeypatch.setattr(api, "load_catalog", counted)
    result = view.consistent(lambda: [view.skill_files("plugin", package, "content-foundations",
        selection=chosen(view)) for _ in range(30)])
    assert len(loads) == 1  # One parsed projection; the fresh final revision check does not parse.
    assert all(b"Current managed" in value["SKILL.md"] for value in result)


def test_nested_projection_retries_after_edit_and_clears_snapshot_on_error(tmp_path):
    package = native_package()
    view, _ = opened_view(tmp_path, [package])
    path = "plugins/imported-content/native/skills/content-foundations/assets/guide.txt"
    attempts = []

    def read():
        result = view.skill_files("plugin", package, "content-foundations", selection=chosen(view))
        attempts.append(True)
        if len(attempts) == 1:
            edit(view.layer, path, "New resource.")
        return result

    result = view.consistent(read)
    assert len(attempts) == 2 and result["assets/guide.txt"] == b"New resource."
    with pytest.raises(RuntimeError):
        view.consistent(lambda: (_ for _ in ()).throw(RuntimeError("reader failed")))
    edit(view.layer, path, "After exception.")
    assert view.skill_files("plugin", package, "content-foundations", selection=chosen(view))["assets/guide.txt"] == b"After exception."


def test_shared_catalog_additions_and_disabled_imports_do_not_change_native_grants(tmp_path):
    package = native_package()
    view, _ = opened_view(tmp_path, [package], inactive=True)
    profile = {"role": {"id": "global-worker"}, "plugins": []}
    selection = view.selection(profile, project_id="project-one", work_type="content")
    assert "imported-content" in selection["package_ids"]
    assert "imported-custom-off" not in selection["package_ids"]
    path = "plugins/imported-custom-off/agentic.yaml"
    manifest = yaml.safe_load(view.layer.catalog.read_resource("imported-custom-off", "agentic.yaml")["content"])
    manifest.pop("native")
    for entry in manifest["entries"]:
        entry.pop("native")
        entry["kind"] = "skill"
    edit(view.layer, path, yaml.safe_dump(manifest))
    assert "imported-custom-off" not in view.selection(profile)["package_ids"]
    template_id = "imported-host-builtins-templates"
    manifest = yaml.safe_load(view.layer.catalog.read_resource(template_id, "agentic.yaml")["content"])
    manifest["native"]["enabled"] = True
    for entry in manifest["entries"]:
        entry["kind"] = "skill"
        entry["native"].update(enabled=True, template=False)
    edit(view.layer, "operations/" + template_id + "/agentic.yaml", yaml.safe_dump(manifest))
    assert template_id not in view.selection(profile)["package_ids"]
    with pytest.raises(DomainError, match="no active host-owned binding"):
        view.skill_files("plugin", native_package(package_id="basic", skill_id="basic"),
                         "basic", selection={"package_ids": [template_id]})
    view.layer.authoring.apply(ATTRIBUTION, [
        {"op": "create", "path": "plugins/brand-new/agentic.yaml", "content": yaml.safe_dump({
            "schema_version": 1, "id": "brand-new", "name": "Shared new procedure", "description": "Fixture",
            "entries": [{"id": "new", "kind": "skill", "path": "SKILL.md", "description": "Fixture",
                         "when_to_use": "New work", "applicability": {"work_types": ["content"]}}]})},
        {"op": "create", "path": "plugins/brand-new/SKILL.md", "content": skill_body("new").decode()},
    ])
    selection = view.selection(profile, work_type="content")
    assert "brand-new" in selection["package_ids"]
    assert "brand-new/new" in {row["ref"] for row in view.layer.catalog.list(selection=selection)["entries"]}
    assert profile["plugins"] == []
    with pytest.raises(AttributeError):
        view.layer = object()
