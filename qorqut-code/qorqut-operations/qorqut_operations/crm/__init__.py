"""CRM tables: memberships of existing Task records, the Table discussion and recorded activity (portable domain).

The host composes ``CRMOperations`` after the Task service and the item resolver exist, installs ``storage_contribution()``,
the catalog entries and the history classifications once, and binds the Table service with ``TableOperations.bind_crm``.
"""

from .operations import (CATALOG_ENTRIES, CLASSIFICATIONS, CRMOperations, IMMUTABLE_FIELDS, MUTATION_NAMES, READ_NAMES, SCHEMA,
                         TABLES, TABLE_NAMES, storage_contribution)

__all__ = ["CATALOG_ENTRIES", "CLASSIFICATIONS", "CRMOperations", "IMMUTABLE_FIELDS", "MUTATION_NAMES", "READ_NAMES", "SCHEMA",
           "TABLES", "TABLE_NAMES", "storage_contribution"]
