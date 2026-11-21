"""The references an agent passes to the Todo board, Focus, workflow context and current work name their record by id."""
from dataclasses import replace
import unittest
from uuid import uuid4

from qorqut.agentic.attention import WorkflowAttention
from qorqut.agents.activity import AgentActivity
from qorqut_operations.contracts import InvocationOptions, plain
from types import SimpleNamespace as NS

from test_single_source import Fixture


class AgentReferenceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.alpha, self.beta = sorted(str(uuid4()) for _ in range(2))
        with self.db.transaction() as uow:
            for project, name in ((self.alpha, 'Alpha'), (self.beta, 'Beta')):
                uow.repository('projects').insert({'id': project, 'slug': name.lower(), 'name': name, 'workspace': name.lower()})
        self.reach = frozenset({self.alpha})
        self.in_alpha = self.work(self.alpha, 'Alpha work')
        self.in_beta = self.work(self.beta, 'Beta work')
        self.in_global = self.work(None, 'Global work')
        AgentActivity(NS(application=self.app)).register()
        context = object.__new__(WorkflowAttention)
        context.app = self.app
        context.register()

    def ctx(self, who):
        ctx = super().ctx(who)
        return ctx if who == self.owner else replace(ctx, allowed_project_ids=self.reach)

    def work(self, project, title):
        everywhere = replace(super().ctx(self.lead), allowed_project_ids=frozenset({self.alpha, self.beta}))
        arguments = {'title': title, **({'scope': 'global'} if project is None else {})}
        result = self.app.invoke(replace(everywhere, project_id=project, review_project_id=project), 'tasks_create', arguments,
                                 {'idempotency_key': str(uuid4())})
        return self.ok(result)['task']['id']

    def memberships(self):
        return self.rows('SELECT task_id,task_project_id FROM attention_memberships WHERE agent_id=? ORDER BY task_id', self.worker)

    def put(self, task):
        return self.invoke(self.lead, 'todo_add', {'agent_id': self.worker, 'task': task})

    def located(self, operation, arguments, ctx=None):
        """The arguments the operation's own code sees."""
        descriptor = self.registry.get(operation)
        with self.db.read() as uow:
            return plain(self.app._locate(ctx or self.ctx(self.lead), descriptor, arguments, InvocationOptions(), uow)[1])

    # -- the Todo board -------------------------------------------------------------------------------------------

    def test_a_task_without_a_project_is_added_with_the_project_that_holds_it(self):
        for identity in (self.in_alpha, self.in_global):
            self.ok(self.put({'type': 'task', 'id': identity}))
        self.assertEqual(sorted(self.memberships()), sorted([(self.in_alpha, self.alpha), (self.in_global, None)]))

    def test_the_stored_membership_is_the_same_as_when_the_project_is_stated(self):
        first = self.ok(self.put({'type': 'task', 'id': self.in_alpha}))
        again = self.ok(self.put({'type': 'task', 'id': self.in_alpha, 'project_id': self.alpha}))
        self.assertEqual(again, first)
        self.assertEqual(first['task'], {'type': 'task', 'id': self.in_alpha, 'project_id': self.alpha})
        self.assertEqual(self.memberships(), [(self.in_alpha, self.alpha)])

    def test_a_task_outside_the_reach_is_answered_like_an_unknown_id(self):
        unknown = self.put({'type': 'task', 'id': str(uuid4())})
        self.assertEqual(unknown.status, 'error')
        hidden = self.put({'type': 'task', 'id': self.in_beta})
        self.assertEqual((hidden.status, hidden.error), (unknown.status, unknown.error))
        self.assertEqual(self.memberships(), [])

    def test_a_stated_project_or_global_is_checked_as_given(self):
        self.assertEqual(self.put({'type': 'task', 'id': self.in_alpha, 'project_id': None}).status, 'error')
        self.assertEqual(self.put({'type': 'task', 'id': self.in_global, 'project_id': self.alpha}).status, 'error')
        self.assertEqual(self.memberships(), [])

    def test_focus_and_task_are_completed_each_by_its_own_type(self):
        for operation in ('attention_focus_set', 'attention_workflow_update'):
            self.assertIsNotNone(self.registry.get(operation).complete_references, operation)
        seen = self.located('attention_focus_set', {'scope': 'global', 'agent_id': self.worker, 'expected_revision': 0,
                                                    'focus': {'type': 'task', 'id': self.in_alpha},
                                                    'task': {'type': 'task', 'id': self.in_alpha}})
        self.assertEqual(seen['focus'], seen['task'])
        self.assertEqual(seen['task'], {'type': 'task', 'id': self.in_alpha, 'project_id': self.alpha})
        # A workflow is not looked up: it reads as Global, which is where a workflow association lives.
        seen = self.located('attention_focus_set', {'scope': 'global', 'agent_id': self.worker, 'expected_revision': 0,
                                                    'focus': {'type': 'workflow', 'id': 'run-1'},
                                                    'task': {'type': 'task', 'id': self.in_global}})
        self.assertEqual((seen['focus'], seen['task']), ({'type': 'workflow', 'id': 'run-1', 'project_id': None},
                                                         {'type': 'task', 'id': self.in_global, 'project_id': None}))
        seen = self.located('attention_workflow_update', {'scope': 'global', 'agent_id': self.worker, 'run_id': 'run-1',
            'expected_revision': 1, 'state': 'blocked', 'related_refs': [
                {'type': 'task', 'id': self.in_alpha}, {'type': 'task', 'id': self.in_beta},
                {'type': 'workflow', 'id': 'run-2'}, {'type': 'task', 'id': self.in_global, 'project_id': None}]})
        self.assertEqual([ref['project_id'] for ref in seen['related_refs']], [self.alpha, None, None, None])

    def test_the_aliases_complete_like_the_operations_they_repeat(self):
        self.assertIs(type(self.registry.get('todo_add').complete_references), type(self.registry.get('attention_add').complete_references))
        seen = self.located('todo_add', {'scope': 'global', 'agent_id': self.worker, 'task': {'type': 'task', 'id': self.in_alpha}})
        self.assertEqual(seen['task']['project_id'], self.alpha)

    def test_the_workflow_context_task_is_completed_and_the_signed_context_keeps_the_full_reference(self):
        arguments = {'scope': 'global', 'operation': 'workflows.start', 'operation_id': 'op', 'consumer_id': 'c', 'binding_generation': 1}
        seen = self.located('attention_workflow_context', {**arguments, 'task': {'type': 'task', 'id': self.in_alpha}})
        self.assertEqual(seen['task'], {'type': 'task', 'id': self.in_alpha, 'project_id': self.alpha})
        sync = self.registry.get('attention_workflow_sync').input_schema
        signed = sync['properties']['context']['properties']['payload']['properties']['task']['anyOf'][0]
        self.assertEqual(tuple(signed['required']), ('type', 'id', 'project_id'))

    # -- current work ---------------------------------------------------------------------------------------------

    def test_a_work_item_names_its_task_by_id_and_a_free_text_item_is_global(self):
        items = [{'task_id': self.in_alpha}, {'task_id': self.in_beta}, {'summary': 'Reading mail'},
                 {'task_id': self.in_global, 'project_id': None}, {'task_id': self.in_alpha, 'project_id': self.alpha}]
        base = {'agent_id': str(uuid4()), 'session_id': str(uuid4()), 'expected_work_revision': 0}
        seen = self.located('agent_work_update', {**base, 'items': items})
        self.assertEqual([item.get('project_id', 'absent') for item in seen['items']], [self.alpha, None, None, None, self.alpha])
        patched = self.located('agent_work_update', {**base, 'patch': [{'action': 'add', 'item': {'task_id': self.in_alpha}},
                                                                       {'action': 'remove', 'item_key': '0' * 16}]})
        self.assertEqual(patched['patch'][0]['item'], {'task_id': self.in_alpha, 'project_id': self.alpha})
        self.assertEqual(patched['patch'][1], {'action': 'remove', 'item_key': '0' * 16})


if __name__ == '__main__':
    unittest.main()
