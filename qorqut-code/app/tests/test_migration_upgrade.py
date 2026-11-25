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
from qorqut.migration import project_hierarchy, rule_checks, upgrade
from qorqut.service.changes import ChangeFeed
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.inventory import IDENTITY_TABLE, read_objects
from qorqut_operations.storage.specs import TableSpec
from qorqut_operations.storage.sqlfunctions import install_sql_functions

BASE, RULE_CHECKS, PROJECT_HIERARCHY = upgrade.STATES
# The composition, with no extension selected, of the last build before the first transform of the chain (dev at 24 November
# 2026).
RELEASED_BASE_DIGEST = '6b63091fac1eb65975ae566b61abc66a90145cce931317fcda1262972d602988'
OBSERVATIONS = ('table', 'experience_observations')
PROJECTS = ('table', 'projects')


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
        self.assertEqual(upgrade.STATES, ('base', 'rule_checks', 'project_hierarchy'))
        self.assertEqual(upgrade.chain(ExtensionSet(()))[0][0].target.digest, RELEASED_BASE_DIGEST)


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
        # A rule an agent filed before the owner's check existed.
        self.connection.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES('agent','agent','Ordubashy','now','now')")
        self.connection.execute("INSERT INTO experience_clusters(id,label,created_by,created_at) VALUES('filed-by-agents','Filed by agents','agent','now')")
        self.connection.execute("INSERT INTO experience_observations(id,cluster_id,kind,subject,text,created_by,created_at) "
                                "VALUES('rule','filed-by-agents','owner_complaint','all agents','Never send mail unasked.','agent','now')")
        # A project made before subprojects existed.
        self.connection.execute("INSERT INTO projects(id,slug,workspace,name,created_at,updated_at) VALUES('p1','alpha','/w/alpha','Alpha','now','now')")

    def at_target(self):
        """A second store, made at this build's own target."""
        path = self.path.with_name('target.sqlite3')
        database = Database(path, assembly=self.states[-1][0])
        database.initialize_target()
        database.close()
        connection = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(connection.close)
        install_sql_functions(connection)
        return connection

    def observation(self, connection=None):
        connection = connection or self.connection
        connection.row_factory = sqlite3.Row
        try:
            return dict(connection.execute("SELECT * FROM experience_observations WHERE id='rule'").fetchone())
        finally:
            connection.row_factory = None

    def project(self, connection=None):
        connection = connection or self.connection
        connection.row_factory = sqlite3.Row
        try:
            return dict(connection.execute("SELECT * FROM projects WHERE id='p1'").fetchone())
        finally:
            connection.row_factory = None

    def test_a_store_at_the_base_gets_the_rule_check_columns_and_keeps_every_row(self):
        objects = read_objects(self.connection)
        self.assertEqual(upgrade.inspect(self.connection, self.extensions), {
            'store': BASE, 'applied': [], 'pending': [RULE_CHECKS, PROJECT_HIERARCHY], 'held_rows': {},
            'digest': self.states[0][0].target.digest, 'target_digest': self.states[-1][0].target.digest})
        result = upgrade.apply(self.connection, self.extensions)
        self.assertEqual((result['from'], result['added'], result['store'], result['held_rows']),
                         (BASE, [RULE_CHECKS, PROJECT_HIERARCHY], PROJECT_HIERARCHY, {RULE_CHECKS: 0, PROJECT_HIERARCHY: 0}))
        after = read_objects(self.connection)
        # Only the observations and projects tables changed, and they are exactly the compiled target's definitions.
        self.assertEqual({key for key in objects if after[key] != objects[key]}, {OBSERVATIONS, PROJECTS})
        self.assertEqual(after, dict(self.states[-1][1]) | {key: value for key, value in after.items() if key[1] == 'synthetic_records'})
        self.assertEqual(self.observation(), {'id': 'rule', 'run_id': None, 'cluster_id': 'filed-by-agents', 'kind': 'owner_complaint',
            'subject': 'all agents', 'text': 'Never send mail unasked.', 'evidence_json': '[]', 'occurred_at': None, 'created_by': 'agent',
            'created_at': 'now', 'owner_check': None, 'check_signoff_id': None, 'checked_at': None})
        # The earlier project stays top level, with its folder unchanged.
        self.assertEqual({key: self.project()[key] for key in ('slug', 'workspace', 'parent_id', 'archived_with', 'folder')},
                         {'slug': 'alpha', 'workspace': '/w/alpha', 'parent_id': None, 'archived_with': None, 'folder': None})
        self.assertEqual(self.connection.execute('SELECT value FROM synthetic_records').fetchall(), [('kept',)])
        self.assertEqual(self.connection.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        # The new rule holds on the upgraded store and on one opened afterwards.
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE experience_observations SET owner_check='maybe' WHERE id='rule'")
        other = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(other.close)
        other.execute("UPDATE experience_observations SET owner_check='approved', checked_at='later' WHERE id='rule'")
        self.assertEqual(self.observation()['owner_check'], 'approved')
        other.execute("INSERT INTO projects(id,slug,workspace,name,created_at,updated_at,parent_id,folder) "
                      "VALUES('p2','beta','/w/alpha/subprojects/beta','Beta','now','now','p1','alpha/subprojects/beta')")
        again = upgrade.apply(self.connection, self.extensions)
        self.assertEqual((again['from'], again['added'], again['held_rows']),
                         (PROJECT_HIERARCHY, [], {RULE_CHECKS: 1, PROJECT_HIERARCHY: 1}))
        # A store opened by this build's database at its own target.
        database = Database(self.path, assembly=self.states[-1][0])
        database.close()

    def test_revert_keeps_the_owner_s_marks_unless_told_to_drop_them(self):
        objects = read_objects(self.connection)
        upgrade.apply(self.connection, self.extensions)
        self.connection.execute("UPDATE experience_observations SET owner_check='unchecked', check_signoff_id='card' WHERE id='rule'")
        target = read_objects(self.connection)
        with self.assertRaises(DomainError) as refusal:
            upgrade.revert(self.connection, self.extensions, to=BASE)
        self.assertEqual(refusal.exception.to_dict()['details'], {'held_rows': {RULE_CHECKS: 1}})
        self.assertEqual(read_objects(self.connection), target)
        result = upgrade.revert(self.connection, self.extensions, to=BASE, discard_rows=True)
        self.assertEqual((result['from'], result['removed'], result['dropped_rows'], result['store']),
                         (PROJECT_HIERARCHY, [RULE_CHECKS, PROJECT_HIERARCHY], {PROJECT_HIERARCHY: 0, RULE_CHECKS: 1}, BASE))
        self.assertEqual(read_objects(self.connection), objects)
        # The rule itself stays; only the mark went with its columns. Forward again, it has none.
        self.assertEqual(self.observation()['text'], 'Never send mail unasked.')
        self.assertNotIn('owner_check', self.observation())
        upgrade.apply(self.connection, self.extensions)
        self.assertIsNone(self.observation()['owner_check'])
        # The earlier definition cannot be derived from a table that does not carry the check.
        with self.assertRaises(DomainError):
            rule_checks._earlier('CREATE TABLE experience_observations (\n id TEXT\n)')
        with self.assertRaises(DomainError):
            project_hierarchy._earlier('CREATE TABLE projects (\n id TEXT\n)')

    def test_revert_of_the_hierarchy_keeps_subprojects_unless_told_to_drop_them(self):
        upgrade.apply(self.connection, self.extensions)
        self.connection.execute("INSERT INTO projects(id,slug,workspace,name,created_at,updated_at,parent_id,folder) "
                                "VALUES('p2','beta','/w/alpha/subprojects/beta','Beta','now','now','p1','alpha/subprojects/beta')")
        with self.assertRaises(DomainError) as refusal:
            upgrade.revert(self.connection, self.extensions, to=RULE_CHECKS)
        self.assertEqual(refusal.exception.to_dict()['details'], {'held_rows': {PROJECT_HIERARCHY: 1}})
        result = upgrade.revert(self.connection, self.extensions, to=RULE_CHECKS, discard_rows=True)
        self.assertEqual((result['from'], result['removed'], result['dropped_rows'], result['store']),
                         (PROJECT_HIERARCHY, [PROJECT_HIERARCHY], {PROJECT_HIERARCHY: 1}, RULE_CHECKS))
        self.assertNotIn('parent_id', self.project())
        # Both projects remain, now as an earlier build sees them.
        self.assertEqual(self.connection.execute('SELECT count(*) FROM projects').fetchone()[0], 2)
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['pending'], [PROJECT_HIERARCHY])
        upgrade.apply(self.connection, self.extensions)
        self.assertIsNone(self.project()['parent_id'])

    def test_a_store_at_the_target_is_recognised_and_left_as_it_is(self):
        connection = self.at_target()
        connection.execute("INSERT INTO synthetic_records(id,value) VALUES('test','kept')")
        objects = read_objects(connection)
        digest = self.states[-1][0].target.digest
        self.assertEqual(upgrade.inspect(connection, self.extensions), {
            'store': PROJECT_HIERARCHY, 'applied': [RULE_CHECKS, PROJECT_HIERARCHY], 'pending': [],
            'held_rows': {RULE_CHECKS: 0, PROJECT_HIERARCHY: 0}, 'digest': digest, 'target_digest': digest})
        result = upgrade.apply(connection, self.extensions)
        self.assertEqual((result['from'], result['added'], result['store']), (PROJECT_HIERARCHY, [], PROJECT_HIERARCHY))
        self.assertEqual(read_objects(connection), objects)
        # Walked back, the base it reaches is exactly the one an earlier build creates.
        result = upgrade.revert(connection, self.extensions, to=BASE)
        self.assertEqual((result['from'], result['removed'], result['dropped_rows']),
                         (PROJECT_HIERARCHY, [RULE_CHECKS, PROJECT_HIERARCHY], {PROJECT_HIERARCHY: 0, RULE_CHECKS: 0}))
        self.assertEqual(read_objects(connection), read_objects(self.connection))
        self.assertEqual(connection.execute('SELECT value FROM synthetic_records').fetchall(), [('kept',)])

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
        self.assertEqual((status, response['ok'], response['result']['store'], response['result']['pending']),
                         (0, True, BASE, [RULE_CHECKS, PROJECT_HIERARCHY]))
        owner.assert_not_called()
        self.assertEqual(read_objects(self.connection), objects)

    def test_cli_apply_and_revert_hold_exclusive_ownership_and_release_it(self):
        for arguments, store in ((('apply',), PROJECT_HIERARCHY), (('revert', '--to', BASE), BASE)):
            status, response, owner = self.command(*arguments)
            self.assertEqual((status, response['ok'], response['result']['store']), (0, True, store))
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
