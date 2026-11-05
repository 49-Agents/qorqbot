"""Explicit component selection for host commands: launch inputs, missing configuration and no silent overrides."""

import argparse
import logging
from types import SimpleNamespace

import pytest

from bazarlyk.browser.access import BusinessBrowserAccess
from bazarlyk.catalog.sources import load_sources
from qorqut_operations.contracts import DomainError
from bazarlyk.installation import app_manifest
from bazarlyk.instance_paths import InstancePathError, InstancePaths
from bazarlyk.service.instance_arguments import add_instance_arguments, instance_arguments, instance_paths_from_arguments
from bazarlyk.service.main import main
from bazarlyk.service.runtime import build_runtime


def parse(*arguments):
    parser = argparse.ArgumentParser()
    add_instance_arguments(parser)
    return parser.parse_args(list(arguments))


@pytest.fixture
def selection(tmp_path):
    tmp_path = tmp_path.resolve()
    return tmp_path, {"app": tmp_path / "code/app", "dashboard": tmp_path / "code/biz-dashboard"}


def launch(root, components, **omit):
    return ["--instance-root", str(root / "instance"),
            *[part for name, path in components.items() if name not in omit for part in ("--component", f"{name}={path}")]]


def test_explicit_launch_inputs_build_the_instance_paths_and_round_trip_to_child_arguments(selection):
    root, components = selection
    paths = instance_paths_from_arguments(parse(*launch(root, components)), required=("app", "dashboard"))
    assert paths.instance_root == root / "instance" and dict(paths.components) == components
    assert paths.database == root / "instance/operations/.runtime/operations.sqlite3"
    assert instance_paths_from_arguments(parse(*instance_arguments(paths)), required=("app",)) == paths


def test_parsing_and_construction_create_nothing_and_ignore_the_environment_and_working_directory(selection, monkeypatch):
    root, components = selection
    monkeypatch.chdir(root)
    monkeypatch.setenv("BIZ_ROOT", str(root / "ignored"))
    monkeypatch.setenv("QORQUT_APP_MANIFEST", str(root / "ignored.json"))
    with pytest.raises(DomainError, match="--instance-root"):
        instance_paths_from_arguments(parse(), required=())
    with pytest.raises(DomainError, match="absolute"):
        instance_paths_from_arguments(parse("--instance-root", "instance", "--component", f"app={components['app']}"), required=())
    instance_paths_from_arguments(parse(*launch(root, components)), required=("app", "dashboard"))
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("selection_args,message", [
    (["--component", "app"], "NAME=ABSOLUTE_PATH"),
    (["--component", "=/x"], "NAME=ABSOLUTE_PATH"),
    (["--component", "app="], "NAME=ABSOLUTE_PATH"),
    (["--component", "core=/x"], "unknown components: core"),
    (["--component", "template=/x/biz-template"], "unknown components: template"),
    (["--component", "app=relative/app"], "absolute"),
    (["--component", "app=/x/a", "--component", "app=/x/b"], "more than once"),
])
def test_malformed_unknown_relative_and_duplicate_components_are_refused(selection, selection_args, message):
    root, _ = selection
    with pytest.raises(DomainError, match=message) as error:
        instance_paths_from_arguments(parse("--instance-root", str(root / "instance"), *selection_args), required=())
    assert error.value.code == "invalid_configuration"


def test_required_components_must_be_supplied_and_unsafe_overlaps_are_refused(selection):
    root, components = selection
    with pytest.raises(DomainError, match="for: dashboard"):
        instance_paths_from_arguments(parse(*launch(root, components, dashboard=1)), required=("app", "dashboard"))
    instance_paths_from_arguments(parse(*launch(root, components, dashboard=1)), required=("app",))
    inside = {**components, "dashboard": root / "instance/operations/dashboard"}
    with pytest.raises(DomainError, match="separate from the operations root"):
        instance_paths_from_arguments(parse(*launch(root, inside)), required=("dashboard",))


def test_service_start_refuses_missing_workspace_or_instance_selection_before_touching_anything(selection, caplog):
    root, components = selection
    base = []
    with caplog.at_level(logging.ERROR):
        assert main(base) == 1
        assert main([*base, "--workspace", str(root / "workspace")]) == 1
        assert main([*base, "--workspace", str(root / "workspace"), *launch(root, components, dashboard=1)]) == 1
    assert "--workspace" in caplog.text and "--instance-root" in caplog.text and "dashboard" in caplog.text
    assert list(root.iterdir()) == []


def test_runtime_assembly_requires_the_explicit_instance_paths(tmp_path):
    with pytest.raises(DomainError) as error:
        build_runtime(tmp_path, paths=tmp_path)
    assert error.value.code == "invalid_configuration"
    with pytest.raises(TypeError, match="paths"):
        build_runtime(tmp_path)


def test_the_default_package_source_map_comes_from_the_explicit_app_component(selection, monkeypatch):
    root, components = selection
    monkeypatch.delenv("QORQUT_PACKAGE_SOURCES", raising=False)
    (components["app"]).mkdir(parents=True)
    (components["app"] / "package-sources.json").write_text(
        '{"business-agent": {"kind": "role", "path": "qorqut-roles/agent-roles", "manifest": "roles/business-agent/role.yaml"}}')
    paths = InstancePaths(root / "instance", components)
    assert load_sources(root / "workspace", paths=paths)["business-agent"][1] == root / "workspace/qorqut-roles/agent-roles"
    with pytest.raises(DomainError, match="source map") as missing:
        load_sources(root / "workspace")
    assert missing.value.code == "invalid_configuration"
    with pytest.raises(InstancePathError, match="app is not configured"):
        load_sources(root / "workspace", paths=InstancePaths(root / "instance", {"dashboard": components["dashboard"]}))


def test_browser_runtime_fallback_uses_the_app_component_and_keeps_explicit_native_selections(selection, monkeypatch, tmp_path):
    root, components = selection
    for name in ("QORQUT_APP_MANIFEST", "QORQUT_INSTALLATION_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    app_manifest.cache_clear()
    access = BusinessBrowserAccess(SimpleNamespace(paths=InstancePaths(root / "instance", components), browser_turns=None))
    assert access.runtime_directory() == str(components["app"] / "tools/browser")
    config = tmp_path / "installation.json"
    config.write_text(f'{{"format": 1, "root": "{root}", "browser_runtime": "{root}/pinned-browser"}}')
    monkeypatch.setenv("QORQUT_INSTALLATION_CONFIG", str(config))
    assert access.runtime_directory() == f"{root}/pinned-browser"
    app_manifest.cache_clear()
