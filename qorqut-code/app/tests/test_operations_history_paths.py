"""The host's path adapters for the Operations snapshot engine, on synthetic instances with real ``InstancePaths``.

The engine itself (capture, Git, status, observer) is tested in the Operations package. These checks cover what the host
supplies: locations and area resolution through ``InstancePaths`` (its symlink and traversal refusals applied again on every
call, its registered-identity mapping, its error translation), the host's safe-segment rule, the registered-area provider
and the stock projection that is part of the host catalog. Each test builds a temporary explicit instance, a real Operations
store with synthetic registrations and rows, and its own private history repository.
"""
import json
import os

import pytest

from bazarlyk.instance_paths import InstancePaths
from bazarlyk.operations_history.composition import area_resolver, history_over, locations_of, registered_areas
from qorqut_operations.contracts import DomainError
from qorqut_operations.history_exports import (BusinessAgentFiles, BusinessHeadquarters, ExportCatalog, GlobalAgentFiles,
                                               GlobalFiles, ResolvedArea)
from test_operations_history_support import (ACME_AGENT, BETA_AGENT, GLOBAL_AGENT, REF, SECRET, blob, build, commits,
                                             everything, git, rest_classified, tree, write)


def test_historical_comment_without_relay_kind_exports_without_private_provenance():
    from bazarlyk.operations_history.projections import attribution
    field = attribution()
    projected = field.project(json.dumps({'migration': {'source_id': 'historical-comment', 'private': SECRET}}))
    checked = field.schema.check(projected, 'attribution_json')
    assert checked == dict.fromkeys(('kind', 'owner_id', 'agent_id', 'statement_text', 'report_text'))
    assert SECRET not in json.dumps(checked)


@pytest.fixture
def env(tmp_path):
    context = build(tmp_path)
    yield context
    context.database.close()


def test_locations_are_the_instance_paths_validated_again_on_every_call(env, tmp_path):
    locations = locations_of(env.paths)
    assert locations() == (env.paths.operations, env.paths.history) == (env.operations, env.repository)

    moved, decoy = tmp_path / "moved-runtime", tmp_path / "decoy-runtime"
    decoy.mkdir()
    env.runtime.rename(moved)
    env.runtime.symlink_to(decoy)  # the link appears after the callable was made
    with pytest.raises(DomainError) as error:
        locations()
    assert error.value.code == "history_unavailable" and error.value.details == {"reason": "unsafe_path"}
    assert error.value.message == "The history location is not available"
    env.runtime.unlink()
    moved.rename(env.runtime)
    assert locations() == (env.operations, env.repository)  # nothing was remembered, nothing stays refused


def test_area_resolver_places_each_registered_kind_below_the_operations_root(env):
    resolve = area_resolver(env.paths)
    selection = (GlobalFiles(excluded=("projection",)), GlobalAgentFiles(GLOBAL_AGENT),
                 BusinessAgentFiles("acme", ACME_AGENT), BusinessHeadquarters("acme", excluded=("exports", "context/email")))
    resolved = resolve(selection)
    assert [item.parts for item in resolved] == [
        ("files",), ("agents", GLOBAL_AGENT, "files"), ("projects", "acme", "agents", ACME_AGENT, "files"),
        ("projects", "acme", "hq")]
    assert resolved[0].excluded == frozenset({"files/projection"})
    assert resolved[3].excluded == frozenset({"projects/acme/hq/exports", "projects/acme/hq/context/email"})
    assert all(isinstance(item, ResolvedArea) and item.blocked is None for item in resolved)


def test_area_resolver_uses_the_fixed_global_agent_folder_mapping(env):
    mapped = "00000000-0000-4000-8000-0000000000c1"
    (env.root / "config").mkdir()
    (env.root / "config" / "global-agent-workspaces.json").write_text(
        json.dumps({"version": 1, "agents": {mapped: "ordubashy"}}))
    resolved = area_resolver(env.paths)((GlobalAgentFiles(mapped), GlobalAgentFiles(GLOBAL_AGENT)))
    assert [item.parts for item in resolved] == [("agents", "ordubashy", "files"), ("agents", GLOBAL_AGENT, "files")]

    # An invalid mapping is not hidden behind a default folder: that area is refused and none of it is read.
    (env.root / "config" / "global-agent-workspaces.json").write_text("{not json")
    (refused,) = area_resolver(env.paths)((GlobalAgentFiles(GLOBAL_AGENT),))
    assert (refused.parts, refused.blocked) == (None, "symlink")


