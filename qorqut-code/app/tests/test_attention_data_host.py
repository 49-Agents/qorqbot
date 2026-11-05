"""Attention operations on one isolated assembled host, without a running service."""
from qorqut_operations.contracts import InvocationOptions, new_id, plain
from pathlib import Path
import yaml
from bazarlyk.agents.attention import register_attention
from host_foundation_support import build_instance, create_registered_global, owner_global_context, select_coordinator, set_agent_overrides, register_sources, publish_role


def test_real_role_grants_match_the_composed_attention_registry(tmp_path):
    host, _ = build_instance(tmp_path)
    try:
        assert publish_role(host, 'ordubashy')
        role = yaml.safe_load((Path(host.workspace) / 'qorqut-roles/agent-roles/roles/ordubashy/role.yaml').read_text())
        names = ('workers_list', 'attention_get', 'attention_add', 'attention_remove', 'attention_focus_set',
                 'attention_focus_clear', 'attention_workflow_update', 'attention_workflow_context', 'attention_workflow_sync')
        for name in names:
            assert role['default_tool_policy'][name] == 'allow'
            assert host.runtime.registry.get(name).name == name
        assert host.runtime.application.ordubashy_attention is not None
        assert host.runtime.workflow_attention is not None
    finally:
        host.runtime.release_instance()
        host.database.close()


def test_owner_board_and_pinned_share_task_without_sharing_membership(tmp_path):
    host, _ = build_instance(tmp_path)
    try:
        register_sources(host, host.business, 'first')
        agent = select_coordinator(host, create_registered_global(host, 'ordubashy'))
        register_attention(host.runtime)
        set_agent_overrides(host, agent, {'tasks_get':'allow', 'focus_get':'allow', 'focus_present':'allow',
            'attention_get':'allow', 'inbox_list':'allow'}, (host.business,))
        owner = owner_global_context(host)
        def call(name, args):
            result = host.runtime.application.invoke(owner, name, {'scope':'global', **args}, InvocationOptions(idempotency_key=new_id()))
            assert result.status == 'ok', result.to_dict()
            return plain(result.data)
        task = call('tasks_create', {'id':'attention-work', 'title':'Attention work'})['task']
        ref = {'type':'task','id':task['id'],'business_id':None}
        pin = call('focus_present', {'source':ref})['item']
        assert pin['task']['id'] == task['id']
        added = call('attention_add', {'task':ref})
        assert added['items'][0]['task']['id'] == task['id']
        focused = call('attention_focus_set', {'task':ref,'focus':ref,'intent':'Find the quote','expected_revision':added['revision']})
        assert focused['attention']['focus']['id'] == task['id']
        item = focused['items'][0]
        removed = call('attention_remove', {'membership_id':item['membership_id'],'expected_membership_revision':item['membership_revision']})
        assert removed['attention']['state'] == 'unfocused' and not removed['items']
        assert call('focus_get', {'item_id':pin['id']})['item']['task']['id'] == task['id']
    finally:
        host.runtime.release_instance()
        host.database.close()


def test_focused_automatic_scan_crosses_unrelated_pages_and_preserves_deferred_events(tmp_path):
    from bazarlyk.native_chat.nudges import HostVisibility
    from qorqut_operations.inbox_nudges.state import PAGE, scan_pending
    host, _ = build_instance(tmp_path)
    try:
        register_sources(host, host.business, 'first')
        agent = select_coordinator(host, create_registered_global(host, 'ordubashy'))
        set_agent_overrides(host, agent, {'tasks_get': 'allow', 'inbox_list': 'allow',
                                          'owner_conversation_get': 'allow'}, (host.business,))
        owner = owner_global_context(host)
        def call(name, args):
            result = host.runtime.application.invoke(owner, name, {'scope': 'global', **args},
                                                     InvocationOptions(idempotency_key=new_id()))
            assert result.status == 'ok', result.to_dict()
            return plain(result.data)
        work = call('tasks_create', {'id': 'work', 'title': 'Work'})['task']
        other = call('tasks_create', {'id': 'other', 'title': 'Other'})['task']
        ref = {'type': 'task', 'id': work['id'], 'business_id': None}
        board = call('attention_add', {'task': ref})
        call('attention_focus_set', {'task': ref, 'focus': ref, 'intent': 'Work', 'expected_revision': board['revision']})
        call('owner_conversation_send', {'source': {'type': 'task', 'id': other['id'], 'business_id': None},
                                         'text': 'Direct owner message'})
        with host.database.transaction() as uow:
            owner_events = [row[0] for row in uow.connection.execute(
                "SELECT e.id FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                "WHERE d.agent_id=? AND e.source_type='owner_conversations' AND e.actor_id=?", (agent, host.owner))]
            assert owner_events
            ids = []
            for source in [other['id']] * (PAGE + 1) + [work['id']]:
                cur = uow.connection.execute("INSERT INTO inbox_events(source_type,source_id,event_type,summary,created_at) "
                                             "VALUES('tasks',?,'changed','Synthetic','2030-01-01T00:00:00Z')", (source,))
                ids.append(cur.lastrowid)
                uow.connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)', (agent, cur.lastrowid))
            visibility = HostVisibility(host.runtime.inbox)
            focused = scan_pending(uow, visibility, agent, budget=PAGE * 2)
            assert ids[-1] in focused.ids and not set(ids[:-1]) & set(focused.ids)
            assert set(owner_events) & set(focused.ids)
            assert uow.connection.execute('SELECT count(*) FROM inbox_deliveries WHERE agent_id=? AND acknowledged_at IS NOT NULL', (agent,)).fetchone()[0] == 0
        current = call('attention_get', {})
        call('attention_focus_clear', {'expected_revision': current['revision']})
        with host.database.read() as uow:
            unfocused = scan_pending(uow, HostVisibility(host.runtime.inbox), agent, budget=PAGE * 2)
            assert ids[0] in unfocused.ids
    finally:
        host.runtime.release_instance()
        host.database.close()
