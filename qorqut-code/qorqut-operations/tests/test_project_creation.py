"""Project creation (``business_create``): frozen record and folder, the directory receipt, and a creator checked on every phase.

Real: the engine's ``Application``, plan validation, the ``create_directory_tree`` effect with its receipts and recovery,
and the creation rules. Faked, and explicit: the five location facts and the validating location callables the host binds
(here recording every call), the ``creator_actor`` callable and the bounded system programs (``worker_authority``). The host's
identity adapter itself stays in the host; its checks are not reproduced here."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path

import pytest

from qorqut_operations.application import Application, OperationRegistry
from qorqut_operations.contracts import (ChangeSet, DomainError, ExpectedTarget, InvocationContext, InvocationOptions, PreparedChange,
                                         Principal, ResourceRef, new_id, plain)
from qorqut_operations.projects.creation import creation as rules
from qorqut_operations.projects.creation import operations as module
from qorqut_operations.projects.creation.operations import COORDINATION_CAPABILITY, BusinessCreationOperations
from qorqut_operations.storage import FileApplier, LockPaths, RegisteredDirectoryTarget, layout_hash, observe_directory
from qorqut_operations.storage.executor import PreparedFileExecutor
from support import HUMAN, FixedPolicy, database_at

CREATOR = "00000000-0000-4000-8000-0000000000c1"
INTRUDER = "00000000-0000-4000-8000-0000000000c2"
PROGRAM = "00000000-0000-4000-8000-0000000000d1"
OTHER_PROGRAM = "00000000-0000-4000-8000-0000000000d2"
INSTALLATION = "11111111-1111-4111-8111-111111111111"
PROJECTS = "projects"
BASELINE_DESCRIPTOR = "0f8b0b5ead07f829266ff9c16e938cce3385089e3049edee5975e5dd86182938"


class Crash(BaseException):
    """A simulated process crash: nothing in the executor's failure handling runs for it."""


class Locations:
    """The host's validating location functions, as explicit callables that record each call."""

    def __init__(self, operations):
        self.operations, self.calls = operations, []

    def project_root(self, row):
        self.calls.append(("root", row["slug"]))
        folder = self.operations / PROJECTS
        if folder.is_symlink() or (folder / row["slug"]).is_symlink():
            raise DomainError("invalid_configuration", "Registered location is unavailable: symlink")
        return folder / row["slug"]

    def project_hq(self, row):
        self.calls.append(("hq", row["slug"]))
        return self.project_root(row) / "hq"

    def relative_path(self, path):
        self.calls.append(("relative", str(path)))
        try:
            return Path(path).relative_to(self.operations).as_posix()
        except ValueError as exc:
            raise DomainError("forbidden", "The registered location is outside the operations root") from exc


class Creator:
    """``creator_actor(uow, identity)``: the role while allowed; it can revoke itself on a chosen call."""

    def __init__(self):
        self.calls, self.phases, self.phase = [], [], None
        self.allowed, self.revoke_on = {CREATOR}, None

    def __call__(self, uow, identity):
        self.calls.append(identity)
        self.phases.append(self.phase)
        if self.revoke_on is not None and len(self.calls) >= self.revoke_on:
            return None
        return "ordubashy" if identity in self.allowed else None


class Programs:
    """The generic worker-authority attachment: bounded programs that require project scope, revalidated every time."""

    def __init__(self):
        self.programs = [type("Program", (), {"id": "queue-driver", "scope_kinds": ("business",)})(),
                         type("Program", (), {"id": "global-only", "scope_kinds": ("global",)})()]
        self.identity, self.required = PROGRAM, []

    def worker_id(self, program_id, uow):
        assert program_id == "queue-driver"
        return self.identity

    def require_worker(self, identity, business_id, uow):
        self.required.append(identity)
        if uow.repository("principals").get(identity) is None:
            raise DomainError("forbidden", "Program is not registered")


