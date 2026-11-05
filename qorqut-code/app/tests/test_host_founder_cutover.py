"""Project creation on the composed host after the Founder, Ideas, Setup and Strategy retirements.

Contract: Founder is retired and no longer authorizes anything, there is no Ideas record or tool and there is no business setup
or strategy authoring (``manage_ideas``, ``manage_business_setup`` and ``write_business_strategy`` are retired metadata that
authorize nothing). Project creation (``business_create``) is open only to the one configured, active Ordubashy holding
``coordinate_businesses``; the owner asks Ordubashy, and a worker or the Learning Applier never create, whatever they hold. An
agent acts as itself; an owner call acts as the owner. The separate Personal Context is retired.

Real: the stock host assembly, the owner's catalog publication, ``agents_create``/``agents_update`` (every grant), the real
Ordubashy and worker roles of ``QORQUT_TEST_ROLE_ROOT``, ``business_create``, ``tasks_*``, notes, the resolver,
preparation and the native Chat store, and a disposable real Context. Synthetic: the tmp instance, the model runner (it records,
never answers) and the explicitly HISTORICAL retained Founder rows (``historical_founder``) seeded only for rejection. No agent,
provider, model, network or operational data is used."""

import json

import pytest

pytest.importorskip("qorqut_context")
pytest.importorskip("qorqut_context.integration.filing")

from qorqut_operations.contracts import DomainError, Principal, new_id, plain

from bazarlyk.bootstrap.preparation import prepare_agent
from bazarlyk.domains.business_creation.access import COORDINATION_CAPABILITY, creator_actor
from bazarlyk.native_chat import store as native_store
from context_completion_support import build
from host_foundation_support import (create_worker, grant_capabilities, historical_founder,
                                     set_agent_overrides)

VERSIONS = {"codex": "codex-cli 0.156.1", "claude-code": "2.1.263 (Claude Code)"}
CREATING = ("business_create",)
COORDINATING = ("read_global_context", COORDINATION_CAPABILITY)


@pytest.fixture
def world(tmp_path):
    host = build(tmp_path, businesses=("first",))
    instance = host.instance
    host.ordubashy = host.coordinator
    host.granted = create_worker(instance, "granted", capabilities=COORDINATING)       # a worker may hold the capability; it never creates
    host.plain = create_worker(instance, "plain")                              # neither capability
    for identity in (host.ordubashy, host.granted, host.plain):                # ordinary policy never decides these cases
        set_agent_overrides(instance, identity, {name: "allow" for name in CREATING}, list(host.ids.values()))
    yield host


def acting(host, identity, business=None):
    """The agent's own invocation context, selecting a business only when one is named."""
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def create_args(slug):
    return {"slug": slug, "name": slug.title(), "summary": "Help busy repair shops handle arrivals."}


def code(result):
    assert result.status == "error", plain(result.data)
    return result.error["code"], result.error.get("message", "")


# ---- eligibility ---------------------------------------------------------------------------------------------------------


def test_creation_eligibility_is_only_the_configured_ordubashy_holding_the_capability(world):
    host = world
    with host.database.read() as uow:
        for identity in (host.granted, host.plain, host.ordubashy, host.instance.owner, new_id()):
            assert creator_actor(uow, identity) is None, identity            # the configured Ordubashy does not hold it yet
    grant_capabilities(host.instance, host.ordubashy, COORDINATION_CAPABILITY)
    with host.database.read() as uow:
        assert creator_actor(uow, host.ordubashy) == "ordubashy"
        assert creator_actor(uow, host.granted) is None and creator_actor(uow, host.plain) is None
    revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", host.granted)[0]["revision"]
    host.data(host.human(None), "agents_deactivate", {"agent_id": host.granted}, expected_revision=revision)
    with host.database.read() as uow:
        assert creator_actor(uow, host.granted) is None


# ---- creation ------------------------------------------------------------------------------------------------------------


def test_no_worker_creates_a_project_whatever_it_holds_and_ordubashy_needs_its_capability(world):
    host = world
    for identity in (host.plain, host.granted):
        refused = host.call(acting(host, identity), "business_create", create_args("nope"))
        assert code(refused)[0] in {"forbidden", "policy_denied"}, identity
    assert host.count("businesses", "slug='nope'") == 0
    assert code(host.call(acting(host, host.ordubashy), "business_create", create_args("nope")))[0] in {"forbidden", "policy_denied"}
    assert code(host.call(host.human(None), "business_create", create_args("nope")))[0] == "forbidden"     # the owner asks Ordubashy
    grant_capabilities(host.instance, host.ordubashy, COORDINATION_CAPABILITY)
    created = host.call(acting(host, host.ordubashy), "business_create", create_args("ordubashy-project"))
    assert created.status == "ok", (created.status, created.error)
    assert host.count("businesses", "slug='ordubashy-project'") == 1


