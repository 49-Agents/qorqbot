"""The agent-browser wrapper runs its own bundled Node; runtime selection fails closed for an incomplete new runtime."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from bazarlyk.bootstrap import agent_browser, browser

TOOLS = Path(__file__).resolve().parents[1] / "tools/browser"
BUNDLED_NODE = """#!/bin/sh
echo "bundled-node"
echo "path=$PATH"
i=0
for argument in "$@"; do echo "arg$i=$argument"; i=$((i + 1)); done
exit "${FAKE_EXIT:-0}"
"""


def package(runtime, version=agent_browser.VERSION):
    directory = runtime / "node_modules/agent-browser"
    (directory / "bin").mkdir(parents=True)
    (directory / "package.json").write_text(json.dumps({"name": "agent-browser", "version": version}))
    (directory / "bin/agent-browser.js").write_text("// cli\n")


def legacy_runtime(base):
    runtime = base / "legacy"
    package(runtime)
    (runtime / "node_modules/.bin").mkdir()
    (runtime / "node_modules/.bin/agent-browser").symlink_to("../agent-browser/bin/agent-browser.js")
    (runtime / "node_modules/agent-browser/bin/agent-browser.js").chmod(0o755)
    return runtime


def bundled_runtime(base, name="bundled"):
    runtime = base / name
    package(runtime)
    shutil.copyfile(TOOLS / "run-agent-browser", runtime / "run-agent-browser")
    (runtime / "run-agent-browser").chmod(0o755)
    (runtime / "node/bin").mkdir(parents=True)
    (runtime / "node/bin/node").write_text(BUNDLED_NODE)
    (runtime / "node/bin/node").chmod(0o755)
    return runtime


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A conflicting host Node first on PATH, recording any use."""
    directory = tmp_path / "host-bin"
    directory.mkdir()
    (directory / "node").write_text(f"#!/bin/sh\necho host-node >> {tmp_path}/host-used\nexit 9\n")
    (directory / "node").chmod(0o755)
    monkeypatch.setattr('bazarlyk.bootstrap.features.browser_supported',lambda *args:True)
    monkeypatch.setenv("PATH", f"{directory}:/usr/bin:/bin")
    return tmp_path


def invoke(runtime, *arguments, env=None):
    return subprocess.run([str(runtime / "run-agent-browser"), *arguments], capture_output=True, text=True, env=env or os.environ)


def test_the_checked_in_wrapper_is_an_executable_posix_script():
    wrapper = TOOLS / "run-agent-browser"
    assert os.access(wrapper, os.X_OK) and wrapper.read_text().startswith("#!/bin/sh\n")
    assert subprocess.run(["sh", "-n", str(wrapper)]).returncode == 0
    assert browser.RUNTIME_WRAPPER == wrapper.name


def test_the_wrapper_execs_its_own_node_with_unchanged_argv_and_a_prepended_path(host):
    runtime = bundled_runtime(host)
    arguments = ["--config", "/x y/agent-browser.json", "", "--cdp=http://127.0.0.1:19400", "fill", "ünï cödé", "a$b;c"]
    result = invoke(runtime, *arguments)
    assert result.returncode == 0 and result.stdout.splitlines()[0] == "bundled-node"
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines()[1:])
    assert lines["path"] == f"{runtime}/node/bin:{host}/host-bin:/usr/bin:/bin"  # only its own node/bin is prepended
    cli = f"{runtime}/node_modules/agent-browser/bin/agent-browser.js"
    assert [lines[f"arg{i}"] for i in range(len(arguments) + 1)] == [cli, *arguments]
    assert not (host / "host-used").exists()


def test_the_wrapper_passes_the_exit_status_through_and_resolves_a_symlinked_invocation(host):
    runtime = bundled_runtime(host)
    link = host / "elsewhere/agent"
    link.parent.mkdir()
    link.symlink_to(runtime / "run-agent-browser")
    result = subprocess.run([str(link), "snapshot"], capture_output=True, text=True, env={**os.environ, "FAKE_EXIT": "7"})
    assert result.returncode == 7 and "bundled-node" in result.stdout
    assert f"arg0={runtime}/node_modules/agent-browser/bin/agent-browser.js" in result.stdout


@pytest.mark.parametrize("damage", ["missing-node", "node-not-executable", "node-is-a-directory", "missing-cli"])
def test_the_wrapper_refuses_an_incomplete_runtime_and_never_falls_back_to_the_host_node(host, damage):
    runtime = bundled_runtime(host)
    if damage == "missing-node":
        (runtime / "node/bin/node").unlink()
    elif damage == "node-not-executable":
        (runtime / "node/bin/node").chmod(0o644)
    elif damage == "node-is-a-directory":
        (runtime / "node/bin/node").unlink()
        (runtime / "node/bin/node").mkdir()
    else:
        (runtime / "node_modules/agent-browser/bin/agent-browser.js").unlink()
    result = invoke(runtime, "snapshot")
    assert result.returncode == 127 and "run-agent-browser:" in result.stderr and not result.stdout
    assert not (host / "host-used").exists()


