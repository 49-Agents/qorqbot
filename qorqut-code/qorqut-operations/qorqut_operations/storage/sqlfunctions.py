"""The pure SQL helper functions retained-domain DDL refers to, installed the same way on every connection.

Triggers and expression indexes carried over from v1 call these by name, so any connection that executes, validates
or inspects a composed schema (operational units, read-only snapshot/target validation and the disposable schema
inventory) must have them. Installing a function changes only that connection, never the database file."""

from __future__ import annotations
from zoneinfo import ZoneInfo

from qorqut_operations import local_time
from qorqut_operations.storage.relocation import refuse


def install_sql_functions(connection) -> None:
    # Attribution of the writing actor for triggers; a unit of work rebinds it to its own actor.
    connection.create_function("inbox_actor", 0, lambda: None)
    # Default refusal of a Task scope relocation; a writable unit of work arms its own guard (see ``storage.relocation``).
    connection.create_function("relocation_permits", 3, refuse)
    # Historical expression indexes retain the meaning of their original function names.
    # Current summaries call local_time directly and use the configured installation zone.
    legacy_zone = ZoneInfo("America/Puerto_Rico")
    connection.create_function("san_juan_date", 1, lambda value: local_time.local_date(value, legacy_zone), deterministic=True)
    connection.create_function("san_juan_hour", 1, lambda value: local_time.local_hour(value, legacy_zone), deterministic=True)
