import json
import os
import shlex
import subprocess
from pathlib import Path
import tomllib

import pytest

from bazarlyk.bootstrap import claude_session, codex_session
from bazarlyk.bootstrap.client import load_registration
from bazarlyk.bootstrap.preparation import build_projection, inspect_projection, prepare_agent, runtime_compatibility
from qorqut_operations.contracts import DomainError, new_id

MANAGED_INBOX_NOTE = "Synthetic current managed inbox note."


def test_both_clis_share_registration_pins_and_credentials(tmp_path):
    from test_agent_configuration import Fixture
    fixture = Fixture(tmp_path)
    principal = fixture.create()
    versions = {"claude-code": "2.1.267 (Claude Code)", "codex": "codex-cli 0.153.4"}
    result = prepare_agent(fixture.resolver, principal, endpoint="http://127.0.0.1:9988", versions=versions)
    assert all(result["runtimes"][runtime]["ready"] for runtime in versions)
    workspace = Path(result["workspace"])
    registration_path = workspace / ".bazarlyk/registration.json"
    registration = load_registration(registration_path)
    credential = (workspace / ".bazarlyk/credential").read_bytes()
    config = tomllib.loads((workspace / ".codex/config.toml").read_text())
    connection = config["mcp_servers"]["bazarlyk"]
    assert connection["command"] == "/bin/sh"
    assert connection["args"] == [str(workspace / ".bazarlyk/mcp-bridge")]
    assert connection["startup_timeout_sec"] == connection["tool_timeout_sec"] == 240
    assert str(registration_path) in (workspace / ".bazarlyk/mcp-bridge").read_text()
    assert credential.strip().decode() not in json.dumps(config)
    fs = config["permissions"]["qorqut-agent"]["filesystem"]
    assert fs[str(workspace / "work")] == "write"
    assert fs[str(workspace / ".bazarlyk/credential")] == "deny"
    assert fs[str(fixture.root / ".bazarlyk")] == "deny"
    for path in (workspace / ".claude/skills").rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (workspace / ".agents/skills" / path.relative_to(workspace / ".claude/skills")).read_bytes()
    bundles = []
    for runtime in ("claude-code", "codex", "claude-code"):
        response = fixture.resolver.bootstrap(principal, registration_id=registration["registration_id"],
            session_id=new_id(), event="startup", runtime=runtime)
        assert response["ready"]
        bundles.append(response["mandatory_context"])
    assert bundles[0] == bundles[1] == bundles[2]
    assert "Monitor" not in bundles[0]["rendered"]
    codex_config = workspace / ".codex/config.toml"
    codex_config.write_text("# changed\n")
    status = fixture.resolver.resolve_agent_config(principal).to_dict()["preparation"]
    assert status["ready"] and status["runtimes"]["claude-code"]["ready"]
    assert not status["runtimes"]["codex"]["ready"]
    repaired = prepare_agent(fixture.resolver, principal, endpoint="http://127.0.0.1:9988", versions={"codex": versions["codex"]})
    assert repaired["ready"] and repaired["runtimes"]["codex"]["ready"]
    assert not repaired["runtimes"]["claude-code"]["ready"]
    assert load_registration(registration_path)["registration_id"] == registration["registration_id"]
    assert (workspace / ".bazarlyk/credential").read_bytes() == credential


def test_codex_hook_definitions_survive_release_change(tmp_path):
    from test_agent_configuration import Fixture
    fixture = Fixture(tmp_path)
    principal = fixture.create()
    config = fixture.resolver.resolve_agent_config(principal)
    registration_id = new_id()
    # Interpreter paths with spaces and shell metacharacters must remain argv,
    # never become code in the generated shell helper.
    releases = []
    for name in ('release one', 'release two;not-a-command'):
        interpreter = tmp_path / name / 'python'
        interpreter.parent.mkdir()
        interpreter.write_text('#!/usr/bin/python3\nimport json,sys\nprint(json.dumps({"argv":sys.argv,"input":sys.stdin.read()}))\n')
        interpreter.chmod(0o700)
        releases.append(build_projection(fixture.resolver, config, registration_id=registration_id,
            endpoint='http://127.0.0.1:9988', interpreter=str(interpreter)))
    old, new = releases
    for filename in ('.claude/settings.local.json', '.codex/hooks.json'):
        generated = json.loads(new.files[filename])['hooks']
        for event in ('SessionStart', 'UserPromptSubmit'):
            assert generated[event][0]['hooks'][0]['timeout'] == 240
    assert old.files['.codex/hooks.json'] == new.files['.codex/hooks.json']
    assert old.files['.bazarlyk/codex-hook'] != new.files['.bazarlyk/codex-hook']
    workspace = Path(config.to_dict()['workspace'])
    helper = workspace / '.bazarlyk/codex-hook'
    helper.parent.mkdir(parents=True, exist_ok=True)
    hooks = json.loads(new.files['.codex/hooks.json'])['hooks']
    commands = {item['command'] for groups in hooks.values() for group in groups for item in group['hooks']}
    for projection in releases:
        helper.write_bytes(projection.files['.bazarlyk/codex-hook'])
        for command in commands:
            result = subprocess.run(shlex.split(command), input='{"hook_event_name":"Stop"}',
                capture_output=True, text=True, check=True)
            delivered = json.loads(result.stdout)
            assert delivered['input'] == '{"hook_event_name":"Stop"}'
            assert delivered['argv'][0] in projection.files['.bazarlyk/codex-hook'].decode()
            assert delivered['argv'][1] == '-I'
            assert str(workspace / '.bazarlyk/registration.json') in delivered['argv']
    assert subprocess.run(['/bin/sh', str(helper), 'unknown']).returncode == 64
    assert subprocess.run(['/bin/sh', str(helper), 'bootstrap', 'extra']).returncode == 64
    helper.write_text('# modified\n')
    assert '.bazarlyk/codex-hook' in inspect_projection(workspace, new.metadata, runtime='codex')


