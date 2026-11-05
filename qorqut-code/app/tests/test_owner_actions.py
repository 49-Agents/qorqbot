"""Delegation uses the real registry, policy, storage and owner Signoff worker."""
from dataclasses import replace

import pytest

from qorqut_operations.contracts import new_id, plain
from bazarlyk.policy.resolver import resolve_operation_policy
from bazarlyk.service.runtime import build_runtime
from test_signoffs import REPO, PATHS, Scenario

ENGINEERING = {'github_organizations': ['ThreadBro'], 'repositories': ['ThreadBro/x-reply-extension'], 'deployment_targets': []}


@pytest.fixture
def actions(tmp_path):
    s = Scenario(tmp_path)
    with s.db.transaction() as u:
        u.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': s.owner.id})
        u.repository('businesses').update(s.business, {'stage': 'validating',
            'config_json': {'one_liner': 'Preserve this', 'code': ['existing/repo']}}, expected_revision=1)
    runtime = build_runtime(s.root, REPO, paths=PATHS, database=s.db)
    s.app, s.service, s.policy, s.resolver, s.registry = runtime.application, runtime.signoffs, runtime.policy, runtime.resolver, runtime.registry
    s.actor = s.add_agent('ordubashy')
    with s.db.transaction() as u:
        u.repository('business_memberships', business_id=s.other_business).insert({'principal_id': s.actor.id})
    s.ctx = lambda: s.agent_context(s.actor)
    yield s
    runtime.stop()


def business(s, identity=None):
    with s.db.read() as u:
        return u.repository('businesses').get(identity or s.business)


def submit(s, action='engineering_update', parameters=None, *, ctx=None, revision=None, key=None, **extra):
    return s.app.invoke(ctx or s.ctx(), 'owner_action_propose',
        {'action': action, 'parameters': ENGINEERING if parameters is None else parameters,
         'rationale': 'Owner requested this exact business setup.', **extra},
        {'expected_revision': revision if revision is not None else business(s)['revision'], 'idempotency_key': key or new_id()})


def staged(result):
    assert result.status == 'signoff_required', result.to_dict()
    return plain(result.signoff)


def apply(s, proposal):
    result = s.decide(proposal)
    assert result.status == 'accepted', result.to_dict()
    s.service.run_pending()
    detail = s.detail(proposal)
    assert detail['state'] == 'applied', detail
    return detail


def test_engineering_stays_pending_until_exact_owner_approval_and_preserves_config(actions):
    s = actions
    before = business(s)
    key = new_id()
    proposal = staged(submit(s, key=key))
    assert staged(submit(s, key=key))['id'] == proposal['id']
    assert business(s) == before
    assert s.service.run_pending() == 0
    assert submit(s, key=key, parameters={**ENGINEERING, 'repositories': ['another/repo']}).status == 'error'
    # Neither the proposer nor an ordinary member can approve for the owner.
    for ctx in (s.ctx(), replace(s.owner_context, principal=s.viewer)):
        assert s.decide(proposal, context=ctx).status == 'error'
    assert s.decide({**proposal, 'payload_hash': 'wrong'}).status == 'error'
    detail = apply(s, proposal)
    after = business(s)
    assert after['config_json'] == {**before['config_json'], 'engineering': ENGINEERING}
    assert detail['proposer_agent_id'] == s.actor.id
    assert detail['decision']['by'] == s.owner.id
    assert s.service.run_pending() == 0
    assert business(s) == after
    assert detail['preview']['title'] == 'Update one engineering settings'
    assert 'all repositories' in str(detail['preview']['effects'])
    assert 'ThreadBro/x-reply-extension' in str(detail['preview']['effects'])
    assert 'deployment_targets: null → []' in detail['preview']['effects']
    next_proposal = staged(submit(s, parameters={**ENGINEERING, 'repositories': ['ThreadBro/another']}))
    assert next_proposal['id'] != proposal['id']
    assert s.service.run_pending() == 0
    assert business(s) == after  # Earlier approval is not reusable authority.


