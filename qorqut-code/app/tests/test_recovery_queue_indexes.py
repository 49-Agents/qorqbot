"""Queue selection is unchanged; indexed scans are verified on SQLite."""
import json
import sqlite3
import unittest

from qorqut_operations.storage.core_schema import CORE_SCHEMA
from qorqut_operations.storage.queue_indexes import RECOVER_PENDING, SCHEMA

PREVIOUS_QUERY = """SELECT id FROM apply_receipts WHERE signoff_id IS NULL AND
    (state IN ('prepared','applying') OR (state='failed' AND json_extract(error_json, '$.retryable')=1
    AND EXISTS (SELECT 1 FROM resource_claims WHERE apply_id=apply_receipts.id))) ORDER BY created_at,id LIMIT ?"""


def queue_fixture(connection):
    for statement in CORE_SCHEMA:
        if statement.startswith(('CREATE TABLE apply_receipts', 'CREATE TABLE resource_claims')):
            connection.execute(statement)
    connection.execute('CREATE TABLE signoffs(id TEXT PRIMARY KEY,state TEXT,updated_sequence INTEGER UNIQUE)')


def add_receipt(connection, identifier, state, *, error=None, signoff=None, created='same', payload=b'plan'):
    connection.execute('INSERT INTO apply_receipts(id,principal_id,target_namespace,operation_name,payload_hash,'
        'change_set,signoff_id,state,error_json,created_at,updated_at) VALUES(?,\'agent\',\'global\',\'test\',\'hash\',?,?,?,?,?,?)',
        (identifier, payload, signoff, state, json.dumps(error) if error is not None else None, created, created))


class QueueSelectionTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.addCleanup(self.connection.close)
        queue_fixture(self.connection)

    def indexes(self):
        for statement in SCHEMA:
            self.connection.execute(statement)

    def test_every_state_retryability_claim_and_signoff_preserves_order_and_limit(self):
        number = 0
        for state in ('prepared', 'applying', 'applied', 'failed', 'conflicted'):
            for error in (None, {}, {'retryable': True}, {'retryable': False}, {'retryable': 'true'}):
                for claim in (False, True):
                    for signoff in (None, 'signoff'):
                        number += 1
                        identifier = f'{number:04}'
                        add_receipt(self.connection, identifier, state, error=error,
                                    signoff=identifier if signoff else None, created=str(number % 3))
                        if claim:
                            self.connection.execute('INSERT INTO resource_claims VALUES(\'global\',\'test\',?,?,\'now\')',
                                                    (identifier, identifier))
        expected = {limit: self.connection.execute(PREVIOUS_QUERY, (limit,)).fetchall() for limit in (1, 7, 50, 100)}
        self.indexes()
        for limit, rows in expected.items():
            self.assertEqual(self.connection.execute(RECOVER_PENDING, (limit,)).fetchall(), rows)
        plan = ' '.join(row[3] for row in self.connection.execute('EXPLAIN QUERY PLAN ' + RECOVER_PENDING, (50,)))
        self.assertIn('apply_receipts_recovery', plan)
        self.assertIn('resource_claims_application', plan)

    def test_empty_queue_work_does_not_grow_with_settled_receipts(self):
        for index in range(3000):
            add_receipt(self.connection, str(index), 'applied', payload=b'x' * 1024)
        def steps(query):
            calls = []
            self.connection.set_progress_handler(lambda: calls.append(1) or 0, 100)
            try:
                self.assertEqual(self.connection.execute(query, (50,)).fetchall(), [])
                return len(calls) * 100
            finally:
                self.connection.set_progress_handler(None, 0)
        before = steps(PREVIOUS_QUERY)
        self.indexes()
        after = steps(RECOVER_PENDING)
        self.assertGreater(before, 10000)
        self.assertLess(after, 100)

    def test_approved_signoffs_remain_ordered_and_use_the_partial_index(self):
        self.connection.executemany('INSERT INTO signoffs VALUES(?,?,?)',
            ((str(i), 'approved' if i % 11 == 0 else 'applied', 1000-i) for i in range(200)))
        query = "SELECT id FROM signoffs WHERE state='approved' ORDER BY updated_sequence,id LIMIT 50"
        expected = self.connection.execute(query).fetchall()
        self.indexes()
        self.assertEqual(self.connection.execute(query).fetchall(), expected)
        plan = ' '.join(row[3] for row in self.connection.execute('EXPLAIN QUERY PLAN ' + query))
        self.assertIn('signoffs_approved_queue', plan)
        self.assertNotIn('TEMP B-TREE', plan)
