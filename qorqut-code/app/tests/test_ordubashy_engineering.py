"""Standing engineering authority is role-bound, scoped and revision-safe."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import tomllib

import pytest

from bazarlyk.agents.ordubashy import additions_for_business
from bazarlyk.bootstrap.preparation import build_projection
from qorqut_operations.contracts import DomainError, new_id, plain
from test_agent_configuration import Fixture
from test_owner_actions import actions, business, ENGINEERING


def update(s, config, *, ctx=None, revision=None, key=None):
    return s.app.invoke(ctx or s.ctx(), 'business_config_update', {'config': config},
        {'expected_revision': revision if revision is not None else business(s)['revision'],
         'idempotency_key': key or new_id()})


def test_cross_business_engineering_add_replace_remove_and_preservation(actions):
    s = actions
    for identity in (s.business, s.other_business):
        ctx = replace(s.ctx(), business_id=identity, review_business_id=identity)
        before = business(s, identity)
        config = {**before['config_json'], 'engineering': ENGINEERING,
                  'custom_settings': {'feature': True}, 'timezone': 'UTC', 'stage': 'validating'}
        key = new_id()
        result = update(s, config, ctx=ctx, revision=before['revision'], key=key)
        assert result.status == 'ok', result.to_dict()
        assert update(s, config, ctx=ctx, revision=before['revision'], key=key).status == 'ok'
        assert business(s, identity)['config_json'] == config
        assert update(s, config, ctx=ctx, revision=before['revision']).error['code'] == 'revision_conflict'
        # Omitting protected blocks preserves them, as for the owner.
        result = update(s, {'custom_settings': config['custom_settings']}, ctx=ctx,
                        revision=business(s, identity)['revision'])
        assert result.status == 'ok'
        assert business(s, identity)['config_json']['engineering'] == ENGINEERING
        config = plain(business(s, identity)['config_json'])
        config['engineering'] = {**ENGINEERING, 'deployment_targets': ['business-app production']}
        assert update(s, config, ctx=ctx, revision=business(s, identity)['revision']).status == 'ok'
        config['engineering'] = {'github_organizations': [], 'repositories': [], 'deployment_targets': []}
        assert update(s, config, ctx=ctx, revision=business(s, identity)['revision']).status == 'ok'
        assert business(s, identity)['config_json']['engineering'] == config['engineering']
    assert s.count('signoffs') == 0


@pytest.mark.parametrize('role', ['business-agent', 'global-marketing'])
def test_other_roles_cannot_manage_protected_config_even_with_allow_or_forged_role(actions, role):
    s = actions
    if role == 'business-agent':
        actor = s.agent
        s.change_override('business_config_update', 'allow', actor=actor)
    else:
        actor = s.add_agent(role, {'business_config_update': 'allow'})
    ctx = replace(s.agent_context(actor), role_artifact_ref=s.ctx().role_artifact_ref)
    before = business(s)
    for key, value in [('engineering', ENGINEERING), ('browser', {'enabled': True, 'proxy_server': 'http://proxy.example:8080'})]:
        result = update(s, {**before['config_json'], key: value}, ctx=ctx)
        assert result.status == 'error' and result.error['code'] == 'forbidden'
        assert business(s) == before
    assert update(s, {**before['config_json'], 'one_liner': 'Ordinary edit'}, ctx=ctx).status == 'ok'


@pytest.mark.parametrize('loss', ['deny', 'membership', 'inactive', 'outside', 'global'])
def test_current_scope_and_authority_cannot_be_bypassed(actions, loss):
    s = actions
    before = business(s)
    ctx = s.ctx()
    if loss == 'deny':
        s.change_override('business_config_update', 'deny', actor=s.actor)
    elif loss in {'membership', 'inactive'}:
        with s.db.transaction() as u:
            if loss == 'membership':
                u.connection.execute('UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?', (s.business, s.actor.id))
            else:
                u.connection.execute('UPDATE principals SET active=0 WHERE id=?', (s.actor.id,))
    else:
        ctx = replace(ctx, business_id=new_id() if loss == 'outside' else None)
    result = update(s, {**before['config_json'], 'engineering': ENGINEERING}, ctx=ctx)
    assert result.status == 'error'
    assert business(s) == before


def test_signoff_override_and_apply_time_membership_revocation(actions):
    s = actions
    s.change_override('business_config_update', 'signoff', actor=s.actor)
    before = business(s)
    result = update(s, {**before['config_json'], 'engineering': ENGINEERING})
    assert result.status == 'signoff_required'
    proposal = plain(result.signoff)
    assert s.decide(proposal, context=s.ctx()).status == 'error'  # still cannot approve
    assert s.decide(proposal).status == 'accepted'
    with s.db.transaction() as u:
        u.connection.execute('UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?', (s.business, s.actor.id))
    s.service.run_pending()
    assert s.detail(proposal)['state'] != 'applied'
    assert business(s) == before


@pytest.mark.parametrize('fields', [
    {'workspace': '/other'}, {'owner_principal_id': 'another-owner'},
    {'engineering': {'repositories': ['../escape']}},
    {'engineering': {'api_token': 'not-a-config-field'}},
    {'browser': {'enabled': True, 'proxy_server': 'http://user:secret@proxy.example:8080'}},
    {'browser': {'enabled': True, 'proxy_server': 'http://proxy.example:8080', 'cdp_port': 9222}},
])
def test_validation_and_registered_identity_stay_protected(actions, fields):
    s = actions
    before = business(s)
    result = update(s, {**before['config_json'], **fields})
    assert result.status == 'error' and result.error['code'] == 'validation_error'
    assert business(s) == before


def test_global_native_projection_covers_all_current_business_code_roots(tmp_path):
    f = Fixture(tmp_path)
    actor = f.create('chief-engineer', 'ordubashy')  # role, not name
    second, base = new_id(), f.root / 'second'
    with f.db.transaction() as u:
        u.repository('businesses').insert({'id': second, 'slug': 'second', 'workspace': str(base),
                                          'hq_path': str(base / 'hq'), 'name': 'Second'})
        for before, data in additions_for_business(u.connection, second, f.registry):
            u.repository('agent_profiles', business_id=None).update(before['agent_id'], {'profile_json': data}, expected_revision=before['revision'])
        u.repository('business_memberships', business_id=second).insert({'principal_id': actor.id})
    def preview():
        return build_projection(f.resolver, f.resolver.resolve_agent_config(actor),
            registration_id='preview-only', endpoint='http://127.0.0.1:9988', interpreter=sys.executable)
    projection = preview()
    roots = {str(f.hq.parent / 'code'), str(base / 'code')}
    assert set(projection.metadata['business_code_roots']) == roots
    assert projection.metadata['business_code_root'] is None
    assert not (base / 'code').exists()  # preview is read-only
    local = json.loads(projection.files['.claude/settings.local.json'])['permissions']
    assert set(local['additionalDirectories']) == roots and 'Bash' in local['allow']
    filesystem = tomllib.loads(projection.files['.codex/config.toml'].decode())['permissions']['qorqut-agent']['filesystem']
    assert all(filesystem[root] == 'write' for root in roots)
    assert filesystem[str(base / 'hq/context')] == 'read'
    assert filesystem[str(f.root / '.bazarlyk')] == 'deny'
    assert str(f.root) not in local['additionalDirectories']
    orientation = projection.files['AGENTS.md'].decode()
    assert 'The owner alone builds software' in orientation
    assert 'owner/Ordubashy intake queue' in orientation
    assert 'Coordinate authorized operations' in orientation
    config = f.resolver.resolve_agent_config(actor, business_id=f.business_id).to_dict()
    assert 'Engineering workspace and authority' in config['mandatory_context']['rendered']
    ready = f.prepare(actor)
    assert all(Path(root).is_dir() for root in roots)
    assert ready['ready']
    with f.db.transaction() as u:
        u.connection.execute('UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?', (second, actor.id))
    assert preview().metadata['business_code_roots'] == [str(f.hq.parent / 'code')]
    f.update(actor, {'role': f.refs['founder']})
    assert preview().metadata['business_code_roots'] == []
    assert all(Path(root).is_dir() for root in roots)  # revocation preserves work


def test_ordubashy_native_roots_reject_symlinks(tmp_path):
    f = Fixture(tmp_path)
    actor = f.create('chief', 'ordubashy')
    (f.hq.parent / 'code').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(DomainError, match='Project code directory'):
        build_projection(f.resolver, f.resolver.resolve_agent_config(actor), registration_id='preview',
                         endpoint='http://127.0.0.1:9988', interpreter=sys.executable)