def test_a_runtime_that_ships_the_wrapper_selects_it(host):
    runtime = bundled_runtime(host)
    assert browser.runtime_executable(str(runtime)) == runtime / "run-agent-browser"
    assert browser.runtime_directory(browser.runtime_executable(str(runtime))) == runtime


@pytest.mark.parametrize("damage,message", [
    ("missing-node", "bundled Node is missing"),
    ("node-not-executable", "bundled Node is missing or not executable"),
    ("wrapper-not-executable", "wrapper is not an executable file"),
    ("missing-cli", "package is incomplete"),
    ("dangling-wrapper", "wrapper is not an executable file"),
])
def test_an_incomplete_new_runtime_fails_closed_instead_of_using_the_legacy_launcher(host, damage, message):
    runtime = bundled_runtime(host)
    # The legacy launcher exists and would work with the host Node, so selecting it would be the silent fallback.
    (runtime / "node_modules/.bin").mkdir()
    (runtime / "node_modules/.bin/agent-browser").symlink_to("../agent-browser/bin/agent-browser.js")
    if damage == "missing-node":
        (runtime / "node/bin/node").unlink()
    elif damage == "node-not-executable":
        (runtime / "node/bin/node").chmod(0o644)
    elif damage == "wrapper-not-executable":
        (runtime / "run-agent-browser").chmod(0o644)
    elif damage == "missing-cli":
        (runtime / "node_modules/agent-browser/bin/agent-browser.js").unlink()
    else:
        (runtime / "run-agent-browser").unlink()
        (runtime / "run-agent-browser").symlink_to(host / "absent")
    with pytest.raises(browser.BrowserError, match=message):
        browser.runtime_executable(str(runtime))


def test_an_older_runtime_without_the_wrapper_keeps_its_existing_selection(host):
    runtime = legacy_runtime(host)
    executable = browser.runtime_executable(str(runtime))
    assert executable == runtime / "node_modules/.bin/agent-browser"
    assert browser.runtime_directory(executable) == runtime
    (runtime / "node_modules/agent-browser/bin/agent-browser.js").chmod(0o644)
    with pytest.raises(browser.BrowserError, match="executable is unavailable"):
        browser.runtime_executable(str(runtime))
    stale = host / "stale"
    package(stale, version="0.0.1")
    with pytest.raises(browser.BrowserError, match="executable is unavailable"):
        browser.runtime_executable(str(stale))
    with pytest.raises(browser.BrowserError, match="absolute"):
        browser.runtime_executable("relative/runtime")


def test_the_selected_wrapper_is_the_first_element_of_the_translated_command(host, tmp_path):
    runtime = bundled_runtime(host)
    executable = browser.runtime_executable(str(runtime))
    command = agent_browser.command(executable, "session", "http://127.0.0.1:19400", tmp_path, ["snapshot"])
    assert command[0] == str(runtime / "run-agent-browser") and command[1:3] == ["--config", str(tmp_path / "agent-browser.json")]


def test_the_run_helper_executes_the_bundled_node_through_the_wrapper_despite_a_host_node(host, tmp_path):
    runtime = bundled_runtime(host)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    code, stdout, _ = browser.run(workspace, ["snapshot"], target=("http://127.0.0.1:19400", browser.runtime_executable(str(runtime))),
                                  instance="x", capture=True)
    # `snapshot` is not routed through observation; the bundled fake Node answered, the host Node did not.
    assert code == 0 and stdout.splitlines()[0] == "bundled-node" and "--cdp" in stdout
    assert not (host / "host-used").exists()


@pytest.mark.parametrize("layout", ["bundled", "legacy"])
def test_a_configured_browser_target_reports_the_runtime_directory_for_either_layout(host, tmp_path, layout):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from bazarlyk.browser.access import BusinessBrowserAccess
    runtime = bundled_runtime(host) if layout == "bundled" else legacy_runtime(host)
    workspace = tmp_path / "agent"
    (workspace / ".bazarlyk").mkdir(parents=True)
    (workspace / ".bazarlyk/browser.json").write_text(json.dumps(
        {"schema_version": 1, "cdp_endpoint": "http://127.0.0.1:9225", "runtime_dir": str(runtime)}))

    class Connection:
        def execute(self, *_):
            return SimpleNamespace(fetchone=lambda: {"workspace": str(workspace)})

    class Database:
        @contextmanager
        def read(self):
            yield SimpleNamespace(connection=Connection())
    access = BusinessBrowserAccess(SimpleNamespace(database=Database()))
    target = access._worker_target("configured:agent-1", None, "agent-1")
    assert target["runtime_dir"] == str(runtime)
    assert browser.runtime_executable(target["runtime_dir"]) == browser.load_target(workspace)[1]
