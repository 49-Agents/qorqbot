import json
from types import SimpleNamespace

import pytest

import shared_context_support as support
from bazarlyk.agents import role_workflow
from qorqut_operations.contracts import new_id, plain
from bazarlyk.domains import business_briefing
from test_service_transports import ORIGIN, ServiceFixture
from test_signoff_inputs import FORM

@pytest.fixture
def service(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    paths = s.runtime.paths  # the shared published graph at its final instance locations, written by the engine itself
    support.Synthetic(paths.context, paths.context_runtime).initialize().populate()
    return s


def call(s, actor, name, args=None, *, business=None, revision=None):
    ctx = s.fixture.ctx if actor is None else s.runtime.resolver.invocation_context(actor, business_id=business)
    result = s.runtime.application.invoke(ctx, name, args or {},
        {'idempotency_key': new_id(), **({'expected_revision': revision} if revision else {})})
    assert result.status in {'ok', 'accepted'}, result.to_dict()
    return result.data


def briefing(s, actor=None, *, business=None):
    ctx = s.fixture.ctx if actor is None else s.runtime.resolver.invocation_context(actor, business_id=business)
    result = s.runtime.application.invoke(ctx, 'business_briefing', {})
    return SimpleNamespace(status=result.status, data=plain(result.data), error=plain(result.error), raw=result)


def seed(s):
    owner = s.fixture.owner.id
    blocker = call(s, None, 'tasks_create', {'title': 'Ship the pricing page', 'assignee_ids': [s.agent.id]})['task']
    blocked = call(s, None, 'tasks_create', {'title': 'Announce pricing', 'assignee_ids': [s.agent.id],
                                             'blocked_by': [blocker['id']], 'due': '2026-01-02'})['task']
    call(s, None, 'tasks_create', {'title': 'Someday idea', 'snooze': {'indefinite': True}})
    call(s, None, 'tasks_create', {'title': 'Sign the contract', 'assignee_ids': [owner]})
    done = call(s, None, 'tasks_create', {'title': 'Set up the domain'})['task']
    call(s, None, 'tasks_complete', {'task_id': done['id']}, revision=done['revision'])
    call(s, None, 'stories_create', {'title': 'Launch campaign', 'purpose': 'Launch', 'state': 'working',
                                     'next_step': 'Draft the first post', 'tags': ['marketing-campaign']})
    call(s, None, 'logs_add', {'tag': 'general', 'summary': 'Posted launch note'})
    question = call(s, s.agent, 'signoffs_input_create', {'request_key': 'supplier', 'form': FORM,
                                                          'attention': {'urgent': True, 'reason': 'Needed today'}})
    return blocker, blocked, done, question


def test_owner_briefing_covers_each_section_within_bound(service):
    s = service
    blocker, blocked, done, _ = seed(s)
    result = briefing(s)
    assert result.status == 'ok', result.raw.to_dict()
    data = result.data
    assert data['business']['slug'] == 'widget' and 'unavailable' not in data
    assert 'brief' not in data and data['context']['status'] == 'ready' and data['context']['empty'] is False
    assert data['context']['totals']['nodes'] == 4 and len(data['context']['revision']) == 40
    assert data['context']['nodes'][0] == {'ref': 'node:alice', 'summary': 'Alice Rivera'}
    work = data['open_work']
    assert work['total'] == 4 and work['snoozed'] == 1 and work['complete']
    assert work['blocked'] == [{'id': blocked['id'], 'title': 'Announce pricing', 'status': 'open', 'due': '2026-01-02',
        'assignees': ['Onbashy'], 'blocked_by': [{'id': blocker['id'], 'title': 'Ship the pricing page'}]}]
    assert work['overdue'] == 1
    assert {item['id'] for item in work['active']} >= {blocker['id']}
    story = data['stories']['items'][0]
    assert story['campaign'] and story['next_step'] == 'Draft the first post'
    waiting = data['waiting_on_owner']
    assert waiting['by_kind'] == {'question': 1} and waiting['signoffs'][0]['urgent']
    assert waiting['owner_tasks'] == 1 and waiting['owner_task_items'][0]['title'] == 'Sign the contract'
    recent = data['done_recently']
    assert recent['tasks_completed'] == 1 and recent['tasks'][0]['id'] == done['id']
    assert recent['log'][0]['summary'] == 'Posted launch note'
    assert data['agents']['items'][0]['agent_id'] == s.agent.id
    assert set(data['notes']) >= {'items', 'more'}
    assert 'processes' in data and 'metrics' in data and 'email' in data
    size = len(json.dumps(data, separators=(',', ':'), ensure_ascii=False))
    assert size <= business_briefing.MAX_CHARACTERS


def test_business_agent_own_business_and_denied_sections_stay_partial(service):
    s = service
    seed(s)
    own = briefing(s, s.agent)
    assert own.status == 'ok', own.raw.to_dict()
    assert own.data['business']['id'] == s.fixture.business_id and own.data['open_work']['total'] == 4
    assert 'unavailable' not in own.data['waiting_on_owner'] and own.data['done_recently']['signoff_outcomes_14d'] == 0
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'tasks_list': 'deny', 'stories_list': 'deny',
                                                             'signoffs_list': 'deny'}}})
    partial = briefing(s, s.agent)
    assert partial.status == 'ok', partial.raw.to_dict()
    assert partial.data['open_work'] == {'unavailable': 'policy_denied'}
    assert {key: partial.data['unavailable'][key] for key in ('open_work', 'stories', 'waiting_on_owner')} == {
        'open_work': 'policy_denied', 'stories': 'policy_denied', 'waiting_on_owner': 'policy_denied'}
    recent = partial.data['done_recently']
    assert 'tasks' not in recent and 'signoff_outcomes_14d' not in recent and recent['log']
    assert partial.data['context']['nodes']
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'business_briefing': 'deny'}}})
    assert briefing(s, s.agent).error['code'] == 'policy_denied'