def test_area_resolver_refuses_a_symlinked_folder_as_a_blocked_area_each_time(env, tmp_path):
    outside = tmp_path / "outside"
    (outside / "hq").mkdir(parents=True)
    resolve = area_resolver(env.paths)
    selection = (BusinessHeadquarters("acme"), BusinessAgentFiles("acme", ACME_AGENT), GlobalFiles())
    assert all(item.blocked is None for item in resolve(selection))

    (env.operations / "projects").mkdir()
    os.symlink(outside, env.operations / "projects" / "acme")  # appears after construction and after the first resolution
    resolved = resolve(selection)
    assert [(item.label, item.parts, item.blocked) for item in resolved] == [
        ("business_hq:acme", None, "symlink"), (f"business_agent_files:acme:{ACME_AGENT}", None, "symlink"),
        ("global_files", ("files",), None)]
    assert all(item.excluded == frozenset() for item in resolved[:2])


def test_history_over_needs_the_explicit_instance_paths_and_the_host_segment_rule(env):
    with pytest.raises(TypeError):
        history_over(env.database, str(env.root), catalog=env.catalog, areas=registered_areas, clock=env.clock)
    with pytest.raises(TypeError):
        env.another(catalog=[])
    unsafe = {
        "traversal": lambda uow: [BusinessHeadquarters("../escape")],
        "separator_agent": lambda uow: [GlobalAgentFiles("a/b")],
        "hidden_segment": lambda uow: [GlobalAgentFiles(".hidden")],
        "empty": lambda uow: [BusinessAgentFiles("acme", "")],
        "too_long": lambda uow: [BusinessHeadquarters("a" * 129)],
        "not_a_string": lambda uow: [GlobalAgentFiles(7)],
    }
    env.history.initialize()
    first = env.history.snapshot()
    for name, provider in unsafe.items():
        with pytest.raises(DomainError) as error:
            env.another(areas=provider).snapshot()
        assert error.value.code == "history_areas_invalid", name
        assert git(env, "rev-parse", REF).decode().strip() == first["commit"], name
    # The longest accepted segment is the host's own bound.
    longest = env.another(areas=lambda uow: [BusinessHeadquarters("a" * 128)])
    assert longest.snapshot()["areas"] == ["business_hq:" + "a" * 128]


def test_missing_operations_root_and_symlinked_repository_are_unavailable_untouched(env, tmp_path):
    nowhere = InstancePaths(tmp_path / "nowhere", {})
    absent = history_over(env.database, nowhere, catalog=env.catalog, areas=registered_areas, clock=env.clock)
    for operation in (absent.initialize, absent.snapshot, absent.validate):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code == "history_unavailable"
    assert not (tmp_path / "nowhere").exists()
    assert absent.status()["state"] == "unavailable"

    other = build(tmp_path / "linked", seed=False)
    (tmp_path / "elsewhere").mkdir()
    other.repository.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(DomainError) as error:
        other.history.initialize()
    assert error.value.code == "history_unavailable" and os.path.islink(other.repository)
    assert other.history.status()["state"] == "unavailable" and other.history.status()["repository"] is None
    assert not list((tmp_path / "elsewhere").iterdir())
    other.database.close()


def test_replaced_runtime_root_is_refused_not_followed_before_or_during_an_operation(env, tmp_path):
    env.history.initialize()
    first = env.history.snapshot()

    # Replaced before the operation: InstancePaths refuses the linked location and nothing follows it.
    moved = tmp_path / "moved-runtime"
    decoy = tmp_path / "decoy-runtime"
    decoy.mkdir()
    env.runtime.rename(moved)
    env.runtime.symlink_to(decoy)
    for operation in (env.history.snapshot, env.history.validate, env.history.initialize):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code in {"history_unavailable", "history_incompatible"}
    assert not list(decoy.iterdir()) and env.history.status()["state"] == "unavailable"
    env.runtime.unlink()
    moved.rename(env.runtime)
    assert env.history.snapshot()["changed"] is False

    # Replaced DURING a snapshot, after every path was validated: the per-command guard refuses the swapped location
    # even though the link points at the real repository (a realpath() approach would have followed it silently).
    write(env, "files/notes/plan.md", "changed so a commit is needed")

    def swap(name, command, kwargs):
        if name == "ls-tree" and not swap.done:
            swap.done = True
            env.runtime.rename(moved)
            env.runtime.symlink_to(moved)
    swap.done = False
    env.runner.intercepts.append(swap)
    with pytest.raises(DomainError) as error:
        env.history.snapshot()
    env.runner.intercepts.clear()
    assert error.value.code == "history_incompatible"
    env.runtime.unlink()
    moved.rename(env.runtime)
    assert git(env, "rev-parse", REF).decode().strip() == first["commit"] and len(commits(env)) == 1
    assert env.history.snapshot()["changed"] is True


