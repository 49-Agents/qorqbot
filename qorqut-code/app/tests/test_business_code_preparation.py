from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tomllib

import pytest

from bazarlyk.bootstrap.preparation import _business_code_root, build_projection
from bazarlyk.bootstrap.codex_config import configuration
from qorqut_operations.contracts import DomainError
from test_agent_configuration import Fixture


def test_business_code_permissions_follow_the_role(tmp_path):
    fixture = Fixture(tmp_path)
    agent = fixture.create()
    coordinator = fixture.create("ecosystem", "ordubashy")
    marketing = fixture.create("marketing", "global-marketing")
    code = fixture.hq.parent / "code"
    assert not code.exists()
    preview = build_projection(fixture.resolver, fixture.resolver.resolve_agent_config(agent),
        registration_id="preview-only", endpoint="http://127.0.0.1:9988", interpreter=sys.executable)
    assert preview.metadata["business_code_root"] == str(code)
    assert not code.exists()
    for principal in (agent, coordinator, marketing):
        ready = fixture.prepare(principal)
        assert ready["ready"]
        workspace = Path(ready["workspace"])
        local = json.loads((workspace / ".claude/settings.local.json").read_text())
        codex = tomllib.loads((workspace / ".codex/config.toml").read_text())
        allowed = principal in (agent, coordinator)
        if principal == agent:
            filesystem = codex["permissions"]["qorqut-agent"]["filesystem"]
            assert {path for path, mode in filesystem.items() if mode == "write"} == {str(workspace / "work"), "/tmp", str(code)}
            assert filesystem[str(fixture.root / ".bazarlyk")] == "deny"
            assert filesystem[str(workspace / ".bazarlyk/credential")] == "deny"
            assert filesystem[str(workspace / "agent.yaml")] == "read"
            assert filesystem[str(fixture.hq / "context")] == "read"
        else:
            assert codex["default_permissions"] == ":danger-full-access"
            assert "permissions" not in codex
        assert (f"Edit(/{code}/**)" in local["permissions"]["allow"]) == allowed
        assert local["permissions"].get("additionalDirectories", []) == ([str(code)] if allowed else [])
        assert "Bash" in local["permissions"]["allow"]
        assert codex["approval_policy"] == "never"
        for name in ("CLAUDE.md", "AGENTS.md"):
            assert (str(code) in (workspace / name).read_text()) == allowed
        for directory in (".claude/skills", ".agents/skills"):
            assert (workspace / directory / "bazarlyk-role-business-operations/SKILL.md").is_file() == allowed
    assert code.is_dir()
    context = fixture.resolver.resolve_agent_config(agent).to_dict()["mandatory_context"]["rendered"]
    assert "Ownership boundary for software and design work: each business's Dev agent builds" in context

    fixture.update(coordinator, {"role": fixture.refs["founder"]})
    ready = fixture.prepare(coordinator)
    workspace = Path(ready["workspace"])
    local = json.loads((workspace / ".claude/settings.local.json").read_text())
    assert "additionalDirectories" not in local["permissions"]
    assert f"Edit(/{code}/**)" not in local["permissions"]["allow"]
    for directory in (".claude/skills", ".agents/skills"):
        assert not (workspace / directory / "bazarlyk-role-business-operations").exists()
    assert code.is_dir()  # Revocation never deletes the business's work.


@pytest.mark.parametrize("invalid", ["outside", "root", "relative", "traversal", "wrong_hq", "symlink", "parent_symlink", "file"])
def test_business_code_rejects_unsafe_roots(tmp_path, invalid):
    root = tmp_path / "businesses"
    base = root / "widget"
    base.mkdir(parents=True)
    hq = base / "hq"
    if invalid == "outside":
        base = tmp_path / "outside"
    elif invalid == "root":
        base = root
    elif invalid == "relative":
        base = Path("relative")
    elif invalid == "traversal":
        base = root / "widget/../other"
    elif invalid == "symlink":
        (base / "code").symlink_to(tmp_path, target_is_directory=True)
    elif invalid == "parent_symlink":
        alias = root / "alias"
        alias.symlink_to(base, target_is_directory=True)
        base = alias
    elif invalid == "file":
        (base / "code").write_text("not a directory")
    if invalid != "wrong_hq":
        hq = base / "hq"
    else:
        hq = root / "other/hq"
    with pytest.raises(DomainError, match="Project code directory"):
        _business_code_root(SimpleNamespace(root=root),
            {"role": {"id": "business-agent"}, "scope": {"kind": "business"}},
            {"workspace": str(base), "hq_path": str(hq)})


def test_business_code_uses_registered_workspace_not_display_name(tmp_path):
    resolver = SimpleNamespace(root=tmp_path)
    data = {"role": {"id": "business-agent"}, "scope": {"kind": "business"}, "name": "another-business"}
    for slug in ("first", "second"):
        base = tmp_path / slug
        assert _business_code_root(resolver, data,
            {"workspace": str(base), "hq_path": str(base / "hq")}) == base / "code"
    data["scope"]["kind"] = "ecosystem"
    assert _business_code_root(resolver, data, None) is None


def test_codex_protected_paths_take_precedence_over_extra_write_roots(tmp_path):
    protected, denied = tmp_path / "protected", tmp_path / "denied"
    body, _ = configuration(workspace=tmp_path, endpoint="http://127.0.0.1:9988",
        header_command="synthetic", command="synthetic", snapshot_command="synthetic",
        protected=[protected], read_denied=[denied], write_roots=[protected, denied])
    filesystem = tomllib.loads(body.decode())["permissions"]["qorqut-agent"]["filesystem"]
    assert filesystem[str(protected)] == "read"
    assert filesystem[str(denied)] == "deny"
