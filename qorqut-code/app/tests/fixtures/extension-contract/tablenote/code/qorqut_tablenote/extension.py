"""Table Note: the test extension with a table of its own (docs/EXTENSION-CONTRACT.md, section 10).

``tables()`` declares one table, ``tablenote_items``. Qorqbot makes it in the store at the first start that loads this
code, records it, and keeps it when the extension is removed. ``tablenote_count`` answers how many rows it holds. The
factory and ``install`` read nothing, start nothing and contact nothing.
"""

from __future__ import annotations

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.plans import schema
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

from qorqut.extensions import Extension

ID = "tablenote"
COUNT_TOOL = "tablenote_count"
TABLE = "tablenote_items"
EXPOSURE = frozenset({"mcp"})
DESCRIPTION = "How many notes the Table Note table holds."
STATEMENTS = (f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY NOT NULL, text TEXT NOT NULL, "
              "revision INTEGER NOT NULL DEFAULT 1)",)


class TableNote(Extension):
    id = ID

    def tables(self):
        return (StorageContribution(ID, {TABLE: TableSpec(("id",))}, schema=STATEMENTS),)

    def catalog(self):
        return (CatalogContribution(ID, entries=(CatalogEntry(COUNT_TOOL, False, "read", EXPOSURE),)),)

    def install(self, runtime):
        runtime.registry.register(OperationDescriptor(
            COUNT_TOOL, schema({"scope": {"const": "global"}}), effect_kind="read", supports_signoff=False,
            scope_kinds={"global"}, exposure=EXPOSURE,
            resolve_namespace=lambda ctx, args: "global", global_capability="read_global_context", default_mode="allow",
            description=DESCRIPTION, read=lambda ctx, args, options, uow: {
                "count": uow.connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]}))


def create(config=None):
    if config:
        raise DomainError("invalid_installation", "Table Note takes no configuration.")
    return TableNote()
