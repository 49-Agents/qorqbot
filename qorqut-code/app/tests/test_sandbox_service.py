from pathlib import Path
import subprocess
from threading import Thread
from types import SimpleNamespace
import json

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.browser import BrowserRuntime
from bazarlyk.sandbox.config import BusinessPaths, HostConfig, ResourceLimits
from bazarlyk.sandbox.network import ProxyEndpoint
from bazarlyk.sandbox.service import BusinessSandboxService
from bazarlyk.sandbox.terminal import environment_fingerprint
from bazarlyk.sandbox.runtime import DockerEnvironmentDriver
from bazarlyk.sandbox.docker import Ownership


BUSINESS = "00000000-0000-4000-8000-000000000001"
IMAGE = "sha256:" + "a" * 64


def service(tmp_path):
    calls = []
    runtime = SimpleNamespace(root=tmp_path, database=object(), resolver=object(),
                              require_authority=lambda: calls.append("authority"))
    listeners = SimpleNamespace(ensure=lambda *args: calls.append("listen"),
                                stop=lambda **kwargs: calls.append("unlisten"),
                                stop_business=lambda *args, **kwargs: calls.append("unlisten-one"))
    class Browser:
        def __init__(self, docker, runtime):
            self.runtime = runtime
        def browser_readiness(self, *args):
            calls.append("browser")
            return True
        def close_observed(self, business, paths, environment):
            calls.append(("close", environment.browser_id, environment.generation, self.runtime.image))
            return True
    def ready(*args):
        calls.append("agents")
        return True
    value = BusinessSandboxService(runtime, docker=object(), network=object(), listeners=listeners,
                                    agent_readiness=ready, browser_factory=Browser)
    return value, calls


def test_assembly_is_inert_and_readiness_requires_both_registered_agents_and_browser(tmp_path, monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("Assembly started external work")
    monkeypatch.setattr(Thread, "start", refused)
    monkeypatch.setattr(subprocess, "Popen", refused)
    value, calls = service(tmp_path)
    assert calls == [] and list(tmp_path.iterdir()) == [] and value.worker._thread is None
    config = SimpleNamespace(browser_image=IMAGE)
    assert value.readiness({}, object(), config, object()) is True
    assert calls == ["listen", "agents", "browser"]
    value.agents = lambda *args: False
    with pytest.raises(DomainError, match="preparation"):
        value.readiness({}, object(), config, object())
    assert calls == ["listen", "agents", "browser", "listen"]


def test_old_browser_close_uses_observed_generation_and_image_without_current_configuration(tmp_path):
    value, calls = service(tmp_path)
    paths = SimpleNamespace(root=tmp_path, business_id=BUSINESS)
    current = {"Id": "b" * 64, "Config": {"Image": IMAGE, "Labels": {"qorqut.generation": "3"}}}
    value.close_browser({"id": BUSINESS}, paths, current)
    assert calls == [("close", "b" * 64, 3, IMAGE)]


def test_default_service_profile_is_a_readable_pinned_package_resource(tmp_path):
    import bazarlyk.sandbox.service as installed_service
    value, calls = service(tmp_path)
    profile = value.browser_runtime(SimpleNamespace(browser_image=IMAGE))
    assert profile.seccomp_profile.parent == Path(installed_service.__file__).parent
    assert profile.profile_document()["syscalls"]
    assert calls == []


def test_service_join_failure_retains_listener_and_never_stops_business_containers(tmp_path):
    value, calls = service(tmp_path)
    def stop(**kwargs):
        calls.append("join")
        raise TimeoutError("in flight")
    value.worker = SimpleNamespace(stop=stop)
    with pytest.raises(TimeoutError):
        value.stop()
    assert calls == ["join"]


def test_browser_close_failure_prevents_docker_removal_or_profile_handoff():
    calls = []
    class Docker:
        def call(self, *args, **kwargs):
            calls.append(args)
    def close(*args):
        raise DomainError("environment_conflict", "Chrome still owns its profile")
    driver = DockerEnvironmentDriver(docker=Docker(), readiness=lambda *args: True, before_stop=close)
    current = {"Id": "b" * 64, "State": {"Running": True}, "Config": {"Labels": {"qorqut.role": "browser"}}}
    with pytest.raises(DomainError, match="profile"):
        driver._drain(Ownership("a" * 20, BUSINESS), [current], business={}, paths=object())
    assert calls == []


def test_browser_stop_flushes_before_stopping_and_removing_owned_container():
    calls = []
    current = {"Id": "b" * 64, "State": {"Running": True}, "Config": {"Labels": {"qorqut.role": "browser"}}}
    class Docker:
        def call(self, *args, **kwargs):
            calls.append(args[0])
            if args[0] == "stop":
                current["State"]["Running"] = False
        def owned(self, *args):
            return current
    driver = DockerEnvironmentDriver(docker=Docker(), readiness=lambda *args: True,
                                       before_stop=lambda *args: calls.append("flush"))
    driver._drain(Ownership("a" * 20, BUSINESS), [current], business={}, paths=object())
    assert calls == ["flush", "stop", "rm"]


class BrowserBindingDatabase:
    def __init__(self, business, state):
        self.business, self.state = business, state

    def read(self):
        database = self
        class Read:
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def repository(self, name):
                values = {"businesses": database.business, "business_sandboxes": database.state}
                return SimpleNamespace(get=lambda *_args, **_kwargs: values[name])
        return Read()


class BrowserBindingDocker:
    def __init__(self, paths, config, generation=7):
        self.paths, self.config, self.generation, self.root_pid = paths, config, generation, 4242
        runtime = BrowserRuntime(config.browser_image, Path(__file__).parents[1] / "bazarlyk/sandbox/browser-seccomp-moby-29.1.3.json")
        profile = json.dumps(runtime.profile_document(), separators=(",", ":"))
        self.workstation_id, self.browser_id = "a" * 64, "b" * 64
        plan = "plan"
        self.agent = {"Id": self.workstation_id, "Name": paths.resource_prefix + f"-g{generation}-agent",
                      "State": {"Running": True}, "Config": {"Image": config.workstation_image, "Labels": {
                          "qorqut.role": "agent", "qorqut.generation": str(generation), "qorqut.plan": plan}}}
        self.browser = {"Id": self.browser_id, "Name": paths.resource_prefix + f"-g{generation}-browser",
                        "State": {"Running": True}, "Config": {"Image": config.browser_image, "User": "1000:1000",
                        "Labels": {"qorqut.role": "browser", "qorqut.generation": str(generation), "qorqut.plan": plan}},
                        "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"], "CapAdd": [],
                        "ShmSize": 256 * 1024 * 1024, "NetworkMode": paths.resource_prefix + "-browser",
                        "SecurityOpt": ["no-new-privileges", "seccomp=" + profile]},
                        "Mounts": [{"Type": "bind", "Source": str(paths.browser / "profile"),
                                    "Destination": str(paths.browser / "profile"), "RW": True},
                                   {"Type": "bind", "Source": str(paths.browser / "downloads"),
                                    "Destination": str(paths.browser / "downloads"), "RW": True}],
                        "NetworkSettings": {"Networks": {paths.resource_prefix + "-browser": {}}}}
        self.network = {"Internal": True, "Containers": {self.browser_id: {}}}

    def owned(self, kind, identity, _ownership):
        if kind == "container":
            return {self.workstation_id: self.agent, self.browser_id: self.browser,
                    self.browser["Name"]: self.browser}.get(identity)
        if kind == "network" and identity == self.paths.resource_prefix + "-browser":
            return self.network
        return None

    def exec(self, _container, _ownership, *arguments, **_kwargs):
        if 'user-data-dir=$profile' in " ".join(arguments):
            return f"{self.root_pid} 99 19400 0"
        raise AssertionError(arguments)


