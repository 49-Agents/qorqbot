"""Owner-directed roles may initiate work, but cannot receive agent delegation."""
import json

from .roles import LEARNING_APPLIER_ROLE

OWNER_DIRECTED_ROLES = frozenset({LEARNING_APPLIER_ROLE})


def owner_directed_target(ctx, identity, uow):
    if ctx.principal_kind != "agent":
        return False
    row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()
    role = json.loads(row["profile_json"]).get("role", {}).get("id") if row else None
    return role in OWNER_DIRECTED_ROLES
