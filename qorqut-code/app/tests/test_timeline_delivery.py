"""Timeline delivery: a Task read carries the state of the Project, each parent Task and the Task, once per conversation.
Global has no timeline: nothing of it is sent, whatever an earlier filing left in the store."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from unittest.mock import PropertyMock, patch
from uuid import uuid4

from qorqut_operations.contracts import Principal, plain
from qorqut.agents.resolver import AgentConfigResolver as AgentResolver
from qorqut_operations.timeline import filing
from qorqut.domains.timeline import Timeline
from qorqut.domains.timeline_delivery import TimelineDelivery
from qorqut.transport import mcp

from test_single_source import Fixture

DAY = '2026-11-13T10:00:00.000000Z'
GLOBAL = {'type': 'global', 'id': 'global', 'business_id': None}
OLD = 'Reply to the owner in plain words.'
FIRST, SECOND = str(uuid4()), str(uuid4())


class Headers:
    def __init__(self, **values):
        self.values = values

    def getlist(self, name):
        return [self.values[name]] if name in self.values else []

    def get(self, name, default=None):
        return self.values.get(name, default)


class Caller(mcp.RegisteredAgentCaller):
    """The agent route's caller with its gates open: these tests are about what the read carries."""

    def __init__(self, ctx):
        self.ctx = ctx

    exposes = lambda self, descriptor: True
    check_operation = validate_options = validate_arguments = lambda self, *a: None
    invocation_context = lambda self, actor, **values: self.ctx


