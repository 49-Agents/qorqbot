"""Project creation (``business_create``) makes one empty project: its row, its memberships and an empty ``hq/`` + ``code/`` folder.

Real: the stock host assembly and ``build_runtime``, the registered Ordubashy coordinator and synthetic global workers
through ``agents_create``/``agents_update``/``permissions_set``, ``business_create`` (policy, Signoff review, the durable file
executor with its directory effect, the plan's validation and application) and the ordinary Task, note, table and routine
operations in the new project. Faked: only the clock and the synthetic principals, rows and files under a pytest
``tmp_path``. These checks run the source trees on ``PYTHONPATH``: they are not installed-application proof and they start no
user instance, provider or model."""

import asyncio
import dataclasses
import json
import os
import threading

import pytest

pytest.importorskip("qorqut_context")

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain

from bazarlyk.agents.models import GLOBAL_CAPABILITIES, RETIRED_GLOBAL_CAPABILITIES
from qorqut_operations.projects.creation.creation import (ACTION, DIRECTORIES, LAYOUT, derive_slug, numbered)
from bazarlyk.instance_paths import PROJECTS_DIRECTORY
from bazarlyk.transport.auth import AuthenticatedPrincipal, OriginPolicy
from bazarlyk.transport.mcp import MCPTransport
from context_completion_support import build
from context_source_status_support import Http, ORIGIN
from host_foundation_support import create_worker, grant_capabilities, revoke_capabilities, set_agent_overrides

RETIRED = "manage_business_setup"


class Crash(BaseException):
    """A simulated process crash: nothing in the application's failure handling runs for it."""


def creating(tmp_path, **options):
    """The stock host with the registered Ordubashy coordinator (granted ``coordinate_businesses`` and the ``business_create``
    operation grant) and two synthetic global workers: one explicitly allowed the operation, one also holding the retired setup
    capability as stored history."""
    host = build(tmp_path, businesses=("first",), **options)
    host.ordubashy = host.coordinator
    grant_capabilities(host.instance, host.ordubashy, "coordinate_businesses")
    host.worker = create_worker(host.instance, "worker")
    host.setup_holder = create_worker(host.instance, "setupholder")
    for identity in (host.ordubashy, host.worker, host.setup_holder):
        set_agent_overrides(host.instance, identity, {"business_create": "allow"}, list(host.ids.values()))
    hold_retired(host, host.setup_holder)                                             # stored history only; a profile edit would refuse it
    return host


def hold_retired(host, identity):
    """Store the retired setup capability on a profile exactly as an older installation did (it cannot be granted anew)."""
    with host.database.transaction() as uow:
        uow.connection.execute(
            "UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.scope.global_capabilities',"
            "json_insert(json_extract(profile_json,'$.scope.global_capabilities'),'$[#]',?)) WHERE agent_id=?", (RETIRED, identity))


def acting(host, identity, business=None):
    return host.runtime.resolver.invocation_context(Principal(identity, "agent"), business_id=business)


def ordubashy(host):
    """A fresh context of the one configured Ordubashy, the only principal that may create a project."""
    return acting(host, host.ordubashy)


def create(host, ctx, name="Repair Shop", key=None, **extra):
    return host.call(ctx, "business_create", {"name": name, **extra}, key=key)


def made(host, ctx, name="Repair Shop", **extra):
    result = create(host, ctx, name, **extra)
    assert result.status == "ok", (result.status, result.error)
    return plain(result.data)


def code(result):
    assert result.status == "error", plain(result.data)
    return result.error["code"]


def folder(host, slug):
    return host.paths.operations / PROJECTS_DIRECTORY / slug


def tree(root):
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) if root.exists() else None


def only_the_empty_project(host, slug):
    root = folder(host, slug)
    assert tree(root) == ["code", "hq"]
    assert all(path.is_dir() and not path.is_symlink() and not any(path.iterdir()) for path in root.iterdir())
    assert not any((root / name).exists() for name in ("README.md", "business.yaml", "agents", ".git"))


