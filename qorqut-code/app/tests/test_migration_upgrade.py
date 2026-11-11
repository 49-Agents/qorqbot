"""The ordered offline upgrade walks a store over every schema transform, in both directions, atomically."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut.composition.assembly import host_assembly
from qorqut.extensions import Extension, ExtensionSet
from qorqut.migration import queue_indexes, signoff_side_chat, signoff_sources, upgrade
from qorqut.service.changes import ChangeFeed
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.inventory import IDENTITY_TABLE, read_objects
from qorqut_operations.storage.specs import TableSpec
from qorqut_operations.storage.sqlfunctions import install_sql_functions

# The composition, with no extension selected, of the last release before the first transform of the chain.
RELEASED_BASE_DIGEST = '72ee676d38b3135dc613aefa6f86e379293f05d6fb9a7c4ab7b1cea596e19ee3'
BASE, SOURCES, INDEXES, SIDE_CHAT = upgrade.STATES
SIGNOFFS = ('table', 'signoffs')


class SyntheticExtension(Extension):
    id = 'synthetic'

    def storage(self):
        return (StorageContribution('extension.synthetic', {'synthetic_records': TableSpec(('id',))},
                schema=('CREATE TABLE synthetic_records(id TEXT PRIMARY KEY,value TEXT,revision INTEGER DEFAULT 1)',)),)


class ChainTests(unittest.TestCase):
    def test_states_run_from_the_released_base_to_this_build_in_release_order(self):
        self.assertEqual(upgrade.STATES, ('base', 'signoff_sources', 'queue_indexes', 'signoff_side_chat'))
        for extensions in (ExtensionSet(()), ExtensionSet((SyntheticExtension(),))):
            states = upgrade.chain(extensions)
            self.assertEqual(states[-1][0].target.digest, host_assembly(ChangeFeed(), extensions).target.digest)
            for transform, (_, old), (_, new) in zip(upgrade.TRANSFORMS, states, states[1:]):
                self.assertEqual(set(new) - set(old), set(transform.OBJECTS))
                self.assertEqual({key for key in old if new[key] != old[key]}, set(getattr(transform, 'CHANGED', ())))
            # The last transform adds nothing: one more accepted Signoff category is the whole difference.
            self.assertEqual(states[-1][1][SIGNOFFS][1].replace(",'side_chat'", ''), states[-2][1][SIGNOFFS][1])
            self.assertEqual(len({assembly.target.digest for assembly, _ in states}), len(upgrade.STATES))

    def test_a_transform_that_changes_an_object_it_does_not_declare_is_refused(self):
        with patch.object(signoff_side_chat, 'CHANGED', frozenset()), self.assertRaises(DomainError) as refusal:
            upgrade.chain()
        self.assertIn('signoff_side_chat', str(refusal.exception))

    def test_the_base_is_the_composition_released_before_the_first_transform(self):
        self.assertEqual(upgrade.chain()[0][0].target.digest, RELEASED_BASE_DIGEST,
            'The oldest store this build can upgrade is no longer the released base. A schema change belongs in '
            'qorqut.migration.upgrade.TRANSFORMS; move the base forward only on purpose, and then pin its digest here.')


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'operations.sqlite3'
        self.extensions = ExtensionSet((SyntheticExtension(),))
        self.states = upgrade.chain(self.extensions)
        self.connection = self.store(BASE)

    def store(self, state, path=None):
        """A store initialised exactly as a release at ``state`` would have, holding a little data of every kind."""
        path = path or self.path
        database = Database(path, assembly=self.states[upgrade.STATES.index(state)][0])
        database.initialize_target()
        database.close()
        connection = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(connection.close)
        install_sql_functions(connection)
        connection.execute("INSERT INTO synthetic_records(id,value) VALUES('test','kept')")
        connection.execute("INSERT INTO principals(id,kind,created_at,updated_at) VALUES('agent','agent','now','now')")
        self.signoff(connection, 'signoff')
        return connection

    @staticmethod
    def signoff(connection, identity, category='internal_change'):
        connection.execute("INSERT INTO signoffs(id,category,review_scope_kind,proposer_principal_id,operation_name,"
            "operation_version,proposal_revision,payload_hash,change_set,preview_json,expected_targets_json,"
            "policy_binding_json,affected_scope_json,state,updated_sequence,created_at,updated_at) VALUES(?,?,'global',"
            "'agent','test',1,1,'hash',x'00','{}','{}','{}','{}','pending',"
            "(SELECT coalesce(max(updated_sequence),0)+1 FROM signoffs),'now','now')", (identity, category))

    def link(self, connection=None):
        (connection or self.connection).execute("INSERT INTO signoff_source_links(id,signoff_id,source_type,source_id,"
            "source_namespace,created_by,created_at) VALUES(?,'signoff','task','task-1','global','agent','now')", ('0' * 36,))

    def rows(self, connection=None):
        connection = connection or self.connection
        return {name: connection.execute(f'SELECT * FROM "{name}"').fetchall()
                for kind, name in read_objects(connection) if kind == 'table' and name != IDENTITY_TABLE}

    def opens(self, state, path=None):
        database = Database(path or self.path, assembly=self.states[upgrade.STATES.index(state)][0])
        self.addCleanup(database.close)
        try:
            database.open_existing()
        except DomainError:
            return False
        return True

    def test_a_store_several_transforms_behind_is_refused_by_each_single_command_and_upgraded_by_the_chain(self):
        before, objects = self.rows(), read_objects(self.connection)
        for single in (signoff_sources, queue_indexes):
            with self.assertRaises(DomainError):
                single.apply(self.connection, self.extensions)
        self.assertFalse(self.opens(SIDE_CHAT))
        self.assertEqual(read_objects(self.connection), objects)
        self.assertEqual(upgrade.inspect(self.connection, self.extensions), {
            'store': BASE, 'applied': [], 'pending': [SOURCES, INDEXES, SIDE_CHAT], 'held_rows': {},
            'digest': self.states[0][0].target.digest, 'target_digest': self.states[-1][0].target.digest})

        result = upgrade.apply(self.connection, self.extensions)

        self.assertEqual((result['from'], result['added'], result['store'], result['pending']),
                         (BASE, [SOURCES, INDEXES, SIDE_CHAT], SIDE_CHAT, []))
        self.assertEqual(result['digest'], self.states[-1][0].target.digest)
        self.assertEqual(set(read_objects(self.connection)) - set(objects), set(signoff_sources.OBJECTS) | set(queue_indexes.OBJECTS))
        after = self.rows()
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(after[signoff_sources.TABLE], [])
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertTrue(self.opens(SIDE_CHAT))
        # Both single commands see their own target, and a second run changes nothing.
        self.assertEqual(signoff_sources.inspect(self.connection, self.extensions)['store'], 'target')
        self.assertEqual(queue_indexes.inspect(self.connection, self.extensions)['store'], 'target')
        again = upgrade.apply(self.connection, self.extensions)
        self.assertEqual((again['from'], again['added']), (SIDE_CHAT, []))
        self.assertEqual(self.rows(), after)

    def test_a_store_part_of_the_way_along_gets_only_the_rest(self):
        path = self.path.with_name('sources.sqlite3')
        connection = self.store(SOURCES, path)
        self.link(connection)
        before = self.rows(connection)
        report = upgrade.inspect(connection, self.extensions)
        self.assertEqual((report['store'], report['applied'], report['pending'], report['held_rows']),
                         (SOURCES, [SOURCES], [INDEXES, SIDE_CHAT], {SOURCES: 1}))
        # The queue-index command alone starts from a store that already accepts side chats, which no release
        # made; with a later transform in the chain, only the chain recognises this one.
        with self.assertRaises(DomainError):
            queue_indexes.inspect(connection, self.extensions)
        result = upgrade.apply(connection, self.extensions)
        self.assertEqual((result['from'], result['added'], result['held_rows']),
                         (SOURCES, [INDEXES, SIDE_CHAT], {SOURCES: 1, INDEXES: 0, SIDE_CHAT: 0}))
        self.assertEqual(self.rows(connection), before)
        self.assertTrue(self.opens(SIDE_CHAT, path))

    def test_side_chats_are_refused_before_the_upgrade_and_stored_after_it(self):
        path = self.path.with_name('indexes.sqlite3')
        connection = self.store(INDEXES, path)
        for category in ('platform_action', 'input_request'):
            self.signoff(connection, category, category)
        with self.assertRaises(sqlite3.IntegrityError):
            self.signoff(connection, 'side-chat', 'side_chat')
        before, objects = self.rows(connection), read_objects(connection)

        result = upgrade.apply(connection, self.extensions)

        self.assertEqual((result['from'], result['added'], result['held_rows'][SIDE_CHAT]), (INDEXES, [SIDE_CHAT], 0))
        # One stored definition is the whole change: no object came or went and no row moved.
        after = read_objects(connection)
        self.assertEqual(set(after), set(objects))
        self.assertEqual({key for key in objects if after[key] != objects[key]}, {SIGNOFFS})
        self.assertEqual(self.rows(connection), before)
        self.assertEqual(connection.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(connection.execute('PRAGMA writable_schema').fetchone()[0], 0)
        self.assertTrue(self.opens(SIDE_CHAT, path))
        # The connection that upgraded and one opened afterwards both apply the new rule, and only that.
        self.signoff(connection, 'side-chat', 'side_chat')
        other = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(other.close)
        install_sql_functions(other)
        self.signoff(other, 'side-chat-2', 'side_chat')
        for each in (connection, other):
            with self.assertRaises(sqlite3.IntegrityError):
                self.signoff(each, 'unknown', 'unknown')
        self.assertEqual(upgrade.inspect(connection, self.extensions)['held_rows'][SIDE_CHAT], 2)

    def test_revert_keeps_side_chats_unless_told_to_drop_them(self):
        upgrade.apply(self.connection, self.extensions)
        self.signoff(self.connection, 'side-chat', 'side_chat')
        target = read_objects(self.connection)
        with self.assertRaises(DomainError) as refusal:
            upgrade.revert(self.connection, self.extensions, to=INDEXES)
        self.assertEqual(refusal.exception.to_dict()['details'], {'held_rows': {SIDE_CHAT: 1}})
        self.assertEqual(read_objects(self.connection), target)
        # A side chat that another row still refers to is not discarded either: the reference check undoes the walk.
        self.connection.execute("INSERT INTO signoff_source_links(id,signoff_id,source_type,source_id,source_namespace,"
            "created_by,created_at) VALUES(?,'side-chat','task','task-1','global','agent','now')", ('1' * 36,))
        with self.assertRaises(DomainError) as refusal:
            upgrade.revert(self.connection, self.extensions, to=INDEXES, discard_rows=True)
        self.assertEqual(refusal.exception.code, 'invalid_database')
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(read_objects(self.connection), target)
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['held_rows'][SIDE_CHAT], 1)
        self.connection.execute('DELETE FROM signoff_source_links')
        result = upgrade.revert(self.connection, self.extensions, to=INDEXES, discard_rows=True)
        self.assertEqual((result['removed'], result['dropped_rows'], result['store']), ([SIDE_CHAT], {SIDE_CHAT: 1}, INDEXES))
        self.assertEqual(self.connection.execute('SELECT id FROM signoffs').fetchall(), [('signoff',)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.signoff(self.connection, 'side-chat', 'side_chat')
        self.assertTrue(self.opens(INDEXES))

    def test_a_definition_that_stored_rows_break_is_refused(self):
        upgrade.apply(self.connection, self.extensions)
        self.signoff(self.connection, 'side-chat', 'side_chat')
        target = read_objects(self.connection)
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            with self.assertRaises(DomainError):
                signoff_side_chat._replace(self.connection, signoff_side_chat._earlier(target[SIGNOFFS][1]))
        finally:
            self.connection.execute('ROLLBACK')
        self.assertEqual(read_objects(self.connection), target)
        self.signoff(self.connection, 'side-chat-2', 'side_chat')

    def test_revert_walks_back_one_state_at_a_time_or_all_the_way_and_the_earlier_release_opens_the_store(self):
        before, objects = self.rows(), read_objects(self.connection)
        upgrade.apply(self.connection, self.extensions)
        result = upgrade.revert(self.connection, self.extensions, to=INDEXES)
        self.assertEqual((result['from'], result['removed'], result['store'], result['dropped_rows']),
                         (SIDE_CHAT, [SIDE_CHAT], INDEXES, {SIDE_CHAT: 0}))
        self.assertTrue(self.opens(INDEXES))
        self.assertFalse(self.opens(SIDE_CHAT))
        result = upgrade.revert(self.connection, self.extensions, to=SOURCES)
        self.assertEqual((result['from'], result['removed'], result['store'], result['dropped_rows']),
                         (INDEXES, [INDEXES], SOURCES, {INDEXES: 0}))
        self.assertTrue(self.opens(SOURCES))
        self.assertFalse(self.opens(INDEXES))
        result = upgrade.revert(self.connection, self.extensions, to=BASE)
        self.assertEqual((result['from'], result['removed'], result['store'], result['digest']),
                         (SOURCES, [SOURCES], BASE, self.states[0][0].target.digest))
        self.assertEqual(read_objects(self.connection), objects)
        self.assertEqual(self.rows(), before)
        self.assertTrue(self.opens(BASE))
        self.assertFalse(self.opens(SOURCES))
        # Already there is not an error; asking for a later state is, because revert never adds.
        self.assertEqual(upgrade.revert(self.connection, self.extensions, to=BASE)['removed'], [])
        for later in (SOURCES, INDEXES, SIDE_CHAT, 'unknown'):
            with self.assertRaises(DomainError) as refusal:
                upgrade.revert(self.connection, self.extensions, to=later)
            self.assertEqual(refusal.exception.code, 'validation_error')
        self.assertEqual(read_objects(self.connection), objects)
        # Straight from the target to the base in one transaction, then forward again.
        upgrade.apply(self.connection, self.extensions)
        self.assertEqual(upgrade.revert(self.connection, self.extensions, to=BASE)['removed'], [SOURCES, INDEXES, SIDE_CHAT])
        self.assertEqual(read_objects(self.connection), objects)
        self.assertEqual(upgrade.apply(self.connection, self.extensions)['added'], [SOURCES, INDEXES, SIDE_CHAT])

    def test_revert_keeps_recorded_source_links_unless_told_to_drop_them(self):
        objects = read_objects(self.connection)
        upgrade.apply(self.connection, self.extensions)
        self.link()
        target = read_objects(self.connection)
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['held_rows'], {SOURCES: 1, INDEXES: 0, SIDE_CHAT: 0})
        with self.assertRaises(DomainError) as refusal:
            upgrade.revert(self.connection, self.extensions, to=BASE)
        self.assertEqual(refusal.exception.code, 'validation_error')
        self.assertEqual(refusal.exception.to_dict()['details'], {'held_rows': {SOURCES: 1}})
        # The refusal undid the narrowed rule and the index removal that came before it in the same walk.
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(read_objects(self.connection), target)
        self.assertEqual(self.connection.execute('SELECT count(*) FROM signoff_source_links').fetchone()[0], 1)
        # Going back only past the indexes never needed the links to go.
        self.assertEqual(upgrade.revert(self.connection, self.extensions, to=SOURCES)['held_rows'], {SOURCES: 1})
        result = upgrade.revert(self.connection, self.extensions, to=BASE, discard_rows=True)
        self.assertEqual((result['removed'], result['dropped_rows'], result['store']), ([SOURCES], {SOURCES: 1}, BASE))
        self.assertEqual(read_objects(self.connection), objects)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM signoffs WHERE id='signoff'").fetchone()[0], 1)
        self.assertTrue(self.opens(BASE))

    def test_a_failure_in_a_later_transform_undoes_the_earlier_ones(self):
        before, objects = self.rows(), read_objects(self.connection)
        # One failure while the queue indexes are created, one when the last transform reaches for the schema catalog.
        for denied in ((sqlite3.SQLITE_CREATE_INDEX, 'signoffs_approved_queue'), (sqlite3.SQLITE_PRAGMA, 'writable_schema')):
            self.connection.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
                if (action, name) == denied else sqlite3.SQLITE_OK)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    upgrade.apply(self.connection, self.extensions)
            finally:
                self.connection.set_authorizer(None)
            self.assertFalse(self.connection.in_transaction)
            self.assertEqual(read_objects(self.connection), objects)
            self.assertEqual(self.rows(), before)
            self.assertEqual(upgrade.inspect(self.connection, self.extensions)['store'], BASE)
        self.assertTrue(self.opens(BASE))

    def test_a_failed_revert_leaves_every_transform_in_place(self):
        upgrade.apply(self.connection, self.extensions)
        target = read_objects(self.connection)
        self.connection.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_DROP_TABLE and name == signoff_sources.TABLE else sqlite3.SQLITE_OK)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                upgrade.revert(self.connection, self.extensions, to=BASE)
        finally:
            self.connection.set_authorizer(None)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(read_objects(self.connection), target)
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['store'], SIDE_CHAT)
        # The narrowed rule went back with everything else: a side chat is still accepted.
        self.signoff(self.connection, 'side-chat', 'side_chat')

    def test_a_store_off_the_chain_is_refused_unchanged_with_its_differences_from_every_state(self):
        objects = read_objects(self.connection)
        # Another extension selection composes other states, so the store matches none of them.
        with self.assertRaises(DomainError) as refusal:
            upgrade.apply(self.connection)
        self.assertEqual(refusal.exception.code, 'invalid_database')
        details = refusal.exception.to_dict()['details']
        self.assertEqual(set(details['states']), set(upgrade.STATES))
        self.assertIn('unexpected table synthetic_records', details['states'][BASE])
        self.assertEqual(details['identity'], [['qorqut-v1.2', 1, self.states[0][0].target.digest]])
        self.assertEqual(read_objects(self.connection), objects)
        # The indexes without the source links were never a released state.
        path = self.path.with_name('indexes-only.sqlite3')
        database = Database(path, assembly=signoff_sources.compositions(self.extensions)[0])
        database.initialize_target()
        database.close()
        with sqlite3.connect(path, isolation_level=None) as other:
            found = read_objects(other)
            for command in (upgrade.inspect, upgrade.apply):
                with self.assertRaises(DomainError) as refusal:
                    command(other, self.extensions)
                self.assertIn('unexpected index signoffs_approved_queue', refusal.exception.to_dict()['details']['states'][BASE])
            with self.assertRaises(DomainError):
                upgrade.revert(other, self.extensions, to=BASE)
            self.assertEqual(read_objects(other), found)
        other.close()
        # One object outside the composition is enough, at any state and in either direction.
        self.connection.execute('CREATE INDEX unexpected ON synthetic_records(value)')
        objects = read_objects(self.connection)
        for command in (upgrade.inspect, upgrade.apply, lambda *arguments: upgrade.revert(*arguments, to=BASE)):
            with self.assertRaises(DomainError):
                command(self.connection, self.extensions)
        self.assertEqual(read_objects(self.connection), objects)
        self.connection.execute('DROP INDEX unexpected')
        # The right objects under another identity are not this composition's store either.
        self.connection.execute(f"UPDATE {IDENTITY_TABLE} SET digest='0' WHERE id='target'")
        with self.assertRaises(DomainError) as refusal:
            upgrade.apply(self.connection, self.extensions)
        self.assertIn('changed target identity', refusal.exception.to_dict()['details']['states'][BASE])
        self.assertTrue(set(signoff_sources.OBJECTS).isdisjoint(read_objects(self.connection)))

    def command(self, *arguments, ownership=None):
        with patch.object(upgrade, 'instance_paths_from_arguments', return_value=SimpleNamespace(database=self.path)), \
                patch.object(upgrade, 'NO_EXTENSIONS', self.extensions), \
                patch.object(upgrade, 'InstanceOwnership') as owner, redirect_stdout(io.StringIO()) as output:
            if ownership is not None:
                owner.return_value.acquire.side_effect = ownership
            status = upgrade.main([*arguments, '--instance-root', str(self.path.parent)])
        return status, json.loads(output.getvalue()), owner

    def test_cli_inspect_is_read_only_and_takes_no_ownership(self):
        before, objects = self.rows(), read_objects(self.connection)
        status, response, owner = self.command('inspect')
        self.assertEqual((status, response['ok'], response['result']['store'], response['result']['pending']),
                         (0, True, BASE, [SOURCES, INDEXES, SIDE_CHAT]))
        owner.assert_not_called()
        self.assertEqual((self.rows(), read_objects(self.connection)), (before, objects))

    def test_cli_apply_and_revert_hold_exclusive_ownership_and_release_it(self):
        status, response, owner = self.command('apply')
        self.assertEqual((status, response['result']['added'], response['result']['store']),
                         (0, [SOURCES, INDEXES, SIDE_CHAT], SIDE_CHAT))
        owner.return_value.acquire.assert_called_once_with()
        owner.return_value.acquire.return_value.release.assert_called_once_with()
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['store'], SIDE_CHAT)
        self.link()
        status, response, owner = self.command('revert', '--to', BASE)
        self.assertEqual((status, response['error']['code'], response['error']['details']), (1, 'validation_error', {'held_rows': {SOURCES: 1}}))
        owner.return_value.acquire.return_value.release.assert_called_once_with()
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['store'], SIDE_CHAT)
        status, response, owner = self.command('revert', '--to', BASE, '--discard-rows')
        self.assertEqual((status, response['result']['removed'], response['result']['dropped_rows']),
                         (0, [SOURCES, INDEXES, SIDE_CHAT], {SIDE_CHAT: 0, INDEXES: 0, SOURCES: 1}))
        owner.return_value.acquire.return_value.release.assert_called_once_with()
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['store'], BASE)

    def test_cli_refuses_before_opening_the_store_when_the_instance_is_owned_or_the_request_is_unclear(self):
        objects = read_objects(self.connection)
        with patch.object(upgrade.sqlite3, 'connect') as connect:
            for arguments in (('apply',), ('revert', '--to', BASE)):
                status, response, _ = self.command(*arguments, ownership=DomainError('instance_in_use', 'Service owns instance'))
                self.assertEqual((status, response['error']['code']), (1, 'instance_in_use'))
            for arguments in (('revert',), ('apply', '--to', BASE), ('inspect', '--discard-rows')):
                status, response, owner = self.command(*arguments)
                self.assertEqual((status, response['error']['code']), (1, 'validation_error'))
                owner.assert_not_called()
            connect.assert_not_called()
        self.assertEqual(read_objects(self.connection), objects)


if __name__ == '__main__':
    unittest.main()
