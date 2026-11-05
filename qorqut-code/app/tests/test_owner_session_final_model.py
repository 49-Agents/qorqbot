"""Owner sessions in the one-business-agent model: bootstrap identities, instructions, preparation."""

import json
import re

import pytest

from starlette.testclient import TestClient

from bazarlyk import owner_sessions
from bazarlyk.bootstrap import owner_prepare
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from bazarlyk.owner_bootstrap import NAME
from test_owner_private_conversations import create_ordubashy
from test_owner_session_mcp import call, code, connect, enable, issue, ok
from test_service_transports import MCP_HEADERS, ServiceFixture

ENDPOINT = "http://testserver"
OWNER_NAME = "Ayla Demir"
RETIRED = re.compile(r"onbashy|strategist|\bcoder\b|outreach[- ]specialist|\bAlp\b", re.IGNORECASE)
CONFIRMATION = re.compile(r"wait for|explicit approval|proposed change|second confirmation is required", re.IGNORECASE)


def set_coordinator(fixture, identity):
    with fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (identity,))


@pytest.fixture
def full(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    with service.fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE principals SET display_name=? WHERE id=?", (OWNER_NAME, service.fixture.owner.id))
    enable(service.fixture)
    service.token = issue(service.fixture)
    return service


@pytest.fixture
def bootstrap(full):
    """One client per app: the MCP session manager starts once."""
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        yield lambda: ok(call(client, headers, NAME, {}))


def test_coordinator_reasons(full, bootstrap):
    fixture = full.fixture
    set_coordinator(fixture, None)
    coordination = bootstrap()["coordination"]
    assert (coordination["coordinator_id"], coordination["available"], coordination["reason"]) == (
        None, False, "not_configured")
    ordu = create_ordubashy(fixture, [fixture.business_id])
    set_coordinator(fixture, ordu.id)
    coordination = bootstrap()["coordination"]
    assert (coordination["coordinator_id"], coordination["coordinator_name"], coordination["available"],
            coordination["reason"]) == (ordu.id, "Ordu", True, None)
    with fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE principal_id=? AND business_id=?",
                               (ordu.id, fixture.business_id))
    coordination = bootstrap()["coordination"]
    assert (coordination["coordinator_id"], coordination["coordinator_name"], coordination["available"],
            coordination["reason"]) == (None, None, False, "inactive_or_out_of_scope")


def test_generated_instructions_describe_the_final_model_without_confirmation_or_names(full, tmp_path):
    top = tmp_path / "code"
    top.mkdir()
    owner_prepare.prepare(full.fixture.db, business="widget", code_root=top, endpoint=ENDPOINT, label="laptop")
    text = (top / "CLAUDE.md").read_text()
    assert text == (top / "AGENTS.md").read_text()
    assert f"on behalf of {OWNER_NAME} (the installation owner)" in text
    assert f"Software and design building stays with {OWNER_NAME}" in text
    assert "create these tasks" in text and "close this Story" in text and "unrelated Qorqut writes" in text
    assert "materially different action" in text and "owner/Ordubashy private conversation" in text
    for operation in ("tasks_complete", "stories_close", "stories_reopen", "notes_list", "channel_required"):
        assert operation in text
    assert not RETIRED.search(text) and not CONFIRMATION.search(text)
    # Without a registered name the text names the role, never a person.
    assert owner_prepare.required_text(None).startswith("You act on behalf of the installation owner in")
    assert owner_prepare.owner_label("x <!-- y") is None and owner_prepare.owner_label("  A  B ") == "A B"


def test_bundle_carries_the_owner_name_and_old_bundles_still_install(full, tmp_path):
    top = tmp_path / "code"
    top.mkdir()
    bundle = tmp_path / "transfer" / "owner.json"
    bundle.parent.mkdir()
    owner_prepare.issue_bundle(full.fixture.db, business="widget", endpoint=ENDPOINT, label="remote", output=bundle)
    payload = json.loads(bundle.read_text())
    assert payload["owner_name"] == OWNER_NAME
    owner_prepare.install_bundle(bundle, code_root=top)
    assert f"on behalf of {OWNER_NAME} (the installation owner)" in (top / "CLAUDE.md").read_text()
    legacy = tmp_path / "transfer" / "legacy.json"
    legacy.write_text(json.dumps({key: value for key, value in payload.items() if key != "owner_name"}))
    legacy.chmod(0o600)
    other = tmp_path / "other"
    other.mkdir()
    owner_prepare.install_bundle(legacy, code_root=other)
    assert "on behalf of the installation owner in" in (other / "CLAUDE.md").read_text()
    forged = tmp_path / "transfer" / "forged.json"
    forged.write_text(json.dumps({**payload, "owner_name": "x\n" + owner_prepare.BLOCK_END}))
    forged.chmod(0o600)
    with pytest.raises(DomainError) as invalid:
        owner_prepare.install_bundle(forged, code_root=other)
    assert invalid.value.code == "invalid_bundle"


