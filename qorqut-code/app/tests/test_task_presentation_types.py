"""Story/Email are Tasks: same identity/state/fields, authorized whole-thread binding."""
from test_host_parent_tasks import host, call, ok, code, task, get


def test_story_keeps_task_contract_and_custom_fields(host):
    story = task(host, 'Client relationship', task_type='story', overview='Next: discuss the brief')
    child = task(host, 'Send proposal', parent_id=story['id'])
    assert story['task_type'] == 'story' and story['fields'] == {}
    assert get(host, story['id'])['children'][0]['id'] == child['id']
    changed = ok(call(host, host.owner, 'tasks_update', {'task_id': story['id'], 'changes': {'fields': {}, 'status': 'done'}}, revision=story['revision']))['task']
    assert changed['overview'] == story['overview'] and changed['task_type'] == 'story'
    assert get(host, child['id'])['status'] == 'open'
    listed = ok(call(host, host.owner, 'tasks_list', {'task_type':'story','include_closed':True}))['tasks']
    assert [t['id'] for t in listed] == [story['id']]
    assert code(call(host, host.owner, 'tasks_create', {'title':'Invalid', 'fields':{'$task':{'task_type':'story'}}})) == 'validation_error'
    ordinary = task(host, 'Ordinary')
    assert ordinary['task_type'] == 'task' and ordinary['email_conversation_id'] is None


def seed_email(host, scope):
    with host.instance.database.transaction() as uow:
        uow.repository('email_mailboxes', business_id=scope).insert({'id':'mail-test','address':'owner@example.test', **({'purpose':'owner_mail'} if scope is None else {})})
        uow.repository('email_conversations', business_id=scope).insert({'id':'conversation-test','mailbox':'mail-test','provider_id':'thread-1', 'subject':'Project proposal'})
        uow.repository('email_messages', business_id=scope).insert({'id':'11111111-1111-4111-8111-111111111111','mailbox':'mail-test','conversation_id':'conversation-test','provider_id':'message-1','message_json':{'subject':'Project proposal'}})


def test_email_binds_whole_existing_conversation_and_can_change_type(host):
    seed_email(host, host.first)
    email = task(host, 'Discuss proposal', task_type='email', email_conversation_id='conversation-test')
    assert email['email_conversation_id'] == 'conversation-test'
    listed = ok(call(host, host.owner, 'tasks_list', {'task_type':'email','email_conversation_id':'conversation-test'}))['tasks']
    assert [t['id'] for t in listed] == [email['id']]
    assert code(call(host, host.owner, 'tasks_create', {'title':'Missing','task_type':'email'})) == 'validation_error'
    assert code(call(host, host.owner, 'tasks_create', {'title':'Single message','task_type':'email','email_conversation_id':'11111111-1111-4111-8111-111111111111'})) == 'validation_error'
    assert code(call(host, host.owner_second, 'tasks_create', {'title':'Wrong scope','task_type':'email','email_conversation_id':'conversation-test'})) == 'not_found'
    assert code(call(host, host.owner, 'tasks_create', {'title':'Wrong type','email_conversation_id':'conversation-test'})) == 'validation_error'
    converted = ok(call(host,host.owner,'tasks_update',{'task_id':email['id'],'changes':{'task_type':'story','email_conversation_id':None,'overview':'Broader relationship'}},revision=email['revision']))['task']
    assert converted['id']==email['id'] and converted['task_type']=='story' and converted['email_conversation_id'] is None


def test_global_email_binding_and_unreadable_source(host):
    seed_email(host, None)
    email = task(host, 'Global conversation', ctx=host.owner_global, task_type='email', email_conversation_id='conversation-test')
    assert email['business_id'] is None
    # Global task permission alone never grants access to the owner's mailbox.
    from dataclasses import replace
    unreadable = replace(host.other_context, business_id=None, review_business_id=None)
    result = call(host, unreadable, 'tasks_create', {'title':'Not granted','task_type':'email','email_conversation_id':'conversation-test'})
    assert code(result) in {'forbidden','policy_denied','not_found'}


def test_overview_exposes_type_without_overview_body(host):
    from dataclasses import replace
    story = task(host, 'Prospective client', task_type='story', overview='Private detailed brief', assignee_ids=[host.agent])
    data=ok(call(host,replace(host.agent_context,business_id=None,review_business_id=None),'tasks_overview'))
    entries=[entry for scope in data['scopes'] for key in ('active','snoozed') for entry in scope[key]]
    selected=next(entry for entry in entries if entry['id']==story['id'])
    assert selected['task_type']=='story' and 'overview' not in selected


def test_focus_carries_task_type_without_loading_task_overview(host):
    story = task(host, 'Client story', task_type='story', overview='Full private details')
    card=ok(call(host,host.owner_global,'focus_present',{'source':{'type':'task','id':story['id'],'business_id':host.first}}))['item']
    assert card['resolved_source']['task_type']=='story'
    assert 'overview' not in card['resolved_source']


