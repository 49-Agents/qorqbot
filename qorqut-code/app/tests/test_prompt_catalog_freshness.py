"""Component checks for shared Claude/Codex handoff with a synthetic revision source."""
import io
import json
import threading
import time

import pytest
from bazarlyk.bootstrap import claude_session, handoff, recovery
from qorqut_operations.contracts import DomainError
from test_bootstrap_handoff import setup, invoke


@pytest.mark.parametrize('consumed', [False, True])
def test_scheduling_hint_cannot_bypass_final_verification(setup, monkeypatch, consumed):
    from contextlib import contextmanager
    from types import SimpleNamespace

    path, reg, root, native, worker = setup
    monkeypatch.setattr(handoff, 'HOOK_WAIT', .05)
    invoke(path, native)
    item = handoff.read(handoff.pending_path(root, native))
    handoff.publish(root, reg, item, response={'ready': True, 'effective_config_id': 'current',
        'agentic_catalog_revision': 'fixture-revision', 'mandatory_context': 'Verified instruction body'})
    if consumed:
        assert 'Verified instruction body' in json.dumps(invoke(path, native))
    saved = handoff.read(handoff.result_path(root, native))
    verified = []

    def reject():
        verified.append(True)
        raise DomainError('bootstrap_pending', 'Installed proof is unavailable', retryable=True)

    @contextmanager
    def observer(root, deadline):
        yield SimpleNamespace(hint=lambda: 'fixture-revision', observe=reject)

    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.open_observer', observer)
    response = invoke(path, native)
    assert verified == [True]
    assert 'Verified instruction body' not in json.dumps(response)
    assert 'bootstrap_pending' in json.dumps(response)
    assert handoff.read(handoff.result_path(root, native)) == saved


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_next_prompt_checks_revision_and_reuses_only_current_body(setup, monkeypatch, runtime):
    path, reg, root, native, worker = setup
    native['runtime'] = runtime
    revision = ['a']
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.observe', lambda root, timeout: revision[0])
    monkeypatch.setattr(handoff, 'HOOK_WAIT', .08)
    # A slow check times out; the same pending check can satisfy a later prompt.
    first = invoke(path, native)
    assert 'bootstrap_pending' in first['hookSpecificOutput']['additionalContext']
    item = handoff.read(handoff.pending_path(root, native))
    handoff.publish(root, reg, item, response={'ready': True, 'effective_config_id': 'a',
        'agentic_catalog_revision': 'a', 'mandatory_context': 'Full preference A and plugin A'})
    result = invoke(path, native)
    assert 'Full preference A and plugin A' in result['hookSpecificOutput']['additionalContext']
    handoff.discard_pending(handoff.pending_path(root, native), item)
    # Same prompt text has no role in freshness; unchanged current response emits nothing.
    assert invoke(path, native) == {}
    assert not handoff.pending_path(root, native).exists()
    revision[0] = 'b'
    before = time.monotonic()
    result = invoke(path, native)
    assert time.monotonic() - before < .5
    assert 'bootstrap_pending' in result['hookSpecificOutput']['additionalContext']
    assert recovery.read(reg, native['session_id']) is None
    pending = handoff.read(handoff.pending_path(root, native))
    assert pending['catalog_revision'] == 'b'
    assert not handoff.publish(root, reg, item, response={'ready': True, 'effective_config_id': 'a',
        'agentic_catalog_revision': 'a', 'mandatory_context': 'Stale'})
    handoff.publish(root, reg, pending, response={'ready': True, 'effective_config_id': 'b',
        'agentic_catalog_revision': 'b', 'mandatory_context': 'Full preference B and plugin B'})
    assert 'Full preference B and plugin B' in invoke(path, native)['hookSpecificOutput']['additionalContext']


def test_unknown_revision_never_satisfies_check_and_retry_keeps_request(setup, monkeypatch):
    path, reg, root, native, worker = setup
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.observe', lambda root, timeout: 'current')
    monkeypatch.setattr(handoff, 'HOOK_WAIT', .05)
    invoke(path, native)
    item = handoff.read(handoff.pending_path(root, native))
    handoff.publish(root, reg, item, response={'ready': True, 'effective_config_id': 'old', 'mandatory_context': 'old'})
    invoke(path, native)
    newer = handoff.read(handoff.pending_path(root, native))
    assert newer['request_id'] != item['request_id']
    invoke(path, native)
    assert handoff.read(handoff.pending_path(root, native))['request_id'] == newer['request_id']
    handoff.publish(root, reg, newer, response={'ready': True, 'effective_config_id': 'new',
        'agentic_catalog_revision': 'current', 'mandatory_context': 'new'})
    assert 'new' in invoke(path, native)['hookSpecificOutput']['additionalContext']


def test_revision_changed_during_candidate_read_cannot_be_delivered(setup, monkeypatch):
    path, reg, root, native, worker = setup
    monkeypatch.setattr(handoff, 'HOOK_WAIT', .05)
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.observe', lambda root, timeout: 'a')
    invoke(path, native)
    old = handoff.read(handoff.pending_path(root, native))
    handoff.publish(root, reg, old, response={'ready': True, 'effective_config_id': 'a',
        'agentic_catalog_revision': 'a', 'mandatory_context': 'stale body'})
    revisions = iter(['a', 'b'])
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.observe', lambda root, timeout: next(revisions))
    result = invoke(path, native)
    assert 'stale body' not in json.dumps(result)
    assert handoff.read(handoff.pending_path(root, native))['catalog_revision'] == 'b'
    assert not handoff.read(handoff.result_path(root, native))['consumed']


def test_no_local_config_uses_direct_authenticated_request(setup, monkeypatch):
    path, reg, root, native, worker = setup
    monkeypatch.delenv('QORQUT_INSTALLATION_CONFIG')
    seen = []
    class Client:
        def __init__(self, registration): self.timeout = 30
        def post(self, route, body):
            seen.append((route, self.timeout))
            return {'ready': True, 'effective_config_id': 'remote', 'agentic_catalog_revision': 'remote-rev',
                    'mandatory_context': 'Remote current instructions'}
    output, receipt, _ = claude_session.handle_hook({'hook_event_name': 'UserPromptSubmit',
        'session_id': native['session_id']}, path, runtime='codex', client_factory=Client)
    assert 'Remote current instructions' in output['hookSpecificOutput']['additionalContext']
    assert receipt['effective_config_id'] == 'remote' and seen == [('/api/agent/bootstrap', 15.0)]


def test_other_native_owner_cannot_consume_matching_revision(setup, monkeypatch):
    path, reg, root, native, worker = setup
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.observe', lambda root, timeout: 'same')
    monkeypatch.setattr(handoff, 'HOOK_WAIT', .05)
    invoke(path, native)
    old = handoff.read(handoff.pending_path(root, native))
    handoff.publish(root, reg, old, response={'ready': True, 'effective_config_id': 'old',
        'agentic_catalog_revision': 'same', 'mandatory_context': 'old owner rules'})
    new_native = {**native, 'owner': 'different-owner'}
    from bazarlyk.bootstrap.client import write_json_private
    def attest_new(*args):
        write_json_private(path.parent / 'native-binding.json', new_native)
        return new_native
    monkeypatch.setattr('bazarlyk.bootstrap.native_binding.attest', attest_new)
    result = invoke(path, new_native)
    assert 'old owner rules' not in json.dumps(result)
    assert handoff.read(handoff.pending_path(root, new_native))['owner'] == 'different-owner'
