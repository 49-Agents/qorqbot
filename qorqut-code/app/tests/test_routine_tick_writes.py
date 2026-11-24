"""The routine scheduler tick on a real store: what an idle tick opens, and what a tick with work does."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from qorqut_operations.contracts import DomainError

from background_fixture import PROGRAM, WriteUnits, build, routine, rows

START = datetime(2026, 11, 1, 9, 0, 30, tzinfo=timezone.utc)


class RoutineTickWrites(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.fixture = build(Path(folder.name))
        self.addCleanup(self.fixture.database.close)
        self.scheduler = self.fixture.scheduler
        self.identity = routine(self.fixture)
        # The first tick only records the slot it first saw, so that a new routine never fires for a slot before it existed.
        self.assertEqual(self.scheduler.tick(START), [])
        self.slot = rows(self.fixture.database, 'SELECT * FROM routine_slots')
        self.assertEqual(len(self.slot), 1)
        self.units = WriteUnits(self.fixture.database)

    def jobs(self):
        return rows(self.fixture.database, 'SELECT id,kind,state FROM jobs')

    def test_an_idle_tick_queues_nothing_and_changes_nothing(self):
        for seconds in (0, 10, 29):
            self.assertEqual(self.scheduler.tick(START + timedelta(seconds=seconds)), [])
        self.assertEqual(self.jobs(), [])
        self.assertEqual(rows(self.fixture.database, 'SELECT * FROM routine_slots'), self.slot)
        self.assertEqual(self.scheduler.errors, [])
        opened, statements = self.units.take()
        self.assertEqual(opened, [('transaction', 'jobs_routines_tick')] * 3)
        self.assertEqual(statements, [['BEGIN', 'IMMEDIATE']] * 3)

    def test_a_due_slot_queues_one_job(self):
        queued = self.scheduler.tick(START + timedelta(minutes=1))
        self.assertEqual(len(queued), 1)
        self.assertEqual(self.jobs(), [{'id': queued[0], 'kind': PROGRAM, 'state': 'queued'}])
        slot, = rows(self.fixture.database, 'SELECT * FROM routine_slots')
        self.assertEqual(slot['slot'], '2026-11-01T09:01:00Z')
        run, = rows(self.fixture.database, 'SELECT routine_id,state,trigger FROM routine_runs')
        self.assertEqual(run, {'routine_id': self.identity, 'state': 'queued', 'trigger': 'schedule'})
        # While that job is pending, later slots wait for it.
        self.assertEqual(self.scheduler.tick(START + timedelta(minutes=2)), [])

    def test_an_idle_tick_reports_a_broken_routine_once_per_tick(self):
        with self.fixture.database.transaction() as uow:
            uow.connection.execute("UPDATE routines SET job_kind='example.not_installed'")
        for _ in range(2):
            self.assertEqual(self.scheduler.tick(START + timedelta(seconds=5)), [])
            self.assertEqual([error['routine_id'] for error in self.scheduler.errors], [self.identity])

    def test_an_idle_tick_is_refused_during_maintenance(self):
        with self.fixture.database.transaction() as uow:
            uow.repository('maintenance_state').insert({'id': 'service', 'state': 'closed', 'message': 'closed for a test'})
        with self.assertRaises(DomainError) as raised:
            self.scheduler.tick(START + timedelta(seconds=5))
        self.assertEqual(raised.exception.code, 'maintenance')


if __name__ == '__main__':
    unittest.main()