def browser_binding_service(tmp_path):
    root, workspace = tmp_path.resolve(), tmp_path / "widget"
    (workspace / "hq").mkdir(parents=True)
    browser_root = root / ".bazarlyk/browsers" / BUSINESS
    (browser_root / "profile").mkdir(parents=True)
    (browser_root / "downloads").mkdir()
    target = {"enabled": True, "proxy_server": "http://proxy.example:8080", "cdp_port": 19400,
              "user_data_dir": str(browser_root / "profile"), "downloads_dir": str(browser_root / "downloads"),
              "credentials_ref": "browser-proxy/" + BUSINESS, "generation": BUSINESS}
    business = {"id": BUSINESS, "workspace": str(workspace), "hq_path": str(workspace / "hq"),
                "config_json": {"browser": target}}
    config = HostConfig(IMAGE, IMAGE, ResourceLimits(512, 512, 128, 1, 256, 8), 1148, enabled=True)
    paths = BusinessPaths.registered(root, business)
    docker = BrowserBindingDocker(paths, config)
    state = {"state": "ready", "desired_state": "running", "desired_generation": 7,
             "observed_generation": 7, "environment_id": docker.workstation_id}
    runtime = SimpleNamespace(root=root, database=BrowserBindingDatabase(business, state), resolver=object(),
                              require_authority=lambda: None)
    value = BusinessSandboxService(runtime, docker=docker, network=object(), config_loader=lambda _: config,
                                   listeners=SimpleNamespace(ensure=lambda *_: None, stop=lambda **_: None,
                                                             stop_business=lambda *_args, **_kwargs: None),
                                   agent_readiness=lambda *_: True)
    value.driver.proxy_loader = lambda *_: ProxyEndpoint("http", "8.8.8.8", 80)
    fingerprint = environment_fingerprint(config, business, paths, proxy_loader=value.driver.proxy_loader)
    docker.agent["Config"]["Labels"]["qorqut.plan"] = fingerprint
    docker.browser["Config"]["Labels"]["qorqut.plan"] = fingerprint
    return value, paths, config, docker, state


