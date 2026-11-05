import sys
"""Synthetic exact-session inputs only; these tests never open Claude's files."""

import io
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from bazarlyk.bootstrap import transcript_snapshot
from bazarlyk.bootstrap.claude_session import run as bootstrap
from bazarlyk.bootstrap.client import load_registration
from bazarlyk.bootstrap.preparation import prepare_claude_agent
from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.service.main import assemble, transcript_capture_configuration
from bazarlyk.service.instance_arguments import instance_arguments
from bazarlyk.service.local import LocalFiles
from bazarlyk.transport.terminals import TerminalService
from test_agent_configuration import Fixture, REPO, PATHS
from test_service_transports import ServiceFixture

SESSION = "synthetic-session-1"


def record(workspace, *, session_id=SESSION, kind="user", text="Synthetic message"):
    return {"type": kind, "sessionId": session_id, "cwd": str(workspace), "isSidechain": False,
            "message": {"role": kind, "content": [{"type": "text", "text": text}]}}


def jsonl(*records):
    return b"".join((json.dumps(value) + "\n").encode() for value in records)


@pytest.fixture
def prepared(tmp_path):
    workspace = tmp_path / "business/hq/agents/business-agent"
    registration = workspace / ".bazarlyk/registration.json"
    registration.parent.mkdir(parents=True)
    registration.write_text(json.dumps({"schema_version": 1, "registration_id": new_id(), "agent_id": new_id(),
        "workspace": str(workspace), "service_url": "http://127.0.0.1:9988", "credential_file": ".bazarlyk/credential",
        "capture_transcripts": True}))
    registration.chmod(0o600)
    source = tmp_path / "synthetic-claude-input" / (SESSION + ".jsonl")
    source.parent.mkdir()
    source.write_bytes(jsonl(record(workspace)))
    hook = {"hook_event_name": "Stop", "session_id": SESSION, "cwd": str(workspace), "transcript_path": str(source)}
    return SimpleNamespace(workspace=workspace, registration=registration, source=source, hook=hook,
        destination=workspace / "sessions" / source.name)


def forbid_transcript_open(monkeypatch, refused):
    # Registration validation now uses descriptor-safe I/O too. Guard only the
    # snapshot module's source opens, leaving the required registration read real.
    guarded = SimpleNamespace(**vars(os))
    guarded.open = refused
    monkeypatch.setattr(transcript_snapshot, "os", guarded)


@pytest.mark.parametrize("selection", [False, None])
def test_capture_is_noop_before_any_transcript_path_access_without_registration_opt_in(prepared, monkeypatch, selection):
    value = json.loads(prepared.registration.read_text())
    if selection is None:
        del value["capture_transcripts"]
    else:
        value["capture_transcripts"] = selection
    prepared.registration.write_text(json.dumps(value))

    def refused(*args, **kwargs):
        raise AssertionError("Disabled capture must not inspect or open a transcript")

    forbid_transcript_open(monkeypatch, refused)
    for key in ("cwd", "session_id", "transcript_path"):
        prepared.hook[key] = object()
    assert transcript_snapshot.snapshot(prepared.hook, prepared.registration) is False
    assert not (prepared.workspace / "sessions").exists()


@pytest.mark.parametrize("selection", ["true", "false", 1, None, [], {}])
def test_invalid_registration_capture_selection_fails_before_opening_source(prepared, monkeypatch, selection):
    value = json.loads(prepared.registration.read_text())
    value["capture_transcripts"] = selection
    prepared.registration.write_text(json.dumps(value))
    with pytest.raises(DomainError, match="must be a boolean"):
        load_registration(prepared.registration)
    forbid_transcript_open(monkeypatch, lambda *args, **kwargs: pytest.fail("Source was opened"))
    errors = io.StringIO()
    assert transcript_snapshot.run(prepared.hook, prepared.registration, diagnostics=errors) == 0
    assert errors.getvalue() == "Bazarlyk transcript snapshot skipped.\n"


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"])
def test_exact_session_snapshot_is_private_and_refreshes_atomically(prepared, monkeypatch, event):
    prepared.hook["hook_event_name"] = event
    assert transcript_snapshot.snapshot(prepared.hook, prepared.registration)
    original = prepared.destination.read_bytes()
    assert original == prepared.source.read_bytes()
    assert prepared.destination.stat().st_mode & 0o777 == 0o600
    assert prepared.destination.parent.stat().st_mode & 0o777 == 0o700
    refreshed = original + jsonl({"type": "progress", "sessionId": SESSION}, record(prepared.workspace, kind="assistant", text="Later synthetic reply"))
    prepared.source.write_bytes(refreshed)
    replace = os.replace

    def checked_replace(source, destination):
        assert Path(destination).read_bytes() == original
        assert Path(source).read_bytes() == refreshed
        assert Path(source).stat().st_mode & 0o777 == 0o600
        replace(source, destination)

    monkeypatch.setattr(os, "replace", checked_replace)
    assert transcript_snapshot.snapshot(prepared.hook, prepared.registration)
    assert prepared.destination.read_bytes() == refreshed
    assert list(prepared.destination.parent.iterdir()) == [prepared.destination]


