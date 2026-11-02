"""Authored package validation; no manifest can load executable handlers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Lock
from types import MappingProxyType
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator

from qorqut_operations.contracts import DomainError, freeze


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"Duplicate or non-string YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
# Dates remain portable JSON strings, never implicit datetime values.
UniqueKeyLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in values if tag != "tag:yaml.org,2002:timestamp"]
    for key, values in UniqueKeyLoader.yaml_implicit_resolvers.items()
}


def parse_yaml(text: str) -> Any:
    try:
        return yaml.load(text, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        raise DomainError("invalid_configuration", f"Invalid structured YAML: {exc}") from exc


def content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def files_digest(files: Mapping[str, bytes]) -> str:
    index = {name: content_hash(data) for name, data in sorted(files.items())}
    return content_hash(json.dumps(index, sort_keys=True, separators=(",", ":")).encode())


def _error(message: str) -> None:
    raise DomainError("invalid_artifact", message)


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        _error("Artifact paths must be nonempty relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        _error(f"Unsafe artifact path: {value}")
    return str(path)


ID = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"}
VERSION = {"type": "string", "pattern": r"^[0-9]+\.[0-9]+\.[0-9]+$"}
SKILL = {"type": "object", "additionalProperties": False, "required": ["id", "path"],
         "properties": {"id": ID, "path": {"type": "string"}}}
BASE = {"schema_version": {"const": 1}, "id": ID, "version": VERSION,
        "scope_kinds": {"type": "array", "items": {"enum": ["business", "ecosystem"]},
                        "uniqueItems": True, "minItems": 1},
        "skills": {"type": "array", "items": SKILL, "maxItems": 32}}
ROLE_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": [*BASE, "brief", "scope_brief", "default_tool_policy"],
    "properties": {**BASE, "brief": {"type": "string"},
        # Immutable historical format: Catalog.get reads stored business-scoped packages through this schema. It is not
        # active business-agent support: publishing refuses business-scoped and retired roles, and registration accepts
        # only the supported global roles.
        "scope_brief": {"enum": ["business-agent", "ecosystem-agent"]},
        "external_connections": {"type": "array", "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"},
                                 "uniqueItems": True, "maxItems": 4},
        "default_tool_policy": {"type": "object", "additionalProperties": {"enum": ["allow", "signoff", "deny"]}}}}
PLUGIN_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": [*BASE, "description", "config_schema", "tools", "rules"],
    "properties": {**BASE, "description": {"type": "string", "minLength": 1, "maxLength": 600},
        "config_schema": {"type": "string"}, "tools": {"type": "string"}, "rules": {"type": "string"}}}


def validate_schema(value: Any, schema: dict, label: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda err: str(err.path))
    if errors:
        _error(f"{label}: {errors[0].message}")


@dataclass(frozen=True)
class ArtifactPackage:
    kind: str
    manifest: Mapping[str, Any]
    manifest_path: str
    files: Mapping[str, bytes]
    digest: str
    skills: tuple[Mapping[str, Any], ...]

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def ref(self) -> dict:
        return {"id": self.id, "version": self.version, "digest": self.digest}


def _descriptor(registry: Any, name: str) -> Any:
    try:
        descriptor = registry.metadata(name)
    except (KeyError, DomainError):
        _error(f"Unknown canonical operation: {name}")
    if descriptor is None or descriptor.name != name:
        _error(f"Unknown canonical operation: {name}")
    return descriptor


def validate_policy(policy: Mapping[str, str], registry: Any) -> None:
    for name, mode in policy.items():
        descriptor = _descriptor(registry, name)
        if mode not in {"allow", "signoff", "deny"}:
            _error(f"Invalid policy mode for {name}: {mode}")
        if mode == "signoff" and not descriptor.supports_signoff:
            _error(f"Operation {name} does not support deterministic signoff")


def artifact_file(root: Path, name: str) -> bytes:
    """Read one publishable file under a resolved package root with the same checks as loading."""
    path = root / name
    if any(part.is_symlink() for part in [path, *path.parents] if part != root.parent):
        _error(f"Symlinks are not publishable: {name}")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        _error(f"Missing artifact file: {name}")
    data = path.read_bytes()
    if len(data) > 1_000_000:
        _error(f"Artifact file exceeds size limit: {name}")
    return data


def load_package(source: Path, manifest_path: str, kind: str, registry: Any) -> ArtifactPackage:
    """Snapshot selected authored bytes. Published reads use the same validator."""
    if kind not in {"role", "plugin"}:
        _error("Artifact kind must be role or plugin")
    try:
        root = Path(source).resolve(strict=True)
    except OSError as exc:
        raise DomainError("missing_artifact", "Registered artifact source is missing") from exc
    files: dict[str, bytes] = {}

    def read(name: str) -> bytes:
        name = relative_path(name)
        data = artifact_file(root, name)
        files[name] = data
        return data

    def decoded(name: str) -> str:
        try:
            return read(name).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError("invalid_artifact", f"Expected UTF-8 text: {name}") from exc

    manifest_path = relative_path(manifest_path)
    manifest = parse_yaml(decoded(manifest_path))
    validate_schema(manifest, ROLE_SCHEMA if kind == "role" else PLUGIN_SCHEMA, manifest_path)
    if kind == "role":
        brief = decoded(manifest["brief"]).strip()
        if not brief or len(brief) > 10000:
            _error("Mandatory role brief must contain 1 to 10000 characters")
        scope = "business" if manifest["scope_brief"] == "business-agent" else "ecosystem"
        if manifest["scope_kinds"] != [scope]:
            _error("Role scope_brief must agree with its scope_kinds")
        validate_policy(manifest["default_tool_policy"], registry)
    else:
        schema = parse_yaml(decoded(manifest["config_schema"]))
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise DomainError("invalid_artifact", f"Invalid plugin config schema: {exc}") from exc
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            _error("Plugin config schema must reject unknown fields")
        tools = parse_yaml(decoded(manifest["tools"]))
        rules = parse_yaml(decoded(manifest["rules"]))
        if not isinstance(tools, dict) or not isinstance(tools.get("tools"), dict) or not isinstance(rules, dict):
            _error("Plugin tools and rules must be structured mappings")
        for alias, definition in tools["tools"].items():
            if (not isinstance(definition, dict) or not isinstance(definition.get("tool"), str)
                    or not isinstance(definition.get("hidden", False), bool)):
                _error(f"Invalid plugin tool definition: {alias}")

    skills = []
    seen = set()
    allowed_extensions = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".png", ".jpg", ".jpeg", ".webp"}
    for entry in manifest["skills"]:
        if entry["id"] in seen:
            _error(f"Duplicate skill ID: {entry['id']}")
        seen.add(entry["id"])
        path = relative_path(entry["path"])
        if PurePosixPath(path).name != "SKILL.md":
            _error(f"Skill path must name SKILL.md: {path}")
        body = decoded(path)
        frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", body, re.S)
        if not frontmatter:
            _error(f"Skill requires YAML frontmatter: {path}")
        metadata = parse_yaml(frontmatter[1])
        name = f"bazarlyk-{kind}-{entry['id']}"
        if not isinstance(metadata, dict) or set(metadata) - {"name", "description", "user-invocable", "disable-model-invocation"}:
            _error(f"Procedure-only skill has unsupported frontmatter: {path}")
        if metadata.get("name") != name or not isinstance(metadata.get("description"), str) or not 1 <= len(metadata["description"]) <= 600:
            _error(f"Skill metadata must name {name} and contain a bounded description")
        for option in ("user-invocable", "disable-model-invocation"):
            if option in metadata and not isinstance(metadata[option], bool):
                _error(f"Skill option {option} must be boolean")
        subtree = root / PurePosixPath(path).parent
        for child in sorted(subtree.rglob("*")):
            if child.is_symlink():
                _error(f"Skill contains a symlink: {child.relative_to(root)}")
            if not child.is_file():
                continue
            if child.suffix.lower() not in allowed_extensions or child.stat().st_mode & 0o111:
                _error(f"Executable or unsupported skill file: {child.relative_to(root)}")
            child_path = child.relative_to(root).as_posix()
            data = read(child_path)
            if child.suffix.lower() in {".md", ".txt"} and b"!`" in data:
                _error(f"Executable skill substitutions are not supported: {child_path}")
        selected = {key: value for key, value in files.items() if key.startswith(str(PurePosixPath(path).parent) + "/")}
        skills.append({"id": entry["id"], "name": name, "description": metadata["description"],
                       "source_path": path, "subtree": str(PurePosixPath(path).parent),
                       "materialization_hash": files_digest(selected)})
    if len(files) > 200 or sum(map(len, files.values())) > 5_000_000:
        _error("Artifact exceeds the bounded file count or total size")
    return ArtifactPackage(kind, freeze(manifest), manifest_path,
                           MappingProxyType(files), files_digest(files), tuple(freeze(skill) for skill in skills))


PACKAGE_CACHE_LIMIT = 64


class Catalog:
    """Registered published artifacts; development source paths are never authority.

    Validated packages are memoized per process. A read reuses a package only
    while the store's file inventory (names, sizes, modification times, inodes,
    link status) is identical to the one recorded at verification; any change
    re-reads and re-hashes the bytes, and a mismatch falls back to the full
    load, which reports the exact conflict as before. Only the YAML parsing
    and schema validation of unchanged bytes are skipped.
    """

    def __init__(self, database: Any, runtime_root: Path, registry: Any):
        """``runtime_root`` is the instance's runtime directory; published artifacts live under ``artifacts/``."""
        self.database = database
        self.root = Path(runtime_root).resolve()
        self.registry = registry
        self._packages: OrderedDict[tuple, tuple[ArtifactPackage, tuple]] = OrderedDict()
        self._packages_lock = Lock()

    def artifact_path(self, package: ArtifactPackage) -> Path:
        return self.root / "artifacts" / (package.kind + "s") / package.id / package.version / package.digest.removeprefix("sha256:")

    def get(self, kind: str, ref: Mapping[str, str], *, connection=None) -> ArtifactPackage:
        if connection is None:
            with self.database.read() as uow:
                return self.get(kind, ref, connection=uow.connection)
        row = connection.execute(
            "SELECT * FROM artifacts WHERE kind=? AND artifact_id=? AND version=? AND digest=?",
            (kind, ref["id"], ref["version"], ref["digest"]),
        ).fetchone()
        if row is None:
            raise DomainError("missing_artifact", f"Unpublished {kind} {ref['id']}@{ref['version']}")
        root = Path(row["stored_path"])
        if not root.resolve().is_relative_to(self.root / "artifacts"):
            _error("Published artifact is outside its registered store")
        metadata = json.loads(row["metadata_json"])
        key = (kind, ref["id"], ref["version"], ref["digest"], str(root), metadata["manifest_path"])
        with self._packages_lock:
            cached = self._packages.get(key)
        if cached is not None:
            package, signature = cached
            current = self._signature(root)
            if current is not None and (current == signature or self._unchanged(root, package)):
                self._remember(key, package, current)
                return package
        package = load_package(root, metadata["manifest_path"], kind, self.registry)
        if package.digest != ref["digest"]:
            raise DomainError("artifact_conflict", f"Published bytes changed: {kind} {ref['id']}@{ref['version']}")
        if self._published_files(root) != set(package.files):
            raise DomainError("artifact_conflict", f"Published file set changed: {kind} {ref['id']}")
        self._remember(key, package, self._signature(root))
        return package

    def _remember(self, key, package, signature):
        if signature is None:
            return
        with self._packages_lock:
            self._packages[key] = (package, signature)
            self._packages.move_to_end(key)
            while len(self._packages) > PACKAGE_CACHE_LIMIT:
                self._packages.popitem(last=False)

    @staticmethod
    def _signature(root: Path):
        """Cheap inventory of everything under the store root, or None when unreadable."""
        try:
            root = root.resolve(strict=True)
            entries = []
            for directory, names, files in os.walk(root, followlinks=False):
                for name in (*names, *files):
                    path = Path(directory) / name
                    info = path.lstat()
                    entries.append((path.relative_to(root).as_posix(), path.is_symlink(),
                                    info.st_size, info.st_mtime_ns, info.st_ino, info.st_mode))
        except OSError:
            return None
        return tuple(sorted(entries))

    @staticmethod
    def _published_files(root: Path) -> set[str]:
        return {item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_file() or item.is_symlink()}

    @staticmethod
    def _unchanged(root: Path, package: ArtifactPackage) -> bool:
        """True only when the store still holds exactly the validated regular files and bytes.

        Any difference falls back to the full load, which reports the exact
        conflict. The resolved root has no symlinked ancestors by construction.
        """
        try:
            root = root.resolve(strict=True)
            actual = set()
            for item in root.rglob("*"):
                if item.is_symlink():
                    return False
                if item.is_file():
                    actual.add(item.relative_to(root).as_posix())
            if actual != set(package.files):
                return False
            files = {}
            for name in package.files:
                data = (root / name).read_bytes()
                if len(data) > 1_000_000:
                    return False
                files[name] = data
        except OSError:
            return False
        return files_digest(files) == package.digest


_DECLARED_TOOLS: dict[str, frozenset[str]] = {}
_DECLARED_TOOLS_LOCK = Lock()


def _plugin_tools(package):
    with _DECLARED_TOOLS_LOCK:
        known = _DECLARED_TOOLS.get(package.digest)
    if known is not None:
        return known
    definitions = parse_yaml(package.files[package.manifest["tools"]].decode())["tools"]
    tools = (frozenset(value["tool"] for value in definitions.values()),
             frozenset(value["tool"] for value in definitions.values() if not value.get("hidden")))
    with _DECLARED_TOOLS_LOCK:
        if len(_DECLARED_TOOLS) >= PACKAGE_CACHE_LIMIT:
            _DECLARED_TOOLS.clear()
        _DECLARED_TOOLS[package.digest] = tools
    return tools


def declared_plugin_tools(package):
    """Direct capability grants belong to the exact published package, not just its ID."""
    return _plugin_tools(package)[0]


def visible_plugin_tools(package):
    """Discovery subset: hidden aliases stay callable but are never listed."""
    return _plugin_tools(package)[1]
