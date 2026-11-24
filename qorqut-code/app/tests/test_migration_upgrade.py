"""The ordered offline upgrade recognises a store by its state, refuses any other, and runs its command with the right ownership."""
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
from qorqut.migration import upgrade
from qorqut.service.changes import ChangeFeed
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.inventory import IDENTITY_TABLE, read_objects
from qorqut_operations.storage.specs import TableSpec
from qorqut_operations.storage.sqlfunctions import install_sql_functions

BASE, = upgrade.STATES


class SyntheticExtension(Extension):
    id = 'synthetic'

    def storage(self):
        return (StorageContribution('extension.synthetic', {'synthetic_records': TableSpec(('id',))},
                schema=('CREATE TABLE synthetic_records(id TEXT PRIMARY KEY,value TEXT,revision INTEGER DEFAULT 1)',)),)


class ChainTests(unittest.TestCase):
    def test_the_chain_ends_at_this_build_s_own_target(self):
        self.assertEqual(upgrade.STATES, ('base', *(transform.NAME for transform in upgrade.TRANSFORMS)))
        for extensions in (ExtensionSet(()), ExtensionSet((SyntheticExtension(),))):
            states = upgrade.chain(extensions)
            self.assertEqual(len(states), len(upgrade.STATES))
            self.assertEqual(states[-1][0].target.digest, host_assembly(ChangeFeed(), extensions).target.digest)
            for transform, (_, old), (_, new) in zip(upgrade.TRANSFORMS, states, states[1:]):
                self.assertEqual(set(new) - set(old), set(transform.OBJECTS))
                self.assertEqual({key for key in old if new[key] != old[key]}, set(getattr(transform, 'CHANGED', ())))
            self.assertEqual(len({assembly.target.digest for assembly, _ in states}), len(upgrade.STATES))


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name).resolve() / 'operations.sqlite3'
        self.extensions = ExtensionSet((SyntheticExtension(),))
        self.states = upgrade.chain(self.extensions)
        database = Database(self.path, assembly=self.states[0][0])
        database.initialize_target()
        database.close()
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.connection.close)
        install_sql_functions(self.connection)
        self.connection.execute("INSERT INTO synthetic_records(id,value) VALUES('test','kept')")

    def test_a_store_at_the_target_is_recognised_and_left_as_it_is(self):
        objects = read_objects(self.connection)
        digest = self.states[0][0].target.digest
        self.assertEqual(upgrade.inspect(self.connection, self.extensions), {
            'store': BASE, 'applied': [], 'pending': [], 'held_rows': {}, 'digest': digest, 'target_digest': digest})
        result = upgrade.apply(self.connection, self.extensions)
        self.assertEqual((result['from'], result['added'], result['store']), (BASE, [], BASE))
        result = upgrade.revert(self.connection, self.extensions, to=BASE)
        self.assertEqual((result['from'], result['removed'], result['dropped_rows']), (BASE, [], {}))
        self.assertEqual(read_objects(self.connection), objects)
        self.assertEqual(self.connection.execute('SELECT value FROM synthetic_records').fetchall(), [('kept',)])

    def test_an_unknown_state_to_revert_to_is_refused(self):
        with self.assertRaises(DomainError) as refusal:
            upgrade.revert(self.connection, self.extensions, to='missing')
        self.assertEqual(refusal.exception.code, 'validation_error')

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
        # One object outside the composition is enough, in either direction.
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

    def command(self, *arguments, ownership=None):
        with patch.object(upgrade, 'instance_paths_from_arguments', return_value=SimpleNamespace(database=self.path)), \
                patch.object(upgrade, 'NO_EXTENSIONS', self.extensions), \
                patch.object(upgrade, 'InstanceOwnership') as owner, redirect_stdout(io.StringIO()) as output:
            if ownership is not None:
                owner.return_value.acquire.side_effect = ownership
            status = upgrade.main([*arguments, '--instance-root', str(self.path.parent)])
        return status, json.loads(output.getvalue()), owner

    def test_cli_inspect_is_read_only_and_takes_no_ownership(self):
        objects = read_objects(self.connection)
        status, response, owner = self.command('inspect')
        self.assertEqual((status, response['ok'], response['result']['store'], response['result']['pending']), (0, True, BASE, []))
        owner.assert_not_called()
        self.assertEqual(read_objects(self.connection), objects)

    def test_cli_apply_and_revert_hold_exclusive_ownership_and_release_it(self):
        for arguments in (('apply',), ('revert', '--to', BASE)):
            status, response, owner = self.command(*arguments)
            self.assertEqual((status, response['ok'], response['result']['store']), (0, True, BASE))
            owner.return_value.acquire.assert_called_once_with()
            owner.return_value.acquire.return_value.release.assert_called_once_with()

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
