import io
import json

import pytest

from bazarlyk.bootstrap import claude_session, recovery
from qorqut_operations.contracts import DomainError, new_id


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
@pytest.mark.parametrize('error_code', ['bootstrap_pending', 'busy', 'bootstrap_unavailable', 'forbidden'])
def test_hook_leaves_transient_recovery_to_helper_but_preserves_identity_denial(tmp_path, monkeypatch, runtime, error_code):
    registration = {'workspace': str(tmp_path), 'registration_id': new_id()}
    monkeypatch.setattr(claude_session, 'load_registration', lambda _: registration)
    monkeypatch.setattr(claude_session, 'snapshot_transcript', lambda *a, **k: None)
    monkeypatch.setattr('bazarlyk.bootstrap.native_binding.attest', lambda *a: None)
    monkeypatch.setattr(claude_session.time, 'sleep', lambda _: pytest.fail('hook must not retry'))
    session = new_id()
    original = recovery.record(registration, session, runtime, 'busy')
    calls = []
    reports = []
    monkeypatch.setattr('bazarlyk.bootstrap.technical_reporting.record', lambda *a, **k: reports.append((a, k)))
    class Client:
        def __init__(self, registration): pass
        def post(self, route, body):
            calls.append(body)
            raise DomainError(error_code, 'test failure', retryable=error_code in {'bootstrap_pending', 'bootstrap_unavailable'})
    out = io.StringIO()
    claude_session.run({'hook_event_name': 'UserPromptSubmit', 'session_id': session, 'cwd': str(tmp_path)},
        tmp_path/'registration', runtime=runtime, client_factory=Client, stream=out, diagnostics=io.StringIO())
    result = json.loads(out.getvalue())
    assert len(calls) == 1
    if error_code == 'forbidden':
        assert result['decision'] == 'block'
    else:
        assert 'Continue the owner' in result['hookSpecificOutput']['additionalContext']
        assert 'Avoid business work' not in out.getvalue()
    assert recovery.read(registration, session)['failure_id'] == original['failure_id']
    if error_code == 'bootstrap_pending':
        assert 'temporarily unavailable' not in out.getvalue()
        assert 'You may use service tools within your existing authorization' in out.getvalue()
        assert 'instructions are missing' in out.getvalue()
        assert not reports  # A healthy asynchronous request must not create a fault ticket.
    else:
        assert reports


@pytest.mark.parametrize('code', ['forbidden', 'reprepare_required', 'invalid_bootstrap', 'bootstrap_unavailable'])
def test_permanent_failures_are_not_retried(monkeypatch, code):
    monkeypatch.setattr(claude_session.time, 'sleep', lambda _: pytest.fail('unexpected retry'))
    class Client:
        def post(self, route, body):
            raise DomainError(code, 'invalid configuration')
    with pytest.raises(DomainError, match='invalid configuration'):
        claude_session.prepare_with_retry(Client(), {})


@pytest.mark.parametrize('status,body,temporary', [
    (503, b'<html>restarting</html>', True),
    (502, b'bad gateway', True),
    (401, b'unauthorized', False),
    (200, b'broken JSON', False),
])
def test_client_classifies_gateway_outages_without_retrying_auth_or_invalid_data(tmp_path, monkeypatch, status, body, temporary):
    from bazarlyk.bootstrap import client as module
    class Response(io.BytesIO):
        pass
    response = Response(body)
    response.status = status
    class Opener:
        def open(self, *args, **kwargs): return response
    monkeypatch.setattr(module, 'auth_headers', lambda _: {})
    monkeypatch.setattr(module, 'build_opener', lambda *args: Opener())
    with pytest.raises(DomainError) as error:
        module.BootstrapClient({'service_url': 'http://localhost:1071','workspace':str(tmp_path),'registration_id':new_id()}).post('/api/agent/bootstrap', {})
    assert error.value.retryable is temporary


def test_connection_failure_is_retryable(tmp_path, monkeypatch):
    from urllib.error import URLError
    from bazarlyk.bootstrap import client as module
    class Opener:
        def open(self, *args, **kwargs): raise URLError('connection refused')
    monkeypatch.setattr(module, 'auth_headers', lambda _: {})
    monkeypatch.setattr(module, 'build_opener', lambda *args: Opener())
    with pytest.raises(DomainError) as error:
        module.BootstrapClient({'service_url': 'http://localhost:1071','workspace':str(tmp_path),'registration_id':new_id()}).post('/api/agent/bootstrap', {})
    assert error.value.code == 'bootstrap_unavailable' and error.value.retryable
