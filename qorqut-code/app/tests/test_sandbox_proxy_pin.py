"""Generation-scoped proxy DNS pins are host control state, never workload input."""

import json
import os
import stat

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.config import BusinessPaths, HostConfig, ResourceLimits
from bazarlyk.sandbox.network import ProxyEndpoint
from bazarlyk.sandbox.runtime import (DockerEnvironmentDriver, _proxy_pin_path,
    assigned_proxy, provision_assigned_proxy)
from bazarlyk.sandbox.terminal import environment_fingerprint


BUSINESS = "00000000-0000-4000-8000-000000000001"
IMAGE = "sha256:" + "a" * 64


def fixture(tmp_path):
    root = tmp_path.resolve()
    workspace = root / "first"
    (workspace / "hq").mkdir(parents=True)
    browser = {"enabled": True, "proxy_server": "https://proxy.example:8443", "cdp_port": 19400,
               "user_data_dir": str(root / ".bazarlyk/browsers" / BUSINESS / "profile"),
               "downloads_dir": str(root / ".bazarlyk/browsers" / BUSINESS / "downloads"),
               "credentials_ref": "browser-proxy/" + BUSINESS, "generation": "proxy-identity-one"}
    business = {"id": BUSINESS, "workspace": str(workspace), "hq_path": str(workspace / "hq"),
                "config_json": {"browser": browser}}
    credentials = root / ".bazarlyk/secrets/browser-proxy"
    credentials.mkdir(parents=True, mode=0o700)
    secret = credentials / (BUSINESS + ".json")
    secret.write_text(json.dumps({"username": "account", "password": "sensitive-proxy-password"}))
    secret.chmod(0o600)
    paths = BusinessPaths.registered(root, business)
    config = HostConfig("repo@" + IMAGE, "repo@" + IMAGE,
                        ResourceLimits(512, 512, 128, 1, 256, 8), 1148, enabled=True)
    return root, business, paths, config


def rotating_resolver(values, calls):
    def resolve(*_args):
        calls.append(True)
        return [(None, None, None, None, (values.pop(0), 0))]
    return resolve


def test_driver_pins_first_dns_answer_and_readers_never_reresolve(tmp_path, monkeypatch):
    _root, business, paths, config = fixture(tmp_path)
    calls, answers = [], ["8.8.8.8", "9.9.9.9"]
    monkeypatch.setattr("bazarlyk.sandbox.runtime.socket.getaddrinfo", rotating_resolver(answers, calls))
    driver = DockerEnvironmentDriver(readiness=lambda *_: True)

    initial = driver._proxy(business, paths, 1)
    assert initial.address == "8.8.8.8" and len(calls) == 1
    pin = json.loads(_proxy_pin_path(paths).read_text())
    assert pin["generation"] == 1 and pin["endpoint"]["address"] == "8.8.8.8"
    assert "password" not in json.dumps(pin) and "account" not in json.dumps(pin)
    assert stat.S_IMODE(_proxy_pin_path(paths).stat().st_mode) == 0o600

    # A dashboard/terminal restart only reads the durable selected endpoint even
    # when DNS rotates or is completely unavailable.
    monkeypatch.setattr("bazarlyk.sandbox.runtime.socket.getaddrinfo",
                        lambda *_: pytest.fail("read-only proxy lookup must not resolve DNS"))
    assert assigned_proxy(business, paths, generation=1).address == "8.8.8.8"
    assert environment_fingerprint(config, business, paths, generation=1)
    assert driver._proxy(business, paths, 1).address == "8.8.8.8"


def test_explicit_new_generation_refreshes_pin_but_stale_generation_refuses(tmp_path, monkeypatch):
    _root, business, paths, _config = fixture(tmp_path)
    calls, answers = [], ["8.8.8.8", "9.9.9.9"]
    monkeypatch.setattr("bazarlyk.sandbox.runtime.socket.getaddrinfo", rotating_resolver(answers, calls))
    driver = DockerEnvironmentDriver(readiness=lambda *_: True)
    assert driver._proxy(business, paths, 1).address == "8.8.8.8"
    assert driver._proxy(business, paths, 2).address == "9.9.9.9"
    assert len(calls) == 2
    assert json.loads(_proxy_pin_path(paths).read_text())["generation"] == 2
    with pytest.raises(DomainError, match="replacement"):
        assigned_proxy(business, paths, generation=1)


