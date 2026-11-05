"""The contract between the snapshot engine and the host callables that tell it where things are.

The engine takes ``locations() -> (operations_root, history_repository)``, ``resolve_areas(validated areas) -> resolved
areas`` and the host's safe-segment validator instead of any path object. These checks use synthetic callables, never a
host: they prove the engine asks again on every use, keeps its own generic validation, refuses a resolver that breaks the
contract, and imports without a host or Context.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from history_snapshot_support import (ACME_AGENT, GLOBAL_AGENT, REF, SAFE_SEGMENT, build, commits, git, locations_of,
                                      resolver_of, tree, write)
from qorqut_operations.contracts import DomainError
from qorqut_operations.history_exports import (BusinessAgentFiles, BusinessHeadquarters, GlobalAgentFiles, GlobalFiles,
                                               History, ResolvedArea)
from qorqut_operations.history_exports import _areas

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path):
    context = build(tmp_path)
    yield context
    context.database.close()


def engine(env, *, locations=None, resolve_areas=None, safe_segment=None, **keywords):
    """The engine over the instance's synthetic callables, any of which a test replaces."""
    return History(env.database, locations=locations or locations_of(env.layout),
                   resolve_areas=resolve_areas or resolver_of(env.layout),
                   safe_segment=safe_segment or SAFE_SEGMENT,
                   **{"catalog": env.catalog, "areas": env.areas, "clock": env.clock, "git_runner": env.runner, **keywords})


def test_locations_and_area_resolution_are_asked_again_on_every_use(env):
    asked = {"locations": 0, "resolve": 0}
    locations, resolve = locations_of(env.layout), resolver_of(env.layout)

    def counted_locations():
        asked["locations"] += 1
        return locations()

    def counted_resolve(areas):
        asked["resolve"] += 1
        return resolve(areas)

    history = engine(env, locations=counted_locations, resolve_areas=counted_resolve)
    assert asked == {"locations": 0, "resolve": 0}  # construction asks nothing
    history.initialize()
    history.snapshot()
    first = dict(asked)
    assert first["resolve"] == 1 and first["locations"] >= 3
    history.snapshot()
    history.status()
    history.validate()
    assert asked["resolve"] == 2 and asked["locations"] > first["locations"]


def test_a_location_the_host_refuses_is_unavailable_everywhere_and_touches_nothing(env):
    def refused():
        raise DomainError("history_unavailable", "The history location is not available", details={"reason": "unsafe_path"})

    history = engine(env, locations=refused)
    for operation in (history.initialize, history.snapshot, history.validate):
        with pytest.raises(DomainError) as error:
            operation()
        assert error.value.code == "history_unavailable"
    status = history.status()
    assert status["state"] == "unavailable" and status["error"] == {"code": "history_unavailable"}
    assert status["repository"] is None
    assert not env.runtime.joinpath("history.git").exists() and not list(env.runtime.glob("history*"))


def test_locations_that_are_not_a_root_and_a_repository_directly_inside_its_runtime_are_unavailable(env, tmp_path):
    cases = {
        "not_a_pair": lambda: env.operations,
        "wrong_depth": lambda: (env.operations, env.operations / ".runtime" / "deeper" / "history.git"),
        "outside_root": lambda: (env.operations, tmp_path / "elsewhere" / ".runtime" / "history.git"),
        "relative_root": lambda: (Path("operations"), Path("operations") / ".runtime" / "history.git"),
    }
    for name, locations in cases.items():
        history = engine(env, locations=locations)
        for operation in (history.initialize, history.snapshot, history.validate):
            with pytest.raises(DomainError) as error:
                operation()
            assert error.value.code == "history_unavailable", name
        assert history.status()["state"] == "unavailable", name
    assert not (tmp_path / "elsewhere").exists() and not env.repository.exists()


def test_the_host_validator_is_the_only_naming_rule_the_engine_applies(env):
    env.history.initialize()
    first = env.history.snapshot()
    names = []

    def only_acme(value):
        names.append(value)
        return value == "acme"

    stricter = engine(env, safe_segment=only_acme, areas=lambda uow: [BusinessHeadquarters("acme"), GlobalAgentFiles("ok")])
    with pytest.raises(DomainError) as error:
        stricter.snapshot()
    assert error.value.code == "history_areas_invalid" and "ok" in names
    assert git(env, "rev-parse", REF).decode().strip() == first["commit"]

    # The engine defines no rule of its own: what the host's validator accepts is selected, however unusual.
    lenient = engine(env, safe_segment=lambda value: True, areas=lambda uow: [GlobalAgentFiles("a b")],
                     resolve_areas=lambda areas: tuple(ResolvedArea.located(area, ("agents", "a b", "files")) for area in areas))
    assert lenient.snapshot()["areas"] == ["global_agent_files:a b"]


