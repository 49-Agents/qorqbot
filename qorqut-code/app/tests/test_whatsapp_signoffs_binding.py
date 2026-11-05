"""The installation binding: version 1 (read-only) is accepted unchanged, version 2 adds the managed-send transport, and the
agent's MCP launch never carries any send path."""
import hashlib
import json
from uuid import uuid4

import pytest

from bazarlyk.bootstrap import whatsapp
from qorqut_operations.contracts import DomainError

AGENT = str(uuid4())
ACCOUNT = "c" * 64


@pytest.fixture
def root(tmp_path):
    instance = tmp_path / "instance"
    (instance / "config").mkdir(parents=True)
    (instance / "private").mkdir()
    return instance


def binary_of(root):
    path = root / "runtime/connectors" / whatsapp.NAME / ("a" * 40) / whatsapp.NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 91\n")
    path.chmod(0o755)
    return path


def binding(root, version=2, **changes):
    path = binary_of(root)
    value = {"version": version, "agent_id": AGENT, "binary": str(path),
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "socket": str(whatsapp.socket_path(root))}
    if version == 2:
        value["managed_send"] = {"socket": str(whatsapp.send_socket_path(root)),
                                 "secret_file": str(whatsapp.send_secret_path(root)), "account_ref": ACCOUNT}
    value.update(changes)
    return value


def write(root, value, mode=0o600):
    target = root / "config" / (whatsapp.NAME + ".json")
    target.write_text(json.dumps(value))
    target.chmod(mode)
    return target


def test_a_version_1_binding_is_accepted_unchanged_and_offers_no_sending(root):
    write(root, binding(root, 1))
    assert whatsapp.validate_binding(binding(root, 1), root)["version"] == 1
    assert whatsapp.managed_send(root) is None


def test_a_version_2_binding_yields_the_managed_send_settings_of_its_agent(root):
    write(root, binding(root))
    assert whatsapp.managed_send(root) == {"agent_id": AGENT, "socket": str(whatsapp.send_socket_path(root)),
        "secret_file": str(whatsapp.send_secret_path(root)), "account_ref": ACCOUNT}


def test_no_binding_means_no_managed_send(root):
    assert whatsapp.managed_send(root) is None


@pytest.mark.parametrize("change", [
    {"version": 3}, {"version": "2"}, {"managed_send": None}, {"managed_send": {}},
    {"managed_send": {"socket": "/run/elsewhere/send.sock", "secret_file": "x", "account_ref": ACCOUNT}},
    {"managed_send_extra": True}, {"agent_id": "not-a-uuid"}, {"socket": "/run/other/read.sock"}])
def test_an_invalid_version_2_binding_is_rejected(root, change):
    value = binding(root)
    value.update(change)
    with pytest.raises(DomainError) as caught:
        whatsapp.validate_binding(value, root)
    assert caught.value.code == "invalid_configuration"


@pytest.mark.parametrize("field,replacement", [
    ("socket", "/run/elsewhere/send.sock"), ("secret_file", "/home/user/.secret"), ("account_ref", "A" * 64),
    ("account_ref", "a" * 63), ("account_ref", "g" * 64), ("account_ref", None), ("extra", "x")])
def test_the_managed_send_section_is_exact(root, field, replacement):
    value = binding(root)
    value["managed_send"][field] = replacement
    with pytest.raises(DomainError):
        whatsapp.validate_binding(value, root)


def test_a_managed_send_section_is_not_allowed_in_a_version_1_binding(root):
    value = binding(root, 1)
    value["managed_send"] = binding(root)["managed_send"]
    with pytest.raises(DomainError):
        whatsapp.validate_binding(value, root)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604])
def test_a_binding_readable_by_others_is_refused(root, mode):
    write(root, binding(root), mode)
    with pytest.raises(DomainError):
        whatsapp.managed_send(root)


def test_a_symlinked_binding_is_refused(root, tmp_path):
    real = tmp_path / "real.json"
    real.write_text(json.dumps(binding(root)))
    real.chmod(0o600)
    (root / "config" / (whatsapp.NAME + ".json")).symlink_to(real)
    with pytest.raises(DomainError):
        whatsapp.managed_send(root)


def test_the_mcp_launch_of_a_version_2_binding_is_the_read_client_only(root):
    value = binding(root)
    write(root, value)

    class Paths:
        instance_root = root

    data = {"role": {"id": "ordubashy"}, "scope": {"kind": "ecosystem"}, "agent_id": AGENT}
    prepared, server = whatsapp.launch(Paths, data)
    assert prepared == {"prepared": True}
    assert server == {"command": value["binary"], "args": ["mcp", "--socket", value["socket"]]}
    flat = json.dumps(server)
    assert "send" not in flat and "managed" not in flat and ACCOUNT not in flat and "key" not in flat
    # Another agent, or a non-Ordubashy role, gets no launch at all.
    assert whatsapp.launch(Paths, {**data, "agent_id": str(uuid4())}) == (None, None)
    assert whatsapp.launch(Paths, {**data, "role": {"id": "founder"}}) == (None, None)


def test_the_guidance_says_the_reader_cannot_send_and_sending_needs_the_owner(root):
    assert "this tool cannot\nsend" in whatsapp.GUIDANCE
    assert "owner" in whatsapp.GUIDANCE and "whatsapp_send" in whatsapp.GUIDANCE


def test_the_telegram_bridge_never_mirrors_a_personal_whatsapp_send():
    from bazarlyk.telegram.signoffs import PRIVATE_OPERATION, bridgeable

    assert PRIVATE_OPERATION == "whatsapp_send"
    assert bridgeable({"operation_name": "tasks_create"}) and bridgeable({"operation_name": "account_email_send"})
    assert not bridgeable({"operation_name": "whatsapp_send"})


def test_the_send_is_parent_only_and_its_program_is_external_global_and_not_schedulable():
    from bazarlyk.agents.operational_delegation import _PARENT_ONLY_OPERATIONS
    from bazarlyk.background.integration import register_programs
    from qorqut_operations.background import ProgramRegistry

    assert "whatsapp_send" in _PARENT_ONLY_OPERATIONS          # no delegated assignment can ever propose it
    program = register_programs(ProgramRegistry()).require("whatsapp.send")
    assert program.external and program.routine_allowed is False and tuple(program.scope_kinds) == ("global",)
    assert not program.permissions and not program.outputs        # no declared output, grant or Context write
