"""Whole Tables move between projects: rows, history, CRM memberships, discussion and follows go with them, ids unchanged.

A Table could not leave its project at all, so a project's closed history (its outreach CRMs, its lists) stayed behind
whenever its Tasks moved. Now a preview shows what travels and what blocks, and the move is one all-or-nothing transaction
recorded in ``table_moves``; a stale reference finds the Table where it is now, and a link whose other end stays behind is
kept in the source project as history."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from qorqut.composition.assembly import host_assembly
from qorqut.domains.tables.relocation import TableHostRelocation
from qorqut.service.changes import ChangeFeed
from qorqut_operations.contracts import DomainError, InvocationContext, Principal
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.record_moves import moved_location
from qorqut_operations.tables.relocation import TableRelocation
from qorqut_operations.tasks import identity

STAMP, LATER = '2026-11-10T12:00:00Z', '2026-11-12T12:00:00Z'
CRM = {'stages': [{'id': 'lead', 'name': 'Lead'}], 'columns': []}


class TableMoveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner = str(uuid4())
        self.source, self.destination = str(uuid4()), str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.owner, kind='human', display_name='Owner', active=1,
                                                     created_at=STAMP, updated_at=STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                                                       owner_principal_id=self.owner, created_at=STAMP, updated_at=STAMP))
            for project, slug in ((self.source, 'launch'), (self.destination, 'product')):
                uow.repository('projects').insert({'id': project, 'name': slug.title(), 'slug': slug,
                                                     'workspace': slug, 'hq_path': 'hq-' + slug})
        self.ctx = InvocationContext(Principal(self.owner, 'human'), source='dashboard', project_id=self.source,
                                     allowed_project_ids=frozenset({self.source, self.destination}))
        self.engine = TableRelocation((TableHostRelocation(),))

    # -- fixtures -------------------------------------------------------------------------------------------------

    def sql(self, statement, parameters=()):
        with self.db.transaction() as uow:
            uow.connection.execute(statement, parameters)

    def table(self, title='Outreach', *, kind='general', committed=True, rows=2):
        table_id = str(uuid4())
        with self.db.transaction() as uow:
            connection = uow.connection
            connection.execute('INSERT INTO data_tables(project_id,id,title,columns_json,created_by,created_at,updated_at,table_kind,'
                               'crm_config_json) VALUES(?,?,?,?,?,?,?,?,?)',
                               (self.source, table_id, title, '[]', self.owner, STAMP, STAMP, kind,
                                json.dumps(CRM) if kind == 'crm' else None))
            for position in range(rows if kind == 'general' else 0):
                connection.execute('INSERT INTO data_rows(project_id,id,table_id,position,cells_json,created_by,updated_by,created_at,'
                                   'updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                                   (self.source, str(uuid4()), table_id, position, '{}', self.owner, self.owner, STAMP, STAMP))
            sequence = connection.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM table_versions').fetchone()[0]
            connection.execute('INSERT INTO table_versions(project_id,id,table_id,table_revision,sequence,actor_id,operation,path,'
                               'snapshot_json,snapshot_hash,git_commit,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                               (self.source, str(uuid4()), table_id, 1, sequence, self.owner, 'create', f'tables/{table_id}.json',
                                None if committed else '{}', 'hash', 'abc123' if committed else None, STAMP))
        return table_id

    def task(self, project=None):
        with self.db.transaction() as uow:
            task_id = identity.new_task_id(uow.connection)
            uow.repository('tasks', project_id=project or self.source).insert(
                {'id': task_id, 'title': 'Creator', 'status': 'done', 'created_by': self.owner, 'created_at': STAMP, 'updated_at': STAMP})
        return task_id

    def edge(self, source, target, *, project=None):
        edge_id = str(uuid4())
        self.sql('INSERT INTO relationships(project_id,id,from_type,from_id,to_type,to_id,role,context,created_at,updated_at,created_by) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (project or self.source, edge_id, *source, *target, 'concerns', '', STAMP, STAMP, self.owner))
        return edge_id

    def preview(self, *ids):
        with self.db.read() as uow:
            return self.engine.inspect(self.ctx, uow, self.source, self.destination, list(ids))

    def move(self, *ids):
        inspection = self.preview(*ids)
        self.assertEqual(inspection.blockers, [])
        with self.db.transaction() as uow:
            return self.engine.apply(self.ctx, uow, self.source, self.destination, list(ids), inspection.fingerprint(self.owner),
                                     move_id=str(uuid4()), at=LATER)

    def scope(self, table, where, parameters):
        with self.db.read() as uow:
            return {row[0] for row in uow.connection.execute(f'SELECT project_id FROM {table} WHERE {where}', parameters)}

    # -- tests ----------------------------------------------------------------------------------------------------

    def test_a_table_moves_with_its_rows_history_discussion_and_follows(self):
        table_id = self.table()
        self.sql("INSERT INTO thread_messages(project_id,id,item_type,item_id,body,author_id,recorder_id,created_at) "
                 "VALUES(?,?,'table',?,'Looks good',?,?,?)", (self.source, str(uuid4()), table_id, self.owner, self.owner, STAMP))
        self.sql("INSERT INTO thread_follows(project_id,item_type,item_id,principal_id,following,reason,created_at,updated_at) "
                 "VALUES(?,'table',?,?,1,'creator',?,?)", (self.source, table_id, self.owner, STAMP, STAMP))
        result = self.move(table_id)
        self.assertEqual(result['counts']['rows'], 2)
        self.assertEqual(result['counts']['comments'], 1)
        for table in ('data_rows', 'table_versions'):
            self.assertEqual(self.scope(table, 'table_id=?', (table_id,)), {self.destination})
        self.assertEqual(self.scope('data_tables', 'id=?', (table_id,)), {self.destination})
        self.assertEqual(self.scope('thread_messages', "item_type='table' AND item_id=?", (table_id,)), {self.destination})
        self.assertEqual(self.scope('thread_follows', "item_type='table' AND item_id=?", (table_id,)), {self.destination})
        with self.db.read() as uow:
            message = uow.connection.execute("SELECT reference_namespace FROM thread_messages WHERE item_id=?", (table_id,)).fetchone()
            self.assertEqual(message[0], 'project:' + self.source)
            path = uow.connection.execute('SELECT path FROM table_versions WHERE table_id=?', (table_id,)).fetchone()[0]
            self.assertEqual(path, f'tables/{table_id}.json')
            self.assertEqual(moved_location(uow, 'table', self.source, table_id), self.destination)
            self.assertIsNone(moved_location(uow, 'table', self.destination, table_id))
            self.assertEqual(uow.connection.execute('SELECT COUNT(*) FROM table_moves WHERE table_id=?', (table_id,)).fetchone()[0], 1)

    def test_a_crm_moves_with_its_memberships_which_keep_naming_their_tasks(self):
        table_id, task_id = self.table(kind='crm'), self.task()
        self.sql("INSERT INTO crm_entries(project_id,id,table_id,record_task_id,record_project_id,record_namespace,position,stage_id,"
                 "created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,0,'lead',?,?,?,?)",
                 (self.source, str(uuid4()), table_id, task_id, self.source, 'project:' + self.source, self.owner, self.owner, STAMP, STAMP))
        self.assertEqual(self.move(table_id)['counts']['crm_entries'], 1)
        with self.db.read() as uow:
            entry = uow.connection.execute('SELECT project_id, record_project_id, record_task_id FROM crm_entries').fetchone()
        self.assertEqual(tuple(entry), (self.destination, self.source, task_id))

    def test_the_guard_admits_only_the_armed_table(self):
        table_id = self.table(kind='crm')
        self.sql("INSERT INTO thread_messages(project_id,id,item_type,item_id,body,author_id,recorder_id,created_at) "
                 "VALUES(?,?,'table',?,'Note',?,?,?)", (self.source, str(uuid4()), table_id, self.owner, self.owner, STAMP))
        with self.assertRaises(Exception):
            self.sql("UPDATE thread_messages SET project_id=? WHERE item_id=?", (self.destination, table_id))

    def test_a_table_whose_latest_change_is_not_yet_saved_to_its_history_waits(self):
        pending = self.table(committed=False)
        self.assertEqual([item['code'] for item in self.preview(pending).blockers], ['history_pending'])

    def test_a_link_to_something_left_behind_stays_as_history_and_one_to_a_moved_task_reunites(self):
        table_id = self.table()
        stays, moved = self.task(), self.task(self.destination)
        behind = self.edge(('table', table_id), ('task', stays))
        with self.db.transaction() as uow:
            uow.connection.execute("INSERT INTO task_moves(move_id,task_id,task_created_at,source_project_id,destination_project_id,"
                                   "revision_before,revision_after,actor_id,moved_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                   (str(uuid4()), moved, STAMP, self.source, self.destination, 1, 2, self.owner, STAMP))
        reunited = self.edge(('table', table_id), ('task', moved))
        result = self.move(table_id)
        self.assertEqual(result['counts']['historical_relationships'], 1)
        self.assertEqual(self.scope('relationships', 'id=?', (behind,)), {self.source})
        self.assertEqual(self.scope('relationships', 'id=?', (reunited,)), {self.destination})

    def test_a_changed_table_refuses_the_old_preview(self):
        table_id = self.table()
        inspection = self.preview(table_id)
        self.sql('UPDATE data_tables SET title=?, revision=revision+1 WHERE id=?', ('Renamed', table_id))
        with self.db.transaction() as uow, self.assertRaises(DomainError) as raised:
            self.engine.apply(self.ctx, uow, self.source, self.destination, [table_id], inspection.fingerprint(self.owner),
                              move_id=str(uuid4()), at=LATER)
        self.assertEqual(raised.exception.code, 'revision_conflict')

    def test_active_work_naming_the_table_blocks_the_move_and_finished_work_does_not(self):
        table_id = self.table()
        for state, dedupe in (('queued', 'a'), ('succeeded', 'b')):
            self.sql("INSERT INTO jobs(id,project_id,target_namespace,kind,principal_id,arguments_json,dedupe_key,state,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid4()), self.source, 'project:' + self.source, 'tables_import', self.owner,
                                                    json.dumps({'table_id': table_id}), dedupe, state, STAMP, STAMP))
        blockers = self.preview(table_id).blockers
        self.assertEqual([(item['code'], item['message'][:15]) for item in blockers], [('unsupported_binding', '1 active backgr')])

    def test_a_global_table_or_the_same_project_is_refused(self):
        table_id = self.table()
        with self.db.read() as uow:
            for source, destination in ((None, self.destination), (self.source, self.source)):
                with self.assertRaises(DomainError):
                    self.engine.inspect(replace(self.ctx, project_id=source), uow, source, destination, [table_id])


if __name__ == '__main__':
    unittest.main()
