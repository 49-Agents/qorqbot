"""Advisory IO stays off the event loop and cannot fail committed mutations."""
import asyncio
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from qorqut.transport import mcp
from qorqut_operations.contracts import DomainError
import test_mcp_phase_timing as fixtures


class AdvisoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_reader_cannot_block_the_event_loop(self):
        loop_thread = threading.get_ident()
        entered, release = threading.Event(), threading.Event()
        value = {'status': 'error', 'error': {'code': 'validation_error', 'message': 'Original'}}
        def read():
            self.assertNotEqual(threading.get_ident(), loop_thread)
            entered.set()
            if not release.wait(5):
                raise AssertionError('event loop did not release the reader')
            return NS(render=lambda key, **values: values['VALUE0'])
        pending = asyncio.create_task(mcp.validation_hint(value, 'todo_add', read_instructions=read))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            self.assertFalse(pending.done())
            release.set()
            result = await pending
        finally:
            release.set()
            await pending
        self.assertEqual(result['error'], value['error'])
        self.assertEqual(result['agent_hint'], '["todo_add"]')

    async def test_validation_hint_failure_preserves_original_error_and_correlation(self):
        value = {'status': 'error', 'error': {'code': 'validation_error'}, 'request_id': 'correlation'}
        read = Mock(side_effect=DomainError('agentic_unavailable', 'Recovery required'))
        self.assertIs(await mcp.validation_hint(value, 'todo_add', read_instructions=read), value)
        read.assert_called_once()
        for result, operation in [({'status': 'ok'}, 'todo_add'), (value, None),
                                  ({**value, 'agent_hint': 'existing'}, 'todo_add')]:
            read.reset_mock()
            self.assertIs(await mcp.validation_hint(result, operation, read_instructions=read), result)
            read.assert_not_called()

    async def test_mutation_guidance_runs_after_success_and_failure_does_not_change_receipt(self):
        clock = fixtures.Clock()
        names = ('tasks_update', 'tasks_complete', 'attention_add', 'todo_add', 'attention_focus_set')
        delays = {name: {'data': {'revision': 2}} for name in names}
        application = fixtures.Application(clock, delays, {})
        def advisory(_ctx, _args, name):
            self.assertEqual(application.invoked[-1], name)
            return 'Current guidance'
        hints = NS(task_update_hint=Mock(side_effect=advisory), mutation_hint=Mock(side_effect=advisory))
        application.ordubashy_attention = hints
        transport = mcp.MCPTransport(NS(application=application,
            registry=fixtures.Registry(clock, delays, {})), None, None, caller=fixtures.Caller(clock, delays))
        for name in names:
            with self.subTest(operation=name):
                result = await transport._single_result(name, {}, fixtures.ACTOR)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['agent_hint'], 'Current guidance')
                selected = hints.task_update_hint if name.startswith('tasks_') else hints.mutation_hint
                selected.side_effect = DomainError('agentic_unavailable', 'Recovery required')
                result = await transport._single_result(name, {}, fixtures.ACTOR)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['data'], {'revision': 2})
                self.assertNotIn('agent_hint', result)
                selected.side_effect = advisory

