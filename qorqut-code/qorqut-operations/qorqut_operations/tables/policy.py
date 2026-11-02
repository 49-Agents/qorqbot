"""The Global-table permission rule, contributed to the policy evaluator as data.

Global tables are a separate namespace from every business. The same eight operations (plus archive) serve both;
``GLOBAL_TABLE_ROLES`` alone may use the Global one, whatever their business defaults. ``global_tables`` is a
``before_override`` rule: the host places it in that phase, at its existing position among the combined rules, and
supplies the configured role and identity facts the evaluator passes in. It alters nothing but the decision it returns."""

from __future__ import annotations

from qorqut_operations.contracts import PolicyDecision
from qorqut_operations.roles import ORDUBASHY_ROLE

GLOBAL_TABLE_ROLES = frozenset({ORDUBASHY_ROLE})
GLOBAL_TABLE_OPERATIONS = frozenset({"tables_list", "tables_get", "tables_rows_list", "tables_history", "tables_create",
                                     "tables_update", "tables_rows_write", "tables_history_retry", "tables_archive"})


def global_tables(name, defaults, overrides, registry, *, role_id, scope, resolve):
    if name in GLOBAL_TABLE_OPERATIONS and scope == "global":
        # The named roles hold direct Global defaults. Any other eligible Global agent (the runtime checks that it is
        # an active ecosystem agent holding read_global_context) needs an explicit override; role business
        # defaults never carry over, and a missing grant denies.
        default_role = role_id in GLOBAL_TABLE_ROLES
        if name == "tables_archive":
            # An agent never archives a Global table on its own authority: an explicit deny (here or on
            # tables_update, which carries the same effect) wins, and no 'allow' removes the owner's approval.
            grants = (overrides.get(name), overrides.get("tables_update"))
            if "deny" in grants:
                return PolicyDecision("deny", "global_table_archive_owner_approval")
            if default_role or {"allow", "signoff"} & set(grants):
                return PolicyDecision("signoff", "global_table_archive_owner_approval")
            return PolicyDecision("deny", "global_table_grant_required")
        if name in overrides:
            return PolicyDecision(overrides[name], "agent_override")
        return (PolicyDecision("allow", "global_table_role") if default_role
                else PolicyDecision("deny", "global_table_grant_required"))
    if name == "tables_archive":
        return (PolicyDecision(overrides[name], "agent_override") if name in overrides
                else resolve("tables_update"))
