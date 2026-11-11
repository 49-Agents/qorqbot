"""One failing board, duty or agent never stops the timed pass for the others."""
from contextlib import contextmanager, nullcontext
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from qorqut.native_chat import nudges
from qorqut.native_chat.nudges import FailureLog, NudgeBackend, isolated
from qorqut_operations.contracts import DomainError


class IsolatedPartTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        self.connection.execute('CREATE TABLE done (part TEXT)')
        self.uow = NS(connection=self.connection)
        patch.object(nudges, 'FAILURES', FailureLog()).start()
        self.addCleanup(patch.stopall)

    def write(self, part, error=None):
        self.connection.execute('INSERT INTO done VALUES(?)', (part,))
        if error:
            raise error

    def parts(self):
        return [row[0] for row in self.connection.execute('SELECT part FROM done ORDER BY part')]

    def test_failing_part_is_rolled_back_alone(self):
        with self.assertLogs(nudges.LOG, 'WARNING') as logs:
            first = isolated(self.uow, 'First duty', lambda: self.write('first'))
            second = isolated(self.uow, 'Second duty',
                              lambda: self.write('second', DomainError('business_archived', 'Archived')))
            third = isolated(self.uow, 'Third duty', lambda: self.write('third', RuntimeError('unexpected')))
            fourth = isolated(self.uow, 'Fourth duty', lambda: self.write('fourth'))
        self.assertEqual((first, second, third, fourth), (True, False, False, True))
        self.assertEqual(self.parts(), ['first', 'fourth'])
        self.assertEqual([record.getMessage() for record in logs.records],
                         ['Second duty failed and was skipped: business_archived',
                          'Third duty failed and was skipped: RuntimeError'])

    def test_failure_that_ends_the_pass_is_still_raised(self):
        for code in ('authority_invalid', 'maintenance'):
            with self.assertRaises(DomainError) as raised:
                isolated(self.uow, 'Duty', lambda: self.write(code, DomainError(code, 'Stop')))
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.parts(), [])


class FailureLogTests(unittest.TestCase):
    def test_repeating_failure_is_reported_when_it_starts_changes_persists_and_clears(self):
        now = [0.0]
        failures = FailureLog(clock=lambda: now[0])
        error = DomainError('business_archived', 'Archived')
        with self.assertLogs(nudges.LOG, 'WARNING') as logs:
            failures.passed('Board')                                    # nothing was failing: nothing to report
            for _ in range(50):
                failures.failed('Board', error)
                now[0] += 3
            now[0] = nudges.REPEAT_SECONDS
            failures.failed('Board', error)
            failures.failed('Board', DomainError('not_found', 'Gone'))
            failures.passed('Board')
            failures.passed('Board')
        self.assertEqual([record.getMessage() for record in logs.records],
                         ['Board failed and was skipped: business_archived',
                          'Board is still failing: business_archived (50 more passes)',
                          'Board failed and was skipped: not_found',
                          'Board recovered'])


class BackendPassTests(unittest.TestCase):
    def backend(self, send_due):
        @contextmanager
        def read():
            yield NS(connection=None)
        runtime = NS(require_authority=lambda: None, paths=None, database=NS(read=read),
                     application=NS(writer_admission=NS(writer=nullcontext)), attention_runtime=NS(send_due=send_due))
        engine = NS(candidates=lambda agents: list(agents), unsettled=lambda **_: [])
        backend = NudgeBackend(runtime, engine, None, None, None, lambda _agent: nullcontext())
        backend.step = Mock()
        return backend

    def run_pass(self, backend):
        profiles = [{'agent_id': agent} for agent in ('first', 'second', 'third')]
        with patch.object(nudges, 'FAILURES', FailureLog()), \
                patch.object(nudges.store, 'observed_agents', return_value=profiles):
            backend.run_once()

    def test_one_agent_with_an_unreadable_board_does_not_end_the_pass_for_the_rest(self):
        reminded = []

        def send_due(profile):
            reminded.append(profile['agent_id'])
            if profile['agent_id'] == 'second':
                raise DomainError('business_archived', 'Archived')
        backend = self.backend(send_due)
        with self.assertLogs(nudges.LOG, 'WARNING') as logs:
            self.run_pass(backend)
        self.assertEqual(reminded, ['first', 'second', 'third'])
        self.assertEqual([call.args[0]['agent_id'] for call in backend.step.call_args_list], ['first', 'second', 'third'])
        self.assertEqual([record.getMessage() for record in logs.records],
                         ['Attention reminder for second failed and was skipped: business_archived'])

    def test_one_agent_whose_inbox_nudge_fails_does_not_end_the_pass_for_the_rest(self):
        backend = self.backend(lambda _profile: None)
        backend.step.side_effect = lambda profile: (_ for _ in ()).throw(
            DomainError('not_found', 'Gone')) if profile['agent_id'] == 'first' else 'sent'
        with self.assertLogs(nudges.LOG, 'WARNING'):
            self.run_pass(backend)
        self.assertEqual([call.args[0]['agent_id'] for call in backend.step.call_args_list], ['first', 'second', 'third'])

    def test_closed_writes_still_end_the_pass(self):
        reminded = []

        def send_due(profile):
            reminded.append(profile['agent_id'])
            raise DomainError('maintenance', 'Writes are unavailable during recovery')
        backend = self.backend(send_due)
        with self.assertRaises(DomainError):
            self.run_pass(backend)
        self.assertEqual(reminded, ['first'])
        backend.step.assert_not_called()


if __name__ == '__main__':
    unittest.main()