def test_scoped_task_links_are_bidirectional_and_preserve_ownership(host):
    from dataclasses import replace
    host.owner=replace(host.owner,allowed_business_ids=frozenset({host.first,host.second}))
    host.owner_second=replace(host.owner_second,allowed_business_ids=frozenset({host.first,host.second}))
    story = task(host, 'Client story', task_type='story')
    other = task(host, 'Other business story', ctx=host.owner_second, task_type='story')
    global_task = task(host, 'Global task', ctx=host.owner_global)
    def link(source, target, ctx=host.owner, linked=True):
        return ok(call(host, ctx, 'tasks_update', {'task_id': source['id'], 'changes': {'related_task': {
            'business_id':target['business_id'],'task_id':target['id'],'linked':linked}}}, revision=source['revision']))['task']
    story = link(story, other)
    story = link(story, global_task)
    read = get(host, story['id'])
    assert {x['task_id'] for x in read['related_tasks']} == {other['id'], global_task['id']}
    assert read['parent_id'] is None and read['business_id'] == host.first
    reverse = ok(call(host, host.owner_second, 'tasks_get', {'task_id':other['id']}))['task']
    assert reverse['related_tasks'][0]['task_id'] == story['id']
    assert reverse['related_tasks'][0]['link_source']['business_id'] == host.first
    # Adding the reverse edge is a no-op, not a duplicate.
    assert link(other, story, host.owner_second)['revision'] == other['revision']
    renamed = ok(call(host, host.owner_second, 'tasks_update', {'task_id':other['id'], 'changes':{'title':'Renamed story'}}, revision=other['revision']))['task']
    assert any(x['title']=='Renamed story' for x in get(host,story['id'])['related_tasks'])
    story = ok(call(host, host.owner, 'tasks_update', {'task_id':story['id'],'changes':{'fields':{}}}, revision=story['revision']))['task']
    assert '$task_links' not in story['fields']
    assert len(get(host,story['id'])['related_tasks']) == 2
    story = link(story, renamed, linked=False)
    assert not ok(call(host,host.owner_second,'tasks_get',{'task_id':other['id']}))['task']['related_tasks']
    assert code(call(host,host.owner,'tasks_update',{'task_id':story['id'],'changes':{'related_task':{'business_id':host.first,'task_id':story['id'],'linked':True}}},revision=story['revision']))=='validation_error'
    assert code(call(host,host.owner,'tasks_update',{'task_id':story['id'],'changes':{'fields':{'$task_links':[]}}},revision=story['revision']))=='validation_error'


def test_task_links_do_not_leak_unreadable_scopes(host):
    from dataclasses import replace
    host.owner=replace(host.owner,allowed_business_ids=frozenset({host.first,host.second}))
    story=task(host,'Readable')
    private=task(host,'Other business',ctx=host.owner_second)
    args={'task_id':story['id'],'changes':{'related_task':{'business_id':host.second,'task_id':private['id'],'linked':True}}}
    ok(call(host,host.owner,'tasks_update',args,revision=story['revision']))
    # Removing current owner membership revokes cross-scope reads, including backlinks.
    with host.instance.database.transaction() as uow:
        member=uow.repository('business_memberships',business_id=host.second).get(host.instance.owner)
        uow.repository('business_memberships',business_id=host.second).update(host.instance.owner,{'active':False},expected_revision=member['revision'])
    current=get(host,story['id'])
    assert current['related_tasks']==[]
    assert code(call(host,host.owner,'tasks_update',args,revision=current['revision'])) in {'forbidden','policy_denied'}


def test_agent_links_use_scoped_identity_and_revision_checks(host):
    first=task(host,'First',id='same-task-id',assignee_ids=[host.agent])
    second=task(host,'Second',ctx=host.owner_second,id='same-task-id')
    args={'task_id':first['id'],'changes':{'related_task':{'business_id':host.second,'task_id':second['id'],'linked':True}}}
    updated=ok(call(host,host.agent_context,'tasks_update',args,revision=first['revision']))['task']
    assert updated['revision']>first['revision']
    assert code(call(host,host.agent_context,'tasks_update',args,revision=first['revision']))=='revision_conflict'
    result=ok(call(host,host.agent_context,'tasks_get',{'task_id':first['id']}))['task']
    assert [(x['task_id'],x['business_id']) for x in result['related_tasks']]==[('same-task-id',host.second)]
    # Ordinary status changes and type conversion retain the association.
    updated=ok(call(host,host.agent_context,'tasks_update',{'task_id':first['id'],'changes':{'task_type':'story','status':'working'}},revision=updated['revision']))['task']
    assert updated['business_id']==host.first and updated['parent_id'] is None
    assert ok(call(host,host.agent_context,'tasks_get',{'task_id':first['id']}))['task']['related_tasks'][0]['title']=='Second'
