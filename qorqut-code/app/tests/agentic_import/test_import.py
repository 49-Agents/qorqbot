import json
import shutil
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from bazarlyk.agentic import ImportFailure, apply_import_plan, build_import_plan, inventory_sources
from bazarlyk.agentic.__main__ import dispatch
from bazarlyk.agentic.support import decoded, digest, encoded
from conftest import write


ATTR = dict(actor_id="synthetic-owner", actor_kind="owner", reason="Offline fixture import")


def snapshot(layer):
    return {p.relative_to(layer.root).as_posix(): p.read_bytes() for p in layer.root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(layer.root).parts}


@pytest.fixture
def layer(tmp_path):
    from qorqut_agentic import AgenticLayer
    root = tmp_path / "authored"
    root.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "agentic"], check=True)
    return AgenticLayer.initialize(root, tmp_path / "state")


def test_inventory_preserves_shared_assets_and_disables_extensions(roots, custom):
    inventory = inventory_sources(roots, ["email-management"], custom)
    assert inventory["coverage"]["complete"], inventory["diagnostics"]
    assert all(source["commit"] and source["clean"] for source in inventory["sources"])
    roles = [b for b in inventory["bindings"] if b["kind"] == "role"]
    shared = [next(s["canonical_entry"] for s in b["skills"] if s["native_skill_id"] == "shared") for b in roles]
    assert len(set(shared)) == 1
    assert len(roles) == 3
    assert all(not b["enabled"] for b in inventory["bindings"] if b["native_id"] in {"paper", "fastmail", "custom-procedure", "agent-terminal-control"})
    assert all(e["kind"] != "tool" for p in inventory["packages"] for e in p["entries"])
    assert next(b for b in inventory["bindings"] if b["native_id"] == "fastmail")["metadata_only"]
    assert not any(e["kind"] == "skill" and "/instance-template/" in e["path"] for p in inventory["packages"] for e in p["entries"])
    source = next(s for s in inventory["sources"] if s["id"] == "email")
    assert source["files"]["skills/email-management/references/rules.md"] == digest(b'Synthetic rule bytes\x00\xff')
    assert all(not Path(asset["path"]).is_absolute() for asset in inventory["assets"])


def test_canonical_content_keeps_native_manifest_and_original_snapshot(roots):
    inventory = inventory_sources(roots, ["content"])
    native = next(p for p in inventory["packages"] if p["id"] == "imported-content")
    binding = next(b for b in inventory["bindings"] if b["native_id"] == "content-creation")
    mapping = binding["skills"][0]
    assert mapping["canonical_entry"].startswith("imported-content/")
    assert mapping["original_sha256"] != mapping["canonical_sha256"]
    assert not any("provenance-snapshot" in e["path"] for e in native["entries"])
    assert decoded(native["files"]["native/plugin.yaml"]["content_base64"]) == Path(roots["content-library"], "plugin.yaml").read_bytes()
    inactive = next(p for p in inventory["packages"] if p["id"] == "imported-content-templates")
    assert not inactive["native"]["enabled"]
    assert mapping["provenance_package_id"] == inactive["id"]
    assert decoded(inactive["files"]["native/provenance-snapshot/skills/content-foundations/SKILL.md"]["content_base64"]) == b"Original upstream foundation"


