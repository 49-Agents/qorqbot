"""The pure SQL helper functions retained-domain DDL refers to, installed the same way on every connection.

Triggers and expression indexes carried over from v1 call these by name, so any connection that executes, validates
or inspects a composed schema (operational units, read-only snapshot/target validation and the disposable schema
inventory) must have them. Installing a function changes only that connection, never the database file."""

from __future__ import annotations

from qorqut_operations import local_time


def install_sql_functions(connection) -> None:
    # Attribution of the writing actor for triggers; a unit of work rebinds it to its own actor.
    connection.create_function("inbox_actor", 0, lambda: None)
    # San Juan operational day/hour of a stored UTC instant (day buckets and expression indexes).
    connection.create_function("san_juan_date", 1, local_time.local_date, deterministic=True)
    connection.create_function("san_juan_hour", 1, local_time.local_hour, deterministic=True)
