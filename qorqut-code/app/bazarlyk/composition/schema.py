"""The retained host domains' storage contributions: source-derived DDL plus the v1 repository specifications.

The DDL is ``schema_ddl`` (generated from the deployed v1 migration source by ``tools/derive_host_schema.py``; no
migration runs here), the table specifications and immutable columns are ``tables``. Every table belongs to exactly
one owner and the engine's own tables are never redefined: the engine refuses a duplicate table, and this module
checks before it hands the contributions over that the generated ownership and the specifications agree."""

from __future__ import annotations

from qorqut_operations import (actions, context_filing, context_graph_edits, context_queue, context_usage, discussion, tags,
                               tasks)
from qorqut_operations.storage.assembly import AssemblyError, StorageContribution

from . import campaigns, capture, schema_ddl, tables
from .documents import RESOURCE_TYPE as DOCUMENTS

# Document availability is tracked beside every file receipt (v1), so the documents owner requires its lifecycle.
REQUIRED_LIFECYCLES = {"documents": frozenset({DOCUMENTS})}


def record_contributions() -> tuple[StorageContribution, ...]:
    """The engine's operational-record storage, first and in the order its DDL needs: the tag catalog and the discussion,
    Tasks on both, Actions on those, the Context queue on the Action revisions, and the three Context companions that
    sit on that one queue (source filings and completion receipts, controlled graph edits, the model-call usage
    ledger). One contribution per owner: the host runtime builds exactly one queue over these tables. The host objects
    that sit on these tables (the content-session event index) and the host tables that reference them are created
    after them."""
    return (tags.storage_contribution(), discussion.storage_contribution(), tasks.storage_contribution(),
            actions.storage_contribution(), context_queue.storage_contribution(), context_filing.storage_contribution(),
            context_graph_edits.storage_contribution(), context_usage.storage_contribution())


def storage_contributions() -> tuple[StorageContribution, ...]:
    owned = {}
    for owner, names in schema_ddl.OWNER_TABLES.items():
        for name in names:
            if name in owned:
                raise AssemblyError(f"Table {name} is assigned to two schema owners")
            owned[name] = owner
    if set(schema_ddl.OWNER_SCHEMA) != set(schema_ddl.OWNER_TABLES):
        raise AssemblyError("Generated schema and table ownership name different owners")
    unowned = sorted(set(tables.TABLE_SPECS) - set(owned))
    if unowned:
        raise AssemblyError(f"Repository specifications without a schema owner: {', '.join(unowned)}")
    stray = sorted(set(tables.IMMUTABLE_FIELDS) - set(tables.TABLE_SPECS))
    if stray:
        raise AssemblyError(f"Immutable columns for undeclared tables: {', '.join(stray)}")
    contributions = []
    for owner, names in schema_ddl.OWNER_TABLES.items():
        specs = {name: tables.TABLE_SPECS[name] for name in names if name in tables.TABLE_SPECS}
        contributions.append(StorageContribution(
            owner, specs, {name: fields for name, fields in tables.IMMUTABLE_FIELDS.items() if name in specs},
            schema_ddl.OWNER_SCHEMA[owner], required_lifecycles=REQUIRED_LIFECYCLES.get(owner, frozenset())))
    # The change-feed capture for the engine's record tables comes last: its triggers write into the host's ``inbox_changes``.
    return (*record_contributions(), *contributions, capture.capture_contribution(), campaigns.campaign_contribution())
