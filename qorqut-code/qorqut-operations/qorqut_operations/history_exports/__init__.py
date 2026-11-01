"""Typed, trusted row exports for the operational history snapshot.

A domain contributes frozen ``HistoryContribution`` values (exported tables with exact scalar columns, a stable key,
closed structured projections and an optional trusted row selector, plus explicit classifications for every other table
it owns). An ``ExportCatalog`` of those contributions exports from one existing read-only Operations unit and reports
which actual tables remain unclassified. Nothing here writes a file, runs Git, loads plugins or exports a table that was
not explicitly declared; the host snapshot library owns the files and the repository. See
``docs/HISTORY_EXPORT_CHECKPOINT.md``.
"""

from .catalog import FORMAT, VERSION, ExportCatalog, ExportedTable, HistoryExport, SchemaIdentity, shard_id
from .contributions import (CLASSIFICATIONS, EXPORTED, PRIVATE_EXCLUDED, REPRESENTED, SEPARATE_HISTORY,
                            TECHNICAL_EXCLUDED, HistoryContribution, RowRequest, StructuredField, TableClassification,
                            TableExport, declared_json)
from .errors import HistoryCompositionError, HistoryExportError
from .fields import Arr, Boolean, FieldType, Integer, Obj, Real, Text

__all__ = ["Arr", "Boolean", "CLASSIFICATIONS", "EXPORTED", "ExportCatalog", "ExportedTable", "FORMAT", "FieldType",
           "HistoryCompositionError", "HistoryContribution", "HistoryExport", "HistoryExportError", "Integer", "Obj",
           "PRIVATE_EXCLUDED", "REPRESENTED", "Real", "RowRequest", "SEPARATE_HISTORY", "SchemaIdentity",
           "StructuredField", "TECHNICAL_EXCLUDED", "TableClassification", "TableExport", "Text", "VERSION",
           "declared_json", "shard_id"]