def test_a_resolver_that_breaks_its_contract_fails_the_snapshot_without_committing(env):
    write(env, "files/notes/plan.md", "authored")
    env.history.initialize()
    first = env.history.snapshot()
    write(env, "files/notes/plan.md", "edited")
    real = resolver_of(env.layout)
    cases = {
        "dropped_area": lambda areas: real(areas)[1:],
        "reordered": lambda areas: tuple(reversed(real(areas))),
        "invented_area": lambda areas: (*real(areas), ResolvedArea.located(GlobalFiles(), ("files",))),
        "untyped": lambda areas: tuple({"area": area} for area in areas),
        "not_iterable": lambda areas: 7,
        "wrong_area": lambda areas: tuple(ResolvedArea.located(BusinessHeadquarters("acme"), ("projects", "acme", "hq"))
                                           for _ in areas),
    }
    for name, resolver in cases.items():
        with pytest.raises(DomainError) as error:
            engine(env, resolve_areas=resolver).snapshot()
        assert error.value.code == "history_areas_invalid", name
        assert git(env, "rev-parse", REF).decode().strip() == first["commit"], name
    assert env.history.snapshot()["changed"] is True


def test_overlapping_resolved_folders_fail_the_whole_selection_case_folded(env):
    env.history.initialize()
    first = env.history.snapshot()

    def overlapping(areas):
        placed = {"global_files": ("files",), "business_hq:acme": ("Files", "hq")}
        return tuple(ResolvedArea.located(area, placed[_areas.label_of(area)]) for area in areas)

    history = engine(env, areas=lambda uow: [GlobalFiles(), BusinessHeadquarters("acme")], resolve_areas=overlapping)
    with pytest.raises(DomainError) as error:
        history.snapshot()
    assert error.value.code == "history_areas_invalid" and error.value.message == "Areas overlap"
    assert git(env, "rev-parse", REF).decode().strip() == first["commit"] and len(commits(env)) == 1


def test_resolved_areas_carry_the_registered_selection_never_a_path_and_normalize_exclusions():
    area = BusinessHeadquarters("acme", excluded=("exports", "context/email"))
    placed = ResolvedArea.located(area, ("projects", "acme", "hq"))
    assert placed.label == "business_hq:acme" and placed.parts == ("projects", "acme", "hq") and placed.blocked is None
    assert placed.excluded == frozenset({"projects/acme/hq/exports", "projects/acme/hq/context/email"})
    refused = ResolvedArea.refused(area)
    assert refused.parts is None and refused.blocked == "symlink" and refused.excluded == frozenset()
    with pytest.raises(DomainError) as error:
        ResolvedArea.located(BusinessHeadquarters("acme", excluded=("../up",)), ("projects", "acme", "hq"))
    assert error.value.code == "history_areas_invalid"


def test_selection_validation_is_generic_and_sorted_by_label():
    valid = _areas.validate([BusinessHeadquarters("beta"), GlobalFiles(), BusinessAgentFiles("acme", ACME_AGENT),
                             GlobalAgentFiles(GLOBAL_AGENT)], lambda value: True)
    assert [_areas.label_of(area) for area in valid] == sorted(_areas.label_of(area) for area in valid)
    with pytest.raises(DomainError):
        _areas.validate([BusinessHeadquarters("Acme"), BusinessHeadquarters("acme")], lambda value: True)  # case-folded twin
    with pytest.raises(DomainError):
        _areas.validate([GlobalAgentFiles(7)], lambda value: True)  # a non-string never reaches the host validator


def test_the_engine_snapshots_the_registered_areas_through_synthetic_callables_only(env):
    write(env, "files/notes/plan.md", "global")
    write(env, "projects/acme/hq/h.md", "hq")
    write(env, f"projects/acme/agents/{ACME_AGENT}/files/n.md", "agent")
    env.history.initialize()
    files = set(tree(env, env.history.snapshot()["commit"]))
    assert {"files/notes/plan.md", "projects/acme/hq/h.md", f"projects/acme/agents/{ACME_AGENT}/files/n.md"} <= files


def test_the_snapshot_engine_imports_without_a_host_or_context(tmp_path):
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "import qorqut_operations.history_exports as exports\n"
        "for name in ('History', 'HistoryObserver', 'ResolvedArea', 'GlobalFiles', 'BusinessHeadquarters'):\n"
        "    assert hasattr(exports, name), name\n"
        "loaded = [name for name in sys.modules if name.split('.')[0] in ('bazarlyk', 'qorqut_context', 'qorqut_paper', 'qorqut_content')]\n"
        "assert not loaded, loaded\n"
        "print('independent')\n")
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "GIT_"))}
    result = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, cwd=tmp_path, env=environment)
    assert result.returncode == 0 and result.stdout.strip() == "independent", result.stderr
