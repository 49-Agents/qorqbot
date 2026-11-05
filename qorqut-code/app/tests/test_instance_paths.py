"""Pure path fixture for the explicit instance layout; every value is synthetic and no service is started."""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from bazarlyk.instance_paths import COMPONENTS, InstancePathError, InstancePaths


def snapshot(root):
    return sorted((str(path.relative_to(root)), path.is_symlink()) for path in Path(root).rglob("*"))


@pytest.fixture
def layout(tmp_path):
    root = tmp_path.resolve()
    instance, code = root / "instance", root / "code"
    components = {"app": code / "app", "operations_engine": code / "qorqut-operations",
                  "context_engine": code / "qorqut-context", "dashboard": code / "biz-dashboard",
                  "roles": instance / "qorqut-roles" / "agent-roles"}
    return instance, components, InstancePaths(instance, components)


def test_the_documented_locations_derive_from_the_explicit_instance_root(layout):
    instance, components, paths = layout
    assert paths.instance_root == instance
    assert paths.operations == instance / "operations"
    assert paths.database == instance / "operations/.runtime/operations.sqlite3"
    assert paths.context == instance / "operations/context-graph"
    assert paths.context_runtime == instance / "operations/.runtime/context"
    assert paths.overseer_archive == instance / "operations/.runtime/overseer/archive"
    assert paths.history == instance / "operations/.runtime/history.git"
    assert paths.files == instance / "operations/files"
    assert paths.roles == instance / "qorqut-roles"
    assert {name: paths.component(name) for name in COMPONENTS} == components
    assert dict(paths.components) == components


def test_overseer_archive_resolution_does_not_create_or_follow_symlinks(layout, tmp_path):
    instance, _, paths = layout
    assert not paths.overseer_archive.exists()
    parent = instance / "operations/.runtime"
    parent.mkdir(parents=True)
    (parent / "overseer").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(InstancePathError):
        _ = paths.overseer_archive


def test_global_and_business_agents_resolve_to_distinct_registered_folders(layout):
    instance, _, paths = layout
    agent = str(uuid4())
    other = str(uuid4())
    assert paths.agent_workspace(agent) == instance / "operations/agents" / agent
    assert paths.agent_files(agent) == instance / "operations/agents" / agent / "files"
    assert paths.agent_transcripts(agent) == instance / "operations/agents" / agent / "transcripts"
    business = instance / "operations/projects/acme-shop"
    assert paths.business_root("acme-shop") == business
    assert paths.business_hq("acme-shop") == business / "hq"
    assert paths.business_code("acme-shop") == business / "code"
    assert paths.agent_workspace(agent, business_folder="acme-shop") == business / "agents" / agent
    assert paths.agent_files(agent, business_folder="acme-shop") == business / "agents" / agent / "files"
    assert paths.agent_transcripts(agent, business_folder="acme-shop") == business / "agents" / agent / "transcripts"
    assert len({paths.agent_workspace(agent), paths.agent_workspace(agent, business_folder="acme-shop"),
                paths.agent_workspace(other, business_folder="acme-shop"),
                paths.agent_workspace(agent, business_folder="other-shop")}) == 4


def test_display_names_and_tags_never_select_or_move_an_agent_folder(layout):
    instance, _, paths = layout
    registry = {str(uuid4()): "Ordubashy", str(uuid4()): "Founder"}
    before = {agent_id: paths.agent_workspace(agent_id, business_folder="acme-shop") for agent_id in registry}
    for agent_id in registry:
        registry[agent_id] = "Renamed " + registry[agent_id] + " / ../x"
    assert {agent_id: paths.agent_workspace(agent_id, business_folder="acme-shop") for agent_id in registry} == before
    for name in registry.values():
        with pytest.raises(InstancePathError):
            paths.agent_workspace(name)
    with pytest.raises(InstancePathError):
        paths.business_root("Acme Shop")


def test_registered_identifier_lengths_are_preserved(layout):
    _, _, paths = layout
    identifier = str(uuid4())
    assert len(identifier) == 36 and paths.agent_workspace(identifier).name == identifier
    assert paths.agent_workspace("a" * 128).name == "a" * 128
    with pytest.raises(InstancePathError):
        paths.agent_workspace("a" * 129)
    assert paths.business_root("b" * 128).name == "b" * 128


@pytest.mark.parametrize("segment", ["", ".", "..", "../x", "x/..", "a/b", "/abs", "\\x", "a\\b", ".hidden", "-x", "a\0b",
                                      "x\n", " x", "x y", "nämé", 7, Path("x")])