def records_of(host, business):
    """Every table row naming the new project, by table: creation may leave only its row's memberships behind."""
    found = {}
    with host.database.read() as uow:
        tables = [row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [row[1] for row in uow.connection.execute(f'PRAGMA table_info("{table}")')]
            if "business_id" in columns:
                count = uow.connection.execute(f'SELECT COUNT(*) FROM "{table}" WHERE business_id=?', (business,)).fetchone()[0]
                if count:
                    found[table] = count
    return found


def system_programs(host):
    return [row["id"] for row in host.query("SELECT id FROM principals WHERE kind='system' AND active=1")]


# -- who may create, and exactly what -------------------------------------------------------------------------------------

def test_the_configured_ordubashy_creates_an_empty_project_from_a_name_alone_and_the_owner_keeps_it(tmp_path):
    host = creating(tmp_path)
    data = made(host, ordubashy(host))
    assert set(data) == {"business", "business_id", "slug"} and data["slug"] == "repair-shop"
    business = data["business"]
    assert set(business) == {"id", "slug", "name", "summary", "workspace_path", "hq_path"}
    assert business["id"] == data["business_id"] and business["name"] == "Repair Shop" and business["summary"] == ""
    assert business["workspace_path"] == str(folder(host, "repair-shop")) == str(host.paths.business_root("repair-shop"))
    assert business["hq_path"] == str(host.paths.business_hq("repair-shop"))
    only_the_empty_project(host, "repair-shop")
    row = host.query("SELECT * FROM businesses WHERE id=?", data["business_id"])[0]
    assert (row["slug"], row["workspace"], row["hq_path"], row["stage"], row["timezone"]) == (
        "repair-shop", business["workspace_path"], business["hq_path"], "idea", "UTC")
    assert row["config_json"] == {"one_liner": "", "code": []}
    members = host.query("SELECT principal_id, is_owner FROM business_memberships WHERE business_id=?", data["business_id"])
    programs = {identity for identity in system_programs(host) if host.runtime.authority.programs.get}
    assert {(member["principal_id"], member["is_owner"]) for member in members if member["is_owner"]} == {(host.owner, 1)}
    assert {member["principal_id"] for member in members} <= {host.owner, *programs}
    assert records_of(host, data["business_id"]).keys() <= {"business_memberships", "inbox_changes"}
    assert host.query("SELECT is_owner FROM business_memberships WHERE business_id=? AND principal_id=?", data["business_id"], host.ordubashy) == []


def test_the_configured_ordubashy_creates_with_a_description_and_a_chosen_slug(tmp_path):
    host = creating(tmp_path)
    data = made(host, ordubashy(host), "Atelier", summary="Custom frames.", slug="frames-co")
    assert data["slug"] == "frames-co" and data["business"]["summary"] == "Custom frames."
    only_the_empty_project(host, "frames-co")
    assert host.query("SELECT config_json FROM businesses WHERE slug='frames-co'")[0]["config_json"]["one_liner"] == "Custom frames."
    assert records_of(host, data["business_id"]).keys() <= {"business_memberships", "inbox_changes"}


def fingerprint(host):
    """Every table's rows and the instance's files: a refused creation must leave both exactly as they were."""
    with host.database.read() as uow:
        tables = sorted(row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'"))
        rows = {table: sorted(repr(tuple(row)) for row in uow.connection.execute(f'SELECT * FROM "{table}"')) for table in tables}
    files = {str(root): tree(root) for root in (host.paths.operations, host.paths.runtime)}
    return rows, files


def test_nobody_else_creates_even_with_an_old_setup_grant_or_an_explicit_allow(tmp_path):
    host = creating(tmp_path)
    other = host.agent_id("learning-applier")                                        # another configured global role
    set_agent_overrides(host.instance, other, {"business_create": "allow"}, list(host.ids.values()))
    grant_capabilities(host.instance, host.worker, "coordinate_businesses")          # coordination alone is not the identity check
    grant_capabilities(host.instance, other, "coordinate_businesses")
    held = acting(host, host.ordubashy)
    revoke_capabilities(host.instance, host.ordubashy, "coordinate_businesses")      # the identity alone is not the capability
    before = fingerprint(host)
    for identity in (host.worker, host.setup_holder, other):
        assert code(create(host, acting(host, identity), "Nope")) == "forbidden", identity
    owner = host.human(None)
    assert code(create(host, owner, "Nope")) == "forbidden"                          # the installation owner asks Ordubashy
    assert code(create(host, host.human("first"), "Nope")) == "forbidden"
    stranger = InvocationContext(Principal(new_id(), "human"), allowed_business_ids=frozenset(host.ids.values()),
                                 global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")
    assert code(create(host, stranger, "Nope")) in {"forbidden", "invalid_principal", "policy_denied"}
    assert code(create(host, acting(host, host.ordubashy), "Nope")) == "forbidden"
    assert code(create(host, held, "Nope")) == "forbidden"                            # nor does a context resolved before the revocation
    for program in system_programs(host):
        system = InvocationContext(Principal(program, "system"), source="worker")
        result = host.runtime.application.invoke(system, "business_create", {"name": "Nope"}, InvocationOptions(idempotency_key=new_id()))
        assert code(result) in {"forbidden", "invalid_principal", "policy_denied"}
    for context in (acting(host, host.worker), owner, host.human("first"), stranger,
                    InvocationContext(Principal(system_programs(host)[0], "system"), source="worker")):
        with host.database.read() as uow, pytest.raises(DomainError) as refused:        # and the hard identity check holds on its own
            host.runtime.domains.business_creation.access(context, uow)
        assert refused.value.code == "forbidden"
    assert host.count("businesses", "slug<>'first'") == 0
    assert sorted(entry.name for entry in (host.paths.operations / PROJECTS_DIRECTORY).iterdir()) == ["first"]    # only the fixture project
    assert fingerprint(host) == before


def test_the_owner_is_refused_over_the_real_http_endpoint_and_by_direct_calls_with_nothing_changed(tmp_path):
    host = creating(tmp_path)
    http, owner = Http(host), host.human(None)
    before = fingerprint(host)
    status, value = http.operation("business_create", {"name": "Owner Project", "idempotency_key": new_id()})
    assert value["status"] == "error" and value["error"]["code"] == "forbidden", (status, value)
    assert "ask Ordubashy" in value["error"]["message"]
    status, value = http.operation("business_create", {"name": "Owner Project", "slug": "owner-project", "idempotency_key": new_id()},
                                   business="first")                                  # a project selection never helps either
    assert value["status"] == "error" and value["error"]["code"] == "forbidden", (status, value)
    refused = create(host, owner, "Owner Project")                                    # the same direct application call
    assert code(refused) == "forbidden" and "ask Ordubashy" in refused.error["message"]
    descriptor = host.runtime.registry.get("business_create")                         # forged lower-level entry points
    with host.database.read() as uow, pytest.raises(DomainError) as denied:
        descriptor.prepare(owner, {"name": "Owner Project"}, InvocationOptions(idempotency_key=new_id()), uow)
    assert denied.value.code == "forbidden"
    plan, _ = prepared(host, ordubashy(host), "Owner Project", slug="owner-project")   # a plan Ordubashy prepared, presented by the owner
    with pytest.raises(DomainError) as denied:
        validated(host, owner, plan)
    assert denied.value.code == "forbidden"
    with host.database.transaction() as uow, pytest.raises(DomainError) as denied:
        host.runtime.domains.business_creation.apply(owner, plan, uow)
    assert denied.value.code == "forbidden"
    assert fingerprint(host) == before
    assert host.count("businesses", "slug='owner-project'") == 0 and not folder(host, "owner-project").exists()


def test_the_configured_ordubashy_creates_through_http_and_mcp_and_the_owner_keeps_ordinary_authority(tmp_path):
    host = creating(tmp_path)
    http = Http(host)
    mcp = MCPTransport(host.runtime, http.authentication, OriginPolicy([ORIGIN]))
    actor = AuthenticatedPrincipal(Principal(host.ordubashy, "agent"))                 # synthetic authenticated actor; the rest is real
    sent = {"name": "MCP Project", "summary": "Through MCP.", "idempotency_key": new_id()}
    value = asyncio.run(mcp._single_result("business_create", sent, actor=actor))
    assert value["status"] == "ok" and value["data"]["slug"] == "mcp-project", value
    assert asyncio.run(mcp._single_result("business_create", sent, actor=actor))["data"] == value["data"]    # replay: the same project
    assert host.count("businesses", "slug='mcp-project'") == 1
    only_the_empty_project(host, "mcp-project")
    other = AuthenticatedPrincipal(Principal(host.worker, "agent"))
    assert asyncio.run(mcp._single_result("business_create", {"name": "Worker", "idempotency_key": new_id()}, actor=other))["error"]["code"] == "forbidden"
    assert asyncio.run(mcp._single_result("business_create", sent, actor=other))["status"] == "error"    # a replay by another caller returns no project
    project = value["data"]["business_id"]                                            # ordinary owner authority in the created project
    members = {row["principal_id"]: row["is_owner"] for row in host.query("SELECT principal_id, is_owner FROM business_memberships WHERE business_id=?", project)}
    assert members[host.owner] == 1 and host.ordubashy not in members
    host.ids["mcp"] = project
    owner = host.human("mcp")
    task = host.data(owner, "tasks_create", {"title": "Owner task"})["task"]
    assert host.data(owner, "tasks_get", {"task_id": task["id"]})["task"]["business_id"] == project
    note = host.data(owner, "notes_create", {"title": "Plan", "body": "Start.", "target_business_id": project})["document"]
    assert note["id"]
    archive = host.call(host.human(None), "business_archive", {"business_id": project, "rationale": "Finished."},
                        expected_revision=host.query("SELECT revision FROM businesses WHERE id=?", project)[0]["revision"])
    assert archive.status == "signoff_required", (archive.status, archive.error)         # the owner's own archive proposal, as for any project


def test_an_inactive_ordubashy_and_a_project_home_do_not_create(tmp_path):
    host = creating(tmp_path)
    home = dataclasses.replace(host.human(None), home_business_id=host.ids["first"])   # creation is a Global operation
    with host.database.read() as uow, pytest.raises(DomainError) as refused:
        host.runtime.domains.business_creation.access(home, uow)
    assert refused.value.code == "forbidden"
    held = acting(host, host.ordubashy)                                               # a context resolved while the agent was still active
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (host.ordubashy,))
    assert code(create(host, held, "Nope")) in {"forbidden", "invalid_principal", "policy_denied", "inactive_agent"}
    with pytest.raises(DomainError) as refused:
        acting(host, host.ordubashy)
    assert refused.value.code == "inactive_agent"
    assert host.count("businesses", "slug<>'first'") == 0


def test_the_input_contract_is_exact(tmp_path):
    host = creating(tmp_path)
    maker = ordubashy(host)
    for arguments in ({}, {"name": ""}, {"name": "   "}, {"name": "x" * 201}, {"name": "A", "summary": "x" * 2001},
                      {"name": "A", "slug": "Bad_Slug"}, {"name": "A", "slug": "9lives"}, {"name": "A", "slug": "a" * 65},
                      {"name": "A", "slug": "../escape"}, {"name": "A", "owner_confirmation": "yes"}, {"name": "A", "core": {}},
                      {"name": "A", "source_refs": []}, {"name": "A", "milestones": []}):
        assert code(host.call(maker, "business_create", arguments)) == "validation_error", arguments
    assert host.count("businesses", "slug<>'first'") == 0
    padded = made(host, maker, "  " + "y" * 200 + "  ")                               # trimmed, then at most 200 characters
    assert padded["business"]["name"] == "y" * 200
    assert made(host, maker, "Plain", summary="", rationale="Because.")["slug"] == "plain"


@pytest.mark.parametrize("name, slug", [
    ("Repair Shop", "repair-shop"), ("  Édition  Spéciale!! ", "edition-speciale"), ("2025 Plans", "project-2025-plans"),
    ("!!!", "project"), ("日本語", "project"), ("a" * 100, "a" * 64), ("a" * 63 + " b", "a" * 63), ("-lead-and-trail-", "lead-and-trail"),
    ("9" + "z" * 100, ("project-9" + "z" * 100)[:64]),
])
def test_the_slug_is_derived_deterministically(name, slug):
    assert derive_slug(name) == slug
    assert len(slug) <= 64 and slug[0].isalpha() and slug == slug.lower() and not slug.endswith("-")


def test_numbered_suffixes_stay_within_the_slug_limit():
    assert numbered("repair", 2) == "repair-2" and len(numbered("a" * 64, 12)) == 64 and numbered("a" * 64, 12).endswith("-12")
    assert not numbered("a" * 62 + "-b", 10).count("--")


# -- collisions, links and idempotency ------------------------------------------------------------------------------------

def test_a_taken_slug_is_refused_with_the_first_free_suggestion(tmp_path):
    host = creating(tmp_path)
    maker = ordubashy(host)
    made(host, maker, "Shop")
    taken = create(host, maker, "Shop")
    assert code(taken) == "revision_conflict" and taken.error["details"] == {"slug": "shop", "suggested_slug": "shop-2"}
    made(host, maker, "Shop", slug="shop-2")
    assert create(host, maker, "Shop").error["details"] == {"slug": "shop", "suggested_slug": "shop-3"}
    chosen = create(host, maker, "Other", slug="shop")                                # an explicit slug is never replaced either
    assert code(chosen) == "revision_conflict" and chosen.error["details"]["suggested_slug"] == "shop-3"
    assert host.count("businesses", "slug LIKE 'shop%'") == 2


def test_an_existing_or_case_colliding_folder_is_never_adopted(tmp_path):
    host = creating(tmp_path)
    maker = ordubashy(host)
    projects = host.paths.operations / PROJECTS_DIRECTORY
    (projects / "identical" / "hq").mkdir(parents=True)
    (projects / "identical" / "code").mkdir()                                         # an identical empty tree is not ownership
    (projects / "Mixed").mkdir()
    for slug in ("identical", "mixed"):
        refused = create(host, maker, "X", slug=slug)
        assert code(refused) == "revision_conflict" and refused.error["details"]["slug"] == slug, slug
    assert host.count("businesses", "slug IN ('identical','mixed')") == 0
    assert tree(projects / "identical") == ["code", "hq"] and (projects / "Mixed").is_dir()


def test_a_symlinked_destination_or_projects_directory_is_refused(tmp_path):
    host = creating(tmp_path)
    maker = ordubashy(host)
    outside = tmp_path / "outside"
    outside.mkdir()
    projects = host.paths.operations / PROJECTS_DIRECTORY
    os.symlink(outside, projects / "linked")
    assert code(create(host, maker, "Linked", slug="linked")) in {"invalid_configuration", "revision_conflict"}
    assert list(outside.iterdir()) == [] and host.count("businesses", "slug='linked'") == 0
    (projects / "linked").unlink()
    projects.rename(tmp_path / "moved-projects")
    os.symlink(outside, projects)                                                     # the whole projects directory leaves the instance
    assert code(create(host, maker, "Fresh")) in {"invalid_configuration", "forbidden", "revision_conflict"}
    assert list(outside.iterdir()) == [] and host.count("businesses", "slug='fresh'") == 0


def test_the_same_key_and_input_return_the_same_project_and_a_changed_input_conflicts(tmp_path):
    host = creating(tmp_path)
    maker = ordubashy(host)
    key = new_id()
    first = made(host, maker, "Idem", key=key)
    again = host.call(maker, "business_create", {"name": "Idem"}, key=key)
    assert again.status == "ok" and plain(again.data) == first
    assert host.count("businesses", "slug='idem'") == 1
    other = host.call(maker, "business_create", {"name": "Idem", "summary": "changed"}, key=key)
    assert other.status == "error" and host.count("businesses", "slug LIKE 'idem%'") == 1


def test_concurrent_different_keys_create_the_destination_once(tmp_path):
    host = creating(tmp_path)
    maker, results, barrier = ordubashy(host), [], threading.Barrier(4)

    def attempt():
        barrier.wait()
        results.append(host.call(maker, "business_create", {"name": "Race", "slug": "race"}, key=new_id()))

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    statuses = sorted(result.status for result in results)
    assert statuses.count("ok") == 1 and all(result.status in {"ok", "error"} for result in results), statuses
    assert [result.error["code"] for result in results if result.status == "error"] and host.count("businesses", "slug='race'") == 1
    only_the_empty_project(host, "race")


# -- policy overrides and Signoff -------------------------------------------------------------------------------------------

def test_an_explicit_deny_stops_creation_and_a_signoff_is_applied_after_the_owner_approves(tmp_path):
    host = creating(tmp_path)
    owner, ordubashy = host.human(None), acting(host, host.ordubashy)
    profile = host.runtime.resolver.profile(Principal(host.ordubashy, "agent"))[0]
    host.data(owner, "permissions_set", {"agent_id": host.ordubashy, "overrides": {"business_create": "deny"}}, expected_revision=profile.revision)
    assert code(create(host, ordubashy, "Denied")) in {"forbidden", "policy_denied"}
    assert host.count("businesses", "slug='denied'") == 0
    profile = host.runtime.resolver.profile(Principal(host.ordubashy, "agent"))[0]
    host.data(owner, "permissions_set", {"agent_id": host.ordubashy, "overrides": {"business_create": "signoff"}}, expected_revision=profile.revision)
    proposed = create(host, acting(host, host.ordubashy), "Reviewed")
    assert proposed.status == "signoff_required", (proposed.status, proposed.error)
    assert host.count("businesses", "slug='reviewed'") == 0 and not folder(host, "reviewed").exists()
    reference = proposed.signoff
    host.runtime.signoffs.review(owner, reference["id"], decision="approve", proposal_revision=reference["proposal_revision"],
                                 payload_hash=reference["payload_hash"], expected_status_revision=reference["status_revision"],
                                 idempotency_key=new_id())
    host.runtime.signoffs.run_one(reference["id"])
    row = host.runtime.signoffs.get(owner, reference["id"]).to_dict()["data"]["signoff"]
    assert row["state"] == "applied", row["state"]
    assert (row["proposer_principal_id"], row["proposer_agent_id"]) == (host.ordubashy, host.ordubashy)   # the owner only approved
    assert host.query("SELECT principal_id FROM apply_receipts WHERE operation_name='business_create'") == [{"principal_id": host.ordubashy}]
    only_the_empty_project(host, "reviewed")
    assert host.count("businesses", "slug='reviewed'") == 1
    members = host.query("SELECT principal_id, is_owner FROM business_memberships WHERE business_id=(SELECT id FROM businesses WHERE slug='reviewed')")
    assert {(member["principal_id"], member["is_owner"]) for member in members if member["is_owner"]} == {(host.owner, 1)}


@pytest.mark.parametrize("demotion", ["capability", "inactive", "replaced"])
def test_a_signoff_cannot_be_replayed_into_a_changed_destination_or_a_demoted_caller(tmp_path, demotion):
    host = creating(tmp_path)
    owner = host.human(None)
    profile = host.runtime.resolver.profile(Principal(host.ordubashy, "agent"))[0]
    host.data(owner, "permissions_set", {"agent_id": host.ordubashy, "overrides": {"business_create": "signoff"}}, expected_revision=profile.revision)
    proposed = create(host, acting(host, host.ordubashy), "Stale", slug="stale")
    assert proposed.status == "signoff_required"
    if demotion == "capability":
        revoke_capabilities(host.instance, host.ordubashy, "coordinate_businesses")   # the caller loses the authority before approval
    elif demotion == "inactive":
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (host.ordubashy,))
    else:                                                                             # another agent becomes the configured coordinator
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (host.worker,))
    reference = proposed.signoff
    host.runtime.signoffs.review(owner, reference["id"], decision="approve", proposal_revision=reference["proposal_revision"],
                                 payload_hash=reference["payload_hash"], expected_status_revision=reference["status_revision"],
                                 idempotency_key=new_id())
    host.runtime.signoffs.run_one(reference["id"])
    assert host.runtime.signoffs.get(owner, reference["id"]).to_dict()["data"]["signoff"]["state"] != "applied"
    assert host.count("businesses", "slug='stale'") == 0 and not folder(host, "stale").exists()


