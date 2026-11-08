import json
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from qorqut_agentic.behavior.tool_help import ToolInstructions, defaults
from bazarlyk.transport import mcp


class Registry:
    def __init__(self, frozen=True):
        self.frozen = frozen
        self.descriptor = SimpleNamespace(name='sample', description='Example', effect_kind='read',
            external_read=False, input_schema={'type': 'object', 'properties': {}},
            transport_only=False, alias_bindings={})

    def get(self, name):
        if name not in ('sample', 'alias'):
            raise KeyError(name)
        return self.descriptor


class View:
    def __init__(self):
        self.messages = dict(defaults())
        self.local = threading.local()
        self.snapshots = 0

    @contextmanager
    def read_snapshot(self):
        self.snapshots += 1
        self.local.active = True
        try:
            yield
        finally:
            self.local.active = False

    def tool_instructions(self):
        assert self.local.active
        return ToolInstructions(json.dumps({'format': 1, 'messages': self.messages}))


class Caller:
    def __init__(self, view):
        self.view = view

    def inventory(self, actor):
        assert self.view.local.active
        return actor


def row(available=True, aliases=('alias',)):
    return {'canonical_operation': 'sample', 'available': available, 'exposed_aliases': aliases}


class DiscoveryCacheTests(unittest.TestCase):
    def setUp(self):
        self.view = View()
        self.registry = Registry()
        resolver = SimpleNamespace(require_agentic_view=lambda: self.view)
        self.runtime = SimpleNamespace(registry=self.registry, resolver=resolver)
        self.transport = mcp.MCPTransport(self.runtime, None, None, caller=Caller(self.view))

    def test_cache_filters_copies_and_rebuilds(self):
        with patch.object(mcp, 'tool_schema', wraps=mcp.tool_schema) as schema:
            first = self.transport._discover_tools([row()])
            original = [tool.model_dump() for tool in first]
            first[0].inputSchema['properties']['damage'] = {}
            first[0].annotations.readOnlyHint = False
            self.assertEqual([tool.model_dump() for tool in self.transport._discover_tools([row()])], original)
            self.assertEqual(schema.call_count, 2)
            self.assertEqual(self.transport._discover_tools([row(False)]), [])
            self.assertEqual([x.name for x in self.transport._discover_tools([row(aliases=())])], ['sample'])
            self.assertEqual(schema.call_count, 2)
            key = next(iter(self.view.messages))
            self.view.messages[key] += ' changed'
            self.transport._discover_tools([row()])
            self.assertEqual(schema.call_count, 4)
            self.runtime.registry = Registry()
            self.transport._discover_tools([row()])
            self.assertEqual(schema.call_count, 6)
            self.runtime.registry = Registry(frozen=False)
            self.transport._discover_tools([row()])
            self.transport._discover_tools([row()])
            self.assertEqual(schema.call_count, 10)
        self.assertEqual(self.view.snapshots, 8)

    def test_invalid_instructions_fail_even_when_warm(self):
        self.transport._discover_tools([row()])
        self.view.messages.clear()
        with self.assertRaises(ValueError):
            self.transport._discover_tools([row()])

    def test_older_reader_cannot_replace_newer_generation(self):
        entered, resume = threading.Event(), threading.Event()
        original = self.view.tool_instructions
        old_messages = dict(self.view.messages)
        def instructions():
            if threading.current_thread().name == 'old':
                entered.set()
                self.assertTrue(resume.wait(5))
                return ToolInstructions(json.dumps({'format': 1, 'messages': old_messages}))
            return original()
        self.view.tool_instructions = instructions
        errors = []
        def old_reader():
            try:
                self.transport._discover_tools([row()])
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=old_reader, name='old')
        thread.start()
        self.assertTrue(entered.wait(5))
        self.view.messages[next(iter(self.view.messages))] += ' new'
        with patch.object(mcp, 'tool_schema', wraps=mcp.tool_schema) as schema:
            self.transport._discover_tools([row()])
            resume.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            count = schema.call_count
            self.transport._discover_tools([row()])
            self.assertEqual(schema.call_count, count)


class RegisteredCallerTests(unittest.TestCase):
    def test_inventory_and_batch_skip_preparation(self):
        calls = []
        rows = [{'canonical_operation': mcp.BATCH_NAME, 'exposed_aliases': [],
                 'available': True, 'effective_mode': 'allow'}]
        def resolve(principal, *, include_preparation=True):
            calls.append((principal, include_preparation))
            return SimpleNamespace(data={'tools': rows})
        caller = mcp.RegisteredAgentCaller(SimpleNamespace(
            resolver=SimpleNamespace(resolve_agent_config=resolve)), None)
        actor = SimpleNamespace(principal='principal', delegation_assignment_id=None)
        self.assertEqual(caller.inventory(actor), rows)
        self.assertIs(caller.check_batch(actor), actor)
        self.assertEqual(calls, [('principal', False), ('principal', False)])


if __name__ == '__main__':
    unittest.main()