def test_apply_read_event_and_no_op(roots, custom, layer):
    plan = build_import_plan(inventory_sources(roots, ["email-management"], custom))
    result = apply_import_plan(layer, plan, ATTR)
    assert not result["no_op"]
    events = layer.changes.list()["changes"]
    assert len(events) == 1
    assert events[0]["attribution"]["actor_id"] == ATTR["actor_id"]
    catalog = layer.catalog.list()
    assert catalog["entries"]
    ref = next(b for b in plan["bindings"] if b["native_id"] == "email-management")["skills"][0]["canonical_entry"]
    assert "Read [rules]" in layer.catalog.read_entry(ref)["body"]
    assert not layer.catalog.validate()["issues"]
    allowed = [p["id"] for p in plan["packages"] if p["native"]["enabled"]]
    selected_catalog = layer.catalog.list(selection={"package_ids": allowed})
    assert all(e["package_id"] not in {"imported-content", "imported-custom-supplied"} for e in selected_catalog["entries"])
    assert snapshot(layer)["packages/imported-email-management/native/skills/email-management/references/rules.md"] == b'Synthetic rule bytes\x00\xff'
    again = build_import_plan(inventory_sources(roots, ["email-management"], custom), snapshot(layer), result["import_manifest"])
    assert not again["changes"] and not again["conflicts"]
    repeated = apply_import_plan(layer, again, ATTR)
    assert repeated["no_op"] and len(layer.changes.list()["changes"]) == 1


def test_complete_offline_import_preserves_all_bytes_revisions_and_event_after_reopen(roots, custom, layer):
    from qorqut_agentic import AgenticLayer

    inventory = inventory_sources(roots, ["email-management", "content", "paper", "fastmail"], custom)
    assert inventory["coverage"]["complete"], inventory["diagnostics"]
    plan = build_import_plan(inventory)
    imported = apply_import_plan(layer, plan, ATTR)
    reopened = AgenticLayer.open(layer.root, layer.state)
    actual = snapshot(reopened)
    assert {path: digest(body) for path, body in actual.items()} == plan["desired_files"]
    # Check every retained native byte, including binary resources and metadata.
    for package in inventory["packages"]:
        for path, record in package["files"].items():
            assert actual[f"packages/{package['id']}/{path}"] == decoded(record["content_base64"])
    catalog = reopened.catalog.list(limit=1000)
    expected_refs = {f"{package['id']}/{entry['id']}" for package in plan["packages"] for entry in package["entries"]}
    assert {entry["ref"] for entry in catalog["entries"]} == expected_refs
    for entry in catalog["entries"]:
        body = reopened.catalog.read_entry(entry["ref"])
        raw = body["body"].encode() if body["body"] is not None else decoded(body["body_base64"])
        assert raw == actual[entry["root_path"]]
        assert entry["revision"] == digest(raw)
        for resource in body["resources"]:
            result = reopened.catalog.read_resource(entry["package_id"], resource["path"])
            raw = result["content"].encode() if result["encoding"] == "utf-8" else decoded(result["content"])
            assert raw == actual[resource["root_path"]]
            assert result["sha256"] == resource["sha256"] == digest(raw)
    events = reopened.changes.list()["changes"]
    assert len(events) == 1 and events[0]["change_id"] == imported["result"]["change"]["change_id"]
    assert events[0]["attribution"]["actor_id"] == ATTR["actor_id"]
    assert {file["path"]: file["after_sha256"] for file in events[0]["files"]} == plan["desired_files"]
    assert set(events[0]["entry_refs"]) == expected_refs
    assert all(file["before_sha256"] is None for file in events[0]["files"])
    assert reopened.history.status()["head"] is None  # The event is durable before any Git commit.
    repeat = build_import_plan(inventory, actual, imported["import_manifest"])
    assert apply_import_plan(reopened, repeat, ATTR)["no_op"]
    assert reopened.changes.list()["changes"] == events


def test_update_does_not_overwrite_local_customization(roots, sources, layer):
    initial = inventory_sources(roots, [])
    receipt = apply_import_plan(layer, build_import_plan(initial), ATTR)["import_manifest"]
    file = "packages/imported-roles/native/skills/shared/SKILL.md"
    current = snapshot(layer)
    current[file] = b"Locally customized instruction"
    current["notes/local.md"] = b"Local only"
    unchanged_upstream = build_import_plan(initial, current, receipt)
    assert any(c["path"] == file for c in unchanged_upstream["conflicts"])
    write(sources["roles"], "skills/shared/SKILL.md", "Upstream changed instruction")
    subprocess.run(["git", "-C", roots["roles"], "add", "."], check=True)
    subprocess.run(["git", "-C", roots["roles"], "commit", "-qm", "Upstream fixture update"], check=True)
    update = build_import_plan(inventory_sources(roots, []), current, receipt)
    assert any(c["path"] == file for c in update["conflicts"])
    assert not any(c["path"] == "notes/local.md" for c in update["changes"])
    with pytest.raises(ImportFailure, match="locally owned"):
        apply_import_plan(layer, update, ATTR)
    assert len(layer.changes.list()["changes"]) == 1


