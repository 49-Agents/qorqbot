"""Who may create a project.

Project creation is coordination authority, not a grantable capability of its own: only the installation's configured, active
Ordubashy creates projects, and only while its stored profile holds `coordinate_businesses` (with the ordinary
`business_create` operation grant on top). The installation owner does not create projects; the owner asks Ordubashy. Reading a
skill or enabling a plugin never authorizes it, and ordinary operation policy still applies."""

from __future__ import annotations

from qorqut.agents.global_scope import active_global
from qorqut.agents.roles import ORDUBASHY_ROLE
from qorqut.management.identity import configured_ordubashy
from qorqut_operations.projects.creation.operations import COORDINATION_CAPABILITY


def creator_actor(uow, identity) -> str | None:
    """``ORDUBASHY_ROLE`` when `identity` is the configured, active Ordubashy holding `coordinate_businesses`, else None.

    Resolved by the installation's configured identity and its active Global profile: never by name, model, runtime or the first
    matching role. An active global worker, a retired role and a system program never qualify, whatever they hold or are granted."""
    data = active_global(uow.connection, identity)
    if (data is None or COORDINATION_CAPABILITY not in data["scope"].get("global_capabilities", [])
            or data["role"]["id"] != ORDUBASHY_ROLE or identity != configured_ordubashy(uow)):
        return None
    return ORDUBASHY_ROLE
