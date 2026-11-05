"""Reuse of immutable validated agent profiles in ``AgentConfigResolver.profile``.

A synthetic, disposable instance holds registered principals and ``agent_profiles`` rows written here. The role pins are
placeholders: ``profile`` never loads artifacts, so no role source, model, provider or network is involved. ``from_dict``
is counted to show when validation actually runs; every check that must stay current is asserted against a warm cache.

Only the supported global roles operate, with ecosystem scope and no business home. ``profile`` returns the reused parse
projected onto the effective scope of the reading transaction (every active business), so each call is a new, equal
object: reuse is shown by the ``from_dict`` count, and ``_parsed_profile`` itself still returns the very same entry.
"""

from __future__ import annotations

import json
import threading
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from bazarlyk.agents import resolver as resolver_module
from bazarlyk.agents.models import AgentProfile
from bazarlyk.agents.resolver import PROFILE_CACHE_LIMIT, AgentConfigResolver
from bazarlyk.composition.assembly import host_assembly, initialize_empty_target
from bazarlyk.composition.catalog import builtin_registry
from bazarlyk.composition.locations import agent_workspace_of
from bazarlyk.service.changes import ChangeFeed
from instance_support import synthetic_paths
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id, utc_now

ROLE = {"version": "1.0.0", "digest": "sha256:" + "0" * 64}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("profile-cache") / "instance"
    root.mkdir()
    paths = synthetic_paths(root)
    return SimpleNamespace(paths=paths, database=initialize_empty_target(paths, host_assembly(ChangeFeed())))


@pytest.fixture
def resolver(world):
    """A resolver over a frozen registry: the serving condition under which profiles may be reused."""
    return AgentConfigResolver(world.database, world.paths, builtin_registry().freeze(), catalog=SimpleNamespace())


@pytest.fixture
def parses(monkeypatch):
    """Every real ``AgentProfile.from_dict`` call, with the registry it validated against."""
    calls = []
    original = AgentProfile.from_dict.__func__

    def counted(cls, value, registry, **options):
        calls.append(registry)
        return original(cls, value, registry, **options)

    monkeypatch.setattr(AgentProfile, "from_dict", classmethod(counted))
    return calls


def business(world):
    identity, slug = new_id(), "b" + new_id()[:8]
    with world.database.transaction() as uow:
        uow.repository("businesses").insert({"id": identity, "slug": slug, "name": slug,
            "workspace": str(world.paths.business_root(slug)), "hq_path": str(world.paths.business_hq(slug))})
    return identity, slug


def profile_data(agent_id, name, scope, *, role="business-agent", revision=1, overrides=None, brief="Synthetic assignment."):
    return {"schema_version": 2, "agent_id": agent_id, "profile_revision": revision, "name": name, "display_name": name.title(),
            "status": "active", "runtime": "claude-code", "role": {"id": role, **ROLE}, "scope": scope,
            "assignment_brief": brief, "plugins": [], "tool_policy": {"overrides": overrides or {}}, "presentation": {},
            "created_at": "2026-10-14T00:00:00Z"}


def register(world, *, home=None, role="global-worker", scope=None, **options):
    """A registered active agent whose stored profile is canonical for its scope: by default a supported global (ecosystem
    scope, no home). ``home`` ``(id, slug)`` stores a historical business-home row, which never operates."""
    agent_id = new_id()
    name = "agent-" + agent_id[:8]
    home_id, slug = home if home else (None, None)
    scope = scope or ({"kind": "business", "business_id": home_id} if home_id else global_scope(capabilities=("read_global_context",)))
    data = profile_data(agent_id, name, scope, role=role, **options)
    with world.database.transaction() as uow:
        uow.repository("principals").insert({"id": agent_id, "kind": "agent", "display_name": name})
        uow.connection.execute(
            "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, status,"
            " profile_json, revision, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (agent_id, agent_id, home_id, home_id or "ecosystem", name,
             str(agent_workspace_of(world.paths, agent_id, home_id, slug)), "active", json.dumps(data),
             data["profile_revision"], utc_now(), utc_now()))
    return Principal(agent_id, "agent", name), data


