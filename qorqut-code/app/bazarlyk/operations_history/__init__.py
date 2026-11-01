"""Operational Git history for the v1.2 host: a read-only snapshot of exported rows and authored files.

``History`` versions the rows exported through a trusted Operations ``ExportCatalog`` (read in one consistent unit, closed
before any file or Git work) together with the authored areas a trusted provider names from the current registrations,
in a private local Git repository at ``InstancePaths.history``. It is history for operational overview: not a migration,
backup, restore or replay mechanism, and independent of Context's Git authority, every source repository, the raw
transcript archives and the exact existing table/document histories. SQLite stays the authority and a history failure
never rolls back, repeats or approves accepted Operations work. See ``docs/v1.2/OPERATIONAL_HISTORY_CHECKPOINT.md``.

Nothing here is wired into application startup: construction and import do nothing, and ``HistoryObserver`` is only
started by its owner.
"""
from ._areas import (Area, AreaProvider, BusinessAgentFiles, BusinessHeadquarters, GlobalAgentFiles, GlobalFiles)
from ._observer import HistoryObserver, ObserverState, ObserverStop
from ._service import History

__all__ = ["Area", "AreaProvider", "BusinessAgentFiles", "BusinessHeadquarters", "GlobalAgentFiles", "GlobalFiles",
           "History", "HistoryObserver", "ObserverState", "ObserverStop"]