def test_changed_credential_reference_invalidates_old_pin_without_dns_fallback(tmp_path, monkeypatch):
    _root, business, paths, _config = fixture(tmp_path)
    monkeypatch.setattr("bazarlyk.sandbox.runtime.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("8.8.8.8", 0))])
    provision_assigned_proxy(business, paths, generation=1)
    business["config_json"]["browser"]["credentials_ref"] = "browser-proxy/00000000-0000-4000-8000-000000000099"
    monkeypatch.setattr("bazarlyk.sandbox.runtime.socket.getaddrinfo",
                        lambda *_: pytest.fail("stale configuration must not fall back to DNS"))
    with pytest.raises(DomainError):
        assigned_proxy(business, paths, generation=1)


def test_credential_rotation_changes_plan_fingerprint_without_entering_pin(tmp_path, monkeypatch):
    root, business, paths, config = fixture(tmp_path)
    monkeypatch.setattr("bazarlyk.sandbox.runtime.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("8.8.8.8", 0))])
    provision_assigned_proxy(business, paths, generation=1)
    before = environment_fingerprint(config, business, paths, generation=1)
    secret = root / ".bazarlyk/secrets/browser-proxy" / (BUSINESS + ".json")
    secret.write_text(json.dumps({"username": "account", "password": "rotated-proxy-password"}))
    secret.chmod(0o600)
    monkeypatch.setattr("bazarlyk.sandbox.runtime.socket.getaddrinfo",
                        lambda *_: pytest.fail("credential rotation must not resolve DNS"))
    after = environment_fingerprint(config, business, paths, generation=1)
    assert before != after
    pin = _proxy_pin_path(paths).read_text()
    assert "sensitive-proxy-password" not in pin and "rotated-proxy-password" not in pin


class _InjectedLoaderReached(Exception):
    pass


def test_repeated_driver_reconcile_with_two_argument_loader_needs_no_pin(tmp_path):
    _root, business, paths, config = fixture(tmp_path)
    calls = []

    class Docker:
        def inspect(self, kind, image):
            assert kind == "image"
            return {"Id": image}

    def injected(current_business, current_paths):
        assert current_business is business and current_paths is paths
        calls.append(True)
        raise _InjectedLoaderReached

    driver = DockerEnvironmentDriver(docker=Docker(), readiness=lambda *_: True, proxy_loader=injected)
    driver._inventory = lambda _kind, _ownership: [{"Config": {"Labels": {"qorqut.generation": "1"}}}]
    for _ in range(2):
        with pytest.raises(_InjectedLoaderReached):
            driver.reconcile(business, paths, config, 1, heartbeat=lambda: None)
    assert calls == [True, True] and not _proxy_pin_path(paths).exists()


@pytest.mark.parametrize("kind", ["malformed", "symlink", "fifo"])
def test_pin_refuses_special_or_malformed_control_file(tmp_path, monkeypatch, kind):
    _root, business, paths, _config = fixture(tmp_path)
    monkeypatch.setattr("bazarlyk.sandbox.runtime.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("8.8.8.8", 0))])
    provision_assigned_proxy(business, paths, generation=1)
    pin = _proxy_pin_path(paths)
    if kind == "malformed":
        pin.write_text("not json")
    elif kind == "symlink":
        target = pin.with_name("elsewhere")
        target.write_text(pin.read_text())
        pin.unlink()
        pin.symlink_to(target)
    else:
        pin.unlink()
        os.mkfifo(pin, 0o600)
    with pytest.raises(DomainError):
        assigned_proxy(business, paths, generation=1)


def test_pin_requires_public_ipv4_and_never_exposes_credentials(tmp_path, monkeypatch):
    _root, business, paths, _config = fixture(tmp_path)
    monkeypatch.setattr("bazarlyk.sandbox.runtime.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("10.0.0.4", 0))])
    with pytest.raises(DomainError, match="resolved") as error:
        provision_assigned_proxy(business, paths, generation=1)
    assert "sensitive-proxy-password" not in str(error.value)
    assert not _proxy_pin_path(paths).exists()
    endpoint = ProxyEndpoint("https", "8.8.8.8", 8443, "proxy.example", "account", "sensitive-proxy-password")
    assert "sensitive-proxy-password" not in repr(endpoint)
