"""Agent-maintained Project and Global tables, their original data uploads and their per-revision Git history.

The operations, uploads and history publisher are built by the host with explicit roots and the source store; the
schema, policy rule, record hook and history-coverage declarations are assembled by the host into its fixed groups."""

from .exports import CLASSIFICATIONS, EXPORTS
from .history import HISTORY_REF, TableHistory
from .operations import ARCHIVE, TableOperations
from .policy import GLOBAL_TABLE_OPERATIONS, GLOBAL_TABLE_ROLES, global_tables
from .records import force_table_revision
from .schema import IMMUTABLE_FIELDS, SCHEMA, TABLE_NAMES, TABLES
from .uploads import DataUploadOperations

__all__ = ["ARCHIVE", "CLASSIFICATIONS", "DataUploadOperations", "EXPORTS", "GLOBAL_TABLE_OPERATIONS", "GLOBAL_TABLE_ROLES",
           "HISTORY_REF", "IMMUTABLE_FIELDS", "SCHEMA", "TABLES", "TABLE_NAMES", "TableHistory", "TableOperations",
           "force_table_revision", "global_tables"]
