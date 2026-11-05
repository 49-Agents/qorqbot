"""Current host identity/policy gates; only synthetic stores in pytest scratch."""
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
from qorqut_agentic import AgenticLayer
from qorqut_operations.contracts import DomainError, Principal, new_id, utc_now

from bazarlyk.agentic.access import AgenticAccess, MANAGEMENT_OPERATIONS
from bazarlyk.agents.operations import WORKER_BARRED_CAPABILITIES
from host_foundation_support import (build_instance, create_registered_global, create_worker,
                                     grant_capabilities, owner_global_context, publish_role, register_sources)


class View:
    def __init__(self, layer):
        self.layer = layer
        self.profiles = []

    def selection(self, profile, *, project_id=None, work_type=None):
        self.profiles.append(profile)
        return {"package_ids": ["common"], "project_id": project_id, "work_type": work_type}


def registration(instance, identity):
    now, registration_id, credential_id = utc_now(), new_id(), new_id()
    with instance.database.transaction() as uow:
        row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()
        uow.repository("credentials").insert({"id": credential_id, "principal_id": identity,
            "token_hash": hashlib.sha256(registration_id.encode()).hexdigest(), "created_at": now, "updated_at": now})
        uow.repository("machine_registrations").insert({"id": registration_id, "principal_id": identity,
            "workspace": row["workspace"], "service_url": "http://synthetic.invalid", "credential_id": credential_id,
            "profile_revision": row["revision"], "created_at": now, "updated_at": now})
    return registration_id


@pytest.fixture
def host(tmp_path):
    instance, _ = build_instance(tmp_path)
    register_sources(instance, instance.business, "first")
    worker = create_worker(instance, "native")
    manager = create_registered_global(instance, "ordubashy")
    applier = create_registered_global(instance, "learning-applier")
    view = instance.runtime.agentic_view
    access = AgenticAccess(instance.runtime.resolver, view)
    registrations = {identity: registration(instance, identity) for identity in (worker, manager, applier)}
    return SimpleNamespace(instance=instance, access=access, view=view, worker=worker, manager=manager,
                           applier=applier, registrations=registrations)


def context(host, identity=None):
    identity = identity or host.worker
    return host.instance.runtime.resolver.runtime_context(
        Principal(identity, "agent"), session_registration_id=host.registrations[identity])


def invoke(host, operation="catalog.list", identity=None, **arguments):
    return host.instance.invoke(context(host, identity), "agentic_access", {"operation": operation, **arguments})


def test_read_is_registered_read_only_and_uses_current_profile(host, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("service access spawned a process"))
    descriptor = host.instance.runtime.registry.get("agentic_access")
    assert descriptor.effect_kind == "read" and not descriptor.supports_signoff
    assert descriptor.exposure == {"mcp"} and descriptor.prepare is None and descriptor.external is None
    result = invoke(host, project_id=host.instance.business, work_type="review")
    assert result.status == "ok", result.error
    data = result.to_dict()["data"]
    assert data["actor"] == {"actor_id": host.worker, "actor_kind": "agent"}
    assert data["registration_id"] == host.registrations[host.worker]
    assert data["root"] == str(host.view.layer.root) and data["state"] == str(host.view.layer.state)
    assert data["instance_id"] == host.view.layer.instance_id and not data["can_manage"]
    assert data["attention_required"] is False
    assert data["selection"]["project_id"] == host.instance.business
    assert data["selection"]["work_type"] == "review"
    assert "agentic-management" in data["selection"]["package_ids"]


def test_attention_requirement_tracks_the_configured_ordubashy(host):
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=?", (host.manager,))
    assert invoke(host, identity=host.manager).to_dict()['data']['attention_required'] is True
    assert invoke(host, identity=host.worker).to_dict()['data']['attention_required'] is False


@pytest.mark.parametrize("field", ["actor", "root", "state", "selection", "runtime", "registration_id"])
def test_access_rejects_override_fields(host, field):
    assert invoke(host, **{field: "forged"}).status == "error"


@pytest.mark.parametrize("operation", ["lifecycle.initialize", "lifecycle.open", "unknown", "tools.execute"])
def test_access_rejects_installer_and_unknown_operations(host, operation):
    assert invoke(host, operation).status == "error"


def test_project_and_policy_revocation(host):
    assert invoke(host, project_id=new_id()).status == "error"
    before = context(host)
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), host.instance.business))
    denied = host.instance.invoke(before, "agentic_access", {"operation": "catalog.list", "project_id": host.instance.business})
    assert denied.status == "error"
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json, "
            "'$.tool_policy.overrides.agentic_access','deny') WHERE agent_id=?", (host.worker,))
    assert invoke(host).status == "error"