def test_linked_project_folders_are_reported_as_symlink_and_none_of_them_is_read(env, tmp_path):
    outside = tmp_path / "outside"
    (outside / "hq").mkdir(parents=True)
    (outside / "outside.txt").write_text("TOPSECRET outside the authored areas")
    (outside / "hq" / "leak.md").write_text("TOPSECRET through a linked business folder")
    write(env, "files/ok.md", "fine")
    (env.operations / "projects").mkdir()
    os.symlink(outside, env.operations / "projects/acme")
    env.history.initialize()

    summary = env.history.snapshot()
    assert summary["files_complete"] and summary["incomplete_total"] == 0
    assert {item["path"]: item["reason"] for item in summary["exclusions"]} == {
        "business_hq:acme": "symlink",  # the linked business folder cannot be resolved; none of it is read
        "business_agent_files:acme:" + ACME_AGENT: "symlink"}
    assert {path for path in tree(env) if not path.startswith("database/")} == {"files/ok.md"}
    assert b"TOPSECRET" not in everything(env)


def test_symlinked_area_ancestors_are_never_followed_even_when_the_link_appears_later(env, tmp_path):
    outside = tmp_path / "outside-agents"
    (outside / ACME_AGENT / "files").mkdir(parents=True)
    (outside / ACME_AGENT / "files" / "leak.md").write_text("TOPSECRET via a linked agents folder")
    write(env, "files/ok.md", "fine")
    env.history.initialize()
    env.history.snapshot()
    shared = env.operations / "projects" / "acme" / "agents"
    shared.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, shared)
    summary = env.history.snapshot()
    assert b"TOPSECRET" not in everything(env)
    assert {"path": f"business_agent_files:acme:{ACME_AGENT}", "reason": "symlink"} in summary["exclusions"]
    assert not any(path.startswith("projects/acme/agents") for path in tree(env, summary["commit"]))


def test_registered_areas_come_from_the_current_registrations_with_the_host_exclusions(env):
    with env.database.read() as uow:
        areas = registered_areas(uow)
    by_label = {(area.kind, *area.identifiers): area for area in areas}
    assert set(by_label) == {("global_files",), ("global_agent_files", GLOBAL_AGENT),
                             ("business_agent_files", "acme", ACME_AGENT), ("business_agent_files", "beta", BETA_AGENT),
                             ("business_hq", "acme"), ("business_hq", "beta")}
    assert by_label[("business_hq", "acme")].excluded == (
        "exports", "tables", "context/email", "context/agent-memory", "agents", "code", "sessions", "transcripts",
        "logs", "browser")
    assert by_label[("global_files",)].excluded == () and by_label[("business_agent_files", "acme", ACME_AGENT)].excluded == ()


def test_host_history_keeps_notes_without_capturing_browser_sessions(env):
    env.catalog = ExportCatalog(rest_classified(env.database))
    marker = b"browser-session-private-sentinel"
    write(env, "projects/acme/hq/browser/profile/Default/Network/Cookies", marker)
    write(env, "projects/acme/hq/browser/profile/Default/Login Data", marker)
    write(env, "projects/acme/hq/notes.md", "retained work")
    history = env.another(areas=registered_areas)
    history.initialize()
    snapshot = history.snapshot()

    assert snapshot["complete"] is True
    assert "projects/acme/hq/notes.md" in tree(env)
    assert not any(path.startswith("projects/acme/hq/browser/") for path in tree(env))
    assert marker not in everything(env)
    assert blob(env, "projects/acme/hq/notes.md") == b"retained work"
