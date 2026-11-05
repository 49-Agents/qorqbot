"""Owner-invoked code-folder preparation: owned files only, conflict-safe."""

import json
from pathlib import Path
import subprocess

import pytest

from starlette.testclient import TestClient

from bazarlyk.bootstrap import owner_prepare
from qorqut_operations.contracts import DomainError
from test_owner_session_mcp import call, connect, enable, ok
from test_service_transports import MCP_HEADERS, ServiceFixture

ENDPOINT = "http://testserver"


@pytest.fixture
def service(tmp_path):
    fixture = ServiceFixture(tmp_path)
    enable(fixture.fixture)
    return fixture


def roots(tmp_path):
    """Explicit code roots outside the business workspace."""
    top = tmp_path / "code"
    nested = top / "widget-site"
    nested.mkdir(parents=True)
    return top, nested


def prepare(service, top, **kwargs):
    kwargs.setdefault("endpoint", ENDPOINT)
    kwargs.setdefault("label", "laptop")
    return owner_prepare.prepare(service.fixture.db, business=service.fixture.business_id, code_root=top, **kwargs)


def token_of(root):
    return (root / owner_prepare.CREDENTIAL).read_text().strip()


def refused(client, token):
    result = client.post("/mcp/owner", headers={**MCP_HEADERS, "Authorization": "Bearer " + token},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}})
    assert result.status_code == 401, result.text


def snapshot(*paths):
    return {str(child): child.read_bytes()
            for path in paths for child in sorted(Path(path).rglob("*")) if child.is_file()}


def test_prepare_writes_managed_files_in_every_root(service, tmp_path):
    top, nested = roots(tmp_path)
    business = owner_prepare.resolve_business(service.fixture.db, "widget")
    preview = owner_prepare.build_projection(business=business, endpoint=ENDPOINT,
                                             roots=[top, nested], label="laptop")
    assert list(preview.files) == [top, nested] and snapshot(top) == {}
    assert owner_prepare.CODEX_CONFIG in preview.files[top] and owner_prepare.CODEX_CONFIG in preview.files[nested]
    assert owner_prepare.CREDENTIAL not in preview.files[top]

    result = prepare(service, top, nested=["widget-site"])
    assert result["roots"] == [str(top), str(nested)]
    assert result["launch"]["claude"][0] == f"cd {top} && claude"
    assert result["launch"]["codex"][0] == f"cd {top} && codex"
    for root in (top, nested):
        for name in (owner_prepare.CREDENTIAL, owner_prepare.HEADERS, owner_prepare.REGISTRATION,
                     ".mcp.json", "CLAUDE.md", "AGENTS.md", ".gitignore"):
            assert (root / name).is_file(), (root, name)
        assert (root / owner_prepare.CREDENTIAL).stat().st_mode & 0o777 == 0o600
        assert (root / owner_prepare.HEADERS).stat().st_mode & 0o777 == 0o700
        servers = json.loads((root / ".mcp.json").read_text())["mcpServers"]
        assert servers["bazarlyk-owner"] == {"type": "http", "url": ENDPOINT + "/mcp/owner",
                                             "headersHelper": str(root / owner_prepare.HEADERS)}
        registration = json.loads((root / owner_prepare.REGISTRATION).read_text())
        assert registration["business_id"] == service.fixture.business_id and registration["version"] == 1
        assert registration["endpoint"] == ENDPOINT and registration["label"] == "laptop"
        assert token_of(root) not in (root / owner_prepare.REGISTRATION).read_text()
        assert ".bazarlyk-owner/" in (root / ".gitignore").read_text().splitlines()
        instructions = (root / "CLAUDE.md").read_text()
        assert instructions == (root / "AGENTS.md").read_text()
        assert owner_prepare.required_text(service.fixture.owner.display_name) in instructions
        for expected in ("owner_session_bootstrap", "context_search", "owner_conversation_send",
                         "channel_required", "idempotency key", service.fixture.business_id, ENDPOINT):
            assert expected in instructions
    # Every explicit launch root gets its own project-local Codex config.
    codex = (top / ".codex/config.toml").read_text()
    assert codex.startswith(owner_prepare.CODEX_MARKER)
    assert ENDPOINT + "/mcp/owner" in codex and "http_headers_helper" in codex and "required = true" in codex
    nested_codex = (nested / ".codex/config.toml").read_text()
    assert str(nested / owner_prepare.HEADERS) in nested_codex
    assert token_of(top) == token_of(nested)