def test_folder_and_identity_segments_must_be_one_safe_name(layout, segment):
    _, _, paths = layout
    for call in (lambda: paths.business_root(segment), lambda: paths.business_hq(segment),
                 lambda: paths.business_code(segment), lambda: paths.agent_workspace(segment),
                 lambda: paths.agent_files(segment, business_folder="acme"),
                 lambda: paths.agent_transcripts(str(uuid4()), business_folder=segment)):
        with pytest.raises(InstancePathError):
            call()


def test_a_previous_businesses_folder_is_never_probed_or_adopted(layout):
    instance, _, paths = layout
    (instance / "operations/businesses/acme-shop/hq").mkdir(parents=True)
    assert paths.business_root("acme-shop") == instance / "operations/projects/acme-shop"
    assert paths.business_hq("acme-shop") == instance / "operations/projects/acme-shop/hq"


def test_symlinks_cannot_move_a_business_or_agent_folder_out_of_the_instance(layout, tmp_path):
    instance, _, paths = layout
    outside = tmp_path / "outside"
    (instance / "operations/projects").mkdir(parents=True)
    (instance / "operations/agents").mkdir()
    outside.mkdir()
    (instance / "operations/projects/evil").symlink_to(outside)
    with pytest.raises(InstancePathError):
        paths.business_root("evil")
    (instance / "operations/projects/real/agents").parent.mkdir()
    (instance / "operations/projects/real/agents").symlink_to(outside)
    with pytest.raises(InstancePathError):
        paths.agent_workspace("agent-1", business_folder="real")
    (instance / "operations/projects/real/hq").symlink_to(outside)
    with pytest.raises(InstancePathError):
        paths.business_hq("real")
    agent = str(uuid4())
    (instance / "operations/agents" / agent).symlink_to(outside)
    with pytest.raises(InstancePathError):
        paths.agent_workspace(agent)
    other = str(uuid4())
    (instance / "operations/agents" / other).mkdir()
    (instance / "operations/agents" / other / "files").symlink_to(outside)
    with pytest.raises(InstancePathError):
        paths.agent_files(other)
    assert paths.agent_workspace(other) == instance / "operations/agents" / other


def test_a_symlinked_operations_or_roles_root_is_refused(layout, tmp_path):
    instance, components, _ = layout
    outside = tmp_path / "outside"
    outside.mkdir()
    instance.mkdir()
    (instance / "operations").symlink_to(outside)
    with pytest.raises(InstancePathError, match="symlink"):
        InstancePaths(instance, components)
    (instance / "operations").unlink()
    (instance / "qorqut-roles").symlink_to(outside)
    with pytest.raises(InstancePathError, match="symlink"):
        InstancePaths(instance, components)


@pytest.mark.parametrize("when", ["before_construction", "after_construction"])
@pytest.mark.parametrize("link,refused", [
    (".runtime", {"database", "context_runtime", "history"}),
    (".runtime/operations.sqlite3", {"database"}),
    (".runtime/history.git", {"history"}),
    ("context-graph", {"context"}),
    ("context", set()),                    # the Global authored tree is not the engine root
    ("files", {"files"})])
def test_operational_roots_refuse_symlinks_when_selected_whether_made_before_or_after_construction(layout, tmp_path, link, refused, when):
    instance, components, _ = layout
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (instance / "operations").mkdir(parents=True)

    def introduce():
        (instance / "operations" / link).parent.mkdir(parents=True, exist_ok=True)
        (instance / "operations" / link).symlink_to(outside)

    if when == "before_construction":
        introduce()
    paths = InstancePaths(instance, components)
    if when == "after_construction":
        introduce()
    for name in ("database", "context", "context_runtime", "history", "files"):
        if name in refused:
            with pytest.raises(InstancePathError, match="symlink"):
                getattr(paths, name)
        else:
            assert str(getattr(paths, name)).startswith(str(instance / "operations"))
    assert paths.operations == instance / "operations" and paths.roles == instance / "qorqut-roles"


def test_a_symlinked_operations_or_roles_root_introduced_after_construction_is_refused_when_selected(layout, tmp_path):
    instance, _, paths = layout
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    instance.mkdir()
    (instance / "operations").symlink_to(outside)
    for name in ("operations", "database", "context", "context_runtime", "history", "files"):
        with pytest.raises(InstancePathError, match="symlink"):
            getattr(paths, name)
    with pytest.raises(InstancePathError):
        paths.business_root("acme-shop")
    (instance / "operations").unlink()
    assert paths.database == instance / "operations/.runtime/operations.sqlite3"
    (instance / "qorqut-roles").symlink_to(outside)
    with pytest.raises(InstancePathError, match="symlink"):
        paths.roles
    with pytest.raises(InstancePathError):
        paths.role_source(instance / "qorqut-roles/agent-roles")
    assert paths.files == instance / "operations/files"


