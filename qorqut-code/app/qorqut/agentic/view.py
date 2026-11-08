"""Read current authored instructions using a separately verified native inventory.

The inventory is installation-owned authority. Nothing in an editable manifest
can create a native binding, enable an import, or change a native package's
permissions. All tree reads go through the module's public catalog API.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import posixpath

from qorqut_agentic.errors import AgenticError
from qorqut_operations.contracts import DomainError

from qorqut.agents.roles import LEARNING_APPLIER_ROLE

from .inventory import package_path
from .support import MAX_FILE_BYTES, ImportFailure, decoded, digest, identifier, relative, read_yaml


CONSISTENCY_ATTEMPTS = 3
# The host-seeded Overseer package belongs only to the identity that serves
# Overseer. The general shared procedural catalog keeps every other package.
OVERSEER_PACKAGE = "overseer"
OVERSEER_ROLE = LEARNING_APPLIER_ROLE


def _role_id(profile):
    role = profile.get("role") if isinstance(profile, dict) else getattr(profile, "role", None)
    if isinstance(role, dict):
        return role.get("id")
    return getattr(role, "id", None)


def _invalid(message):
    raise DomainError("invalid_agentic_inventory", message)


def _bytes(resource):
    if resource["encoding"] == "utf-8":
        body = resource["content"].encode("utf-8")
    elif resource["encoding"] == "base64":
        body = base64.b64decode(resource["content"], validate=True)
    else:
        raise DomainError("invalid_agentic_resource", "Unsupported managed resource encoding")
    if hashlib.sha256(body).hexdigest() != resource["sha256"]:
        raise DomainError("agentic_revision_conflict", "Managed resource changed during reading", retryable=True)
    return body


class HostAgenticView:
    def __init__(self, layer, inventory):
        self._layer = layer
        self._inventory = deepcopy(inventory)
        try:
            self._validate_inventory()
        except (KeyError, TypeError, ValueError, ImportFailure) as exc:
            _invalid("Inconsistent host-owned agentic inventory: " + str(exc))

    def _validate_inventory(self):
        inv = self._inventory
        if inv["schema_version"] != 2 or inv["coverage"]["complete"] is not True:
            _invalid("A complete version 2 inventory is required; older layouts require an explicit upgrade")
        if inv["source_digest"] != digest(inv["sources"]):
            _invalid("Inventory source digest does not match")
        if any(row["severity"] in {"error", "pending"} for row in inv["diagnostics"]):
            _invalid("Inventory has unresolved coverage diagnostics")
        self._sources, self._packages, self._entries = {}, {}, {}
        for source in inv["sources"]:
            source_id = identifier(source["id"])
            if source_id in self._sources or type(source["enabled"]) is not bool:
                _invalid("Duplicate or invalid inventory source")
            self._sources[source_id] = source
        destinations = set()
        for package in inv["packages"]:
            package_id = identifier(package["id"])
            if package_id in self._packages or type(package["native"]["enabled"]) is not bool:
                _invalid("Duplicate or invalid inventory package")
            destination = relative(package["path"])
            if destination != package_path(package_id) or destination in destinations:
                _invalid("Invalid or duplicate package destination")
            destinations.add(destination)
            self._packages[package_id] = package
            if package_id == "user-preferences":
                entries = package["entries"]
                if (len(entries) != 1 or entries[0].get("id") != "preferences"
                        or entries[0].get("kind") != "skill" or entries[0].get("path") != "SKILL.md"
                        or "SKILL.md" not in package["files"]):
                    _invalid("Malformed user-preferences singleton")
            if "manifest_file" in package:
                item = package["manifest_file"]
                body = decoded(item["content_base64"])
                source = self._sources.get(item["source_id"])
                manifest = read_yaml(body)
                expected = {(e["id"], e["kind"], e["path"]) for e in package["entries"]}
                supplied = {(e.get("id"), e.get("kind"), e.get("path")) for e in manifest.get("entries", [])}
                if (source is None or item["source_path"] != destination + "/agentic.yaml"
                        or item["sha256"] != digest(body) or source["files"].get(item["source_path"]) != item["sha256"]
                        or manifest.get("id") != package_id or expected != supplied
                        or len(supplied) != len(manifest.get("entries", []))):
                    _invalid("Invalid bundled package manifest or provenance")
            for path, item in package["files"].items():
                relative(path)
                if item["sha256"] != digest(decoded(item["content_base64"])):
                    _invalid("Inventory file digest does not match")
                if item["provenance"]["source_id"] not in self._sources:
                    _invalid("Inventory file has an unknown source")
            for entry in package["entries"]:
                ref = package_id + "/" + identifier(entry["id"])
                if ref in self._entries or entry["path"] not in package["files"]:
                    _invalid("Duplicate or missing inventory entry")
                if entry["kind"] == "template" and package["native"]["enabled"]:
                    _invalid("Imported templates must be isolated in inactive packages")
                self._entries[ref] = entry
        self._assets = {}
        asset_destinations = set()
        for asset in inv["assets"]:
            source_id, path = asset["source_id"], relative(asset["path"])
            package_id = asset["package_id"]
            prefix = self._packages[package_id]["path"] + "/"
            destination = relative(asset["destination"])
            if source_id not in self._sources or package_id not in self._packages or not destination.startswith(prefix):
                _invalid("Asset has an unknown source, package or destination")
            if destination in asset_destinations:
                _invalid("Duplicate asset destination")
            asset_destinations.add(destination)
            item = self._packages[package_id]["files"][destination[len(prefix):]]
            if item["sha256"] != asset["sha256"]:
                _invalid("Asset and package digests differ")
            if type(asset["enabled"]) is not bool or type(asset["template"]) is not bool:
                _invalid("Invalid asset activation state")
            if asset["template"] and self._packages[package_id]["native"]["enabled"]:
                _invalid("Imported template resources must be isolated in inactive packages")
            self._assets.setdefault((source_id, path), []).append(asset)
            if "native_source" in asset:
                from qorqut_agentic.behavior.sources import location
                alias = asset["native_source"]
                if (set(alias) != {"source_id", "path"} or alias["source_id"] not in self._sources
                        or source_id != "roles" or location(alias["source_id"], alias["path"]) != path):
                    _invalid("Invalid instruction source alias")
                self._assets.setdefault((alias["source_id"], alias["path"]), []).append(asset)
        self._bindings = {}
        for binding in inv["bindings"]:
            key = binding["kind"], identifier(binding["native_id"])
            if key[0] not in {"builtin", "role", "plugin", "extension", "skill"} or key in self._bindings:
                _invalid("Duplicate or unknown native binding")
            if binding["source_id"] not in self._sources or binding["package_id"] not in self._packages:
                _invalid("Binding has an unknown source or package")
            if type(binding["enabled"]) is not bool or type(binding["template"]) is not bool:
                _invalid("Invalid binding activation state")
            if binding["enabled"] and (binding["template"] or not self._packages[binding["package_id"]]["native"]["enabled"]
                                       or not self._sources[binding["source_id"]]["enabled"]):
                _invalid("Binding activation disagrees with its trusted package or source")
            if any(ref not in self._entries for ref in binding["entry_refs"]):
                _invalid("Binding has an unknown entry")
            seen = set()
            for skill in binding.get("skills", ()):
                skill_id = identifier(skill["native_skill_id"])
                ref = skill["canonical_entry"]
                if skill_id in seen or ref not in binding["entry_refs"] or self._entries[ref]["kind"] not in {"skill", "template"}:
                    _invalid("Invalid canonical native skill binding")
                seen.add(skill_id)
            self._bindings[key] = binding

    @property
    def layer(self):
        """The opened module supplied by installation; never reopened or rebound."""
        return self._layer

    def selection(self, profile, *, project_id=None, work_type=None) -> dict:
        """Shared procedural catalog routing; never a native operational grant.

        The caller authorizes the project using the current registered profile.
        Native projection independently uses its validated role/plugin pins. The
        host-seeded Overseer package is eligible only to the current validated
        role that serves Overseer; every other shared package keeps the general
        routing rule.
        """
        for key, value in (("project_id", project_id), ("work_type", work_type)):
            if value is not None and not isinstance(value, str):
                raise DomainError("invalid_agentic_selection", key + " must be a string")

        def read():
            report = self.layer.catalog.validate()
            role_id = _role_id(profile)
            candidates = []
            for package in report["packages"]:
                package_id = package["id"]
                known = self._packages.get(package_id)
                if known is not None and not known["native"]["enabled"]:
                    continue
                if package.get("area") == "overseer" and role_id != OVERSEER_ROLE:
                    continue
                if not report["valid"] and not self.layer.catalog.validate(package_ids=[package_id])["valid"]:
                    continue
                candidates.append(package_id)
            return {"package_ids": sorted(candidates), "project_id": project_id, "work_type": work_type}

        return self.consistent(read)

    @property
    def catalog_revision(self) -> str:
        result = self.layer.catalog.revision()
        if result["recovery_required"]:
            raise DomainError("agentic_unavailable", "Managed instruction recovery is required")
        return result["catalog_revision"]

    @contextmanager
    def read_snapshot(self):
        """Bind a request to one catalogue and validate it before returning."""
        try:
            with self.layer.catalog.read_snapshot():
                yield
        except AgenticError as exc:
            if exc.code == "revision_conflict":
                raise DomainError("agentic_revision_conflict", "Managed instructions changed during projection; retry",
                                  retryable=True) from exc
            raise DomainError("agentic_unavailable", exc.message, details={"agentic_code": exc.code}) from exc

    def consistent(self, reader, *, revision=None):
        """Return a whole projection from one observed revision, or fail boundedly.

        Readers must be read-only. A caller supplying a configuration revision
        cannot silently switch to a newer configuration halfway through a write.
        """
        for _ in range(CONSISTENCY_ATTEMPTS):
            error = None
            with self.layer.catalog.read_session():
                before = self.catalog_revision
                if revision is not None and revision != before:
                    break
                try:
                    result = reader()
                except AgenticError as exc:
                    error = exc
            if error is not None and error.code == "revision_conflict":
                raise DomainError("agentic_revision_conflict", "Managed instructions changed during projection; retry", retryable=True) from error

            if self.catalog_revision == before:
                if error is not None:
                    raise DomainError("agentic_unavailable", error.message,
                                      details={"agentic_code": error.code}) from error
                return result
        raise DomainError("agentic_revision_conflict", "Managed instructions changed during projection; retry with current configuration",
                          retryable=True)

    def resource(self, source_id, source_path, *, symbol=None) -> dict:
        relative(source_path)
        assets = [asset for asset in self._assets.get((source_id, source_path), ())
                  if asset.get("symbol") == symbol]
        if len(assets) != 1:
            raise DomainError("agentic_resource_unavailable", "Managed source has no unique imported asset",
                              details={"source_id": source_id, "path": source_path, "symbol": symbol})
        asset = assets[0]
        prefix = self._packages[asset["package_id"]]["path"] + "/"
        try:
            return self.layer.catalog.read_resource(asset["package_id"], asset["destination"][len(prefix):],
                                                    max_bytes=MAX_FILE_BYTES)
        except AgenticError as exc:
            if exc.code == "revision_conflict":
                raise DomainError("agentic_revision_conflict", "Managed instructions changed during projection; retry", retryable=True) from exc
            raise DomainError("agentic_resource_unavailable", exc.message,
                              details={"source_id": source_id, "path": source_path, "agentic_code": exc.code}) from exc

    def runtime_instructions(self):
        """Current protected runtime prose; never use bundled code strings as fallback."""
        from qorqut_agentic.behavior.instructions import from_catalog

        def read():
            def resource(path):
                value = self.resource("roles", "operations/runtime-instructions/" + path)
                return _bytes(value).decode("utf-8")
            return from_catalog(resource)

        return self.consistent(read)

    def tool_instructions(self):
        """Current tool wording, independent of the registered tool's authority."""
        from qorqut_agentic.behavior.tool_help import ToolInstructions
        def read():
            return ToolInstructions(_bytes(self.resource("roles", "operations/tool-instructions/messages.json")))
        return self.consistent(read)

    def role_brief(self, native_package) -> dict:
        """Read the current role instruction through its trusted native binding."""
        if native_package.kind != "role":
            _invalid("Native package kind does not match its requested binding")
        binding = self._bindings.get(("role", native_package.id))
        if not binding or not binding["enabled"] or binding["template"]:
            raise DomainError("agentic_role_unavailable", "Native role has no active host-owned binding")
        refs = [ref for ref in binding["entry_refs"]
                if self._entries[ref]["kind"] == "instruction"
                and self._packages[ref.split("/", 1)[0]]["path"].startswith("roles/")]
        if len(refs) != 1:
            _invalid("Native role has no unique canonical instruction")
        ref = refs[0]
        entry = self._entries[ref]
        if entry.get("native", {}).get("template"):
            raise DomainError("agentic_role_unavailable", "Template cannot become a native role instruction")
        expected_root = self._packages[ref.split("/", 1)[0]]["path"] + "/" + entry["path"]

        def read():
            current = self.layer.catalog.read_entry(ref)
            described, body = current["entry"], current["body"]
            if (described["ref"] != ref or described["kind"] != "instruction"
                    or described["path"] != entry["path"] or described["root_path"] != expected_root
                    or not isinstance(body, str) or not body.strip()
                    or not isinstance(described["revision"], str) or not described["revision"]):
                raise DomainError("agentic_role_unavailable", "Canonical role instruction moved or is invalid")
            return {"body": body, "path": described["root_path"], "revision": described["revision"],
                    "sha256": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()}

        return self.consistent(read)

    def skill_files(self, kind, native_package, native_skill_id, *, selection) -> dict[str, bytes]:
        if kind != native_package.kind:
            _invalid("Native package kind does not match its requested binding")
        binding = self._bindings.get((kind, native_package.id))
        if not binding or not binding["enabled"] or binding["template"]:
            raise DomainError("agentic_skill_unavailable", "Native skill has no active host-owned binding")
        if not any(skill["id"] == native_skill_id for skill in native_package.skills):
            raise DomainError("agentic_skill_unavailable", "Skill is not declared by the validated native package")
        matches = [skill for skill in binding.get("skills", ()) if skill["native_skill_id"] == native_skill_id]
        if len(matches) != 1:
            _invalid("Native skill has no unique canonical entry")
        ref = matches[0]["canonical_entry"]
        entry = self._entries[ref]
        if entry.get("native", {}).get("template") or entry["kind"] != "skill":
            raise DomainError("agentic_skill_unavailable", "Template skill cannot become a native instruction")
        package_id = ref.split("/", 1)[0]
        if selection.get("package_ids") is None or package_id not in selection["package_ids"]:
            raise DomainError("agentic_skill_unavailable", "Native skill is outside the current selection")

        def read():
            current = self.layer.catalog.read_entry(ref, selection=selection)
            described = current["entry"]
            if described["kind"] != "skill" or described["path"] != entry["path"] or not described["applicable"]:
                raise DomainError("agentic_skill_unavailable", "Canonical skill moved or is no longer applicable")
            root = posixpath.dirname(entry["path"])
            prefix = root + "/" if root else ""
            paths = [entry["path"], *(item["path"] for item in current["resources"])]
            result = {}
            for path in paths:
                if not path.startswith(prefix):
                    _invalid("Skill resource escaped its canonical root")
                resource = self.layer.catalog.read_resource(package_id, path, selection=selection, max_bytes=MAX_FILE_BYTES)
                result[relative(path[len(prefix):])] = _bytes(resource)
            return result

        return self.consistent(read)
