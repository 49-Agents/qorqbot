import json

import pytest

from bazarlyk.application.catalog import builtin_registry
from bazarlyk.catalog.packages import load_package
from bazarlyk.catalog.sources import load_sources
from qorqut_operations.contracts import DomainError
from instance_support import PATHS, workspace_root
from test_agent_configuration import role_ready_registry


WORKSPACE = workspace_root()


def test_configured_sources_include_bootstrap_roles_and_valid_packages():
    # Source locations are configurable discovery inputs, not version locks.
    # Published catalogue references pin version/digest independently; source
    # repositories may publish new releases without editing this engine test.
    sources = load_sources(WORKSPACE, paths=PATHS)
    assert {"founder", "business-agent", "global-marketing", "ordubashy"} <= sources.keys()
    registry = role_ready_registry()
    for name, (kind, source, manifest) in sources.items():
        package = load_package(source, manifest, kind, registry)
        assert package.id == name
        assert package.kind == kind


def test_retired_story_question_grants_are_metadata_only():
    registry = builtin_registry()
    for operation in ("stories_question_add", "stories_question_update"):
        assert registry.known(operation)
        with pytest.raises(DomainError) as error:
            registry.get(operation)
        assert error.value.code == "operation_unavailable"


def test_source_map_supports_external_repositories_and_package_subdirectories(tmp_path):
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({
        "reddit-organic": {"kind": "plugin", "path": "../plugins/reddit", "manifest": "plugin.yaml"},
        "business-agent": {"kind": "role", "path": "roles", "manifest": "roles/business-agent/role.yaml"},
    }))
    sources = load_sources(tmp_path / "workspace", config_path=config)
    assert sources["reddit-organic"][1] == tmp_path / "plugins/reddit"
    assert sources["business-agent"][1] == tmp_path / "workspace/roles"


def test_source_map_can_be_selected_by_installation(tmp_path, monkeypatch):
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"business-agent": {"kind": "role", "path": str(tmp_path), "manifest": "role.yaml"}}))
    monkeypatch.setenv("QORQUT_PACKAGE_SOURCES", str(config))
    assert load_sources(tmp_path)["business-agent"] == ("role", tmp_path, "role.yaml")


@pytest.mark.parametrize("value", [[], {}, {"bad": {"kind": "executable", "path": "x", "manifest": "x"}},
    {"bad": {"kind": "plugin", "path": "x", "manifest": "../outside.yaml"}}])
def test_invalid_source_maps_are_rejected(tmp_path, value):
    config = tmp_path / "sources.json"
    config.write_text(json.dumps(value))
    with pytest.raises(DomainError):
        load_sources(tmp_path, config_path=config)


def test_missing_source_map_is_an_explicit_configuration_error(tmp_path):
    with pytest.raises(DomainError) as error:
        load_sources(tmp_path, config_path=tmp_path / "missing.json")
    assert error.value.code == "invalid_configuration"
