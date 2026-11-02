"""Explicit, immutable instance paths and component locations.

One value names every location the host derives from the instance root and from the code/role components it was
given. There is no working-directory default, no probing of installed copies or sibling checkouts, and no
implicit configuration discovery. Global workspace selection reads only the explicit instance-owned
``config/global-agent-workspaces.json`` mapping when present. Construction and every derived location
validate existing path parts to refuse symlinks, including the mapping file. Nothing here creates or writes a file. This is path validation, not a
guard against a same-user filesystem race.

Layout under ``instance_root``::

    operations/                           records and working material of this instance
      files/                              global authored working material
      context/                            the one published shared Context
      agentic/                            authored agentic Git tree (explicitly initialized)
      agents/{agent_id}/                  global agent workspace (Ordubashy and Learning Applier may use configured names)
      extensions/{name}/                  owner-editable guidance of an optional instruction package (seeded once)
      projects/{folder}/{hq,code}         project notes/documents and project source code
      projects/{folder}/agents/{id}/      historical per-project agent workspace (retired agents; no new ones are created)
      .runtime/operations.sqlite3         the one operational database
      .runtime/locks/                     application claims, apply locks and the recovery lock
      .runtime/context/                   Context-owned execution and index state
      .runtime/agentic/                   agentic module state, separate from its authored tree
      .runtime/context-questions.sqlite3  host-owned external review questions (outside the Context and its runtime)
      .runtime/history.git/               private operational Git snapshots
    qorqut-roles/                         the separate role-source repositories

Components (``app``, ``operations_engine``, ``context_engine``, ``dashboard``, ``roles``) are
explicitly supplied source or installed-artifact roots; they are never inferred from this instance root. The
``roles`` component is the reusable role source and must stay separate from every code component.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
import json
from uuid import UUID
from pathlib import Path
import re
from types import MappingProxyType

COMPONENTS = ("app", "operations_engine", "context_engine", "dashboard", "roles")
# The engine-owned published Context repository below ``operations``; no authored document path may name it.
GRAPH_DIRECTORY = "context-graph"
# The directory below ``operations`` that holds every registered project (the internal ``business`` records) by slug.
PROJECTS_DIRECTORY = "projects"
# The directory below ``operations`` that holds the operator-editable working material of optional instruction packages.
EXTENSIONS_DIRECTORY = "extensions"

# One path segment for a registered business folder or an agent identity. Registered v1 identifiers (UUIDs) are
# kept at their full length; the character set excludes separators, traversal and hidden names.
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class InstancePathError(ValueError):
    """An explicit path or segment was missing, ambiguous, unsafe or overlapped a protected boundary."""


def _absolute(value, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise InstancePathError(f"{label} must be a path")
    path = Path(value)
    if not path.is_absolute():
        raise InstancePathError(f"{label} must be an absolute path")
    if ".." in path.parts or "\0" in str(path):
        raise InstancePathError(f"{label} must not contain traversal or NUL")
    return Path(os.path.realpath(path))


def _inside(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def _overlaps(first: Path, second: Path) -> bool:
    return _inside(first, second) or _inside(second, first)


def _segment(value, label: str) -> str:
    if not isinstance(value, str) or SEGMENT.fullmatch(value) is None:
        raise InstancePathError(f"{label} must be one safe path segment")
    return value


class InstancePaths:
    """The explicit instance root and component locations of one host."""

    __slots__ = ("_root", "_components", "_operations", "_roles")

    def __init__(self, instance_root: Path, components: Mapping[str, Path]):
        if not isinstance(components, Mapping):
            raise InstancePathError("components must be a mapping of component names to absolute paths")
        root = _absolute(instance_root, "instance_root")
        if root == Path(root.anchor):
            raise InstancePathError("instance_root must not be the filesystem root")
        unknown = sorted(str(name) for name in components if name not in COMPONENTS)
        if unknown:
            raise InstancePathError(f"unknown components: {', '.join(unknown)}")
        resolved = {name: _absolute(components[name], f"component {name}") for name in COMPONENTS if name in components}
        operations, roles = root / "operations", root / "qorqut-roles"
        for label, path in (("operations", operations), ("qorqut-roles", roles)):
            if Path(os.path.realpath(path)) != path:
                raise InstancePathError(f"the {label} root must not be a symlink")
        seen: dict[Path, str] = {}
        for name, path in resolved.items():
            if path in seen:
                raise InstancePathError(f"components {seen[path]} and {name} name the same location")
            seen[path] = name
            if _overlaps(path, operations):
                raise InstancePathError(f"component {name} must be separate from the operations root")
            if _inside(root, path):
                raise InstancePathError(f"instance_root must not be a component location or inside component {name}")
            if name != "roles" and _overlaps(path, roles):
                raise InstancePathError(f"component {name} must be separate from the qorqut-roles root")
        if "roles" in resolved:
            for name, path in resolved.items():
                if name != "roles" and _overlaps(path, resolved["roles"]):
                    raise InstancePathError(f"component {name} must be separate from the roles component")
        object.__setattr__(self, "_root", root)
        object.__setattr__(self, "_components", MappingProxyType(resolved))
        object.__setattr__(self, "_operations", operations)
        object.__setattr__(self, "_roles", roles)

    def __setattr__(self, name, value):
        raise AttributeError("InstancePaths is immutable")

    def __delattr__(self, name):
        raise AttributeError("InstancePaths is immutable")

    def __eq__(self, other):
        return isinstance(other, InstancePaths) and (self._root, dict(self._components)) == (other._root, dict(other._components))

    def __hash__(self):
        return hash((self._root, tuple(self._components.items())))

    def __repr__(self):
        return f"InstancePaths({str(self._root)!r}, {{{', '.join(f'{k!r}: {str(v)!r}' for k, v in self._components.items())}}})"

    @property
    def instance_root(self) -> Path:
        return self._root

    @property
    def components(self) -> Mapping[str, Path]:
        return self._components

    @property
    def operations(self) -> Path:
        return self._checked(self._operations)

    @property
    def runtime(self) -> Path:
        """Runtime metadata of this instance (never authored working material)."""
        return self._checked(self._operations / ".runtime")

    @property
    def locks(self) -> Path:
        """Prepared-application claims, per-receipt file locks and the installation recovery lock."""
        return self._checked(self._operations / ".runtime" / "locks")

    @property
    def database(self) -> Path:
        return self._checked(self._operations / ".runtime" / "operations.sqlite3")

    @property
    def context(self) -> Path:
        """The one published Context repository (engine-owned Git root). Global authored documents such as
        ``context/_inbox/...`` live beside it under ``operations/context`` and are a different tree."""
        return self._checked(self._operations / GRAPH_DIRECTORY)

    @property
    def context_runtime(self) -> Path:
        return self._checked(self._operations / ".runtime" / "context")

    @property
    def agentic_root(self) -> Path:
        """The agentic authoring tree; resolving the path creates nothing."""
        return self._checked(self._operations / "agentic")

    @property
    def agentic_runtime(self) -> Path:
        """Agentic module state outside its authoring tree and Context."""
        return self._checked(self._operations / ".runtime" / "agentic")

    @property
    def overseer_archive(self) -> Path:
        """The explicitly provisioned private Overseer archive; resolution creates nothing."""
        return self._checked(self._operations / ".runtime" / "overseer" / "archive")

    @property
    def context_questions(self) -> Path:
        """The host-owned question store file: outside the published Context, its runtime and any Context capture."""
        return self._checked(self._operations / ".runtime" / "context-questions.sqlite3")

    @property
    def history(self) -> Path:
        return self._checked(self._operations / ".runtime" / "history.git")

    @property
    def files(self) -> Path:
        return self._checked(self._operations / "files")

    @property
    def roles(self) -> Path:
        return self._checked(self._roles)

    def extension_home(self, name: str) -> Path:
        """The owner-editable instance directory of one instruction package (``operations/extensions/<name>``)."""
        return self._checked(self._operations / EXTENSIONS_DIRECTORY / _segment(name, "extension name"))

    def component(self, name: str) -> Path:
        if name not in COMPONENTS:
            raise InstancePathError(f"unknown component: {name!r}")
        try:
            return self._components[name]
        except KeyError:
            raise InstancePathError(f"component {name} is not configured") from None

    def role_source(self, path) -> Path:
        """A configured role source: inside the instance ``qorqut-roles`` root or the explicit ``roles`` component."""
        resolved = _absolute(path, "role source")
        bases = [self._roles] + ([self._components["roles"]] if "roles" in self._components else [])
        if not any(_inside(resolved, base) for base in bases):
            raise InstancePathError("role source is outside the qorqut-roles root and the configured roles component")
        return resolved

    def business_root(self, folder: str) -> Path:
        """The registered project's folder; the internal ``business`` names and identifiers are unchanged."""
        return self._checked(self._operations / PROJECTS_DIRECTORY / _segment(folder, "project folder"))

    def business_hq(self, folder: str) -> Path:
        return self._checked(self.business_root(folder) / "hq")

    def business_code(self, folder: str) -> Path:
        return self._checked(self.business_root(folder) / "code")

    def agent_workspace(self, agent_id: str, *, business_folder: str | None = None) -> Path:
        """Resolve identity; only an explicitly mapped Ordubashy or Learning Applier uses a readable folder name; workers keep UUID folders."""
        agent = _segment(agent_id, "agent id")
        base = self._operations if business_folder is None else self.business_root(business_folder)
        if business_folder is None:
            agent = self.global_agent_workspaces().get(agent, agent)
        return self._checked(base / "agents" / agent)

    def global_agent_workspaces(self) -> dict[str, str]:
        """Explicit ID-to-folder mapping, never a display-name or directory lookup."""
        path = self._checked(self._root / "config" / "global-agent-workspaces.json")
        try:
            with path.open("rb") as stream:
                raw = stream.read(8193)
        except FileNotFoundError:
            return {}
        try:
            value = json.loads(raw)
            if len(raw) > 8192 or not isinstance(value, dict) or set(value) != {"version", "agents"} or value["version"] != 1:
                raise ValueError
            agents = value["agents"]
            allowed = {"ordubashy", "learning-applier"}
            if not isinstance(agents, dict) or len(agents) > 2 or len(set(agents.values())) != len(agents):
                raise ValueError
            for identity, folder in agents.items():
                if str(UUID(identity)) != identity or folder not in allowed:
                    raise ValueError
            return dict(agents)
        except (ValueError, TypeError, AttributeError):
            raise InstancePathError("Invalid fixed global-agent workspace mapping") from None

    def agent_files(self, agent_id: str, *, business_folder: str | None = None) -> Path:
        return self._checked(self.agent_workspace(agent_id, business_folder=business_folder) / "files")

    def agent_transcripts(self, agent_id: str, *, business_folder: str | None = None) -> Path:
        return self._checked(self.agent_workspace(agent_id, business_folder=business_folder) / "transcripts")

    def _checked(self, path: Path) -> Path:
        """Refuse a derived path whose existing parts include a symlink; unwritten paths cannot escape.

        Evaluated on every selection, so a link created after construction is refused too."""
        if Path(os.path.realpath(path)) != path:
            raise InstancePathError(f"path leaves the instance through a symlink: {path}")
        return path