def test_upstream_update_delete_and_already_desired(roots, sources, layer):
    receipt = apply_import_plan(layer, build_import_plan(inventory_sources(roots, [])), ATTR)["import_manifest"]
    path = sources["roles"] / "skills/shared/references/procedure.md"
    path.unlink()
    write(sources["roles"], "skills/shared/SKILL.md", "Upstream changed instruction")
    subprocess.run(["git", "-C", roots["roles"], "add", "."], check=True)
    subprocess.run(["git", "-C", roots["roles"], "commit", "-qm", "Update and delete"], check=True)
    current = snapshot(layer)
    current["notes/local.md"] = b"Keep"
    update = build_import_plan(inventory_sources(roots, []), current, receipt)
    assert not update["conflicts"]
    assert {c["op"] for c in update["changes"]} >= {"edit", "delete"}
    already = "packages/imported-roles/native/skills/shared/SKILL.md"
    current[already] = b"Upstream changed instruction"
    update = build_import_plan(inventory_sources(roots, []), current, receipt)
    assert not any(c["path"] == already for c in update["changes"])
    apply_import_plan(layer, update, ATTR)
    assert len(layer.changes.list()["changes"]) == 2


@pytest.mark.parametrize("defect", ["missing", "escape", "symlink", "unclassified", "duplicate", "credentials"])
def test_incomplete_sources_are_not_applied(roots, sources, layer, defect):
    root = sources["email"]
    manifest_path = root / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    if defect == "missing":
        (root / "skills/email-management/references/rules.md").unlink()
        write(root, "skills/email-management/SKILL.md", "Read [missing](references/rules.md).")
    elif defect == "escape":
        manifest["rules"] = "../outside.yaml"
    elif defect == "symlink":
        path = root / "skills/email-management/references/rules.md"
        path.unlink()
        path.symlink_to(sources["custom"] / "rules.yaml")
    elif defect == "unclassified":
        write(root, "skills/forgotten/SKILL.md", "Unlisted skill")
    elif defect == "credentials":
        write(root, "skills/email-management/secret.token", "Not a source asset")
    elif defect == "duplicate":
        manifest["skills"].append(manifest["skills"][0])
    write(root, "plugin.yaml", yaml.safe_dump(manifest))
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "Defective fixture"], check=True)
    inventory = inventory_sources(roots, [])
    assert not inventory["coverage"]["complete"]
    with pytest.raises(ImportFailure, match="diagnostics"):
        apply_import_plan(layer, build_import_plan(inventory), ATTR)
    assert not layer.changes.list()["changes"]


def test_plan_and_previous_receipt_tampering_rejected(roots, layer):
    inventory = inventory_sources(roots, [])
    plan = build_import_plan(inventory)
    plan["changes"][0]["content_base64"] = encoded(b"changed")
    with pytest.raises(ImportFailure, match="digest"):
        apply_import_plan(layer, plan, ATTR)
    receipt = apply_import_plan(layer, build_import_plan(inventory), ATTR)["import_manifest"]
    receipt["files"]["packages/imported-roles/agentic.yaml"] = "0" * 64
    with pytest.raises(ImportFailure, match="digest"):
        build_import_plan(inventory, snapshot(layer), receipt)
    with pytest.raises(ImportFailure, match="previous"):
        build_import_plan(inventory, {})