def global_scope(*businesses, capabilities=()):
    return {"kind": "ecosystem", "allowed_business_ids": sorted(businesses), "global_capabilities": sorted(capabilities)}


def active(world):
    with world.database.read() as uow:
        return sorted(row[0] for row in uow.connection.execute("SELECT id FROM businesses WHERE archived_at IS NULL"))


def effective(world, data):
    """The stored profile as ``profile`` serves it now: its scope lists every active business, never the saved list."""
    return {**data, "scope": {**data["scope"], "allowed_business_ids": active(world)}}


def store(world, principal, *, profile=None, text=None, **columns):
    """Replace the stored profile text and/or indexed columns, as a concurrent writer committing between two reads."""
    values = dict(columns)
    if profile is not None or text is not None:
        values["profile_json"] = text if text is not None else json.dumps(profile)
    with world.database.transaction() as uow:
        uow.connection.execute(f"UPDATE agent_profiles SET {', '.join(f'{name}=?' for name in values)} WHERE agent_id=?",
                               (*values.values(), principal.id))


def refused(resolver, principal, code):
    with pytest.raises(DomainError) as error:
        resolver.profile(principal)
    assert error.value.code == code, error.value.message


def test_an_identical_payload_validates_once_on_a_warm_frozen_registry(world, resolver, parses):
    agent, data = register(world)
    first, row = resolver.profile(agent)
    assert len(parses) == 1 and parses[0] is resolver.registry
    for _ in range(5):
        again, again_row = resolver.profile(agent)
        assert again == first                             # a fresh effective-scope projection of the one reused parse
        assert again_row == row and again_row is not row  # the indexed row is read afresh, never reused
    assert len(parses) == 1
    assert first.to_dict() == effective(world, data)


def test_nothing_is_cached_while_assembly_can_still_change_the_registry(world, parses):
    registry = builtin_registry()
    resolver = AgentConfigResolver(world.database, world.paths, registry, catalog=SimpleNamespace())
    agent, _ = register(world)
    assert not registry.frozen
    first = resolver.profile(agent)[0]
    second = resolver.profile(agent)[0]
    assert len(parses) == 2 and first is not second and first == second
    registry.freeze()
    warm = resolver.profile(agent)[0]
    assert len(parses) == 3
    assert resolver.profile(agent)[0] == warm
    assert len(parses) == 3


def test_a_registry_that_does_not_declare_itself_frozen_is_never_cached_against(world, parses):
    resolver = AgentConfigResolver(world.database, world.paths, SimpleNamespace(), catalog=SimpleNamespace())
    agent, _ = register(world)
    resolver.profile(agent)
    resolver.profile(agent)
    assert len(parses) == 2 and not resolver._profiles


def test_changed_json_or_revision_misses_the_cache(world, resolver, parses):
    agent, data = register(world)
    assert resolver.profile(agent)[0].revision == 1
    # A new revision committed in both the canonical text and the index.
    data2 = {**data, "profile_revision": 2, "assignment_brief": "Second assignment."}
    store(world, agent, profile=data2, revision=2)
    second = resolver.profile(agent)[0]
    assert second.revision == 2 and second.data["assignment_brief"] == "Second assignment."
    assert len(parses) == 2
    # The same revision with different content is a different payload, not a hit.
    data3 = {**data2, "assignment_brief": "Edited without a revision change."}
    store(world, agent, profile=data3)
    third = resolver.profile(agent)[0]
    assert third.data["assignment_brief"] == "Edited without a revision change." and len(parses) == 3
    # Equal meaning with different bytes is also a miss: the key is the exact stored text.
    store(world, agent, text=json.dumps(data3, indent=2, sort_keys=True))
    fourth = resolver.profile(agent)[0]
    assert fourth == third and fourth is not third and len(parses) == 4


