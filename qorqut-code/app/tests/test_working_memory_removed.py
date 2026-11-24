"""Working memory and the old plan-file tools are removed: declared names stay valid, nothing handles them."""
from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut.catalog.packages import validate_policy
from qorqut.composition.catalog import builtin_registry
from qorqut.domains.agent_context import AgentContext
from qorqut_operations.contracts import DomainError

REMOVED = ('working_memory_get', 'working_memory_update', 'plans_get', 'plans_set', 'plans_list')
AGENT = '11111111-1111-4111-8111-111111111111'


class WorkingMemoryRemovedTests(unittest.TestCase):
    def test_names_stay_declared_but_have_no_handler(self):
        registry = builtin_registry()
        for name in REMOVED:
            with self.subTest(name=name):
                self.assertTrue(registry.known(name))
                self.assertIsNotNone(registry.metadata(name))
                with self.assertRaises(DomainError) as raised:
                    registry.get(name)
                self.assertEqual(raised.exception.code, 'operation_unavailable')
                self.assertNotIn(name, [descriptor.name for descriptor in registry.installed()])

    def test_a_role_permission_map_naming_a_removed_tool_is_still_valid(self):
        validate_policy({name: 'allow' for name in REMOVED}, builtin_registry())

    def test_startup_context_has_no_memory_part(self):
        @contextmanager
        def read():
            yield object()
        app = SimpleNamespace(database=SimpleNamespace(read=read), current_context=lambda ctx, uow: ctx,
                              registry=builtin_registry())
        resolver = SimpleNamespace(invocation_context=lambda principal, project_id=None: SimpleNamespace(project_id=project_id))
        context = AgentContext.__new__(AgentContext)
        context.app, context.resolver = app, resolver
        context.authorize = lambda ctx, operation, uow: None
        context.access = lambda ctx, args, uow: AGENT
        config = {'project_id': None, 'mandatory_context': {'rendered': ''}}
        with mock.patch('qorqut.domains.startup_inventory.StartupInventory') as inventory:
            inventory.return_value.page.return_value = {'attention': []}
            result = context.bootstrap(SimpleNamespace(), config)
        self.assertTrue(result['rendered'])
        text = result['rendered'].lower()
        self.assertNotIn('memory', text)
        self.assertEqual(set(json.loads(result['rendered'].split('\n', 1)[1])), {'agent_id', 'project_id', 'startup_inventory', 'context_hash'})


if __name__ == '__main__':
    unittest.main()
