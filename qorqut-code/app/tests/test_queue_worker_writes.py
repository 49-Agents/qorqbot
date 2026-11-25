"""A background queue worker's pass on a real store: what an idle pass opens, and what a pass with work does."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from background_fixture import OTHER_PROGRAM, OTHER_QUEUE, WriteUnits, build, routine, rows

START = datetime(2026, 11, 1, 9, 0, 30, tzinfo=timezone.utc)


class QueueWorkerWrites(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.now = START
        self.fixture = build(Path(folder.name), clock=lambda: self.now)
        self.addCleanup(self.fixture.database.close)
        self.background = self.fixture.background
        self.identity = routine(self.fixture)
        self.other = routine(self.fixture, program=OTHER_PROGRAM)
        self.fixture.scheduler.tick(START)
        self.units = WriteUnits(self.fixture.database)

    def queue_jobs(self):
        """One due job on each queue: the general one first."""
        self.now += timedelta(minutes=1)
        queued = self.fixture.scheduler.tick(self.now)
        self.units.take()
        kinds = {row['id']: row['kind'] for row in rows(self.fixture.database, 'SELECT id,kind FROM jobs')}
        return sorted(queued, key=lambda job: kinds[job] == OTHER_PROGRAM)

    def job(self, identity):
        row, = rows(self.fixture.database, 'SELECT state,attempts,claimed_by FROM jobs WHERE id=?', identity)
        return row

    def test_an_idle_pass_runs_nothing(self):
        self.assertEqual(self.background.queues, ('general', OTHER_QUEUE))
        for queue in self.background.queues:
            self.assertIsNone(self.background.run_one(queue=queue))
        self.assertEqual(self.fixture.calls, [])
        self.assertIsNone(self.background.last_error)
        # Each queue looks first and opens no write unit for nothing.
        self.assertEqual(self.units.take(), ([], []))

    def after_look(self, change):
        """Run ``change`` once, right after the pass's look and before its write unit would select."""
        select, done = self.background._next_job, []

        def next_job(uow, queue=None):
            found = select(uow, queue)
            if not done:
                done.append(True)
                change()
            return found
        self.background._next_job = next_job

    def test_a_job_that_becomes_due_after_the_look_is_run_by_the_next_pass(self):
        jobs = []
        self.after_look(lambda: jobs.extend(self.queue_jobs()))
        self.assertIsNone(self.background.run_one(queue='general'))
        self.assertEqual(self.job(jobs[0])['state'], 'queued')
        self.assertEqual(self.background.run_one(queue='general'), {'job_id': jobs[0], 'state': 'succeeded'})

    def test_a_job_gone_by_the_write_unit_is_an_ordinary_empty_result(self):
        job = self.queue_jobs()[0]

        def cancel():
            with self.fixture.database.transaction() as uow:
                uow.connection.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job,))
        self.after_look(cancel)
        self.units.take()
        self.assertIsNone(self.background.run_one(queue='general'))
        self.assertIsNone(self.background.last_error)
        self.assertEqual(self.fixture.calls, [])
        # The cancelling unit, then the pass's own write unit, which found nothing to claim and wrote nothing.
        opened, statements = self.units.take()
        self.assertEqual(opened, [('transaction', None)] * 2)
        self.assertEqual(statements[-1], ['BEGIN', 'IMMEDIATE'])

    def test_a_job_waiting_for_its_retry_time_is_not_run(self):
        jobs = self.queue_jobs()
        with self.fixture.database.transaction() as uow:
            uow.connection.execute("UPDATE jobs SET next_retry_at='2026-11-01T10:00:00Z'")
        for queue, job in zip(self.background.queues, jobs):
            self.assertIsNone(self.background.run_one(queue=queue))
            self.assertEqual(self.job(job)['state'], 'queued')
        self.assertEqual(self.fixture.calls, [])

    def test_a_queued_job_is_run_to_completion_on_its_own_queue(self):
        jobs = self.queue_jobs()
        for queue, job in zip(self.background.queues, jobs):
            self.assertEqual(self.background.run_one(queue=queue), {'job_id': job, 'state': 'succeeded'})
            self.assertEqual(self.job(job), {'state': 'succeeded', 'attempts': 1, 'claimed_by': None})
            self.assertIsNone(self.background.run_one(queue=queue))
        self.assertEqual(self.fixture.calls, [{}, {}])
        self.assertEqual([run['state'] for run in rows(self.fixture.database, 'SELECT state FROM routine_runs')],
                         ['succeeded', 'succeeded'])

    def test_an_idle_pass_during_maintenance_reports_it(self):
        with self.fixture.database.transaction() as uow:
            uow.repository('maintenance_state').insert({'id': 'service', 'state': 'closed', 'message': 'closed for a test'})
        result = self.background.run_one(queue='general')
        self.assertEqual((result['job_id'], result['error']['code']), (None, 'maintenance'))
        self.assertEqual(self.background.last_error, 'maintenance')


if __name__ == '__main__':
    unittest.main()
