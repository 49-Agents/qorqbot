"""Role identifiers of the global-only operating model."""

from __future__ import annotations

import json

from qorqut_operations.global_agents import REPEATABLE_GLOBAL_ROLES, SUPPORTED_GLOBAL_ROLES

# The one repeatable role: permanent workers managed by Ordubashy. Eligibility alone grants no authority.
GLOBAL_WORKER_ROLE = "global-worker"
ORDUBASHY_ROLE = "ordubashy"
LEARNING_APPLIER_ROLE = "learning-applier"

# SUPPORTED_GLOBAL_ROLES, the only roles an active profile may hold (never a business home; one ecosystem registration
# per role except the repeatable REPEATABLE_GLOBAL_ROLES), is owned by the shared Operations eligibility authority and
# re-exported here for host consumers.


def profile_role(row) -> str | None:
    value = row["profile_json"]
    data = json.loads(value) if isinstance(value, str) else value
    return (data.get("role") or {}).get("id") if isinstance(data, dict) else None