@pytest.mark.parametrize("change", ["cwd", "traversal", "filename", "relative", "event", "id"])
def test_input_identity_checks_precede_source_open(prepared, monkeypatch, change):
    if change == "cwd":
        prepared.hook["cwd"] = str(prepared.workspace.parent / "teammate")
    elif change == "traversal":
        prepared.hook["transcript_path"] = str(prepared.source.parent / ".." / prepared.source.parent.name / prepared.source.name)
    elif change == "filename":
        prepared.hook["transcript_path"] = str(prepared.source.with_name("other-session.jsonl"))
    elif change == "relative":
        prepared.hook["transcript_path"] = prepared.source.name
    elif change == "event":
        prepared.hook["hook_event_name"] = "PreToolUse"
    else:
        prepared.hook["session_id"] = "../../not-an-agent"
    forbid_transcript_open(monkeypatch, lambda *args, **kwargs: pytest.fail("Source was opened"))
    with pytest.raises(ValueError):
        transcript_snapshot.snapshot(prepared.hook, prepared.registration)


@pytest.mark.parametrize("target", ["source", "source_parent", "destination", "destination_parent", "registration"])
def test_symlinks_are_refused_without_changing_their_targets(prepared, target):
    if target == "source_parent":
        original = prepared.source.parent
        preserved = original.with_name("preserved-input")
        original.rename(preserved)
    elif target == "destination_parent":
        original = prepared.destination.parent
        preserved = prepared.workspace / "unrelated-folder"
        preserved.mkdir()
    else:
        original = {"source": prepared.source, "destination": prepared.destination, "registration": prepared.registration}[target]
        original.parent.mkdir(exist_ok=True)
        preserved = original.with_name("preserved-file")
        if original.exists():
            original.rename(preserved)
        else:
            preserved.write_bytes(b"private unrelated data")
    original.symlink_to(preserved, target_is_directory=preserved.is_dir())
    before = {path: path.read_bytes() for path in preserved.rglob("*") if path.is_file()} if preserved.is_dir() else preserved.read_bytes()
    assert transcript_snapshot.run(prepared.hook, prepared.registration, diagnostics=io.StringIO()) == 0
    after = {path: path.read_bytes() for path in preserved.rglob("*") if path.is_file()} if preserved.is_dir() else preserved.read_bytes()
    assert after == before
    assert original.is_symlink()


@pytest.mark.parametrize("damage", ["different_session", "different_workspace", "no_workspace", "sidechain", "legacy",
    "wrong_message", "wrong_content", "mixed_session", "partial", "non_utf8", "too_large", "fifo"])
