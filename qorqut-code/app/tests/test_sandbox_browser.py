from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.browser import BrowserRuntime, ChromeProcess, SandboxBrowser


BUSINESS = "00000000-0000-4000-8000-000000000001"
IMAGE = "example/browser@sha256:" + "b" * 64
CONTAINER = "b" * 64


@dataclass(frozen=True)
class Ownership:
    business_id: str = BUSINESS


class Docker:
    def __init__(self, paths, runtime):
        self.paths, self.runtime, self.running, self.chrome, self.close_fails, self.renderer = paths, runtime, True, False, False, True
        self.root_pid = 4242
        self.profile_prefix_collision = False
        profile = json.dumps(json.loads(runtime.seccomp_profile.read_text()), separators=(",", ":"))
        self.record = {
            "Id": CONTAINER,
            "State": {"Running": True},
            "Config": {"Image": IMAGE, "User": "1000:1000", "Labels": {
                "qorqut.role": "browser", "qorqut.generation": "7"}},
            "HostConfig": {"ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"], "CapAdd": [], "ShmSize": 256 * 1024 * 1024,
                           "SecurityOpt": ["no-new-privileges", "seccomp=" + profile]},
            "Mounts": [
                {"Type": "bind", "Source": str(paths.browser / "profile"), "Destination": str(paths.browser / "profile"), "RW": True},
                {"Type": "bind", "Source": str(paths.browser / "downloads"), "Destination": str(paths.browser / "downloads"), "RW": True},
            ],
        }

    def owned(self, kind, identity, ownership):
        assert kind == "container" and identity == CONTAINER and ownership == Ownership()
        return self.record if self.running else None

    def call(self, *arguments, **kwargs):
        assert arguments[:4] == ("exec", "-d", "--user", "1000:1000")
        assert arguments[4:10] == ("--env", "HOME=" + str(self.paths.browser / "profile"),
                                   "--env", "XDG_CONFIG_HOME=" + str(self.paths.browser / "profile"),
                                   "--env", "XDG_CACHE_HOME=/tmp")
        self.chrome = True
        return ""

    def exec(self, container_id, ownership, *arguments, **kwargs):
        assert container_id == CONTAINER and ownership == Ownership()
        command = " ".join(arguments)
        if "qorqut-browser-close" in command:
            if self.close_fails:
                raise DomainError("environment_unavailable", "fixture close failure")
            self.chrome = False
            return ""
        if 'user-data-dir=$profile' in command:
            if self.profile_prefix_collision and "grep -azqxF" in command:
                return ""
            return f"{self.root_pid} 99 19400 0" if self.chrome else ""
        if "userns=" in command:
            return (f"{self.root_pid} user:[1] 0000000000000000 1 2 1 0\n"
                    "4243 user:[2] 0000000000000000 1 2 2 " + ("1" if self.renderer else "0") + "\n") if self.chrome else ""
        if 'basename "$proc"' in command:
            return "4242\n4243" if self.chrome else ""
        if "curl --fail" in command:
            return json.dumps({"Browser": "Chrome/153.0.8010.36"})
        raise AssertionError(command)


def fixture(tmp_path):
    root = tmp_path.resolve()
    browser = root / ".bazarlyk" / "browsers" / BUSINESS
    (browser / "profile").mkdir(parents=True)
    (browser / "downloads").mkdir()
    paths = SimpleNamespace(root=root, browser=browser)
    target = {"enabled": True, "proxy_server": "https://proxy.example:443", "cdp_port": 19400,
              "user_data_dir": str(browser / "profile"), "downloads_dir": str(browser / "downloads"),
              "credentials_ref": "browser-proxy/" + BUSINESS, "generation": BUSINESS}
    business = {"id": BUSINESS, "config_json": {"browser": target}}
    profile = Path(__file__).parents[1] / "bazarlyk/sandbox/browser-seccomp-moby-29.1.3.json"
    runtime = BrowserRuntime(IMAGE, profile)
    environment = SimpleNamespace(ownership=Ownership(), generation=7, browser_id=CONTAINER)
    return paths, business, SimpleNamespace(browser_image=IMAGE), environment, runtime


