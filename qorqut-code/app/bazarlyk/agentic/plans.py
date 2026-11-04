"""Deterministic import plans with explicit three-way update protection."""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping

import yaml

from .inventory import package_path
from .support import ImportFailure, decoded, digest, encoded, identifier, relative


def _files(value):
    if not isinstance(value, Mapping):
        raise ImportFailure("invalid_files", "existing_files must be a path-to-bytes mapping")
    result = {}
    for path, body in value.items():
        relative(path)
        if isinstance(body, str):
            body = body.encode()
        elif isinstance(body, dict) and set(body) == {"content_base64"}:
            body = decoded(body["content_base64"])
        if not isinstance(body, bytes):
            raise ImportFailure("invalid_files", "File values must be bytes, text or {content_base64}", path=path)
        result[path] = body
    return result


def _validate_manifest(manifest):
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or not isinstance(manifest.get("files"), dict)):
        raise ImportFailure("invalid_previous_import", "Expected an import manifest returned by apply_import_plan")
    if manifest.get("manifest_digest") != digest({k: v for k, v in manifest.items() if k != "manifest_digest"}):
        raise ImportFailure("invalid_previous_import", "Previous import manifest digest does not match")
    for path, sha in manifest["files"].items():
        relative(path)
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ImportFailure("invalid_previous_import", "Invalid file digest", path=path)
    return manifest["files"]


def _desired(inventory):
    if not isinstance(inventory, dict) or inventory.get("schema_version") != 2:
        raise ImportFailure("invalid_inventory", "Expected a version 2 source inventory; older layouts require an explicit upgrade")
    if inventory.get("source_digest") != digest(inventory.get("sources")):
        raise ImportFailure("invalid_inventory", "Source digest does not match provenance records")
    files, package_ids, destinations = {}, set(), set()
    for package in inventory["packages"]:
        package_id = identifier(package["id"])
        if package_id in package_ids:
            raise ImportFailure("invalid_inventory", "Duplicate package ID", package_id=package_id)
        package_ids.add(package_id)
        destination = relative(package["path"])
        if destination != package_path(package_id) or destination in destinations:
            raise ImportFailure("invalid_inventory", "Invalid or duplicate package destination", path=destination)
        destinations.add(destination)
        prefix = destination + "/"
        if package_id == "user-preferences":
            entries = package["entries"]
            if (len(entries) != 1 or entries[0].get("id") != "preferences"
                    or entries[0].get("kind") != "skill" or entries[0].get("path") != "SKILL.md"
                    or "SKILL.md" not in package["files"]):
                raise ImportFailure("invalid_inventory", "Malformed user-preferences singleton")
        source_ids = set()
        for path, item in package["files"].items():
            relative(path)
            runtime_resource = package_id == "runtime-instructions" and (
                path in {"index.json", "README.md"} or path.startswith("messages/") and path.endswith(".md"))
            if not (runtime_resource or path.startswith("native/") or (package_id == "user-preferences" and path == "SKILL.md") or (package_id == "imported-email-management" and path.startswith("guidance/email/"))):
                raise ImportFailure("invalid_inventory", "Imported path is not a supported native or instruction asset", path=path)
            body = decoded(item["content_base64"])
            if item["sha256"] != digest(body):
                raise ImportFailure("invalid_inventory", "Resource digest does not match its bytes", path=path)
            source_ids.add(item["provenance"]["source_id"])
            files[prefix + path] = body
        manifest = dict(schema_version=1, id=package_id, name=package["name"], description=package["description"],
                        entries=package["entries"], native=package["native"],
                        source={"import_adapter": "v1.3-offline", "sources": [record for record in inventory["sources"]
                                                                               if record["id"] in source_ids]})
        if "manifest_file" in package:
            supplied = package["manifest_file"]
            body = decoded(supplied["content_base64"])
            if digest(body) != supplied["sha256"]:
                raise ImportFailure("invalid_inventory", "Bundle manifest digest does not match")
            files[prefix + "agentic.yaml"] = body
        else:
            files[prefix + "agentic.yaml"] = yaml.safe_dump(manifest, sort_keys=True, allow_unicode=True).encode()
    return files


