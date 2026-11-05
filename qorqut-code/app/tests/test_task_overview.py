"""The owner/self overview is brief, complete-by-cursor and currently scoped."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.domains.tasks.models import DEFAULT_CONFIG
from test_global_tasks import call, ok, setup
from mcp_efficiency_support import isolated_service


def overview(s, **args):
    return ok(call(s, 'tasks_overview', args))


def entries(result):
    return [(group['business_id'], category, task)
            for group in result['scopes'] for category in ('active', 'snoozed')
            for task in group[category]]


def business(s, slug, *, grant=True):
    identity = new_id()
    hq = s.fixture.root / slug / 'hq'
    hq.mkdir(parents=True)
    with s.fixture.db.transaction() as uow:
        uow.repository('businesses').insert({'id': identity, 'slug': slug, 'name': slug.title(),
                                           'hq_path': str(hq), 'workspace': str(hq.parent)})
        uow.repository('business_memberships', business_id=identity).insert(
            {'principal_id': s.fixture.owner.id, 'is_owner': True})
        # Production business creation expands all-business Ordubashy profiles
        # atomically; mirror that existing invariant in this isolated fixture.
        from bazarlyk.agents.ordubashy import additions_for_business
        for row, updated in additions_for_business(uow.connection, identity, s.runtime.registry):
            uow.repository('agent_profiles', business_id=None).update(row['agent_id'],
                {'profile_json': updated}, expected_revision=row['revision'])
            uow.repository('business_memberships', business_id=identity).insert(
                {'principal_id': row['agent_id'], 'active': grant})
    s.fixture.ctx = replace(s.fixture.ctx, allowed_business_ids=s.fixture.ctx.allowed_business_ids | {identity})
    s.orductx = s.runtime.resolver.invocation_context(s.ordu)
    return identity


def seed(s, business_id, identity, *, assignees=None, **values):
    if assignees is None:
        assignees = [(s.fixture.owner.id, 'person')]
    with s.fixture.db.transaction() as uow:
        uow.repository('tasks', business_id=business_id).insert(
            {'id': identity, 'title': identity, 'status': 'open', 'created_by': s.fixture.owner.id, **values})
        for assignee_id, kind in assignees:
            uow.repository('relationships', business_id=business_id).insert({'id': new_id(),
                'from_type': 'task', 'from_id': identity, 'to_type': kind,
                'to_id': assignee_id, 'role': 'assigned_to'})


def test_default_combines_owner_self_and_all_readable_scopes_without_detail(setup, monkeypatch):
    s = setup
    other, hidden = business(s, 'beta'), business(s, 'secret', grant=False)
    for scope in (None, s.fixture.business_id, other, hidden):
        seed(s, scope, 'same-id', tags_json=['engineering', 'design/software'] if scope is None else [])
    seed(s, other, 'self', assignees=[(s.ordu.id, 'agent')], due='2026-09-15')
    seed(s, None, 'both', assignees=[(s.ordu.id, 'agent'), (s.fixture.owner.id, 'person')])
    seed(s, None, 'unassigned', assignees=[])
    seed(s, s.fixture.business_id, 'someone-else', assignees=[(s.agent.id, 'agent')])
    seed(s, None, 'long', title='x' * 1000, fields_json={'private-detail': 'never in overview'})
    with s.fixture.db.transaction() as uow:
        uow.repository('task_notes', business_id=None).insert({'id': new_id(), 'task_id': 'long',
            'body': 'private note must not be loaded', 'created_by': s.fixture.owner.id})
    def no_detail(*args, **kwargs):
        pytest.fail('Overview must not hydrate full task details')
    monkeypatch.setattr(s.runtime.domains.tasks, 'detail', no_detail)
    result = overview(s)
    rows = entries(result)
    assert len(rows) == 6 and result['counts'] == {'active': 6, 'snoozed': 0}
    assert result['next_cursor'] is None
    assert {scope for scope, _, _ in rows} == {None, s.fixture.business_id, other}
    assert sum(task['id'] == 'same-id' for _, _, task in rows) == 3
    assert next(task for scope, _, task in rows if scope is None and task['id'] == 'same-id')['tags'] == ['engineering', 'design/software']
    assert next(task for _, _, task in rows if task['id'] == 'both')['assignees'] == ['owner', 'self']
    long = next(task for _, _, task in rows if task['id'] == 'long')
    assert len(long['title']) == 180 and long['title_truncated']
    assert set(long) == {'id', 'revision', 'title', 'title_truncated', 'status', 'due', 'assignees', 'tags', 'task_type'}
    assert long['tags'] == []
    assert 'private' not in json.dumps(result) and 'secret' not in json.dumps(result)
    assert result['assignees'] == {'owner': s.fixture.owner.id, 'self': s.ordu.id}
    selected = s.orductx.select_business(s.fixture.business_id)
    assert ok(call(s, 'tasks_overview', {}, ctx=selected)) == result
    assert overview(s, assignee='owner')['counts']['active'] == 5
    assert overview(s, assignee='self')['counts']['active'] == 2


def test_overview_revision_supports_explicit_due_and_tag_edit_without_detail_read(tmp_path):
    with isolated_service(tmp_path) as s:
        s.ordu = s.fixture.create('ordu', 'ordubashy')
        s.app = s.runtime.application
        s.orductx = s.runtime.resolver.invocation_context(s.ordu)
        s.fixture.update(s.ordu, {'tool_policy': {'overrides': {'tasks_overview': 'allow'}}})
        s.orductx = s.runtime.resolver.invocation_context(s.ordu)
        seed(s, None, 'overview-edit', due='2026-09-30', tags_json=['operations'])
        row = next(task for _, _, task in entries(overview(s)) if task['id'] == 'overview-edit')
        assert isinstance(row['revision'], int)

        updated = ok(call(s, 'tasks_update', {'scope': 'global', 'task_id': row['id'],
            'changes': {'due': '2026-10-05', 'tags': ['operations', 'engineering']}},
            revision=row['revision']))['task']
        assert updated['due'] == '2026-10-05'
        assert updated['tags'] == ['operations', 'engineering']
        assert updated['revision'] > row['revision']

        stale = call(s, 'tasks_update', {'scope': 'global', 'task_id': row['id'],
            'changes': {'due': '2026-10-12'}}, revision=row['revision'])
        assert stale.status == 'error' and stale.error['code'] == 'revision_conflict'


def test_snooze_expiry_closed_custom_states_and_archives(setup, monkeypatch):
    s = setup
    now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(s.runtime.domains.tasks, 'clock', lambda: now)
    seed(s, None, 'active', due='2026-09-13', tags_json=['research', 'design/software', 'custom-note'])
    seed(s, None, 'expired', snoozed_until='2026-09-12T12:00:00.000000Z', tags_json=['design/software'])
    seed(s, None, 'later', snoozed_until='2026-09-13T12:00:00.000000Z', tags_json=['design/software'])
    seed(s, None, 'indefinite', snooze_indefinite=1, tags_json=['custom-note'])
    seed(s, None, 'closed', status='done')
    seed(s, None, 'archived', archived=True)
    config = {**DEFAULT_CONFIG, 'states': [{'name': 'todo', 'kind': 'open', 'default': True},
        {'name': 'done', 'kind': 'open', 'paused': True}, {'name': 'finished', 'kind': 'closed'}]}
    ok(call(s, 'tasks_config_update', {'config': config}, ctx=s.fixture.ctx))
    seed(s, s.fixture.business_id, 'custom-open', status='done')
    seed(s, s.fixture.business_id, 'custom-closed', status='finished')
    result = overview(s)
    assert result['counts'] == {'active': 3, 'snoozed': 2}
    snoozed = {task['id']: task['snooze'] for _, category, task in entries(result) if category == 'snoozed'}
    assert snoozed == {'indefinite': {'indefinite': True}, 'later': {'until': '2026-09-13T12:00:00.000000Z'}}
    snoozed_tags = {task['id']: task['tags'] for _, category, task in entries(result) if category == 'snoozed'}
    assert snoozed_tags == {'indefinite': ['custom-note'], 'later': ['design/software']}
    assert next(task for _, _, task in entries(result) if task['id'] == 'active')['tags'] == ['data-science', 'design/software', 'custom-note']
    assert overview(s, snooze_filter='active')['counts'] == {'active': 3, 'snoozed': 0}
    assert overview(s, snooze_filter='snoozed')['counts'] == {'active': 0, 'snoozed': 2}
    assert overview(s, include_closed=True)['counts'] == {'active': 5, 'snoozed': 2}
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    assert overview(s)['counts'] == {'active': 4, 'snoozed': 1}


def test_pagination_is_global_unique_ordered_and_counts_are_not_page_counts(setup, monkeypatch):
    s = setup
    seed(s, None, 'same')
    seed(s, s.fixture.business_id, 'same')
    with s.fixture.db.transaction() as uow:
        for i in range(205):
            identity = f'page-{i:03}'
            uow.repository('tasks', business_id=None).insert({'id': identity, 'title': identity,
                'status': 'open', 'created_by': s.fixture.owner.id, 'due': '2026-09-13' if i == 100 else None})
            uow.repository('relationships', business_id=None).insert({'id': new_id(),
                'from_type': 'task', 'from_id': identity, 'to_type': 'person',
                'to_id': s.fixture.owner.id, 'role': 'assigned_to'})
    first = overview(s)
    assert len(entries(first)) == 100 and first['counts']['active'] == 207
    assert entries(first)[0][2]['id'] == 'page-100'
    seen, cursor = [], None
    while True:
        result = overview(s, limit=80, **({'cursor': cursor} if cursor else {}))
        assert result['counts']['active'] == 207
        seen.extend((scope, task['id']) for scope, _, task in entries(result))
        cursor = result['next_cursor']
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 207
    cursor = first['next_cursor']
    assert call(s, 'tasks_overview', {'cursor': cursor, 'assignee': 'owner'}).error['code'] == 'validation_error'
    other = s.fixture.create('another-ordu', 'ordubashy')
    assert call(s, 'tasks_overview', {'cursor': cursor},
                ctx=s.runtime.resolver.invocation_context(other)).error['code'] == 'validation_error'
    monkeypatch.setattr('bazarlyk.domains.tasks.overview.MAX_MATCHES', 2)
    assert call(s, 'tasks_overview', {}).error['code'] == 'result_too_large'


def test_role_scope_membership_and_read_overrides_are_not_bypassed(setup):
    s = setup
    seed(s, None, 'global')
    other = business(s, 'beta')
    seed(s, other, 'business')
    cursor = overview(s, limit=1)['next_cursor']
    with s.fixture.db.transaction() as uow:
        member = uow.repository('business_memberships', business_id=other).get(s.ordu.id)
        uow.repository('business_memberships', business_id=other).update(s.ordu.id, {'active': False}, expected_revision=member['revision'])
    assert overview(s)['counts']['active'] == 1
    assert call(s, 'tasks_overview', {'cursor': cursor}).error['code'] == 'validation_error'
    for operation in ('tasks_overview', 'tasks_list'):
        s.fixture.update(s.ordu, {'tool_policy': {'overrides': {operation: 'deny'}}})
        assert call(s, 'tasks_overview', {}).status == 'error'
    s.fixture.update(s.ordu, {'tool_policy': {'overrides': {}}})
    for principal in (s.agent, s.fixture.create('marketing', 'global-marketing')):
        s.fixture.update(principal, {'tool_policy': {'overrides': {'tasks_overview': 'allow'}}})
        assert call(s, 'tasks_overview', {}, ctx=s.runtime.resolver.invocation_context(principal)).status == 'error'
    applier = s.fixture.create('applier', 'learning-applier', scope={'kind': 'ecosystem',
        'allowed_business_ids': [], 'global_capabilities': ['read_global_context']},
        tool_policy={'overrides': {'tasks_overview': 'allow'}})
    assert call(s, 'tasks_overview', {}, ctx=s.runtime.resolver.invocation_context(applier)).error['code'] == 'forbidden'
    assert call(s, 'tasks_overview', {}, ctx=s.owner).error['code'] == 'forbidden'
    profile, _ = s.runtime.resolver.profile(s.ordu)
    scope = profile.to_dict()['scope']
    scope['global_capabilities'] = []
    with pytest.raises(DomainError, match='read_global_context'):
        s.fixture.update(s.ordu, {'scope': scope})
    # A corrupted all-business profile must also fail closed on a fresh read.
    with s.fixture.db.transaction() as uow:
        data = profile.to_dict()
        data['scope']['allowed_business_ids'] = []
        data['profile_revision'] += 1
        uow.repository('agent_profiles', business_id=None).update(s.ordu.id,
            {'profile_json': data}, expected_revision=profile.revision)
    assert call(s, 'tasks_overview', {}).error['code'] == 'invalid_scope'


def test_authoritative_owner_not_name_or_other_human_and_inactive_assignments(setup):
    s = setup
    owner, impostor = s.fixture.owner.id, new_id()
    with s.fixture.db.transaction() as uow:
        uow.repository('principals').insert({'id': impostor, 'kind': 'human', 'display_name': 'Owner'})
        uow.repository('business_memberships', business_id=s.fixture.business_id).insert(
            {'principal_id': impostor, 'is_owner': True})
    seed(s, s.fixture.business_id, 'actual-owner')
    seed(s, s.fixture.business_id, 'impostor', assignees=[(impostor, 'person')])
    seed(s, s.fixture.business_id, 'self-and-owner', assignees=[(owner, 'person'), (s.ordu.id, 'agent')])
    assert overview(s)['counts']['active'] == 2
    with s.fixture.db.transaction() as uow:
        uow.repository('business_memberships', business_id=s.fixture.business_id).update(owner, {'active': False}, expected_revision=1)
    rows = entries(overview(s))
    assert len(rows) == 1 and rows[0][2]['assignees'] == ['self']
    with s.fixture.db.transaction() as uow:
        uow.repository('principals').update(owner, {'active': False}, expected_revision=1)
    assert call(s, 'tasks_overview', {}).error['code'] == 'unavailable'


def test_invalid_filters_and_identity_spoofing_fail(setup):
    for arguments in ({'assignee': 'some-agent'}, {'owner_id': new_id()}, {'scope': 'business'},
                      {'limit': 0}, {'limit': 201}, {'snooze_filter': 'unknown'}, {'cursor': 'bad'}):
        assert call(setup, 'tasks_overview', arguments).error['code'] == 'validation_error'


def test_registered_mcp_tool_and_native_projections(setup, monkeypatch):
    s = setup
    seed(s, None, 'visible', tags_json=['design/software', 'custom-note'])
    ready = s.fixture.prepare(s.ordu)
    workspace = Path(ready['workspace'])
    s.tokens[s.ordu.id] = (workspace / '.bazarlyk/credential').read_text().strip()
    s.registrations[s.ordu.id] = ready['registration_id']
    for prefix in ('.claude/skills', '.agents/skills'):
        assert 'tasks_overview once' in (workspace / prefix / 'bazarlyk-role-ecosystem-review/SKILL.md').read_text()
    settings = json.loads((workspace / '.claude/settings.json').read_text())
    assert 'mcp__bazarlyk__tasks_overview' in settings['permissions']['allow']
    monkeypatch.setattr(s.runtime.background, 'start', lambda: None)
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.ordu)
        response = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        assert 'tasks_overview' in {tool['name'] for tool in response.json()['result']['tools']}
        wire = s.tool(client, headers, 'tasks_overview', {})
        assert not wire['isError'] and wire['decoded']['data']['counts']['active'] == 1
        assert wire['decoded']['data']['scopes'][0]['active'][0]['tags'] == ['design/software', 'custom-note']
        assert len(wire['content'][0]['text']) < 1200
        other_headers = s.initialize(client, s.agent)
        denied = s.tool(client, other_headers, 'tasks_overview', {})
        assert denied['isError']
