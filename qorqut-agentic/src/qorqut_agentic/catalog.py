"""Catalog model, loader and validator.

``load_catalog`` builds an immutable view from any file source (the disk or a
disk-plus-pending-changes overlay), so the same validation judges the current
tree and a projected batch. Only valid entries appear in ``Catalog.entries``;
every problem is reported as an :class:`Issue`.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from .errors import InvalidRequestError, UnsafePathError
from .files import FileEntry, split_path
from .schemas import (
    ACQUISITION_MODES,
    DEFAULT_TOOL_LIMITS,
    MANIFEST_SCHEMA,
    TEXT_REF_KINDS,
    TOOL_DEFINITION_SCHEMA,
    WORKFLOW_SCHEMA,
    check_embedded_schema,
    schema_issues,
    validate_instance,
)
from .util import is_id, load_yaml, sha256_hex, split_frontmatter

PACKAGES_DIR = "packages"
MANIFEST_NAME = "agentic.yaml"


class Source(Protocol):
    prefix: str
    files: dict[str, FileEntry]
    special: dict[str, str]

    def read(self, rel: str) -> bytes: ...


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: str = "error"
    package_id: str | None = None
    entry_ref: str | None = None
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "package_id": self.package_id,
            "entry_ref": self.entry_ref,
            "path": self.path,
        }

    def key(self) -> tuple[str, str, str | None, str | None, str | None]:
        return (self.severity, self.code, self.package_id, self.entry_ref, self.message)


@dataclass(frozen=True)
class ResourceInfo:
    path: str  # package-relative
    root_path: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "root_path": self.root_path, "sha256": self.sha256, "size": self.size}


@dataclass
class Package:
    id: str
    name: str
    description: str
    dir: str  # root-relative, e.g. packages/email-composition
    manifest_path: str
    manifest_sha256: str
    source: dict[str, Any] | None
    native: dict[str, Any] | None
    entry_ids: list[str] = field(default_factory=list)


@dataclass
class Entry:
    ref: str
    package_id: str
    entry_id: str
    kind: str
    description: str
    when_to_use: str
    path: str  # package-relative
    root_path: str
    declared_requires: tuple[str, ...]
    requires: tuple[str, ...]  # declared plus references made inside a workflow definition
    work_types: tuple[str, ...]
    project_ids: tuple[str, ...]
    native: dict[str, Any] | None
    revision: str
    bundle_revision: str
    resources: tuple[ResourceInfo, ...]
    definition: dict[str, Any] | None = None  # parsed tool/workflow definition
    code_revision: str | None = None  # tools: digest of code and resources

    @property
    def scope(self) -> str:
        return "scoped" if (self.work_types or self.project_ids) else "global"


@dataclass(frozen=True)
class Selection:
    """What the caller may see. ``None`` fields mean "not restricted/unspecified"."""

    package_ids: frozenset[str] | None = None
    project_id: str | None = None
    work_type: str | None = None

    @classmethod
    def from_value(cls, value: "Selection | dict[str, Any] | None") -> "Selection":
        if value is None:
            return cls()
        if isinstance(value, Selection):
            return value
        if not isinstance(value, dict):
            raise InvalidRequestError("selection must be an object")
        unknown = set(value) - {"package_ids", "project_id", "work_type"}
        if unknown:
            raise InvalidRequestError(f"unknown selection field(s): {sorted(unknown)}")
        package_ids = value.get("package_ids")
        if package_ids is not None:
            if not isinstance(package_ids, (list, tuple, set, frozenset)) or not all(
                isinstance(item, str) for item in package_ids
            ):
                raise InvalidRequestError("selection.package_ids must be a list of strings")
            package_ids = frozenset(package_ids)
        for key in ("project_id", "work_type"):
            if value.get(key) is not None and not isinstance(value[key], str):
                raise InvalidRequestError(f"selection.{key} must be a string")
        return cls(package_ids=package_ids, project_id=value.get("project_id"), work_type=value.get("work_type"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_ids": None if self.package_ids is None else sorted(self.package_ids),
            "project_id": self.project_id,
            "work_type": self.work_type,
        }


def bundle_digest(parts: Iterable[tuple[str, str]]) -> str:
    """Digest of ``(path, sha256)`` pairs, independent of order."""
    text = "".join(f"{path}\0{sha}\n" for path, sha in sorted(set(parts)))
    return sha256_hex(text.encode("utf-8"))


class Catalog:
    def __init__(self, source: Source) -> None:
        self.source = source
        self.packages: dict[str, Package] = {}
        self.entries: dict[str, Entry] = {}
        self.issues: list[Issue] = []
        self.revision = bundle_digest((rel, entry.sha256) for rel, entry in source.files.items() if rel.startswith(PACKAGES_DIR + "/"))

    # -- lookups ---------------------------------------------------------------
    def get(self, ref: str) -> Entry | None:
        return self.entries.get(ref)

    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    def read_text(self, rel: str) -> str:
        return self.source.read(rel).decode("utf-8")

    def dependency_closure(self, ref: str) -> list[str]:
        """Transitive requirements of ``ref``, dependencies before dependents."""
        order: list[str] = []
        seen: set[str] = set()

        def visit(current: str) -> None:
            entry = self.entries.get(current)
            if entry is None:
                return
            for req in entry.requires:
                if req not in seen:
                    seen.add(req)
                    visit(req)
                    order.append(req)

        seen.add(ref)
        visit(ref)
        return order

    # -- selection -------------------------------------------------------------
    def availability(self, entry: Entry, selection: Selection) -> tuple[bool, list[str]]:
        """Whether ``entry`` and its dependencies are enabled for the selection."""
        diagnostics: list[str] = []
        if selection.package_ids is not None and entry.package_id not in selection.package_ids:
            return False, [f"package {entry.package_id!r} is not enabled for this selection"]
        for req in self.dependency_closure(entry.ref):
            target = self.entries.get(req)
            if target is None:
                diagnostics.append(f"dependency {req} is not available")
            elif selection.package_ids is not None and target.package_id not in selection.package_ids:
                diagnostics.append(f"dependency {req} is unavailable: package {target.package_id!r} is not enabled")
        return (not diagnostics), diagnostics

    @staticmethod
    def applicability(entry: Entry, selection: Selection) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if selection.work_type is not None and entry.work_types and selection.work_type not in entry.work_types:
            reasons.append(f"applies to work types {list(entry.work_types)}, not {selection.work_type!r}")
        if selection.project_id is not None and entry.project_ids and selection.project_id not in entry.project_ids:
            reasons.append(f"applies to projects {list(entry.project_ids)}, not {selection.project_id!r}")
        return (not reasons), reasons

    def describe(self, entry: Entry, selection: Selection) -> dict[str, Any]:
        available, unavailable = self.availability(entry, selection)
        applicable, inapplicable = self.applicability(entry, selection)
        package = self.packages[entry.package_id]
        return {
            "ref": entry.ref,
            "package_id": entry.package_id,
            "entry_id": entry.entry_id,
            "kind": entry.kind,
            "description": entry.description,
            "when_to_use": entry.when_to_use,
            "path": entry.path,
            "root_path": entry.root_path,
            "scope": entry.scope,
            "applicability": {"work_types": list(entry.work_types), "project_ids": list(entry.project_ids)},
            "requires": list(entry.requires),
            "source": package.source,
            "revision": entry.revision,
            "bundle_revision": entry.bundle_revision,
            "code_revision": entry.code_revision,
            "available": available,
            "applicable": applicable,
            "diagnostics": unavailable + inapplicable,
        }

    def search(
        self,
        selection: Selection,
        *,
        query: str | None = None,
        kinds: Iterable[str] | None = None,
        package_ids: Iterable[str] | None = None,
        include_unavailable: bool = False,
        include_inapplicable: bool = False,
    ) -> list[dict[str, Any]]:
        kind_filter = set(kinds) if kinds else None
        package_filter = set(package_ids) if package_ids else None
        tokens = [token for token in (query or "").lower().split() if token]
        results = []
        for ref in sorted(self.entries):
            entry = self.entries[ref]
            if kind_filter is not None and entry.kind not in kind_filter:
                continue
            if package_filter is not None and entry.package_id not in package_filter:
                continue
            described = self.describe(entry, selection)
            if not described["available"] and not include_unavailable:
                continue
            if not described["applicable"] and not include_inapplicable:
                continue
            if tokens:
                package = self.packages[entry.package_id]
                haystack = " ".join(
                    [
                        entry.ref,
                        entry.kind,
                        entry.description,
                        entry.when_to_use,
                        package.name,
                        package.description,
                        *entry.work_types,
                        *entry.project_ids,
                    ]
                ).lower()
                if not all(token in haystack for token in tokens):
                    continue
            results.append(described)
        return results


def _package_of(rel: str) -> str | None:
    parts = rel.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == PACKAGES_DIR else None


def load_catalog(source: Source) -> Catalog:
    catalog = Catalog(source)
    issues = catalog.issues
    by_dir: dict[str, list[str]] = {}
    for rel in sorted(source.files):
        parts = rel.split("/")
        if parts[0] != PACKAGES_DIR:
            continue
        if len(parts) == 2:
            issues.append(Issue("stray_file", f"file directly under {PACKAGES_DIR}/ is ignored: {rel}", "warning", path=rel))
        else:
            by_dir.setdefault(parts[1], []).append(rel)
    for rel, kind in sorted(source.special.items()):
        issues.append(
            Issue("symlink_not_allowed" if kind == "symlink" else "special_file_not_allowed",
                  f"{kind} in the catalog tree is not allowed: {rel}", package_id=_package_of(rel), path=rel)
        )
        pkg_dir = _package_of(rel)
        if pkg_dir:
            by_dir.setdefault(pkg_dir, [])

    built: dict[str, Entry] = {}
    declared: dict[str, str] = {}  # ref -> package presence, for messages
    for dir_name in sorted(by_dir):
        _load_package(catalog, dir_name, built, declared)

    valid = dict(built)
    _prune_unresolvable(catalog, valid, declared)
    _prune_cycles(catalog, valid)
    _prune_unresolvable(catalog, valid, declared)
    catalog.entries = valid
    for package in catalog.packages.values():
        package.entry_ids = sorted(entry.entry_id for entry in valid.values() if entry.package_id == package.id)
    return catalog


def _load_package(catalog: Catalog, dir_name: str, built: dict[str, Entry], declared: dict[str, str]) -> None:
    source, issues = catalog.source, catalog.issues
    if not is_id(dir_name):
        issues.append(Issue("invalid_package_dir", f"package directory name is not a valid ID: {dir_name!r}",
                            path=f"{PACKAGES_DIR}/{dir_name}"))
        return
    pkg_dir = f"{PACKAGES_DIR}/{dir_name}"
    manifest_path = f"{pkg_dir}/{MANIFEST_NAME}"
    if manifest_path not in source.files:
        issues.append(Issue("missing_manifest", f"package {dir_name!r} has no {MANIFEST_NAME}", package_id=dir_name, path=manifest_path))
        return
    raw_bytes = source.read(manifest_path)
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        issues.append(Issue("invalid_manifest", f"{MANIFEST_NAME} is not UTF-8", package_id=dir_name, path=manifest_path))
        return
    try:
        manifest = load_yaml(text)
    except ValueError as exc:
        issues.append(Issue("invalid_manifest", str(exc), package_id=dir_name, path=manifest_path))
        return
    problems = schema_issues(manifest, MANIFEST_SCHEMA)
    if problems:
        for problem in problems:
            issues.append(Issue("invalid_manifest", problem, package_id=dir_name, path=manifest_path))
        return
    if manifest["id"] != dir_name:
        issues.append(Issue("package_id_mismatch",
                            f"manifest id {manifest['id']!r} must equal its directory name {dir_name!r}",
                            package_id=dir_name, path=manifest_path))
        return
    package = Package(
        id=dir_name,
        name=manifest["name"],
        description=manifest["description"],
        dir=pkg_dir,
        manifest_path=manifest_path,
        manifest_sha256=sha256_hex(raw_bytes),
        source=manifest.get("source"),
        native=manifest.get("native"),
    )
    catalog.packages[dir_name] = package
    seen: set[str] = set()
    for raw in manifest["entries"]:
        ref = f"{dir_name}/{raw['id']}"
        if raw["id"] in seen:
            issues.append(Issue("duplicate_entry_id", f"duplicate entry id {raw['id']!r}", package_id=dir_name,
                                entry_ref=ref, path=manifest_path))
            built.pop(ref, None)
            continue
        seen.add(raw["id"])
        declared[ref] = "present"
        entry = _build_entry(catalog, package, raw)
        if entry is not None:
            built[ref] = entry


def _files_under(source: Source, prefix: str) -> list[str]:
    return sorted(rel for rel in source.files if rel.startswith(prefix + "/"))


def _build_entry(catalog: Catalog, package: Package, raw: dict[str, Any]) -> Entry | None:
    source, issues = catalog.source, catalog.issues
    ref = f"{package.id}/{raw['id']}"
    kind = raw["kind"]

    def problem(code: str, message: str, path: str | None = None) -> None:
        issues.append(Issue(code, message, package_id=package.id, entry_ref=ref, path=path or package.manifest_path))

    try:
        split_path(raw["path"], field="entry path")
    except UnsafePathError as exc:
        problem("unsafe_entry_path", exc.message)
        return None
    root_path = f"{package.dir}/{raw['path']}"
    if root_path in source.special:
        problem("symlink_not_allowed", f"entry file is a {source.special[root_path]}: {root_path}", root_path)
        return None
    if root_path not in source.files:
        problem("missing_entry_file", f"entry file does not exist: {root_path}", root_path)
        return None
    data = source.read(root_path)
    text: str | None = None
    if kind != "template":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problem("invalid_encoding", f"{kind} file is not UTF-8: {root_path}", root_path)
            return None

    resources: list[ResourceInfo] = []
    definition: dict[str, Any] | None = None
    implied: list[str] = []
    code_revision: str | None = None
    rel_dir = posixpath.dirname(raw["path"])

    if kind == "skill":
        if posixpath.basename(raw["path"]) != "SKILL.md":
            problem("invalid_skill_path", f"skill entry path must end in SKILL.md: {raw['path']}", root_path)
            return None
        _, frontmatter_error = split_frontmatter(text or "")
        if frontmatter_error:
            problem("invalid_frontmatter", f"SKILL.md frontmatter: {frontmatter_error}", root_path)
            return None
        base = f"{package.dir}/{rel_dir}" if rel_dir else package.dir
        for rel in _files_under(source, base):
            if rel == root_path or rel == package.manifest_path:
                continue
            resources.append(_resource(package, rel, source.files[rel]))
    elif kind == "tool":
        definition = _build_tool_definition(catalog, package, ref, raw, text or "", root_path, problem)
        if definition is None:
            return None
        resources = definition.pop("_resources")
        code_revision = definition.pop("_code_revision")
    elif kind == "workflow":
        definition = _build_workflow_definition(raw, text or "", root_path, problem)
        if definition is None:
            return None
        for step in definition["steps"]:
            if "instruction_ref" in step:
                implied.append(step["instruction_ref"])
            if "verifier" in step:
                implied.append(step["verifier"])

    applicability = raw.get("applicability", {})
    declared_requires = tuple(raw.get("requires", ()))
    for target in declared_requires:
        if target == ref:
            problem("dependency_cycle", f"{ref} requires itself", root_path)
            return None
    requires = tuple(dict.fromkeys([*declared_requires, *implied]))
    revision = sha256_hex(data)
    bundle = bundle_digest([(root_path, revision), *((r.root_path, r.sha256) for r in resources)])
    return Entry(
        ref=ref,
        package_id=package.id,
        entry_id=raw["id"],
        kind=kind,
        description=raw["description"],
        when_to_use=raw["when_to_use"],
        path=raw["path"],
        root_path=root_path,
        declared_requires=declared_requires,
        requires=requires,
        work_types=tuple(applicability.get("work_types", ())),
        project_ids=tuple(applicability.get("project_ids", ())),
        native=raw.get("native"),
        revision=revision,
        bundle_revision=bundle,
        resources=tuple(resources),
        definition=definition,
        code_revision=code_revision,
    )


def _resource(package: Package, rel: str, info: FileEntry) -> ResourceInfo:
    return ResourceInfo(path=rel[len(package.dir) + 1 :], root_path=rel, sha256=info.sha256, size=info.size)


def _build_tool_definition(catalog: Catalog, package: Package, ref: str, raw: dict[str, Any], text: str,
                           root_path: str, problem) -> dict[str, Any] | None:
    source = catalog.source
    try:
        definition = load_yaml(text)
    except ValueError as exc:
        problem("invalid_tool_definition", str(exc), root_path)
        return None
    found = schema_issues(definition, TOOL_DEFINITION_SCHEMA)
    if found:
        for item in found:
            problem("invalid_tool_definition", item, root_path)
        return None
    for key in ("input_schema", "output_schema"):
        bad = check_embedded_schema(definition[key])
        if bad:
            problem("invalid_tool_definition", f"{key} is not a usable JSON Schema: {bad}", root_path)
            return None
    resources: dict[str, ResourceInfo] = {}

    def add_path(declared_path: str, what: str, *, directory_ok: bool) -> bool:
        try:
            split_path(declared_path, field=what)
        except UnsafePathError as exc:
            problem("unsafe_tool_path", exc.message, root_path)
            return False
        full = f"{package.dir}/{declared_path}"
        if full in source.special:
            problem("symlink_not_allowed", f"{what} is a {source.special[full]}: {full}", full)
            return False
        if full in source.files:
            resources[full] = _resource(package, full, source.files[full])
            return True
        nested = _files_under(source, full)
        if nested and directory_ok:
            for rel in nested:
                resources[rel] = _resource(package, rel, source.files[rel])
            return True
        problem("missing_tool_resource", f"{what} does not exist: {full}", full)
        return False

    if not add_path(definition["code"], "tool code", directory_ok=False):
        return None
    code_full = f"{package.dir}/{definition['code']}"
    for extra in definition.get("resources", ()):
        if not add_path(extra, "tool resource", directory_ok=True):
            return None
    ordered = [resources[code_full]] + [r for key, r in sorted(resources.items()) if key != code_full]
    normalized = dict(definition)
    normalized["limits"] = {**DEFAULT_TOOL_LIMITS, **definition.get("limits", {})}
    normalized["_resources"] = ordered
    normalized["_code_revision"] = bundle_digest((r.root_path, r.sha256) for r in ordered)
    return normalized


def _build_workflow_definition(raw: dict[str, Any], text: str, root_path: str, problem) -> dict[str, Any] | None:
    try:
        definition = load_yaml(text)
    except ValueError as exc:
        problem("invalid_workflow", str(exc), root_path)
        return None
    found = schema_issues(definition, WORKFLOW_SCHEMA)
    if found:
        for item in found:
            problem("invalid_workflow", item, root_path)
        return None
    messages = workflow_semantic_issues(definition)
    if definition["id"] != raw["id"]:
        messages.append(f"workflow id {definition['id']!r} must equal its entry id {raw['id']!r}")
    if messages:
        for item in messages:
            problem("invalid_workflow", item, root_path)
        return None
    definition.setdefault("arguments", [])
    for step in definition["steps"]:
        step.setdefault("depends_on", [])
    return definition


def workflow_semantic_issues(definition: dict[str, Any]) -> list[str]:
    """Checks JSON Schema cannot express: references, cycles, argument rules."""
    messages: list[str] = []
    step_ids: list[str] = [step["id"] for step in definition["steps"]]
    for step_id in sorted({s for s in step_ids if step_ids.count(s) > 1}):
        messages.append(f"duplicate step id {step_id!r}")
    known = set(step_ids)
    graph: dict[str, list[str]] = {}
    for step in definition["steps"]:
        deps = step.get("depends_on", [])
        graph[step["id"]] = list(deps)
        for dep in deps:
            if dep == step["id"]:
                messages.append(f"step {step['id']!r} depends on itself")
            elif dep not in known:
                messages.append(f"step {step['id']!r} depends on unknown step {dep!r}")
    cycle = _find_cycle(graph)
    if cycle:
        messages.append("step dependency cycle: " + " -> ".join(cycle))
    seen_args: set[str] = set()
    for argument in definition.get("arguments", []):
        name = argument["name"]
        if name in seen_args:
            messages.append(f"duplicate argument {name!r}")
        seen_args.add(name)
        bad = check_embedded_schema(argument["schema"])
        if bad:
            messages.append(f"argument {name!r} schema is not a usable JSON Schema: {bad}")
            continue
        mode = argument["acquisition"]
        if mode not in ACQUISITION_MODES:  # pragma: no cover - guarded by the schema
            messages.append(f"argument {name!r} has unknown acquisition mode {mode!r}")
        if "step" in argument and argument["step"] not in known:
            messages.append(f"argument {name!r} names unknown step {argument['step']!r}")
        if mode == "ask_at_step" and "step" not in argument:
            messages.append(f"argument {name!r} uses ask_at_step and must name its step")
        if mode in ("default", "confirm_default") and "default" not in argument:
            messages.append(f"argument {name!r} uses {mode} and must define a default")
        if "default" in argument:
            default_problems = validate_instance(argument["default"], argument["schema"], limit=3)
            if default_problems:
                messages.append(f"argument {name!r} default violates its schema: {default_problems[0]}")
    return messages


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    """Return one cycle as a path ``[a, b, ..., a]`` or ``None`` (iterative DFS)."""
    state: dict[str, int] = {}
    for start in sorted(graph):
        if state.get(start):
            continue
        stack: list[tuple[str, Iterable[str]]] = [(start, iter(sorted(graph.get(start, ()))))]
        path = [start]
        state[start] = 1
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if child not in graph:
                    continue
                if state.get(child) == 1:
                    return path[path.index(child):] + [child]
                if not state.get(child):
                    state[child] = 1
                    path.append(child)
                    stack.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
            if not advanced:
                state[node] = 2
                stack.pop()
                path.pop()
    return None


def _prune_unresolvable(catalog: Catalog, valid: dict[str, Entry], declared: dict[str, str]) -> None:
    changed = True
    while changed:
        changed = False
        for ref in sorted(valid):
            entry = valid[ref]
            for target in entry.requires:
                reason = None
                if target not in valid:
                    target_package = target.split("/", 1)[0]
                    if target_package not in catalog.packages:
                        reason = f"package {target_package!r} is not in the catalog"
                    elif target not in declared:
                        reason = "no such entry"
                    else:
                        reason = "that entry is invalid"
                else:
                    wanted = valid[target]
                    reason = _kind_problem(entry, ref, wanted)
                if reason:
                    catalog.issues.append(Issue("broken_reference", f"{ref} requires {target}: {reason}",
                                                package_id=entry.package_id, entry_ref=ref, path=entry.root_path))
                    del valid[ref]
                    changed = True
                    break


def _kind_problem(entry: Entry, ref: str, target: Entry) -> str | None:
    if entry.kind != "workflow":
        return None
    for step in (entry.definition or {}).get("steps", ()):
        if step.get("verifier") == target.ref and target.kind != "tool":
            return f"verifier must be a tool entry, found {target.kind}"
        if step.get("instruction_ref") == target.ref and target.kind not in TEXT_REF_KINDS:
            return f"instruction_ref must be one of {list(TEXT_REF_KINDS)}, found {target.kind}"
    return None


def _prune_cycles(catalog: Catalog, valid: dict[str, Entry]) -> None:
    graph = {ref: [t for t in entry.requires if t in valid] for ref, entry in valid.items()}
    for component in _strongly_connected(graph):
        if len(component) == 1 and component[0] not in graph[component[0]]:
            continue
        members = sorted(component)
        first = valid[members[0]]
        catalog.issues.append(Issue("dependency_cycle", "dependency cycle among: " + ", ".join(members),
                                    package_id=first.package_id, entry_ref=members[0], path=first.root_path))
        for member in members:
            valid.pop(member, None)


def _strongly_connected(graph: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_index = work.pop()
            if child_index == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            children = graph.get(node, [])
            recurse = False
            for position in range(child_index, len(children)):
                child = children[position]
                if child not in index:
                    work.append((node, position + 1))
                    work.append((child, 0))
                    recurse = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if recurse:
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(component)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return result