def test_dynamic_instructions_are_pending_without_execution(roots, sources):
    write(sources["host"], "app/bazarlyk/agents/context.py", 'INSTRUCTIONS = "' + 'Static preserved guidance. '*4 + '"\n'
          'def compose(identity):\n    return f"For {identity}: ' + 'Dynamically composed prompt. '*4 + '"\n'
          'raise RuntimeError("never execute")\n')
    subprocess.run(["git", "-C", roots["host"], "add", "."], check=True)
    subprocess.run(["git", "-C", roots["host"], "commit", "-qm", "Dynamic fixture"], check=True)
    inventory = inventory_sources(roots, [])
    assert inventory["coverage"]["pending"] == 1
    assert any(a["symbol"] == "INSTRUCTIONS" and a["kind"] == "instruction" for a in inventory["assets"])
    assert not inventory["coverage"]["complete"]


def test_native_json_command_and_unknown_arguments(roots):
    request = dict(operation="inventory_sources", arguments=dict(source_roots=roots, selected_extensions=[]))
    result = subprocess.run([sys.executable, "-m", "bazarlyk.agentic"], input=json.dumps(request),
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["result"]["coverage"]["complete"]
    for request in ({"operation": "unknown", "arguments": {}},
                    {"operation": "inventory_sources", "arguments": {"source_roots": roots, "selected_extensions": [], "python": "untrusted"}},
                    {"operation": "apply_import_plan", "arguments": {"plan": {}, "attribution": {}}}):
        with pytest.raises(ImportFailure):
            dispatch(request)
    for bad in ('{"operation":"inventory_sources","operation":"anything","arguments":{}}', 'NaN', '{'):
        result = subprocess.run([sys.executable, "-m", "bazarlyk.agentic"], input=bad, capture_output=True, text=True)
        assert result.returncode == 1
        assert not json.loads(result.stdout)["ok"]


def test_custom_skill_and_explicit_source_failures(roots, sources):
    custom = dict(id="standalone", root=str(sources["custom"]), kind="skill", owner="owner", enabled=True)
    write(sources["custom"], "SKILL.md", "Short standalone guidance")
    # An unversioned snapshot is allowed, but a tracked dirty checkout is not silently called complete.
    inventory = inventory_sources(roots, [], [custom])
    assert any(d["code"] == "dirty_source" for d in inventory["diagnostics"])
    incomplete = inventory_sources({k: v for k, v in roots.items() if k != "roles"}, [])
    assert any(d["code"] == "missing_source" and d["source_id"] == "roles" for d in incomplete["diagnostics"])
    with pytest.raises(ImportFailure):
        inventory_sources(roots, ["invented-extension"])
    with pytest.raises(ImportFailure):
        inventory_sources(roots, [], [dict(custom, owner="")])


def test_cli_apply_receipt_and_failed_validation_are_atomic(roots, sources, layer):
    plan = build_import_plan(inventory_sources(roots, []))
    result = subprocess.run([sys.executable, "-m", "bazarlyk.agentic", "--root", str(layer.root), "--state", str(layer.state)],
                            input=json.dumps(dict(operation="apply_import_plan", arguments=dict(plan=plan, attribution=ATTR))),
                            capture_output=True, text=True, check=True)
    receipt = json.loads(result.stdout)["result"]["import_manifest"]
    assert receipt["plan_digest"] == plan["plan_digest"]
    assert len(layer.changes.list()["changes"]) == 1
    # Invalid frontmatter must not gain a complete inventory or partially write an update.
    write(sources["roles"], "skills/shared/SKILL.md", "---\nname: unclosed\n")
    subprocess.run(["git", "-C", roots["roles"], "add", "."], check=True)
    subprocess.run(["git", "-C", roots["roles"], "commit", "-qm", "Invalid frontmatter"], check=True)
    inventory = inventory_sources(roots, [])
    assert any(d["code"] == "invalid_frontmatter" for d in inventory["diagnostics"])
    update = build_import_plan(inventory, snapshot(layer), receipt)
    before = snapshot(layer)
    with pytest.raises(ImportFailure):
        apply_import_plan(layer, update, ATTR)
    assert snapshot(layer) == before
    assert len(layer.changes.list()["changes"]) == 1


def test_late_destination_change_is_not_overwritten(roots, sources, layer):
    from qorqut_agentic.errors import ConflictError
    receipt = apply_import_plan(layer, build_import_plan(inventory_sources(roots, [])), ATTR)["import_manifest"]
    write(sources["roles"], "skills/shared/SKILL.md", "Upstream instruction")
    subprocess.run(["git", "-C", roots["roles"], "add", "."], check=True)
    subprocess.run(["git", "-C", roots["roles"], "commit", "-qm", "Change upstream"], check=True)
    plan = build_import_plan(inventory_sources(roots, []), snapshot(layer), receipt)
    target = layer.root / "packages/imported-roles/native/skills/shared/SKILL.md"
    target.write_text("Late local edit")
    with pytest.raises(ConflictError):
        apply_import_plan(layer, plan, ATTR)
    assert target.read_text() == "Late local edit"


def test_conditional_extension_orientation_is_not_fabricated(roots, sources):
    write(sources["paper"], "qorqut_paper/native.py", 'class PaperNative:\n'
          '    def orientation(prepared):\n        return "Connected" if prepared else "Not connected"\n'
          '    def guidance():\n        return "Static preserved guidance"\n')
    subprocess.run(["git", "-C", roots["paper"], "add", "."], check=True)
    subprocess.run(["git", "-C", roots["paper"], "commit", "-qm", "Dynamic native orientation"], check=True)
    inventory = inventory_sources(roots, [])
    assert any(d["source_id"] == "paper" and d["code"] == "pending_host_composition" for d in inventory["diagnostics"])
    assert not any(a["symbol"] == "orientation" and a["kind"] == "instruction" for a in inventory["assets"])
    assert any(a["symbol"] == "guidance" and a["kind"] == "instruction" for a in inventory["assets"])


def test_native_root_skill_import_preserves_resources_and_excludes_private_material(roots, sources, layer):
    root = sources["custom"]
    shutil.rmtree(root / "skills")
    manifest = yaml.safe_load((root / "plugin.yaml").read_text())
    manifest["skills"] = [{"id": "test", "path": "SKILL.md"}]
    write(root, "plugin.yaml", yaml.safe_dump(manifest))
    write(root, "SKILL.md", "---\nname: test\n---\nRead [resource](references/rules.md).")
    write(root, "references/rules.md", "Root skill resource")
    write(root, "resources/checklist.bin", b"\x00\x01\xff")
    write(root, "runtime/state.sqlite3", "synthetic runtime data")
    write(root, "sessions/saved.md", "synthetic session")
    write(root, ".claude/projects/session.json", "synthetic native session")
    write(root, ".env", "synthetic credential")
    write(root, "secret.token", "synthetic credential")
    write(root, "secrets.json", "synthetic credential")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "Root skill fixture"], check=True)
    inventory = inventory_sources(roots, [], [dict(id="root-skill", root=str(root), owner="owner", kind="plugin", enabled=True)])
    assert inventory["coverage"]["complete"], inventory["diagnostics"]
    package = next(p for p in inventory["packages"] if p["id"] == "imported-custom-root-skill")
    assert [e["path"] for e in package["entries"]] == ["native/SKILL.md"]
    assert not any(name.startswith(("native/.git", "native/.claude", "native/runtime", "native/sessions", "native/.env"))
                   or name in {"native/secret.token", "native/secrets.json"} for name in package["files"])
    apply_import_plan(layer, build_import_plan(inventory), ATTR)
    binding = next(b for b in inventory["bindings"] if b["native_id"] == "custom-procedure")
    assert "Read [resource]" in layer.catalog.read_entry(binding["entry_refs"][0])["body"]
    assert snapshot(layer)["packages/imported-custom-root-skill/native/resources/checklist.bin"] == b"\x00\x01\xff"
    assert len(layer.changes.list()["changes"]) == 1