class DeliveryTests(Fixture):
    def setUp(self):
        super().setUp()
        Timeline(NS(application=self.app, registry=self.registry)).register()
        self.delivery = TimelineDelivery(NS(database=self.db))
        self.business = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('businesses').insert({'id': self.business, 'name': 'Example project', 'slug': 'example',
                                                 'workspace': 'example', 'hq_path': 'hq'})
        self.clock = ['2026-11-13T14:02:00.000000Z']
        for target, value in (('qorqut.domains.timeline.timestamp', lambda: DAY),
                              ('qorqut.domains.timeline_delivery.timestamp', lambda: self.clock[0])):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.headers, self.meta = Headers(), {}
        self.transport = mcp.MCPTransport(NS(application=self.app, registry=self.registry, timeline_delivery=self.delivery,
                                             database=self.db), None, None, caller=Caller(self.ctx(self.worker)))
        patcher = patch.object(type(self.transport.server), 'request_context', new_callable=PropertyMock,
                               return_value=NS(request=NS(headers=self.headers, scope={}), meta=self.meta))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ctx(self, who):
        return replace(super().ctx(who), allowed_business_ids=frozenset({self.business}))

    def conversation(self, session=FIRST):
        self.headers.values = {'x-qorqut-native-session': session} if session else {}

    def file(self, item, kind, text):
        return self.ok(self.app.invoke(self.ctx(self.lead), 'timeline_file', {
            'scope': 'global', 'item': item, 'changes': [{'action': 'add', 'kind': kind, 'text': text}]},
            {'idempotency_key': str(uuid4())}))

    def old_global(self, text=OLD):
        """A row filed on Global before it lost its timeline, written as the store layer still accepts it."""
        with self.db.transaction() as uow:
            filing.write(uow.connection, GLOBAL, [{'action': 'add', 'kind': 'decision', 'text': text}], [], created_by=self.lead, at=DAY)

    def item(self, task):
        return {'type': 'task', 'id': task['id'], 'business_id': None}

    def tasks_get(self, task_id):
        """The agent route's tasks_get call as the model receives it (the result data)."""
        value = asyncio.run(self.transport._single_result_timed(
            'tasks_get', {'scope': 'global', 'task_id': task_id}, NS(principal=Principal(self.worker, 'agent')), 'request', {}))
        return plain(value)

    def reread(self, task_id, token=''):
        """A tasks_get with ``if_read_token``: the whole result, whose ``read_state`` carries the token to echo next time."""
        value = asyncio.run(self.transport._single_result_timed(
            'tasks_get', {'scope': 'global', 'task_id': task_id, 'if_read_token': token},
            NS(principal=Principal(self.worker, 'agent'), credential_id='credential', registration_id='registration'),
            'request', {}))
        return plain(value)

    def chain(self, task_id, **more):
        value = self.tasks_get(task_id)
        self.assertEqual(value['status'], 'ok', value)
        return value['data'].get('timeline')

    def rows(self, sql, *values):
        with self.db.read() as uow:
            return [tuple(row) for row in uow.connection.execute(sql, values).fetchall()]

    def recorded(self):
        return self.rows('SELECT conversation_id, item_type, business_id, revision FROM timeline_deliveries ORDER BY item_type, item_id')

    # -- what a Task read carries ---------------------------------------------------------------------------------

    def test_a_task_read_carries_the_chain_and_the_second_read_shows_unchanged_lines(self):
        parent = self.task(self.lead, 'Pay creators')
        child = self.task(self.lead, 'Pay October creators', parent_id=parent['id'])
        self.file(self.item(parent), 'fact', 'The hold on creator R. was cleared.')
        self.file(self.item(child), 'progress', '9 of 14 paid.')
        self.conversation()
        first = self.chain(child['id'])
        self.assertEqual(first.split('\n\n'), [
            'Task "Pay creators" — what holds now (revision 1)\nFacts:\n- [%s] The hold on creator R. was cleared. (13 Nov)\n'
            'Further reading: 1 subtask (timeline_get with history)' % self.entry(self.item(parent)),
            'Task "Pay October creators" — what holds now (revision 2)\nProgress: 9 of 14 paid. (13 Nov)'])
        self.assertEqual(len(self.recorded()), 2)
        self.assertEqual(self.chain(child['id']).split('\n\n'), [
            'Task "Pay creators": unchanged since 14:02; timeline_get reads it again',
            'Task "Pay October creators": unchanged since 14:02; timeline_get reads it again'])

    def entry(self, item):
        return self.rows('SELECT id FROM timeline_entries WHERE item_type=? AND item_id=?', item['type'], item['id'])[0][0]

    def test_a_level_without_entries_or_questions_is_its_title_only_and_needs_no_record(self):
        task = self.task(self.lead, 'Plain work')
        self.conversation()
        self.assertEqual(self.chain(task['id']), 'Task "Plain work"')
        self.assertEqual(self.recorded(), [])

    def test_a_level_that_changed_since_is_sent_again_in_full(self):
        parent = self.task(self.lead, 'Pay creators')
        child = self.task(self.lead, 'Pay October creators', parent_id=parent['id'])
        self.file(self.item(parent), 'decision', 'One provider only.')
        self.conversation()
        self.chain(child['id'])
        self.clock[0] = '2026-11-13T15:30:00.000000Z'
        self.file(self.item(parent), 'decision', 'Pay on the 20th.')
        again = self.chain(child['id']).split('\n\n')
        self.assertEqual(len(again), 2)
        self.assertIn('Pay on the 20th.', again[0])
        self.assertIn('One provider only.', again[0])
        self.assertEqual(self.chain(child['id']).split('\n\n')[0],
                         'Task "Pay creators": unchanged since 15:30; timeline_get reads it again')

    def test_every_conversation_of_an_agent_is_sent_its_own_copy(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation(FIRST)
        self.chain(task['id'])
        self.conversation(SECOND)
        self.assertIn('what holds now', self.chain(task['id']))
        self.assertEqual({row[0] for row in self.recorded()}, {FIRST, SECOND})

    def test_an_item_whose_entries_were_all_removed_says_nothing_holds_now(self):
        task = self.task(self.lead, 'Pay creators')
        gone = self.file(self.item(task), 'fact', 'The hold stands.')['filed']['entries'][0]
        self.conversation()
        self.chain(task['id'])
        self.ok(self.app.invoke(self.ctx(self.lead), 'timeline_file', {'scope': 'global', 'item': self.item(task),
                'changes': [{'action': 'remove', 'entry_id': gone}]}, {'idempotency_key': str(uuid4())}))
        self.assertEqual(self.chain(task['id']), 'Task "Pay creators" — nothing holds now (revision 2)')

    def test_a_project_task_carries_the_project_level_then_each_parent_root_first_then_the_task(self):
        project = {'type': 'project', 'id': self.business, 'business_id': self.business}
        self.file(project, 'decision', 'Reusable cards for every shop.')
        levels = self.delivery.levels({'id': 'x', 'business_id': self.business, 'parent_id': None, 'title': 'Cards'}, self.read_uow())
        self.assertEqual([(item['type'], title) for item, title in levels], [('project', 'Example project'), ('task', 'Cards')])

        def inside(title, **extra):
            made = self.app.invoke(replace(self.ctx(self.lead), business_id=self.business), 'tasks_create', {'title': title, **extra},
                                   {'idempotency_key': str(uuid4())})
            return self.ok(made)['task']
        root = inside('Root')
        middle = inside('Middle', parent_id=root['id'])
        levels = self.delivery.levels({'id': 'x', 'business_id': self.business, 'parent_id': middle['id'], 'title': 'Leaf'}, self.read_uow())
        self.assertEqual([(item['type'], title) for item, title in levels],
                         [('project', 'Example project'), ('task', 'Root'), ('task', 'Middle'), ('task', 'Leaf')])

    def test_a_global_task_carries_each_parent_root_first_then_the_task_and_no_project_level(self):
        root = self.task(self.lead, 'Root')
        middle = self.task(self.lead, 'Middle', parent_id=root['id'])
        task = {'id': 'x', 'business_id': None, 'parent_id': middle['id'], 'title': 'Leaf'}
        levels = self.delivery.levels(task, self.read_uow())
        self.assertEqual([(item['type'], title) for item, title in levels], [('task', 'Root'), ('task', 'Middle'), ('task', 'Leaf')])
        self.assertEqual([item['business_id'] for item, title in levels], [None, None, None])
        alone = self.delivery.levels({'id': 'y', 'business_id': None, 'parent_id': None, 'title': 'Alone'}, self.read_uow())
        self.assertEqual([(item['type'], title) for item, title in alone], [('task', 'Alone')])

    def read_uow(self):
        manager = self.db.read()
        self.addCleanup(manager.__exit__, None, None, None)
        return manager.__enter__()

    # -- who is talking --------------------------------------------------------------------------------------------

    def test_an_unknown_conversation_gets_everything_and_nothing_is_recorded(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation(None)
        for _ in range(2):
            self.assertIn('what holds now', self.chain(task['id']))
        self.assertEqual(self.recorded(), [])
        self.headers.values = {'x-qorqut-native-session': 'not-a-session'}
        self.assertIn('what holds now', self.chain(task['id']))
        self.assertEqual(self.recorded(), [])

    def test_the_conversation_is_the_bridge_session_or_the_codex_thread_and_never_refused(self):
        self.assertEqual(mcp.conversation_id(Headers(**{'x-qorqut-native-session': FIRST}), {}), FIRST)
        self.assertEqual(mcp.conversation_id(Headers(), {'threadId': SECOND}), SECOND)
        self.assertEqual(mcp.conversation_id(Headers(**{'x-qorqut-native-session': FIRST}), NS(model_dump=lambda: {'threadId': SECOND})), FIRST)
        self.assertIsNone(mcp.conversation_id(Headers(), {}))
        self.assertIsNone(mcp.conversation_id(Headers(**{'x-qorqut-native-session': 'x'}), {'threadId': 5}))

    def test_a_codex_thread_is_the_conversation_when_the_bridge_names_none(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation(None)
        self.meta['threadId'] = SECOND
        self.chain(task['id'])
        self.assertEqual({row[0] for row in self.recorded()}, {SECOND})

    def test_a_failed_read_records_nothing_and_other_reads_of_a_task_stay_unchanged(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation()
        self.assertEqual(self.tasks_get('no-such-task')['status'], 'error')
        self.assertEqual(self.recorded(), [])
        # The ordinary read, as the dashboard and internal callers make it, has no chain.
        self.assertNotIn('timeline', self.ok(self.invoke(self.lead, 'tasks_get', {'task_id': task['id']})))

    # -- reset ------------------------------------------------------------------------------------------------------

    def test_a_new_context_resets_the_record_and_a_resume_or_prompt_keeps_it(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation()
        for event, last, resets in (('startup', 'c', True), ('clear', 'c', True), ('compact', 'c', True), ('fork', 'c', True),
                                    ('prompt', None, True), ('resume', 'c', False), ('prompt', 'c', False)):
            with self.subTest(event=event, last=last):
                self.chain(task['id'])
                self.assertEqual(len(self.recorded()), 1)
                self.delivery.start(self.worker, FIRST, event, last)
                self.assertEqual(len(self.recorded()), 0 if resets else 1)
                self.assertEqual('what holds now' in self.chain(task['id']), resets or not self.recorded())
                self.delivery.reset(self.worker, FIRST)

    def test_a_reset_leaves_other_conversations_and_agents_alone(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation(FIRST)
        self.chain(task['id'])
        self.conversation(SECOND)
        self.chain(task['id'])
        self.delivery.start(self.worker, FIRST, 'compact', 'c')
        self.delivery.start(self.lead, SECOND, 'compact', 'c')
        self.assertEqual({row[0] for row in self.recorded()}, {SECOND})

    # -- Global has no timeline -------------------------------------------------------------------------------------

    def test_an_old_global_row_is_not_sent_in_a_chain_and_is_never_recorded(self):
        self.old_global()
        task = self.task(self.lead, 'Pay creators')
        self.conversation()
        self.assertEqual(self.chain(task['id']), 'Task "Pay creators"')
        self.assertEqual(self.recorded(), [])
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        text = self.chain(task['id'])
        self.assertNotIn(OLD, text)
        self.assertNotIn('Global', text)
        self.assertEqual({row[1] for row in self.recorded()}, {'task'})
        self.assertFalse(hasattr(TimelineDelivery, 'compose') or hasattr(TimelineDelivery, 'brief'))
        self.assertEqual(self.rows('SELECT COUNT(*) FROM timeline_entries WHERE item_type=?', 'global'), [(1,)])

    def test_an_old_global_row_is_not_in_the_start_up_briefing(self):
        self.old_global()
        self.conversation()
        for event, last in (('startup', None), ('resume', 'config-1'), ('compact', 'config-1')):
            with self.subTest(event=event):
                result = self.bootstrap(event, last)
                self.assertEqual(result['work_briefing']['rendered'], 'Briefing.')
        self.assertEqual(self.recorded(), [])

    # -- the start hook's report ------------------------------------------------------------------------------------

    def bootstrap(self, event, last='config-1', session=FIRST):
        """``AgentResolver.bootstrap`` for a ready agent, with the configuration and the briefing stood in."""
        data = {'preparation': {'ready': True, 'runtimes': {'codex': {'ready': True}}}, 'context_refresh_required': False, 'tools': [],
                'agentic_catalog_revision': 'r', 'mandatory_context': {'rendered': 'Mandatory.'}}
        config = NS(effective_config_id='config-1', to_dict=lambda: data)
        resolver = NS(resolve_agent_config=lambda *a, **k: config, briefing=lambda *a: {'rendered': 'Briefing.'},
                      timeline_delivery=self.delivery)
        with patch('qorqut.bootstrap.skill_refresh.refresh', return_value=False):
            return AgentResolver._bootstrap(resolver, Principal(self.worker, 'agent'), registration_id='reg', session_id=session,
                                            event=event, last_effective_config_id=last, runtime='codex')

    def test_the_start_hook_resets_on_a_new_context_and_adds_no_timeline_to_the_work_briefing(self):
        self.old_global()
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation()
        briefing = self.bootstrap('startup', None)['work_briefing']['rendered']
        self.assertEqual(briefing, 'Briefing.')
        self.assertIn('The hold was cleared.', self.chain(task['id']))
        for event, last, kept in (('prompt', 'config-1', True), ('resume', 'config-1', True), ('compact', 'config-1', False),
                                  ('fork', 'config-1', False), ('clear', 'config-1', False), ('prompt', None, False)):
            with self.subTest(event=event, last=last):
                self.chain(task['id'])
                result = self.bootstrap(event, last)
                # A resume sends its context again but keeps the record; the briefing never carries a timeline.
                self.assertEqual(len(self.recorded()), 1 if kept else 0, self.recorded())
                self.assertNotIn(OLD, result.get('work_briefing', {}).get('rendered', ''))
                self.delivery.reset(self.worker, FIRST)

    def test_a_refused_start_does_not_reset(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation()
        self.chain(task['id'])
        with patch('qorqut.bootstrap.skill_refresh.refresh', side_effect=OSError):
            data = {'preparation': {'ready': False, 'runtimes': {}, 'projection_drift': []}}
            resolver = NS(resolve_agent_config=lambda *a, **k: NS(effective_config_id='c', to_dict=lambda: data),
                          timeline_delivery=self.delivery)
            result = AgentResolver._bootstrap(resolver, Principal(self.worker, 'agent'), registration_id='reg', session_id=FIRST,
                                              event='compact', last_effective_config_id=None, runtime='codex')
        self.assertFalse(result['ready'])
        self.assertEqual(len(self.recorded()), 1)

    # -- a re-read with a read token -----------------------------------------------------------------------------------

    def test_a_not_modified_reread_carries_the_chain_only_when_a_level_changed_for_the_conversation(self):
        parent = self.task(self.lead, 'Pay creators')
        child = self.task(self.lead, 'Pay October creators', parent_id=parent['id'])
        self.file(self.item(parent), 'decision', 'One provider only.')
        self.file(self.item(child), 'progress', '9 of 14 paid.')
        self.conversation()
        first = self.reread(child['id'])
        token = first['read_state']['token']
        self.assertIn('what holds now', first['data']['timeline'])
        # Every level was sent and nothing changed: the short result stays short.
        second = self.reread(child['id'], token)
        self.assertEqual(second['data'], {'not_modified': True})
        # The Task is unchanged, but a decision filed on its parent since is told, the rest as one-line markers.
        self.clock[0] = '2026-11-13T15:30:00.000000Z'
        self.file(self.item(parent), 'decision', 'Pay on the 20th.')
        third = self.reread(child['id'], token)
        self.assertTrue(third['data']['not_modified'])
        lines = third['data']['timeline'].split('\n\n')
        self.assertEqual(len(lines), 2)
        self.assertIn('Pay on the 20th.', lines[0])
        self.assertTrue(lines[1].startswith('Task "Pay October creators": unchanged since 14:02'))
        # It was recorded, so the next re-read is short again.
        self.assertEqual(self.reread(child['id'], token)['data'], {'not_modified': True})

    def test_a_not_modified_reread_from_an_unknown_conversation_carries_the_full_chain(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation(None)
        token = self.reread(task['id'])['read_state']['token']
        again = self.reread(task['id'], token)
        self.assertTrue(again['data']['not_modified'])
        self.assertIn('The hold was cleared.', again['data']['timeline'])
        self.assertEqual(self.recorded(), [])

    def test_a_failed_record_still_sends_every_level_in_full(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation()
        with patch.object(TimelineDelivery, 'record', side_effect=RuntimeError('database is locked')), \
                self.assertLogs('qorqut.domains.timeline_delivery', 'ERROR'):
            self.assertIn('The hold was cleared.', self.chain(task['id']))
        self.assertEqual(self.recorded(), [])
        # Nothing was recorded, so the next read sends it in full again.
        self.assertIn('The hold was cleared.', self.chain(task['id']))

    def test_a_chain_that_cannot_be_built_is_dropped_and_the_read_still_succeeds(self):
        task = self.task(self.lead, 'Pay creators')
        self.conversation()
        with patch.object(TimelineDelivery, 'build', side_effect=RuntimeError('broken')), self.assertLogs('qorqut.transport.mcp', 'ERROR'):
            value = self.tasks_get(task['id'])
        self.assertEqual(value['status'], 'ok')
        self.assertNotIn('timeline', value['data'])

    def test_a_reset_drops_the_agents_rows_older_than_thirty_days(self):
        task = self.task(self.lead, 'Pay creators')
        self.file(self.item(task), 'fact', 'The hold was cleared.')
        self.conversation(SECOND)
        self.chain(task['id'])
        self.clock[0] = '2026-12-20T09:00:00.000000Z'
        self.conversation(FIRST)
        self.chain(task['id'])
        self.delivery.start(self.worker, FIRST, 'startup', None)
        self.assertEqual(self.recorded(), [])
        self.clock[0] = '2026-12-20T09:00:00.000000Z'
        self.chain(task['id'])
        self.clock[0] = '2026-12-25T09:00:00.000000Z'
        self.delivery.start(self.worker, SECOND, 'resume', 'c')
        self.assertEqual(len(self.recorded()), 1)

    def test_a_failing_start_does_not_fail_the_start_hook(self):
        with patch.object(TimelineDelivery, 'start', side_effect=RuntimeError('locked')), self.assertLogs('qorqut.agents.resolver', 'ERROR'):
            result = self.bootstrap('startup', None)
        self.assertTrue(result['ready'])
        self.assertEqual(result['work_briefing']['rendered'], 'Briefing.')

    # -- the three replays ------------------------------------------------------------------------------------------

    def test_replay_a_hold_cleared_by_the_owner_reaches_a_fresh_worker_on_a_sibling_task(self):
        payouts = self.task(self.lead, 'Pay creators for October')
        self.task(self.lead, 'Pay creator Nadia', parent_id=payouts['id'])
        sibling = self.task(self.lead, 'Pay creator Rustam', parent_id=payouts['id'])
        self.file(self.item(payouts), 'fact', 'The payment hold on creator Nadia was cleared by the owner on 13 Nov.')
        # A fresh conversation of the worker opens the sibling payments Task and is told.
        self.conversation(SECOND)
        self.assertIn('The payment hold on creator Nadia was cleared by the owner on 13 Nov.', self.chain(sibling['id']))

    def test_replay_a_reusable_card_decision_filed_on_a_project_survives_a_compaction(self):
        project = {'type': 'project', 'id': self.business, 'business_id': self.business}
        self.file(project, 'decision', 'Shop cards are reusable across seasons.')
        task = {'id': 'cards', 'business_id': self.business, 'parent_id': None, 'title': 'Design cards'}
        sent = self.delivery.chain(self.worker, FIRST, task)
        self.assertIn('Shop cards are reusable across seasons.', sent)
        self.assertIn('Project "Example project": unchanged since 14:02', self.delivery.chain(self.worker, FIRST, task))
        # The conversation is compacted: its start hook reports it, and the next Task read sends the decision in full.
        self.delivery.start(self.worker, FIRST, 'compact', 'config-1')
        self.assertIn('Shop cards are reusable across seasons.', self.delivery.chain(self.worker, FIRST, task))

    def test_replay_an_email_cadence_decision_survives_a_failed_tool_call(self):
        task = self.task(self.lead, 'Send the weekly emails')
        self.file(self.item(task), 'decision', 'Send campaign emails every 20 minutes.')
        self.conversation()
        # The call fails before the model reads anything: nothing may count as delivered.
        failed = self.tasks_get('no-such-task')
        self.assertEqual(failed['status'], 'error')
        with patch.object(self.delivery, 'chain', side_effect=RuntimeError('lost in transit')), self.assertLogs(mcp.logger, 'ERROR'):
            self.assertNotIn('timeline', self.tasks_get(task['id'])['data'])
        self.assertEqual(self.recorded(), [])
        self.assertIn('Send campaign emails every 20 minutes.', self.chain(task['id']))


if __name__ == '__main__':
    unittest.main()
