"""The broadcast dispatcher's check on a real store: what an idle check opens, and what a check with work does."""
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace as NS
import tempfile
import unittest

from qorqut_operations.application.engine import Application
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage.database import Database

from qorqut.composition.assembly import host_assembly
from qorqut.inbox.broadcasts import BroadcastDispatcher
from qorqut.service.changes import ChangeFeed

from background_fixture import WriteUnits, rows

NOW = datetime(2026, 11, 1, 9, 0, 30, tzinfo=timezone.utc)
STAMP = '2026-11-01T09:00:30.000000Z'


def refuse(*args, **kwargs):
    raise DomainError('forbidden', 'The sender no longer reaches this recipient')


class BroadcastDispatchWrites(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Database(Path(folder.name) / 'operations.sqlite', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.database.close)
        self.sender, self.recipient = new_id(), new_id()
        with self.database.transaction() as uow:
            for identity in (self.sender, self.recipient):
                uow.repository('principals').insert({'id': identity, 'kind': 'agent', 'display_name': 'agent'})
        # Delivery itself is out of scope here: the sender's context is refused, so a claimed delivery is ``denied``.
        application = NS(writer_admission=None, ensure_writes_open=Application.ensure_writes_open,
                         policy=NS(context_for=refuse))
        runtime = NS(application=application, database=self.database, require_authority=lambda: None)
        self.dispatcher = BroadcastDispatcher(runtime, broadcasts=None, clock=lambda: NOW)
        self.units = WriteUnits(self.database)

    def broadcast(self, *, next_due_at, status='active', manual_count=0, max_occurrences=None, occurrence_count=0):
        identity = new_id()
        timing = {'kind': 'interval', 'at': '2026-11-01T08:00:00Z', 'every_seconds': 3600}
        with self.database.transaction() as uow:
            uow.connection.execute(
                'INSERT INTO broadcasts(id,project_id,sender_id,name,created_by,delivery,message,recipients_json,timing_json,'
                'status,max_occurrences,occurrence_count,manual_count,next_due_at,created_at,updated_at) '
                "VALUES(?,NULL,?,'Example',?,'inbox','A synthetic message',?,?,?,?,?,?,?,?,?)",
                (identity, self.sender, self.sender, json.dumps([{'agent_id': self.recipient, 'project_id': None}]),
                 json.dumps(timing), status, max_occurrences, occurrence_count, manual_count, next_due_at, STAMP, STAMP))
        self.units.take()
        return identity

    def stored(self, identity):
        row, = rows(self.database, 'SELECT status,occurrence_count,manual_count,next_due_at FROM broadcasts WHERE id=?', identity)
        return row

    def deliveries(self):
        return rows(self.database, 'SELECT broadcast_id,occurrence,recipient_id,state FROM broadcast_deliveries')

    def test_an_idle_check_finds_nothing(self):
        later = self.broadcast(next_due_at='2026-11-01T10:00:00Z')
        self.broadcast(next_due_at='2026-11-01T08:00:00Z', status='paused')
        self.broadcast(next_due_at='2026-11-01T08:00:00Z', status='cancelled', manual_count=1)
        self.assertIsNone(self.dispatcher.tick())
        self.assertEqual(self.deliveries(), [])
        self.assertEqual(self.stored(later)['occurrence_count'], 0)
        opened, statements = self.units.take()
        self.assertEqual(opened, [('transaction', None)])
        self.assertEqual(statements, [['BEGIN', 'IMMEDIATE']])

    def test_a_due_broadcast_is_claimed_and_delivered(self):
        identity = self.broadcast(next_due_at='2026-11-01T09:00:00Z')
        self.assertEqual(self.dispatcher.tick(), identity)
        self.assertEqual(self.stored(identity), {'status': 'active', 'occurrence_count': 1, 'manual_count': 0,
                                                 'next_due_at': '2026-11-01T10:00:00Z'})
        self.assertEqual(self.deliveries(), [{'broadcast_id': identity, 'occurrence': 1, 'recipient_id': self.recipient,
                                              'state': 'denied'}])
        self.assertIsNone(self.dispatcher.tick())

    def test_a_manual_request_on_a_paused_broadcast_is_claimed(self):
        identity = self.broadcast(next_due_at=None, status='paused', manual_count=1)
        self.assertEqual(self.dispatcher.tick(), identity)
        self.assertEqual(self.stored(identity)['manual_count'], 0)
        self.assertEqual(len(self.deliveries()), 1)

    def test_a_broadcast_at_its_limit_is_paused(self):
        identity = self.broadcast(next_due_at='2026-11-01T09:00:00Z', max_occurrences=2, occurrence_count=2)
        self.assertEqual(self.dispatcher.tick(), identity)
        self.assertEqual(self.stored(identity)['status'], 'paused')
        self.assertEqual(self.deliveries(), [])

    def test_an_idle_check_is_refused_during_maintenance(self):
        with self.database.transaction() as uow:
            uow.repository('maintenance_state').insert({'id': 'service', 'state': 'closed', 'message': 'closed for a test'})
        with self.assertRaises(DomainError) as raised:
            self.dispatcher.tick()
        self.assertEqual(raised.exception.code, 'maintenance')


if __name__ == '__main__':
    unittest.main()