def test_rejection_and_stale_business_revision_never_apply(actions):
    s = actions
    before = business(s)
    proposal = staged(submit(s))
    assert s.decide(proposal, 'reject', feedback='Not this scope').status == 'ok'
    assert s.service.run_pending() == 0 and business(s) == before
    proposal = staged(submit(s))
    with s.db.transaction() as u:
        u.repository('businesses').update(s.business, {'config_json': {'newer': 'Owner edit'}}, expected_revision=before['revision'])
    changed = business(s)
    s.decide(proposal)
    s.service.run_pending()
    assert s.detail(proposal)['state'] != 'applied'
    assert business(s) == changed
    assert submit(s, revision=before['revision']).error['code'] == 'revision_conflict'


@pytest.mark.parametrize('loss', ['override', 'membership', 'inactive'])
def test_current_authority_is_rechecked_after_approval(actions, loss):
    s = actions
    before = business(s)
    proposal = staged(submit(s))
    assert s.decide(proposal).status == 'accepted'
    if loss == 'override':
        s.change_override('owner_action_propose', 'deny', actor=s.actor)
    else:
        with s.db.transaction() as u:
            if loss == 'membership':
                u.connection.execute('UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?', (s.business, s.actor.id))
            else:
                u.connection.execute('UPDATE principals SET active=0 WHERE id=?', (s.actor.id,))
    s.service.run_pending()
    assert s.detail(proposal)['state'] != 'applied'
    assert business(s) == before


def test_role_lock_and_allow_override_cannot_remove_approval(actions):
    s = actions
    s.change_override('owner_action_propose', 'allow', actor=s.actor)
    staged(submit(s))
    s.change_override('owner_action_propose', 'allow', actor=s.agent)
    for actor in (s.agent, s.add_agent('global-marketing', {'owner_action_propose': 'allow'})):
        # Supplying Ordubashy's artifact ref does not override registered identity.
        ctx = replace(s.agent_context(actor), role_artifact_ref=s.ctx().role_artifact_ref)
        assert submit(s, ctx=ctx).status == 'error'
    for role in ('business-agent', 'global-marketing', 'founder', 'learning-applier', None):
        decision = resolve_operation_policy('owner_action_propose', {'owner_action_propose': 'allow'}, {}, s.registry, role_id=role)
        assert decision.mode == 'deny'
    assert submit(s, ctx=replace(s.ctx(), business_id=new_id())).status == 'error'
    assert submit(s, ctx=replace(s.ctx(), business_id=None)).status == 'error'


@pytest.mark.parametrize('change', [
    {'action': 'signoffs_review', 'parameters': {'decision': 'approve'}},
    {'action': 'terminals_write', 'parameters': {'text': 'anything'}},
    {'action': 'signoffs_platform_fulfill', 'parameters': {}},
    {'action': 'business_config_update', 'parameters': {}},
    {'approved': True}, {'principal_kind': 'human'},
    {'parameters': {**ENGINEERING, 'token': 'not-allowed'}},
    {'parameters': {**ENGINEERING, 'repositories': ['../other']}},
    {'parameters': {'github_organizations': ['ThreadBro']}},
    {'rationale': '   '},
])
def test_no_arbitrary_owner_operations_or_claimed_consent(actions, change):
    s = actions
    before = business(s)
    assert submit(s, **change).status == 'error'
    assert s.count('signoffs') == 0 and business(s) == before