def test_the_owner_cannot_forge_a_pending_signoff_or_approve_away_the_caller_check(tmp_path):
    host = creating(tmp_path)
    owner = host.human(None)
    profile = host.runtime.resolver.profile(Principal(host.ordubashy, "agent"))[0]
    host.data(owner, "permissions_set", {"agent_id": host.ordubashy, "overrides": {"business_create": "signoff"}}, expected_revision=profile.revision)
    assert code(create(host, owner, "Forged")) == "forbidden"                           # no owner proposal exists to approve
    assert host.count("signoffs", "operation_name='business_create'") == 0
    plan, _ = prepared(host, ordubashy(host), "Forged", slug="forged")                  # the owner cannot present Ordubashy's plan either
    with host.database.read() as uow, pytest.raises(DomainError) as denied:
        host.runtime.domains.business_creation.validate(owner, plan, uow)
    assert denied.value.code == "forbidden"
    assert host.count("businesses", "slug='forged'") == 0 and not folder(host, "forged").exists()


# -- the frozen plan ------------------------------------------------------------------------------------------------------

def prepared(host, ctx, name="Frozen", **extra):
    descriptor = host.runtime.registry.get("business_create")
    with host.database.read() as uow:
        plan = descriptor.prepare(ctx, {"name": name, **extra}, InvocationOptions(idempotency_key=new_id()), uow)
    return plan, next(change for change in plan.changes if change.action == ACTION)


