#!/usr/bin/env python3
"""Project a verified source export into the native runtime subset, then audit that subset and its wheels.

    runtime_source.py project --source EXPORT --output NEW_DIRECTORY --report NEW_BUILD_REPORT.json
    runtime_source.py audit   --source RUNTIME_SOURCE [--wheels WHEELS_DIRECTORY] [--report NEW_AUDIT_REPORT.json]
    runtime_source.py audit   --prepared PREPARED_DIRECTORY [--report NEW_AUDIT_REPORT.json]

Builder-only. Nothing here runs in a delivered installation, creates an instance, reads a credential, contacts a provider
or installs anything. The input export (``tools/export-source.py``) is never edited: its commits and trees identify the
origin, the recomputed snapshot digests in the new ``SOURCE-MANIFEST.json`` identify the runtime subset. The full
exclusion/edit/provenance report goes only to the separately requested ``--report`` file, never into the delivered source.

``project`` exits 0 when a new runtime source was written, 2 for any refusal. ``audit`` exits 0 only with no finding, 1 when
the payload still carries findings (the JSON says which are known extraction blockers and which are policy leaks), 2 for an
unreadable or tampered input. A clean audit is a statement about this content policy only, never a qualification.
"""
from __future__ import annotations

import argparse
import ast
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tomllib
import zipfile
from collections import Counter

POLICY = "qorqbot-native-runtime-v1"

# Default native-core components. Anything else must be an explicitly excluded family or it needs a reviewed decision.
CORE_COMPONENTS = ("qorqut-code", "qorqut-operations", "qorqut-context", "biz-dashboard", "qorqut-agentic",
                   "document-history", "email-management")
# Components that never ship in the default payload. Matched by name token so a renamed checkout of one is still recognised.
EXCLUDED_FAMILIES = (
    ("fastmail", re.compile(r"(?:^|-)fastmail(?:-|$)")),
    ("telegram", re.compile(r"(?:^|-)telegram(?:-|$)")),
    ("whatsapp", re.compile(r"(?:^|-)whatsapp(?:-|$)")),
    ("email-engine", re.compile(r"(?:^|-)email-engine(?:-|$)")),
    ("paper", re.compile(r"(?:^|-)paper(?:-|$)")),
    ("content", re.compile(r"(?:^|-)content(?:-(?:library|thinking))?(?:-|$)|^content-library$")),
    ("cloudflare", re.compile(r"cloudflare")),
)
# Import roots of the excluded components: a retained module must not import them unguarded.
EXCLUDED_IMPORT_ROOTS = frozenset({"qorqut_fastmail", "engine_email", "qorqut_paper", "email_outreach", "qorqut_content",
                                   "content_library", "qorqut_telegram", "qorqut_whatsapp"})

