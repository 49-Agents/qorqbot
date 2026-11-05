"""Operational history: typed row exports and the read-only Git snapshot engine that versions them.

A domain contributes frozen ``HistoryContribution`` values (exported tables with exact scalar columns, a stable key,
closed structured projections and an optional trusted row selector, plus explicit classifications for every other table
it owns). An ``ExportCatalog`` of those contributions exports from one existing read-only Operations unit and reports
which actual tables remain unclassified. Nothing in the export half writes a file, runs Git, loads plugins or exports a
table that was not explicitly declared. See ``docs/HISTORY_EXPORT_CHECKPOINT.md``.

``History`` is the snapshot engine: it versions the exported rows (read in one consistent unit, closed before any file or
Git work) together with the authored areas a trusted provider names from the current registrations, in a private local
Git repository. It is history for operational overview: not a migration, backup, restore or replay mechanism, and
independent of Context's Git authority, every source repository, the raw transcript archives and the exact existing
table/document histories. SQLite stays the authority and a history failure never rolls back, repeats or approves accepted
Operations work.

The host decides where things are. It supplies ``locations() -> (operations_root, history_repository)`` and
``resolve_areas(validated_areas) -> resolved areas`` (built with ``ResolvedArea.located`` / ``ResolvedArea.refused``), plus
the validator for one registered ID or slug; they are asked again on every use. One ``HistoryObserver`` bound to committed
changes triggers snapshots through the host's owned lifecycle. Construction and import do nothing; only an explicit
``History.initialize()`` creates the history repository.
"""

from ._areas import (Area, AreaProvider, BusinessAgentFiles, BusinessHeadquarters, GlobalAgentFiles, GlobalFiles,
                     ResolvedArea)
from .catalog import FORMAT, VERSION, ExportCatalog, ExportedTable, HistoryExport, SchemaIdentity, shard_id
from .contributions import (CLASSIFICATIONS, EXPORTED, PRIVATE_EXCLUDED, REPRESENTED, SEPARATE_HISTORY,
                            TECHNICAL_EXCLUDED, HistoryContribution, RowRequest, StructuredField, TableClassification,
                            TableExport, declared_json)
from .errors import HistoryCompositionError, HistoryExportError
from .fields import Arr, Boolean, FieldType, Integer, Obj, Real, Text
from ._observer import HistoryObserver, ObserverState, ObserverStop
from ._service import History

__all__ = ["Area", "AreaProvider", "Arr", "Boolean", "BusinessAgentFiles", "BusinessHeadquarters", "CLASSIFICATIONS",
           "EXPORTED", "ExportCatalog", "ExportedTable", "FORMAT", "FieldType", "GlobalAgentFiles", "GlobalFiles",
           "History", "HistoryCompositionError", "HistoryContribution", "HistoryExport", "HistoryExportError",
           "HistoryObserver", "Integer", "ObserverState", "ObserverStop", "Obj", "PRIVATE_EXCLUDED", "REPRESENTED",
           "Real", "ResolvedArea", "RowRequest", "SEPARATE_HISTORY", "SchemaIdentity", "StructuredField",
           "TECHNICAL_EXCLUDED", "TableClassification", "TableExport", "Text", "VERSION", "declared_json", "shard_id"]
