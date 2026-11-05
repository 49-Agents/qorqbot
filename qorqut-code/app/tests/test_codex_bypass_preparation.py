"""Operator-selected Codex bypass survives preparation without weakening defaults."""
import tomllib
from pathlib import Path

import pytest

from bazarlyk.bootstrap.preparation import prepare_agent
from qorqut_operations.contracts import DomainError
from test_preparation_recovery import VERSIONS, bootstrap, registration
from test_service_transports import ServiceFixture


def test_operator_bypass_is_persisted_and_can_be_revoked(tmp_path):
    service = ServiceFixture(tmp_path)
    def prepare(**kw):
        return prepare_agent(service.fixture.resolver, service.agent,
            endpoint='http://127.0.0.1:9988', versions=VERSIONS, **kw)
    result = prepare()
    path = Path(result['workspace']) / '.codex/config.toml'
    assert tomllib.loads(path.read_text())['approval_policy'] == 'never'
    assert tomllib.loads(path.read_text())['model'] == 'gpt-6-luna'
    assert tomllib.loads(path.read_text())['model_reasoning_effort'] == 'high'
    claude = (path.parent.parent / '.claude/settings.local.json').read_bytes()
    local = __import__('json').loads(claude)
    assert local['permissions']['defaultMode'] == 'dontAsk'
    assert local['permissions']['deny']
    assert 'Read' in local['permissions']['allow']
    assert any(r.startswith('Edit(') for r in local['permissions']['allow'])
    credential_id = registration(service)['credential_id']
    for selection in [{'codex_permission_mode': 'bypass'}, {}]:
        prepare(**selection)
        config = tomllib.loads(path.read_text())
        assert config['model'] == 'gpt-6-luna'
        assert config['model_reasoning_effort'] == 'high'
        assert config['default_permissions'] == ':danger-full-access'
        assert config['approval_policy'] == 'never'
        assert 'permissions' not in config  # no sticky deny overlay on Full Access
        assert registration(service)['projection_json']['codex_permission_mode'] == 'bypass'
        assert registration(service)['credential_id'] == credential_id
        assert (path.parent.parent / '.claude/settings.local.json').read_bytes() == claude
        assert bootstrap(service, 'codex')['ready']
    # Bypass does not turn off generated-file integrity checking.
    original = path.read_bytes()
    path.write_bytes(original + b'\nmodel = "tampered"\n')
    assert not bootstrap(service, 'codex')['ready']
    prepare(codex_permission_mode='scoped')
    config = tomllib.loads(path.read_text())
    assert config['approval_policy'] == 'never'
    assert 'deny' in config['permissions']['qorqut-agent']['filesystem'].values()
    assert bootstrap(service, 'codex')['ready']
    before = path.read_bytes()
    with pytest.raises(DomainError, match='Codex permission mode'):
        prepare(codex_permission_mode='invalid')
    assert path.read_bytes() == before


def test_new_global_defaults_to_bypass_business_stays_scoped(tmp_path):
    from test_agent_configuration import Fixture
    fixture = Fixture(tmp_path)
    founder = fixture.create('founder', role='founder')
    result = prepare_agent(fixture.resolver, founder,
        endpoint='http://127.0.0.1:9988', versions=VERSIONS)
    config = tomllib.loads((Path(result['workspace']) / '.codex/config.toml').read_text())
    assert (config['model'], config['model_reasoning_effort']) == ('gpt-6-astra', 'low')
    assert config['default_permissions'] == ':danger-full-access'
    assert 'permissions' not in config
    local = __import__('json').loads((Path(result['workspace']) / '.claude/settings.local.json').read_text())
    assert local['permissions']['defaultMode'] == 'dontAsk'
    assert local['permissions']['deny'] == []
    launch = __import__('json').loads((Path(result['workspace']) / '.bazarlyk/claude-launch-settings.json').read_text())
    assert launch['permissions']['defaultMode'] == 'bypassPermissions'
    assert config['approval_policy'] == 'never'
    assert config['mcp_servers']['bazarlyk']['default_tools_approval_mode'] == 'approve'