def test_browser_proposal_freezes_identity_and_preserves_it_on_update(actions, monkeypatch):
    s = actions
    monkeypatch.setattr('bazarlyk.browser.config.port_available', lambda port: True)
    before = business(s)
    parameters = {'enabled': True, 'proxy_server': 'http://proxy.example:8080'}
    proposal = staged(submit(s, 'browser_update', parameters))
    assert 'http://proxy.example:8080' in str(s.detail(proposal)['preview']['effects'])
    assert business(s) == before
    assert not (s.root / '.bazarlyk/browsers').exists()
    apply(s, proposal)
    initial = business(s)['config_json']['browser']
    assert initial['cdp_port'] == 19400
    assert initial['user_data_dir'] == str(s.root / '.bazarlyk/browsers' / s.business / 'profile')
    assert business(s)['config_json']['code'] == ['existing/repo']
    # Configuration approval does not launch Chrome or write credential files.
    assert not (s.root / '.bazarlyk/browsers').exists()
    proposal = staged(submit(s, 'browser_update', {**parameters, 'proxy_server': 'http://new-proxy.example:8080'}))
    apply(s, proposal)
    updated = business(s)['config_json']['browser']
    for key in ('cdp_port', 'user_data_dir', 'downloads_dir', 'credentials_ref'):
        assert updated[key] == initial[key]
    assert updated['generation'] != initial['generation']
    for extra in ({'cdp_port': 9222}, {'user_data_dir': '/some/other/persona'}, {'credentials_ref': 'another'}, {'proxy_server': 'http://user:secret@proxy.example:8080'}):
        assert submit(s, 'browser_update', {**parameters, **extra}).status == 'error'


def test_competing_browser_allocations_cannot_silently_reassign_approved_port(actions, monkeypatch):
    s = actions
    monkeypatch.setattr('bazarlyk.browser.config.port_available', lambda port: True)
    parameters = {'enabled': True, 'proxy_server': 'http://proxy.example:8080'}
    first = staged(submit(s, 'browser_update', parameters))
    ctx = replace(s.ctx(), business_id=s.other_business, review_business_id=s.other_business)
    second = staged(submit(s, 'browser_update', parameters, ctx=ctx, revision=business(s, s.other_business)['revision']))
    apply(s, first)
    owner = replace(s.owner_context, business_id=s.other_business, review_business_id=s.other_business)
    assert s.decide(second, context=owner).status == 'accepted'
    s.service.run_pending()
    assert s.detail(second, owner)['state'] != 'applied'
    assert 'browser' not in business(s, s.other_business)['config_json']


ISOLATED_BROWSER = {'enabled': True, 'proxy_server': 'http://188.220.114.215:12323', 'mode': 'isolated-desktop-v1',
                    'expected_exit_ip': '188.220.114.215'}


def test_isolated_browser_proposal_is_exact_and_needs_no_launch(actions, monkeypatch):
    s = actions
    monkeypatch.setattr('bazarlyk.browser.config.port_available', lambda port: True)
    before = business(s)
    proposal = staged(submit(s, 'browser_update', ISOLATED_BROWSER))
    effects = str(s.detail(proposal)['preview']['effects'])
    assert 'isolated desktop browser' in effects and '188.220.114.215' in effects
    assert business(s) == before
    apply(s, proposal)
    saved = business(s)['config_json']['browser']
    assert saved['mode'] == 'isolated-desktop-v1' and saved['expected_exit_ip'] == '188.220.114.215'
    assert saved['user_data_dir'] == business(s)['hq_path'] + '/browser/profile'
    assert not (s.root / '.bazarlyk/browsers').exists()
    for bad in ({'proxy_server': 'http://proxy.example:8080'}, {'expected_exit_ip': 'proxy.example'},
                {'proxy_server': 'socks5://188.220.114.215:1080'}, {'cdp_port': 9225}, {'mode': 'other'}):
        assert submit(s, 'browser_update', {**ISOLATED_BROWSER, **bad}).status == 'error'
    missing = {key: value for key, value in ISOLATED_BROWSER.items() if key != 'expected_exit_ip'}
    assert submit(s, 'browser_update', missing).status == 'error'


