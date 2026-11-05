"""Native Overseer toolkit: nested dispatch, local effects and role-scoped eligibility.

Synthetic instances only: the real module/runtime and the existing mocked
authenticated MCP peer. No native product session, provider call or live data.
"""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, Principal, new_id, utc_now

from bazarlyk.agentic import client as native
from bazarlyk.agentic.access import AgenticAccess
from bazarlyk.agentic.view import OVERSEER_PACKAGE, OVERSEER_ROLE
from host_foundation_support import (build_instance, create_registered_global, create_worker,
                                     grant_capabilities, register_sources)
from test_agentic_client import fixture, call


def material():
    return {"schema_version": 1, "id": str(new_id())}


def test_native_client_dispatches_nested_overseer_reads_and_mutations(fixture):
    fixture.peer.manage = True
    empty = call(fixture, "overseer.proposals.list")
    assert empty["ok"], empty
    assert empty["result"] == {"items": [], "next_cursor": None, "complete": True, "availability": "available"}
    record = material()
    request = {"record": record, "operation_id": "native-material"}
    created = call(fixture, "overseer.materials.record", request)
    assert created["ok"], created
    assert created["result"] == record
    assert call(fixture, "overseer.materials.record", request) == created
    assert call(fixture, "overseer.materials.get", {"id": record["id"]})["result"] == record


def test_nested_overseer_operations_keep_current_management_authority(fixture):
    from qorqut_agentic.errors import AgenticError
    record = material()
    denied = call(fixture, "overseer.materials.record", {"record": record, "operation_id": "denied"})
    assert not denied["ok"] and denied["error"]["code"] == "unavailable", denied
    assert fixture.peer.requests[-1]["method"] == "tools/call"
    with pytest.raises(AgenticError):
        fixture.layer.overseer.materials.get(record["id"])
    fixture.peer.failure = "denied"
    refused = call(fixture, "overseer.proposals.list")
    assert not refused["ok"] and refused["error"]["code"] == "forbidden" and "SECRET" not in json.dumps(refused)


@pytest.mark.parametrize("field", ["actor", "actor_id", "selection", "root", "state", "instance_id", "runtime"])
def test_overseer_request_cannot_override_native_authority(fixture, field):
    fixture.peer.manage = True
    result = call(fixture, "overseer.materials.record",
                  {"record": material(), "operation_id": "override", field: "forged"})
    assert result["error"]["code"] == "invalid_request", result
    assert fixture.peer.requests == []


@pytest.mark.parametrize("arguments", [
    {"record": material(), "operation_id": "attributed", "attribution": {"reason": "claimed owner instruction"}},
    {"record": material(), "operation_id": "attributed", "attribution": {"reason": "x", "actor_id": "forged"}},
])
def test_overseer_mutations_accept_no_caller_attribution(fixture, arguments):
    fixture.peer.manage = True
    result = call(fixture, "overseer.materials.record", arguments)
    assert result["error"]["code"] == "invalid_request", result
    assert fixture.peer.requests == []


def test_overseer_effect_classification_covers_every_allowlisted_operation():
    from qorqut_agentic.cli import OPERATIONS
    reads = native._OVERSEER_READS
    assert reads <= native._OVERSEER_OPERATIONS <= set(OPERATIONS)
    verbs = {"get", "list", "get_batch", "record", "create", "revise", "bind", "save_batch", "save_plan"}
    assert {name.rsplit(".", 1)[1] for name in native._OVERSEER_OPERATIONS} <= verbs
    assert {name for name in native._OVERSEER_OPERATIONS if name.rsplit(".", 1)[1] in {"get", "list", "get_batch"}} == reads
    assert native._OVERSEER_MUTATIONS == native._OVERSEER_OPERATIONS - reads


@pytest.mark.parametrize("operation,mutates", [
    ("overseer.materials.record", True),
    ("overseer.proposals.list", False),
])
def test_lost_overseer_reply_reports_uncertainty_only_for_mutations(fixture, monkeypatch, operation, mutates):
    fixture.peer.manage = True
    record = material()
    arguments = {"record": record, "operation_id": "lost-reply"} if mutates else {}
    original = native.dispatch
    attempts = []
    def lose_reply(layer, request):
        result = original(layer, request)
        attempts.append(result)
        raise RuntimeError("SECRET lost locally")
    monkeypatch.setattr(native, "dispatch", lose_reply)
    result = call(fixture, operation, arguments)
    assert not result["ok"] and "SECRET" not in json.dumps(result)
    assert len(attempts) == 1
    assert result["error"]["details"]["outcome_unknown"] is mutates
    if mutates:
        assert fixture.layer.overseer.materials.get(record["id"]) == record


