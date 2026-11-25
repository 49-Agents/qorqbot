"""The due-extraction reconcile query starts from the occurrences and returns exactly what the jobs-first join returned."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from qorqut_operations.storage.database import Database

from qorqut.composition.assembly import host_assembly
from qorqut.overseer.due import MAX_RECONCILE, UNASSIGNED_OCCURRENCES
from qorqut.service.changes import ChangeFeed

# The query as it was before it was rewritten, kept here as the reference for the same rows.
JOBS_FIRST = (
    "SELECT o.* FROM overseer_due_occurrences o JOIN jobs j ON j.id=o.job_id "
    "LEFT JOIN overseer_due_request_occurrences m ON m.occurrence_id=o.id "
    "WHERE m.occurrence_id IS NULL AND j.state='succeeded' ORDER BY o.created_at,o.id LIMIT ?")

STAMP = '2026-11-01T09:00:{:02d}.000000Z'


class DueReconcileQueryTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        path = Path(self.folder.name) / 'operations.sqlite'
        Database(path, assembly=host_assembly(ChangeFeed())).initialize_target()
        # The fixture writes the three tables directly; the rows they point at elsewhere are not part of this query.
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=OFF')
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.connection.close)

    def _job(self, identity, state, second):
        self.connection.execute(
            "INSERT INTO jobs(id,target_namespace,kind,principal_id,arguments_json,dedupe_key,state,created_at,updated_at) "
            "VALUES(?,'global','routine','p','{}',?,?,?,?)", (identity, identity, state, STAMP.format(second), STAMP.format(second)))

    def _occurrence(self, identity, job, second):
        self.connection.execute(
            "INSERT INTO overseer_due_occurrences(id,routine_run_id,job_id,program,program_digest,routine_namespace,"
            "routine_id,routine_revision,scheduled_slot,missed_count,missed_capped,target_agent_id,"
            "target_conversation_id,extraction_arguments_json,created_at) "
            "VALUES(?,?,?,'overseer.extraction_due','d','global','r',1,'s',0,0,'a','c','{}',?)",
            (identity, 'run-' + identity, job, STAMP.format(second)))

    def _member(self, occurrence):
        self.connection.execute(
            "INSERT INTO overseer_due_request_occurrences(request_id,occurrence_id,created_at) VALUES('req',?,?)",
            (occurrence, STAMP.format(0)))

    def _rows(self, sql, limit=MAX_RECONCILE):
        return [tuple(row) for row in self.connection.execute(sql, (limit,)).fetchall()]

    def test_plan_starts_from_the_occurrences(self):
        plan = self.connection.execute('EXPLAIN QUERY PLAN ' + UNASSIGNED_OCCURRENCES, (MAX_RECONCILE,)).fetchall()
        outer = [row['detail'] for row in plan if row['parent'] == 0]
        self.assertTrue(outer[0].startswith('SCAN o'), outer)
        self.assertIn('overseer_due_occurrence_created', outer[0])
        # The jobs are only probed by key inside a subquery, never walked by the outer loop.
        self.assertNotIn('j', [detail.split()[1] for detail in outer], outer)

    def test_same_rows_as_the_jobs_first_join(self):
        # Matching, already a member, job not succeeded, job queued, and unrelated succeeded jobs with no occurrence.
        # Seconds are out of insertion order and two share one creation time, so the order and its id tie-break count.
        cases = (('o5', 'succeeded', None, 5), ('o1', 'succeeded', None, 3), ('o2', 'succeeded', 'member', 3),
                 ('o3', 'failed', None, 1), ('o4', 'queued', None, 2), ('o0', 'succeeded', None, 3),
                 ('o6', 'running', 'member', 4), ('o7', 'succeeded', None, 9))
        for occurrence, state, member, second in cases:
            self._job('job-' + occurrence, state, second)
            self._occurrence(occurrence, 'job-' + occurrence, second)
            if member:
                self._member(occurrence)
        for index in range(20):
            self._job(f'other-{index}', 'succeeded', 0)
        self.connection.commit()
        expected = self._rows(JOBS_FIRST)
        self.assertEqual([row[0] for row in expected], ['o0', 'o1', 'o5', 'o7'])
        self.assertEqual(self._rows(UNASSIGNED_OCCURRENCES), expected)
        self.assertEqual(self._rows(UNASSIGNED_OCCURRENCES, 2), self._rows(JOBS_FIRST, 2))

    def test_no_rows_when_nothing_is_unassigned(self):
        self._job('job-a', 'succeeded', 1)
        self._occurrence('a', 'job-a', 1)
        self._member('a')
        self.connection.commit()
        self.assertEqual(self._rows(UNASSIGNED_OCCURRENCES), [])
        self.assertEqual(self._rows(JOBS_FIRST), [])


if __name__ == '__main__':
    unittest.main()
