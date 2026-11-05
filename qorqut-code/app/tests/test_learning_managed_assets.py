"""Focused proof that the retired managed ``learning-work`` asset left the package and obeys the three-way rule.

The host inventory is the real offline import: it validates the declared index and every render call site, and
rejects an undeclared Markdown fragment. The removal plan uses the existing ``build_import_plan`` three-way
comparison directly (a synthetic fixture of the installed binding) so no live managed tree or service is used.
"""
import base64
from pathlib import Path

import bazarlyk
from bazarlyk.agentic.inventory import HOST_PYTHON, Inventory
from bazarlyk.agentic.plans import build_import_plan
from bazarlyk.agentic.support import Source, digest

ROOT = Path(bazarlyk.__file__).resolve().parents[2]
INDEX = "app/bazarlyk/agentic/assets/index.json"
RETIRED_PATH = "app/bazarlyk/agentic/assets/learning-work.md"
RETAINED_PATH = "app/bazarlyk/agentic/assets/learning-review.md"
PREFIX = "packages/imported-host-builtins/"
RETIRED_DESTINATION = PREFIX + "native/" + RETIRED_PATH
RETAINED_DESTINATION = PREFIX + "native/" + RETAINED_PATH
RETIRED_FILE = "native/" + RETIRED_PATH
RETAINED_FILE = "native/" + RETAINED_PATH
OLD_BYTES = b"Read learnings_work_list/learnings_work_get for retained procedural work history only.\n"
OWNER_EDIT = b"Owner-edited managed guidance that must never be overwritten.\n"


def host_inventory():
    """The real host declaration tree, read from this checkout without Git or network access."""
    source = Source("host", ROOT, "host", True, [], export_component={"commit": "0" * 40},
                    manifest_digest="fixture")
    inventory = Inventory()
    package = inventory.package("imported-host-builtins", "host", True)
    inventory.attempt(source, inventory.declared, source, package, INDEX,
                      {"app/bazarlyk/" + path for path in HOST_PYTHON})
    return inventory, package


def test_the_retired_asset_left_the_real_package_and_its_index():
    inventory, package = host_inventory()
    assert inventory.diagnostics == [], "the host index and its render call sites still validate"
    assert not (ROOT / RETIRED_PATH).exists()
    assert RETIRED_FILE not in package["files"]
    assert RETAINED_FILE in package["files"], "the retained review guidance stays in the package"
    assert not [entry for entry in package["entries"] if entry["path"] == RETIRED_DESTINATION]
    assert not [asset for asset in inventory.assets if asset["path"] == RETIRED_PATH]
    index_text = (ROOT / INDEX).read_text()
    assert "learning-work" not in index_text


def test_review_guidance_states_completeness_indicators_independently():
    text = " ".join((ROOT / RETAINED_PATH).read_text().lower().split())
    for phrase in ("independent", "content_cursor completion", "references_complete", "history_complete",
                   "evidence_status", "learnings_legacy_pending", "learnings_review_list",
                   "cannot enumerate all historical records"):
        assert phrase in text, phrase


def desired_inventory(files):
    sources = [{"id": "host", "name": "qorqut-code", "commit": "0" * 40}]
    package_files = {}
    for destination, body in files.items():
        relative = destination.removeprefix(PREFIX)
        package_files[relative] = {
            "content_base64": base64.b64encode(body).decode("ascii"),
            "sha256": digest(body),
            "provenance": {"source_id": "host", "path": relative.removeprefix("native/"), "commit": "0" * 40,
                           "source_sha256": digest(body), "symbol": None, "line": None},
        }
    return {"schema_version": 1, "source_digest": digest({"files": sorted(package_files)}),
            "sources": sources,
            "packages": [{"id": "imported-host-builtins", "name": "imported-host-builtins",
                          "description": "Offline native assets for host", "entries": [],
                          "native": {"owner": "host", "enabled": True, "offline_only": True},
                          "files": package_files}],
            "bindings": [], "coverage": {"complete": True, "diagnostics": []}, "diagnostics": []}


def previous_import(files):
    manifest = {"schema_version": 1, "files": {path: digest(body) for path, body in files.items()}}
    manifest["manifest_digest"] = digest(manifest)
    return manifest


def plan_for(existing):
    retained = b"Factual review and explicit historical reads.\n"
    inventory = desired_inventory({RETAINED_DESTINATION: retained})
    # desired_inventory intentionally ignores source_digest exactness; recompute the contract digest it expects.
    inventory["source_digest"] = digest(inventory["sources"])
    baseline = {RETIRED_DESTINATION: OLD_BYTES, RETAINED_DESTINATION: OLD_BYTES}
    return build_import_plan(inventory, existing_files=existing, previous_import=previous_import(baseline))


def test_an_unmodified_managed_copy_is_deleted_by_the_offline_upgrade():
    plan = plan_for({RETIRED_DESTINATION: OLD_BYTES, RETAINED_DESTINATION: OLD_BYTES})
    assert plan["conflicts"] == []
    touched = {change["path"]: change for change in plan["changes"] if change["path"] in (RETIRED_DESTINATION, RETAINED_DESTINATION)}
    assert touched[RETIRED_DESTINATION]["op"] == "delete"
    assert touched[RETIRED_DESTINATION]["expected_sha256"] == digest(OLD_BYTES)
    assert touched[RETAINED_DESTINATION]["op"] == "edit", "the changed review guidance is updated, not recreated"
    assert {change["path"] for change in plan["changes"]} == {
        "packages/imported-host-builtins/agentic.yaml", RETIRED_DESTINATION, RETAINED_DESTINATION}
    assert next(change for change in plan["changes"] if change["path"].endswith("agentic.yaml"))["op"] == "create"


def test_an_owner_edited_copy_is_a_conflict_never_an_overwrite_or_a_tree_reset():
    plan = plan_for({RETIRED_DESTINATION: OWNER_EDIT, RETAINED_DESTINATION: OLD_BYTES})
    assert all(change["path"] != RETIRED_DESTINATION for change in plan["changes"])
    conflict = next(conflict for conflict in plan["conflicts"] if conflict["path"] == RETIRED_DESTINATION)
    assert conflict["code"] == "local_modification"
    assert conflict["previous_sha256"] == digest(OLD_BYTES)
    assert conflict["current_sha256"] == digest(OWNER_EDIT)
    assert conflict["desired_sha256"] is None, "the desired state is absence; the owner's bytes still win"
    assert all(change["path"] != RETIRED_DESTINATION for change in plan["changes"])