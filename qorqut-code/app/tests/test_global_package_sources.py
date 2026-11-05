"""The checked-in package source map names only the supported global roles and the two retained plugins.

Paths are relative to an explicit installation workspace and carry no repository ownership or URL. Read-only: the map in this
checkout and the test workspace's role-source copy; nothing is published, installed or configured."""

import json

from bazarlyk.agents.roles import SUPPORTED_GLOBAL_ROLES
from qorqut_operations.storage.structured import parse_yaml
from host_foundation_support import APP, roles_source

PLUGINS = {"agent-terminal-control", "email-management"}


def test_role_sources_are_exactly_the_supported_globals_and_exist_in_the_role_source():
    entries = json.loads((APP / "package-sources.json").read_text())
    roles = {name: entry for name, entry in entries.items() if entry["kind"] == "role"}
    assert set(roles) == SUPPORTED_GLOBAL_ROLES
    assert {name for name, entry in entries.items() if entry["kind"] == "plugin"} == PLUGINS
    source = roles_source()
    for name, entry in roles.items():
        assert (entry["path"], entry["manifest"]) == ("qorqut-agentic/src/qorqut_agentic/resources/defaults", f"roles/imported-roles/native/roles/{name}/role.yaml")
        manifest = parse_yaml((source / entry["manifest"]).read_text())
        assert (manifest["id"], manifest["scope_kinds"], manifest["scope_brief"]) == (name, ["ecosystem"], "ecosystem-agent")


def test_the_worker_source_sits_beside_the_other_role_sources_and_entries_carry_only_local_locations():
    entries = json.loads((APP / "package-sources.json").read_text())
    assert entries["global-worker"] == {"kind": "role", "path": "qorqut-agentic/src/qorqut_agentic/resources/defaults", "manifest": "roles/imported-roles/native/roles/global-worker/role.yaml"}
    assert {key: entries["global-worker"][key] for key in ("kind", "path")} == {key: entries["ordubashy"][key] for key in ("kind", "path")}
    assert all(set(entry) == {"kind", "path", "manifest"} for entry in entries.values())       # no repository ownership metadata or URL
    assert entries["agent-terminal-control"]["path"] == "qorqut-code/app"
    assert entries["email-management"]["path"] == "dependencies/email-management"
