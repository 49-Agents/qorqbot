import json

import pytest

from bazarlyk.sandbox import tmux_session


def request(session="agent-one"):
    return {"session": session, "shell": ["/bin/bash", "-i"], "cwd": "/work",
            "env": {"CANARY": "one", "PRIVATE": "secret"}, "cols": 120, "rows": 32}


def test_helper_is_idempotent_and_does_not_rewrite_existing_session(tmp_path, monkeypatch):
    monkeypatch.setattr(tmux_session, "_LOCK", str(tmp_path / "lock"))
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode, self.stdout, self.stderr = returncode, "", ""

    def run(*args):
        calls.append(args)
        return Result(0 if len(calls) == 1 else 99)

    monkeypatch.setattr(tmux_session, "_run", run)
    assert tmux_session.ensure(request()) == 0
    assert calls == [("has-session", "-t", "=agent-one")]


def test_helper_creates_session_with_stdin_environment_and_no_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(tmux_session, "_LOCK", str(tmp_path / "lock"))
    calls = []

    class Result:
        def __init__(self, returncode):
            self.returncode, self.stdout, self.stderr = returncode, "", ""

    def run(*args):
        calls.append(args)
        return Result(1 if len(calls) == 1 else 0)

    monkeypatch.setattr(tmux_session, "_run", run)
    assert tmux_session.ensure(request()) == 0
    assert calls[0] == ("has-session", "-t", "=agent-one")
    assert calls[1][:10] == ("new-session", "-d", "-s", "agent-one", "-c", "/work", "-x", "120", "-y", "32")
    assert "-e" in calls[1] and "CANARY=one" in calls[1] and "PRIVATE=secret" in calls[1]
    assert calls[1][-2:] == ("/bin/bash", "-i")
    assert calls[2] == ("set-option", "-w", "-t", "=agent-one:", "window-size", "manual")


@pytest.mark.parametrize("value", [
    {"session": "bad/name"},
    {"session": "agent", "shell": [], "cwd": "/work", "env": {}, "cols": 120, "rows": 32},
])
def test_helper_rejects_malformed_request(value):
    with pytest.raises(ValueError):
        tmux_session._validate(value)


def test_main_reads_bounded_json_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr(tmux_session.sys, "stdin", type("Input", (), {"buffer": type("Buffer", (), {
        "read": lambda self, size: json.dumps(request()).encode()
    })()})())
    monkeypatch.setattr(tmux_session, "ensure", lambda value: 0)
    assert tmux_session.main() == 0
    assert capsys.readouterr().err == ""
