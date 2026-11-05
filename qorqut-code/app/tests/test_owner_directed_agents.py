import pytest

from qorqut_operations.contracts import new_id
from inbox_support import pending_count
from test_agent_inbox import inbox
from test_agent_messages import call, sent


@pytest.mark.parametrize('role', ['founder', 'learning-applier'])
def test_owner_directed_inbound_outbound_and_assignment(inbox, role):
    s = inbox
    target = s.fixture.create('guided', role, scope={
        'kind': 'ecosystem', 'allowed_business_ids': [s.fixture.business_id],
        'global_capabilities': ['read_global_context']})
    def owner(op, args):
        return s.runtime.application.invoke(s.fixture.ctx, op, args, {'idempotency_key': new_id()})
    assert target.id not in {r['agent_id'] for r in s.call('agents_message_recipients', {}).data['items']}
    assert target.id in {r['agent_id'] for r in owner('agents_message_recipients', {}).data['items']}
    assert s.call('agents_message_send', {'to_agent_id': target.id, 'message': 'delegate'}).status == 'error'
    outgoing = sent(call(s, target, 'agents_message_send', {'to_agent_id': s.agent.id, 'message': 'outbound'}, business=s.fixture.business_id))
    assert s.call('agents_message_send', {'reply_to': outgoing['id'], 'message': 'reply'}).status == 'error'
    sent(owner('agents_message_send', {'to_agent_id': target.id, 'message': 'owner direction'}))
    assert target.id not in {r['id'] for r in s.call('assignees_list', {}).data['assignees']}
    assert target.id in {r['id'] for r in owner('assignees_list', {}).data['assignees']}
    args = {'title': 'Owner selected work', 'assignee_ids': [target.id]}
    rejected = call(s, s.agent, 'tasks_create', args)
    assert rejected.status == 'error' and rejected.error['code'] == 'invalid_assignee', rejected.to_dict()
    created = owner('tasks_create', args)
    assert created.status == 'ok', created.to_dict()
    task = created.data['task']
    for op, values in [
        ('tasks_assign', {'task_id': task['id'], 'assignee_ids': [target.id]}),
        ('tasks_update', {'task_id': task['id'], 'changes': {'assignee_ids': [target.id]}}),
    ]:
        result = s.runtime.application.invoke(s.ctx(), op, values, {'idempotency_key': new_id(), 'expected_revision': task['revision']})
        assert result.status == 'error' and result.error['code'] == 'invalid_assignee', result.to_dict()


def test_environment_inventory_has_names_not_values():
    from bazarlyk.bootstrap.environment import environment_context
    text = environment_context({'CLOUDFLARE_API_TOKEN': 'never-print-this', 'CLOUDFLARE_ACCOUNT_ID': '', 'BAD\nNAME': 'x'})
    assert 'CLOUDFLARE_API_TOKEN' in text and 'CLOUDFLARE_ACCOUNT_ID' in text
    assert 'never-print-this' not in text and 'BAD\nNAME' not in text
    assert 'new terminal' not in text
    assert 'none' in environment_context({})
    assert len(environment_context({f'VAR_{n}': 'secret' for n in range(1000)})) < 3200
    for budget in (0, 100, 200, 500, 1000):
        assert len(environment_context({'TOKEN': 'secret'}, max_chars=budget)) <= budget


@pytest.mark.parametrize('global_scope', [False, True])
def test_ordubashy_can_discover_message_and_reply_to_founder(inbox, global_scope):
    s = inbox
    coordinator = s.fixture.create('coordinator', 'ordubashy')
    founder = s.fixture.create('founder-recipient', 'founder', scope={
        'kind': 'ecosystem', 'allowed_business_ids': [s.fixture.business_id],
        'global_capabilities': ['read_global_context']})
    business = None if global_scope else s.fixture.business_id
    args = {'scope': 'global'} if global_scope else {}
    recipients = call(s, coordinator, 'agents_message_recipients', args, business=business)
    assert founder.id in {r['agent_id'] for r in recipients.data['items']}
    message = sent(call(s, coordinator, 'agents_message_send', {
        **args, 'to_agent_id': founder.id, 'message': 'Please review this update.'}, business=business))
    reply = sent(call(s, founder, 'agents_message_send', {
        **args, 'reply_to': message['id'], 'message': 'What changed?'}, business=business))
    sent(call(s, coordinator, 'agents_message_send', {
        **args, 'reply_to': reply['id'], 'message': 'Here are the details.'}, business=business))
    founder_ctx = s.runtime.resolver.invocation_context(founder)
    if business:
        founder_ctx = founder_ctx.select_business(business)
    assert pending_count(s.runtime.inbox, founder_ctx, new_id()) == 2


def test_ordubashy_exception_preserves_other_owner_directed_boundaries(inbox):
    s = inbox
    coordinator = s.fixture.create('coordinator', 'ordubashy')
    founder = s.fixture.create('founder-recipient', 'founder', scope={
        'kind': 'ecosystem', 'allowed_business_ids': [s.fixture.business_id],
        'global_capabilities': ['read_global_context']})
    learning = s.fixture.create('learning-recipient', 'learning-applier', scope={
        'kind': 'ecosystem', 'allowed_business_ids': [s.fixture.business_id],
        'global_capabilities': ['read_global_context']})
    business = s.fixture.business_id
    assert call(s, coordinator, 'agents_message_send', {
        'to_agent_id': learning.id, 'message': 'No exception'}, business=business).status == 'error'
    rejected = call(s, coordinator, 'tasks_create', {
        'title': 'Not an assignment exception', 'assignee_ids': [founder.id]}, business=business)
    assert rejected.status == 'error' and rejected.error['code'] == 'invalid_assignee'
    s.fixture.update(coordinator, {'tool_policy': {'overrides': {'agents_message_send': 'deny'}}})
    assert call(s, coordinator, 'agents_message_send', {
        'to_agent_id': founder.id, 'message': 'Policy still applies'}, business=business).status == 'error'
