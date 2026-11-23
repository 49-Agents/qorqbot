"""The ``native_transport_managed`` transform: a store from before managed agents accepts a managed binding after the ordered upgrade.

It follows the pattern of the Signoff category transform (``tests/test_migration_upgrade.py``): the store is created exactly as the
release before it would have, then walked through ``qorqut.migration.upgrade`` in both directions.
"""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from qorqut.extensions import ExtensionSet
from qorqut.migration import native_transport_managed, upgrade
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.inventory import read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

EXTENSIONS = ExtensionSet(())
MANAGED = native_transport_managed.NAME
AT = upgrade.STATES.index(MANAGED)
PREVIOUS = upgrade.STATES[AT - 1]
# The transforms that came after this one; none of them touches the native session table.
LATER = list(upgrade.STATES[AT + 1:])
LAST = upgrade.STATES[-1]
TABLE = ('table', 'native_sessions')


class NativeTransportMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name).resolve() / 'operations.sqlite3'
        self.states = upgrade.chain(EXTENSIONS)
        self.connection = self.store(PREVIOUS)

    def store(self, state, path=None):
        """A store initialised exactly as a release at ``state`` would have, with an agent and a terminal binding."""
        path = path or self.path
        database = Database(path, assembly=self.states[upgrade.STATES.index(state)][0])
        database.initialize_target()
        database.close()
        connection = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(connection.close)
        install_sql_functions(connection)
        connection.execute("INSERT INTO principals(id,kind,created_at,updated_at) VALUES('agent','agent','now','now')")
        self.session(connection, 'terminal-session', 1, 'terminal')
        return connection

    @staticmethod
    def session(connection, identity, generation, transport):
        connection.execute(
            "INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,process_started,"
            "owner,observed_at,verified_at) VALUES(?,'agent','claude','conversation',?,?,4242,'1',?,'now','now')",
            (identity, generation, transport, 'host:4242:1' if transport == 'managed' else None))

    def later(self):
        """What the transforms after this one added."""
        return set(self.states[-1][1]) - set(self.states[AT][1])

    def capture(self, session):
        self.connection.execute("INSERT INTO native_captures(agent_id,provider,conversation_id,transcript_path,session_id,created_at,"
                                "updated_at) VALUES('agent','claude','conversation','/transcript.jsonl',?,'now','now')", (session,))

    def sessions(self, connection=None):
        return (connection or self.connection).execute('SELECT id,transport,owner FROM native_sessions ORDER BY generation').fetchall()

    def opens(self, state):
        database = Database(self.path, assembly=self.states[upgrade.STATES.index(state)][0])
        self.addCleanup(database.close)
        try:
            database.open_existing()
        except DomainError:
            return False
        return True

    # ----- the chain -------------------------------------------------------------------------------------------------

    def test_it_changes_only_the_native_session_table(self):
        self.assertIs(upgrade.TRANSFORMS[AT - 1], native_transport_managed)
        (_, old), (_, new) = self.states[AT - 1], self.states[AT]
        self.assertEqual(set(new) - set(old), set())
        self.assertEqual({key for key in old if new[key] != old[key]}, {TABLE})
        # Exactly one value is added to the rule.
        self.assertEqual(new[TABLE][1].replace(",'managed'", ''), old[TABLE][1])
        self.assertIn("CHECK(transport IN ('terminal','codex_queue','managed'))", new[TABLE][1])
        self.assertEqual(self.states[-1][0].target.digest, upgrade.chain(EXTENSIONS)[-1][0].target.digest)

    def test_a_transform_that_changes_an_object_it_does_not_declare_is_refused(self):
        with patch.object(native_transport_managed, 'CHANGED', frozenset()), self.assertRaises(DomainError) as refusal:
            upgrade.chain(EXTENSIONS)
        self.assertIn(MANAGED, str(refusal.exception))

    def test_the_stripped_composition_is_the_previous_release_for_this_table_alone(self):
        for assembly, objects in self.states[:AT]:
            self.assertIn("CHECK(transport IN ('terminal','codex_queue'))", objects[TABLE][1])
            self.assertNotIn('managed', objects[TABLE][1])

    # ----- forward ---------------------------------------------------------------------------------------------------

    def test_a_store_before_the_transform_refuses_a_managed_binding_and_accepts_one_after_it(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.session(self.connection, 'managed-session', 2, 'managed')
        before, objects = self.sessions(), read_objects(self.connection)
        self.assertEqual(upgrade.inspect(self.connection, EXTENSIONS)['store'], PREVIOUS)
        self.assertEqual(upgrade.inspect(self.connection, EXTENSIONS)['pending'], [MANAGED, *LATER])

        result = upgrade.apply(self.connection, EXTENSIONS)

        self.assertEqual((result['from'], result['added'], result['store'], result['pending']), (PREVIOUS, [MANAGED, *LATER], LAST, []))
        after = read_objects(self.connection)
        self.assertEqual({key for key in objects if after[key] != objects[key]}, {TABLE})
        self.assertEqual(set(after) - set(objects), self.later())
        self.assertEqual(self.sessions(), before)  # no row moved
        self.assertEqual(self.connection.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(self.connection.execute('PRAGMA writable_schema').fetchone()[0], 0)
        self.assertTrue(self.opens(LAST))
        # The connection that upgraded and one opened afterwards both apply the new rule, and only that.
        self.session(self.connection, 'managed-session', 2, 'managed')
        other = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(other.close)
        install_sql_functions(other)
        self.session(other, 'managed-session-2', 3, 'managed')
        for each in (self.connection, other):
            with self.assertRaises(sqlite3.IntegrityError):
                self.session(each, 'unknown', 9, 'unknown')
        self.assertEqual(upgrade.inspect(self.connection, EXTENSIONS)['held_rows'][MANAGED], 2)
        # A second run changes nothing.
        self.assertEqual(upgrade.apply(self.connection, EXTENSIONS)['added'], [])

    def test_a_store_several_transforms_behind_is_walked_the_whole_way(self):
        path = self.path.with_name('base.sqlite3')
        connection = self.store(upgrade.BASE, path)
        result = upgrade.apply(connection, EXTENSIONS)
        self.assertEqual((result['from'], result['added'][-1], result['store']), (upgrade.BASE, LAST, LAST))
        self.session(connection, 'managed-session', 2, 'managed')
        self.assertEqual(connection.execute('SELECT transport FROM native_sessions ORDER BY generation').fetchall(),
                         [('terminal',), ('managed',)])

    def test_a_store_with_terminal_and_codex_bindings_keeps_every_row_and_the_deletion_guard(self):
        self.session(self.connection, 'codex-session', 2, 'codex_queue')
        before = self.sessions()
        upgrade.apply(self.connection, EXTENSIONS)
        self.assertEqual(self.sessions(), before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM native_sessions WHERE id='codex-session'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE native_sessions SET transport='managed' WHERE id='codex-session'")

    # ----- backward --------------------------------------------------------------------------------------------------

    def test_revert_with_no_managed_binding_narrows_the_rule_and_the_earlier_release_opens_the_store(self):
        upgrade.apply(self.connection, EXTENSIONS)
        target = read_objects(self.connection)
        result = upgrade.revert(self.connection, EXTENSIONS, to=PREVIOUS)
        self.assertEqual((result['removed'], result['dropped_rows'], result['store']), ([MANAGED, *LATER], {MANAGED: 0, **{name: 0 for name in LATER}}, PREVIOUS))
        self.assertNotEqual(read_objects(self.connection)[TABLE], target[TABLE])
        self.assertEqual(self.sessions(), [('terminal-session', 'terminal', None)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.session(self.connection, 'managed-session', 2, 'managed')
        self.assertTrue(self.opens(PREVIOUS))
        self.assertFalse(self.opens(LAST))
        # Forward again returns exactly the target.
        upgrade.apply(self.connection, EXTENSIONS)
        self.assertEqual(read_objects(self.connection), target)

    def test_revert_keeps_a_managed_binding_unless_told_to_drop_it(self):
        upgrade.apply(self.connection, EXTENSIONS)
        self.session(self.connection, 'managed-session', 2, 'managed')
        target = read_objects(self.connection)
        with self.assertRaises(DomainError) as refusal:
            upgrade.revert(self.connection, EXTENSIONS, to=PREVIOUS)
        self.assertEqual((refusal.exception.code, refusal.exception.to_dict()['details']), ('validation_error', {'held_rows': {MANAGED: 1}}))
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(read_objects(self.connection), target)
        self.assertEqual(len(self.sessions()), 2)

        result = upgrade.revert(self.connection, EXTENSIONS, to=PREVIOUS, discard_rows=True)

        self.assertEqual((result['dropped_rows'], result['store']), ({MANAGED: 1, **{name: 0 for name in LATER}}, PREVIOUS))
        self.assertEqual(self.sessions(), [('terminal-session', 'terminal', None)])
        self.assertEqual(self.connection.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertTrue(self.opens(PREVIOUS))
        # The deletion guard is back, exactly as it was: bindings are still never deleted.
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM native_sessions WHERE id='terminal-session'")
        self.assertEqual(read_objects(self.connection)[('trigger', native_transport_managed.NO_DELETE)],
                         target[('trigger', native_transport_managed.NO_DELETE)])
        self.assertEqual(set(read_objects(self.connection)), set(target) - self.later())

    def test_a_managed_binding_that_history_refers_to_is_not_discarded(self):
        upgrade.apply(self.connection, EXTENSIONS)
        self.session(self.connection, 'managed-session', 2, 'managed')
        self.capture('managed-session')
        target = read_objects(self.connection)
        with self.assertRaises(DomainError) as refusal:
            upgrade.revert(self.connection, EXTENSIONS, to=PREVIOUS, discard_rows=True)
        # The reference check undoes the whole walk, the deleted row and the dropped guard included.
        self.assertEqual(refusal.exception.code, 'invalid_database')
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(read_objects(self.connection), target)
        self.assertEqual(len(self.sessions()), 2)
        self.assertEqual(upgrade.inspect(self.connection, EXTENSIONS)['store'], LAST)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM native_sessions WHERE id='managed-session'")

    def test_a_failure_while_replacing_the_definition_leaves_the_store_as_it_was(self):
        objects = read_objects(self.connection)
        self.connection.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
                                       if (action, name) == (sqlite3.SQLITE_PRAGMA, 'writable_schema') else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                upgrade.apply(self.connection, EXTENSIONS)
        finally:
            self.connection.set_authorizer(None)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(read_objects(self.connection), objects)
        self.assertEqual(upgrade.inspect(self.connection, EXTENSIONS)['store'], PREVIOUS)
        self.assertTrue(self.opens(PREVIOUS))

    def test_a_failed_revert_leaves_the_managed_binding_and_the_guard_in_place(self):
        upgrade.apply(self.connection, EXTENSIONS)
        self.session(self.connection, 'managed-session', 2, 'managed')
        target = read_objects(self.connection)
        self.connection.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
                                       if (action, name) == (sqlite3.SQLITE_PRAGMA, 'writable_schema') else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                upgrade.revert(self.connection, EXTENSIONS, to=PREVIOUS, discard_rows=True)
        finally:
            self.connection.set_authorizer(None)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(read_objects(self.connection), target)
        self.assertEqual(len(self.sessions()), 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM native_sessions WHERE id='managed-session'")

    def test_a_definition_that_stored_rows_break_is_refused(self):
        upgrade.apply(self.connection, EXTENSIONS)
        self.session(self.connection, 'managed-session', 2, 'managed')
        target = read_objects(self.connection)
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(DomainError):
                native_transport_managed._replace(self.connection, native_transport_managed._earlier(target[TABLE][1]))
        finally:
            self.connection.execute('ROLLBACK')
        self.assertEqual(read_objects(self.connection), target)
        self.session(self.connection, 'managed-session-2', 3, 'managed')


if __name__ == '__main__':
    unittest.main()