@pytest.mark.parametrize("version,supported", [("codex-cli 0.153.4", True), ("codex-cli 0.153.3", False), ("", False)])
def test_codex_version_contract(version, supported):
    assert runtime_compatibility("codex", version)["supported"] is supported


def registration(tmp_path, *, capture=True):
    path = tmp_path / ".bazarlyk/registration.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"schema_version": 1, "registration_id": new_id(), "agent_id": new_id(),
        "workspace": str(tmp_path), "service_url": "http://127.0.0.1:9988", "credential_file": ".bazarlyk/credential",
        "capture_transcripts": capture}))
    return path


def test_codex_hook_uses_same_composer_and_runtime_specific_setup(tmp_path, monkeypatch):
    path = registration(tmp_path, capture=False)
    requests = []
    class Client:
        def __init__(self, registration): pass
        def post(self, route, body):
            requests.append((route, body))
            return {"ready": True, "effective_config_id": "digest", "mandatory_context": "Shared policy",
                "work_briefing": {"rendered": "Current saved work"}, "inbox_available": True,
                "inbox_session_note": MANAGED_INBOX_NOTE}
    hook = {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(tmp_path), "session_id": new_id()}
    output, receipt, _ = claude_session.handle_hook(hook, path, runtime="codex", client_factory=Client)
    assert requests[0][1]["runtime"] == "codex"
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "Shared policy\n\nCurrent saved work" in context
    assert MANAGED_INBOX_NOTE in context and "consumer_id" not in context
    assert "reloadSkills" not in output["hookSpecificOutput"]
    assert "persistent: true" not in context
    assert "timeout_ms: 1800000" not in context
    from bazarlyk.catalog.packages import content_hash
    assert receipt["bundle_hash"] == content_hash(b"Shared policy")


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_compact_memory_yields_to_mandatory_and_consumer_budget(tmp_path, monkeypatch, runtime):
    from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
    path = registration(tmp_path, capture=False)
    mandatory = 'MANDATORY ' + 'x' * (MAX_CONTEXT_CHARACTERS - 10000)
    class Client:
        def __init__(self, registration): pass
        def post(self, route, body):
            return {'ready': True, 'effective_config_id': 'stable', 'mandatory_context': mandatory,
                    'work_briefing': {'rendered': 'OPTIONAL MEMORY ' + 'm' * 11000, 'compact': True},
                    'inbox_available': True, 'inbox_consumer_id': body['session_id'],
                    'inbox_session_note': MANAGED_INBOX_NOTE}
    hook = {'hook_event_name': 'SessionStart', 'source': 'startup', 'cwd': str(tmp_path), 'session_id': new_id()}
    output, receipt, _ = claude_session.handle_hook(hook, path, runtime=runtime, client_factory=Client)
    context = output['hookSpecificOutput']['additionalContext']
    assert context.startswith(mandatory)
    assert len(context) <= MAX_CONTEXT_CHARACTERS
    assert 'OPTIONAL MEMORY' not in context
    assert 'agent_context_get' in context and MANAGED_INBOX_NOTE in context
    assert receipt['session_id'] == hook['session_id']


