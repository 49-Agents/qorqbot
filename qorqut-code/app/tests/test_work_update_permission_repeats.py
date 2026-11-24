"""Declaring current work asks each permission question once per (project, operation) when preparing and once when
applying, however many items share that project; a refusal still refuses the whole declaration."""
from collections import Counter
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

from qorqut.agents.activity import AgentActivity
from qorqut_operations.contracts import PolicyDecision, plain

from test_single_source import STAMP, Fixture


class WorkUpdatePermissionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.alpha = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('projects').insert({'id': self.alpha, 'slug': 'alpha', 'name': 'Alpha', 'workspace': 'alpha'})
        self.tasks_in_alpha = [self.work(f'Alpha work {index}') for index in range(3)]
        AgentActivity(SimpleNamespace(application=self.app, database=self.db)).register()
        # Marking the session reviewed reads the agent's native transcript, which is outside what is counted here.
        reviewed = mock.patch.object(AgentActivity, 'mark_reviewed', lambda *args: None)
        reviewed.start()
        self.addCleanup(reviewed.stop)
        self.session, registration = str(uuid4()), str(uuid4())
        with self.db.transaction() as uow:
            uow.connection.execute('INSERT INTO machine_registrations(id,principal_id,workspace,service_url,profile_revision,'
                                   'created_at,updated_at) VALUES(?,?,?,?,1,?,?)',
                                   (registration, self.lead, '/lead-workspace', 'http://service.invalid', STAMP, STAMP))
            uow.connection.execute('INSERT INTO agent_activity(agent_id,session_id,registration_id,runtime) VALUES(?,?,?,?)',
                                   (self.lead, self.session, registration, 'claude-code'))
        self.asked = Counter()
        real = self.app.authorize

        def authorize(context, descriptor, targets, uow, **options):
            self.asked[('write' if uow.writable else 'read', descriptor.name, context.project_id)] += 1
            return real(context, descriptor, targets, uow, **options)
        patch = mock.patch.object(self.app, 'authorize', authorize)
        patch.start()
        self.addCleanup(patch.stop)

    def ctx(self, who):
        ctx = super().ctx(who)
        return ctx if who == self.owner else replace(ctx, allowed_project_ids=frozenset({self.alpha}))

    def work(self, title):
        lead = replace(self.ctx(self.lead), project_id=self.alpha, review_project_id=self.alpha)
        result = self.app.invoke(lead, 'tasks_create', {'title': title}, {'idempotency_key': str(uuid4())})
        self.assertEqual('ok', result.status, plain(result.error))
        return plain(result.data)['task']['id']

    def declare(self, items):
        lead = replace(self.ctx(self.lead), project_id=self.alpha, review_project_id=self.alpha)
        return self.app.invoke(lead, 'agent_work_update',
                               {'session_id': self.session, 'expected_work_revision': 1, 'items': items},
                               {'idempotency_key': str(uuid4())})

    def items(self):
        return [{'project_id': self.alpha, 'task_id': task, 'summary': f'Working on item {index}'}
                for index, task in enumerate(self.tasks_in_alpha)]

    def test_items_in_one_project_ask_each_question_once_per_unit(self):
        self.asked.clear()
        result = self.declare(self.items())
        self.assertEqual('ok', result.status, plain(result.error))
        for unit in ('read', 'write'):
            # Three items in one project: three of each before, one of each now.
            self.assertEqual(1, self.asked[(unit, 'agents_activity_list', self.alpha)], self.asked)
            self.assertEqual(1, self.asked[(unit, 'tasks_get', self.alpha)], self.asked)

    def test_a_refused_question_still_refuses_the_declaration(self):
        real = self.app.policy.resolve

        def resolve(context, descriptor, targets, uow):
            if descriptor.name == 'tasks_get':
                return PolicyDecision('deny', 'test')
            return real(context, descriptor, targets, uow)
        self.app.policy.resolve = resolve
        result = self.declare(self.items())
        self.assertEqual('error', result.status)
        self.assertEqual('policy_denied', plain(result.error)['code'])


if __name__ == '__main__':
    unittest.main()
