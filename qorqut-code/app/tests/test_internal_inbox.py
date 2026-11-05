from pathlib import Path

import pytest
import yaml

from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.inbox.policy import INBOX_OPERATIONS
from bazarlyk.inbox.defaults import INBOX_INSTRUCTIONS
from bazarlyk.policy import resolve_operation_policy
from bazarlyk.application.catalog import builtin_registry
from inbox_support import pending_count
from test_service_transports import ServiceFixture


def custom_role(f, tmp_path, *, legacy=False):
    source = tmp_path / 'custom-role'
    original = f.catalog.get('role', f.refs['learning-applier'])
    for relative, body in original.files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    manifest = source / 'roles/learning-applier/role.yaml'
    data = yaml.safe_load(manifest.read_text())
    data.update(id='custom-role', skills=[], default_tool_policy={})
    if legacy:
        skill = source / 'skills/agent-inbox/SKILL.md'
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text('---\nname: bazarlyk-role-agent-inbox\ndescription: Legacy inbox instructions.\n---\nLegacy role instruction.\n')
        data['skills'] = [{'id':'agent-inbox','path':'skills/agent-inbox/SKILL.md'}]
    manifest.write_text(yaml.safe_dump(data, sort_keys=False))
    f.sources['custom-role'] = ('role', source, 'roles/learning-applier/role.yaml')
    f.publisher.sources['custom-role'] = f.sources['custom-role']
    f.refs['custom-role'] = f.publish('custom-role')
    # A custom role runs as a Global agent; no skills and no plugin grants.
    scope = {'kind': 'ecosystem', 'allowed_business_ids': [f.business_id], 'global_capabilities': ['read_global_context']}
    return f.create('custom', role='custom-role', plugins=[], scope=scope)


def test_core_defaults_do_not_depend_on_role_and_preserve_owner_overrides():
    registry = builtin_registry()
    for name in INBOX_OPERATIONS:
        for defaults in ({}, {name:'deny'}):
            decision = resolve_operation_policy(name, defaults, {}, registry, role_id='anything')
            assert (decision.mode, decision.provenance) == ('allow','core_default')
        assert resolve_operation_policy(name, {}, {name:'deny'}, registry).mode == 'deny'
    assert resolve_operation_policy('tasks_create', {}, {}, registry).mode == 'deny'


def test_custom_role_without_skills_or_grants_can_use_own_inbox(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    agent = custom_role(s.fixture, tmp_path)
    ctx = s.runtime.resolver.invocation_context(agent)
    consumer = new_id()
    assert pending_count(s.runtime.inbox, ctx, consumer) == 0
    result = s.runtime.application.invoke(ctx,'inbox_list',{'consumer_id':consumer})
    assert result.status == 'ok', result.to_dict()
    config = s.runtime.resolver.resolve_agent_config(agent).to_dict()
    # Core supplies the generated role entry point even to an empty custom role.
    assert [skill['name'] for skill in config['skills']] == ['role']
    assert INBOX_INSTRUCTIONS in config['mandatory_context']['rendered']
    tools = {row['canonical_operation']:row for row in config['tools']}
    for name in INBOX_OPERATIONS:
        assert tools[name]['available'] and tools[name]['source']['kind'] == 'core_default'
    assert not tools['stories_get']['available']
    s.fixture.prepare(agent)
    workspace = Path(config['workspace'])
    guidance = (workspace/'CLAUDE.md').read_text()
    assert '## Inbox' in guidance and 'inbox.watcher' not in guidance
    assert not (workspace/'.claude/skills/bazarlyk-role-agent-inbox').exists()
    s.fixture.update(agent, {'tool_policy':{'overrides':{'inbox_list':'deny'}}})
    denied = s.runtime.application.invoke(s.runtime.resolver.invocation_context(agent), 'inbox_list', {'consumer_id': consumer})
    assert denied.status == 'error'


def test_repreparation_removes_legacy_skill_without_changing_role_pin(tmp_path, monkeypatch):
    f = ServiceFixture(tmp_path, full=True).fixture
    agent = custom_role(f, tmp_path, legacy=True)
    with monkeypatch.context() as patch:
        patch.setattr('bazarlyk.agents.resolver.legacy_inbox_skill', lambda *args:False)
        patch.setattr('bazarlyk.bootstrap.preparation.legacy_inbox_skill', lambda *args:False)
        f.prepare(agent)
    f.resolver._cache.clear()
    config = f.resolver.resolve_agent_config(agent).to_dict()
    workspace = Path(config['workspace'])
    legacy = workspace/'.claude/skills/bazarlyk-role-agent-inbox'
    assert legacy.exists()
    role = config['role']
    f.prepare(agent)
    assert not legacy.exists()
    after = f.resolver.resolve_agent_config(agent).to_dict()
    assert after['role'] == role
    assert [skill['name'] for skill in after['skills']] == ['role']
    assert INBOX_INSTRUCTIONS in after['mandatory_context']['rendered']