@pytest.mark.parametrize("name,where", [
    ("app", "instance/operations/app"), ("dashboard", "instance/operations"), ("dashboard", "instance"),
    ("app", "instance/operations/.runtime/code"), ("context_engine", ""), ("app", "instance/qorqut-roles/app"),
    ("operations_engine", "instance/qorqut-roles")])
def test_code_components_must_be_separate_from_operations_roles_and_the_instance(layout, tmp_path, name, where):
    instance, components, _ = layout
    with pytest.raises(InstancePathError):
        InstancePaths(instance, {**components, name: tmp_path.resolve() / where if where else tmp_path.resolve()})


def test_the_roles_component_is_separate_from_every_code_component_in_both_directions(layout, tmp_path):
    instance, components, _ = layout
    source = tmp_path.resolve() / "src"
    app = source / "app"
    with pytest.raises(InstancePathError, match="separate from the roles component"):
        InstancePaths(instance, {"app": app, "roles": app / "roles"})
    with pytest.raises(InstancePathError, match="separate from the roles component"):
        InstancePaths(instance, {"app": app, "roles": source})
    with pytest.raises(InstancePathError, match="separate from the roles component"):
        InstancePaths(instance, {"dashboard": source / "roles" / "dashboard", "roles": source / "roles"})
    with pytest.raises(InstancePathError, match="separate from the roles component"):
        InstancePaths(instance, {**components, "roles": components["dashboard"] / "roles"})
    installed = tmp_path.resolve() / "artifacts/roles-1"
    external = InstancePaths(instance, {**components, "roles": installed})
    assert external.component("roles") == installed
    assert InstancePaths(instance, {"app": app, "roles": source / "app-roles"}).component("roles") == source / "app-roles"
    assert InstancePaths(instance, {"app": app}).component("app") == app


def test_the_instance_root_cannot_live_inside_a_component_or_equal_one(layout):
    instance, components, _ = layout
    with pytest.raises(InstancePathError):
        InstancePaths(components["app"] / "operations-here", components)
    with pytest.raises(InstancePathError):
        InstancePaths(instance, {**components, "dashboard": instance})
    with pytest.raises(InstancePathError):
        InstancePaths(Path("/"), {})


def test_duplicate_relative_traversing_unknown_and_missing_components_fail_clearly(layout):
    instance, components, paths = layout
    with pytest.raises(InstancePathError, match="same location"):
        InstancePaths(instance, {**components, "context_engine": components["dashboard"]})
    with pytest.raises(InstancePathError, match="absolute"):
        InstancePaths(instance, {**components, "app": Path("app")})
    with pytest.raises(InstancePathError, match="absolute"):
        InstancePaths("relative/instance", components)
    with pytest.raises(InstancePathError, match="traversal"):
        InstancePaths(instance, {**components, "app": components["app"] / ".." / "other"})
    with pytest.raises(InstancePathError, match="unknown components: biz-core"):
        InstancePaths(instance, {**components, "biz-core": components["app"]})
    with pytest.raises(InstancePathError):
        InstancePaths(instance, ["app"])
    with pytest.raises(InstancePathError, match="unknown component"):
        paths.component("biz-core")
    partial = InstancePaths(instance, {"app": components["app"]})
    assert partial.component("app") == components["app"]
    with pytest.raises(InstancePathError, match="dashboard is not configured"):
        partial.component("dashboard")
    # The separate business-template component is gone: it is neither a known component nor accepted in a selection.
    assert "template" not in COMPONENTS
    with pytest.raises(InstancePathError, match="unknown components: template"):
        InstancePaths(instance, {**components, "template": components["app"].parent / "biz-template"})
    with pytest.raises(InstancePathError, match="unknown component"):
        paths.component("template")


def test_nothing_defaults_from_the_working_directory(layout, tmp_path, monkeypatch):
    instance, components, _ = layout
    monkeypatch.chdir(tmp_path)
    with pytest.raises(InstancePathError, match="absolute"):
        InstancePaths(Path(instance.name), components)
    with pytest.raises(TypeError):
        InstancePaths(components)
    with pytest.raises(TypeError):
        InstancePaths(instance)


def test_role_sources_are_inside_the_instance_roles_root_or_the_explicit_roles_component(layout, tmp_path):
    instance, components, paths = layout
    installed = tmp_path.resolve() / "artifacts/roles-1"
    assert paths.role_source(instance / "qorqut-roles/agent-roles/roles/founder") == instance / "qorqut-roles/agent-roles/roles/founder"
    with pytest.raises(InstancePathError):
        paths.role_source(installed)
    with pytest.raises(InstancePathError):
        paths.role_source(instance / "operations/agents/role")
    with pytest.raises(InstancePathError):
        paths.role_source("agent-roles")
    external = InstancePaths(instance, {**components, "roles": installed})
    assert external.role_source(installed / "roles/founder") == installed / "roles/founder"
    assert external.role_source(instance / "qorqut-roles/agent-roles") == instance / "qorqut-roles/agent-roles"
    with pytest.raises(InstancePathError):
        external.role_source(tmp_path / "artifacts/roles-2")
    outside = tmp_path / "outside"
    outside.mkdir()
    (instance / "qorqut-roles").mkdir(parents=True)
    (instance / "qorqut-roles/link").symlink_to(outside)
    with pytest.raises(InstancePathError):
        paths.role_source(instance / "qorqut-roles/link")


