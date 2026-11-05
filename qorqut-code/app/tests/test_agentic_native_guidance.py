"""Persistent native prose has its own explicitly prepared materialization."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from bazarlyk.bootstrap import preparation, skill_refresh
from qorqut_operations.contracts import DomainError
from test_agentic_projection import host, ready
from test_agentic_view import edit


PREFIX = 'packages/imported-host-builtins/native/app/bazarlyk/agentic/assets/'


def connection(host, *, selected=True):
    def orientation(prepared, ordubashy, *, agentic_view):
        if prepared:
            return 'Synthetic prepared connection.'
        return agentic_view.resource('host', 'app/bazarlyk/agentic/assets/connection-orientation.md')['content']
    def guidance(*, agentic_view):
        return agentic_view.resource('host', 'app/bazarlyk/agentic/assets/connection-guidance.md')['content']
    return SimpleNamespace(server='synthetic-native', roles=('global-worker',) if selected else (), retires=(),
        status=lambda *args: {'prepared': True}, entry=lambda *args: {'command': '/synthetic/not-executed'},
        files=lambda *args: {}, orientation=orientation, guidance=guidance)


def registration(host, registration_id):
    with host.instance.database.read() as uow:
        return uow.repository('machine_registrations').get(registration_id)


def test_software_drift_requires_explicit_preparation_and_keeps_other_checks(host):
    first = ready(host)
    before = host.resolver.resolve_agent_config(host.principal).to_dict()
    workspace = Path(first['workspace'])
    original = (workspace / 'AGENTS.md').read_bytes()
    edit(host.view.layer, PREFIX + 'software-ownership.md', 'New managed software boundary.')
    after = host.resolver.resolve_agent_config(host.principal)
    assert after.data['native_guidance_digest'] != before['native_guidance_digest']
    assert after.data['preparation']['projection_drift'] == ('native_guidance_changed',)
    assert after.data['preparation']['status'] == 'reprepare_required'
    assert not skill_refresh.refresh(host.resolver, host.principal, after, first['registration_id'], 'codex')
    assert (workspace / 'AGENTS.md').read_bytes() == original
    blocked = host.resolver.bootstrap(host.principal, registration_id=first['registration_id'],
                                     session_id='synthetic-session', event='prompt', runtime='codex')
    assert not blocked['ready'] and 'explicit agent preparation is required' in blocked['error']['message']
    current = ready(host)
    assert current['ready']
    assert b'New managed software boundary.' in (workspace / 'AGENTS.md').read_bytes()
    assert registration(host, first['registration_id'])['projection_json']['native_guidance_digest'] == after.data['native_guidance_digest']
    (workspace / '.bazarlyk/agentic').chmod(0o600)
    remaining = host.resolver.resolve_agent_config(host.principal).data['preparation']
    assert not remaining['ready'] and '.bazarlyk/agentic' in remaining['projection_drift']
    assert 'native_guidance_changed' not in remaining['projection_drift']


@pytest.mark.parametrize('asset', ['connection-guidance.md', 'connection-orientation.md'])
def test_selected_connection_guidance_and_both_orientations_enter_digest(host, asset):
    selected = connection(host)
    host.resolver.native_connections = {'synthetic': selected}
    first = ready(host)
    before = host.resolver.resolve_agent_config(host.principal).to_dict()
    edit(host.view.layer, PREFIX + asset, 'Changed selected native text.')
    after = host.resolver.resolve_agent_config(host.principal)
    assert after.data['native_guidance_digest'] != before['native_guidance_digest']
    assert 'native_guidance_changed' in after.data['preparation']['projection_drift']
    assert not skill_refresh.refresh(host.resolver, host.principal, after, first['registration_id'], 'codex')


def test_unselected_native_connection_is_excluded_without_status_probes(host):
    disabled = connection(host, selected=False)
    host.resolver.native_connections = {'unselected': disabled}
    before = host.resolver.resolve_agent_config(host.principal).to_dict()
    disabled.orientation = lambda *args, **kwargs: pytest.fail('Unselected orientation must not be read')
    disabled.guidance = lambda **kwargs: pytest.fail('Unselected guidance must not be read')
    disabled.status = lambda *args: pytest.fail('Digest must not probe native readiness')
    edit(host.view.layer, PREFIX + 'connection-guidance.md', 'Changed unselected native text.')
    after = host.resolver.resolve_agent_config(host.principal).to_dict()
    assert after['native_guidance_digest'] == before['native_guidance_digest']
    selected = connection(host)
    selected.status = lambda *args: pytest.fail('Digest must not probe readiness even for selected connections')
    host.resolver.native_connections = {'synthetic': selected}
    assert host.resolver.native_guidance_digest(after) != after['native_guidance_digest']


@pytest.mark.parametrize('value', [None, 123, 'malformed', 'sha256:' + '0' * 64])
def test_missing_or_malformed_legacy_digest_requires_preparation(host, value):
    first = ready(host)
    with host.instance.database.transaction() as uow:
        repo = uow.repository('machine_registrations')
        row = repo.get(first['registration_id'])
        projection = row['projection_json']
        if value is None:
            projection.pop('native_guidance_digest')
        else:
            projection['native_guidance_digest'] = value
        repo.update(row['id'], {'projection_json': projection}, expected_revision=row['revision'])
    status = host.resolver.resolve_agent_config(host.principal).data['preparation']
    assert not status['ready'] and 'native_guidance_changed' in status['projection_drift']
    assert ready(host)['ready']


def test_native_projection_rejects_concurrent_guidance_revision(host):
    config = host.resolver.resolve_agent_config(host.principal)
    text = sys.modules['bazarlyk.agentic.text']
    render = text.render
    changed = False
    def racing(view, source_id, path, **values):
        nonlocal changed
        result = render(view, source_id, path, **values)
        if path.endswith('software-ownership.md') and not changed:
            changed = True
            edit(view.layer, PREFIX + 'software-ownership.md', 'Changed during materialization.')
        return result
    host.monkeypatch.setattr(text, 'render', racing)
    with pytest.raises(DomainError) as error:
        preparation.build_projection(host.resolver, config, registration_id='fixture',
            endpoint='http://127.0.0.1:9988', interpreter=sys.executable, interpreter_validator=lambda _: True)
    assert error.value.code == 'agentic_revision_conflict'