def test_managed_lists_cover_everything_preparation_writes(service, tmp_path):
    """Conversion backups capture only these names, so preparation must write nothing else."""
    top, nested = roots(tmp_path)
    prepare(service, top, nested=["widget-site"])
    for root in (top, nested):
        written = [path for path in root.rglob("*") if root == nested or not path.is_relative_to(nested)]
        assert {p.relative_to(root).as_posix() for p in written if p.is_file()} == set(owner_prepare.MANAGED_FILES)
        assert {p.relative_to(root).as_posix() for p in written if p.is_dir()} == set(owner_prepare.MANAGED_DIRECTORIES)


def test_existing_content_is_preserved_and_repreparation_rotates_the_token(service, tmp_path):
    top, nested = roots(tmp_path)
    (top / "CLAUDE.md").write_text("# Widget site\n\nAuthored conventions stay.\n")
    (top / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"type": "stdio"}}, "extra": 1},
                                              indent=2, sort_keys=True) + "\n")
    (top / ".gitignore").write_text(".venv\nnode_modules/\n")
    prepare(service, top, nested=["widget-site"])
    first = token_of(top)
    claude = (top / "CLAUDE.md").read_text()
    assert claude.startswith("# Widget site\n\nAuthored conventions stay.\n")
    document = json.loads((top / ".mcp.json").read_text())
    assert document["extra"] == 1 and document["mcpServers"]["other"] == {"type": "stdio"}
    assert (top / ".gitignore").read_text().splitlines() == [".venv", "node_modules/", ".bazarlyk-owner/"]

    prepare(service, top, nested=["widget-site"])
    second = token_of(top)
    assert second != first and token_of(nested) == second
    again = (top / "CLAUDE.md").read_text()
    assert again.count(owner_prepare.BLOCK_START) == 1 and again.count(owner_prepare.BLOCK_END) == 1
    assert again.startswith("# Widget site\n\nAuthored conventions stay.\n")
    assert list(json.loads((top / ".mcp.json").read_text())["mcpServers"]) == ["bazarlyk-owner", "other"]
    assert (top / ".gitignore").read_text().splitlines() == [".venv", "node_modules/", ".bazarlyk-owner/"]
    with TestClient(service.web) as client:
        refused(client, first)
        headers = connect(client, second)
        assert ok(call(client, headers, "tasks_list", {})).get("tasks", []) == []


def test_conflicts_are_detected_before_any_root_is_written(service, tmp_path):
    top, nested = roots(tmp_path)
    (top / "CLAUDE.md").write_text("Authored.\n")
    (nested / "AGENTS.md").write_text("Authored.\n" + owner_prepare.BLOCK_START + "\nhalf a block\n")
    before = snapshot(top)
    with pytest.raises(DomainError) as conflict:
        prepare(service, top, nested=["widget-site"])
    assert conflict.value.code == "projection_conflict"
    assert snapshot(top) == before
    with service.fixture.db.read() as uow:
        assert uow.connection.execute("SELECT COUNT(*) FROM credentials WHERE principal_id=?",
                                      (service.fixture.owner.id,)).fetchone()[0] == 0


def test_invalid_existing_mcp_json_is_a_conflict(service, tmp_path):
    top, _ = roots(tmp_path)
    (top / ".mcp.json").write_text("{not json")
    with pytest.raises(DomainError) as conflict:
        prepare(service, top)
    assert conflict.value.code == "projection_conflict"
    assert (top / ".mcp.json").read_text() == "{not json"


def test_symlinked_and_escaping_roots_are_rejected(service, tmp_path):
    top, nested = roots(tmp_path)
    link = tmp_path / "linked-code"
    link.symlink_to(top)
    with pytest.raises(DomainError) as refusal:
        prepare(service, link)
    assert refusal.value.code in {"projection_conflict", "validation_error"}
    for bad in ("../elsewhere", str(nested), "/tmp"):
        with pytest.raises(DomainError):
            prepare(service, top, nested=[bad])
    outside = tmp_path / "vendor"
    outside.mkdir()
    with pytest.raises(DomainError):
        prepare(service, top, nested=[str(outside)])
    result = prepare(service, top, outside_roots=[str(outside)])
    assert result["roots"] == [str(top), str(outside)]
    assert (outside / owner_prepare.CREDENTIAL).is_file()
    assert (outside / owner_prepare.CODEX_CONFIG).is_file()