# -- fresh host installation: the packaged package is eligible only to its role --

def registration(instance, identity):
    """One current machine registration for an already created identity (synthetic)."""
    import hashlib
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


def context(host, identity):
    return host.instance.runtime.resolver.runtime_context(
        Principal(identity, "agent"), session_registration_id=host.registrations[identity])


def invoke(host, identity, operation="catalog.list"):
    return host.instance.invoke(context(host, identity), "agentic_access", {"operation": operation})


def selection(host, identity):
    result = invoke(host, identity)
    assert result.status == "ok", result.error
    return result.to_dict()["data"]["selection"]


def projection(host, selection_value):
    return {row["ref"] for row in host.view.layer.catalog.list(selection=selection_value)["entries"]}


def test_fresh_installation_offers_overseer_only_to_the_current_overseer_role(host):
    applier = selection(host, host.applier)
    assert OVERSEER_PACKAGE in applier["package_ids"]
    assert OVERSEER_ROLE == "learning-applier"
    for identity in (host.manager, host.worker):
        unrelated = selection(host, identity)
        assert OVERSEER_PACKAGE not in unrelated["package_ids"]
        assert not any(ref.startswith(OVERSEER_PACKAGE + "/") for ref in projection(host, unrelated))
    # The intended current role receives the packaged procedures, including the
    # historical missed-instruction extraction guide that stays with Overseer.
    offered = projection(host, applier)
    assert {"overseer/extract", "overseer/review", "overseer/apply",
            "overseer/extract-instructions", "overseer/review-instructions",
            "overseer/apply-instructions", "overseer/record-direct-instruction"} <= offered
    # Ordubashy keeps its existing shared management toolkit; the missed
    # direct-instruction extraction guide is not part of that shared package.
    manager = projection(host, selection(host, host.manager))
    assert {"agentic-management/manage", "agentic-management/bootstrap"} <= manager
    assert not any(ref.endswith("record-direct-instruction") for ref in manager)


def test_shared_procedural_model_and_operational_authority_stay_separate(host):
    # A missing or unknown role still sees the general shared catalog; only the
    # Overseer package is withheld by the narrow role rule.
    general = host.view.selection({})
    assert "agentic-management" in general["package_ids"] and OVERSEER_PACKAGE not in general["package_ids"]
    assert host.view.selection({"role": {"id": "global-worker"}})["package_ids"] == general["package_ids"]
    # Selection is routing, never authority: the toolkit operation itself still
    # requires the current identity and management capability.
    for operation in ("overseer.proposals.list", "overseer.materials.record", "overseer.applications.save_plan"):
        assert invoke(host, host.worker, operation).status == "error"
        assert invoke(host, host.applier, operation).status == "error"
    grant_capabilities(host.instance, host.applier, "manage_agentic_layer")
    for operation in ("overseer.proposals.list", "overseer.materials.record"):
        assert invoke(host, host.applier, operation).status == "ok", operation
    assert invoke(host, host.worker, "overseer.materials.record").status == "error"


def test_current_registration_and_explicit_deny_still_refuse_overseer_access(host):
    with host.instance.database.read() as uow:
        with pytest.raises(DomainError):
            host.access.read(replace(context(host, host.applier), session_registration_id=host.registrations[host.manager]),
                             {"operation": "overseer.proposals.list"}, None, uow)
        stale = replace(context(host, host.applier), session_registration_id=new_id())
        with pytest.raises(DomainError):
            host.access.read(stale, {"operation": "overseer.proposals.list"}, None, uow)
    with host.instance.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json, "
            "'$.tool_policy.overrides.agentic_access','deny') WHERE agent_id=?", (host.applier,))
    assert invoke(host, host.applier).status == "error"
    assert invoke(host, host.manager).status == "ok"