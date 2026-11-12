"""A published Context input tells the coordinator about a prepared handoff note, except when the input is an Action."""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4

from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut_context.integration.queue.transitions import record_published
from qorqut_operations.storage.database import Database

STAMP = '2026-11-10T12:00:00.000000Z'
PREPARED = {'disposition': 'prepared', 'text': 'Supplier terms changed.'}
PROFILE = json.dumps({'status': 'active', 'role': {'id': 'ordubashy'},
                      'scope': {'kind': 'ecosystem', 'business_id': None, 'global_capabilities': ['read_global_context']}})


class HandoffNoticeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'operations.sqlite3'
        Database(path, assembly=host_assembly(ChangeFeed())).initialize_target().close()
        # A second connection without foreign key checks, so the fixture holds only the rows this case reads.
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function('inbox_actor', 0, lambda: None)
        self.addCleanup(self.conn.close)
        # The stored Action revision an Action input snapshots is not part of what is tested here.
        self.conn.execute('DROP TRIGGER context_inputs_action_revision')
        self.driver, self.lead = str(uuid4()), str(uuid4())
        for identity, kind in ((self.driver, 'system'), (self.lead, 'agent')):
            self.conn.execute('INSERT INTO principals(id,kind,display_name,active,created_at,updated_at) VALUES(?,?,?,1,?,?)',
                              (identity, kind, kind, STAMP, STAMP))
        self.conn.execute('INSERT INTO agent_profiles(agent_id,principal_id,namespace,name,workspace,profile_json,created_at,'
                          "updated_at) VALUES(?,?,'ecosystem','lead','/lead',?,?,?)", (self.lead, self.lead, PROFILE, STAMP, STAMP))
        self.uow = NS(connection=self.conn, require_write=lambda: None)

    def publish(self, kind, note, event):
        identity = str(uuid4())
        item, revision = (str(uuid4()), 1) if kind == 'action' else (None, None)
        payload = {'action_ref': 'action', 'revision_ref': 'revision'} if kind == 'action' else {'path': 'notes/source.md'}
        self.conn.execute('INSERT INTO context_inputs(id,input_kind,item_id,revision,operation,payload_json,submitted_by,created_at,'
                          "state,context_event_id,attempt) VALUES(?,?,?,?,'add',?,?,?,'processing',?,1)",
                          (identity, kind, item, revision, json.dumps(payload), self.lead, STAMP, event))
        self.conn.execute("INSERT INTO context_input_attempts(input_id,attempt,context_event_id,cause,started_at) "
                          "VALUES(?,1,?,'queue',?)", (identity, event, STAMP))
        record_published(self.uow, driver_id=self.driver, input_id=identity,
                         result={'context_event_id': event, 'published_commit': 'abc123'}, handoff_note=note,
                         ordubashy_id=self.lead, outcomes=NS(record=lambda uow, row: None))
        saved = json.loads(self.conn.execute('SELECT result_json FROM context_inputs WHERE id=?', (identity,)).fetchone()[0])
        notices = self.conn.execute("SELECT count(*) FROM inbox_events WHERE source_type='context_inputs' AND source_id=?",
                                    (identity,)).fetchone()[0]
        return saved, notices

    def test_a_general_input_with_a_prepared_note_notifies_the_coordinator(self):
        saved, notices = self.publish('general', PREPARED, 'aaaa1')
        self.assertEqual(notices, 1)
        self.assertIsNotNone(saved['handoff_event_id'])

    def test_an_action_input_keeps_its_note_and_notifies_nobody(self):
        saved, notices = self.publish('action', PREPARED, 'aaaa2')
        self.assertEqual(notices, 0)
        self.assertIsNone(saved['handoff_event_id'])
        self.assertEqual(saved['handoff_note'], PREPARED)

    def test_an_empty_note_notifies_nobody(self):
        _, notices = self.publish('general', {'disposition': 'empty', 'text': ''}, 'aaaa3')
        self.assertEqual(notices, 0)


if __name__ == '__main__':
    unittest.main()
