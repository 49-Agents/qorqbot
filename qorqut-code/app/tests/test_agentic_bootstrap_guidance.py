"""Managed bootstrap fragments over the existing HTTP/handoff response contract."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
from bazarlyk.bootstrap import claude_session, handoff, preparation
from bazarlyk.bootstrap.client import load_registration, write_json_private
from qorqut_operations.contracts import DomainError, new_id, utc_now
from test_agentic_projection import host, ready
from test_agentic_view import edit


@pytest.fixture
def hook_fixture(tmp_path, monkeypatch):
    path = tmp_path / '.bazarlyk/registration.json'
    write_json_private(path, {'schema_version': 1, 'registration_id': new_id(), 'agent_id': new_id(),
        'workspace': str(tmp_path), 'service_url': 'http://127.0.0.1:9988',
        'credential_file': '.bazarlyk/credential', 'capture_transcripts': False})
    session_id = new_id()
    hook = {'hook_event_name': 'SessionStart', 'source': 'startup', 'session_id': session_id}
    response = {'ready': True, 'effective_config_id': 'synthetic', 'mandatory_context': 'Managed rules',
                'inbox_available': True, 'inbox_consumer_id': session_id,
                'inbox_session_note': 'Current managed session note.'}
    monkeypatch.setattr('bazarlyk.bootstrap.native_binding.attest', lambda *args: None)
    monkeypatch.setattr(claude_session, 'snapshot_transcript', lambda *args, **kwargs: None)
    monkeypatch.setattr(handoff, 'request', lambda *args: None)
    calls = []
    class Client:
        def __init__(self, registration):
            pass
        def post(self, route, body):
            calls.append(route)
            return response
    return path, hook, response, Client, calls


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_both_hooks_emit_managed_note_through_existing_response(hook_fixture, runtime):
    path, hook, response, client, calls = hook_fixture
    output, receipt, _ = claude_session.handle_hook(hook, path, runtime=runtime, client_factory=client)
    assert response['inbox_session_note'] in output['hookSpecificOutput']['additionalContext']
    assert calls == ['/api/agent/bootstrap']
    assert receipt['bundle_hash'] != hashlib.sha256(response['inbox_session_note'].encode()).hexdigest()


@pytest.mark.parametrize('note', [None, '', ' ', 13, 'x' * (MAX_CONTEXT_CHARACTERS + 1)])
def test_missing_or_invalid_note_fails_even_on_unchanged_prompt(hook_fixture, note):
    path, hook, response, client, _ = hook_fixture
    response['inbox_session_note'] = note
    response['refresh_required'] = False
    hook['hook_event_name'] = 'UserPromptSubmit'
    receipt = path.parent / 'delivery' / (hashlib.sha256(hook['session_id'].encode()).hexdigest() + '.json')
    write_json_private(receipt, {'registration_id': load_registration(path)['registration_id'],
                                'effective_config_id': response['effective_config_id']})
    with pytest.raises(DomainError) as error:
        claude_session.handle_hook(hook, path, client_factory=client)
    assert error.value.code == 'invalid_bootstrap'


def test_binding_error_keeps_diagnostic_without_requiring_available_note(hook_fixture):
    path, hook, response, client, _ = hook_fixture
    response.pop('inbox_session_note')
    response['inbox_binding_error'] = 'inbox_delivery_unavailable'
    output, _, _ = claude_session.handle_hook(hook, path, client_factory=client)
    assert 'Inbox notices are paused: inbox_delivery_unavailable' in output['hookSpecificOutput']['additionalContext']


def test_note_stays_inside_combined_context_budget(hook_fixture):
    path, hook, response, client, _ = hook_fixture
    response['mandatory_context'] = 'x' * MAX_CONTEXT_CHARACTERS
    with pytest.raises(DomainError, match='Combined bootstrap'):
        claude_session.handle_hook(hook, path, client_factory=client)


def test_prepared_handoff_preserves_managed_note(tmp_path, monkeypatch):
    path = tmp_path / '.bazarlyk/registration.json'
    write_json_private(path, {'schema_version': 1, 'registration_id': new_id(), 'agent_id': new_id(),
        'workspace': str(tmp_path), 'service_url': 'http://127.0.0.1:9988',
        'credential_file': '.bazarlyk/credential', 'capture_transcripts': False})
    registration = load_registration(path)
    root = tmp_path / 'synthetic-install'
    config = root / 'config/installation.json'
    write_json_private(config, {'format': 1, 'root': str(root)})
    monkeypatch.setenv('QORQUT_INSTALLATION_CONFIG', str(config))
    native = {'registration_id': registration['registration_id'], 'session_id': new_id(),
              'owner': '123:456', 'runtime': 'codex', 'registration_path': str(path)}
    monkeypatch.setattr(handoff, 'running', lambda _: True)
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.observe', lambda root, timeout: 'fixture-revision')
    from contextlib import contextmanager
    from types import SimpleNamespace
    @contextmanager
    def fake_observer(root, deadline):
        yield SimpleNamespace(observe=lambda: 'fixture-revision', hint=lambda: 'fixture-revision')
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.open_observer', fake_observer)
    monkeypatch.setattr('bazarlyk.bootstrap.native_binding.attest', lambda *args: dict(native))
    monkeypatch.setattr(claude_session, 'snapshot_transcript', lambda *args, **kwargs: None)
    handoff.offer(root, path, registration, native, {'event': 'startup'})
    item = handoff.read(handoff.pending_path(root, native))
    response = {'ready': True, 'effective_config_id': 'current', 'agentic_catalog_revision': 'fixture-revision', 'mandatory_context': 'Rules',
                'inbox_available': True, 'inbox_consumer_id': native['session_id'],
                'inbox_session_note': 'Managed handoff session note.'}
    assert handoff.publish(root, registration, item, response=response)
    class Client:
        def __init__(self, registration):
            pass
        def post(self, *args):
            pytest.fail('Prepared handoff must not make another bootstrap request')
    output, _, client = claude_session.handle_hook({'hook_event_name': 'SessionStart', 'source': 'startup',
        'session_id': native['session_id']}, path, runtime='codex', client_factory=Client)
    assert response['inbox_session_note'] in output['hookSpecificOutput']['additionalContext']
    assert client.handoff['response']['inbox_session_note'] == response['inbox_session_note']


def test_host_sends_current_note_on_unchanged_prompt_and_refreshes_edited_note(host):
    registration = ready(host)
    resolver = host.resolver
    session_id = new_id()
    response = resolver.bootstrap(host.principal, registration_id=registration['registration_id'],
                                  session_id=session_id, event='startup', runtime='codex')
    assert response['inbox_available'] and response['inbox_session_note'] == 'Managed inbox session note.\n'
    resolver.record_delivery(host.principal, registration_id=registration['registration_id'], session_id=session_id,
        effective_config_id=response['effective_config_id'], bundle_hash=response['mandatory_context']['bundle_hash'],
        event='startup', emitted_at=utc_now())
    unchanged = resolver.bootstrap(host.principal, registration_id=registration['registration_id'],
        session_id=session_id, event='prompt', last_effective_config_id=response['effective_config_id'], runtime='codex')
    assert not unchanged['refresh_required'] and 'mandatory_context' not in unchanged
    assert unchanged['agentic_catalog_revision'] == response['agentic_catalog_revision'] == host.view.catalog_revision
    assert unchanged['inbox_session_note'] == response['inbox_session_note']
    edit(host.view.layer, 'operations/imported-host-builtins/native/app/bazarlyk/agentic/assets/inbox-session-note.md',
         'Edited managed inbox session note.')
    changed = resolver.bootstrap(host.principal, registration_id=registration['registration_id'],
        session_id=session_id, event='prompt', last_effective_config_id=response['effective_config_id'], runtime='codex')
    assert changed['refresh_required'] and changed['effective_config_id'] != response['effective_config_id']
    assert changed['agentic_catalog_revision'] == host.view.catalog_revision != response['agentic_catalog_revision']
    assert changed['inbox_session_note'] == 'Edited managed inbox session note.'
    assert changed['preparation']['ready']
    assert 'native_guidance_changed' not in changed['preparation']['projection_drift']


def test_software_boundary_uses_current_asset_and_has_no_missing_body_fallback(host):
    path = 'operations/imported-host-builtins/native/app/bazarlyk/agentic/assets/software-ownership.md'
    edit(host.view.layer, path, 'Edited managed software ownership boundary.')
    config = host.resolver.resolve_agent_config(host.principal)
    projection = preparation.build_projection(host.resolver, config, registration_id='fixture',
        endpoint='http://127.0.0.1:9988', interpreter=sys.executable, interpreter_validator=lambda _: True)
    assert b'Edited managed software ownership boundary.' in projection.files['AGENTS.md']
    assert b'Edited managed software ownership boundary.' in projection.files['CLAUDE.md']
    (host.view.layer.root / path).unlink()  # Synthetic missing managed file, no source/native session touched.
    with pytest.raises(DomainError, match='no such resource'):
        preparation.build_projection(host.resolver, config, registration_id='fixture',
            endpoint='http://127.0.0.1:9988', interpreter=sys.executable, interpreter_validator=lambda _: True)