# ---- retired capabilities and the retired Personal Context ------------------------------------------------------------


def test_the_retired_capability_names_authorize_no_creation(world):
    host = world
    with host.database.transaction() as uow:           # synthetic: an unmigrated stored profile that still names the retired capabilities
        stored = json.loads(uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (host.plain,)).fetchone()[0])
        stored["scope"]["global_capabilities"] = sorted({*stored["scope"]["global_capabilities"], "manage_ideas", "manage_business_setup",
                                                                  "write_business_strategy"})
        uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (json.dumps(stored), host.plain))
    assert code(host.call(acting(host, host.plain), "business_create", create_args("nope")))[0] in {"forbidden", "policy_denied"}
    assert host.count("businesses", "slug='nope'") == 0
    with host.database.read() as uow:
        assert creator_actor(uow, host.plain) is None


def test_the_separate_personal_context_is_retired_and_its_grant_names_authorize_nothing(world):
    host = world
    grant_capabilities(host.instance, host.ordubashy, COORDINATION_CAPABILITY)
    for name in ("personal_context_get", "personal_context_list", "personal_context_create", "personal_context_update",
                 "personal_context_archive"):
        assert not host.runtime.registry.known(name)
        assert host.call(acting(host, host.ordubashy), name, {}).status == "error"
        assert host.call(host.human(None), name, {}).status == "error"


# ---- the retired Founder role ------------------------------------------------------------------------------------------


def pin_of_founder():
    return {"id": "founder", "version": "2.6.0", "digest": "sha256:" + "0" * 64}


def test_nothing_creates_or_switches_to_the_retired_founder_role(world):
    host = world
    owner = host.human(None)
    arguments = {"role": pin_of_founder(), "name": "spawn", "assignment_brief": "No.", "plugins": [], "display_name": "Spawn",
                 "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]}}
    assert code(host.call(owner, "agents_create", arguments))[0] == "retired_role"
    with host.database.read() as uow:
        revision = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (host.plain,)).fetchone()[0]
    switched = host.call(owner, "agents_update", {"agent_id": host.plain, "changes": {"role": pin_of_founder()}}, expected_revision=revision)
    assert code(switched)[0] == "retired_role"
    assert host.count("agent_profiles", "json_extract(profile_json,'$.role.id')='founder'") == 0


def test_a_stored_active_founder_never_authenticates_prepares_or_receives_delivery(world):
    host = world
    founder = historical_founder(host.instance, retained=True)                     # stored active, with its artifact still retained
    resolver, owner = host.runtime.resolver, host.human(None)
    with pytest.raises(DomainError) as refused:
        resolver.profile(Principal(founder, "agent"))
    assert refused.value.code == "inactive_agent"
    with pytest.raises(DomainError):
        acting(host, founder)                                                       # no invocation context is ever built
    with pytest.raises(DomainError) as prepared:
        prepare_agent(resolver, Principal(founder, "agent"), endpoint="http://127.0.0.1:9988", versions=VERSIONS)
    assert prepared.value.code in {"inactive_agent", "retired_role"}
    with host.database.read() as uow:
        # Whatever capabilities its stored profile holds, the retired role is never a creator.
        assert creator_actor(uow, founder) is None
        assert native_store.active_profile(uow.connection, founder, host.paths) is None                    # no session starts
        assert native_store.active_profile(uow.connection, founder, host.paths, any_status=True)["agent_id"] == founder
        assert founder not in {row["agent_id"] for row in native_store.observed_agents(uow.connection, host.paths)}
    # Messages are never delivered to it.
    sent = host.call(owner, "agents_message_send", {"scope": "global", "to_agent_id": founder, "message": "Hello"})
    assert sent.status == "error"


def test_the_canonical_deactivation_still_closes_the_retained_founder(world):
    host = world
    founder = historical_founder(host.instance, retained=True)
    revision = host.query("SELECT revision FROM agent_profiles WHERE agent_id=?", founder)[0]["revision"]
    host.data(host.human(None), "agents_deactivate", {"agent_id": founder}, expected_revision=revision)
    row = host.query("SELECT status, json_extract(profile_json,'$.status') AS stored FROM agent_profiles WHERE agent_id=?", founder)[0]
    assert (row["status"], row["stored"]) == ("inactive", "inactive")
    assert host.query("SELECT active FROM principals WHERE id=?", founder)[0]["active"] == 0
    # The historical record stays readable, with the workspace mapping the registration used.
    assert host.query("SELECT profile_json FROM agent_profiles WHERE agent_id=?", founder)[0]["profile_json"]["role"]["id"] == "founder"