def build_import_plan(inventory, existing_files=None, previous_import=None):
    """Plan an empty-root import, or an update with a verified previous receipt.

    existing_files contains actual bytes (or JSON {content_base64} records), never
    hashes alone. Files outside the previous manifest remain locally owned.
    """
    desired = _desired(inventory)
    existing = {} if existing_files is None else _files(existing_files)
    if existing_files is not None and previous_import is None:
        raise ImportFailure("previous_import_required", "Updates require the explicit previous import manifest")
    if previous_import is not None and existing_files is None:
        raise ImportFailure("existing_files_required", "Updates require the explicit current file snapshot")
    previous = {} if previous_import is None else _validate_manifest(previous_import)
    changes, conflicts = [], []
    for path in sorted(set(previous) | set(desired)):
        current, target, old = existing.get(path), desired.get(path), previous.get(path)
        current_sha = None if current is None else digest(current)
        target_sha = None if target is None else digest(target)
        # Already desired includes a previously removed file that is still absent.
        if current_sha == target_sha:
            continue
        # Preserve local edits even when upstream has not changed, and surface their ownership.
        if current_sha != old:
            conflicts.append(dict(path=path, code="local_modification", previous_sha256=old,
                                  current_sha256=current_sha, desired_sha256=target_sha))
            continue
        if current is None:
            op = dict(op="create", path=path, content_base64=encoded(target))
        elif target is None:
            op = dict(op="delete", path=path, expected_sha256=current_sha)
        else:
            op = dict(op="edit", path=path, expected_sha256=current_sha, content_base64=encoded(target))
        changes.append(op)
    plan = dict(schema_version=1, packages=deepcopy(inventory["packages"]), bindings=deepcopy(inventory["bindings"]),
                coverage={**deepcopy(inventory["coverage"]), "diagnostics": deepcopy(inventory["diagnostics"])},
                changes=changes, conflicts=conflicts, source_digest=inventory["source_digest"],
                desired_files={path: digest(body) for path, body in sorted(desired.items())},
                existing_digest=digest({path: digest(body) for path, body in sorted(existing.items())}),
                previous_import_digest=None if previous_import is None else previous_import["manifest_digest"])
    plan["plan_digest"] = digest(plan)
    return plan


def apply_import_plan(layer, plan, attribution):
    """Make one validated authoring batch; return a caller-owned import receipt.

    This function does not write receipts, initialize stores, commit, publish,
    project native skills, grant permissions, or register/execute a tool.
    """
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ImportFailure("invalid_plan", "Expected a version 1 import plan")
    expected = digest({key: value for key, value in plan.items() if key != "plan_digest"})
    if plan.get("plan_digest") != expected:
        raise ImportFailure("invalid_plan", "Import plan digest does not match")
    if plan.get("conflicts"):
        raise ImportFailure("import_conflict", "Import would overwrite locally owned content", conflicts=plan["conflicts"])
    if plan.get("coverage", {}).get("complete") is not True:
        raise ImportFailure("incomplete_coverage", "Resolve inventory diagnostics before applying this plan",
                            coverage=plan.get("coverage"))
    result = layer.authoring.apply(attribution=attribution, changes=plan["changes"]) if plan["changes"] else None
    manifest = dict(schema_version=1, source_digest=plan["source_digest"], plan_digest=plan["plan_digest"],
                    previous_import_digest=plan["previous_import_digest"], existing_digest=plan["existing_digest"],
                    files=deepcopy(plan["desired_files"]), bindings=deepcopy(plan["bindings"]))
    manifest["manifest_digest"] = digest(manifest)
    return {"result": result, "no_op": not plan["changes"], "import_manifest": manifest}
