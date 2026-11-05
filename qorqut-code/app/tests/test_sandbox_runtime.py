"""Real persistent workstation acceptance, explicitly opt-in and disposable.

This exercises the production Docker driver; the separate native agent/browser
acceptance is still required before rollout. No model sessions are started here.
"""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.config import BusinessPaths, HostConfig, ResourceLimits
from bazarlyk.sandbox.docker import Docker
from bazarlyk.sandbox.linux import LinuxNetwork
from bazarlyk.sandbox.runtime import DockerEnvironmentDriver
from test_sandbox_network_runtime import assigned_proxy


pytestmark = pytest.mark.skipif(os.environ.get("QORQUT_TEST_SANDBOX_RUNTIME") != "1",
                              reason="Explicit disposable Docker workstation test only")


def test_persistent_driver_retries_crashes_and_replaces_without_duplicate_writers(tmp_path):
    image = os.environ.get("QORQUT_TEST_WORKSTATION_IMAGE")
    if not image:
        pytest.skip("A pinned workstation image is required")
    identity = str(uuid4())
    root = tmp_path / "installation"
    workspace = root / "disposable"
    (workspace / "hq/agents/coder").mkdir(parents=True)
    business = {"id": identity, "workspace": str(workspace), "hq_path": str(workspace / "hq")}
    paths = BusinessPaths.registered(root, business)
    docker = Docker(command=("sudo", "-n", "docker"))
    network = LinuxNetwork(docker, command=("sudo", "-n"))
    calls, crash = [], [True]
    proxy = assigned_proxy()
    config = HostConfig(image, image, ResourceLimits(512, 256, 256, 1, 256, 8), 1148)

    def ready(business, paths, config, environment):
        calls.append(environment)
        tools = docker.exec(environment.workstation_id, environment.ownership, "bash", "-lc",
                            "id -u; git config --system init.defaultBranch; claude --version; codex --version; tmux -V")
        assert tools.startswith("1000\nmain\n") and "Claude Code" in tools and "codex-cli" in tools
        if crash[0]:
            crash[0] = False
            raise RuntimeError("simulated interruption before ready publication")
        return True

    driver = DockerEnvironmentDriver(docker=docker, network=network, readiness=ready, proxy_loader=lambda *args: proxy)
    owner = driver.ownership(paths)
    try:
        with pytest.raises(RuntimeError, match="interruption"):
            driver.reconcile(business, paths, config, 1, heartbeat=lambda: None)
        first = driver.reconcile(business, paths, config, 1, heartbeat=lambda: None)
        assert calls[0].workstation_id == first.environment_id
        assert len(driver._inventory("container", owner)) == 3
        first_runtime = calls[-1]
        # Native tmux survives an ordinary reconcile and host attachment restart.
        docker.exec(first.environment_id, owner, "tmux", "-L", "acceptance", "new-session", "-d", "-s", "coder")
        driver.reconcile(business, paths, config, 1, heartbeat=lambda: None)
        docker.exec(first.environment_id, owner, "tmux", "-L", "acceptance", "has-session", "-t", "coder")
        # Git internals, dirty worktrees and HOME are real persistent bind mounts.
        code = """
import pathlib,subprocess,sys
base=pathlib.Path(sys.argv[1])/'code/repository'
main=base/'main'; main.mkdir(parents=True)
def git(*args):subprocess.run(['git',*args],check=True,stdout=subprocess.DEVNULL)
git('init','--initial-branch=main',str(main))
git('-C',str(main),'config','user.email','fixture@example.test')
git('-C',str(main),'config','user.name','Fixture')
(main/'tracked').write_text('saved')
git('-C',str(main),'add','tracked'); git('-C',str(main),'commit','-m','fixture')
git('-C',str(main),'worktree','add','-b','ongoing',str(base/'worktrees/ongoing'))
(base/'worktrees/ongoing/dirty').write_text('uncommitted')
(pathlib.Path.home()/'.saved-credential').write_text('fixture-only-canary')
"""
        docker.exec(first.environment_id, owner, "python3", "-c", code, str(workspace))
        docker.exec(first_runtime.browser_id, owner, "python3", "-c",
                    "import pathlib,sys; (pathlib.Path(sys.argv[1])/'Cookies').write_text('fixture-profile')", str(paths.browser / "profile"))
        second = driver.reconcile(business, paths, config, 2, heartbeat=lambda: None)
        assert first.environment_id != second.environment_id
        assert docker.owned("container", first.environment_id, owner) is None
        assert len(driver._inventory("container", owner)) == 3
        assert docker.exec(second.environment_id, owner, "git", "-C", str(workspace / "code/repository/worktrees/ongoing"),
                           "status", "--porcelain") == "?? dirty"
        assert docker.exec(second.environment_id, owner, "cat", "/home/agent/.saved-credential") == "fixture-only-canary"
        assert docker.exec(calls[-1].browser_id, owner, "cat", str(paths.browser / "profile/Cookies")) == "fixture-profile"
        # The former environment ID cannot be reused by an old terminal request.
        with pytest.raises(DomainError):
            docker.exec(first.environment_id, owner, "true")
        for current in driver._inventory("container", owner):
            assert current["HostConfig"]["ReadonlyRootfs"] and not current["HostConfig"]["Privileged"]
            assert current["HostConfig"]["CapDrop"] == ["ALL"]
            assert current["HostConfig"]["RestartPolicy"]["Name"] == "no"
            assert all(m["Destination"] != "/var/run/docker.sock" for m in current["Mounts"])
        assert Path(paths.home / ".saved-credential").read_text() == "fixture-only-canary"
    finally:
        driver._drain(owner, driver._inventory("container", owner))
        for current in driver._inventory("network", owner):
            network.remove_bridge(current["Id"])
            docker.call("network", "rm", current["Id"], timeout=180)
