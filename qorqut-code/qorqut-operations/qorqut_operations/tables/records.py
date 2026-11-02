"""The table-only record hook the host's record effects call."""

from __future__ import annotations


def force_table_revision(plan, change):
    """Whether ``change`` must advance its record's revision although its own fields are unchanged.

    A table whose rows change in the same plan always takes a new revision (a row-only edit included), so each saved
    row batch is one table revision and one history version. ``policy_effect`` rows carry an approval, not a change."""
    table = change.resource.resource_type
    return table == "data_tables" and any(
        child.resource.resource_type == "data_rows" and child.action != "policy_effect" for child in plan.changes)