@pytest.fixture
def codex_process_tree(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    home = tmp_path / "codex-home"
    proc.mkdir()
    home.mkdir()
    monkeypatch.setattr(codex_session, "PROC_ROOT", proc)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(codex_session.os, "getppid", lambda: 10)

    def process(pid, parent, argv, *, executable="/installed/codex", ticks="123", state="S"):
        path = proc / str(pid)
        path.mkdir(exist_ok=True)
        (path / "fd").mkdir(exist_ok=True)
        (path / "fdinfo").mkdir(exist_ok=True)
        (path / "stat").write_text(f"{pid} (runtime) " + " ".join([state, str(parent)] + ["0"] * 17 + [ticks]))
        (path / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
        (path / "exe").symlink_to(executable)
        return path

    process(10, 20, ["python", "-m", "bazarlyk.bootstrap.claude_session"], executable="/bin/python")
    return proc, home, process


@pytest.mark.parametrize("deleted", [False, True])
def test_codex_shared_server_uses_exact_held_thread_lock(codex_process_tree, deleted):
    _, home, process = codex_process_tree
    session_id = new_id()
    server = process(20, 1, ["codex", "-c", "features.code_mode_host=true", "app-server", "--listen", "unix://"],
                     executable="/installed/codex" + (" (deleted)" if deleted else ""))
    lock = home / "thread-writer-locks" / (session_id + ".lock")
    lock.parent.mkdir()
    lock.touch()
    (server / "fd/7").symlink_to(lock)
    info = server / "fdinfo/7"
    device, inode = lock.stat().st_dev, lock.stat().st_ino
    lock_info = f"ino:\t{inode}\nlock:\t1: FLOCK ADVISORY WRITE 20 {os.major(device):x}:{os.minor(device):x}:{inode} 0 EOF\n"
    info.write_text(lock_info)
    owner = codex_session.session_process(session_id)
    assert owner == f"20:123:7:{lock.stat().st_dev}:{lock.stat().st_ino}"
    assert codex_session.owner_alive(owner)
    with pytest.raises(DomainError):
        codex_session.session_process(new_id())
    info.write_text(lock_info.replace(f":{inode} 0 EOF", f":{inode + 1} 0 EOF"))
    assert not codex_session.owner_alive(owner)
    info.write_text(lock_info.replace("WRITE 20", "WRITE 21"))
    assert not codex_session.owner_alive(owner)

    # A loaded sibling thread and a live server cannot keep this watcher alive.
    info.write_text("ino:\t123\n")
    assert not codex_session.owner_alive(owner)
    with pytest.raises(DomainError):
        codex_session.session_process(session_id)
    info.write_text(lock_info)
    (server / "fd/7").unlink()
    replacement = lock.parent / (new_id() + ".lock")
    replacement.touch()
    (server / "fd/7").symlink_to(replacement)
    assert not codex_session.owner_alive(owner)


def test_codex_server_without_thread_lock_fails_closed(codex_process_tree):
    _, _, process = codex_process_tree
    process(20, 1, ["codex", "app-server"])
    with pytest.raises(DomainError, match="running local Codex session"):
        codex_session.session_process(new_id())


def test_codex_embedded_process_still_owns_its_watcher(codex_process_tree):
    _, _, process = codex_process_tree
    cli = process(20, 1, ["codex"])
    owner = codex_session.session_process(new_id())
    assert owner == "20:123"
    assert codex_session.owner_alive(owner)
    (cli / "stat").write_text("20 (runtime) " + " ".join(["S", "1"] + ["0"] * 17 + ["124"]))
    assert not codex_session.owner_alive(owner)


@pytest.mark.parametrize("command", ["exec", "queue"])
def test_codex_noninteractive_process_is_not_a_session_owner(codex_process_tree, command):
    _, _, process = codex_process_tree
    process(20, 1, ["codex", command])
    with pytest.raises(DomainError):
        codex_session.session_process(new_id())


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_hook_has_separate_settings_environment_section(tmp_path, monkeypatch, runtime):
    path = registration(tmp_path, capture=False)
    monkeypatch.delenv('CLOUDFLARE_API_TOKEN', raising=False)
    class Client:
        def __init__(self, registration): pass
        def post(self, route, body):
            return {'ready': True, 'effective_config_id': 'config', 'mandatory_context': 'Rules',
                    'environment': {'status': 'ok', 'items': [
                        {'name': 'CLOUDFLARE_API_TOKEN', 'configured': True, 'origin': 'global'}]}}
    output, _, _ = claude_session.handle_hook(
        {'hook_event_name': 'SessionStart', 'source': 'startup', 'cwd': str(tmp_path), 'session_id': new_id()},
        path, runtime=runtime, client_factory=Client)
    text = output['hookSpecificOutput']['additionalContext']
    assert '## Environment and credentials (names only)' in text
    assert 'CLOUDFLARE_API_TOKEN (global, configured)' in text
    assert 'CLOUDFLARE_API_TOKEN' not in text.split('Startup-process variables: ')[1]
