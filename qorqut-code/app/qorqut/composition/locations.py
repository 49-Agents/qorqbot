"""Registered rows resolved to target locations through the one ``InstancePaths``.

A project is located by its registered ``slug``, which is its folder; a global or project agent by its registered
agent identity. The stored ``workspace``/``hq_path`` columns, display names, tags and role names never select a
location, and no probing of an older root happens here. Every function only derives and validates a path (symlinks
of existing parts are refused by ``InstancePaths``); nothing is created.

Document files keep their path relative to the document root of their scope: a project's ``hq`` (project notes,
research, assets) or, for the files of a registered agent, the scope root that holds ``agents/{agent-id}``; a global
document is relative to ``operations``. The path's first segment alone decides between the two project roots, so a
document can never be resolved outside the scope the plan was issued for."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from qorqut_operations.contracts import DomainError

from qorqut.instance_paths import GRAPH_DIRECTORY, PROJECTS_DIRECTORY, InstancePathError, InstancePaths

AGENTS_SEGMENT = "agents"


def _selected(paths, call, *arguments, **options):
    if not isinstance(paths, InstancePaths):
        raise DomainError("invalid_configuration", "Locations require the explicit instance paths")
    try:
        return call(*arguments, **options)
    except InstancePathError as exc:
        raise DomainError("invalid_configuration", f"Registered location is unavailable: {exc}") from exc


def project_folder(project) -> str:
    folder = project["slug"] if hasattr(project, "keys") else None
    if not isinstance(folder, str) or not folder:
        raise DomainError("invalid_configuration", "A registered project folder is required")
    return folder


def project_root(paths, project) -> Path:
    return _selected(paths, lambda: paths.project_root(project_folder(project)))


def project_hq(paths, project) -> Path:
    return _selected(paths, lambda: paths.project_hq(project_folder(project)))


def project_code(paths, project) -> Path:
    return _selected(paths, lambda: paths.project_code(project_folder(project)))


def agent_workspace(paths, agent_id, project=None) -> Path:
    """The registered agent's workspace; ``project`` is its home project row, or ``None`` for a global agent."""
    folder = None if project is None else project_folder(project)
    return _selected(paths, lambda: paths.agent_workspace(agent_id, project_folder=folder))


def scope_root(paths, project=None) -> Path:
    """The directory holding ``agents/``: the project root, or ``operations`` for Global material."""
    return paths.operations if project is None else project_root(paths, project)


def reserve_graph_root(project, relative_path) -> None:
    """A Global authored path never names the published Context's engine root (``operations/context-graph``).

    Graph files change only through the Context queue and edit authority, so a managed document can neither be registered
    nor written there. Project documents resolve below their own HQ and are unaffected."""
    if project is None and PurePosixPath(relative_path).parts[:1] == (GRAPH_DIRECTORY,):
        raise DomainError("forbidden", "That location belongs to the published Context and cannot hold managed documents")


def document_root(paths, project, relative_path) -> Path:
    """The root a registered document path is relative to, chosen only by the path's first segment."""
    reserve_graph_root(project, relative_path)
    parts = PurePosixPath(relative_path).parts
    if parts and parts[0] == AGENTS_SEGMENT:
        return scope_root(paths, project)
    return paths.operations if project is None else project_hq(paths, project)


def relative_to_operations(paths, path) -> str:
    """An absolute target location as the operations-relative POSIX path the owner's file viewer addresses."""
    try:
        return Path(path).relative_to(paths.operations).as_posix()
    except ValueError as exc:
        raise DomainError("forbidden", "The registered location is outside the operations root") from exc


def document_location(paths, project, relative_path) -> str:
    """The operations-relative location of a registered document path in its scope."""
    return relative_to_operations(paths, document_root(paths, project, relative_path) / relative_path)


def project_document_prefixes(paths, project):
    """``(hq prefix, scope prefix)``: the operations-relative roots a project's document paths can resolve under."""
    return (relative_to_operations(paths, project_hq(paths, project)),
            relative_to_operations(paths, project_root(paths, project)))


def document_path_at(paths, project, location):
    """The registered document path of an operations-relative location in a project, or ``None`` outside it.

    Inverse of ``document_location``: ``agents/...`` below the project root, anything else below its ``hq``."""
    hq, scope = project_document_prefixes(paths, project)
    if location.startswith(scope + "/" + AGENTS_SEGMENT + "/"):
        return location[len(scope) + 1:]
    if location.startswith(hq + "/"):
        rest = location[len(hq) + 1:]
        return None if rest.split("/", 1)[0] == AGENTS_SEGMENT else rest
    return None


def document_path_at_global(paths, location):
    """A Global document's path: any operations-relative location outside the projects, runtime and Context graph trees."""
    first = location.split("/", 1)[0]
    return None if first in {PROJECTS_DIRECTORY, ".runtime", GRAPH_DIRECTORY} else location


def agent_workspace_of(paths, agent_id, home_project_id, slug) -> Path:
    """The workspace of a registered agent from its row's home project id and that project's slug."""
    return agent_workspace(paths, agent_id, None if home_project_id is None else {"slug": slug})