class Place:
    def __init__(self, tmp_path, *, attach=True):
        self.tmp = Path(tmp_path)
        self.root = self.tmp / "operations"
        (self.root / PROJECTS).mkdir(parents=True)
        self.staging = self.root / ".runtime" / "staging" / PROJECTS
        self.database = database_at(tmp_path)
        with self.database.transaction() as uow:
            uow.repository("installation").insert({"id": "service", "installation_id": INSTALLATION, "owner_principal_id": HUMAN})
            principals = uow.repository("principals")
            for identity, kind, name in ((CREATOR, "agent", "Coordinator"), (INTRUDER, "agent", "Intruder"),
                                         (PROGRAM, "system", "Queue driver"), (OTHER_PROGRAM, "system", "Replacement driver")):
                principals.insert({"id": identity, "kind": kind, "display_name": name})
            for identity, role in ((CREATOR, "ordubashy"), (INTRUDER, "global-worker")):       # active global profiles: the engine's agent check
                profile = {"status": "active", "role": {"id": role}, "runtime": "codex", "scope": {
                    "kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": [COORDINATION_CAPABILITY]}}
                uow.connection.execute(
                    "INSERT INTO agent_profiles(agent_id, principal_id, home_business_id, namespace, name, workspace, profile_json, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?, 'now', 'now')",
                    (identity, identity, None, "ecosystem", "agent-" + identity[-2:], "/nonexistent/" + identity, json.dumps(profile)))
        self.locations, self.creator, self.programs = Locations(self.root), Creator(), Programs()
        self.crash = None
        self.locks = LockPaths.under(self.tmp / "locks")
        self.ctx = InvocationContext(Principal(CREATOR, "agent"), source="mcp", profile_revision=1,
                                     global_capabilities=frozenset({COORDINATION_CAPABILITY}))
        self.registry = OperationRegistry()
        self.applier = FileApplier(self.database, lambda resource: self.operations.creation.directory_target(resource),
                                   locks=self.locks, after_phase=self.fail_after)
        self.app = Application(self.database, self.registry, policy=FixedPolicy(),
                               file_executor=PreparedFileExecutor(self.database, self.applier, context_loader=lambda plan, uow: self.ctx))
        self.operations = BusinessCreationOperations(
            self.app, operations_root=self.root, staging_root=self.staging, project_root=self.locations.project_root,
            project_hq=self.locations.project_hq, relative_path=self.locations.relative_path, projects_directory=PROJECTS,
            creator_actor=self.creator)
        if attach:
            self.operations.worker_authority = self.programs
        for name in ("prepare", "validate", "apply"):            # note which operation phase each creator consultation belongs to
            self.mark(name)
        self.operations.register(self.registry)

    def mark(self, name):
        original = getattr(self.operations, name)

        def call(*arguments):
            self.creator.phase = name
            return original(*arguments)
        setattr(self.operations, name, call)

    def fail_after(self, resource, phase):
        if self.crash == phase:
            raise Crash(phase)

    def create(self, name="Alpha Project", ctx=None, key=None, **arguments):
        return self.app.invoke(ctx or self.ctx, "business_create", {"name": name, **arguments},
                               {"idempotency_key": key or new_id()})

    def prepared(self, **arguments):
        """The prepared plan only, as the engine would have saved it."""
        with self.database.transaction() as uow:
            return self.operations.prepare(self.ctx, {"name": "Alpha Project", **arguments}, InvocationOptions(idempotency_key=new_id()), uow)

    def phase(self, name, plan, ctx=None):
        with self.database.transaction() as uow:
            return getattr(self.operations, name)(ctx or self.ctx, plan, uow)

    def rows(self, sql, *parameters):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, parameters)]

    def tree(self, slug="alpha-project"):
        path = self.root / PROJECTS / slug
        return sorted(str(item.relative_to(path)) for item in path.rglob("*")) if path.is_dir() else None


@pytest.fixture
def place(tmp_path):
    return Place(tmp_path)


# -- the operation descriptor ---------------------------------------------------------------------------------------------------------------

def test_the_descriptor_keeps_its_name_version_description_schema_scope_and_capability(place):
    fields = {name: getattr(module, name) for name in ("NAME", "SLUG", "SUMMARY", "PROJECT", "OUTPUT", "VERSION", "DESCRIPTION")}
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == BASELINE_DESCRIPTOR                  # the digest of the same constants in the pre-move host module
    descriptor = place.registry.get("business_create")
    assert (descriptor.name, descriptor.version, descriptor.effect_kind) == ("business_create", 2, "compound")
    assert descriptor.scope_kinds == frozenset({"global"}) and descriptor.global_capability == "coordinate_businesses"
    assert descriptor.description == module.DESCRIPTION and list(descriptor.input_schema["required"]) == ["name"]
    assert set(descriptor.input_schema["properties"]) == {"name", "slug", "summary", "rationale"}
    assert COORDINATION_CAPABILITY == "coordinate_businesses"
    assert (rules.DIRECTORIES, rules.NAMES, rules.ACTION, rules.LAYOUT) == (
        "business_creation_directories", "business_creation_names", "create_business", ("code", "hq"))


