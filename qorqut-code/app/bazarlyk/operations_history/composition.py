"""Host-owned registration and lifecycle wiring for operational history.

The snapshot engine is ``qorqut_operations.history_exports``. This module gives it the installation's facts: where the
operations root and history repository are, how a registered area maps to a folder, which identifiers are safe path
segments, the catalog of exported rows, the area provider and the clock. The location and area adapters go through
``InstancePaths`` on every call, so its symlink and traversal refusals are re-applied each time, and a refused path
becomes the same ``DomainError`` (or blocked area) as before; nothing resolved here is remembered.
"""
from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.history_exports import (BusinessAgentFiles, BusinessHeadquarters, GlobalAgentFiles, GlobalFiles,
                                               History, HistoryObserver, ResolvedArea)

from bazarlyk.instance_paths import SEGMENT, InstancePathError, InstancePaths

from .catalog import catalog


def registered_areas(uow):
    areas = [GlobalFiles()]
    businesses = {row["id"]: row["slug"] for row in uow.connection.execute("SELECT id,slug FROM businesses")}
    for row in uow.connection.execute("SELECT agent_id,home_business_id FROM agent_profiles ORDER BY agent_id"):
        home = row["home_business_id"]
        areas.append(GlobalAgentFiles(row["agent_id"]) if home is None else
                     BusinessAgentFiles(businesses[home], row["agent_id"]))
    # These are projections or independently protected sources, not authored HQ
    # working notes. Runtime/config/nested repositories also have capture rules.
    areas.extend(BusinessHeadquarters(slug, excluded=("exports", "tables", "context/email", "context/agent-memory",
                                                    "agents", "code", "sessions", "transcripts", "logs", "browser"))
                 for slug in sorted(businesses.values()))
    return areas


def locations_of(paths):
    """``() -> (operations root, history repository)`` through ``InstancePaths``, validated again on every call."""
    def locations():
        try:
            return paths.operations, paths.history
        except InstancePathError as error:
            raise DomainError("history_unavailable", "The history location is not available",
                              details={"reason": "unsafe_path"}) from error
    return locations


def area_resolver(paths):
    """``(validated areas) -> resolved areas``: each registered ID or slug becomes its folder through ``InstancePaths``.

    A folder whose existing path crosses a symlink is not an error of the selection: it is returned refused, and the
    capture reports it as an excluded symlink without reading any of it."""
    def resolve(areas):
        operations = paths.operations
        resolved = []
        for area in areas:
            try:
                if isinstance(area, GlobalFiles):
                    folder = paths.files
                elif isinstance(area, GlobalAgentFiles):
                    folder = paths.agent_files(area.agent_id)
                elif isinstance(area, BusinessAgentFiles):
                    folder = paths.agent_files(area.agent_id, business_folder=area.business)
                else:
                    folder = paths.business_hq(area.business)
            except InstancePathError:
                resolved.append(ResolvedArea.refused(area))
                continue
            resolved.append(ResolvedArea.located(area, folder.relative_to(operations).parts))
        return tuple(resolved)
    return resolve


def history_over(database, paths, *, catalog, areas, clock, git_runner=None, git_timeout=300):
    """The engine bound to one explicit ``InstancePaths``; ``history_for`` is the installation's own selection of the rest."""
    if not isinstance(paths, InstancePaths):
        raise TypeError("History needs the explicit InstancePaths")
    return History(database, locations=locations_of(paths), resolve_areas=area_resolver(paths),
                   safe_segment=SEGMENT.fullmatch, catalog=catalog, areas=areas, clock=clock, git_runner=git_runner,
                   git_timeout=git_timeout)


def history_for(database, paths, extensions=()):
    return history_over(database, paths, catalog=catalog(extensions), areas=registered_areas, clock=utc_now)


def bind(runtime):
    runtime.history = history_for(runtime.database, runtime.paths, runtime.selection.history())
    runtime.history_observer = HistoryObserver(runtime.history.snapshot)
    runtime.changes.add_commit_listener(runtime.history_observer.notify_commit)


def start(runtime):
    runtime.history.validate()  # serving never initializes or repairs a target
    runtime.history_observer.start()