def test_invalid_or_unfinished_source_preserves_previous_snapshot(prepared, monkeypatch, damage):
    transcript_snapshot.snapshot(prepared.hook, prepared.registration)
    previous = prepared.destination.read_bytes()
    value = record(prepared.workspace)
    if damage == "different_session":
        value["sessionId"] = "someone-else"
    elif damage == "different_workspace":
        value["cwd"] = str(prepared.workspace.parent / "teammate")
    elif damage == "no_workspace":
        del value["cwd"]
    elif damage == "sidechain":
        value["isSidechain"] = True
    elif damage == "legacy":
        value = {"ts": "2026-09-08", "kind": "assistant", "agent": "growth", "content": "Old harness"}
    elif damage == "wrong_message":
        value["message"] = "not a Claude message"
    elif damage == "wrong_content":
        value["message"]["content"] = ["not a Claude content block"]
    contents = jsonl(value)
    if damage == "mixed_session":
        contents += jsonl({"type": "progress", "sessionId": "another-session"})
    elif damage == "partial":
        contents += b'{"type":"assistant","private":"unfinished'
    elif damage == "non_utf8":
        contents += b"\xff\n"
    elif damage == "too_large":
        monkeypatch.setattr(transcript_snapshot, "MAX_TRANSCRIPT_BYTES", len(contents) - 1)
    prepared.source.write_bytes(contents)
    if damage == "fifo":
        prepared.source.unlink()
        os.mkfifo(prepared.source)
    diagnostics = io.StringIO()
    assert transcript_snapshot.run(prepared.hook, prepared.registration, diagnostics=diagnostics) == 0
    assert diagnostics.getvalue() == "Bazarlyk transcript snapshot skipped.\n"
    assert prepared.destination.read_bytes() == previous
    assert list(prepared.destination.parent.iterdir()) == [prepared.destination]


def test_failed_atomic_replace_leaves_previous_snapshot_and_no_temporary_file(prepared, monkeypatch):
    transcript_snapshot.snapshot(prepared.hook, prepared.registration)
    previous = prepared.destination.read_bytes()
    prepared.source.write_bytes(previous + jsonl(record(prepared.workspace, kind="assistant")))

    def refused(*args):
        raise OSError("private path and content must not appear in diagnostics")

    monkeypatch.setattr(os, "replace", refused)
    diagnostics = io.StringIO()
    transcript_snapshot.run(prepared.hook, prepared.registration, diagnostics=diagnostics)
    assert diagnostics.getvalue() == "Bazarlyk transcript snapshot skipped.\n"
    assert prepared.destination.read_bytes() == previous
    assert list(prepared.destination.parent.iterdir()) == [prepared.destination]


def test_successful_bootstrap_refreshes_same_session_but_capture_errors_never_block(prepared):
    class Client:
        def __init__(self, registration):
            pass
        def post(self, route, values):
            return {"ready": True, "effective_config_id": "config", "mandatory_context": "Synthetic current context"}

    for event in ("SessionStart", "UserPromptSubmit"):
        output, errors = io.StringIO(), io.StringIO()
        bootstrap({**prepared.hook, "hook_event_name": event}, prepared.registration,
            stream=output, diagnostics=errors, client_factory=Client)
        assert "decision" not in json.loads(output.getvalue())
        assert prepared.destination.read_bytes() == prepared.source.read_bytes()
        prepared.source.write_bytes(prepared.source.read_bytes() + jsonl(record(prepared.workspace, kind="assistant")))
    previous = prepared.destination.read_bytes()
    prepared.source.write_bytes(b'{"unfinished":')
    output, errors = io.StringIO(), io.StringIO()
    bootstrap({**prepared.hook, "hook_event_name": "UserPromptSubmit"}, prepared.registration,
        stream=output, diagnostics=errors, client_factory=Client)
    assert json.loads(output.getvalue()) == {}
    assert errors.getvalue() == "Bazarlyk transcript snapshot skipped.\n"
    assert prepared.destination.read_bytes() == previous


def test_refused_bootstrap_does_not_read_transcript_even_with_opt_in(prepared, monkeypatch):
    class Client:
        def __init__(self, registration):
            pass
        def post(self, route, values):
            raise DomainError("reprepare_required", "Synthetic revoked registration")

    monkeypatch.setattr(transcript_snapshot, "snapshot", lambda *args: pytest.fail("Refused bootstrap tried to capture"))
    output = io.StringIO()
    bootstrap({**prepared.hook, "hook_event_name": "UserPromptSubmit"}, prepared.registration,
        stream=output, diagnostics=io.StringIO(), client_factory=Client)
    assert json.loads(output.getvalue())["decision"] == "block"


