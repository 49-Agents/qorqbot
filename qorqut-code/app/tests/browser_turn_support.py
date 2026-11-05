"""A synthetic assembled store for browser-turn queue checks: the host's own composed schema, no runtime or service.

It replaces the obsolete ``test_operational_delegation_store.setup_store`` for these checks with the current synthetic
setup (``host_assembly`` and ``initialize_empty_target`` on pytest ``tmp_path`` paths). The returned tuple keeps that
fixture's shape: ``(database, store, parent_id, business_id, now)``. The parent is an active agent with an ecosystem
profile and no Global role, so it can own delegated assignments but is not a current Global agent.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bazarlyk.agents.operational_delegation import OperationalDelegation
from host_foundation_support import make_paths
from qorqut_operations.contracts import new_id


def setup_store(tmp_path):
    from bazarlyk.composition.assembly import host_assembly, initialize_empty_target
    from bazarlyk.service.changes import ChangeFeed

    database = initialize_empty_target(make_paths(tmp_path), host_assembly(ChangeFeed()))
    now = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    store = OperationalDelegation(clock=lambda: now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                                  dispatch_reason=None)
    parent_id, business_id = new_id(), new_id()
    with database.transaction() as uow:
        now_text = store.clock()
        uow.repository("principals").insert({"id": parent_id, "kind": "agent", "display_name": "Astra",
            "active": 1, "created_at": now_text, "updated_at": now_text})
        uow.repository("businesses").insert({"id": business_id, "slug": "demo", "workspace": str(tmp_path / "demo"),
            "hq_path": str(tmp_path / "demo-hq"), "name": "Demo", "created_at": now_text, "updated_at": now_text})
        uow.repository("agent_profiles", business_id=None).insert({"agent_id": parent_id, "principal_id": parent_id,
            "namespace": "ecosystem", "name": "Astra", "workspace": str(tmp_path / "parent"),
            "profile_json": {"scope": {"kind": "ecosystem", "allowed_business_ids": [business_id],
                "global_capabilities": ["manage_delegated_assignments"]}}, "status": "active",
            "created_at": now_text, "updated_at": now_text})
    return database, store, parent_id, business_id, now