def with_values(plan, change, edit):
    values = plain(change.values)
    edit(values)
    return dataclasses.replace(plan, changes=tuple(dataclasses.replace(item, values=values) if item is change else item
                                                  for item in plan.changes))


def validated(host, ctx, plan):
    with host.database.read() as uow:
        host.runtime.domains.business_creation.validate(ctx, plan, uow)


def test_the_plan_freezes_identity_slug_memberships_and_the_directory_layout(tmp_path):
    host = creating(tmp_path)
    maker = ordubashy(host)
    plan, change = prepared(host, maker, "Frozen", summary="Short.")
    values = plain(change.values)
    assert set(values) == {"business", "owner_id", "worker_ids", "directories"} and values["directories"] == list(LAYOUT)
    assert values["owner_id"] == host.owner and values["business"]["slug"] == "frozen" and values["business"]["config_json"] == {"one_liner": "Short.", "code": []}
    assert plan.operation_version == 2 and [item.action for item in plan.changes] == [ACTION, "create_directory_tree"]
    folder_change = plan.changes[1]
    assert folder_change.kind == "file" and folder_change.resource.resource_type == DIRECTORIES and folder_change.resource.resource_id == "frozen"
    assert list(folder_change.values["directories"]) == ["code", "hq"] and folder_change.values["before_hash"] is None
    validated(host, maker, plan)                                                      # an exact plan validates
    assert not folder(host, "frozen").exists()


