"""Host-owned registration and lifecycle wiring for operational history."""
from qorqut_operations.contracts import utc_now

from . import BusinessAgentFiles, BusinessHeadquarters, GlobalAgentFiles, GlobalFiles, History, HistoryObserver
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


def history_for(database, paths, extensions=()):
    return History(database, paths=paths, catalog=catalog(extensions), areas=registered_areas, clock=utc_now)


def bind(runtime):
    runtime.history = history_for(runtime.database, runtime.paths, runtime.selection.history())
    runtime.history_observer = HistoryObserver(runtime.history.snapshot)
    runtime.changes.add_commit_listener(runtime.history_observer.notify_commit)


def start(runtime):
    runtime.history.validate()  # serving never initializes or repairs a target
    runtime.history_observer.start()
