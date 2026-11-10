"""Queue selection is unchanged; indexed scans and explicit migrations are verified on SQLite."""
import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut.extensions import Extension, ExtensionSet
from qorqut.migration import queue_indexes as migration
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.core_schema import CORE_SCHEMA
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.inventory import IDENTITY_TABLE, read_objects
from qorqut_operations.storage.queue_indexes import OBJECTS, RECOVER_PENDING, SCHEMA
from qorqut_operations.storage.specs import TableSpec
from qorqut_operations.storage.sqlfunctions import install_sql_functions

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


class SyntheticExtension(Extension):
    id = 'synthetic'

    def storage(self):
        return (StorageContribution('extension.synthetic', {'synthetic_records': TableSpec(('id',))},
                schema=('CREATE TABLE synthetic_records(id TEXT PRIMARY KEY,value TEXT,revision INTEGER DEFAULT 1)',)),)


class QueueMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'operations.sqlite3'
        self.extensions = ExtensionSet((SyntheticExtension(),))
        self.predecessor, self.target = migration.compositions(self.extensions)
        database = Database(self.path, assembly=self.predecessor)
        database.initialize_target()
        database.close()
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.connection.close)
        install_sql_functions(self.connection)
        self.connection.execute("INSERT INTO synthetic_records(id,value) VALUES('test','kept')")
        self.connection.execute("INSERT INTO principals(id,kind,created_at,updated_at) VALUES('agent','agent','now','now')")
        add_receipt(self.connection, 'history', 'applied', payload=b'preserved durable plan')

    def rows(self):
        return {name: self.connection.execute(f'SELECT * FROM "{name}"').fetchall()
                for kind, name in read_objects(self.connection) if kind == 'table' and name != IDENTITY_TABLE}

    def test_explicit_atomic_upgrade_preserves_records_and_extensions_and_is_idempotent(self):
        before = self.rows()
        self.assertEqual(migration.inspect(self.connection, self.extensions)['store'], 'predecessor')
        result = migration.apply(self.connection, self.extensions)
        self.assertEqual(result['schema'], 'added')
        self.assertEqual(migration.inspect(self.connection, self.extensions)['store'], 'target')
        self.assertEqual(self.rows(), before)
        self.assertEqual(migration.apply(self.connection, self.extensions)['schema'], 'already_current')
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        database = Database(self.path, assembly=self.target)
        self.addCleanup(database.close)
        database.open_existing()

    def test_service_open_does_not_silently_upgrade_the_schema(self):
        before = read_objects(self.connection)
        database = Database(self.path, assembly=self.target)
        self.addCleanup(database.close)
        with self.assertRaises(DomainError):
            database.open_existing()
        self.assertEqual(read_objects(self.connection), before)
        self.assertEqual(migration.inspect(self.connection, self.extensions)['store'], 'predecessor')

    def test_partial_ddl_failure_rolls_back_indexes_and_identity_together(self):
        before = read_objects(self.connection)
        self.connection.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_CREATE_INDEX and name == 'signoffs_approved_queue' else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                migration.apply(self.connection, self.extensions)
        finally:
            self.connection.set_authorizer(None)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(read_objects(self.connection), before)
        self.assertEqual(migration.inspect(self.connection, self.extensions)['store'], 'predecessor')

    def test_unknown_schema_and_wrong_extension_selection_are_refused_without_changes(self):
        before = read_objects(self.connection)
        with self.assertRaises(DomainError):
            migration.apply(self.connection)
        self.assertEqual(read_objects(self.connection), before)
        self.connection.execute('CREATE INDEX unexpected ON synthetic_records(value)')
        before = read_objects(self.connection)
        with self.assertRaises(DomainError):
            migration.apply(self.connection, self.extensions)
        self.assertEqual(read_objects(self.connection), before)
        self.assertTrue(OBJECTS.isdisjoint(before))

    def test_cli_requires_exclusive_ownership_before_opening_for_apply(self):
        with patch.object(migration, 'instance_paths_from_arguments',
                          return_value=SimpleNamespace(database=self.path)), \
                patch.object(migration, 'InstanceOwnership') as ownership, \
                patch.object(migration.sqlite3, 'connect') as connect, redirect_stdout(io.StringIO()) as output:
            ownership.return_value.acquire.side_effect = DomainError('instance_in_use', 'Service owns instance')
            self.assertEqual(migration.main(['apply', '--instance-root', str(self.path.parent)]), 1)
            connect.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())['error']['code'], 'instance_in_use')
        self.assertTrue(OBJECTS.isdisjoint(read_objects(self.connection)))

    def test_cli_inspect_is_read_only_and_does_not_take_service_ownership(self):
        before = self.rows()
        with patch.object(migration, 'instance_paths_from_arguments',
                          return_value=SimpleNamespace(database=self.path)), \
                patch.object(migration, 'NO_EXTENSIONS', self.extensions), \
                patch.object(migration, 'InstanceOwnership') as ownership, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(migration.main(['inspect', '--instance-root', str(self.path.parent)]), 0)
            self.assertEqual(json.loads(output.getvalue())['result']['store'], 'predecessor')
            ownership.assert_not_called()
        self.assertEqual(self.rows(), before)
        self.assertTrue(OBJECTS.isdisjoint(read_objects(self.connection)))