def test_headers_helper_prints_the_credential_header(service, tmp_path):
    top, _ = roots(tmp_path)
    prepare(service, top)
    helper = top / owner_prepare.HEADERS
    result = subprocess.run(["sh", str(helper)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '{"Authorization":"Bearer ' + token_of(top) + '"}'
    assert json.loads(result.stdout)["Authorization"].startswith("Bearer ")


def test_prepared_credential_opens_an_owner_session(service, tmp_path):
    top, nested = roots(tmp_path)
    prepare(service, top, nested=["widget-site"])
    with TestClient(service.web) as client:
        headers = connect(client, token_of(nested))
        assert ok(call(client, headers, "tasks_list", {})).get("tasks", []) == []


def test_remove_restores_foreign_content_and_deletes_owned_files(service, tmp_path):
    top, nested = roots(tmp_path)
    (top / "CLAUDE.md").write_text("# Widget site\n\nAuthored conventions stay.\n")
    (top / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"type": "stdio"}}},
                                              indent=2, sort_keys=True) + "\n")
    (top / ".gitignore").write_text(".venv\n")
    before = snapshot(top)
    prepare(service, top, nested=["widget-site"])
    owner_prepare.remove(code_root=top, nested=["widget-site"])
    assert snapshot(top) == before
    assert not (top / ".bazarlyk-owner").exists() and not (nested / ".bazarlyk-owner").exists()
    assert not (top / ".codex/config.toml").exists()
    assert not (nested / ".codex/config.toml").exists()
    # Files that only existed because of preparation are gone again.
    assert not (nested / "CLAUDE.md").exists() and not (nested / ".mcp.json").exists()
    assert not (nested / ".gitignore").exists() and not (nested / "AGENTS.md").exists()
    owner_prepare.remove(code_root=top, nested=["widget-site"])
    assert snapshot(top) == before


def test_foreign_codex_configuration_is_preserved_and_owned_block_removed(service, tmp_path):
    top, _ = roots(tmp_path)
    (top / ".codex").mkdir()
    original = 'approval_policy = "never"\n\n[profiles.owner]\nmodel = "usual"\n'
    (top / ".codex/config.toml").write_text(original)
    prepare(service, top)
    generated = (top / ".codex/config.toml").read_text()
    assert generated.startswith(original)
    assert owner_prepare.CODEX_MARKER in generated and owner_prepare.CODEX_END in generated
    owner_prepare.remove(code_root=top)
    assert (top / ".codex/config.toml").read_text() == original


@pytest.mark.parametrize("original", [
    'approval_policy = "never"',
    'approval_policy = "never"\n\n\n',
])
def test_codex_removal_restores_original_newline_bytes(service, tmp_path, original):
    top, _ = roots(tmp_path)
    (top / ".codex").mkdir()
    path = top / ".codex/config.toml"
    path.write_bytes(original.encode())
    prepare(service, top)
    owner_prepare.remove(code_root=top)
    assert path.read_bytes() == original.encode()


def test_same_named_foreign_mcp_server_is_a_conflict_and_unchanged(service, tmp_path):
    top, _ = roots(tmp_path)
    original = json.dumps({"mcpServers": {"bazarlyk-owner": {"type": "stdio", "command": "foreign"}}},
                          indent=2, sort_keys=True) + "\n"
    (top / ".mcp.json").write_text(original)
    with pytest.raises(DomainError) as conflict:
        prepare(service, top)
    assert conflict.value.code == "projection_conflict"
    assert (top / ".mcp.json").read_text() == original
    assert not (top / owner_prepare.CREDENTIAL).exists()


def test_foreign_owner_directory_is_a_conflict_and_unchanged(service, tmp_path):
    top, _ = roots(tmp_path)
    managed = top / owner_prepare.DIRECTORY
    managed.mkdir()
    (managed / "credential").write_text("foreign-token\n")
    with pytest.raises(DomainError) as conflict:
        prepare(service, top)
    assert conflict.value.code == "projection_conflict"
    assert (managed / "credential").read_text() == "foreign-token\n"
    assert not (top / ".mcp.json").exists()


