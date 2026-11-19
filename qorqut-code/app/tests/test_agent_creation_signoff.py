"""Creating agents is free up to three active workers in all; past that each creation is staged for approval."""
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from qorqut.agents import resolver
from qorqut.agents.global_scope import active_role_count


class AgentCreationSignoffTests(unittest.TestCase):
    def basis(self, workers, *, default='allow', overrides=None):
        profile = NS(data={'role': {'id': 'ordubashy'}, 'tool_policy': {'overrides': overrides or {}}}, revision=7)
        role = NS(manifest={'default_tool_policy': {'agents_create': default, 'tasks_get': 'allow'}})
        agent = NS(profile=lambda *_a, **_k: (profile, None), catalog=NS(get=lambda *_a, **_k: role))
        with patch('qorqut.agents.global_scope.active_role_count', return_value=workers) as count:
            basis = resolver.AgentConfigResolver.policy_basis(agent, NS(principal='p'), NS(connection=None))
        return basis, count

    def mode(self, basis):
        return basis.overrides.get('agents_create', basis.defaults.get('agents_create'))

    def test_creation_is_free_below_three_workers_and_staged_from_the_third(self):
        for workers, expected in ((0, 'allow'), (2, 'allow'), (3, 'signoff'), (11, 'signoff')):
            basis, _ = self.basis(workers)
            self.assertEqual(self.mode(basis), expected, workers)
            self.assertEqual(basis.defaults['tasks_get'], 'allow')
            self.assertEqual(basis.revision, 7)

    def test_an_explicit_allow_override_is_staged_too_and_a_refusal_is_never_loosened(self):
        self.assertEqual(self.mode(self.basis(5, default='deny', overrides={'agents_create': 'allow'})[0]), 'signoff')
        for default, overrides in (('deny', {}), ('allow', {'agents_create': 'deny'}), ('signoff', {})):
            basis, count = self.basis(5, default=default, overrides=overrides)
            self.assertEqual(self.mode(basis), overrides.get('agents_create', default))
            count.assert_not_called()                                     # an agent that cannot create is not counted

    def test_only_active_workers_are_counted(self):
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.executescript('''
            CREATE TABLE principals (id TEXT, kind TEXT, active INTEGER);
            CREATE TABLE agent_profiles (agent_id TEXT, home_business_id TEXT, status TEXT, profile_json TEXT);''')
        for name, status in (('a', 'active'), ('b', 'active'), ('c', 'inactive'), ('d', 'active')):
            connection.execute('INSERT INTO principals VALUES(?,?,1)', (name, 'agent'))
            connection.execute('INSERT INTO agent_profiles VALUES(?,NULL,?,?)', (name, status, name))
        roles = {'a': 'global-worker', 'b': 'global-worker', 'd': 'ordubashy'}
        with patch('qorqut.agents.global_scope.global_profile_data',
                   side_effect=lambda row: {'role': {'id': roles[row['agent_id']]}}):
            self.assertEqual(active_role_count(connection, 'global-worker'), 2)


if __name__ == '__main__':
    unittest.main()