TEST_DIRS = {"tests", "test", "__tests__", "__snapshots__", "test-results", "coverage"}
DEV_DIRS = {".git", ".github", ".gitlab", ".circleci", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", "__pycache__"}
DEV_FILES = {".pre-commit-config.yaml", "pytest.ini", "tox.ini", ".coverage", "Makefile", "conftest.py"}
GIT_CONTROL_FILES = {".gitignore", ".gitattributes", ".gitmodules"}
# Runtime data trees whose own file names (a plugin template's .gitignore, a .gitkeep) are content, not repository control.
RUNTIME_DATA = {"qorqut-code": ("app/templates/", "app/plugins/"), "qorqut-agentic": ("src/qorqut_agentic/resources/",)}
WHEEL_RUNTIME_DATA = ("qorqut_agentic/resources/",)
PRIVATE_ROOTS = {"execution", "inventory", "worktrees"}
SOURCE_ONLY_REPORTS = {"REPORT.md", "RESULT.md", "IMPLEMENTATION-PLAN.md"}
RELEASE_HELPERS = {"__init__.py", "lifetime.py", "reference_targets.py", "continuity.py"}
RELEASE_PACKAGE = "app/bazarlyk/release_worker/"
RELEASE_ENTRY_POINTS = ("release-worker", "qorqut-release-worker")
RELEASE_TARGET = "bazarlyk.release_worker.cli:main"
HOST_EXCLUDES = (
    "tools", "docs/release", "docs/distribution", "docs/v1.2", "docs/INSTALLED-TOOLING.md",
    "app/scripts", "app/tools/release_worker", "app/tools/check_installed_host.py", "app/tools/installed_host_probe.py",
    "app/tools/rehearse_agentic_install.py", "app/tools/prepare_agentic_helper_manifest.py",
    "app/tools/browser/build_runtime.py", "app/tools/business_sandbox/native_smoke.py",
)
HOST_OPTIONAL_EXTENSIONS = ("extensions/fastmail", "extensions/telegram", "extensions/whatsapp")
RUNTIME_INIT = b'"""Shared runtime/session helpers; release execution is not included in the download."""\n'

# Initialization, migration, recovery, schema/template, helper, instruction and notice files the runtime cannot do without.
# Paths are relative to the export root. A missing file is reported by the audit, never created.
REQUIRED_RUNTIME_ASSETS = {
    "initialization": ("qorqut-code/app/bazarlyk/bootstrap/initialize.py",),
    "migrations": ("qorqut-code/app/bazarlyk/migration/crm_upgrade.py", "qorqut-code/app/bazarlyk/migration/crm_baseline_fingerprint.json",
                   "qorqut-code/app/bazarlyk/agentic/upgrade.py", "qorqut-code/app/bazarlyk/reminders/upgrade.py"),
    "recovery": ("qorqut-code/app/bazarlyk/bootstrap/recovery.py", "qorqut-code/app/bazarlyk/storage/recovery.py",
                 "qorqut-code/app/bazarlyk/storage/full_recovery.py",
                 "qorqut-code/qorqut-context/qorqut_context/recovery.py"),
    "templates-and-schemas": ("qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/source-instructions/host/app/templates/plugins/basic/plugin.yaml", "qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/source-instructions/host/app/plugins/agent-terminal-control/plugin.yaml",
                              "qorqut-code/app/package-sources.json", "qorqut-code/app/service-constraints.txt"),
    "native-helpers": ("qorqut-code/app/bazarlyk/bootstrap/browser_init.cjs", "qorqut-code/app/tools/browser/desktop_session.py",
                       "qorqut-code/app/tools/browser/run-agent-browser", "qorqut-code/app/tools/browser/package.json",
                       "qorqut-code/biz-dashboard/index.html", "qorqut-code/biz-dashboard/pty_broker.py"),
    "core-instructions": ("qorqut-agentic/src/qorqut_agentic/resources/defaults/roles/imported-roles",
                          "qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/source-instructions/email/plugin.yaml", "qorqut-code/app/bazarlyk/release_worker/lifetime.py"),
    "google-calendar": ("qorqut-code/extensions/google-calendar/pyproject.toml",),
}
NOTICE_NAMES = re.compile(r"^(?:LICENSE|LICENCE|NOTICE|COPYING|THIRD[-_ ]PARTY)(?:[-_.].*)?$", re.I)

# Optional-only payload groups the default package must not carry. Each is matched by whole name tokens, so "telegram" in a
# migration, a history enum value or a prose document is not a payload; only executable code, packaged resources, guidance
# bundles and dashboard assets whose own path names the provider are.
PAYLOAD_GROUPS = {
    "telegram": (("telegram",),), "whatsapp": (("whatsapp",),), "cloudflare": (("cloudflare",),), "fastmail": (("fastmail",),),
    "paper": (("paper",),), "email-engine": (("engine", "email"), ("email", "engine")),
    "content": (("qorqut", "content"), ("content", "library")),
}
CODE_SUFFIXES = {".py", ".go", ".js", ".mjs", ".cjs", ".css", ".html", ".svg"}
MIGRATION_PARTS = re.compile(r"^(?:migrations?|migrate|migration_.*)$")
DASHBOARD_ASSET_DIRS = {"js", "css", "icons"}

FORBIDDEN_KINDS = {"source-control-metadata": "source-provenance", "private-artifact": "source-provenance",
                   "source-only-report": "source-provenance", "provider-guidance": "optional-payload"}


class AuditError(ValueError):
    pass


def load_snapshot(path=None):
    """The host's pure format-1 integrity helper, executed from the tooling checkout without writing bytecode."""
    path = Path(path) if path else Path(__file__).resolve().parents[2] / "app/bazarlyk/source_snapshot.py"
    module = type(sys)("qorqut_source_snapshot")
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    return digest(Path(path).read_bytes())


def pretty(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


# --- component selection ---------------------------------------------------------------------------------------------
def excluded_family(name):
    return next((family for family, pattern in EXCLUDED_FAMILIES if pattern.search(name)), None)


def select_components(components, allow=CORE_COMPONENTS, require_all=True):
    """Split a manifest into kept and removed components; an unreviewed or missing component is a refusal."""
    keep, drop, unknown = [], [], []
    for component in components:
        if component["name"] in allow:
            keep.append(component)
        elif (family := excluded_family(component["name"])):
            drop.append((component, family))
        else:
            unknown.append(component)
    if unknown:
        raise ValueError("Review required: component " + ", ".join(
            f"{c['name']!r} ({c['destination']}, {c['commit'][:12]})" for c in unknown) + " is neither in the native-core allowlist "
            "nor in the explicitly excluded optional-component families. It is not shipped or silently "
            "dropped: a reviewed payload decision must add it to one list first.")
    names = {c["name"] for c in components}
    if require_all and (missing := [n for n in allow if n not in names]):
        raise ValueError("The export lacks native-core component(s): " + ", ".join(missing))
    kept = {c["destination"] for c in keep}
    for component, _ in drop:
        for other in keep:
            if other["destination"].startswith(component["destination"] + "/"):
                raise ValueError(f"Kept component {other['name']!r} is nested inside excluded component {component['name']!r}; "
                                 "review the layout before projecting")
    return keep, drop


def owner(destinations, relative):
    return next((c for c in destinations if relative.startswith(c["destination"] + "/")), None)


# --- exclusion policy ------------------------------------------------------------------------------------------------
def exclusion(component, local):
    """First matching reason to leave a component-relative path out of the runtime, or None to keep it.

    Rules are explicit paths and directory names, never every ``test_*`` or ``deploy*`` file name: a feature module called
    ``test_sends.py``, a browser ``deployment.py`` and the database migrations are runtime code and stay.
    """
    parts = PurePosixPath(local).parts
    name = parts[-1]
    if set(parts) & TEST_DIRS:
        return "test-suite"
    if parts[0] in PRIVATE_ROOTS:
        return "private-artifact"
    if len(parts) == 1 and name in SOURCE_ONLY_REPORTS:
        return "source-only-report"
    if name in GIT_CONTROL_FILES and not local.startswith(RUNTIME_DATA.get(component, ())):
        return "source-control-metadata"
    if set(parts) & DEV_DIRS or name in DEV_FILES or name.endswith((".pyc", ".pyo")):
        return "development-metadata"
    if re.search(r"\.(?:test|spec)\.(?:[cm]?js|tsx?|jsx)$", name) or name.endswith("_test.go"):
        return "test-case"
    if parts[0] in {"deploy", "scripts", "evaluations", "examples"}:
        return "development-deployment-tools"
    if component == "qorqut-code":
        if any(local == p or local.startswith(p + "/") for p in HOST_OPTIONAL_EXTENSIONS):
            return "optional-extension-source"
        if any(local == p or local.startswith(p + "/") for p in HOST_EXCLUDES):
            return "build-deployment-tools"
        if local.startswith(RELEASE_PACKAGE) and local[len(RELEASE_PACKAGE):] not in RELEASE_HELPERS:
            return "release-executor"
    if component in {"biz-dashboard", "qorqut-operations"} and parts[0] == "tools":
        return "build-tools"
    return None


# --- the two recorded metadata edits ---------------------------------------------------------------------------------
def runtime_pyproject(raw):
    """Omit the developer release entry points from the host wheel's metadata, changing nothing else."""
    text = raw.decode()
    before = tomllib.loads(text)
    scripts = before.get("project", {}).get("scripts", {})
    for name in RELEASE_ENTRY_POINTS:
        if scripts.get(name) != RELEASE_TARGET:
            raise ValueError("Unexpected release entry point; review the runtime policy: " + name)
        text, count = re.subn(r"^" + re.escape(name) + r'\s*=\s*"' + re.escape(RELEASE_TARGET) + r'"\s*\n', "", text, flags=re.M)
        if count != 1:
            raise ValueError("Cannot remove release entry point: " + name)
    text = re.sub(r'^"bazarlyk\.release_worker"\s*=\s*\["installation\.schema\.json"\]\s*\n', "", text, flags=re.M)
    after = tomllib.loads(text)
    expected = json.loads(json.dumps(before))
    for name in RELEASE_ENTRY_POINTS:
        del expected["project"]["scripts"][name]
    expected.get("tool", {}).get("setuptools", {}).get("package-data", {}).pop("bazarlyk.release_worker", None)
    if after != expected:
        raise ValueError("Runtime metadata edit changed unrelated fields")
    return text.encode()


def runtime_release_init(raw):
    if b"from .worker import" not in raw:
        raise ValueError("Unexpected release_worker package initializer; review the runtime policy")
    return RUNTIME_INIT


EDITS = {("qorqut-code", "app/pyproject.toml"): ("release-entry-points", runtime_pyproject),
         ("qorqut-code", "app/bazarlyk/release_worker/__init__.py"): ("release-worker-initializer", runtime_release_init)}


# --- projection ------------------------------------------------------------------------------------------------------
def _real_directories(path, what):
    """Refuse a path that is not absolute or has a symbolic link in any existing part."""
    path = Path(os.path.abspath(path))
    probe = Path(path.anchor)
    for part in path.parts[1:]:
        probe = probe / part
        try:
            info = os.lstat(probe)
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"{what} must not pass through a symbolic link: {probe}")
    return path


def _overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


def check_locations(source, output, report=None):
    source = _real_directories(source, "Source")
    output = _real_directories(output, "Runtime output")
    if os.path.lexists(output):
        raise ValueError("Runtime output must be new")
    if _overlaps(source, output):
        raise ValueError("Runtime output must not overlap the source")
    if not output.parent.is_dir():
        raise ValueError("The parent of the runtime output must be an existing directory")
    if report is not None:
        report = _real_directories(report, "Build report")
        if os.path.lexists(report):
            raise ValueError("Build report must be a new file")
        if _overlaps(report, source) or _overlaps(report, output):
            raise ValueError("The build report must not be written inside the source or the delivered runtime")
        if not report.parent.is_dir():
            raise ValueError("The parent of the build report must be an existing directory")
    return source, output, report


def _index(snapshot, root, components):
    return {c["destination"] + "/" + r["path"]: r for c in components for r in snapshot.component_records(root, c, components)}


def _plan(source, destinations, kept_names):
    """Every file and symlink of the export with its owner and the reason it is left out (None: retained)."""
    plan = []
    for directory, names, files in os.walk(source):
        names.sort()
        for entry in sorted(files + [n for n in names if (Path(directory) / n).is_symlink()]):
            path = Path(directory) / entry
            relative = path.relative_to(source).as_posix()
            if relative == "SOURCE-MANIFEST.json":
                continue
            component = owner(destinations, relative)
            if component is None:
                raise ValueError("Unowned source file: " + relative)
            local = relative[len(component["destination"]) + 1:]
            reason = "component-excluded" if component["name"] not in kept_names else exclusion(component["name"], local)
            plan.append({"path": relative, "component": component["name"], "local": local, "reason": reason,
                         "bytes": path.lstat().st_size})
    return plan


def _check_links(root):
    real = Path(root).resolve(strict=True)
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError):
                target = None
            if target is None or not target.is_relative_to(real):
                raise ValueError("Runtime selection leaves an unavailable symlink target: " + path.relative_to(root).as_posix())