def test_a_malformed_same_revision_replacement_is_rejected_and_never_cached(world, resolver, parses):
    agent, data = register(world, revision=3)
    original = resolver.profile(agent)[0]
    broken = json.dumps({key: value for key, value in data.items() if key != "assignment_brief"})
    store(world, agent, text=broken)
    refused(resolver, agent, "invalid_profile")
    refused(resolver, agent, "invalid_profile")
    assert len(parses) == 3  # one valid parse, then the invalid text is re-validated every time
    for replacement in ({**data, "status": "paused"}, {**data, "scope": {"kind": "business"}},
                        {**data, "tool_policy": {"overrides": {"no_such_operation": "allow"}}}):
        store(world, agent, profile=replacement)
        with pytest.raises(DomainError):
            resolver.profile(agent)
    store(world, agent, text="[]")
    refused(resolver, agent, "invalid_profile")
    # Restoring the original bytes is served by the entry the failures never displaced.
    store(world, agent, profile=data)
    before = len(parses)
    assert resolver.profile(agent)[0] == original and len(parses) == before


def test_revision_and_denormalized_fields_are_checked_against_a_cached_profile(world, resolver, parses):
    other = business(world)
    agent, data = register(world)
    cached = resolver.profile(agent)[0]
    for column, value in (("revision", 2), ("name", "renamed-in-index"), ("workspace", "/not/the/registered/workspace")):
        row = resolver.profile(agent)[1]
        store(world, agent, **{column: value})
        refused(resolver, agent, "registration_conflict" if column == "workspace" else "profile_conflict")
        store(world, agent, **{column: row[column]})
        assert resolver.profile(agent)[0] == cached
    store(world, agent, home_business_id=other[0], namespace=other[0])   # a business home is historical: never operates
    refused(resolver, agent, "inactive_agent")
    assert len(parses) == 1


def test_a_stored_profile_naming_another_agent_is_refused_even_when_that_agent_is_cached(world, resolver, parses):
    first, first_data = register(world)
    second, _ = register(world)
    resolver.profile(first)
    swapped = dict(first_data)
    store(world, second, profile=swapped)
    refused(resolver, second, "profile_conflict")
    assert len(parses) == 2  # the same bytes under another principal are a separate entry, never a shared one
    assert resolver.profile(first)[0].agent_id == first.id


def test_deactivation_and_a_missing_registration_apply_to_the_next_read(world, resolver):
    agent, _ = register(world)
    resolver.profile(agent)
    with world.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (agent.id,))
    refused(resolver, agent, "inactive_agent")
    with world.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (agent.id,))
    resolver.profile(agent)
    store(world, agent, status="inactive")
    refused(resolver, agent, "inactive_agent")
    store(world, agent, status="active")
    resolver.profile(agent)
    with world.database.transaction() as uow:
        uow.connection.execute("DELETE FROM agent_profiles WHERE agent_id=?", (agent.id,))
    refused(resolver, agent, "inactive_agent")
    with pytest.raises(DomainError):
        resolver.profile(Principal(agent.id, "human"))


def test_a_role_retired_after_caching_stops_operating_on_the_next_read(world, resolver, parses, monkeypatch):
    agent, data = register(world)
    cached = resolver.profile(agent)[0]
    with monkeypatch.context() as retirement:
        retirement.setattr(resolver_module, "RETIRED_ROLE_IDS", frozenset({"global-worker"}))
        refused(resolver, agent, "inactive_agent")
    assert resolver.profile(agent)[0] == cached
    assert len(parses) == 1
    # A stored profile that moves to a retired role is a different payload and refused as well.
    store(world, agent, profile={**data, "role": {"id": "coder", **ROLE}})
    refused(resolver, agent, "inactive_agent")
    assert len(parses) == 2


def test_a_business_home_row_is_historical_and_refused_before_any_parse(world, resolver, parses):
    agent, _ = register(world, home=business(world), role="business-agent")
    refused(resolver, agent, "inactive_agent")
    refused(resolver, agent, "inactive_agent")
    assert parses == [] and not resolver._profiles


def test_archiving_a_business_applies_to_the_next_read(world, resolver, parses):
    agent, _ = register(world)
    archived = business(world)[0]
    assert archived in resolver.profile(agent)[0].scope.allowed_business_ids
    with world.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), archived))
    assert archived not in resolver.profile(agent)[0].scope.allowed_business_ids
    with world.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=NULL WHERE id=?", (archived,))
    assert archived in resolver.profile(agent)[0].scope.allowed_business_ids and len(parses) == 1


