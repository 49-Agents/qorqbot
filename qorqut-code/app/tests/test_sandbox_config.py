import json
from pathlib import Path

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.config import BusinessPaths, HostConfig, ResourceLimits


IDENTITY = "00000000-0000-4000-8000-000000000002"
IMAGE = "sha256:" + "a" * 64


def configuration(tmp_path):
    path = tmp_path / ".bazarlyk/sandbox-host.json"
    path.parent.mkdir()
    data = {"schema_version": 1, "workstation_image": IMAGE, "browser_image": IMAGE,
            "agent_api_port": 1148, "limits": {"agent_memory_mb": 512, "browser_memory_mb": 512,
                "gateway_memory_mb": 128, "cpus": 1, "pids": 256, "disk_gb": 8}}
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path, data


def test_host_configuration_must_be_private_and_images_immutable(tmp_path):
    assert HostConfig.read(tmp_path) is None
    path, data = configuration(tmp_path)
    value = HostConfig.read(tmp_path)
    assert value.limits.memory_mb == 1152
    assert value.shared_environment_keys == ()
    assert not value.enabled
    path.write_text(json.dumps({**data, "enabled": True}))
    assert HostConfig.read(tmp_path).enabled
    path.chmod(0o644)
    with pytest.raises(DomainError):
        HostConfig.read(tmp_path)
    path.chmod(0o600)
    for changed in ({"workstation_image": "anything:latest"}, {"agent_api_port": 1049},
                    {"docker_socket": "/var/run/docker.sock"}, {"limits": {**data["limits"], "cpus": float("nan")}},
                    {"direct_domains": ["*.example.com"]}, {"runtime": "arbitrary-runtime"}):
        path.write_text(json.dumps({**data, **changed}))
        with pytest.raises((DomainError, ValueError)):
            HostConfig.read(tmp_path)


def test_registered_paths_cannot_mount_a_sibling_or_alias_private_control(tmp_path):
    workspace = tmp_path / "business"
    (workspace / "hq").mkdir(parents=True)
    row = {"id": IDENTITY, "workspace": str(workspace), "hq_path": str(workspace / "hq")}
    paths = BusinessPaths.registered(tmp_path, row)
    assert paths.home == tmp_path / ".bazarlyk/sandboxes" / IDENTITY / "home"
    assert paths.workspace == workspace
    with pytest.raises(DomainError):
        BusinessPaths.registered(tmp_path, {**row, "workspace": str(tmp_path)})
    private = tmp_path / ".bazarlyk/sandboxes"
    private.mkdir(parents=True)
    (private / IDENTITY).symlink_to(workspace, target_is_directory=True)
    with pytest.raises(DomainError):
        BusinessPaths.registered(tmp_path, row)


def test_pilot_can_enable_explicit_adoption_without_changing_new_business_creation(tmp_path):
    from bazarlyk.sandbox.config import creation_enabled
    path, data = configuration(tmp_path)
    path.write_text(json.dumps({**data, "enabled": True, "provision_new_businesses": False}))
    assert HostConfig.read(tmp_path).enabled is True
    assert creation_enabled(tmp_path) is False
    path.write_text(json.dumps({**data, "enabled": True}))
    assert creation_enabled(tmp_path) is True
    path.write_text(json.dumps({**data, "enabled": True, "provision_new_businesses": "false"}))
    with pytest.raises(DomainError):
        creation_enabled(tmp_path)
def test_invalid_existing_host_configuration_cannot_select_legacy_creation(tmp_path):
    from bazarlyk.sandbox.config import creation_enabled
    assert creation_enabled(tmp_path) is False
    path, _ = configuration(tmp_path)
    path.write_text("invalid configuration")
    with pytest.raises(DomainError):
        creation_enabled(tmp_path)
    path.unlink()
    path.symlink_to(tmp_path / "missing-configuration")
    with pytest.raises(DomainError):
        creation_enabled(tmp_path)