def test_the_constructor_requires_every_explicit_fact_and_the_creator_callable(place):
    facts = dict(operations_root=place.root, staging_root=place.staging, project_root=place.locations.project_root,
                 project_hq=place.locations.project_hq, relative_path=place.locations.relative_path, projects_directory=PROJECTS,
                 creator_actor=place.creator)
    for missing in facts:
        with pytest.raises(TypeError):
            BusinessCreationOperations(place.app, **{name: value for name, value in facts.items() if name != missing})
    with pytest.raises(TypeError):
        BusinessCreationOperations(place.app, place.root)


# -- creation, the folder receipt and the exact record ----------------------------------------------------------------------------------

def test_creation_makes_the_row_memberships_and_empty_folder_and_nothing_else(place):
    before = {table: place.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"] for table in ("items", "documents", "principals")}
    result = place.create("Alpha Project", summary="First description")
    assert result.status == "ok", result.error
    data = plain(result.data)
    slug = "alpha-project"
    assert data["slug"] == slug and data["business"]["name"] == "Alpha Project" and data["business"]["summary"] == "First description"
    assert data["business"]["workspace_path"] == str(place.root / PROJECTS / slug)
    assert data["business"]["hq_path"] == str(place.root / PROJECTS / slug / "hq")
    assert place.tree() == ["code", "hq"] and all(not any(p.iterdir()) for p in (place.root / PROJECTS / slug).iterdir())
    [row] = place.rows("SELECT * FROM businesses WHERE slug=?", slug)
    assert (row["stage"], row["timezone"], row["name"]) == ("idea", "UTC", "Alpha Project")
    assert json.loads(row["config_json"]) == {"one_liner": "First description", "code": []}
    members = place.rows("SELECT principal_id, is_owner FROM business_memberships WHERE business_id=? ORDER BY is_owner DESC", row["id"])
    assert [(m["principal_id"], m["is_owner"]) for m in members] == [(HUMAN, 1), (PROGRAM, 0)]          # owner first, then the program
    assert [place.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"] for table in ("items", "documents", "principals")] == list(before.values())
    assert place.staging.is_dir() and list(place.staging.iterdir()) == []                               # nothing left in private staging
    assert observe_directory(place.operations.creation.directory_target(ResourceRef(rules.DIRECTORIES, slug, None, "global"))) \
        == layout_hash(rules.LAYOUT)
    # The same key replays its one saved result; another key finds the folder taken and adopts nothing.
    again = place.create("Alpha Project", key=None)
    assert again.status == "error" and again.error["code"] == "revision_conflict"
    assert again.error["details"]["suggested_slug"] == "alpha-project-2"


def test_the_registered_target_is_the_operations_relative_folder_staged_privately(place):
    target = place.operations.creation.directory_target(ResourceRef(rules.DIRECTORIES, "alpha", None, "global"))
    assert target == RegisteredDirectoryTarget(place.root, "projects/alpha", place.staging)
    place.locations.calls.clear()
    place.operations.creation.directory_target(ResourceRef(rules.DIRECTORIES, "alpha", None, "global"))
    assert place.locations.calls == [("root", "alpha"), ("relative", str(place.root / PROJECTS / "alpha"))]    # called afresh each time
    for bad in (ResourceRef(rules.DIRECTORIES, "Alpha", None, "global"), ResourceRef(rules.DIRECTORIES, "-alpha", None, "global"),
                ResourceRef(rules.DIRECTORIES, "a/b", None, "global"), ResourceRef(rules.DIRECTORIES, "alpha", new_id(), "global"),
                ResourceRef(rules.DIRECTORIES, "alpha", None, "business"), ResourceRef("other", "alpha", None, "global")):
        with pytest.raises(DomainError) as error:
            place.operations.creation.directory_target(bad)
        assert error.value.code == "invalid_plan", bad


def test_a_symlinked_projects_directory_or_destination_is_refused_by_the_bound_function(place):
    elsewhere = place.tmp / "elsewhere"
    elsewhere.mkdir()
    (place.root / PROJECTS / "linked").symlink_to(elsewhere)
    result = place.create("Linked")
    assert result.status == "error" and result.error["code"] == "invalid_configuration" and list(elsewhere.iterdir()) == []
    (place.root / PROJECTS).rename(place.root / "real-projects")
    (place.root / PROJECTS).symlink_to(place.root / "real-projects")
    result = place.create("Beta")
    assert result.status == "error" and result.error["code"] == "invalid_configuration"
    assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug='beta'")[0]["n"] == 0


def test_a_taken_slug_is_a_conflict_with_the_next_free_suggestion_and_adopts_nothing(place):
    (place.root / PROJECTS / "alpha-project").mkdir()
    (place.root / PROJECTS / "alpha-project-2").mkdir()
    for blocked in ("Alpha-Project-3", "ALPHA-PROJECT-4"):                                            # case variants reserve the name too
        (place.root / PROJECTS / blocked).mkdir()
    result = place.create("Alpha Project")
    assert result.status == "error" and result.error["code"] == "revision_conflict"
    assert result.error["details"] == {"slug": "alpha-project", "suggested_slug": "alpha-project-5"}
    assert sorted(os.listdir(place.root / PROJECTS / "alpha-project")) == []                         # nothing was written into it
    assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug LIKE 'alpha%'")[0]["n"] == 0
    registered = place.create("First")                                                              # a registered row also holds its name
    assert registered.status == "error" and registered.error["details"]["suggested_slug"] == "first-2"
    result = place.create("Case", slug="case")
    assert result.status == "ok"
    assert place.create("Case", slug="case").error["code"] == "revision_conflict"


def test_a_case_variant_that_appears_after_preparation_refuses_every_later_phase(place):
    plan = place.prepared()
    (place.root / PROJECTS / "Alpha-Project").mkdir()
    for phase_name in ("validate", "apply"):
        with pytest.raises(DomainError) as error:
            place.phase(phase_name, plan)
        assert error.value.code == "revision_conflict" and error.value.details["slug"] == "alpha-project"
    assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug='alpha-project'")[0]["n"] == 0


def test_a_crash_after_publication_resumes_once_and_commits_one_row(place):
    place.crash = "published"
    with pytest.raises(Crash):
        place.create("Alpha Project", key="key-1")
    assert place.tree() == ["code", "hq"] and place.rows("SELECT count(*) AS n FROM businesses WHERE slug='alpha-project'")[0]["n"] == 0
    place.crash = None
    results = place.app.file_executor.recover_pending(place.app)
    assert [item.status for item in results] == ["ok"], [item.error for item in results]
    assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug='alpha-project'")[0]["n"] == 1
    assert place.create("Alpha Project", key="key-1").status == "ok"                                 # the original key replays the result
    assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug='alpha-project'")[0]["n"] == 1
    assert place.app.file_executor.recover_pending(place.app) == []


def test_a_folder_that_changed_after_publication_is_not_adopted_and_commits_nothing(place):
    def tamper(resource, phase):
        if phase == "published":
            (place.root / PROJECTS / "alpha-project" / "hq" / "intruder.txt").write_text("x")
    place.applier.after_phase = tamper
    result = place.create("Alpha Project")
    assert result.status == "error" and result.error["code"] == "revision_conflict"
    assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug='alpha-project'")[0]["n"] == 0


# -- the frozen plan --------------------------------------------------------------------------------------------------------------------

def with_values(plan, index, **values):
    change = plan.changes[index]
    changed = PreparedChange(change.kind, change.resource, change.action, {**plain(change.values), **values}, change.operation_name)
    changes = tuple(changed if position == index else item for position, item in enumerate(plan.changes))
    return dataclasses.replace(plan, changes=changes)


def test_the_prepared_plan_freezes_exactly_one_project_and_one_folder(place):
    plan = place.prepared(slug="gamma", summary="About")
    [created] = [change for change in plan.changes if change.action == rules.ACTION]
    [folder] = [change for change in plan.changes if change.kind == "file"]
    values = plain(created.values)
    assert set(values) == rules.VALUE_KEYS and values["directories"] == ["code", "hq"] and values["worker_ids"] == [PROGRAM]
    assert values["owner_id"] == HUMAN and values["business"]["slug"] == "gamma"
    assert set(values["business"]["config_json"]) == rules.CONFIG_KEYS
    assert values["business"]["workspace"] == str(place.root / PROJECTS / "gamma")
    assert values["business"]["hq_path"] == str(place.root / PROJECTS / "gamma" / "hq")
    assert folder.resource == ResourceRef(rules.DIRECTORIES, "gamma", None, "global")
    targets = {(target.resource.resource_type, target.resource.resource_id): target.exists for target in plan.expected_targets}
    assert targets[(rules.NAMES, "gamma")] is False and targets[(rules.DIRECTORIES, "gamma")] is False
    assert targets[("installation", "service")] is True and targets[("principals", PROGRAM)] is True


@pytest.mark.parametrize("change,message,code", [
    (lambda plan: dataclasses.replace(plan, changes=plan.changes + plan.changes[-1:]), "exactly one project", "invalid_plan"),
    (lambda plan: dataclasses.replace(plan, changes=plan.changes[:1]), "exactly one project", "invalid_plan"),
    (lambda plan: with_values(plan, 0, extra=1), "differs from what preparation writes", "invalid_plan"),
    (lambda plan: with_values(plan, 0, directories=["code"]), "differs from what preparation writes", "invalid_plan"),
    (lambda plan: with_values(plan, 0, business={**plain(plan.changes[0].values)["business"], "stage": "launch"}),
     "differs from what preparation writes", "invalid_plan"),
    (lambda plan: with_values(plan, 0, business={**plain(plan.changes[0].values)["business"], "slug": "Bad Slug"}),
     "differs from what preparation writes", "invalid_plan"),
    (lambda plan: with_values(plan, 0, business={**plain(plan.changes[0].values)["business"], "workspace": "/elsewhere/alpha"}),
     "location changed", "revision_conflict"),
    (lambda plan: with_values(plan, 0, business={**plain(plan.changes[0].values)["business"], "hq_path": "/elsewhere/alpha/hq"}),
     "location changed", "revision_conflict"),
    (lambda plan: with_values(plan, 0, owner_id=INTRUDER), "owner changed", "revision_conflict"),
])
def test_a_plan_that_differs_from_what_preparation_writes_is_refused_at_every_phase(place, change, message, code):
    plan = change(place.prepared(slug="alpha"))
    for phase_name in ("validate", "apply"):
        with pytest.raises(DomainError) as error:
            place.phase(phase_name, plan)
        assert error.value.code == code and message in error.value.message, phase_name
    assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug='alpha'")[0]["n"] == 0


def test_the_bound_location_functions_are_called_again_at_each_validation_point(place):
    plan = place.prepared(slug="alpha")
    place.locations.calls.clear()
    place.phase("validate", plan)
    validate_calls = list(place.locations.calls)
    assert ("root", "alpha") in validate_calls and ("hq", "alpha") in validate_calls     # the frozen locations are compared afresh
    place.locations.calls.clear()
    place.phase("apply", plan)
    assert ("root", "alpha") in place.locations.calls and ("hq", "alpha") in place.locations.calls
    # A location function that now refuses stops the phase: nothing was cached from preparation.
    refusing = place.prepared(slug="delta")
    place.operations.project_root = lambda row: (_ for _ in ()).throw(DomainError("invalid_configuration", "root moved"))
    for phase_name in ("validate", "apply"):
        with pytest.raises(DomainError) as error:
            place.phase(phase_name, refusing)
        assert error.value.code == "invalid_configuration"


# -- the creator and the programs -------------------------------------------------------------------------------------------------------

def test_only_the_creator_callable_decides_and_it_is_consulted_with_the_callers_identity(place):
    assert place.create(ctx=InvocationContext(Principal(INTRUDER, "agent"), source="mcp", profile_revision=1,
                                              global_capabilities=frozenset({COORDINATION_CAPABILITY}))).error["code"] == "forbidden"
    owner = InvocationContext(Principal(HUMAN, "human"), source="dashboard", global_capabilities=frozenset({COORDINATION_CAPABILITY}))
    place.creator.allowed.add(HUMAN)
    assert place.create(ctx=owner).error["code"] == "forbidden"          # a human never creates, whatever the callable would say
    system = InvocationContext(Principal(PROGRAM, "system"), source="worker", global_capabilities=frozenset({COORDINATION_CAPABILITY}))
    place.creator.allowed.add(PROGRAM)
    assert place.create(ctx=system).error["code"] == "forbidden"
    assert place.creator.calls == [INTRUDER]                             # only an agent is ever looked up
    assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug='alpha-project'")[0]["n"] == 0
    # A selected project, or a project home, is not Global: refused by the operation's own access check on every phase.
    business = database_business(place)
    place.creator.calls.clear()
    for selected in (InvocationContext(Principal(CREATOR, "agent"), source="mcp", profile_revision=1, business_id=business,
                                       allowed_business_ids=frozenset({business})),
                     InvocationContext(Principal(CREATOR, "agent"), source="mcp", profile_revision=1, home_business_id=business,
                                       allowed_business_ids=frozenset({business}))):
        with place.database.read() as uow:
            with pytest.raises(DomainError) as error:
                place.operations.access(selected, uow)
        assert error.value.code == "forbidden" and "Global operation" in error.value.message
    assert place.creator.calls == []                                     # refused before the identity is looked up


def database_business(place):
    return place.rows("SELECT id FROM businesses ORDER BY slug LIMIT 1")[0]["id"]


def test_the_creator_is_rechecked_at_every_prepare_validate_and_apply_point(place):
    assert place.create("Alpha Project").status == "ok"
    phases = list(place.creator.phases)
    assert set(phases) == {"prepare", "validate", "apply"} and phases[0] == "prepare" and phases[-1] == "apply"
    assert set(place.creator.calls) == {CREATOR}
    for index, phase in enumerate(phases):                               # revoke at each consultation in turn
        slug = f"revoked-{index}"
        place.creator.calls.clear(), place.creator.phases.clear()
        place.creator.revoke_on = index + 1
        result = place.create("Revoked", slug=slug)
        assert result.status == "error" and result.error["code"] == "forbidden", (index, phase)
        assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug=?", slug)[0]["n"] == 0, (index, phase)
        assert place.creator.phases[-1] == phase                         # it stopped exactly at the revoked point
        if phase == "prepare":
            assert not (place.root / PROJECTS / slug).exists()
        elif (place.root / PROJECTS / slug).exists():                    # after publication only the exact empty layout may exist
            assert place.tree(slug) == ["code", "hq"]
    place.creator.revoke_on = None


def test_validation_and_application_deny_a_creator_revoked_after_preparation(place):
    plan = place.prepared(slug="alpha")
    place.creator.allowed.clear()
    for phase_name in ("validate", "apply"):
        with pytest.raises(DomainError) as error:
            place.phase(phase_name, plan)
        assert error.value.code == "forbidden", phase_name
    assert place.rows("SELECT count(*) AS n FROM businesses WHERE slug='alpha'")[0]["n"] == 0


def test_programs_are_required_revalidated_and_never_created(tmp_path):
    bare = Place(tmp_path / "bare", attach=False)
    result = bare.create("Alpha Project")
    assert result.status == "error" and result.error["code"] == "configuration_required"
    assert "Configure background programs" in result.error["message"]
    place = Place(tmp_path / "attached")
    plan = place.prepared(slug="alpha")
    assert place.programs.required.count(PROGRAM) >= 1
    place.programs.identity = OTHER_PROGRAM                               # the program set changed after preparation
    for phase_name in ("validate", "apply"):
        with pytest.raises(DomainError) as error:
            place.phase(phase_name, plan)
        assert error.value.code == "revision_conflict" and "Background programs changed" in error.value.message
    place.programs.identity = PROGRAM
    principals = place.rows("SELECT count(*) AS n FROM principals")[0]["n"]
    assert place.create("Alpha Project").status == "ok" and place.rows("SELECT count(*) AS n FROM principals")[0]["n"] == principals
    unregistered = Place(tmp_path / "unregistered")
    unregistered.programs.identity = new_id()                             # an unregistered program is refused at preparation
    assert unregistered.create("Alpha Project").error["code"] == "forbidden"


def test_name_and_description_bounds_are_enforced_at_preparation(place):
    assert place.create("   ").error["code"] in {"validation_error", "invalid_arguments"}
    assert place.create("x" * 201).error["code"] == "validation_error"
    assert place.create("Fine", summary="y" * 2001).error["code"] in {"validation_error", "invalid_arguments"}
    assert place.rows("SELECT count(*) AS n FROM businesses")[0]["n"] == 2                                   # only the two seeded rows


def test_slug_derivation_is_deterministic_and_always_valid():
    assert rules.derive_slug("Alpha Project") == "alpha-project"
    assert rules.derive_slug("  Ünïcode  Näme!! ") == "unicode-name"
    assert rules.derive_slug("123 numbers") == "project-123-numbers"
    assert rules.derive_slug("!!!") == "project"
    assert rules.derive_slug("a" * 100) == "a" * 64
    assert rules.numbered("alpha", 2) == "alpha-2" and len(rules.numbered("a" * 64, 12)) <= 64
    for name in ("Alpha Project", "9 lives", "---", "ß", "x" * 90):
        assert rules.SLUG.fullmatch(rules.derive_slug(name))
