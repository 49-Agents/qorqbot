"""Acceptance case 5: global and business registrations resolve through the one explicit ``InstancePaths``.

A business is located by its registered folder (slug) and an agent by its registered identity; display names, the
stored ``workspace``/``hq_path`` columns and any older root never select a location. Businesses have no resident agents: the
registered global agent's workspace is global and it reaches every active business, but file targets cannot leave their
scope root, and one scope can never address another's files."""

import pytest
from qorqut_operations.contracts import DomainError, Principal

from bazarlyk.composition import locations
from host_foundation_support import (agent_context, build_instance, create_registered_global, historical_business_agent,
                                     register_sources, tree)


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    for slug in ("first", "second"):
        register_sources(instance, ids[slug], slug)
    agent = create_registered_global(instance, "ordubashy")       # the coordinator role, for ordinary business work
    instance.runtime.freeze_registry()
    return instance, ids, agent, tmp_path


def business_row(instance, business):
    with instance.database.read() as uow:
        return dict(uow.repository("businesses").get(business, required=True))


def file_target(instance, business_id, path, document="d"):
    store = instance.runtime.domains.documents.store
    return instance.runtime.domains.resolve_file_target(store.file_ref(document, path, business_id)).resolve()


def test_locations_follow_registered_folder_and_identity_and_survive_renames(host):
    instance, ids, agent, _ = host
    paths, first = instance.paths, ids["first"]
    row = business_row(instance, first)
    workspace = paths.operations / "agents" / agent                  # a global agent's workspace is global: no business home
    assert locations.agent_workspace(paths, agent) == workspace
    assert locations.agent_workspace(paths, agent, row) == paths.operations / "projects" / "first" / "agents" / agent
    assert locations.business_hq(paths, row) == paths.operations / "projects" / "first" / "hq"
    assert instance.runtime.resolver.profile(Principal(agent, "agent"))[1]["workspace"] == str(workspace)

    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET name='Totally Different Co' WHERE id=?", (first,))
        uow.connection.execute("UPDATE principals SET display_name='Another Name' WHERE id=?", (agent,))
    renamed = business_row(instance, first)
    assert renamed["name"] == "Totally Different Co"
    assert locations.agent_workspace(paths, agent) == workspace
    assert locations.business_hq(paths, renamed) == paths.operations / "projects" / "first" / "hq"
    assert instance.runtime.resolver.profile(Principal(agent, "agent"))[1]["workspace"] == str(workspace)

    # A stored workspace that is not the registered identity's location is a registration conflict, not a location.
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET workspace=? WHERE agent_id=?",
                               (str(paths.instance_root / "elsewhere"), agent))
    with pytest.raises(DomainError) as refused:
        instance.runtime.resolver.profile(Principal(agent, "agent"))
    assert refused.value.code == "registration_conflict"


def test_no_older_root_is_probed_for_a_registered_location(host):
    instance, ids, agents, tmp_path = host
    paths, first = instance.paths, ids["first"]
    row = business_row(instance, first)
    for older in (paths.operations / "businesses" / "first" / "hq", paths.instance_root / "businesses" / "first" / "hq",
                  paths.instance_root / "first" / "hq",
                  tmp_path / "workspace" / "businesses" / "first" / "hq"):
        older.mkdir(parents=True)
        (older / "legacy.md").write_text("older root content")
    hq = locations.business_hq(paths, row)
    assert hq == paths.operations / "projects" / "first" / "hq" and not (hq / "legacy.md").exists()
    assert file_target(instance, first, "legacy.md") == hq / "legacy.md" and not (hq / "legacy.md").exists()
    assert locations.document_path_at(paths, row, "projects/first/hq/legacy.md") == "legacy.md"
    assert locations.document_path_at_global(paths, "projects/first/hq/legacy.md") is None
    assert locations.document_path_at_global(paths, ".runtime/operations.sqlite3") is None


@pytest.mark.parametrize("path", ["../escape.md", "notes/../../escape.md", "/etc/passwd", "notes\\..\\escape.md", ""])
def test_a_document_target_cannot_leave_its_scope_root(host, path):
    instance, ids, _, _ = host
    instance.paths.business_hq("first").mkdir(parents=True, exist_ok=True)
    with pytest.raises(DomainError) as refused:
        file_target(instance, ids["first"], path)
    assert refused.value.code in {"validation_error", "invalid_plan"}


def test_a_symlink_inside_a_scope_root_is_never_followed(host):
    instance, ids, _, tmp_path = host
    hq, outside = instance.paths.business_hq("first"), tmp_path / "outside"
    outside.mkdir()
    hq.mkdir(parents=True, exist_ok=True)
    (hq / "notes").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError) as refused:
        file_target(instance, ids["first"], "notes/leak.md")
    assert refused.value.code == "forbidden"
    assert tree(outside) == []


def test_a_business_folder_that_is_a_symlink_is_refused_and_nothing_is_written_through_it(host):
    instance, ids, agent, tmp_path = host
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = instance.paths.operations / "projects" / "second"
    folder.rename(tmp_path / "moved-second")
    folder.symlink_to(outside, target_is_directory=True)

    with pytest.raises(DomainError) as refused:
        locations.business_hq(instance.paths, business_row(instance, ids["second"]))
    assert refused.value.code == "invalid_configuration"
    # A global agent's own notes live in its global workspace; a business-owned record is written below the business folder.
    note = instance.invoke(instance.human(ids["second"]), "notes_create", {
        "title": "Through a link", "body": "must not land", "target_business_id": ids["second"]})
    assert note.status == "error"
    assert tree(outside) == []


def test_one_scope_cannot_address_another_scopes_files(host):
    instance, ids, agent, _ = host
    first, second = ids["first"], ids["second"]
    # Businesses have no resident agents: the first business's own file is a business-owned record (a global agent's notes
    # live in its global workspace).
    written = instance.invoke(instance.human(first), "notes_create", {
        "title": "Private to first", "body": "first only", "target_business_id": first})
    assert written.status == "ok", written.error
    document = written.data["document"]
    assert (instance.paths.business_hq("first") / document["path"]).is_file()

    # The global agent reaches both businesses, but selecting the second never addresses the first's files.
    for ctx in (agent_context(agent, second), instance.human(second)):
        other = instance.invoke(ctx, "notes_get", {"document_id": document["id"]})
        assert other.status == "error" and other.error["code"] == "not_found"
    # A historical business-home identity of the second business addresses nothing, in either business.
    historical = historical_business_agent(instance, second)
    for business in (first, second):
        spoofed = instance.invoke(agent_context(historical, business), "notes_get", {"document_id": document["id"]})
        assert spoofed.status == "error" and spoofed.error["code"] == "inactive_agent"

    # The same registered relative path names a different file in every scope; none can resolve into another.
    in_first = file_target(instance, first, document["path"])
    in_second = file_target(instance, second, document["path"])
    in_global = file_target(instance, None, document["path"])
    assert len({in_first, in_second, in_global}) == 3
    assert in_first.is_file() and not in_second.exists() and not in_global.exists()
    assert in_global == instance.paths.operations / document["path"]
