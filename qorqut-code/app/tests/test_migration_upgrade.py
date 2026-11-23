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
from qorqut.migration import compactions, experience, list_indexes, owner_turns, queue_indexes, record_moves, report_tasks, signoff_side_chat, signoff_sources, timeline, timeline_deliveries, upgrade
from qorqut.service.changes import ChangeFeed
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.inventory import IDENTITY_TABLE, read_objects
from qorqut_operations.storage.specs import TableSpec
from qorqut_operations.storage.sqlfunctions import install_sql_functions

# The composition, with no extension selected, of the last release before the first transform of the chain.
RELEASED_BASE_DIGEST = '72ee676d38b3135dc613aefa6f86e379293f05d6fb9a7c4ab7b1cea596e19ee3'
BASE, SOURCES, INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS = upgrade.STATES
SIGNOFFS = ('table', 'signoffs')


class SyntheticExtension(Extension):
    id = 'synthetic'

    def storage(self):
        return (StorageContribution('extension.synthetic', {'synthetic_records': TableSpec(('id',))},
                schema=('CREATE TABLE synthetic_records(id TEXT PRIMARY KEY,value TEXT,revision INTEGER DEFAULT 1)',)),)


class SignoffChildExtension(Extension):
    """An extension table that refers to a Signoff and guards its rows against deletion, as Telegram's does."""
    id = 'signoff_child'

    def storage(self):
        return (StorageContribution('extension.signoff_child', {'signoff_marks': TableSpec(('id',))}, schema=(
            'CREATE TABLE signoff_marks(id TEXT PRIMARY KEY,signoff_id TEXT NOT NULL REFERENCES signoffs(id),revision INTEGER DEFAULT 1)',
            "CREATE TRIGGER signoff_marks_keep BEFORE DELETE ON signoff_marks BEGIN SELECT RAISE(ABORT,'Marks are kept'); END")),)


class ReadPairExtension(Extension):
    """An extension table that refers to a private read position by both of its key columns."""
    id = 'read_pair'

    def storage(self):
        return (StorageContribution('extension.read_pair', {'read_pairs': TableSpec(('id',))}, schema=(
            'CREATE TABLE read_pairs(id TEXT PRIMARY KEY,conversation_id TEXT,principal_id TEXT,revision INTEGER DEFAULT 1,'
            'FOREIGN KEY(conversation_id,principal_id) REFERENCES owner_conversation_reads(conversation_id,principal_id))',)),)


