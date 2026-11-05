import json
import os
from pathlib import Path
from types import SimpleNamespace
import shlex
import time

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.docker import Ownership
from bazarlyk.sandbox.docker import Docker
from bazarlyk.sandbox.tmux import BOOT_COMMAND, DockerTmuxBroker
from bazarlyk.transport.terminals import TerminalService


BUSINESS_ONE = "00000000-0000-4000-8000-000000000001"
BUSINESS_TWO = "00000000-0000-4000-8000-000000000002"
CONTAINER_ONE = "a" * 64
CONTAINER_TWO = "b" * 64


class DockerStub:
    command = ("sudo", "-n", "docker")

    def __init__(self, ownership, container_id, *, running=True, output="", generation=3, role="agent", status=0):
        self.ownership = ownership
        self.container_id = container_id
        self.running = running
        self.output = output
        self.generation = generation
        self.role = role
        self.status = status
        self.owned_calls, self.exec_calls = [], []

    def owned(self, kind, identity, ownership):
        self.owned_calls.append((kind, identity, ownership))
        if kind != "container" or identity != self.container_id:
            return None
        labels = {**ownership.labels, "qorqut.role": self.role, "qorqut.generation": str(self.generation)}
        if ownership != self.ownership:
            labels["qorqut.business"] = BUSINESS_TWO
        ownership.require(labels)
        return {"Config": {"Labels": labels}, "State": {"Running": self.running}}

    def exec(self, container_id, ownership, *arguments, input=None, timeout=90):
        self.exec_calls.append((container_id, ownership, arguments, input, timeout))
        if arguments[:1] == ("/opt/qorqut/venv/bin/python",):
            return "QORQUT_STATUS:1"
        if len(arguments) >= 2 and arguments[:2] in (("sh", "-c"), ("/bin/sh", "-c")):
            if "has-session" in arguments[2] or "kill-session" in arguments[2]:
                body = "can't find session: =one__agent" if self.status == 1 else ""
                return f"{body}\nQORQUT_STATUS:{self.status}\n"
            if "list-sessions" in arguments[2]:
                if self.status == 1 and not self.output:
                    return f"no server running on /tmp/tmux-1000/default\nQORQUT_STATUS:{self.status}\n"
                return self.output + f"\nQORQUT_STATUS:{self.status}\n"
        return self.output


class Mirror:
    def __init__(self):
        self.calls, self.mirrors = [], {}

    def session_name(self, slug, agent):
        return f"{slug}__{agent}"

    def session_prefix(self, slug):
        return f"{slug}__"

    def get_or_start(self, key, command, cwd, **kwargs):
        self.calls.append(("get_or_start", key, command, cwd, kwargs))
        return self.mirrors.setdefault(key, object())

    def get(self, key):
        self.calls.append(("get", key))
        return self.mirrors.get(key)

    def stop(self, key):
        self.calls.append(("stop", key))
        return self.mirrors.pop(key, None) is not None


def broker(container_id=CONTAINER_ONE, business_id=BUSINESS_ONE, *, generation=3, output=""):
    ownership = Ownership("a" * 20, business_id)
    docker = DockerStub(ownership, container_id, output=output, generation=generation)
    mirror = Mirror()
    return DockerTmuxBroker(docker, ownership, container_id, generation, mirror), docker, mirror


def test_two_business_targets_namespace_host_mirrors_and_keep_secrets_off_argv():
    first, docker_one, mirror = broker()
    second, docker_two, _ = broker(CONTAINER_TWO, BUSINESS_TWO, generation=4)
    second = DockerTmuxBroker(docker_two, second.ownership, second.container_id, second.generation, mirror)
    secret = "business-secret-value"
    session_one, session_two = first.session_name("one", "agent"), second.session_name("two", "agent")

    first.ensure_session(session_one, ["/bin/bash", "-i"], "/business/one", env={"PRIVATE": secret})
    first.get_or_start("agent-key", first.attach_cmd(session_one), "/business/one")
    second.ensure_session(session_two, ["/bin/bash", "-i"], "/business/two", env={"PRIVATE": secret})
    second.get_or_start("agent-key", second.attach_cmd(session_two), "/business/two")

    payload = json.loads(docker_one.exec_calls[0][3])
    assert payload["env"] == {"PRIVATE": secret}
    assert secret not in " ".join(docker_one.exec_calls[0][2])
    assert secret not in " ".join(mirror.calls[0][2])
    keys = [call[1] for call in mirror.calls if call[0] == "get_or_start"]
    assert keys[0] != keys[1]
    assert BUSINESS_ONE in keys[0] and CONTAINER_ONE in keys[0] and "\x003" in keys[0]
    assert BUSINESS_TWO in keys[1] and CONTAINER_TWO in keys[1] and "\x004" in keys[1]
    assert docker_two.exec_calls[0][2] == BOOT_COMMAND


