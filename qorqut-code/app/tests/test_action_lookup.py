"""An Action tool that changes one Action needs only its action_id: the project that stores it is found from the row."""
from dataclasses import replace
import unittest
from uuid import uuid4

from qorqut_operations.actions.lookup import ActionLookup
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, plain

from test_action_task_lookup import Harness

CHANGING = ('actions_correct', 'actions_withdraw', 'actions_comment', 'actions_follow')


class ActionLookupTests(Harness):
    def working_in(self, project):
        return InvocationContext(Principal(self.agent, 'agent'), allowed_project_ids={self.alpha, self.beta},
                                 project_id=project, profile_revision=1, source='mcp')

    def stored_in(self, project):
        """A project-operational Action stored in ``project`` and linked to it, recorded by the agent while working in that project."""
        return self.recorded(self.working_in(project))['action_id']

    def shared(self, *projects):
        return self.recorded(category='project-operational', project_ids=list(projects))['action_id']

    def located(self, ctx, action_id):
        with self.db.read() as uow:
            return ActionLookup()(ctx, {'action_id': action_id}, uow)

    def test_every_changing_operation_takes_the_hook_for_every_caller_source(self):
        for name in CHANGING:
            descriptor = self.registry.get(name)
            self.assertIsInstance(descriptor.locate_scope, ActionLookup, name)
            self.assertTrue(descriptor.locate_all_sources, name)
        for name in ('actions_get', 'actions_list', 'actions_thread_get', 'actions_record'):
            self.assertIsNone(self.registry.get(name).locate_scope, name)

    def test_an_action_stored_in_a_reachable_project_runs_there(self):
        in_alpha, in_beta = self.stored_in(self.alpha), self.stored_in(self.beta)
        self.assertEqual(self.located(self.agent_ctx, in_alpha)[1], 'project:' + self.alpha)
        self.assertEqual(self.located(self.agent_ctx, in_beta)[1], 'project:' + self.beta)

    def test_a_global_action_runs_in_global(self):
        action = self.recorded(category='global-operational')['action_id']
        self.assertEqual(self.located(self.agent_ctx, action)[1], 'global')

    def test_a_shared_global_action_runs_in_global_whichever_project_it_is_shared_with(self):
        action = self.shared(self.alpha, self.beta)
        self.assertEqual(self.located(self.agent_ctx, action)[1], 'global')

    def test_the_selected_project_stands(self):
        in_alpha = self.stored_in(self.alpha)
        arguments, namespace = self.located(self.working_in(self.beta), in_alpha)
        self.assertEqual((arguments, namespace), ({'action_id': in_alpha}, None))

    def test_an_action_the_caller_cannot_see_is_not_found_without_detail(self):
        with self.db.transaction() as uow:
            hidden = str(uuid4())
            uow.repository('action_logs', project_id=self.gamma).insert(dict(
                id=hidden, category='project-operational', action_text='Closed the account.', performed_by=self.agent,
                recorded_by=self.agent, origin='agent', occurred_at='2026-11-10T12:00:00Z', refs_json={},
                created_at='2026-11-10T12:00:00Z', updated_at='2026-11-10T12:00:00Z'))
            uow.connection.execute('INSERT INTO action_projects(action_id,project_id,created_at) VALUES(?,?,?)',
                                   (hidden, self.gamma, '2026-11-10T12:00:00Z'))
        for action_id in (hidden, str(uuid4())):
            with self.assertRaises(DomainError) as raised:
                self.located(self.agent_ctx, action_id)
            self.assertEqual((raised.exception.code, raised.exception.message), ('not_found', 'Action does not exist'))
            self.assertFalse(raised.exception.details)

    def test_an_mcp_agent_without_a_project_changes_an_action_by_its_id(self):
        in_alpha = self.stored_in(self.alpha)
        key = str(uuid4())
        withdrawn = self.call('actions_withdraw', {'action_id': in_alpha}, self.agent_ctx,
                              idempotency_key=key, expected_revision=1)
        self.assertEqual(withdrawn.status, 'ok', withdrawn.to_dict())
        self.assertTrue(withdrawn.data['action']['withdrawn'])
        self.assertEqual(self.receipts(key), ['project:' + self.alpha])

    def test_a_dashboard_caller_without_a_project_changes_an_action_by_its_id(self):
        shared = self.shared(self.alpha)
        key = str(uuid4())
        corrected = self.call('actions_correct', {'action_id': shared, 'text': 'Sent the corrected email.'},
                              replace(self.owner_ctx, source='dashboard'), idempotency_key=key, expected_revision=1)
        self.assertEqual(corrected.status, 'ok', corrected.to_dict())
        self.assertEqual(plain(corrected.data['action'])['text'], 'Sent the corrected email.')
        self.assertEqual(self.receipts(key), ['global'])

    def test_an_unknown_action_is_not_found_for_a_dashboard_caller(self):
        result = self.call('actions_withdraw', {'action_id': str(uuid4())}, replace(self.owner_ctx, source='dashboard'),
                           expected_revision=1)
        self.refused(result, 'not_found', 'Action does not exist')


if __name__ == '__main__':
    unittest.main()