@pytest.mark.parametrize("tamper", [
    lambda values: values["business"].update(slug="elsewhere"),
    lambda values: values["business"].update(workspace="/tmp/elsewhere"),
    lambda values: values["business"]["config_json"].update(forged=new_id()),
    lambda values: values.update(directories=["hq", "code", "agents"]),
    lambda values: values.update(owner_id=new_id()),
    lambda values: values.update(extra="field"),
    lambda values: values["business"].update(stage="launched"),
])
def test_a_forged_plan_is_refused_at_validation(tmp_path, tamper):
    host = creating(tmp_path)
    maker = ordubashy(host)
    plan, change = prepared(host, maker)
    with pytest.raises(DomainError) as error:
        validated(host, maker, with_values(plan, change, tamper))
    assert error.value.code in {"invalid_plan", "revision_conflict"}


def test_the_plan_binds_the_registered_system_memberships_and_revalidates_them(tmp_path):
    host = creating(tmp_path)
    maker = ordubashy(host)
    plan, change = prepared(host, maker)
    assert set(plain(change.values)["worker_ids"]) <= set(system_programs(host))
    forged = with_values(plan, change, lambda values: values["worker_ids"].append(new_id()))
    with pytest.raises(DomainError) as error:
        validated(host, maker, forged)
    assert error.value.code == "revision_conflict"
    data = made(host, maker, "Members")
    members = {row["principal_id"] for row in host.query("SELECT principal_id FROM business_memberships WHERE business_id=?", data["business_id"])}
    assert members == {host.owner, *plain(change.values)["worker_ids"]}
    assert not host.query("SELECT 1 FROM principals WHERE created_at>=? AND kind='system'", "9999") and len(system_programs(host)) == len(set(system_programs(host)))


# -- fault points and recovery ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phase", ["intent", "built", "staged", "renamed", "published"])
def test_a_crash_at_each_directory_boundary_resumes_to_one_complete_project(tmp_path, phase):
    host = creating(tmp_path)
    maker, key = ordubashy(host), new_id()

    def crash(resource, reached):
        if reached == phase:
            raise Crash(reached)

    host.runtime.file_applier.after_phase = crash
    with pytest.raises(Crash):
        host.call(maker, "business_create", {"name": "Resilient"}, key=key)
    assert host.count("businesses", "slug='resilient'") == 0                            # the row never precedes the folder proof
    host.runtime.file_applier.after_phase = None
    result = host.call(maker, "business_create", {"name": "Resilient"}, key=key)
    if phase == "built":
        # The staging directory exists but its identity was never recorded: fail closed, adopt and delete nothing.
        assert code(result) == "apply_incomplete" and result.error["details"]["disposition"] == "unproven_staging"
        assert host.count("businesses", "slug='resilient'") == 0 and not folder(host, "resilient").exists()
        assert host.count("resource_claims") > 0                                           # the claims stay for reconciliation
        assert code(create(host, maker, "Resilient", slug="resilient")) in {"busy", "apply_incomplete", "revision_conflict"}
        return
    assert result.status == "ok", (result.status, result.error)
    only_the_empty_project(host, "resilient")
    assert host.count("businesses", "slug='resilient'") == 1
    assert plain(host.call(maker, "business_create", {"name": "Resilient"}, key=key).data) == plain(result.data)
    assert not any((host.paths.runtime / "staging").rglob("*")) or all(path.is_dir() for path in (host.paths.runtime / "staging").rglob("*"))