def test_a_new_business_is_reachable_on_the_next_read_without_a_profile_change(world, resolver, parses):
    agent, data = register(world, role="ordubashy")
    cached = resolver.profile(agent)[0]
    added = business(world)
    current = resolver.profile(agent)[0]
    assert added[0] in current.scope.allowed_business_ids and added[0] not in cached.scope.allowed_business_ids
    assert current.revision == cached.revision == 1 and len(parses) == 1


def test_a_saved_business_list_is_not_authority_and_archiving_ends_access_at_the_context_refresh(world, resolver):
    kept, dropped = business(world), business(world)
    agent, data = register(world, role="global-worker", scope=global_scope(kept[0], dropped[0], capabilities=("read_global_context",)))
    context = InvocationContext(agent, business_id=dropped[0], home_business_id=None,
                                allowed_business_ids=frozenset({kept[0], dropped[0]}), source="mcp")
    with world.database.read() as uow:
        assert resolver.refresh_invocation_context(context, uow).business_id == dropped[0]
    # Narrowing the saved list changes nothing: every active business stays reachable.
    store(world, agent, profile={**data, "profile_revision": 2, "scope": global_scope(kept[0], capabilities=("read_global_context",))}, revision=2)
    with world.database.read() as uow:
        assert resolver.refresh_invocation_context(context, uow).business_id == dropped[0]
    with world.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at=? WHERE id=?", (utc_now(), dropped[0]))
    with world.database.read() as uow, pytest.raises(DomainError) as error:
        resolver.refresh_invocation_context(context, uow)
    assert error.value.code == "business_archived"
    with world.database.read() as uow:
        refreshed = resolver.refresh_invocation_context(replace(context, business_id=kept[0]), uow)
    assert refreshed.profile_revision == 2 and kept[0] in refreshed.allowed_business_ids
    assert dropped[0] not in refreshed.allowed_business_ids


def test_registry_identity_change_discards_entries_and_validates_against_the_new_registry(world, resolver, parses):
    agent, data = register(world, overrides={"tasks_list": "allow"})
    registry = resolver.registry
    cached = resolver.profile(agent)[0]
    assert resolver.profile(agent)[0] == cached and len(parses) == 1
    replacement = builtin_registry().freeze()
    resolver.registry = replacement
    rebuilt = resolver.profile(agent)[0]
    assert rebuilt is not cached and rebuilt == cached
    assert len(parses) == 2 and parses[-1] is replacement
    # An entry validated under the old registry must not answer for one that does not know the operation.
    resolver.registry = OperationRegistry().freeze()
    with pytest.raises(DomainError):
        resolver.profile(agent)
    resolver.registry = registry
    assert resolver.profile(agent)[0] is not cached
    assert len(parses) == 4


def test_the_cache_is_bounded_and_evicts_the_least_recently_used(world, resolver, parses):
    agent, data = register(world)
    texts = [json.dumps({**data, "assignment_brief": f"Variant {index}"}) for index in range(PROFILE_CACHE_LIMIT + 1)]
    assert PROFILE_CACHE_LIMIT == 128
    parsed = [resolver._parsed_profile(agent.id, text) for text in texts[:PROFILE_CACHE_LIMIT]]
    assert len(resolver._profiles) == PROFILE_CACHE_LIMIT and len(parses) == PROFILE_CACHE_LIMIT
    assert resolver._parsed_profile(agent.id, texts[0]) is parsed[0]  # refreshes the oldest entry
    resolver._parsed_profile(agent.id, texts[PROFILE_CACHE_LIMIT])  # evicts texts[1], the least recently used
    assert len(resolver._profiles) == PROFILE_CACHE_LIMIT
    before = len(parses)
    assert resolver._parsed_profile(agent.id, texts[0]) is parsed[0] and len(parses) == before
    assert resolver._parsed_profile(agent.id, texts[1]) is not parsed[1] and len(parses) == before + 1
    assert resolver._parsed_profile(agent.id, texts[1]).data["assignment_brief"] == "Variant 1"
    assert len(resolver._profiles) == PROFILE_CACHE_LIMIT