def test_chrome_inventory_identifies_root_while_forked_children_inherit_its_argv():
    # /proc enumeration need not return the ancestor first. A nested fork may
    # still carry identical profile/CDP arguments before exec rewrites argv.
    assert SandboxBrowser._parse_root(
        "448 14338304 19400 26\n449 14338304 19400 448\n26 14337088 19400 0"
    ) == ChromeProcess(26, "14337088", 19400)
    assert SandboxBrowser._parse_root("") is None


@pytest.mark.parametrize("inventory", [
    "26 100 19400 0\n448 101 19400 0",  # independent writers
    "26 100 19400 0\n448 101 19400 99",  # unproven ancestry
    "26 100 19400 0\n448 99 19400 26",  # stale parent PID reuse
    "26 100 19400 0\n448 101 19401 26",  # different browser endpoint
    "26 100 19400 0\n448 101 19400 449\n449 101 19400 448",  # cycle
    "26 100 19400 26",  # self-parent
    "26 100 19400 0\n26 100 19400 0",  # duplicate observation
    "26 100 19400", "26 bad 19400 0", "26 100 80 0", "0 100 19400 0",
])
def test_chrome_inventory_still_refuses_ambiguous_or_inconsistent_writers(inventory):
    with pytest.raises(DomainError, match="ambiguous Chrome writer"):
        SandboxBrowser._parse_root(inventory)