@pytest.mark.parametrize("phase", ["staged", "published"])
def test_recovery_rechecks_the_configured_ordubashy_before_it_finishes_a_creation(tmp_path, phase):
    host = creating(tmp_path)
    maker, key = ordubashy(host), new_id()

    def crash(resource, reached):
        if reached == phase:
            raise Crash(reached)

    host.runtime.file_applier.after_phase = crash
    with pytest.raises(Crash):
        host.call(maker, "business_create", {"name": "Recheck", "slug": "recheck"}, key=key)
    host.runtime.file_applier.after_phase = None
    revoke_capabilities(host.instance, host.ordubashy, "coordinate_businesses")          # the caller is no longer the creator
    recovered = host.runtime.application.file_executor.recover_pending(host.runtime.application)
    assert [result.status for result in recovered] == ["error"] and recovered[0].error["code"] == "forbidden", recovered
    assert host.count("businesses", "slug='recheck'") == 0
    assert code(host.call(acting(host, host.ordubashy), "business_create", {"name": "Recheck", "slug": "recheck"}, key=key)) == "forbidden"
    grant_capabilities(host.instance, host.ordubashy, "coordinate_businesses")           # the same caller again: the same key finishes
    done = host.call(acting(host, host.ordubashy), "business_create", {"name": "Recheck", "slug": "recheck"}, key=key)
    if phase == "staged":
        # The refused, unpublished attempt was abandoned (its staging tree was proven and removed), so its key stays refused; a new key creates.
        assert code(done) == "revision_conflict" and not folder(host, "recheck").exists() and host.count("resource_claims") == 0
        done = host.call(acting(host, host.ordubashy), "business_create", {"name": "Recheck", "slug": "recheck"}, key=new_id())
    assert done.status == "ok", (done.status, done.error)
    only_the_empty_project(host, "recheck")
    assert host.count("businesses", "slug='recheck'") == 1 and host.count("resource_claims") == 0


def test_a_folder_that_appears_after_preparation_is_never_adopted(tmp_path):
    host = creating(tmp_path)
    maker = ordubashy(host)
    plan, _ = prepared(host, maker, "Late", slug="late")
    (folder(host, "late") / "hq").mkdir(parents=True)
    (folder(host, "late") / "code").mkdir()
    with host.database.read() as uow, pytest.raises(DomainError) as error:
        host.runtime.application.check_targets(maker, plan, uow)
    assert error.value.code == "revision_conflict"
    assert code(create(host, maker, "Late", slug="late")) == "revision_conflict"
    assert host.count("businesses", "slug='late'") == 0


# -- the case-insensitive reservation holds after preparation ---------------------------------------------------------------------

def foreign_variant(host, slug="Case-Race"):
    """Another filesystem writer's folder that differs from the slug only in letter case; returns its identity."""
    folder(host, slug).mkdir()
    info = folder(host, slug).stat()
    return info.st_dev, info.st_ino


def variant_untouched(host, identity, slug="Case-Race"):
    info = folder(host, slug).stat()
    assert (info.st_dev, info.st_ino) == identity and not any(folder(host, slug).iterdir())
    assert sorted(path.name for path in (host.paths.operations / PROJECTS_DIRECTORY).iterdir() if path.name.lower() == slug.lower()) == [slug] or \
        (folder(host, slug.lower()).is_dir() and slug != slug.lower())


def after_prepare(host, action):
    """Run ``action`` immediately after the real preparation of ``business_create`` and before any validation or application."""
    descriptor = host.runtime.registry.get("business_create")

    def prepare(*args, **kwargs):
        plan = descriptor.prepare(*args, **kwargs)
        action()
        return plan

    host.runtime.registry._descriptors["business_create"] = dataclasses.replace(descriptor, prepare=prepare)
    return lambda: host.runtime.registry._descriptors.__setitem__("business_create", descriptor)


def test_a_case_variant_arriving_between_preparation_and_application_is_refused_and_left_alone(tmp_path):
    host = creating(tmp_path)
    maker, seen = ordubashy(host), {}
    restore = after_prepare(host, lambda: seen.setdefault("identity", foreign_variant(host)))
    refused = create(host, maker, "Case Race", slug="case-race")
    restore()
    assert code(refused) == "revision_conflict", refused.error
    assert refused.error["details"] == {"slug": "case-race", "suggested_slug": "case-race-2"}
    assert not folder(host, "case-race").exists() and host.count("businesses", "slug='case-race'") == 0
    variant_untouched(host, seen["identity"])
    assert host.count("resource_claims") == 0 and host.count("apply_receipts", "operation_name='business_create'") == 0   # nothing was started
    made(host, maker, "Case Race", slug="case-race-2")                                       # the suggestion is usable
    only_the_empty_project(host, "case-race-2")