def test_construction_and_derivation_write_nothing(tmp_path):
    root = tmp_path.resolve() / "absent"
    components = {name: tmp_path.resolve() / "src" / name for name in COMPONENTS if name != "roles"}
    before = snapshot(tmp_path)
    paths = InstancePaths(root, components)
    for name in ("instance_root", "operations", "database", "context", "context_runtime", "history", "files", "roles", "components"):
        getattr(paths, name)
    paths.business_root("x"), paths.business_hq("x"), paths.business_code("x")
    paths.agent_workspace("a"), paths.agent_files("a", business_folder="x"), paths.agent_transcripts("a")
    paths.role_source(paths.roles / "agent-roles")
    repr(paths), hash(paths)
    assert snapshot(tmp_path) == before and not root.exists() and not paths.operations.exists()


def test_paths_are_immutable_comparable_values(layout):
    instance, components, paths = layout
    with pytest.raises(AttributeError):
        paths.operations = instance
    with pytest.raises(AttributeError):
        paths.extra = 1
    with pytest.raises(AttributeError):
        del paths.components
    with pytest.raises(TypeError):
        paths.components["app"] = instance
    components["app"] = instance / "mutated-after-construction-is-not-visible"
    assert paths.component("app") != components["app"]
    assert paths == InstancePaths(instance, {name: paths.component(name) for name in COMPONENTS})
    assert hash(paths) == hash(InstancePaths(instance, dict(paths.components)))
    assert paths != InstancePaths(instance.parent / "other", dict(paths.components))


def test_roots_under_a_symlinked_parent_are_normalized_to_their_real_location(tmp_path):
    real = tmp_path.resolve() / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    paths = InstancePaths(tmp_path / "link" / "instance", {"app": tmp_path / "link" / "code" / "app"})
    assert paths.instance_root == real / "instance" and paths.component("app") == real / "code" / "app"
    assert paths.agent_workspace("a") == real / "instance/operations/agents/a"
    assert os.path.realpath(paths.agent_workspace("a")) == str(paths.agent_workspace("a"))


def test_fixed_global_mapping_preserves_identity_and_business_paths(layout):
    import json
    instance, _, paths = layout
    names = ('ordubashy', 'learning-applier', 'founder', 'marketing')
    mapping = {str(uuid4()): name for name in names}
    (instance / 'config').mkdir(parents=True)
    (instance / 'config/global-agent-workspaces.json').write_text(json.dumps({'version': 1, 'agents': mapping}))
    for identity, name in mapping.items():
        assert paths.agent_workspace(identity) == instance / 'operations/agents' / name
        assert paths.agent_files(identity) == instance / 'operations/agents' / name / 'files'
        assert paths.agent_transcripts(identity) == instance / 'operations/agents' / name / 'transcripts'
        assert paths.agent_workspace(identity, business_folder='acme') == instance / 'operations/projects/acme/agents' / identity
    unknown = str(uuid4())
    assert paths.agent_workspace(unknown).name == unknown


@pytest.mark.parametrize('mapping', [
    {'not-an-id': 'ordubashy'}, {str(uuid4()): '../outside'},
    {str(uuid4()): 'custom-agent'}, {str(uuid4()): 'ordubashy', str(uuid4()): 'ordubashy'},
    {str(uuid4()): 'strategist'}, {str(uuid4()): 'Marketing'},
])
def test_fixed_global_mapping_rejects_invalid_entries(layout, mapping):
    import json
    instance, _, paths = layout
    (instance / 'config').mkdir(parents=True)
    (instance / 'config/global-agent-workspaces.json').write_text(json.dumps({'version': 1, 'agents': mapping}))
    with pytest.raises(InstancePathError, match='mapping'):
        paths.agent_workspace(str(uuid4()))


def test_fixed_global_mapping_refuses_symlinked_configuration(layout, tmp_path):
    instance, _, paths = layout
    (instance / 'config').mkdir(parents=True)
    target = tmp_path / 'other-config.json'
    target.write_text('{"version": 1, "agents": {}}')
    (instance / 'config/global-agent-workspaces.json').symlink_to(target)
    with pytest.raises(InstancePathError, match='symlink'):
        paths.agent_workspace(str(uuid4()))