class ChainTests(unittest.TestCase):
    def test_states_run_from_the_released_base_to_this_build_in_release_order(self):
        self.assertEqual(upgrade.STATES, ('base', 'signoff_sources', 'queue_indexes', 'signoff_side_chat', 'side_chat_titles', 'owner_turns', 'private_message_refs', 'report_tasks', 'timeline', 'timeline_deliveries', 'record_moves', 'native_transport_managed', 'compactions', 'experience', 'side_chats_removed', 'list_indexes'))
        for extensions in (ExtensionSet(()), ExtensionSet((SyntheticExtension(),))):
            states = upgrade.chain(extensions)
            self.assertEqual(states[-1][0].target.digest, host_assembly(ChangeFeed(), extensions).target.digest)
            for transform, (_, old), (_, new) in zip(upgrade.TRANSFORMS, states, states[1:]):
                self.assertEqual(set(new) - set(old), set(transform.OBJECTS))
                self.assertEqual({key for key in old if new[key] != old[key]}, set(getattr(transform, 'CHANGED', ())))
            # The category transform only widens the rule; the following transform appends the display title.
            side_chat, titles = upgrade.STATES.index(SIDE_CHAT), upgrade.STATES.index(TITLES)
            self.assertEqual(states[titles - 1][1][SIGNOFFS][1].replace(",'side_chat'", ''), states[side_chat - 1][1][SIGNOFFS][1])
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
        self.path = Path(directory.name).resolve() / 'operations.sqlite3'
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
        # Compare retained columns across schemas; the new nullable title is checked separately.
        rows = {}
        for kind, name in read_objects(connection):
            if kind != 'table' or name == IDENTITY_TABLE:
                continue
            columns = ','.join('"' + column[1] + '"' for column in connection.execute(f'PRAGMA table_info("{name}")')
                               if (name, column[1]) not in {('signoffs', 'display_title'), ('owner_conversation_messages', 'refs_json')})
            rows[name] = connection.execute(f'SELECT {columns} FROM "{name}"').fetchall()
        return rows

    @staticmethod
    def kept(rows, earlier):
        """``rows`` of the tables ``earlier`` already had: a later state may add an empty table of its own."""
        return {name: rows[name] for name in earlier}

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
        self.assertFalse(self.opens(LISTS))
        self.assertEqual(read_objects(self.connection), objects)
        self.assertEqual(upgrade.inspect(self.connection, self.extensions), {
            'store': BASE, 'applied': [], 'pending': [SOURCES, INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], 'held_rows': {},
            'digest': self.states[0][0].target.digest, 'target_digest': self.states[-1][0].target.digest})

        result = upgrade.apply(self.connection, self.extensions)

        self.assertEqual((result['from'], result['added'], result['store'], result['pending']),
                         (BASE, [SOURCES, INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], LISTS, []))
        self.assertEqual(result['digest'], self.states[-1][0].target.digest)
        self.assertEqual(set(read_objects(self.connection)) - set(objects), set(signoff_sources.OBJECTS) | set(queue_indexes.OBJECTS) | set(owner_turns.OBJECTS) | set(report_tasks.OBJECTS) | set(timeline.OBJECTS) | set(timeline_deliveries.OBJECTS) | set(record_moves.OBJECTS) | set(compactions.OBJECTS) | set(experience.OBJECTS) | set(list_indexes.OBJECTS))
        after = self.rows()
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(after[signoff_sources.TABLE], [])
        self.assertEqual(self.connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertTrue(self.opens(LISTS))
        # Both single commands see their own target, and a second run changes nothing.
        self.assertEqual(signoff_sources.inspect(self.connection, self.extensions)['store'], 'target')
        self.assertEqual(queue_indexes.inspect(self.connection, self.extensions)['store'], 'target')
        again = upgrade.apply(self.connection, self.extensions)
        self.assertEqual((again['from'], again['added']), (LISTS, []))
        self.assertEqual(self.rows(), after)

    def test_a_store_part_of_the_way_along_gets_only_the_rest(self):
        path = self.path.with_name('sources.sqlite3')
        connection = self.store(SOURCES, path)
        self.link(connection)
        before = self.rows(connection)
        report = upgrade.inspect(connection, self.extensions)
        self.assertEqual((report['store'], report['applied'], report['pending'], report['held_rows']),
                         (SOURCES, [SOURCES], [INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], {SOURCES: 1}))
        # The queue-index command alone starts from a store that already accepts side chats, which no release
        # made; with a later transform in the chain, only the chain recognises this one.
        with self.assertRaises(DomainError):
            queue_indexes.inspect(connection, self.extensions)
        result = upgrade.apply(connection, self.extensions)
        self.assertEqual((result['from'], result['added'], result['held_rows']),
                         (SOURCES, [INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], {SOURCES: 1, INDEXES: 0, SIDE_CHAT: 0, TITLES: 0, TURNS: 0, REFS: 0, REPORTS: 0, TIMELINE: 0, DELIVERIES: 0, MOVES: 0, MANAGED: 0, COMPACTIONS: 0, EXPERIENCE: 0, REMOVED: 0, LISTS: 0}))
        self.assertEqual(self.kept(self.rows(connection), before), before)
        self.assertTrue(self.opens(LISTS, path))

    def test_side_chats_were_accepted_only_between_their_step_and_their_removal(self):
        path = self.path.with_name('indexes.sqlite3')
        connection = self.store(INDEXES, path)
        for category in ('platform_action', 'input_request'):
            self.signoff(connection, category, category)
        with self.assertRaises(sqlite3.IntegrityError):
            self.signoff(connection, 'side-chat', 'side_chat')
        before, objects = self.rows(connection), read_objects(connection)

        result = upgrade.apply(connection, self.extensions)

        self.assertEqual((result['from'], result['added'], result['held_rows'][SIDE_CHAT]), (INDEXES, [SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], 0))
        # Two stored definitions and the two move guards are the whole change to what was there (the private messages'
        # references and the native session transport rule; the Signoff rule is back where it was): only empty tables and
        # indexes came, and no row moved.
        after = read_objects(connection)
        self.assertEqual(set(after) - set(objects), set(owner_turns.OBJECTS) | set(report_tasks.OBJECTS) | set(timeline.OBJECTS) | set(timeline_deliveries.OBJECTS) | set(record_moves.OBJECTS) | set(compactions.OBJECTS) | set(experience.OBJECTS) | set(list_indexes.OBJECTS))
        self.assertEqual({key for key in objects if after[key] != objects[key]}, {('table', 'owner_conversation_messages'), ('table', 'native_sessions'), *record_moves.CHANGED})
        self.assertEqual(self.kept(self.rows(connection), before), before)
        self.assertEqual(connection.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
        self.assertEqual(connection.execute('PRAGMA writable_schema').fetchone()[0], 0)
        self.assertTrue(self.opens(LISTS, path))
        with self.assertRaises(sqlite3.IntegrityError):
            self.signoff(connection, 'side-chat', 'side_chat')
        # Back before the removal, the connection that reverted and one opened afterwards both accept a side chat, and only that.
        upgrade.revert(connection, self.extensions, to=EXPERIENCE)
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
        upgrade.revert(self.connection, self.extensions, to=EXPERIENCE)
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
        self.assertEqual((result['removed'], result['dropped_rows'], result['store']), ([SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE], {TURNS: 0, REFS: 0, REPORTS: 0, TIMELINE: 0, DELIVERIES: 0, MOVES: 0, MANAGED: 0, COMPACTIONS: 0, EXPERIENCE: 0, TITLES: 0, SIDE_CHAT: 1}, INDEXES))
        self.assertEqual(self.connection.execute('SELECT id FROM signoffs').fetchall(), [('signoff',)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.signoff(self.connection, 'side-chat', 'side_chat')
        self.assertTrue(self.opens(INDEXES))

    def test_names_were_editable_before_the_removal_and_revert_without_losing_the_conversation(self):
        connection = self.connection
        upgrade.apply(connection, self.extensions)
        upgrade.revert(connection, self.extensions, to=EXPERIENCE)
        self.signoff(connection, 'side-chat', 'side_chat')
        connection.execute("INSERT INTO owner_conversations(id,source_key,source_type,source_id,audience,created_at,updated_at) "
                           "VALUES('chat','signoff:global:side-chat','signoff','side-chat','owner_ordubashy','now','now')")
        connection.execute("INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,created_at) "
                           "VALUES('message','chat','agent','owner','Keep this message','now')")
        self.assertIsNone(connection.execute("SELECT display_title FROM signoffs WHERE id='side-chat'").fetchone()[0])
        before = self.rows(connection)
        connection.execute("UPDATE signoffs SET display_title='New name' WHERE id='side-chat'")
        renamed = self.rows(connection)
        for name in ('signoffs', 'owner_conversations', 'owner_conversation_messages', 'owner_conversation_reads'):
            self.assertEqual(renamed[name], before[name])
        self.assertEqual(upgrade.inspect(connection, self.extensions)['held_rows'][TITLES], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE signoffs SET display_title='An approval' WHERE id='signoff'")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE signoffs SET preview_json='{}' WHERE id='side-chat'")
        with self.assertRaises(DomainError):
            upgrade.revert(connection, self.extensions, to=SIDE_CHAT)
        self.assertEqual(connection.execute("SELECT display_title FROM signoffs WHERE id='side-chat'").fetchone()[0], 'New name')
        result = upgrade.revert(connection, self.extensions, to=SIDE_CHAT, discard_rows=True)
        self.assertEqual((result['removed'], result['dropped_rows']), ([TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE], {TURNS: 0, REFS: 0, REPORTS: 0, TIMELINE: 0, DELIVERIES: 0, MOVES: 0, MANAGED: 0, COMPACTIONS: 0, EXPERIENCE: 0, TITLES: 1}))
        reverted = self.rows(connection)
        self.assertEqual(set(renamed) - set(reverted), {owner_turns.TABLE, report_tasks.TABLE, *timeline.TABLES, timeline_deliveries.TABLE, *record_moves.TABLES, compactions.TABLE, *experience.TABLES})
        self.assertEqual(self.kept(renamed, reverted), reverted)
        self.assertTrue(self.opens(SIDE_CHAT))
        # Upgrading again walks over the names and then deletes the side chat with its conversation.
        result = upgrade.apply(connection, self.extensions)
        self.assertEqual((result['from'], result['added']), (SIDE_CHAT, [TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS]))
        self.assertEqual(connection.execute('SELECT id FROM signoffs').fetchall(), [('signoff',)])
        self.assertEqual(connection.execute('SELECT count(*) FROM owner_conversation_messages').fetchone()[0], 0)
        self.assertTrue(self.opens(LISTS))

    def test_a_definition_that_stored_rows_break_is_refused(self):
        upgrade.apply(self.connection, self.extensions)
        upgrade.revert(self.connection, self.extensions, to=EXPERIENCE)
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

    def use(self, *extensions):
        """Compose the states with these extensions besides the synthetic one, for the stores made after this call."""
        self.extensions = ExtensionSet((SyntheticExtension(), *extensions))
        self.states = upgrade.chain(self.extensions)

    @staticmethod
    def event(connection, source_type, source_id):
        event = connection.execute("INSERT INTO inbox_events(source_type,source_id,actor_id,event_type,summary,created_at) "
                                   "VALUES(?,?,'agent','changed','Changed','now')", (source_type, source_id)).lastrowid
        connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES('agent',?)", (event,))
        return event

    @staticmethod
    def receipt(connection, operation, key, data):
        connection.execute("INSERT INTO idempotency_receipts(principal_id,target_namespace,operation_name,idempotency_key,"
                           "input_hash,result_json,created_at) VALUES('agent','global',?,?,'hash',?,'now')",
                           (operation, key, json.dumps({'ok': True, 'data': data})))

    def conversation(self, connection, identity, source_type, source_id, body):
        """A private owner conversation with one message, a read position, its announcement and its two receipts."""
        connection.execute("INSERT INTO owner_conversations(id,source_key,source_type,source_id,audience,latest_sequence,"
                           "created_at,updated_at) VALUES(?,?,?,?,'owner_ordubashy',1,'now','now')",
                           (identity, f'{source_type}:global:{source_id}', source_type, source_id))
        connection.execute("INSERT INTO owner_conversation_messages(id,conversation_id,created_by,author_role,body,created_at) "
                           "VALUES(?,?,'agent','owner',?,'now')", (identity + '-message', identity, body))
        connection.execute("INSERT INTO owner_conversation_reads(conversation_id,principal_id,last_read_sequence,updated_at) "
                           "VALUES(?,'agent',1,'now')", (identity,))
        self.event(connection, 'owner_conversations', identity)
        self.receipt(connection, 'owner_conversation_send', identity + '-send',
                     {'message': {'id': identity + '-message', 'body': body}, 'conversation': {'id': identity, 'agent_id': None}})
        self.receipt(connection, 'owner_conversation_mark_read', identity + '-read',
                     {'read_state': {'last_read_sequence': 1}, 'conversation': {'id': identity, 'agent_id': None}})

    def ordinary(self, connection):
        """An ordinary Signoff with its own private conversation and a Task thread, each with events and receipts."""
        connection.execute("INSERT INTO agent_profiles(agent_id,principal_id,namespace,name,workspace,profile_json,created_at,"
                           "updated_at) VALUES('agent','agent','agent','Agent','agent','{}','now','now')")
        self.conversation(connection, 'approval-chat', 'signoff', 'signoff', 'About the approval')
        self.conversation(connection, 'task-chat', 'task', 'task-1', 'About the Task')
        self.event(connection, 'signoffs', 'signoff')
        connection.execute("INSERT INTO tasks(id,title,status,created_by,created_at,updated_at) VALUES('task-1','A Task','open','agent','now','now')")
        event = self.event(connection, 'tasks', 'task-1')
        connection.execute("INSERT INTO thread_messages(id,item_type,item_id,body,author_id,recorder_id,event_id,created_at) "
                           "VALUES('comment','task','task-1','A Task comment','agent','agent',?,'now')", (event,))
        connection.execute("INSERT INTO signoff_notes(id,signoff_id,body,created_by,created_at) VALUES('note','signoff','A note','agent','now')")
        self.receipt(connection, 'tasks_create', 'task', {'task': {'id': 'task-1', 'from': 'side-chat'}})

    def side_chat(self, connection):
        """A side chat with everything that holds it: messages, read position, events, receipts, a note, attention and a mark."""
        self.signoff(connection, 'side-chat', 'side_chat')
        connection.execute("UPDATE signoffs SET display_title='Named' WHERE id='side-chat'")
        self.conversation(connection, 'side-talk', 'signoff', 'side-chat', 'Said in a side chat')
        self.event(connection, 'signoffs', 'side-chat')
        self.receipt(connection, 'side_chats_create', 'opened', {'signoff': {'id': 'side-chat'}})
        self.receipt(connection, 'side_chats_rename', 'renamed', {'signoff': {'id': 'side-chat'}})
        connection.execute("INSERT INTO signoff_notes(id,signoff_id,body,created_by,created_at) VALUES('side-note','side-chat','A note','agent','now')")
        connection.execute("INSERT INTO signoff_attention(signoff_id,updated_at) VALUES('side-chat','now')")
        connection.execute("INSERT INTO signoff_marks(id,signoff_id) VALUES('mark','side-chat')")

    def test_the_removal_deletes_every_side_chat_and_leaves_every_other_row_as_it_was(self):
        self.use(SignoffChildExtension())
        connection, plain = self.store(EXPERIENCE, self.path.with_name('side-chats.sqlite3')), self.store(EXPERIENCE, self.path.with_name('plain.sqlite3'))
        for each in (connection, plain):
            self.ordinary(each)
            each.execute("INSERT INTO signoff_marks(id,signoff_id) VALUES('kept','signoff')")
        self.side_chat(connection)
        objects, changes, untouched = read_objects(connection), connection.execute('SELECT * FROM inbox_changes').fetchall(), self.rows(plain)
        self.assertEqual(connection.execute("SELECT count(*) FROM idempotency_receipts").fetchone()[0], 9)

        result = upgrade.apply(connection, self.extensions)

        self.assertEqual((result['from'], result['added'], result['pending'], result['held_rows'][REMOVED]), (EXPERIENCE, [REMOVED, LISTS], [], 0))
        # Only the side-chat rows went: every other row is exactly the row of the same store made without them.
        # A store without side chats upgrades with every row as it was.
        self.assertEqual(upgrade.apply(plain, self.extensions)['added'], [REMOVED, LISTS])
        after, expected = self.rows(connection), self.rows(plain)
        self.assertEqual(expected, untouched)
        self.assertEqual({name: rows for name, rows in after.items() if name != 'inbox_changes'},
                         {name: rows for name, rows in expected.items() if name != 'inbox_changes'})
        self.assertEqual(connection.execute('SELECT * FROM inbox_changes').fetchall(), changes)
        # The same objects stand, triggers included, and only the Signoff definition changed: three categories, no title.
        found = read_objects(connection)
        self.assertEqual({key for key in objects if found.get(key) != objects[key]}, {SIGNOFFS})
        # The indexes of the step after it stand beside them.
        self.assertEqual(set(found) - set(objects), set(list_indexes.OBJECTS))
        self.assertEqual(set(objects) - set(found), set())
        self.assertNotIn('display_title', [column[1] for column in connection.execute('PRAGMA table_info(signoffs)')])
        with self.assertRaises(sqlite3.IntegrityError):
            self.signoff(connection, 'side-chat', 'side_chat')
        for statement in ("DELETE FROM owner_conversation_messages", "DELETE FROM inbox_events", "DELETE FROM signoff_notes",
                          "DELETE FROM signoff_marks"):
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(statement)
        self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(upgrade.inspect(connection, self.extensions)['pending'], [])
        self.assertTrue(self.opens(LISTS, self.path.with_name('side-chats.sqlite3')))

    def test_the_removal_reverts_to_the_earlier_definition_without_the_side_chats_and_applies_again(self):
        self.use(SignoffChildExtension())
        connection = self.store(EXPERIENCE, self.path.with_name('side-chats.sqlite3'))
        self.ordinary(connection)
        self.side_chat(connection)
        upgrade.apply(connection, self.extensions)
        result = upgrade.revert(connection, self.extensions, to=EXPERIENCE)
        self.assertEqual((result['removed'], result['dropped_rows'], result['store']), ([REMOVED, LISTS], {REMOVED: 0, LISTS: 0}, EXPERIENCE))
        self.assertTrue(self.opens(EXPERIENCE, self.path.with_name('side-chats.sqlite3')))
        self.assertEqual(connection.execute("SELECT count(*) FROM signoffs WHERE category='side_chat'").fetchone()[0], 0)
        self.side_chat(connection)
        self.assertEqual(upgrade.apply(connection, self.extensions)['added'], [REMOVED, LISTS])
        self.assertEqual(connection.execute("SELECT count(*) FROM owner_conversations WHERE source_id='side-chat'").fetchone()[0], 0)

    def test_the_indexes_revert_alone_and_the_removal_reverts_after_them(self):
        self.use(SignoffChildExtension())
        connection = self.store(EXPERIENCE, self.path.with_name('side-chats.sqlite3'))
        upgrade.apply(connection, self.extensions)
        target = read_objects(connection)
        result = upgrade.revert(connection, self.extensions, to=REMOVED)
        self.assertEqual((result['removed'], result['store']), ([LISTS], REMOVED))
        self.assertEqual(set(target) - set(read_objects(connection)), set(list_indexes.OBJECTS))
        self.assertEqual({key for key, value in read_objects(connection).items() if target[key] != value}, set())
        self.assertTrue(self.opens(REMOVED, self.path.with_name('side-chats.sqlite3')))
        self.assertFalse(self.opens(LISTS, self.path.with_name('side-chats.sqlite3')))
        result = upgrade.revert(connection, self.extensions, to=EXPERIENCE)
        self.assertEqual((result['removed'], result['store']), ([REMOVED], EXPERIENCE))
        self.assertTrue(self.opens(EXPERIENCE, self.path.with_name('side-chats.sqlite3')))
        self.assertEqual(upgrade.apply(connection, self.extensions)['added'], [REMOVED, LISTS])

    def test_a_reference_by_several_columns_into_a_deleted_table_refuses_the_whole_upgrade(self):
        self.use(ReadPairExtension())
        connection = self.store(EXPERIENCE, self.path.with_name('pairs.sqlite3'))
        self.ordinary(connection)
        objects, before = read_objects(connection), self.rows(connection)
        with self.assertRaises(DomainError) as refusal:
            upgrade.apply(connection, self.extensions)
        self.assertIn('read_pairs', str(refusal.exception))
        self.assertFalse(connection.in_transaction)
        self.assertEqual((read_objects(connection), self.rows(connection)), (objects, before))
        self.assertEqual(upgrade.inspect(connection, self.extensions)['store'], EXPERIENCE)

    def test_revert_walks_back_one_state_at_a_time_or_all_the_way_and_the_earlier_release_opens_the_store(self):
        before, objects = self.rows(), read_objects(self.connection)
        upgrade.apply(self.connection, self.extensions)
        result = upgrade.revert(self.connection, self.extensions, to=INDEXES)
        self.assertEqual((result['from'], result['removed'], result['store'], result['dropped_rows']),
                         (LISTS, [SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], INDEXES, {TURNS: 0, REFS: 0, REPORTS: 0, TIMELINE: 0, DELIVERIES: 0, MOVES: 0, MANAGED: 0, COMPACTIONS: 0, EXPERIENCE: 0, REMOVED: 0, LISTS: 0, TITLES: 0, SIDE_CHAT: 0}))
        self.assertTrue(self.opens(INDEXES))
        self.assertFalse(self.opens(LISTS))
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
        for later in (SOURCES, INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, 'unknown'):
            with self.assertRaises(DomainError) as refusal:
                upgrade.revert(self.connection, self.extensions, to=later)
            self.assertEqual(refusal.exception.code, 'validation_error')
        self.assertEqual(read_objects(self.connection), objects)
        # Straight from the target to the base in one transaction, then forward again.
        upgrade.apply(self.connection, self.extensions)
        self.assertEqual(upgrade.revert(self.connection, self.extensions, to=BASE)['removed'], [SOURCES, INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS])
        self.assertEqual(read_objects(self.connection), objects)
        self.assertEqual(upgrade.apply(self.connection, self.extensions)['added'], [SOURCES, INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS])

    def test_revert_keeps_recorded_source_links_unless_told_to_drop_them(self):
        objects = read_objects(self.connection)
        upgrade.apply(self.connection, self.extensions)
        self.link()
        target = read_objects(self.connection)
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['held_rows'], {SOURCES: 1, INDEXES: 0, SIDE_CHAT: 0, TITLES: 0, TURNS: 0, REFS: 0, REPORTS: 0, TIMELINE: 0, DELIVERIES: 0, MOVES: 0, MANAGED: 0, COMPACTIONS: 0, EXPERIENCE: 0, REMOVED: 0, LISTS: 0})
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

    def test_revert_keeps_delivery_records_unless_told_to_drop_them(self):
        upgrade.apply(self.connection, self.extensions)
        self.connection.execute('PRAGMA foreign_keys=OFF')
        self.connection.execute("INSERT INTO timeline_deliveries(agent_id,conversation_id,item_type,item_id,business_id,revision,delivered_at) "
                                "VALUES('agent','conversation','global','global','',1,'now')")
        target = read_objects(self.connection)
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['held_rows'][DELIVERIES], 1)
        with self.assertRaises(DomainError) as refusal:
            upgrade.revert(self.connection, self.extensions, to=TIMELINE)
        self.assertEqual(refusal.exception.to_dict()['details'], {'held_rows': {DELIVERIES: 1}})
        self.assertEqual(read_objects(self.connection), target)
        result = upgrade.revert(self.connection, self.extensions, to=TIMELINE, discard_rows=True)
        self.assertEqual((result['removed'], result['dropped_rows'], result['store']), ([DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], {DELIVERIES: 1, MOVES: 0, MANAGED: 0, COMPACTIONS: 0, EXPERIENCE: 0, REMOVED: 0, LISTS: 0}, TIMELINE))
        self.assertTrue(self.opens(TIMELINE))
        self.assertFalse(self.opens(DELIVERIES))
        self.assertEqual(set(target) - set(read_objects(self.connection)), set(timeline_deliveries.OBJECTS) | set(record_moves.OBJECTS) | set(compactions.OBJECTS) | set(experience.OBJECTS) | set(list_indexes.OBJECTS))

    def test_revert_of_record_moves_restores_both_guards_and_keeps_recorded_moves_unless_told(self):
        before = read_objects(self.connection)
        upgrade.apply(self.connection, self.extensions)
        self.connection.execute('PRAGMA foreign_keys=OFF')
        self.connection.execute("INSERT INTO table_moves(move_id,table_id,source_business_id,destination_business_id,revision_before,"
                                "actor_id,moved_at) VALUES(?,?,?,?,1,'actor','now')", ('m' * 36, 'table', 'a', 'b'))
        with self.assertRaises(DomainError):
            upgrade.revert(self.connection, self.extensions, to=DELIVERIES)
        result = upgrade.revert(self.connection, self.extensions, to=DELIVERIES, discard_rows=True)
        self.assertEqual((result['removed'], result['dropped_rows']), ([MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], {MOVES: 1, MANAGED: 0, COMPACTIONS: 0, EXPERIENCE: 0, REMOVED: 0, LISTS: 0}))
        self.assertTrue(self.opens(DELIVERIES))
        reverted = read_objects(self.connection)
        for name in (record_moves.THREAD_TRIGGER, record_moves.CRM_TRIGGER):
            self.assertEqual(reverted[('trigger', name)], before[('trigger', name)])

    def test_a_failure_in_a_later_transform_undoes_the_earlier_ones(self):
        before, objects = self.rows(), read_objects(self.connection)
        # Fail index creation, category replacement, and display-title addition independently.
        for denied in ((sqlite3.SQLITE_CREATE_INDEX, 'signoffs_approved_queue'), (sqlite3.SQLITE_PRAGMA, 'writable_schema'),
                       (sqlite3.SQLITE_ALTER_TABLE, 'main')):
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
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['store'], LISTS)
        # The widened rule was undone with everything else: a side chat is still refused.
        with self.assertRaises(sqlite3.IntegrityError):
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
                self.assertIn('unexpected index apply_receipts_recovery', refusal.exception.to_dict()['details']['states'][BASE])
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
                         (0, True, BASE, [SOURCES, INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS]))
        owner.assert_not_called()
        self.assertEqual((self.rows(), read_objects(self.connection)), (before, objects))

    def test_cli_apply_and_revert_hold_exclusive_ownership_and_release_it(self):
        status, response, owner = self.command('apply')
        self.assertEqual((status, response['result']['added'], response['result']['store']),
                         (0, [SOURCES, INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], LISTS))
        owner.return_value.acquire.assert_called_once_with()
        owner.return_value.acquire.return_value.release.assert_called_once_with()
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['store'], LISTS)
        self.link()
        status, response, owner = self.command('revert', '--to', BASE)
        self.assertEqual((status, response['error']['code'], response['error']['details']), (1, 'validation_error', {'held_rows': {SOURCES: 1}}))
        owner.return_value.acquire.return_value.release.assert_called_once_with()
        self.assertEqual(upgrade.inspect(self.connection, self.extensions)['store'], LISTS)
        status, response, owner = self.command('revert', '--to', BASE, '--discard-rows')
        self.assertEqual((status, response['result']['removed'], response['result']['dropped_rows']),
                         (0, [SOURCES, INDEXES, SIDE_CHAT, TITLES, TURNS, REFS, REPORTS, TIMELINE, DELIVERIES, MOVES, MANAGED, COMPACTIONS, EXPERIENCE, REMOVED, LISTS], {TURNS: 0, REFS: 0, REPORTS: 0, TIMELINE: 0, DELIVERIES: 0, MOVES: 0, MANAGED: 0, COMPACTIONS: 0, EXPERIENCE: 0, REMOVED: 0, LISTS: 0, TITLES: 0, SIDE_CHAT: 0, INDEXES: 0, SOURCES: 1}))
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