@pytest.mark.parametrize("setting,expected", [(None, True), ("false", False), ("true", True), (" TRUE ", True)])
def test_service_only_capture_setting_is_explicit_boolean(monkeypatch, setting, expected):
    monkeypatch.delenv("BAZARLYK_CAPTURE_TRANSCRIPTS", raising=False)
    if setting is not None:
        monkeypatch.setenv("BAZARLYK_CAPTURE_TRANSCRIPTS", setting)
    assert transcript_capture_configuration() is expected
    assert transcript_capture_configuration(False) is False


@pytest.mark.parametrize("setting", ["yes", "1", "0", "", "enabled"])
def test_invalid_service_capture_setting_is_rejected(monkeypatch, setting):
    monkeypatch.setenv("BAZARLYK_CAPTURE_TRANSCRIPTS", setting)
    with pytest.raises(DomainError, match="must be true or false"):
        transcript_capture_configuration()


@pytest.mark.parametrize("role", ["business-agent", "ordubashy", "learning-applier"])
def test_default_capture_for_business_and_global_agents_uses_installed_runtime(tmp_path, role):
    fixture = Fixture(tmp_path)
    scope = {"scope": {"kind": "ecosystem", "allowed_business_ids": [fixture.business_id], "global_capabilities": ["read_global_context"]}} if role == "learning-applier" else {}
    principal = fixture.create() if role == "business-agent" else fixture.create(role, role=role, **scope)
    first = fixture.prepare(principal)
    workspace = Path(first["workspace"])
    path = workspace / ".bazarlyk/registration.json"
    assert load_registration(path)["capture_transcripts"] is True
    assert "sessions/" in (workspace / ".gitignore").read_text().splitlines()
    enabled = prepare_claude_agent(fixture.resolver, principal, endpoint="http://127.0.0.1:9988",
        claude_version="2.1.263 (Claude Code)", capture_transcripts=True)
    assert enabled["ready"]
    assert enabled["settings_digest"] == first["settings_digest"]
    assert load_registration(path)["capture_transcripts"] is True
    hooks = json.loads((workspace / ".claude/settings.local.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd",
                          "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"}
    source = tmp_path / (SESSION + ".jsonl")
    source.write_bytes(jsonl(record(workspace)))
    event = {"hook_event_name": "Stop", "session_id": SESSION, "cwd": str(workspace), "transcript_path": str(source)}
    for name in ("Stop", "SessionEnd"):
        command = shlex.split(hooks[name][0]["hooks"][0]["command"])
        assert command[:4] == [sys.executable, "-I", "-m", "bazarlyk.bootstrap.transcript_snapshot"]
        completed = subprocess.run(command, input=json.dumps({**event, "hook_event_name": name}), cwd=workspace,
            capture_output=True, text=True, check=True, timeout=5, env={"PATH": "/usr/bin:/bin"})
        assert completed.stdout == completed.stderr == ""
    destination = workspace / "sessions" / source.name
    assert destination.read_bytes() == source.read_bytes()
    if role == "business-agent":
        local = LocalFiles(SimpleNamespace(database=fixture.db, root=fixture.root))
        snapshots = local.transcripts(fixture.business_id, principal.id)
        assert len(snapshots) == 1
        assert snapshots[0]["session_id"] == SESSION
        assert snapshots[0]["agent_id"] == principal.id
        assert snapshots[0]["content"].encode() == source.read_bytes()
        assert snapshots[0]["transcript_hash"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert snapshots[0]["source_id"] == destination.relative_to(fixture.root).as_posix()
    disabled = prepare_claude_agent(fixture.resolver, principal, endpoint="http://127.0.0.1:9988",
        claude_version="2.1.263", capture_transcripts=False)
    assert disabled["ready"]
    assert load_registration(path)["capture_transcripts"] is False
    disabled_hooks = json.loads((workspace / ".claude/settings.local.json").read_text())["hooks"]
    assert set(disabled_hooks) == set(hooks)
    assert all('bazarlyk.bootstrap.transcript_snapshot' not in hook['command']
               for groups in disabled_hooks.values() for group in groups for hook in group['hooks'])
    source.unlink()
    assert transcript_snapshot.snapshot(event, path) is False
    assert destination.is_file()


def test_service_and_terminal_use_trusted_selection_not_agent_environment(tmp_path, monkeypatch):
    service = ServiceFixture(tmp_path)
    monkeypatch.setenv("BAZARLYK_CAPTURE_TRANSCRIPTS", "true")
    app = assemble(service.fixture.root, REPO, paths=PATHS, origins=["http://testserver"], endpoint="http://testserver/mcp",
        database=service.fixture.db, signing_key=b"fixture-key" * 8, nightly_maintenance=None)
    assert app.state.http_api.terminals.capture_transcripts is True
    with service.fixture.db.read() as uow:
        business = uow.repository("businesses").get(service.fixture.business_id)
    calls = []
    broker = SimpleNamespace(tmux_available=lambda: True, session_name=lambda *args: "synthetic-mirror",
        ensure_session=lambda *args, **kwargs: None, attach_cmd=lambda *args: ["synthetic-mirror"],
        get_or_start=lambda *args, **kwargs: object())
    terminal = TerminalService(service.runtime, broker, service_url="http://testserver/mcp",
        capture_transcripts=False, environment=lambda _: {"BAZARLYK_CAPTURE_TRANSCRIPTS": "true"},
        preparation=lambda *args, **kwargs: calls.append(kwargs), version_provider=lambda: "2.1.263")
    terminal.open(service.fixture.ctx, business, service.agent.id)
    assert calls[0]["capture_transcripts"] is False
    assert not (service.fixture.hq / "agents/business-agent/sessions").exists()


def test_local_input_selects_newest_twenty_copies_with_stable_filename_ties(tmp_path):
    fixture = Fixture(tmp_path)
    principal = fixture.create()
    ready = prepare_claude_agent(fixture.resolver, principal, endpoint="http://127.0.0.1:9988",
        claude_version="2.1.263", capture_transcripts=True)
    workspace = Path(ready["workspace"])
    source_directory = tmp_path / "synthetic-claude-input"
    source_directory.mkdir()
    names = []
    for index in range(25):
        session_id = f"{25 - index:08x}-0000-4000-8000-{index:012x}"
        names.append(session_id)
        source = source_directory / (session_id + ".jsonl")
        source.write_bytes(jsonl(record(workspace, session_id=session_id)))
        assert transcript_snapshot.snapshot({"hook_event_name": "Stop", "session_id": session_id,
            "cwd": str(workspace), "transcript_path": str(source)}, workspace / ".bazarlyk/registration.json")
        modified = 1_700_000_000_000_000_000 + min(index, 23) * 1000
        os.utime(workspace / "sessions" / source.name, ns=(modified, modified))
    (workspace / "sessions/ffffffff-0000-4000-8000-000000000000.jsonl").symlink_to(source)
    local = LocalFiles(SimpleNamespace(database=fixture.db, root=fixture.root))
    snapshots = local.transcripts(fixture.business_id, principal.id)
    assert [snapshot["session_id"] for snapshot in snapshots] == [names[23], names[24], *reversed(names[5:23])]
    assert len(snapshots) == 20
    assert all(snapshot["agent_id"] == principal.id for snapshot in snapshots)


@pytest.mark.parametrize("selection", [True, False])
def test_explicit_service_prepare_command_passes_service_selection(tmp_path, monkeypatch, selection):
    from bazarlyk.service.main import main

    service = ServiceFixture(tmp_path)
    monkeypatch.setenv("BAZARLYK_CAPTURE_TRANSCRIPTS", "true" if selection else "false")
    assert main(["--root", str(service.fixture.root), "--workspace", str(REPO), *instance_arguments(PATHS), "--port", "13377",
                 "--prepare-agent", "widget", service.agent.id, "--claude-version", "2.1.263"]) == 0
    registration = load_registration(service.fixture.hq / "agents/business-agent/.bazarlyk/registration.json")
    assert registration["agent_id"] == service.agent.id
    assert registration["capture_transcripts"] is selection


def test_snapshot_command_invalid_input_remains_nonblocking(prepared):
    command = [sys.executable, "-I", "-m", "bazarlyk.bootstrap.transcript_snapshot",
               "--registration", str(prepared.registration)]
    completed = subprocess.run(command, input="{invalid", capture_output=True, text=True, check=True,
        timeout=5, env={"PATH": "/usr/bin:/bin"})
    assert completed.stdout == ""
    assert completed.stderr == "Bazarlyk transcript snapshot skipped: invalid hook input.\n"
    assert not prepared.destination.exists()