def test_two_signoffs_for_one_proxy_ip_apply_only_once(actions, monkeypatch):
    s = actions
    monkeypatch.setattr('bazarlyk.browser.config.port_available', lambda port: True)
    first = staged(submit(s, 'browser_update', ISOLATED_BROWSER))
    ctx = replace(s.ctx(), business_id=s.other_business, review_business_id=s.other_business)
    # Prepared independently, on a different port: only apply-time uniqueness can arbitrate.
    second = staged(submit(s, 'browser_update', {**ISOLATED_BROWSER, 'proxy_server': 'http://188.220.114.215:9999'},
                           ctx=ctx, revision=business(s, s.other_business)['revision']))
    apply(s, first)
    owner = replace(s.owner_context, business_id=s.other_business, review_business_id=s.other_business)
    # The pending exact change is revalidated at approval and again when applied.
    decision = s.decide(second, context=owner)
    assert decision.status == 'error' and 'already assigned' in str(decision.to_dict()), decision.to_dict()
    s.service.run_pending()
    assert s.detail(second, owner)['state'] != 'applied'
    assert 'browser' not in business(s, s.other_business)['config_json']
    # Preparation now refuses the same claim outright.
    assert submit(s, 'browser_update', ISOLATED_BROWSER, ctx=ctx,
                  revision=business(s, s.other_business)['revision']).status == 'error'


def test_story_lifecycle_uses_owner_approval_and_agent_attribution(actions):
    s = actions
    result = s.app.invoke(s.owner_context, 'stories_create', {'title': 'Site launch', 'purpose': 'Ship the site'}, {'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    story_id = result.data['story']['id']
    def story():
        with s.db.read() as u:
            return u.repository('stories', business_id=s.business).get(story_id)
    parameters = {'story_id': story_id, 'outcome': 'success', 'note': 'Owner confirmed the site shipped'}
    assert s.app.invoke(s.ctx(), 'stories_close', parameters, {'expected_revision': 1, 'idempotency_key': new_id()}).status == 'error'
    proposal = staged(submit(s, 'story_close', parameters, revision=1))
    assert story()['state'] == 'planned'
    apply(s, proposal)
    assert story()['state'] == 'closed'
    with s.db.read() as u:
        event = u.connection.execute("SELECT actor_id FROM story_events WHERE story_id=? AND kind='close'", (story_id,)).fetchone()
        assert event['actor_id'] == s.actor.id
    parameters = {'story_id': story_id, 'state': 'working', 'note': 'Another release is needed'}
    proposal = staged(submit(s, 'story_reopen', parameters, revision=story()['revision']))
    assert story()['state'] == 'closed'
    apply(s, proposal)
    assert story()['state'] == 'working' and story()['outcome'] is None


def test_direct_config_route_works_and_optional_proposal_schema_is_typed(actions, monkeypatch):
    from jsonschema import Draft202012Validator
    from bazarlyk.transport.mcp import tool_schema
    s = actions
    monkeypatch.setattr('bazarlyk.browser.config.port_available', lambda port: True)
    for key, value in (('engineering', ENGINEERING), ('browser', {'enabled': True, 'proxy_server': 'http://proxy.example:8080'})):
        before = business(s)
        result = s.app.invoke(s.ctx(), 'business_config_update', {'config': {**before['config_json'], key: value}},
            {'expected_revision': before['revision'], 'idempotency_key': new_id()})
        assert result.status == 'ok', result.to_dict()
        assert s.count('signoffs') == 0
        assert business(s)['config_json']['one_liner'] == before['config_json']['one_liner']
    # Existing ordinary configuration edits remain available without extra signoff.
    before = business(s)
    result = s.app.invoke(s.ctx(), 'business_config_update', {'config': {**before['config_json'], 'one_liner': 'Ordinary edit'}},
        {'expected_revision': before['revision'], 'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    validator = Draft202012Validator(tool_schema(s.registry, 'owner_action_propose'))
    request = {'action': 'engineering_update', 'parameters': ENGINEERING, 'rationale': 'Requested setup',
               'expected_revision': business(s)['revision'], 'idempotency_key': new_id()}
    assert validator.is_valid(request)
    assert not validator.is_valid({**request, 'approved': True})
    assert not validator.is_valid({**request, 'parameters': {**ENGINEERING, 'secret': 'no'}})
    package = s.catalog.get('role', s.roles['ordubashy'])
    assert 'skills/ecosystem-review/references/owner-actions.md' in package.files
