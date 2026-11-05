"""Role identifiers of the global-only operating model."""

from __future__ import annotations

import json

from qorqut_operations.global_agents import REPEATABLE_GLOBAL_ROLES, SUPPORTED_GLOBAL_ROLES

# Retired: its ID stays so historical profiles resolve and the owner-only terminal protection still names it. No agent holds it.
DEV_ROLE = "dev"
# The one repeatable role: permanent workers managed by Ordubashy. Eligibility alone grants no authority.
GLOBAL_WORKER_ROLE = "global-worker"
ORDUBASHY_ROLE = "ordubashy"
LEARNING_APPLIER_ROLE = "learning-applier"

# SUPPORTED_GLOBAL_ROLES, the only roles an active profile may hold (never a business home; one ecosystem registration
# per role except the repeatable REPEATABLE_GLOBAL_ROLES), is owned by the shared Operations eligibility authority and
# re-exported here for host consumers.

# Historical pins stay readable for old records; no active profile may use them, and no operation registers, switches to
# or republishes them. Businesses own resources only, and the specialist roles (Founder, Global Marketing, Strategist,
# Coder) and the former business roster (Onbashy, Dev) ended with the move to Ordubashy and general workers, though their
# retained records and attribution still resolve.
RETIRED_ROLE_IDS = frozenset({"business-agent", "coder", "dev", "founder", "global-marketing", "onbashy",
                              "outreach-specialist", "marketing", "strategist"})
RETIRED_PLUGIN_IDS = frozenset({"cold-outbound-marketing", "email-outreach", "founding", "launchpads", "playbooks",
                                "right-framework-of-thinking"})


def profile_role(row) -> str | None:
    value = row["profile_json"]
    data = json.loads(value) if isinstance(value, str) else value
    return (data.get("role") or {}).get("id") if isinstance(data, dict) else None