def test_every_container_operation_rechecks_exact_owned_running_target():
    target, docker, mirror = broker(output="one__agent\ntwo__agent\n")
    target.tmux_available()
    target.list_sessions("one__")
    target.session_alive(target.session_name("one", "agent"))
    target.resize_session(target.session_name("one", "agent"), 24, 90)
    target.kill_session(target.session_name("one", "agent"))
    target.get("agent-key")
    assert len(docker.owned_calls) >= 6
    assert target.list_sessions("one__") == ["one__agent"]

    docker.running = False
    before = len(mirror.calls)
    with pytest.raises(DomainError, match="not running"):
        target.attach_cmd(target.session_name("one", "agent"))
    assert len(mirror.calls) == before


def test_canonical_terminal_binding_can_attach_reconnect_and_stop(tmp_path):
    target, docker, mirror = broker()
    root = tmp_path.resolve()
    hq = root / "one/hq"
    workspace = hq / "agents/coder"
    workspace.mkdir(parents=True)
    terminal = TerminalService(SimpleNamespace(root=root, workspace=root), target,
        service_url="http://127.0.0.1:1148")
    key, session, cwd = terminal.binding({"hq_path": str(hq), "slug": "one"},
        {"workspace": str(workspace)})
    assert "\0" in key
    attached = target.get_or_start(key, target.attach_cmd(session), cwd)
    assert target.get(key) is attached
    assert target.get_or_start(key, target.attach_cmd(session), cwd) is attached
    assert all("\0" not in value for call in mirror.calls if call[0] == "get_or_start" for value in call[2])
    assert target.stop(key)
    assert target.get(key) is None


@pytest.mark.parametrize("field", ["role", "generation"])
def test_container_identity_requires_agent_role_and_observed_generation(field):
    target, docker, _ = broker()
    if field == "role":
        docker.role = "gateway"
    else:
        docker.generation = 4
    with pytest.raises(DomainError, match="generation is not current"):
        target.tmux_available()


@pytest.mark.parametrize(("output", "body", "status"), [
    ("QORQUT_STATUS:0", "", 0),
    ("agent-one\nQORQUT_STATUS:1", "agent-one", 1),
    ("\nQORQUT_STATUS:127", "", 127),
])
def test_status_marker_survives_docker_stdout_trimming(output, body, status):
    assert DockerTmuxBroker._status(output, operation="fixture") == (body, status)


def test_tmux_absent_server_is_distinct_from_tool_failure():
    target, docker, _ = broker()
    docker.status = 1
    assert target.list_sessions("one__") == []
    assert not target.session_alive(target.session_name("one", "agent"))
    assert not target.kill_session(target.session_name("one", "agent"))
    docker.status = 2
    with pytest.raises(DomainError, match="listing failed"):
        target.list_sessions("one__")
    docker.status = 1
    docker.output = "permission denied"
    with pytest.raises(DomainError, match="listing failed"):
        target.list_sessions("one__")


@pytest.mark.parametrize("container_id", ["named-container", "A" * 64, "a" * 63])
def test_stale_or_unverifiable_container_identity_fails_closed(container_id):
    ownership = Ownership("a" * 20, BUSINESS_ONE)
    with pytest.raises(DomainError):
        DockerTmuxBroker(DockerStub(ownership, CONTAINER_ONE), ownership, container_id, 1, Mirror())

    target, docker, _ = broker()
    docker.ownership = Ownership("a" * 20, BUSINESS_TWO)
    with pytest.raises(DomainError, match="another environment"):
        target.tmux_available()