def test_a_case_variant_arriving_after_the_directory_is_published_keeps_the_receipt_and_both_folders(tmp_path):
    host = creating(tmp_path)
    maker, key, seen = ordubashy(host), new_id(), {}

    def arrive(resource, reached):
        if reached == "published":
            host.runtime.file_applier.after_phase = None
            seen["identity"] = foreign_variant(host)

    host.runtime.file_applier.after_phase = arrive
    refused = host.call(maker, "business_create", {"name": "Case Race", "slug": "case-race"}, key=key)
    assert code(refused) == "revision_conflict" and refused.error["details"] == {"slug": "case-race", "suggested_slug": "case-race-2"}
    assert host.count("businesses", "slug='case-race'") == 0                                  # the row never commits over the sibling
    only_the_empty_project(host, "case-race")                                                 # the published folder is kept for reconciliation
    published = folder(host, "case-race").stat().st_ino
    variant_untouched(host, seen["identity"])
    assert host.count("resource_claims") > 0
    assert host.query("SELECT state FROM apply_receipts WHERE operation_name='business_create'") == [{"state": "conflicted"}]
    assert code(create(host, maker, "Another", slug="case-race")) in {"busy", "revision_conflict"}
    again = host.call(maker, "business_create", {"name": "Case Race", "slug": "case-race"}, key=key)
    assert code(again) == "revision_conflict" and host.count("businesses", "slug='case-race'") == 0   # still refused while the sibling stays
    folder(host, "Case-Race").rmdir()                                                        # its maker removes it: the same key completes
    done = host.call(maker, "business_create", {"name": "Case Race", "slug": "case-race"}, key=key)
    assert done.status == "ok", (done.status, done.error)
    assert folder(host, "case-race").stat().st_ino == published and host.count("businesses", "slug='case-race'") == 1
    only_the_empty_project(host, "case-race")
    assert host.count("resource_claims") == 0


def test_a_case_variant_arriving_while_a_signoff_waits_conflicts_the_proposal_and_creates_nothing(tmp_path):
    host = creating(tmp_path)
    owner = host.human(None)
    profile = host.runtime.resolver.profile(Principal(host.ordubashy, "agent"))[0]
    host.data(owner, "permissions_set", {"agent_id": host.ordubashy, "overrides": {"business_create": "signoff"}}, expected_revision=profile.revision)
    proposed = create(host, acting(host, host.ordubashy), "Case Race", slug="case-race")
    assert proposed.status == "signoff_required", (proposed.status, proposed.error)
    identity = foreign_variant(host)                                                          # arrives while the proposal waits
    reference = proposed.signoff
    host.runtime.signoffs.review(owner, reference["id"], decision="approve", proposal_revision=reference["proposal_revision"],
                                 payload_hash=reference["payload_hash"], expected_status_revision=reference["status_revision"],
                                 idempotency_key=new_id())
    host.runtime.signoffs.run_one(reference["id"])
    state = host.runtime.signoffs.get(owner, reference["id"]).to_dict()["data"]["signoff"]
    assert state["state"] != "applied", state
    assert not folder(host, "case-race").exists() and host.count("businesses", "slug='case-race'") == 0
    variant_untouched(host, identity)
    assert host.count("resource_claims") == 0


def test_a_case_variant_arriving_after_a_signoff_published_the_directory_keeps_the_receipt(tmp_path):
    host = creating(tmp_path)
    owner, seen = host.human(None), {}
    profile = host.runtime.resolver.profile(Principal(host.ordubashy, "agent"))[0]
    host.data(owner, "permissions_set", {"agent_id": host.ordubashy, "overrides": {"business_create": "signoff"}}, expected_revision=profile.revision)
    proposed = create(host, acting(host, host.ordubashy), "Case Race", slug="case-race")
    assert proposed.status == "signoff_required", (proposed.status, proposed.error)
    reference = proposed.signoff
    host.runtime.signoffs.review(owner, reference["id"], decision="approve", proposal_revision=reference["proposal_revision"],
                                 payload_hash=reference["payload_hash"], expected_status_revision=reference["status_revision"],
                                 idempotency_key=new_id())

    def arrive(resource, reached):
        if reached == "published":
            host.runtime.file_applier.after_phase = None
            seen["identity"] = foreign_variant(host)

    host.runtime.file_applier.after_phase = arrive
    host.runtime.signoffs.run_one(reference["id"])
    state = host.runtime.signoffs.get(owner, reference["id"]).to_dict()["data"]["signoff"]
    assert state["state"] != "applied", state
    assert host.count("businesses", "slug='case-race'") == 0
    only_the_empty_project(host, "case-race")
    variant_untouched(host, seen["identity"])
    assert host.count("resource_claims") > 0                                                  # the claims stay for reconciliation


@pytest.mark.parametrize("phase", ["staged", "published"])
def test_a_case_variant_arriving_across_a_crash_blocks_recovery_until_it_is_gone(tmp_path, phase):
    host = creating(tmp_path)
    maker, key, seen = ordubashy(host), new_id(), {}

    def crash(resource, reached):
        if reached == phase:
            raise Crash(reached)

    host.runtime.file_applier.after_phase = crash
    with pytest.raises(Crash):
        host.call(maker, "business_create", {"name": "Case Race", "slug": "case-race"}, key=key)
    host.runtime.file_applier.after_phase = None
    seen["identity"] = foreign_variant(host)                                                  # appears while the process is down
    recovered = host.runtime.application.file_executor.recover_pending(host.runtime.application)
    assert [result.status for result in recovered] == ["error"] and recovered[0].error["code"] == "revision_conflict"
    assert recovered[0].error["details"] == {"slug": "case-race", "suggested_slug": "case-race-2"}
    assert host.count("businesses", "slug='case-race'") == 0
    variant_untouched(host, seen["identity"])
    if phase == "staged":
        assert not folder(host, "case-race").exists() and host.count("resource_claims") == 0     # only the proven staging tree was removed
        assert not any((host.paths.runtime / "staging" / PROJECTS_DIRECTORY).iterdir())
    else:
        only_the_empty_project(host, "case-race")
        assert host.count("resource_claims") > 0
    folder(host, "Case-Race").rmdir()
    if phase == "staged":
        # The refused, unpublished attempt was abandoned like any lost publication race, so its key stays refused; a new key creates.
        assert code(host.call(maker, "business_create", {"name": "Case Race", "slug": "case-race"}, key=key)) == "revision_conflict"
        key = new_id()
    done = host.call(maker, "business_create", {"name": "Case Race", "slug": "case-race"}, key=key)
    assert done.status == "ok", (done.status, done.error)
    only_the_empty_project(host, "case-race")
    assert host.count("businesses", "slug='case-race'") == 1 and host.count("resource_claims") == 0


