"""Focused synthetic checks for the attention data boundary."""
from datetime import datetime, timezone
from types import SimpleNamespace
import json
import sqlite3
import unittest
from unittest.mock import patch

from bazarlyk.agents.attention import Attention, task_recipients
from bazarlyk.composition.attention import SCHEMA
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id


class AttentionDataTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE agent_profiles(agent_id TEXT PRIMARY KEY)')
        self.db.execute("INSERT INTO agent_profiles VALUES('agent')")
        for statement in SCHEMA:
            self.db.execute(statement)
        self.uow = SimpleNamespace(connection=self.db, repository=lambda name: SimpleNamespace(get=lambda key: {'owner_principal_id':'owner'}))
        self.agent = 'agent'
        runtime = SimpleNamespace(application=SimpleNamespace(), focus=SimpleNamespace(inbox=SimpleNamespace(clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))))
        self.service = Attention(runtime)
        self.ctx = SimpleNamespace(principal_kind='human', principal_id='owner')
        self.service.context = lambda ctx, args, uow: (ctx, self.agent)
        self.tasks = {}
        self.service.task = lambda ctx, ref, uow: self.tasks[ref['id']]
        self.service.resolve = lambda ctx, agent, ref, uow: {**ref, 'label':'Focus'}
        self.emit = patch('bazarlyk.agents.attention.emit_event')
        self.emit.start()
        self.addCleanup(self.emit.stop)
        self.addCleanup(self.db.close)

    def ref(self, identity):
        return {'type':'task','id':identity,'business_id':None}

    def task(self, identity, status='open', completed=None):
        self.tasks[identity] = {'id':identity, 'created_at':'2026-01-01T00:00:00Z', 'status':status,
            'task_type':'task', 'archived':False, 'revision':1, 'latest_completed_at':completed}
        return self.ref(identity)

    def apply(self, operation, **args):
        return self.service.apply(self.ctx, SimpleNamespace(operation_name=operation, arguments=args), self.uow)

    def test_membership_focus_and_clock_fields(self):
        ref = self.task('one')
        self.assertEqual(self.service.state(self.uow, self.agent)['revision'], 0)
        first = self.apply('attention_add', task=ref, expected_revision=0)
        self.assertEqual(first['revision'], 1)
        self.assertEqual(self.apply('attention_add', task=ref)['revision'], 1)
        focused = self.apply('attention_focus_set', task=ref, focus=ref, intent='  Find quote  ', expected_revision=1)
        self.assertEqual(focused['attention']['intent'], 'Find quote')
        generation = focused['attention']['focus_generation']
        self.service.update_clock(self.uow, self.agent, {'focus_due_at':'2026-01-01T00:10:00Z'})
        edited = self.apply('attention_focus_set', task=ref, focus=ref, intent='Draft reply', expected_revision=2)
        self.assertEqual(edited['attention']['focus_generation'], generation)
        self.assertEqual(self.service.state(self.uow, self.agent)['focus_due_at'], '2026-01-01T00:10:00Z')
        item = edited['items'][0]
        cleared = self.apply('attention_remove', membership_id=item['membership_id'], expected_membership_revision=item['membership_revision'])
        self.assertEqual(cleared['attention']['state'], 'unfocused')
        self.assertEqual(cleared['items'], [])

    def test_clear_quarantines_started_emission(self):
        ref = self.task('one')
        self.apply('attention_add', task=ref)
        self.apply('attention_focus_set', task=ref, focus=ref, intent='Work', expected_revision=1)
        self.db.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,state,route,claim_token,claim_consumer_id,claim_binding_generation,claim_expires_at,emission_started_at,created_at,updated_at) VALUES('rem','agent','focus',1,0,'2026','2026','claimed','hook','token','consumer',1,'2026','2026','2026','2026')")
        self.apply('attention_focus_clear', expected_revision=2)
        self.assertEqual(self.db.execute("SELECT state FROM attention_reminders WHERE id='rem'").fetchone()[0], 'uncertain')

    def test_removing_unrelated_membership_preserves_focus_and_unfocused_deadline(self):
        one, two = self.task('one'), self.task('two')
        self.apply('attention_add', task=one)
        added = self.apply('attention_add', task=two)
        focused = self.apply('attention_focus_set', task=one, focus=one, intent='Work', expected_revision=added['revision'])
        self.db.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,state,created_at,updated_at) VALUES('focus','agent','focus',1,0,'2026','2026','pending','2026','2026')")
        other = next(item for item in focused['items'] if item['task_ref'] == two)
        result = self.apply('attention_remove', membership_id=other['membership_id'], expected_membership_revision=1)
        self.assertEqual(result['attention']['focus_generation'], 1)
        self.assertEqual(self.db.execute("SELECT state FROM attention_reminders WHERE id='focus'").fetchone()[0], 'pending')
        self.apply('attention_focus_clear', expected_revision=result['revision'])
        self.service.update_clock(self.uow, self.agent, {'board_mode':'normal','board_due_at':'2026-01-01T00:15:00Z'})
        item = self.service.read(self.ctx, {}, self.uow)['items'][0]
        self.apply('attention_remove', membership_id=item['membership_id'], expected_membership_revision=1)
        self.assertEqual(self.service.state(self.uow, self.agent)['board_due_at'], '2026-01-01T00:15:00Z')

    def test_workflow_episode_invalidation_is_scoped_to_current_focus(self):
        ref = self.task('one')
        added = self.apply('attention_add', task=ref)
        for run in ('current','other'):
            self.db.execute("INSERT INTO attention_workflows(agent_id,run_id,consumer_id,work_task_json,workflow_ref,workflow_name,updated_at) VALUES('agent',?,'consumer',?,?,'Workflow','2026')", (run,json.dumps(ref),run))
        workflow = {'type':'workflow','id':'current','business_id':None}
        focused = self.apply('attention_focus_set', task=ref, focus=workflow, intent='Work', expected_revision=added['revision'])
        blocked = self.apply('attention_workflow_update', run_id='current', state='blocked', blocker_key='first', reason='Waiting', expected_revision=focused['revision'])
        first_since = self.db.execute("SELECT blocked_since FROM attention_workflows WHERE run_id='current'").fetchone()[0]
        self.db.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,workflow_run_id,blocked_since,due_at,not_before,state,created_at,updated_at) VALUES('detach','agent','workflow_detach',1,0,'current',?,'2026','2026','pending','2026','2026')", (first_since,))
        unrelated = self.apply('attention_workflow_update', run_id='other', state='ready', expected_revision=blocked['revision'])
        self.assertEqual(self.db.execute("SELECT state FROM attention_reminders WHERE id='detach'").fetchone()[0], 'pending')
        repeated = self.apply('attention_workflow_update', run_id='current', state='blocked', blocker_key='first', reason='Still waiting', expected_revision=unrelated['revision'])
        self.assertEqual(self.db.execute("SELECT blocked_since FROM attention_workflows WHERE run_id='current'").fetchone()[0], first_since)
        self.assertEqual(self.db.execute("SELECT state FROM attention_reminders WHERE id='detach'").fetchone()[0], 'pending')
        ready = self.apply('attention_workflow_update', run_id='current', state='ready', expected_revision=repeated['revision'])
        self.assertEqual(self.db.execute("SELECT state FROM attention_reminders WHERE id='detach'").fetchone()[0], 'cancelled')
        self.service.clock = lambda: datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        self.apply('attention_workflow_update', run_id='current', state='blocked', blocker_key='second', reason='New blocker', expected_revision=ready['revision'])
        self.assertNotEqual(self.db.execute("SELECT blocked_since FROM attention_workflows WHERE run_id='current'").fetchone()[0], first_since)
        self.db.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,workflow_run_id,blocked_since,due_at,not_before,state,created_at,updated_at) VALUES('next','agent','workflow_detach',1,1,'current',?,'2026','2026','pending','2026','2026')", (self.db.execute("SELECT blocked_since FROM attention_workflows WHERE run_id='current'").fetchone()[0],))
        self.assertEqual(self.db.execute("SELECT state FROM attention_reminders WHERE id='next'").fetchone()[0], 'pending')

    def test_order_pages_and_done_removal_rule(self):
        for name, status, completed in [('old','done','2026-01-01T01:00:00Z'), ('new','done','2026-01-02T01:00:00Z'), ('open','open',None), ('blocked','blocked',None)]:
            self.apply('attention_add', task=self.task(name, status, completed))
        first = self.service.read(self.ctx, {'limit':2}, self.uow)
        second = self.service.read(self.ctx, {'limit':2, 'cursor':first['next_cursor']}, self.uow)
        self.assertEqual([i['task_ref']['id'] for i in first['items'] + second['items']], ['blocked','open','new','old'])
        self.ctx.principal_kind = 'agent'
        blocked = first['items'][0]
        with self.assertRaises(DomainError): self.apply('attention_remove', membership_id=blocked['membership_id'], expected_membership_revision=1)
        done = second['items'][0]
        with self.assertRaises(DomainError): self.apply('attention_remove', membership_id=done['membership_id'], expected_membership_revision=1)
        self.apply('attention_remove', membership_id=done['membership_id'], expected_membership_revision=1, owner_requested=True, rationale='Owner asked to remove it')

    def test_related_events_are_typed_and_not_project_wide(self):
        self.db.execute('CREATE TABLE relationships(business_id TEXT,from_type TEXT,from_id TEXT,to_type TEXT,to_id TEXT,role TEXT)')
        self.db.execute('CREATE TABLE agent_messages(id TEXT,current_sender_id TEXT,current_recipient_id TEXT,delegation_assignment_id TEXT)')
        self.db.execute('CREATE TABLE operational_assignments(id TEXT,parent_principal_id TEXT,business_id TEXT,scope_json TEXT,links_json TEXT)')
        ref = self.task('one')
        self.apply('attention_add', task=ref)
        self.apply('attention_focus_set', task=ref, focus=ref, intent='Work', expected_revision=1)
        self.db.execute("INSERT INTO relationships VALUES(NULL,'task','one','task','input','blocked_by')")
        self.assertTrue(self.service.related_event(self.uow, self.agent, {'source_type':'tasks','source_id':'input','business_id':None}))
        self.assertFalse(self.service.related_event(self.uow, self.agent, {'source_type':'tasks','source_id':'other','business_id':None}))
        self.db.execute("INSERT INTO operational_assignments VALUES('assignment','agent',NULL,'{}',?)", (json.dumps({'tasks':['one']}),))
        self.assertTrue(self.service.related_event(self.uow, self.agent, {'source_type':'operational_assignments','source_id':'assignment'}))
        self.db.execute("INSERT INTO agent_messages VALUES('msg','owner','agent',NULL)")
        self.assertTrue(self.service.related_event(self.uow, self.agent, {'source_type':'agent_messages','source_id':'msg'}))

    def test_task_recipients_require_current_membership_and_identity(self):
        ref = self.task('one')
        self.apply('attention_add', task=ref)
        with patch('bazarlyk.agents.attention.configured_ordubashy', return_value='agent'):
            task = self.tasks['one']
            self.assertEqual(task_recipients(self.uow, None, task, 'owner'), {'agent'})
            self.assertEqual(task_recipients(self.uow, None, task, 'agent'), set())
            self.assertEqual(task_recipients(self.uow, 'other', task, 'owner'), set())
            self.assertEqual(task_recipients(self.uow, None, {**task, 'created_at':'different'}, 'owner'), set())

    def test_membership_audit_is_append_only_and_records_instruction(self):
        ref = self.task('one', 'done')
        item = self.apply('attention_add', task=ref)['items'][0]
        self.ctx.principal_kind = 'agent'
        self.apply('attention_remove', membership_id=item['membership_id'], expected_membership_revision=1,
            owner_requested=True, rationale='Owner instructed removal')
        rows = self.db.execute('SELECT * FROM attention_membership_events ORDER BY rowid').fetchall()
        self.assertEqual([r['event_type'] for r in rows], ['added', 'removed'])
        self.assertEqual(rows[-1]['rationale'], 'Owner instructed removal')
        with self.assertRaises(sqlite3.IntegrityError): self.db.execute('DELETE FROM attention_membership_events')

    def test_workflow_focus_helper_obeys_generation(self):
        ref = self.task('one')
        self.apply('attention_add', task=ref)
        self.assertEqual(self.service.authorized_task(self.uow, self.ctx, self.agent, ref)['task']['id'], 'one')
        self.assertFalse(self.service.set_focus(self.uow, self.ctx, self.agent, focus=ref, task=ref, intent='Work', expected_focus_generation=2))
        self.assertTrue(self.service.set_focus(self.uow, self.ctx, self.agent, focus=ref, task=ref, intent='Work', expected_focus_generation=0))
        self.assertEqual(self.service.state(self.uow, self.agent)['focus_generation'], 1)

    def test_reminder_requires_current_binding_policy_and_source(self):
        agent = new_id()
        self.db.execute('INSERT INTO agent_profiles VALUES(?)', (agent,))
        self.db.execute('CREATE TABLE native_session_owners(agent_id TEXT,consumer_id TEXT,generation INTEGER,desired_state TEXT,binding_json TEXT)')
        registration, consumer = new_id(), new_id()
        binding = {'registration_id':registration, 'agent_id':agent, 'session_id':consumer, 'workspace':'synthetic'}
        self.db.execute('INSERT INTO native_session_owners VALUES(?,?,?,?,?)', (agent,consumer,3,'running',json.dumps(binding)))
        registration_row = {'principal_id':agent, 'workspace':'synthetic'}
        self.uow.repository = lambda name: SimpleNamespace(get=lambda key: registration_row if name == 'machine_registrations' else {'owner_principal_id':'owner'})
        context = InvocationContext(Principal(agent, 'agent'))
        self.service.inbox.resolver = SimpleNamespace(runtime_context=lambda principal:context)
        self.service.inbox.context = lambda ctx, uow:ctx
        self.service.inbox.session_owner = lambda ctx, consumer, uow: None
        self.service.app.policy = SimpleNamespace(resolve=lambda *args:SimpleNamespace(mode='allow'))
        self.service.app.registry = SimpleNamespace(get=lambda name:name)
        self.service.board_mode = lambda uow, agent_id:'normal'
        with patch('bazarlyk.agents.attention.configured_ordubashy', return_value=agent):
            self.assertTrue(self.service.reminder_eligible(self.uow, agent, consumer_id=consumer, binding_generation=3, focus_generation=0, kind='board_normal'))
            self.assertFalse(self.service.reminder_eligible(self.uow, agent, consumer_id=consumer, binding_generation=2, focus_generation=0, kind='board_normal'))
            self.db.execute("UPDATE native_session_owners SET desired_state='paused'")
            self.assertFalse(self.service.reminder_eligible(self.uow, agent, consumer_id=consumer, binding_generation=3, focus_generation=0, kind='board_normal'))

    def test_global_unique_and_json_constraints(self):
        ref = self.task('one')
        self.apply('attention_add', task=ref)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO attention_memberships VALUES('other','agent',NULL,'one','2026',1,'2026')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE agent_attention SET focus_json='[]' WHERE agent_id='agent'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,due_at,not_before,state,created_at,updated_at) VALUES('bad','agent','workflow_detach',0,0,'2026','2026','pending','2026','2026')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO attention_reminders(id,agent_id,kind,focus_generation,sequence,workflow_run_id,blocked_since,due_at,not_before,state,created_at,updated_at) VALUES('bad','agent','focus',0,0,'run','2026','2026','2026','pending','2026','2026')")


if __name__ == '__main__': unittest.main()