def test_real_container_tmux_sessions_reconnect_and_reject_foreign_target(tmp_path):
    """Opt-in smoke test for an image containing tmux and the installed helper.

    The image is supplied by the image fixture/build job.  Keeping this opt-in
    avoids silently starting Docker during the ordinary unit suite.
    """
    image = os.environ.get("QORQUT_TEST_TMUX_IMAGE")
    if not image:
        pytest.skip("set QORQUT_TEST_TMUX_IMAGE to run the Docker tmux smoke test")
    dashboard_root = Path(os.environ.get("QORQUT_TEST_WORKSPACE_ROOT", "/absolute/path/to/qorqut"))
    if not (dashboard_root / "biz-dashboard/pty_broker.py").is_file():
        pytest.skip("dashboard PTY broker is unavailable")
    from bazarlyk.service.main import load_terminal_broker

    installation = "c" * 20
    ownership = Ownership(installation, BUSINESS_ONE)
    command = tuple(shlex.split(os.environ.get("QORQUT_TEST_DOCKER_COMMAND", "sudo -n docker")))
    docker = Docker(command=command)
    mirror = load_terminal_broker(dashboard_root)
    if not mirror.tmux_available():
        pytest.skip("host tmux is unavailable")
    mirror.TMUX_SOCK = "qorqut-integration-" + BUSINESS_ONE[-8:]
    cwd_one = tmp_path / "container-work-one"
    cwd_two = tmp_path / "container-work-two"
    cwd_one.mkdir()
    cwd_two.mkdir()
    tmp_path.chmod(0o777)
    cwd_one.chmod(0o777)
    cwd_two.chmod(0o777)
    labels = ownership.labels | {"qorqut.role": "agent", "qorqut.generation": "7"}

    def run_container(container_labels, *, mount=True):
        args = ["run", "-d", "--user", "1000:1000", "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--memory", "512m",
                "--memory-swap", "512m", "--cpus", "0.5", "--pids-limit", "128",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                "--tmpfs", "/run:rw,nosuid,nodev,size=16m,mode=755",
                "--tmpfs", "/home/agent:rw,nosuid,nodev,size=64m,mode=700,uid=1000,gid=1000"]
        if mount:
            args += ["--mount", f"type=bind,src={tmp_path},dst={tmp_path}"]
        args += [arg for pair in container_labels.items() for arg in ("--label", f"{pair[0]}={pair[1]}")]
        args += ["--entrypoint", "/bin/sh", image, "-c", "while :; do sleep 60; done"]
        return docker.call(*args).splitlines()[-1].strip()

    container_id = run_container(labels)
    target = DockerTmuxBroker(docker, ownership, container_id, 7, mirror)
    sessions = ("one-" + BUSINESS_ONE[-6:], "two-" + BUSINESS_ONE[-6:])
    key = "agent-key"
    foreign = None
    primary_removed = False
    try:
        target.ensure_session(sessions[0], ["/bin/bash", "-i"], str(cwd_one),
            env={"QORQUT_CANARY": "CANARY_ONE"}, cols=100, rows=24)
        # A retry with changed input must reuse the existing environment.
        target.ensure_session(sessions[0], ["/bin/bash", "-i"], str(cwd_two),
            env={"QORQUT_CANARY": "MUTATED"}, cols=100, rows=24)
        target.ensure_session(sessions[1], ["/bin/bash", "-i"], str(cwd_two),
            env={"QORQUT_CANARY": "CANARY_TWO"}, cols=100, rows=24)
        assert target.session_alive(sessions[1])
        assert target.session_alive(sessions[0])
        first = target.get_or_start(key, target.attach_cmd(sessions[0]), str(cwd_one), cols=100, rows=24)
        first.write(b"printf '\\n%s\\n' \"$QORQUT_CANARY\"; pwd\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and b"CANARY_ONE" not in first.snapshot()[0]:
            time.sleep(0.05)
        assert b"CANARY_ONE" in first.snapshot()[0]
        assert str(cwd_one).encode() in first.snapshot()[0]
        first.resize(24, 90)
        target.resize_session(sessions[0], 24, 90)
        assert target._exec("tmux", "display-message", "-p", "-t", "=" + sessions[0] + ":",
                            "#{window_width}x#{window_height}") == "90x24"
        assert target.stop(key)
        second = target.get_or_start("agent-two", target.attach_cmd(sessions[1]), str(cwd_two), cols=100, rows=24)
        second.write(b"printf '\\n%s\\n' \"$QORQUT_CANARY\"\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and b"CANARY_TWO" not in second.snapshot()[0]:
            time.sleep(0.05)
        assert b"CANARY_TWO" in second.snapshot()[0]
        reconnected = target.get_or_start(key, target.attach_cmd(sessions[0]), str(cwd_one), cols=90, rows=24)
        assert reconnected is not first
        reconnected.write(b"printf '\\n%s\\n' \"$QORQUT_CANARY\"\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and b"CANARY_ONE" not in reconnected.snapshot()[0]:
            time.sleep(0.05)
        assert b"CANARY_ONE" in reconnected.snapshot()[0]
        assert target.kill_session(sessions[1])
        assert not target.session_alive(sessions[1])

        foreign = run_container(ownership.labels | {"qorqut.role": "agent", "qorqut.generation": "8", "qorqut.business": BUSINESS_TWO}, mount=False)
        foreign_target = DockerTmuxBroker(docker, ownership, foreign, 8, mirror)
        with pytest.raises(DomainError, match="another environment"):
            foreign_target.tmux_available()
        docker.call("rm", "-f", foreign)
        foreign = None
        docker.call("rm", "-f", container_id)
        primary_removed = True
        with pytest.raises(DomainError):
            target.tmux_available()
    finally:
        target.stop(key)
        target.stop("agent-two")
        if foreign:
            docker.call("rm", "-f", foreign)
        if not primary_removed:
            docker.call("rm", "-f", container_id)


def test_broadcast_submit_uses_owned_container_and_separate_enter():
    value, docker, mirror = broker()
    message = 'Review now\nThen report.'
    value.submit_input('one__agent', message)
    container, ownership, args, body, _ = docker.exec_calls[-2]
    assert container == CONTAINER_ONE
    assert ownership.business_id == BUSINESS_ONE
    assert body == message and message not in args
    assert args[:2] == ('/usr/bin/tmux', 'load-buffer')
    assert docker.exec_calls[-1][2] == ('/usr/bin/tmux', 'send-keys', '-t', '=one__agent:', 'Enter')
    assert docker.exec_calls[-1][3] is None
    assert 'new-session' not in args
    assert not mirror.calls
    docker.running = False
    with pytest.raises(DomainError):
        value.submit_input('one__agent', message)
