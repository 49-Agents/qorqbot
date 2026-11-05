from types import SimpleNamespace
import hashlib

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.config import BusinessPaths, HostConfig, ResourceLimits
from bazarlyk.sandbox.docker import Ownership
from bazarlyk.sandbox.terminal import SandboxTerminalProvider
from bazarlyk.transport.terminals import TerminalExecution


BUSINESS = "00000000-0000-4000-8000-000000000001"
CONTAINER = "a" * 64


class Docker:
    def __init__(self, ownership, paths):
        self.ownership, self.paths = ownership, paths
        self.container_name = paths.resource_prefix + "-g3-agent"

    def owned(self, kind, identity, ownership):
        if ownership != self.ownership:
            raise DomainError("environment_conflict", "another environment")
        if kind == "container" and identity == self.container_name:
            return {"Id": CONTAINER, "Config": {"Labels": {**ownership.labels, "qorqut.role": "agent", "qorqut.generation": "3", "qorqut.plan": "fixture-plan"}},
                    "State": {"Running": True}, "NetworkSettings": {"Networks": {self.paths.resource_prefix + "-agent": {}}}}
        if kind == "container" and identity == CONTAINER:
            return {"Id": CONTAINER, "Config": {"Labels": {**ownership.labels, "qorqut.role": "agent", "qorqut.generation": "3", "qorqut.plan": "fixture-plan"}},
                    "State": {"Running": True}}
        if kind == "network" and identity == self.paths.resource_prefix + "-agent":
            return {"IPAM": {"Config": [{"Gateway": "172.20.0.1"}]}, "Containers": {CONTAINER: {}}}
        return None

    def exec(self, *args, **kwargs):
        return ""


class Read:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return SimpleNamespace(repository=lambda name: SimpleNamespace(get=lambda business_id: self.row))

    def __exit__(self, *args):
        return False


class Database:
    def __init__(self, row):
        self.row = row

    def read(self):
        return Read(self.row)


class Local:
    def environment_pairs(self, business_id=None):
        if business_id is None:
            return [{"key": "SHARED", "value": "shared"}, {"key": "IGNORED", "value": "no"}]
        return [{"key": "PRIVATE", "value": "business"}]

    def private_environment(self, business_id, *, role=None):
        return {"ROLE_SECRET": role}


def make_provider(tmp_path, state="ready", *, enabled=True):
    root = tmp_path.resolve()
    workspace = root / "business"
    (workspace / "hq").mkdir(parents=True)
    business = {"id": BUSINESS, "workspace": str(workspace), "hq_path": str(workspace / "hq")}
    paths = BusinessPaths.registered(root, business)
    row = {"state": state, "observed_generation": 3, "desired_generation": 3, "environment_id": CONTAINER}
    database = Database(row)
    ownership = Ownership(hashlib.sha256(str(root).encode()).hexdigest()[:20], BUSINESS)
    docker = Docker(ownership, paths)
    config = HostConfig("repo@sha256:" + "a" * 64, "repo@sha256:" + "b" * 64,
        ResourceLimits(256, 256, 128, 1, 128, 8), 1148, shared_environment_keys=("SHARED",), enabled=enabled)
    provider = SandboxTerminalProvider(database, root, docker, SimpleNamespace(),
        config_loader=lambda root: config, local_files=Local(),
        fingerprint_loader=lambda config, business, paths: "fixture-plan")
    return provider, business


def test_absent_intent_is_legacy_and_pending_never_falls_back(tmp_path):
    provider, business = make_provider(tmp_path)
    provider.database.row = None
    assert provider(business) is None
    provider.database.row = {"state": "pending", "observed_generation": None, "desired_generation": 1}
    with pytest.raises(DomainError, match="not ready"):
        provider(business)
    provider.database.row = {"state": "ready", "desired_state": "stopped", "observed_generation": 3,
                             "desired_generation": 3, "environment_id": CONTAINER}
    with pytest.raises(DomainError, match="not ready"):
        provider(business)


def test_ready_provider_selects_exact_container_and_scoped_environment(tmp_path):
    provider, business = make_provider(tmp_path)
    execution = provider(business)
    assert isinstance(execution, TerminalExecution)
    assert execution.endpoint == "http://172.20.0.1:1148"
    assert execution.interpreter == "/opt/qorqut/venv/bin/python"
    profile = SimpleNamespace(role=SimpleNamespace(id="coder"))
    assert execution.environment_resolver(profile, business) == {
        "SHARED": "shared", "PRIVATE": "business", "ROLE_SECRET": "coder"
    }


def test_disabled_configuration_cannot_use_a_previously_ready_environment(tmp_path):
    provider, business = make_provider(tmp_path, enabled=False)
    with pytest.raises(DomainError, match="disabled"):
        provider(business)