def test_readiness_checks_exact_container_and_kernel_sandbox_state(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    docker = Docker(paths, runtime)
    browser = SandboxBrowser(docker, runtime, monotonic=lambda: 0, sleep=lambda _: None)
    assert browser.runtime.container_options()[1].startswith("seccomp=")
    assert browser.browser_readiness(business, paths, config, environment) is True
    assert docker.chrome


def test_host_profile_writer_blocks_handoff(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    lock_path = paths.browser / "service.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        browser = SandboxBrowser(Docker(paths, runtime), runtime, monotonic=lambda: 0, sleep=lambda _: None)
        with pytest.raises(DomainError, match="drained"):
            browser.begin(business, paths, config, environment)


def test_close_observed_uses_image_local_cdp_without_current_launch_config(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    docker = Docker(paths, runtime)
    browser = SandboxBrowser(docker, runtime, monotonic=lambda: 0, sleep=lambda _: None)
    browser.browser_readiness(business, paths, config, environment)
    assert browser.close_observed(business, paths, environment) is True
    assert not docker.chrome


def test_observed_binding_is_current_lock_owned_and_never_exposes_a_browser_url(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    docker = Docker(paths, runtime)
    browser = SandboxBrowser(docker, runtime, monotonic=lambda: 0, sleep=lambda _: None)
    browser.browser_readiness(business, paths, config, environment)
    binding = browser.observed_browser_binding(business, paths, config, environment)
    assert binding.business_id == BUSINESS and binding.browser_id == CONTAINER
    assert binding.generation == 7 and binding.cdp_port == 19400
    assert binding.instance_id == CONTAINER + ":4242:99"
    assert browser.owns_profile_lock(paths, environment)
    assert "127.0.0.1" not in repr(binding)


def test_observed_binding_rejects_a_profile_argument_prefix_collision(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    docker = Docker(paths, runtime)
    browser = SandboxBrowser(docker, runtime, monotonic=lambda: 0, sleep=lambda _: None)
    browser.browser_readiness(business, paths, config, environment)
    docker.profile_prefix_collision = True
    with pytest.raises(DomainError, match="not running"):
        browser.observed_browser_binding(business, paths, config, environment)


def test_profile_lock_verifier_rejects_replaced_lock_and_foreign_generation(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    browser = SandboxBrowser(Docker(paths, runtime), runtime, monotonic=lambda: 0, sleep=lambda _: None)
    browser.browser_readiness(business, paths, config, environment)
    assert not browser.owns_profile_lock(paths, SimpleNamespace(**{**vars(environment), "generation": 8}))
    replacement = paths.browser / "replacement.lock"
    replacement.write_text("")
    replacement.replace(paths.browser / "service.lock")
    assert not browser.owns_profile_lock(paths, environment)


def test_writer_lock_refuses_a_fifo_without_blocking(tmp_path):
    paths, _business, _config, environment, runtime = fixture(tmp_path)
    os.mkfifo(paths.browser / "service.lock")
    with pytest.raises(DomainError, match="regular registered file"):
        SandboxBrowser._writer_lock(paths, environment)


def test_profile_lock_verifier_rejects_a_closed_cached_descriptor(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    browser = SandboxBrowser(Docker(paths, runtime), runtime, monotonic=lambda: 0, sleep=lambda _: None)
    browser.browser_readiness(business, paths, config, environment)
    browser._locks[(BUSINESS, 7)].descriptor.close()
    assert not browser.owns_profile_lock(paths, environment)


def test_observed_binding_refuses_a_runtime_image_mismatch(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    browser = SandboxBrowser(Docker(paths, runtime), runtime, monotonic=lambda: 0, sleep=lambda _: None)
    browser.browser_readiness(business, paths, config, environment)
    with pytest.raises(DomainError, match="runtime image"):
        browser.observed_browser_binding(business, paths, SimpleNamespace(browser_image=IMAGE + "0"), environment)


def test_same_generation_restart_replaces_a_verified_dead_chrome_identity(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    docker = Docker(paths, runtime)
    browser = SandboxBrowser(docker, runtime, monotonic=lambda: 0, sleep=lambda _: None)
    assert browser.browser_readiness(business, paths, config, environment) is True
    first = browser.observed_browser_binding(business, paths, config, environment)
    docker.chrome = False
    assert browser.begin(business, paths, config, environment) is None
    docker.root_pid = 5252
    assert browser.readiness(business, paths, config, environment) is True
    assert browser.observed_browser_binding(business, paths, config, environment) != first


def test_profile_checksum_refuses_a_modified_operator_copy(tmp_path):
    paths, _, _, _, runtime = fixture(tmp_path)
    modified = tmp_path / "profile.json"
    modified.write_text(runtime.seccomp_profile.read_text() + "\n")
    with pytest.raises(DomainError, match="checksum"):
        BrowserRuntime(IMAGE, modified).container_options()


def test_readiness_rejects_child_without_explicit_renderer_marker(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    docker = Docker(paths, runtime)
    docker.renderer = False
    clock = iter((0, 0, 31))
    browser = SandboxBrowser(docker, runtime, monotonic=lambda: next(clock), sleep=lambda _: None)
    with pytest.raises(DomainError, match="renderer sandbox"):
        browser.browser_readiness(business, paths, config, environment)


def test_failed_close_retains_profile_writer_lock(tmp_path):
    paths, business, config, environment, runtime = fixture(tmp_path)
    docker = Docker(paths, runtime)
    browser = SandboxBrowser(docker, runtime, monotonic=lambda: 0, sleep=lambda _: None)
    browser.browser_readiness(business, paths, config, environment)
    docker.close_fails = True
    with pytest.raises(DomainError, match="fixture close failure"):
        browser.close(business, paths, config, environment)
    with (paths.browser / "service.lock").open("a") as contender:
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_rollback_profile_proof_accepts_a_safe_never_materialized_profile_but_not_a_held_lock(tmp_path):
    paths = SimpleNamespace(browser=tmp_path / "never-materialized" / "browser")
    assert SandboxBrowser.profile_lock_unheld(paths)
    paths.browser.mkdir(parents=True)
    with (paths.browser / "service.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not SandboxBrowser.profile_lock_unheld(paths)
