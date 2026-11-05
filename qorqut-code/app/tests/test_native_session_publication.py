"""Restart publication under delayed bootstrap; synthetic sessions and files only."""
import json
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.bootstrap import claude_session, handoff, native_binding, terminal_chat
from native_chat_support import Native


@pytest.fixture
def publication(tmp_path, monkeypatch):
    workspace = tmp_path / 'agent'
    root = workspace / '.bazarlyk'
    root.mkdir(parents=True)
    registration = {'schema_version': 1, 'registration_id': new_id(), 'agent_id': new_id(),
                    'workspace': str(workspace), 'service_url': 'http://127.0.0.1:9988',
                    'credential_file': '.bazarlyk/credential', 'capture_transcripts': True}
    path = root / 'registration.json'
    path.write_text(json.dumps(registration))
    session = new_id()
    hook = {'hook_event_name': 'UserPromptSubmit', 'session_id': session, 'cwd': str(workspace),
            'transcript_path': str(tmp_path / f'rollout-2026-10-14T13-36-39-{session}.jsonl')}
    process = {'pid': 123, 'started': '456'}
    binding = {**registration, **process, 'session_id': session, 'runtime': 'codex', 'event': 'prompt'}
    marker = {key: binding[key] for key in terminal_chat.NATIVE_IDENTITY}
    marker.update(transcript_path=hook['transcript_path'], event='SessionEnd', session_id=new_id())
    (root / 'terminal-chat.json').write_text(json.dumps(marker))
    monkeypatch.setattr(terminal_chat, 'native_process', lambda runtime: process)
    return SimpleNamespace(root=root, path=path, hook=hook, binding=binding, marker=marker, process=process)


def test_pending_bootstrap_still_publishes_the_attested_conversation(publication, monkeypatch):
    fixture = publication

    def attest(*args):
        (fixture.root / 'native-binding.json').write_text(json.dumps(fixture.binding))
        return fixture.binding

    def pending(*args):
        # Publication must precede the background request, which can time out.
        marker = json.loads((fixture.root / 'terminal-chat.json').read_text())
        assert marker['session_id'] == fixture.hook['session_id']
        assert marker['event'] == 'UserPromptSubmit'
        raise DomainError('bootstrap_pending', 'Context is still being prepared')

    monkeypatch.setattr(native_binding, 'attest', attest)
    monkeypatch.setattr(handoff, 'request', pending)
    with pytest.raises(DomainError, match='Context is still being prepared'):
        claude_session.handle_hook(fixture.hook, fixture.path, runtime='codex',
                                   client_factory=lambda registration: SimpleNamespace())


@pytest.mark.parametrize('event', ['Stop', 'SessionEnd'])
def test_first_completion_can_replace_old_marker_only_for_current_binding(publication, event):
    fixture = publication
    hook = {**fixture.hook, 'hook_event_name': event}
    assert terminal_chat.publish(hook, fixture.path, runtime='codex') is False
    (fixture.root / 'native-binding.json').write_text(json.dumps(fixture.binding))
    assert terminal_chat.publish(hook, fixture.path, runtime='codex') is True
    assert json.loads((fixture.root / 'terminal-chat.json').read_text())['session_id'] == hook['session_id']


@pytest.mark.parametrize('same_conversation', [False, True])
def test_old_completion_cannot_replace_new_conversation_or_process(publication, same_conversation):
    fixture = publication
    (fixture.root / 'native-binding.json').write_text(json.dumps(fixture.binding))
    assert terminal_chat.publish(fixture.hook, fixture.path, runtime='codex') is True
    before = (fixture.root / 'terminal-chat.json').read_bytes()
    old_session = fixture.hook['session_id'] if same_conversation else new_id()
    fixture.process.update(pid=124, started='old-start')
    old = {**fixture.hook, 'session_id': old_session, 'hook_event_name': 'SessionEnd',
           'transcript_path': str(fixture.root.parent / f'rollout-2026-10-14T12-00-00-{old_session}.jsonl')}
    assert terminal_chat.publish(old, fixture.path, runtime='codex') is False
    assert (fixture.root / 'terminal-chat.json').read_bytes() == before


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_chat_uses_new_binding_while_old_transcript_marker_still_exists(tmp_path, provider):
    native = Native(tmp_path, provider)
    native.publish()
    native.pass_()
    path = native.workspace / '.bazarlyk/terminal-chat.json'
    old = json.loads(path.read_text())
    native.new_session()
    fresh = json.loads(path.read_text())
    registration_path = native.workspace / '.bazarlyk/registration.json'
    registration = json.loads(registration_path.read_text())
    registration['registration_id'] = new_id()
    registration_path.write_text(json.dumps(registration))
    binding = {**fresh, 'registration_id': registration['registration_id'], 'event': 'prompt'}
    binding.pop('transcript_path')
    (native.workspace / '.bazarlyk/native-binding.json').write_text(json.dumps(binding))
    path.write_text(json.dumps({**old, 'event': 'SessionEnd'}))
    native.pass_()
    state = native.chat.state(native.owner, native.agent)['session']
    assert state['state'] == 'live' and state['reason'] == 'capture_not_ready'
    assert state['can_send'] is False
    native.publish()
    native.pass_(2)
    state = native.chat.state(native.owner, native.agent)['session']
    assert state['can_send'] is True and state['conversation_id'] == native.session_id
    native.chat.send(native.owner, native.agent, 'New session only', 'restart-chat-check', native.pin())
    native.pass_(2)
    assert native.pane.typed() == ['New session only']