def test_the_exact_destination_of_this_creation_is_not_a_case_variant(tmp_path):
    host = creating(tmp_path)
    maker, key = ordubashy(host), new_id()

    def crash(resource, reached):
        if reached == "published":
            raise Crash(reached)

    host.runtime.file_applier.after_phase = crash
    with pytest.raises(Crash):
        host.call(maker, "business_create", {"name": "Own Folder", "slug": "own-folder"}, key=key)
    host.runtime.file_applier.after_phase = None
    only_the_empty_project(host, "own-folder")                                                # published, not yet committed
    done = host.call(maker, "business_create", {"name": "Own Folder", "slug": "own-folder"}, key=key)
    assert done.status == "ok", (done.status, done.error)
    assert host.count("businesses", "slug='own-folder'") == 1


# -- creation has no automatic effects ----------------------------------------------------------------------------------------

def test_creation_makes_no_provisioning_filing_agent_or_routine_effect_even_with_integrations_on(tmp_path, monkeypatch):
    host = creating(tmp_path)
    maker = ordubashy(host)
    import bazarlyk.sandbox.config as sandbox_config
    monkeypatch.setattr(sandbox_config, "creation_enabled", lambda root: True)
    before = {table: host.count(table) for table in ("jobs", "routines", "agent_profiles", "principals", "tasks", "documents",
                                                      "context_inputs", "context_source_versions", "business_sandboxes", "email_mailboxes")}
    data = made(host, maker, "Quiet")
    after = {table: host.count(table) for table in before}
    assert after == before, {table: (before[table], after[table]) for table in before if before[table] != after[table]}
    assert records_of(host, data["business_id"]).keys() <= {"business_memberships", "inbox_changes"}
    assert not (folder(host, "quiet") / "agents").exists()
    assert not list(host.paths.operations.glob("projects/quiet/**/*.*"))


def test_the_retired_setup_capability_can_no_longer_be_granted(tmp_path):
    host = creating(tmp_path)
    owner = host.human(None)
    with host.database.read() as uow:
        row = uow.connection.execute("SELECT revision, profile_json FROM agent_profiles WHERE agent_id=?", (host.worker,)).fetchone()
    scope = json.loads(row["profile_json"])["scope"] if isinstance(row["profile_json"], str) else row["profile_json"]["scope"]
    scope = {**scope, "global_capabilities": sorted({*scope["global_capabilities"], RETIRED})}
    refused = host.call(owner, "agents_update", {"agent_id": host.worker, "changes": {"scope": scope}}, expected_revision=row["revision"])
    assert code(refused) in {"invalid_profile", "validation_error"}
    assert RETIRED not in host.query("SELECT profile_json FROM agent_profiles WHERE agent_id=?", host.worker)[0]["profile_json"][
        "scope"]["global_capabilities"]
    assert RETIRED not in GLOBAL_CAPABILITIES and RETIRED in RETIRED_GLOBAL_CAPABILITIES


def test_setup_operations_are_gone_and_the_explicit_services_stay_registered(tmp_path):
    host = creating(tmp_path)
    registry = host.runtime.registry
    for retired in ("business_setup_get", "business_setup_submit", "business_setup_update"):
        with pytest.raises(Exception):
            registry.get(retired).prepare if registry.get(retired).prepare else registry.get(retired).read
    owner = host.human("first")
    for retired in ("business_setup_get", "business_setup_submit", "business_setup_update"):
        assert host.call(owner, retired, {}).status == "error"
    for kept in ("cloudflare_zone_token_ensure", "cloudflare_dns_records_ensure", "cloudflare_api_request",
                 "environment_set", "business_create"):
        assert registry.get(kept) is not None, kept
    # Business mailbox provisioning is retired with the core mail domain; Fastmail is an optional extension.
    with pytest.raises(DomainError):
        registry.get("business_email_default_set")
    # The SEO site service is retired with the rest of the commercial layer; the general Cloudflare services above stay.
    for retired in ("seo_site_enable", "seo_site_get", "seo_site_check", "seo_site_update"):
        with pytest.raises(DomainError):
            registry.get(retired)


# -- an empty project is an ordinary project ------------------------------------------------------------------------------------

def test_an_empty_project_supports_ordinary_work_and_stays_isolated_from_its_neighbors(tmp_path):
    host = creating(tmp_path)
    maker = ordubashy(host)
    one, two = made(host, maker, "One")["business_id"], made(host, maker, "Two")["business_id"]
    host.ids.update(one=one, two=two)
    one_ctx, two_ctx = host.human("one"), host.human("two")                         # the owner's ordinary work in Ordubashy-created projects
    task = host.data(one_ctx, "tasks_create", {"title": "First task"})["task"]
    assert host.data(one_ctx, "tasks_get", {"task_id": task["id"]})["task"]["title"] == "First task"
    assert [item["id"] for item in host.data(two_ctx, "tasks_list", {})["tasks"]] == []
    note = host.data(one_ctx, "notes_create", {"title": "Plan", "body": "Start small.", "target_business_id": one})["document"]
    assert host.data(one_ctx, "notes_get", {"document_id": note["id"]})["document"]["id"] == note["id"]
    assert (host.paths.business_hq("one")).is_dir() and any((host.paths.business_hq("one")).rglob("*"))
    assert not any((host.paths.business_hq("two")).rglob("*"))
    table = host.data(one_ctx, "tables_create", {"title": "Leads", "columns": [{"id": "name", "name": "Name", "type": "text"}]})["table"]
    assert table["id"] and host.data(two_ctx, "tables_list", {})["items"] == []
    assert host.data(host.human(None), "tasks_list", {"scope": "global"})["tasks"] == []   # Global is unaffected by project work
    routine = host.data(one_ctx, "routines_create", {"definition": {
        "name": "Weekly check", "why": "Keep the project fresh.", "schedule": "0 5 * * 1", "timezone": "UTC", "status": "paused",
        "program": "test.unrelated", "arguments": {}}})
    assert routine["records"][0]["name"] == "Weekly check"
    assert host.data(two_ctx, "routines_list", {})["items"] == []
    assert tree(folder(host, "two")) == ["code", "hq"]