def test_preexisting_gitignore_entry_survives_removal(service, tmp_path):
    top, _ = roots(tmp_path)
    path = top / ".gitignore"
    path.write_text(".bazarlyk-owner/\n")
    prepare(service, top)
    owner_prepare.remove(code_root=top)
    assert path.read_text() == ".bazarlyk-owner/\n"


def test_owned_gitignore_entry_survives_reprepare_until_removal(service, tmp_path):
    top, _ = roots(tmp_path)
    prepare(service, top)
    prepare(service, top)
    assert ".bazarlyk-owner/" in (top / ".gitignore").read_text().splitlines()
    owner_prepare.remove(code_root=top)
    assert not (top / ".gitignore").exists()


def test_issue_and_install_bundle_are_independent_of_database(service, tmp_path):
    top, nested = roots(tmp_path)
    bundle = tmp_path / "transfer" / "owner-session.json"
    bundle.parent.mkdir()
    result = owner_prepare.issue_bundle(service.fixture.db, business="widget", endpoint=ENDPOINT,
                                        label="remote-laptop", output=bundle)
    assert result["bundle"] == str(bundle)
    assert bundle.stat().st_mode & 0o777 == 0o600
    payload = json.loads(bundle.read_text())
    assert payload["version"] == 1 and payload["business"]["id"] == service.fixture.business_id
    assert "token" not in result and str(top) not in bundle.read_text()
    installed = owner_prepare.install_bundle(bundle, code_root=top, nested=["widget-site"])
    assert installed["business_id"] == service.fixture.business_id
    assert token_of(top) == payload["token"] == token_of(nested)
    # A copied bundle can target a different local tree without opening the DB.
    remote = tmp_path / "remote tree with spaces"
    remote_nested = remote / "nested checkout"
    remote_nested.mkdir(parents=True)
    owner_prepare.install_bundle(bundle, code_root=remote, nested=["nested checkout"])
    assert token_of(remote_nested) == payload["token"]
    assert str(top) not in (remote / owner_prepare.REGISTRATION).read_text()


def test_bundle_output_collision_does_not_revoke_existing_credential(service, tmp_path):
    bundle = tmp_path / "owner-session.json"
    bundle.write_text("keep me\n")
    with service.fixture.db.read() as uow:
        before = uow.connection.execute(
            "SELECT COUNT(*) FROM credentials WHERE principal_id=? AND revoked_at IS NULL",
            (service.fixture.owner.id,)).fetchone()[0]
    with pytest.raises(DomainError) as conflict:
        owner_prepare.issue_bundle(service.fixture.db, business="widget", endpoint=ENDPOINT,
                                   label="collision", output=bundle)
    assert conflict.value.code == "projection_conflict"
    assert bundle.read_text() == "keep me\n"
    with service.fixture.db.read() as uow:
        after = uow.connection.execute(
            "SELECT COUNT(*) FROM credentials WHERE principal_id=? AND revoked_at IS NULL",
            (service.fixture.owner.id,)).fetchone()[0]
    assert after == before


def test_revoke_invalidates_the_prepared_credential(service, tmp_path):
    top, _ = roots(tmp_path)
    prepare(service, top)
    with TestClient(service.web) as client:
        connect(client, token_of(top))
        result = owner_prepare.revoke(service.fixture.db, business="widget", label="laptop")
        assert result["revoked"] == 1
        refused(client, token_of(top))


def test_cli_prepares_and_prints_launch_instructions(service, tmp_path, capsys):
    top, nested = roots(tmp_path)
    assert owner_prepare.main(["prepare", "--businesses-root", str(service.fixture.root),
                               "--business", "widget", "--root", str(top), "--nested", "widget-site",
                               "--endpoint", ENDPOINT, "--label", "cli"]) == 0
    printed = capsys.readouterr().out
    assert f"cd {top} && claude" in printed
    assert f"cd {top} && codex" in printed
    assert str(nested) in printed
    assert (nested / owner_prepare.CREDENTIAL).is_file()
    with TestClient(service.web) as client:
        connect(client, token_of(top))
    assert owner_prepare.main(["remove", "--root", str(top), "--nested", "widget-site"]) == 0
    assert not (top / ".bazarlyk-owner").exists()