def test_nested_root_repreparation_preserves_unrelated_configuration(full, tmp_path):
    top = tmp_path / "code"
    nested = top / "landing page"
    (nested / ".codex").mkdir(parents=True)
    authored = {
        "CLAUDE.md": "# Landing\n\nUse pnpm.\n",
        "AGENTS.md": "# Agents\n\nKeep the design tokens.\n",
        ".mcp.json": json.dumps({"mcpServers": {"figma": {"type": "http", "url": "http://figma.local"}},
                                 "note": "authored"}, indent=2) + "\n",
        ".codex/config.toml": 'model = "usual"\n\n[mcp_servers.figma]\nurl = "http://figma.local"\n',
        ".gitignore": "node_modules/\n",
    }
    for name, content in authored.items():
        (nested / name).write_text(content)
    for _ in range(2):
        owner_prepare.prepare(full.fixture.db, business="widget", code_root=top, nested=["landing page"],
                              endpoint=ENDPOINT, label="laptop")
        for name in ("CLAUDE.md", "AGENTS.md"):
            text = (nested / name).read_text()
            assert text.startswith(authored[name]) and text.count(owner_prepare.BLOCK_START) == 1
        mcp = json.loads((nested / ".mcp.json").read_text())
        assert mcp["note"] == "authored" and set(mcp["mcpServers"]) == {"figma", owner_prepare.SERVER}
        assert (nested / ".codex/config.toml").read_text().startswith(authored[".codex/config.toml"])
    token = (nested / owner_prepare.CREDENTIAL).read_text().strip()
    with TestClient(full.web) as client:
        connect(client, token)
    owner_prepare.remove(code_root=top, nested=["landing page"])
    for name, content in authored.items():
        assert (nested / name).read_text() == content


def test_agent_credential_retirement_never_touches_owner_session_credentials(full, tmp_path):
    fixture = full.fixture
    ordu = create_ordubashy(fixture, [fixture.business_id])
    fixture.prepare(ordu)
    with fixture.db.read() as uow:
        agent_credentials = [row["id"] for row in uow.connection.execute(
            "SELECT id FROM credentials WHERE principal_id=? AND revoked_at IS NULL", (ordu.id,))]
        revision = uow.repository("agent_profiles", business_id=None).get(ordu.id)["revision"]
        plan = fixture.ops.prepare("agents_deactivate", fixture.ctx, {"agent_id": ordu.id},
                                   InvocationOptions(expected_revision=revision, idempotency_key=new_id()), uow)
    assert agent_credentials
    with fixture.db.transaction() as uow:
        fixture.ops.apply(fixture.ctx, plan, uow)
    with fixture.db.read() as uow:
        rows = uow.connection.execute("SELECT principal_id, metadata_json, revoked_at FROM credentials").fetchall()
    owner_rows = [row for row in rows if owner_sessions.credential_binding(row["metadata_json"])]
    assert owner_rows and all(row["revoked_at"] is None for row in owner_rows)
    assert all(row["revoked_at"] for row in rows if row["principal_id"] == ordu.id)
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        assert ok(call(client, headers, "tasks_list", {})) is not None
        # Owner-session revocation is purpose-bound as well: agent credentials stay untouched.
        live = {row["id"] for row in _live(fixture) if row["principal_id"] != fixture.owner.id}
        assert owner_prepare.revoke(fixture.db, business="widget", label=None)["revoked"] == 1
        assert {row["id"] for row in _live(fixture) if row["principal_id"] != fixture.owner.id} == live
        refused = client.post("/mcp/owner", headers={**MCP_HEADERS, "Authorization": "Bearer " + full.token},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}})
        assert refused.status_code == 401


def _live(fixture):
    with fixture.db.read() as uow:
        return uow.connection.execute("SELECT id, principal_id FROM credentials WHERE revoked_at IS NULL").fetchall()


def test_excluded_administration_stays_unavailable(full):
    agent_id = create_ordubashy(full.fixture, [full.fixture.business_id]).id
    with TestClient(full.web) as client:
        headers = connect(client, full.token)
        for operation, arguments in (
            ("agents_deactivate", {"agent_id": agent_id}),
            ("agents_update", {"agent_id": agent_id, "changes": {"display_name": "Renamed"}}),
            ("working_memory_update", {"body": "x"}),
            ("agent_work_update", {"summary": "x"}),
            ("tasks_comment", {"task_id": new_id(), "text": "x"}),
        ):
            assert (operation, code(call(client, headers, operation, {**arguments, "idempotency_key": new_id()}))) == (
                operation, "forbidden")
        # Signoff decisions have no MCP descriptor at all; the owner route cannot reach them.
        for operation in ("signoffs_review", "signoffs_reject", "signoffs_retry_apply"):
            assert code(call(client, headers, operation, {"signoff_id": new_id(), "idempotency_key": new_id()})) in {
                "forbidden", "operation_unavailable"}
