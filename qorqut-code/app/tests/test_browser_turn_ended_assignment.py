"""A browser turn whose assignment has ended is handed on, even when its last command was never reconciled.

Only an assignment's current generation reconciles an unsettled command. A cancelled or completed assignment never has one
again, so its turn used to own the browser for good and every later assignment queued behind it.
"""
from pathlib import Path
import tempfile
import unittest

from qorqut_operations.contracts import new_id
from qorqut_operations.storage.database import Database

from qorqut.agents.operational_delegation import OperationalDelegation
from qorqut.browser.turns import BrowserTurnQueue
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed

START = '2026-11-20T00:00:00Z'
IN_FENCE = '2026-11-20T00:00:30Z'        # the 60 second command fence still holds
LEASE_OVER = '2026-11-20T00:10:00Z'      # past the fence and the 300 second turn lease


class EndedAssignmentTurn(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.database = Database(root / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.database.close)
        self.parent = new_id()
        self.key = 'configured:' + self.parent
        self.store = OperationalDelegation(clock=lambda: START)
        self.turns = BrowserTurnQueue(clock=lambda: START, delegation=self.store)
        with self.database.transaction() as uow:
            uow.repository('principals').insert({'id': self.parent, 'kind': 'agent', 'display_name': 'Parent'})

    def running(self, uow, now=START):
        """A claimed assignment: its first generation is running."""
        assignment = self.store.create(uow, parent_principal_id=self.parent, project_id=None,
            scope={'kind': 'global', 'allowed_project_ids': [], 'record_ids': {}, 'source_ids': {}, 'browser_key': self.key},
            objective='Browser work', plan={'steps': ['open the page']}, allowed_operations=['browser_use'])
        return self.store.claim(uow, assignment['id'], actor_principal_id=self.parent, dispatcher_ready=True, now=now)

    def acquire(self, uow, assignment, now):
        return self.turns.acquire(uow, browser_key=self.key, project_id=None, parent_principal_id=self.parent,
            requester_kind='assignment', requester_id=assignment['id'], assignment_id=assignment['id'],
            generation=assignment['execution_generation'], now=now)

    def mid_command(self, uow):
        """An assignment that owns the browser and has a command in flight that nothing has settled."""
        assignment = self.running(uow)
        turn = self.acquire(uow, assignment, START)
        self.assertEqual(turn['state'], 'owning')
        self.turns.begin_action(uow, turn['id'], parent_principal_id=self.parent, requester_id=assignment['id'],
            generation=assignment['execution_generation'], lease_id=turn['lease_id'], action_id='a-' + turn['id'],
            command=['click', '#send'], now=START)
        return assignment, turn

    def control(self, uow, assignment, action):
        current = self.store.get(uow, assignment['id'])
        return self.store.control(uow, assignment['id'], parent_principal_id=self.parent,
                                  expected_revision=current['revision'], action=action)

    def turn(self, uow, identity):
        return dict(uow.connection.execute("SELECT * FROM browser_turns WHERE id=?", (identity,)).fetchone())

    def action(self, uow, turn):
        return dict(uow.connection.execute("SELECT * FROM browser_turn_actions WHERE turn_id=?", (turn['id'],)).fetchone())

    def test_a_cancelled_assignments_unsettled_turn_is_handed_to_the_next_assignment(self):
        with self.database.transaction() as uow:
            cancelled, held = self.mid_command(uow)
            self.control(uow, cancelled, 'cancel')
            # Cancelling does not take the browser while the command may still be executing.
            self.assertEqual(self.turn(uow, held['id'])['state'], 'owning')
            waiting = self.running(uow, now=IN_FENCE)
            self.assertEqual(self.acquire(uow, waiting, IN_FENCE)['state'], 'queued')
            self.assertEqual(self.turn(uow, held['id'])['state'], 'owning')

            handed = self.acquire(uow, waiting, LEASE_OVER)
            self.assertEqual(handed['state'], 'owning')
            released = self.turn(uow, held['id'])
            self.assertEqual((released['state'], released['command_id']), ('released', None))
            # What the command did is still unknown, and the record says so.
            unknown = self.action(uow, held)
            self.assertEqual((unknown['state'], unknown['error']),
                             ('uncertain', 'assignment ended before this action was reconciled'))

    def test_a_paused_assignment_keeps_its_unsettled_turn_for_the_generation_that_resumes_it(self):
        with self.database.transaction() as uow:
            paused, held = self.mid_command(uow)
            self.control(uow, paused, 'pause')
            waiting = self.running(uow, now=IN_FENCE)
            self.assertEqual(self.acquire(uow, waiting, LEASE_OVER)['state'], 'queued')
            self.assertEqual(self.turn(uow, held['id'])['state'], 'owning')
            self.assertEqual(self.action(uow, held)['state'], 'pending')


if __name__ == '__main__':
    unittest.main()