def project(source, output, snapshot=None, *, allow=CORE_COMPONENTS, require_all=True, report_path=None):
    """Write the runtime subset of ``source`` to the new directory ``output`` and return its provenance record.

    Everything is checked before and after, and the new tree appears at ``output`` only when complete:
    the input digests, the component policy, the exact removed/retained path accounting against a file-level index of the
    input, retained symlink targets, the recomputed nested digests, and an unchanged input afterwards.
    """
    snapshot = snapshot or load_snapshot()
    source, output, _ = check_locations(source, output, report_path)
    original, original_sha = snapshot.validate_export(source)
    components = original["components"]
    keep, drop = select_components(components, allow, require_all)
    kept_names = {c["name"] for c in keep}
    destinations = sorted(components, key=lambda c: len(c["destination"]), reverse=True)
    plan = _plan(source, destinations, kept_names)
    before = _index(snapshot, source, components)
    if {p["path"] for p in plan} != set(before):
        raise ValueError("Export file list differs from its validated snapshot; re-export")
    staging = output.parent / f".{output.name}.runtime-staging-{os.getpid()}"
    if os.path.lexists(staging):
        raise ValueError("A stale runtime staging directory exists: " + staging.name)
    removed, changed = [], []
    try:
        staging.mkdir()
        for component in keep:
            (staging / component["destination"]).mkdir(parents=True, exist_ok=True)
        for item in plan:
            if item["reason"]:
                removed.append({"path": item["path"], "component": item["component"], "reason": item["reason"], "bytes": item["bytes"]})
                continue
            origin, target = source / item["path"], staging / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if origin.is_symlink():
                target.symlink_to(os.readlink(origin))
                continue
            edit = EDITS.get((item["component"], item["local"]))
            if edit is None:
                shutil.copy2(origin, target)
                continue
            replacement = edit[1](origin.read_bytes())
            with open(target, "xb") as handle:
                handle.write(replacement)
            shutil.copystat(origin, target)
            changed.append({"path": item["path"], "edit": edit[0], "source_sha256": file_digest(origin), "runtime_sha256": file_digest(target)})
        _check_links(staging)
        manifest = json.loads(json.dumps(original))
        manifest["components"] = [dict(c) for c in manifest["components"] if c["name"] in kept_names]
        for component in manifest["components"]:
            component["snapshot_sha256"] = snapshot.component_digest(staging, component, manifest["components"])
        (staging / "SOURCE-MANIFEST.json").write_bytes(snapshot.canonical(manifest) + b"\n")
        _, runtime_sha = snapshot.validate_export(staging)
        _account(snapshot, staging, manifest["components"], before, removed, changed)
        if snapshot.validate_export(source)[1] != original_sha or _index(snapshot, source, components) != before:
            raise ValueError("Source changed during runtime projection")
        os.rename(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if snapshot.validate_export(output)[1] != runtime_sha:
        raise ValueError("Runtime source changed after projection")
    return _record(snapshot, original, original_sha, manifest, runtime_sha, drop, removed, changed, plan)


def _account(snapshot, staging, kept, before, removed, changed):
    """Prove retained bytes equal the input except the recorded edits, and that nothing else disappeared."""
    after = _index(snapshot, staging, kept)
    edits = {c["path"]: c for c in changed}
    for path, found in after.items():
        if path in edits:
            if found.get("sha256") != edits[path]["runtime_sha256"] or found["executable"] != before[path]["executable"]:
                raise ValueError("Recorded edit does not match the projected file: " + path)
        elif found != before.get(path):
            raise ValueError("Retained file differs from the input: " + path)
    if set(before) - set(after) != {r["path"] for r in removed}:
        raise ValueError("Removed paths do not match the recorded exclusions")


def _record(snapshot, original, original_sha, manifest, runtime_sha, drop, removed, changed, plan):
    retained = [p for p in plan if not p["reason"]]
    by_reason, by_component = {}, {}
    for item in removed:
        for table, key in ((by_reason, item["reason"]), (by_component, item["component"])):
            row = table.setdefault(key, {"files": 0, "bytes": 0})
            row["files"] += 1
            row["bytes"] += item["bytes"]
    here = Path(__file__).resolve()
    return {"format": 1, "policy": POLICY, "origin_manifest_sha256": original_sha, "origin_manifest": original,
            "manifest_sha256": runtime_sha, "manifest": manifest, "components": manifest["components"],
            "components_removed": [{"name": c["name"], "destination": c["destination"], "commit": c["commit"], "tree": c["tree"],
                                    "family": family} for c, family in drop],
            "retained_files": len(retained), "retained_bytes": sum(p["bytes"] for p in retained),
            "excluded": removed, "excluded_files": len(removed), "excluded_bytes": sum(p["bytes"] for p in removed),
            "excluded_by_reason": by_reason, "excluded_by_component": by_component, "modified": changed,
            "tooling": {"runtime_source_sha256": file_digest(here)},
            "note": "Commit/tree identify the original export. Snapshot hashes identify the runtime subset, not the full Git tree."}


def write_private(path, data):
    """Create a new owner-only file; refuses an existing path or a link."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return digest(data)


# --- audit ------------------------------------------------------------------------------------------------------------
def _tokens(text):
    return [t for t in re.split(r"[^a-z]+", text.lower()) if t]


def _has_sequence(tokens, sequence):
    n = len(sequence)
    return any(tuple(tokens[i:i + n]) == sequence for i in range(len(tokens) - n + 1))


def _group_of(text):
    tokens = _tokens(text)
    return next((g for g, sequences in PAYLOAD_GROUPS.items() if any(_has_sequence(tokens, s) for s in sequences)), None)


def payload_group(relative, packaged=False):
    """The optional-payload group a retained path belongs to, or None.

    Only executable code, packaged resources, guidance bundles and dashboard assets whose own path names the provider
    count. A provider word inside a file, a migration, a history enum value or a prose document is not a payload.
    ``packaged`` is true for a path inside a Python package (or a wheel).
    """
    parts = PurePosixPath(relative).parts
    # Agentic owns optional-connector prose too. These data-only sources cannot
    # load a provider or grant its tools; the selected native binding gates delivery.
    if ("qorqut_agentic/resources/defaults/operations/source-instructions/" in relative
            and PurePosixPath(relative).suffix in {".md", ".json", ".yaml"}):
        return None
    if any(p in {"docs", "doc"} or MIGRATION_PARTS.match(p) for p in parts):
        return None
    for directory in parts[:-1]:
        if (group := _group_of(directory)):
            return group
    name = PurePosixPath(parts[-1])
    group = _group_of(name.stem if name.suffix else name.name)
    if group is None:
        return None
    asset = "biz-dashboard" in parts and bool(set(parts) & DASHBOARD_ASSET_DIRS)
    if name.suffix in CODE_SUFFIXES or name.name in {"go.mod", "go.sum"} or packaged or asset or "providers" in parts[:-1]:
        return group
    return None


class _Imports(ast.NodeVisitor):
    """Import statements of one module, noting those guarded by ``except ImportError`` and skipping type-checking blocks."""

    def __init__(self):
        self.items, self._guarded, self._typing = [], 0, 0

    def visit_Import(self, node):
        if not self._typing:
            self.items += [(0, a.name, (), node.lineno, bool(self._guarded)) for a in node.names]

    def visit_ImportFrom(self, node):
        if not self._typing:
            self.items.append((node.level, node.module or "", tuple(a.name for a in node.names), node.lineno, bool(self._guarded)))

    def visit_Try(self, node):
        guard = any(self._catches(h) for h in node.handlers)
        self._guarded += guard
        for child in node.body:
            self.visit(child)
        self._guarded -= guard
        for child in [*node.handlers, *node.orelse, *node.finalbody]:
            self.visit(child)

    visit_TryStar = visit_Try

    def visit_If(self, node):
        test = node.test
        typing = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
        self._typing += typing
        for child in node.body:
            self.visit(child)
        self._typing -= typing
        for child in node.orelse:
            self.visit(child)

    @staticmethod
    def _catches(handler):
        if handler.type is None:
            return True
        names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        return any(isinstance(n, ast.Name) and n.id in {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"} for n in names)


def _bound_names(tree):
    """Names a module binds at its top level; ``*`` when it re-exports or resolves names dynamically."""
    names = set()

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                if node.name == "__getattr__":
                    names.add("*")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add("*" if alias.name == "*" else alias.asname or alias.name.split(".")[0])
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field, []))
                for handler in getattr(node, "handlers", []):
                    walk(handler.body)
    walk(tree.body)
    return names


def _module_index(files):
    """Dotted name of every module that sits directly in a package, and the set of package directories."""
    packages = {PurePosixPath(f).parent for f in files if PurePosixPath(f).name == "__init__.py"}
    modules = {}
    for relative in files:
        path = PurePosixPath(relative)
        if path.suffix != ".py" or path.parent not in packages:
            continue
        chain, directory = [], path.parent
        while directory in packages:
            chain.append(directory.name)
            directory = directory.parent
        chain.reverse()
        modules[".".join(chain if path.name == "__init__.py" else [*chain, path.stem])] = relative
    return modules, packages


def _relative_target(package, level, module):
    base = package.split(".") if package else []
    keep = len(base) - (level - 1)
    if keep < 0:
        return None
    return ".".join([*base[:keep], *([module] if module else [])])


def import_findings(root, files, extra_roots=EXCLUDED_IMPORT_ROOTS):
    """Static first-party import closure of the retained Python files.

    Returns (findings, graph, modules): an unguarded import of a first-party module or name the source does not contain,
    each file's resolved imports, and the module index.
    """
    modules, _ = _module_index(files)
    by_path = {path: dotted for dotted, path in modules.items()}
    roots = {m.split(".")[0] for m in modules} | set(extra_roots)
    trees, imports, findings = {}, {}, []
    for relative in sorted(f for f in files if f.endswith(".py")):
        try:
            tree = ast.parse((Path(root) / relative).read_bytes(), filename=relative)
        except (SyntaxError, ValueError, OSError) as exc:
            findings.append({"kind": "unparseable-python", "path": relative, "detail": str(exc)[:200]})
            continue
        trees[relative], collector = tree, _Imports()
        collector.visit(tree)
        imports[relative] = collector.items
    bound = {}

    def names_of(dotted):
        path = modules[dotted]
        if path not in bound:
            bound[path] = _bound_names(trees[path]) if path in trees else {"*"}
        return bound[path]

    graph = {}
    for relative, items in imports.items():
        me = by_path.get(relative)
        for level, module, names, lineno, guarded in items:
            if level:
                if me is None:
                    continue
                target = _relative_target(me if relative.endswith("/__init__.py") else me.rpartition(".")[0], level, module)
            else:
                target = module
            if not target or target.split(".")[0] not in roots:
                continue
            reached, missing = graph.setdefault(relative, set()), None
            if target not in modules:
                missing = target
            else:
                reached.add(modules[target])
                for name in names:
                    child = f"{target}.{name}"
                    if child in modules:
                        reached.add(modules[child])
                    elif name != "*" and "*" not in names_of(target) and name not in names_of(target):
                        missing = child
            if missing and not guarded:
                findings.append({"kind": "unresolved-import", "path": relative,
                                 "detail": f"line {lineno} imports {missing}, which the runtime source does not contain"})
    return findings, graph, modules


def _coupling(graph, groups):
    """For each optional payload group: its Python files and the other retained files that import them."""
    result = {}
    for group, paths in sorted(groups.items()):
        members = {p for p in paths if p.endswith(".py")}
        result[group] = {"python_files": len(members),
                         "retained_importers": sorted(i for i, targets in graph.items() if i not in members and targets & members)}
    return result


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _projects(root, files, find):
    """{normalized project name: pyproject} and every ``tool.setuptools.package-data`` pattern with its matching files."""
    projects, rows, fileset = {}, [], set(files)
    for relative in sorted(f for f in files if PurePosixPath(f).name == "pyproject.toml"):
        try:
            data = tomllib.loads((Path(root) / relative).read_text())
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
            find("unparseable-metadata", relative, str(exc)[:200])
            continue
        project = data.get("project", {})
        if project.get("name"):
            projects[normalize(project["name"])] = relative
        for table in ("scripts", "gui-scripts"):
            for script, target in project.get(table, {}).items():
                if "release_worker" in str(target) or script in RELEASE_ENTRY_POINTS:
                    find("development-entry-point", relative, f"{script} = {target}")
        setuptools = data.get("tool", {}).get("setuptools", {})
        base = PurePosixPath(relative).parent / ((setuptools.get("packages", {}).get("find", {}).get("where") or ["."])[0])
        for package, patterns in setuptools.get("package-data", {}).items():
            if package == "*":
                continue
            directory = Path(root) / base / package.replace(".", "/")
            for pattern in patterns:
                matches = sorted(p.relative_to(Path(root) / base).as_posix() for p in directory.glob(pattern) if p.is_file() or p.is_symlink())
                rows.append({"pyproject": relative, "project": normalize(project.get("name", "")), "package": package, "pattern": pattern,
                             "matches": [m for m in matches if (base / m).as_posix() in fileset],
                             "sha256": {m: file_digest(Path(root) / base / m) for m in matches if (base / m).as_posix() in fileset}})
    return projects, rows


def audit(source, wheels=None, snapshot=None, required=REQUIRED_RUNTIME_ASSETS, allow=CORE_COMPONENTS, require_notice=True,
          environments=()):
    """Deterministic content audit of a projected source and every first-party wheel built from it.

    Raises ValueError when the source fails its snapshot integrity check. Otherwise returns the report. ``passed`` means no
    finding at all. Findings of kind ``optional-payload`` are known extraction blockers; every other kind is a policy leak or
    a missing runtime requirement. The result is never a package qualification.
    """
    snapshot = snapshot or load_snapshot()
    source = Path(source)
    manifest, manifest_sha = snapshot.validate_export(source)
    components = sorted(manifest["components"], key=lambda c: len(c["destination"]), reverse=True)
    findings = []

    def find(kind, path, detail=""):
        findings.append({"kind": kind, "path": path, "detail": detail})

    present = {c["name"] for c in components}
    for component in manifest["components"]:
        if component["name"] not in allow:
            find("excluded-component", component["name"], f"component at {component['destination']} is not in the native-core allowlist")
    for name in allow:
        if name not in present:
            find("missing-required-asset", name, "native-core component is absent from the source manifest")
    files = []
    for directory, names, entries in os.walk(source):
        names.sort()
        for entry in sorted(entries + [n for n in names if (Path(directory) / n).is_symlink()]):
            relative = (Path(directory) / entry).relative_to(source).as_posix()
            if relative != "SOURCE-MANIFEST.json":
                files.append(relative)
    _, packages = _module_index(files)
    groups, notices = {}, []
    for relative in files:
        component = owner(components, relative)
        if component is None:
            find("unowned-file", relative, "not inside any manifest component")
            continue
        path = PurePosixPath(relative)
        reason = exclusion(component["name"], relative[len(component["destination"]) + 1:])
        group = payload_group(relative, any(parent in packages for parent in path.parents))
        if reason == "provider-guidance":
            group = group or _group_of(relative)
        if reason:
            find(FORBIDDEN_KINDS.get(reason, "forbidden-path"), relative, group or reason)
        elif group:
            find("optional-payload", relative, group)
        if group and (not reason or reason == "provider-guidance"):
            groups.setdefault(group, []).append(relative)
        if NOTICE_NAMES.match(path.name):
            notices.append(relative)
    if require_notice and not notices:
        find("missing-required-asset", "LICENSE", "no license or notice file exists in any component; the license decision is still open")
    for category, paths in required.items():
        for path in paths:
            if not os.path.lexists(source / path):
                find("missing-required-asset", path, category)
    projects, package_data = _projects(source, files, find)
    for row in package_data:
        if not row["matches"]:
            find("missing-required-asset", row["pyproject"], f"package-data {row['package']}: {row['pattern']} matches no file")
    import_found, graph, modules = import_findings(source, files)
    findings += import_found
    for environment in environments:
        for name in RELEASE_ENTRY_POINTS:
            if os.path.lexists(Path(environment) / "bin" / name):
                find("development-entry-point", f"{Path(environment).name}/bin/{name}", "console script")
    report_wheels = [] if wheels is None else _audit_wheels(wheels, projects, package_data, find)
    findings.sort(key=lambda f: (f["kind"], f["path"], f["detail"]))
    counts = {}
    for item in findings:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    leaks = [f for f in findings if f["kind"] != "optional-payload"]
    return {"format": 1, "policy": POLICY, "source_manifest_sha256": manifest_sha,
            "components": [c["name"] for c in manifest["components"]], "files": len(files), "python_modules": len(modules),
            "wheels": report_wheels, "notices": sorted(notices),
            "status": "clean" if not findings else "failed" if leaks else "blocked-by-known-optional-payload",
            "passed": not findings, "counts": dict(sorted(counts.items())),
            "optional_payload_groups": {g: len(p) for g, p in sorted(groups.items())},
            "optional_payload_coupling": _coupling(graph, groups), "findings": findings,
            "note": "A clean result describes this content policy only. It does not qualify a package."}


def _wheel_paths(wheels):
    if isinstance(wheels, (str, os.PathLike)):
        wheels = Path(wheels)
        return sorted(wheels.glob("*.whl")) if wheels.is_dir() else [wheels]
    return sorted(Path(w) for w in wheels)


def _wheel_reason(member):
    parts = PurePosixPath(member).parts
    if not parts or member.startswith("/") or ".." in parts or "\\" in member or any(ord(c) < 32 for c in member):
        return "unsafe-member"
    name = parts[-1]
    if set(parts) & TEST_DIRS:
        return "test-suite"
    if name in GIT_CONTROL_FILES and not member.startswith(WHEEL_RUNTIME_DATA):
        return "development-metadata"
    if set(parts) & DEV_DIRS or name in DEV_FILES or name.endswith((".pyc", ".pyo")):
        return "development-metadata"
    if re.search(r"\.(?:test|spec)\.(?:[cm]?js|tsx?|jsx)$", name) or name.endswith("_test.go"):
        return "test-case"
    if parts[0] in {"tools", "scripts", "deploy", "examples", "evaluations", *PRIVATE_ROOTS}:
        return "development-deployment-tools"
    if member.startswith("bazarlyk/release_worker/") and name not in RELEASE_HELPERS:
        return "release-executor"
    if re.fullmatch(r"[^/]+\.data/scripts/(?:qorqut-)?release-worker", member):
        return "release-executor"
    return None


def _audit_wheels(wheels, projects, package_data, find):
    expected = {}
    for row in package_data:
        expected.setdefault(row["project"], {}).update(row["sha256"])
    report, built = [], set()
    for path in _wheel_paths(wheels):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for member, count in Counter(names).items():
                if count > 1:
                    find("invalid-wheel", f"{path.name}:{member}", "duplicate archive member")
            metadata_files = [n for n in names if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
            metadata = metadata_files[0] if len(metadata_files) == 1 else None
            if metadata is None:
                find("invalid-wheel", path.name, "expected exactly one distribution METADATA file")
            text = archive.read(metadata).decode(errors="replace") if metadata else ""
            project = next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.lower().startswith("name:")), "")
            for line in text.splitlines():
                if line.lower().startswith("requires-dist:"):
                    required = normalize(re.split(r"[\s;<>=!~\[(]", line.split(":", 1)[1].strip(), 1)[0])
                    if excluded_family(required):
                        find("excluded-component", path.name, "requires " + required)
            if excluded_family(normalize(project)):
                find("excluded-component", path.name, f"wheel built for excluded component {project}")
            elif normalize(project) not in projects:
                find("unexpected-wheel", path.name, f"{project or 'no project name'} is not built from any pyproject in the runtime source")
            else:
                if normalize(project) in built:
                    find("invalid-wheel", path.name, "multiple wheels for the same first-party project")
                built.add(normalize(project))
            for member in names:
                if (reason := _wheel_reason(member)):
                    find("forbidden-path", f"{path.name}:{member}", reason)
                elif (group := payload_group(member, True)):
                    find("optional-payload", f"{path.name}:{member}", group)
                if member.endswith(".dist-info/entry_points.txt"):
                    parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
                    parser.optionxform = str
                    try:
                        parser.read_string(archive.read(member).decode())
                    except (configparser.Error, UnicodeDecodeError):
                        find("development-entry-point", f"{path.name}:{member}", "unreadable entry points")
                        continue
                    for section in parser.sections():
                        for script, target in parser.items(section):
                            if "release_worker" in target or script.strip() in RELEASE_ENTRY_POINTS:
                                find("development-entry-point", f"{path.name}:{member}", f"{script.strip()} = {target.strip()}")
            assets = expected.get(normalize(project), {})
            for member in sorted(set(assets) - set(names)):
                find("missing-required-asset", f"{path.name}:{member}", "package data present in the runtime source is absent from the wheel")
            for member in sorted(set(assets) & set(names)):
                if digest(archive.read(member)) != assets[member]:
                    find("changed-runtime-asset", f"{path.name}:{member}", "packaged resource differs from the verified source")
            report.append({"name": path.name, "sha256": file_digest(path), "project": project, "members": len(names)})
    for project in sorted(set(projects) - built):
        find("missing-required-asset", projects[project], "first-party wheel is absent: " + project)
    return report


def audit_prepared(prepared, **kwargs):
    """Audit a ``prepare-local.py`` output: its ``src`` copy, its ``wheels`` and the console scripts of its environments."""
    prepared = Path(prepared)
    return audit(prepared / "src", prepared / "wheels" if (prepared / "wheels").is_dir() else None,
                 environments=sorted(p for p in prepared.glob("*venv") if p.is_dir()), **kwargs)


# --- command line ------------------------------------------------------------------------------------------------------
def summary(record):
    keys = ("policy", "origin_manifest_sha256", "manifest_sha256", "retained_files", "retained_bytes", "excluded_files",
            "excluded_bytes", "excluded_by_reason", "excluded_by_component")
    return {**{k: record[k] for k in keys},
            "components_kept": [c["name"] for c in record["components"]],
            "components_removed": [c["name"] for c in record["components_removed"]],
            "modified": [{k: m[k] for k in ("path", "edit")} for m in record["modified"]]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("project", help="write the runtime subset of an export to a new directory")
    one.add_argument("--source", required=True)
    one.add_argument("--output", required=True)
    one.add_argument("--report", required=True, help="new private file for the full exclusion/edit/provenance report")
    two = commands.add_parser("audit", help="audit a projected source and its wheels")
    group = two.add_mutually_exclusive_group(required=True)
    group.add_argument("--source")
    group.add_argument("--prepared")
    two.add_argument("--wheels")
    two.add_argument("--report")
    args = parser.parse_args(argv)
    try:
        if args.command == "project":
            check_locations(args.source, args.output, args.report)
            record = project(args.source, args.output, report_path=args.report)
            sha = write_private(args.report, pretty(record))
            print(json.dumps({**summary(record), "report_sha256": sha}, indent=2, sort_keys=True))
            return 0
        if args.prepared and args.wheels:
            raise ValueError("--wheels applies to --source only")
        if args.report:
            report = _real_directories(args.report, "Audit report")
            if os.path.lexists(args.report):
                raise ValueError("Audit report must be a new file")
            for tree in (args.prepared or args.source, args.wheels):
                if tree and _overlaps(report, _real_directories(tree, "Audit input")):
                    raise ValueError("Audit report must be outside the audited payload")
        result = audit_prepared(args.prepared) if args.prepared else audit(args.source, args.wheels)
        if args.report:
            write_private(args.report, pretty(result))
        print(json.dumps({k: v for k, v in result.items() if k not in {"findings", "optional_payload_coupling"}}, indent=2, sort_keys=True))
        return 0 if result["passed"] else 1
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        print(f"runtime_source: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
