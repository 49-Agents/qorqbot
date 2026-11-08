import asyncio
import re
import threading
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from qorqut_operations.contracts import DomainError, OperationResult
from bazarlyk.transport import mcp

ACTOR = SimpleNamespace(principal=SimpleNamespace(id='agent', kind='agent'))
FIELDS = {'operation', 'request_id', 'configuration_ms', 'execution_ms', 'total_tool_ms'}


class Clock:
    """Controlled perf_counter: only the stubs advance it, in whole milliseconds."""

    def __init__(self):
        self.ms = 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.ms / 1000

    def advance(self, ms):
        with self.lock:
            self.ms += ms


@dataclass(frozen=True)
class Context:
    request_id: str = ''
    business_id: str = None


class Registry:
    def __init__(self, clock, delays, aliases):
        self.clock, self.delays, self.aliases = clock, delays, aliases
        self.unknown_ms = 0

    def get(self, name):
        canonical = self.aliases.get(name, name)
        if canonical not in self.delays:
            self.clock.advance(self.unknown_ms)
            raise DomainError('operation_unavailable', 'Operation is not installed')
        self.clock.advance(self.delays[canonical].get('lookup', 0))
        return SimpleNamespace(name=canonical, transport_only=False, input_schema={'properties': {}},
            scope_kinds={'business'}, version=1)


class Caller:
    scope = 'agent'

    def __init__(self, clock, delays):
        self.clock, self.delays = clock, delays
        self.configured = []

    def exposes(self, descriptor):
        return True

    def check_operation(self, canonical_name):
        pass

    def validate_options(self, actor, canonical_name, options):
        pass

    def validate_arguments(self, actor, canonical_name, args):
        pass

    def identity(self, actor):
        return ('agent',)

    def invocation_context(self, actor, *, canonical_name, options, native_binding=None):
        self.clock.advance(self.delays[canonical_name].get('config', 0))
        failure = self.delays[canonical_name].get('config_error')
        if failure is not None:
            raise failure
        self.configured.append(canonical_name)
        return Context()


class Application:
    def __init__(self, clock, delays, aliases):
        self.clock, self.delays, self.aliases = clock, delays, aliases
        self.invoked = []
        self.hooks = {}

    def invoke(self, context, name, args, options):
        name = self.aliases.get(name, name)
        self.invoked.append(name)
        hook = self.hooks.get(name)
        if hook is not None:
            hook()
        spec = self.delays[name]
        self.clock.advance(spec.get('execute', 0))
        if spec.get('execute_error') is not None:
            raise spec['execute_error']
        return OperationResult.ok(spec.get('data', {'ok': True}), request_id=context.request_id)


def fields(record):
    return dict(re.findall(r'(\w+)=(\S+)', record.getMessage()))


def slow_records(captured):
    return [record for record in captured.records if 'total_tool_ms' in record.getMessage()]


class PhaseTimingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.delays = {'sample_read': {}, 'slow_a': {}, 'slow_b': {}}
        self.caller = Caller(self.clock, self.delays)
        aliases = {'sample_alias': 'sample_read'}
        self.application = Application(self.clock, self.delays, aliases)
        self.registry = Registry(self.clock, self.delays, aliases)
        runtime = SimpleNamespace(registry=self.registry, application=self.application)
        self.transport = mcp.MCPTransport(runtime, None, None, caller=self.caller)
        patcher = patch.object(mcp, 'time', SimpleNamespace(perf_counter=self.clock))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_fast_call_emits_no_log(self):
        self.delays['sample_read'] = {'lookup': 9, 'config': 40, 'execute': 200}
        with self.assertNoLogs(mcp.logger, 'WARNING'):
            result = await self.transport._single_result('sample_read', {}, ACTOR)
        self.assertEqual(result['status'], 'ok')

    async def test_threshold_is_inclusive(self):
        self.delays['sample_read'] = {'config': 50, 'execute': 200}
        with self.assertLogs(mcp.logger, 'WARNING') as captured:
            await self.transport._single_result('sample_read', {}, ACTOR)
        self.assertEqual(len(slow_records(captured)), 1)

    async def test_slow_success_logs_once_with_phases_and_correlation_id(self):
        self.delays['sample_read'] = {'lookup': 15, 'config': 40, 'execute': 300}
        with self.assertLogs(mcp.logger, 'WARNING') as captured:
            result = await self.transport._single_result('sample_alias', {}, ACTOR)
        self.assertEqual(result['status'], 'ok')
        records = slow_records(captured)
        self.assertEqual(len(records), 1)
        logged = fields(records[0])
        self.assertEqual(set(logged), FIELDS)
        self.assertEqual(logged['request_id'], result['request_id'])
        self.assertEqual(logged['operation'], 'sample_read')
        self.assertEqual(float(logged['configuration_ms']), 40.0)
        self.assertEqual(float(logged['execution_ms']), 300.0)
        # The total also covers work outside both phases, so it exceeds their sum.
        self.assertEqual(float(logged['total_tool_ms']), 355.0)

    async def test_failed_configuration_records_elapsed_time_and_preserves_error(self):
        denied = DomainError('forbidden', 'Not allowed here')
        self.delays['sample_read'] = {'config': 300, 'config_error': denied, 'execute': 500}
        with self.assertLogs(mcp.logger, 'WARNING') as captured:
            result = await self.transport._single_result('sample_read', {}, ACTOR)
        self.assertEqual(self.application.invoked, [])
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error']['code'], 'forbidden')
        self.assertEqual(result['error']['message'], 'Not allowed here')
        records = slow_records(captured)
        self.assertEqual(len(records), 1)
        logged = fields(records[0])
        self.assertEqual(logged['request_id'], result['request_id'])
        self.assertEqual(float(logged['configuration_ms']), 300.0)
        self.assertEqual(float(logged['execution_ms']), 0.0)
        self.assertEqual(float(logged['total_tool_ms']), 300.0)

    async def test_failed_execution_records_elapsed_time_without_exception_text(self):
        self.delays['sample_read'] = {'config': 10, 'execute': 400,
            'execute_error': RuntimeError('SECRET-EXCEPTION-TEXT')}
        with self.assertLogs(mcp.logger, 'WARNING') as captured:
            result = await self.transport._single_result('sample_read', {}, ACTOR)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error']['code'], 'internal_error')
        records = slow_records(captured)
        self.assertEqual(len(records), 1)
        logged = fields(records[0])
        self.assertEqual(logged['request_id'], result['request_id'])
        self.assertEqual(float(logged['configuration_ms']), 10.0)
        self.assertEqual(float(logged['execution_ms']), 400.0)
        self.assertNotIn('SECRET-EXCEPTION-TEXT', records[0].getMessage())
        self.assertIsNone(records[0].exc_info)

    async def test_concurrent_calls_keep_separate_timings(self):
        self.delays['slow_a'] = {'config': 10, 'execute': 5}
        self.delays['slow_b'] = {'config': 20, 'execute': 300}
        entered, release = threading.Event(), threading.Event()
        def hold():
            entered.set()
            self.assertTrue(release.wait(5))
        self.application.hooks['slow_a'] = hold
        loop = asyncio.get_running_loop()
        with self.assertLogs(mcp.logger, 'WARNING') as captured:
            first = asyncio.create_task(self.transport._single_result('slow_a', {}, ACTOR))
            self.assertTrue(await loop.run_in_executor(None, entered.wait, 5))
            second = await self.transport._single_result('slow_b', {}, ACTOR)
            release.set()
            first = await first
        by_request = {fields(record)['request_id']: fields(record) for record in slow_records(captured)}
        self.assertEqual(set(by_request), {first['request_id'], second['request_id']})
        a, b = by_request[first['request_id']], by_request[second['request_id']]
        self.assertEqual((a['operation'], b['operation']), ('slow_a', 'slow_b'))
        self.assertEqual([float(a[key]) for key in ('configuration_ms', 'execution_ms', 'total_tool_ms')],
            [10.0, 325.0, 335.0])
        self.assertEqual([float(b[key]) for key in ('configuration_ms', 'execution_ms', 'total_tool_ms')],
            [20.0, 300.0, 320.0])

    async def test_log_has_no_client_payload_response_or_unknown_name(self):
        self.registry.unknown_ms = 300
        with self.assertLogs(mcp.logger, 'WARNING') as captured:
            result = await self.transport._single_result('SECRET-TOOL-NAME', {'note': 'SECRET-ARGUMENT'}, ACTOR)
        self.assertEqual(result['error']['code'], 'operation_unavailable')
        record, = slow_records(captured)
        logged = fields(record)
        self.assertEqual(set(logged), FIELDS)
        self.assertEqual(logged['operation'], 'unknown')
        self.assertEqual(logged['request_id'], result['request_id'])
        self.assertEqual(float(logged['total_tool_ms']), 300.0)
        self.delays['sample_read'] = {'execute': 300, 'data': {'secret': 'SECRET-RESPONSE'}}
        with self.assertLogs(mcp.logger, 'WARNING') as captured:
            await self.transport._single_result('sample_read', {'note': 'SECRET-ARGUMENT'}, ACTOR)
        record, = slow_records(captured)
        self.assertEqual(set(fields(record)), FIELDS)
        self.assertIsNone(record.exc_info)
        for text in ('SECRET-ARGUMENT', 'SECRET-RESPONSE', 'agent'):
            self.assertNotIn(text, record.getMessage())

    async def test_logging_failure_does_not_change_the_result(self):
        self.delays['sample_read'] = {'execute': 300, 'data': {'value': 7}}
        with patch.object(mcp.logger, 'warning', side_effect=RuntimeError('log sink down')) as warning:
            result = await self.transport._single_result('sample_read', {}, ACTOR)
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data'], {'value': 7})
        self.delays['sample_read'] = {'config': 300, 'config_error': DomainError('forbidden', 'Denied')}
        with patch.object(mcp.logger, 'warning', side_effect=RuntimeError('log sink down')) as warning:
            result = await self.transport._single_result('sample_read', {}, ACTOR)
        self.assertEqual(warning.call_count, 1)
        self.assertEqual((result['status'], result['error']['code']), ('error', 'forbidden'))


if __name__ == '__main__':
    unittest.main()
