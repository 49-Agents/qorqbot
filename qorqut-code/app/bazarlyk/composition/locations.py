"""Registered rows resolved to target locations through the one ``InstancePaths``.

A business is located by its registered ``slug``, which is its folder; a global or business agent by its registered
agent identity. The stored ``workspace``/``hq_path`` columns, display names, tags and role names never select a
location, and no probing of an older root happens here. Every function only derives and validates a path (symlinks
of existing parts are refused by ``InstancePaths``); nothing is created.

Document files keep their path relative to the document root of their scope: a business's ``hq`` (business notes,
research, assets) or, for the files of a registered agent, the scope root that holds ``agents/{agent-id}``; a global
document is relative to ``operations``. The path's first segment alone decides between the two business roots, so a
document can never be resolved outside the scope the plan was issued for."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from qorqut_operations.contracts import DomainError

from bazarlyk.instance_paths import GRAPH_DIRECTORY, InstancePathError, InstancePaths

AGENTS_SEGMENT = "agents"


def _selected(paths, call, *arguments, **options):
    if not isinstance(paths, InstancePaths):
        raise DomainError("invalid_configuration", "Locations require the explicit instance paths")
    try:
        return call(*arguments, **options)
    except InstancePathError as exc:
        raise DomainError("invalid_configuration", f"Registered location is unavailable: {exc}") from exc


def business_folder(business) -> str:
    folder = business["slug"] if hasattr(business, "keys") else None
    if not isinstance(folder, str) or not folder:
        raise DomainError("invalid_configuration", "A registered business folder is required")
    return folder


def business_root(paths, business) -> Path:
    return _selected(paths, lambda: paths.business_root(business_folder(business)))


def business_hq(paths, business) -> Path:
    return _selected(paths, lambda: paths.business_hq(business_folder(business)))


def business_code(paths, business) -> Path:
    return _selected(paths, lambda: paths.business_code(business_folder(business)))


def agent_workspace(paths, agent_id, business=None) -> Path:
    """The registered agent's workspace; ``business`` is its home business row, or ``None`` for a global agent."""
    folder = None if business is None else business_folder(business)
    return _selected(paths, lambda: paths.agent_workspace(agent_id, business_folder=folder))


def scope_root(paths, business=None) -> Path:
    """The directory holding ``agents/``: the business root, or ``operations`` for Global material."""
    return paths.operations if business is None else business_root(paths, business)


def reserve_graph_root(business, relative_path) -> None:
    """A Global authored path never names the published Context's engine root (``operations/context-graph``).

    Graph files change only through the Context queue and edit authority, so a managed document can neither be registered
    nor written there. Business documents resolve below their own HQ and are unaffected."""
    if business is None and PurePosixPath(relative_path).parts[:1] == (GRAPH_DIRECTORY,):
        raise DomainError("forbidden", "That location belongs to the published Context and cannot hold managed documents")


def document_root(paths, business, relative_path) -> Path:
    """The root a registered document path is relative to, chosen only by the path's first segment."""
    reserve_graph_root(business, relative_path)
    parts = PurePosixPath(relative_path).parts
    if parts and parts[0] == AGENTS_SEGMENT:
        return scope_root(paths, business)
    return paths.operations if business is None else business_hq(paths, business)


def relative_to_operations(paths, path) -> str:
    """An absolute target location as the operations-relative POSIX path the owner's file viewer addresses."""
    try:
        return Path(path).relative_to(paths.operations).as_posix()
    except ValueError as exc:
        raise DomainError("forbidden", "The registered location is outside the operations root") from exc


def document_location(paths, business, relative_path) -> str:
    """The operations-relative location of a registered document path in its scope."""
    return relative_to_operations(paths, document_root(paths, business, relative_path) / relative_path)


def business_document_prefixes(paths, business):
    """``(hq prefix, scope prefix)``: the operations-relative roots a business's document paths can resolve under."""
    return (relative_to_operations(paths, business_hq(paths, business)),
            relative_to_operations(paths, business_root(paths, business)))


def document_path_at(paths, business, location):
    """The registered document path of an operations-relative location in a business, or ``None`` outside it.

    Inverse of ``document_location``: ``agents/...`` below the business root, anything else below its ``hq``."""
    hq, scope = business_document_prefixes(paths, business)
    if location.startswith(scope + "/" + AGENTS_SEGMENT + "/"):
        return location[len(scope) + 1:]
    if location.startswith(hq + "/"):
        rest = location[len(hq) + 1:]
        return None if rest.split("/", 1)[0] == AGENTS_SEGMENT else rest
    return None


def document_path_at_global(paths, location):
    """A Global document's path: any operations-relative location outside the businesses, runtime and Context graph trees."""
    first = location.split("/", 1)[0]
    return None if first in {"businesses", ".runtime", GRAPH_DIRECTORY} else location


def business_runtime(paths, business_id) -> Path:
    """Per-business private runtime state of this instance (never authored working material)."""
    return paths.runtime / "businesses" / business_id


def seed_marker(paths, business_id) -> Path:
    """The private marker that lets an interrupted new-business materialization resume, outside every authored root."""
    return business_runtime(paths, business_id) / "bootstrap-seed.json"


def agent_workspace_of(paths, agent_id, home_business_id, slug) -> Path:
    """The workspace of a registered agent from its row's home business id and that business's slug."""
    return agent_workspace(paths, agent_id, None if home_business_id is None else {"slug": slug})
