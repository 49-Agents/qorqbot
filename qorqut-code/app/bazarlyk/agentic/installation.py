"""Explicit fresh installation and strict, read-only opening of the host binding.

The private record holds initial provenance and native bindings. Current authored
instructions live only in the module tree and are expected to change.
"""
from __future__ import annotations

import base64
from importlib import metadata, resources
import json
import os
from pathlib import Path
import re
import shutil

from qorqut_agentic import AgenticLayer
from qorqut_operations.contracts import DomainError

from bazarlyk.source_snapshot import unique_object, validate_export
from bazarlyk.telegram.private_files import (fsync_directory, read_private_file, require_private_directory,
                                             write_private_file)
from .inventory import inventory_sources
from .plans import apply_import_plan, build_import_plan, _validate_manifest
from .support import canonical, digest, relative
from .view import HostAgenticView

RECORD = "agentic-layer.json"
MAX_RECORD = 96 * 1024 * 1024
SOURCES = {"host": "qorqut-code", "roles": "agent-roles", "email": "email-management",
           "content": "qorqut-content", "content-library": "content-library", "paper": "qorqut-paper", "fastmail": "qorqut-fastmail"}
CORE = {"email-management", "agent-terminal-control"}
IDENTITY = {"name": "Qorqut", "email": "qorqut@localhost"}


def _failure(message):
    return DomainError("agentic_installation_invalid", message + "; prepare and initialize a fresh instance offline, or use an explicitly authorized offline update")


def _seed_files():
    root = resources.files("qorqut_agentic").joinpath("resources/management")
    result = {}
    def walk(node, prefix=""):
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            relative = prefix + child.name
            if child.is_dir():
                walk(child, relative + "/")
            elif child.is_file():
                body = child.read_bytes()
                if len(body) > 1024 * 1024:
                    raise _failure("Oversized packaged management resource")
                result[relative] = body
            else:
                raise _failure("Invalid packaged management resource")
    walk(root)
    if "agentic.yaml" not in result:
        raise _failure("Missing packaged management manifest")
    return result


def prepare_installation(export_root, selected_extensions):
    """Read-only preflight. The explicit verified export is the only source map."""
    try:
        manifest, manifest_digest = validate_export(export_root)
        components = {c["name"]: c for c in manifest["components"]}
        if "qorqut-agentic" not in components:
            raise _failure("Missing qorqut-agentic source component")
        roots = {key: str(Path(export_root) / components[name]["destination"])
                 for key, name in SOURCES.items() if name in components}
        selected = sorted(set(selected_extensions))
        inventory = inventory_sources(roots, sorted(CORE | set(selected)), export_root=export_root)
        if inventory["coverage"]["complete"] is not True:
            raise _failure("Incomplete agentic source inventory: " + json.dumps(inventory["diagnostics"]))
        # Validate the host-owned record contract before any target write.
        HostAgenticView(None, inventory)
        _verify_inventory(inventory, selected, manifest_digest)
        plan = build_import_plan(inventory)
        seed = _seed_files()
        git = shutil.which("git", path=os.defpath)
        if git is None:
            raise _failure("Git is required for agentic initialization")
        return {"inventory": inventory, "plan": plan, "seed": seed, "git": git,
                "selected_extensions": selected, "source_manifest_sha256": manifest_digest}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise _failure(str(exc)) from exc


def initialize_installation(paths, prepared):
    """Initialize once, author through the public API, then atomically seal binding."""
    layer = AgenticLayer.initialize(paths.agentic_root, paths.agentic_runtime, git_executable=prepared["git"],
                                    git_user=IDENTITY, attribution_source="host_installer")
    attribution = {"actor_id": "system:installer", "actor_kind": "system", "reason": "Initialize agentic procedures from verified prepared sources",
                   "source_refs": ["source-manifest:sha256:" + prepared["source_manifest_sha256"]]}
    imported = apply_import_plan(layer, prepared["plan"], attribution)
    seed = prepared["seed"]
    layer.authoring.apply(attribution={**attribution, "reason": "Install the module's packaged management guidance"},
        changes=[{"op": "create", "path": "packages/agentic-management/" + path, "content_base64": base64.b64encode(body).decode()}
                 for path, body in sorted(seed.items())])
    report = layer.catalog.validate()
    if not report["valid"] or layer.recovery_required:
        raise _failure("Initialized agentic catalog is invalid")
    for ref in ("agentic-management/bootstrap", "agentic-management/manage"):
        layer.catalog.read_entry(ref)
    commit = layer.history.commit("Initialize imported procedures and agentic management guidance", author=IDENTITY)["commit"]
    inventory = prepared["inventory"]
    record = {"version": 1, "instance_id": layer.instance_id, "root": str(paths.agentic_root), "state": str(paths.agentic_runtime),
              "inventory": inventory, "inventory_sha256": digest(inventory), "import_manifest": imported["import_manifest"],
              "source_manifest_sha256": prepared["source_manifest_sha256"], "selected_extensions": prepared["selected_extensions"],
              "initial_commit": commit, "management_seed": {"distribution": "qorqut-agentic", "version": metadata.version("qorqut-agentic"),
                  "resource": "qorqut_agentic/resources/management", "files": {path: digest(body) for path, body in sorted(seed.items())}}}
    raw = canonical(record) + b"\n"
    if len(raw) > MAX_RECORD:
        raise _failure("Agentic binding exceeds its size bound")
    directory = require_private_directory(paths.instance_root / "config", create=True)
    # The existing private writer supplies ownership, exclusive create and fsync.
    # Publish only its complete bytes under the final name; never replace a binding.
    temp = write_private_file(directory, ".agentic-layer.pending", raw)
    os.link(temp, directory / RECORD, follow_symlinks=False)
    os.unlink(temp)
    fsync_directory(directory)
    return {"path": str(directory / RECORD), "sha256": digest(raw), "instance_id": layer.instance_id, "initial_commit": commit}