def test_returned_profiles_are_immutable_and_current_reads_cannot_be_corrupted(world, resolver):
    agent, data = register(world)
    profile, row = resolver.profile(agent)
    assert isinstance(profile.data, MappingProxyType) and isinstance(profile.data["plugins"], tuple)
    assert isinstance(profile.data["role"], MappingProxyType)
    with pytest.raises(TypeError):
        profile.data["name"] = "tampered"
    with pytest.raises(TypeError):
        profile.data["tool_policy"]["overrides"]["tasks_list"] = "allow"
    with pytest.raises(FrozenInstanceError):
        profile.data = {}
    exported = profile.to_dict()
    exported["name"] = "changed"
    exported["tool_policy"]["overrides"]["tasks_list"] = "allow"
    row["name"] = "changed"
    again, again_row = resolver.profile(agent)
    assert again == profile and again.to_dict() == effective(world, data) and again_row["name"] == data["name"]


def test_concurrent_readers_share_one_consistent_result(world, resolver, parses):
    agent, data = register(world)
    threads, rounds = 8, 25
    barrier, results, failures = threading.Barrier(threads), [], []

    def read():
        try:
            barrier.wait()
            results.extend(resolver.profile(agent)[0] for _ in range(rounds))
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            failures.append(exc)

    workers = [threading.Thread(target=read) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert not failures and len(results) == threads * rounds
    served = effective(world, data)
    assert all(item.to_dict() == served for item in results)
    # Concurrent cold misses may each validate once; afterwards the entry is shared.
    assert 1 <= len(parses) <= threads
    settled = resolver.profile(agent)[0]
    count = len(parses)
    assert resolver.profile(agent)[0] == settled and len(parses) == count


def test_concurrent_eviction_keeps_the_cache_bounded(world, resolver):
    agent, data = register(world)
    texts = [json.dumps({**data, "assignment_brief": f"Variant {index}"}) for index in range(PROFILE_CACHE_LIMIT * 2)]
    threads, barrier, failures = 6, threading.Barrier(6), []

    def read(offset):
        try:
            barrier.wait()
            for index in range(len(texts)):
                text = texts[(index + offset * 17) % len(texts)]
                assert resolver._parsed_profile(agent.id, text).data["assignment_brief"] == json.loads(text)["assignment_brief"]
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    workers = [threading.Thread(target=read, args=(number,)) for number in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert not failures
    assert len(resolver._profiles) == PROFILE_CACHE_LIMIT


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """The assembled host with its registry frozen and the registered global Ordubashy (needs the role-source workspace)."""
    from host_foundation_support import build_instance, create_registered_global, register_sources

    instance, ids = build_instance(tmp_path_factory.mktemp("profile-cache-host"))
    business_id = ids["first"]
    register_sources(instance, business_id, "first")
    agent = create_registered_global(instance, "ordubashy")
    instance.runtime.freeze_registry()
    return instance, business_id, agent


def test_the_serving_path_validates_a_stored_profile_once_and_sees_a_permission_change_at_once(host, parses):
    from host_foundation_support import agent_context, set_agent_overrides
    from qorqut_operations.contracts import InvocationOptions

    instance, business_id, agent = host
    assert instance.runtime.resolver.registry is instance.runtime.registry and instance.runtime.registry.frozen

    def read():
        return instance.runtime.application.invoke(agent_context(agent, business_id), "tasks_list", {"scope": "business"},
                                                   InvocationOptions(idempotency_key=new_id()))

    assert read().status == "ok"
    warm = len(parses)
    assert warm >= 1
    for _ in range(3):
        assert read().status == "ok"
    assert len(parses) == warm  # three more complete invocations, each reading the profile several times, validate nothing
    set_agent_overrides(instance, agent, {"tasks_list": "deny"}, {business_id})
    changed = len(parses)
    denied = read()
    assert denied.status == "error" and denied.error["code"] == "policy_denied", denied.to_dict()
    after = len(parses)
    assert after - changed == 1  # the replaced profile is validated once; its policy applies on the very next read
    assert read().error["code"] == "policy_denied" and len(parses) == after