def test_service_returns_only_a_committed_lock_owned_browser_binding(tmp_path):
    value, paths, config, docker, _state = browser_binding_service(tmp_path)
    _business, observed_paths, _config, environment = value._observed_browser(BUSINESS)
    assert observed_paths == paths
    value.browser(config.browser_image)._lock(paths, environment)
    binding = value.observed_browser_binding(BUSINESS)
    assert binding.browser_id == docker.browser_id and binding.generation == 7
    assert binding.instance_id == docker.browser_id + ":4242:99"
    assert value.owns_browser_lock(BUSINESS)
    replacement = paths.browser / "replacement.lock"
    replacement.write_text("")
    replacement.replace(paths.browser / "service.lock")
    assert not value.owns_browser_lock(BUSINESS)
    with pytest.raises(DomainError, match="profile lock"):
        value.observed_browser_binding(BUSINESS)


def test_service_refuses_stale_generation_and_wrong_browser_runtime(tmp_path):
    value, paths, config, docker, state = browser_binding_service(tmp_path)
    state["desired_generation"] = 8
    with pytest.raises(DomainError, match="committed ready"):
        value.observed_browser_binding(BUSINESS)
    assert not value.owns_browser_lock(BUSINESS)
    state["desired_generation"] = 7
    docker.network["Internal"] = False
    with pytest.raises(DomainError, match="committed ready generation"):
        value.observed_browser_binding(BUSINESS)
    docker.network["Internal"] = True
    docker.agent["Config"]["Image"] = "sha256:" + "d" * 64
    with pytest.raises(DomainError, match="committed ready generation"):
        value.observed_browser_binding(BUSINESS)
    docker.agent["Config"]["Image"] = config.workstation_image
    value.driver.proxy_loader = lambda *_: ProxyEndpoint("http", "9.9.9.9", 80)
    with pytest.raises(DomainError, match="committed ready generation"):
        value.observed_browser_binding(BUSINESS)
    value.driver.proxy_loader = lambda *_: ProxyEndpoint("http", "8.8.8.8", 80)
    docker.browser["Config"]["Image"] = "sha256:" + "c" * 64
    _business, _paths, _config, environment = value._observed_browser(BUSINESS)
    value.browser(config.browser_image)._lock(paths, environment)
    with pytest.raises(DomainError, match="trusted confinement"):
        value.observed_browser_binding(BUSINESS)


def test_browser_origin_rejects_same_generation_stale_listener_workstation_gateway_or_address(tmp_path):
    from dataclasses import replace
    from threading import RLock
    from bazarlyk.sandbox.listener import ListenerBinding
    value, paths, config, docker, _ = browser_binding_service(tmp_path)
    _, _, _, environment = value._observed_browser(BUSINESS)
    value.browser(config.browser_image)._lock(paths, environment)
    binding = value.observed_browser_binding(BUSINESS)
    saved = ListenerBinding(BUSINESS, 7, docker.workstation_id, "c" * 64, "172.18.9.1", 1148,
                            ("172.18.9.3", "172.18.9.2"))
    value.listeners = SimpleNamespace(_guard=RLock(), _servers={BUSINESS: (saved, SimpleNamespace(running=True))})
    owned = docker.owned
    def observed(kind, identity, owner):
        if kind == "container" and identity.endswith("-gateway"):
            return {"Id": "c" * 64, "State": {"Running": True}, "Config": {"Labels": {
                "qorqut.role": "gateway", "qorqut.generation": "7", "qorqut.plan": environment.plan_fingerprint}}}
        if kind == "network" and identity.endswith("-agent"):
            return {"Internal": True, "Containers": {docker.workstation_id: {"IPv4Address": "172.18.9.3/24"},
                    "c" * 64: {"IPv4Address": "172.18.9.2/24"}},
                    "IPAM": {"Config": [{"Gateway": "172.18.9.1"}]}}
        return owned(kind, identity, owner)
    docker.owned = observed
    assert value.agent_origin(BUSINESS, binding) == "http://172.18.9.1:1148"
    assert value.account_proxy(BUSINESS) == "http://172.18.9.2:18080"
    for changes in ({"workstation_id": "d" * 64}, {"gateway_id": "e" * 64}, {"address": "172.18.10.1"},
                    {"port": 1049}, {"peers": ("172.18.9.3", "172.18.11.2")}):
        value.listeners._servers[BUSINESS] = replace(saved, **changes), SimpleNamespace(running=True)
        with pytest.raises(DomainError, match="outside its current"):
            value.agent_origin(BUSINESS, binding)
        with pytest.raises(DomainError, match="outside its current"):
            value.account_proxy(BUSINESS)