def test_global_agents_select_a_business(service):
    s = service
    seed(s)
    growth = s.fixture.create('growth', 'global-marketing')
    for agent in (s.manager, growth):
        assert briefing(s, agent).error['code'] == 'scope_required'
        selected = briefing(s, agent, business=s.fixture.business_id)
        assert selected.status == 'ok', selected.raw.to_dict()
        assert selected.data['business']['id'] == s.fixture.business_id
    marketing = briefing(s, growth, business=s.fixture.business_id).data
    assert marketing['open_work']['total'] == 4 and 'processes' in marketing['unavailable']


def test_mcp_tool_and_owner_http_route(service):
    from starlette.testclient import TestClient
    s = service
    seed(s)
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.manager)
        tools = client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}).json()
        tool = next(item for item in tools['result']['tools'] if item['name'] == 'business_briefing')
        assert tool['annotations']['readOnlyHint'] and 'business_id' in tool['inputSchema']['properties']
        result = s.tool(client, headers, 'business_briefing', {'business_id': s.fixture.business_id})['decoded']
        assert result['status'] == 'ok' and result['data']['open_work']['total'] == 4, result
        response = client.post('/api/biz/widget/operations/business_briefing', json={}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert response.json()['data']['waiting_on_owner']['pending_signoffs'] == 1


def test_bound_trims_largest_lists():
    rows = [{'id': new_id(), 'title': 'x' * 80} for _ in range(200)]
    result = business_briefing.bound({'open_work': {'active': rows, 'total': 200}, 'note': 'kept'})
    assert len(json.dumps(result, separators=(',', ':'))) <= business_briefing.MAX_CHARACTERS
    assert result['trimmed'] and result['open_work']['total'] == 200 and result['note'] == 'kept'


def test_guidance_reaches_business_and_global_agents(service):
    s = service
    text = ' '.join(role_workflow.INSTRUCTIONS.split())
    assert ("Onbashy reads it for its own business at startup and resume; Dev reads it when the owner's current "
            "project needs it.") in text
    assert 'business_briefing(business_id=...) first whenever the owner names a project' in text
    manager = s.runtime.resolver.resolve_agent_config(s.manager).data['mandatory_context']['rendered']
    agent = s.runtime.resolver.resolve_agent_config(s.agent).data['mandatory_context']['rendered']
    sentence = 'When the owner names a project to work on, call business_briefing(business_id=...) first'
    assert sentence in manager and sentence not in agent
    assert 'business_briefing' in agent