@pytest.mark.parametrize("kind", ["absent", "foreign", "stale", "workspace", "revoked", "expired", "inactive", "delegated", "worker_source"])
def test_native_registration_and_current_identity_required(host, kind):
    ctx = context(host)
    with host.instance.database.transaction() as uow:
        if kind in {"absent", "foreign", "stale"}:
            ctx = replace(ctx, session_registration_id={"absent": None, "foreign": host.registrations[host.manager],
                                                       "stale": new_id()}[kind])
        elif kind == "workspace":
            uow.connection.execute("UPDATE machine_registrations SET workspace=? WHERE id=?",
                                   ("/synthetic/wrong", ctx.session_registration_id))
        elif kind in {"revoked", "expired"}:
            column = "revoked_at" if kind == "revoked" else "expires_at"
            uow.connection.execute(f"UPDATE credentials SET {column}='2000-01-01T00:00:00Z' WHERE id="
                                   "(SELECT credential_id FROM machine_registrations WHERE id=?)", (ctx.session_registration_id,))
        elif kind == "inactive":
            uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (host.worker,))
        elif kind == "delegated":
            ctx = replace(ctx, delegation_binding_id=new_id(), delegation_assignment_id=new_id(), delegation_generation=1)
        else:
            ctx = replace(ctx, source="worker")
    with host.instance.database.read() as uow, pytest.raises(DomainError):
        host.access.read(ctx, {"operation": "catalog.list"}, None, uow)


def test_management_requires_identity_and_capability(host):
    for identity in (host.worker, host.manager, host.applier):
        assert invoke(host, "history.status", identity).status == "error"
    for identity in (host.manager, host.applier):
        grant_capabilities(host.instance, identity, "manage_agentic_layer")
    # Ordubashy is only the manager when the installation explicitly selects it.
    assert invoke(host, "history.status", host.manager).status == "error"
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=?", (host.manager,))
        # Even an old forged worker grant must not turn that identity into a manager.
        row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (host.worker,)).fetchone()
        data = json.loads(row[0]); data["scope"]["global_capabilities"].append("manage_agentic_layer")
        uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (json.dumps(data), host.worker))
    for operation in MANAGEMENT_OPERATIONS:
        active_edit = operation.startswith('authoring.') or operation == 'history.restore'
        assert invoke(host, operation, host.manager).status == ("error" if active_edit or operation.startswith('overseer.') else "ok"), operation
        assert invoke(host, operation, host.applier).status == ("error" if active_edit else "ok"), operation
        assert invoke(host, operation, host.worker).status == "error", operation


def test_capability_cannot_be_granted_to_worker_on_create_or_update(host):
    assert "manage_agentic_layer" in WORKER_BARRED_CAPABILITIES
    owner = owner_global_context(host.instance)
    scope = {"kind": "ecosystem", "allowed_business_ids": [],
             "global_capabilities": ["read_global_context", "manage_agentic_layer"]}
    created = host.instance.invoke(owner, "agents_create", {"name": "barred", "display_name": "Barred",
        "role": publish_role(host.instance, "global-worker"), "scope": scope, "plugins": [],
        "assignment_brief": "Synthetic worker"})
    updated = host.instance.invoke(owner, "agents_update", {"agent_id": host.worker, "changes": {"scope": scope}})
    assert created.status == updated.status == "error"


def test_overseer_catalog_is_scoped_to_the_current_learning_applier_role(host):
    with host.instance.database.read() as uow:
        profiles = {agent: json.loads(uow.connection.execute(
            "SELECT profile_json FROM agent_profiles WHERE agent_id=?", (agent,)).fetchone()[0])
            for agent in (host.worker, host.manager, host.applier)}
    applier = host.view.selection(profiles[host.applier])
    manager = host.view.selection(profiles[host.manager])
    worker = host.view.selection(profiles[host.worker])
    assert "overseer" in applier["package_ids"]
    assert "agentic-management" in applier["package_ids"]
    assert "overseer" not in manager["package_ids"] and "overseer" not in worker["package_ids"]
    assert "agentic-management" in manager["package_ids"] and "agentic-management" in worker["package_ids"]
    # The direct-instruction entry ships only with the Overseer package, so it is not offered to Ordubashy's ordinary
    # immediate-application path.
    assert host.view.layer.catalog.read_entry("overseer/record-direct-instruction")