def _verify_inventory(inventory, selected, manifest_digest):
    required = {"host", "roles", "email"} | set(selected)
    if "content" in required:
        required.add("content-library")
    sources = {s["id"] for s in inventory["sources"]}
    if not required <= sources or inventory["coverage"]["asset_count"] != len(inventory["assets"]) or inventory["coverage"]["package_count"] != len(inventory["packages"]):
        raise ValueError("Incomplete inventory source or coverage records")
    for source in inventory["sources"]:
        if source.get("provenance_kind") != "committed_export" or source.get("clean", False) is not None:
            raise ValueError("Installed source lacks committed-export provenance")
        for key, size in (("commit", 40), ("tree", 40), ("snapshot_sha256", 64), ("source_manifest_sha256", 64)):
            if not isinstance(source.get(key), str) or not re.fullmatch(r"[0-9a-f]{" + str(size) + "}", source[key]):
                raise ValueError("Malformed exported-source provenance")
        if source["source_manifest_sha256"] != manifest_digest or source["snapshot_digest"] != digest(source["files"]):
            raise ValueError("Source provenance digest mismatch")
        for path, sha in source["files"].items():
            relative(path)
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("Invalid used-file provenance")
    packages = {p["id"]: p for p in inventory["packages"]}
    expected = {"imported-host-builtins", "imported-roles", "imported-email-management", "imported-terminal-control"}
    if not expected <= packages.keys() or any(not packages[p]["native"]["enabled"] for p in expected):
        raise ValueError("Missing required active core packages")
    roles = {b["native_id"] for b in inventory["bindings"] if b["kind"] == "role" and b["enabled"]}
    if not {"ordubashy", "global-worker", "learning-applier"} <= roles:
        raise ValueError("Missing required role bindings")


def open_installation(paths, selected_extensions):
    """Never initialize, rebind, repair, scan, import, or change package selection."""
    try:
        record = json.loads(read_private_file(paths.instance_root / "config" / RECORD, max_bytes=MAX_RECORD), object_pairs_hook=unique_object)
        keys = {"version", "instance_id", "root", "state", "inventory", "inventory_sha256", "import_manifest",
                "source_manifest_sha256", "selected_extensions", "initial_commit", "management_seed"}
        if not isinstance(record, dict) or set(record) != keys or type(record["version"]) is not int or record["version"] != 1:
            raise ValueError("Missing or malformed version 1 binding")
        if record["root"] != str(paths.agentic_root) or record["state"] != str(paths.agentic_runtime):
            raise ValueError("Agentic binding paths differ from InstancePaths")
        if record["selected_extensions"] != sorted(set(selected_extensions)):
            raise ValueError("Selected trusted extensions differ from the installed selection")
        if not isinstance(record["instance_id"], str) or not re.fullmatch(r"inst_[0-9a-f]{32}", record["instance_id"]):
            raise ValueError("Malformed module identity")
        for key in ("inventory_sha256", "source_manifest_sha256", "initial_commit"):
            if not isinstance(record[key], str) or not re.fullmatch(r"[0-9a-f]{" + ("40" if key == "initial_commit" else "64") + "}", record[key]):
                raise ValueError("Malformed binding digest or commit")
        if record["inventory_sha256"] != digest(record["inventory"]):
            raise ValueError("Inventory digest mismatch")
        _verify_inventory(record["inventory"], record["selected_extensions"], record["source_manifest_sha256"])
        _validate_manifest(record["import_manifest"])
        expected_import = build_import_plan(record["inventory"])
        if record["import_manifest"]["files"] != expected_import["desired_files"] or record["import_manifest"]["bindings"] != record["inventory"]["bindings"]:
            raise ValueError("Import manifest does not preserve the complete inventory")
        if record["import_manifest"]["source_digest"] != record["inventory"]["source_digest"]:
            raise ValueError("Import manifest differs from the bound inventory")
        seed = record["management_seed"]
        if (not isinstance(seed, dict) or set(seed) != {"distribution", "version", "resource", "files"}
                or seed["distribution"] != "qorqut-agentic" or seed["version"] != "0.1.0"
                or seed["resource"] != "qorqut_agentic/resources/management" or not isinstance(seed["files"], dict)
                or not {"agentic.yaml", "bootstrap.md", "skills/manage/SKILL.md"} <= seed["files"].keys()
                or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in seed["files"].values())):
            raise ValueError("Malformed management seed provenance")
        for path in seed["files"]:
            relative(path)
        git_directory = paths.agentic_root / ".git"
        if git_directory.is_symlink() or not git_directory.is_dir():
            raise ValueError("The initialized agentic Git repository is missing or not a local directory")
        layer = AgenticLayer.open(paths.agentic_root, paths.agentic_runtime, attribution_source="host_authenticated")
        if layer.instance_id != record["instance_id"]:
            raise ValueError("Wrong agentic module instance")
        if layer.recovery_required or not layer.catalog.validate()["valid"]:
            raise ValueError("Agentic state requires recovery or catalog correction")
        view = HostAgenticView(layer, record["inventory"])
        return layer, view
    except Exception as exc:
        raise _failure("Cannot open installed agentic layer: " + str(exc)) from exc
