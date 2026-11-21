"""One id names one record in the whole store: a second record of the same family may not reuse an id in any other scope."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut_operations.errors import DomainError
from qorqut_operations.storage.database import Database

STAMP = '2026-11-10T12:00:00Z'
CRM = json.dumps({'stages': [{'id': 'lead', 'name': 'Lead'}], 'columns': []})


class UniqueRecordIdTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'operations.sqlite3', assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner = str(uuid4())
        self.alpha, self.beta = str(uuid4()), str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.owner, kind='human', display_name='Owner', active=1,
                                                     created_at=STAMP, updated_at=STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                                                       owner_principal_id=self.owner, created_at=STAMP, updated_at=STAMP))
            for project, slug in ((self.alpha, 'alpha'), (self.beta, 'beta')):
                uow.repository('projects').insert({'id': project, 'name': slug.title(), 'slug': slug,
                                                     'workspace': slug, 'hq_path': 'hq-' + slug})

    def insert(self, statement, parameters):
        with self.db.transaction() as uow:
            uow.connection.execute(statement, parameters)

    def refused(self, statement, parameters):
        with self.assertRaises(DomainError) as caught:
            self.insert(statement, parameters)
        self.assertEqual(caught.exception.code, 'revision_conflict')

    def scopes(self):
        return (self.alpha, self.beta, None)

    def check(self, statement, parameters_for):
        """The first scope takes the id; every other scope, Global included, is refused it."""
        first, *others = self.scopes()
        self.insert(statement, parameters_for(first))
        for scope in others:
            self.refused(statement, parameters_for(scope))

    def table(self, scope, kind='general'):
        table_id = str(uuid4())
        self.insert('INSERT INTO data_tables(project_id,id,title,columns_json,created_by,created_at,updated_at,table_kind,'
                    'crm_config_json) VALUES(?,?,?,?,?,?,?,?,?)',
                    (scope, table_id, 'Table', '[]', self.owner, STAMP, STAMP, kind, CRM if kind == 'crm' else None))
        return table_id

    def task(self, scope, identity):
        self.insert('INSERT INTO tasks(project_id,id,title,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                    (scope, identity, 'Task', 'open', self.owner, STAMP, STAMP))

    def test_a_task_id_is_refused_in_every_other_scope(self):
        self.check('INSERT INTO tasks(project_id,id,title,status,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   lambda scope: (scope, 'shared', 'Task', 'open', self.owner, STAMP, STAMP))

    def test_a_table_id_and_a_row_id_are_refused_in_every_other_scope(self):
        tables = {scope: self.table(scope) for scope in self.scopes()}
        self.check('INSERT INTO data_tables(project_id,id,title,columns_json,created_by,created_at,updated_at) '
                   'VALUES(?,?,?,?,?,?,?)', lambda scope: (scope, 'shared-table', 'Table', '[]', self.owner, STAMP, STAMP))
        row = ('INSERT INTO data_rows(project_id,id,table_id,position,cells_json,created_by,updated_by,created_at,updated_at) '
               'VALUES(?,?,?,0,?,?,?,?,?)')
        first, *others = self.scopes()
        self.insert(row, (first, 'shared-row', tables[first], '{}', self.owner, self.owner, STAMP, STAMP))
        for scope in others:
            self.refused(row, (scope, 'shared-row', tables[scope], '{}', self.owner, self.owner, STAMP, STAMP))

    def test_a_crm_entry_id_is_refused_in_every_other_scope(self):
        tables = {scope: self.table(scope, 'crm') for scope in self.scopes()}
        entry = ('INSERT INTO crm_entries(project_id,id,table_id,record_task_id,record_project_id,record_namespace,position,'
                 'stage_id,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,0,?,?,?,?,?)')
        parameters = {}
        for scope in self.scopes():
            self.task(scope, 'task-' + str(scope)[:8])
            namespace = 'global' if scope is None else 'project:' + scope
            parameters[scope] = (scope, 'shared-entry', tables[scope], 'task-' + str(scope)[:8], scope, namespace, 'lead',
                                 self.owner, self.owner, STAMP, STAMP)
        first, *others = self.scopes()
        self.insert(entry, parameters[first])
        for scope in others:
            self.refused(entry, parameters[scope])

    def test_a_relationship_id_and_an_entity_id_are_refused_in_every_other_scope(self):
        self.check('INSERT INTO relationships(project_id,id,from_type,from_id,to_type,to_id,role,context,created_at,updated_at,'
                   'created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   lambda scope: (scope, 'shared-edge', 'task', 'a', 'task', 'b', 'concerns', '', STAMP, STAMP, self.owner))
        first, second = self.alpha, self.beta
        entity = 'INSERT INTO context_entities(project_id,id,label,created_at,updated_at) VALUES(?,?,?,?,?)'
        self.insert(entity, (first, 'shared-entity', 'Label', STAMP, STAMP))
        self.refused(entity, (second, 'shared-entity', 'Label', STAMP, STAMP))

    def test_a_routine_id_is_refused_in_every_other_namespace(self):
        routine = ('INSERT INTO routines(namespace,project_id,id,name,scope,target,schedule,timezone,status,job_kind,'
                   'arguments_json,timeout_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)')

        def values(scope):
            return (scope or 'global', scope, 'shared-routine', 'Routine', 'global' if scope is None else 'project', 'target',
                    '0 9 * * *', 'UTC', 'active', 'kind', '{}', 60, STAMP, STAMP)
        self.check(routine, values)

    def test_a_learning_candidate_id_and_a_learning_run_id_are_refused_in_every_other_scope(self):
        self.check('INSERT INTO learning_candidates(project_id,id,candidate_json,created_at,updated_at) VALUES(?,?,?,?,?)',
                   lambda scope: (scope, 'shared-candidate', '{}', STAMP, STAMP))
        self.check('INSERT INTO learning_runs(project_id,id,result_json,created_at) VALUES(?,?,?,?)',
                   lambda scope: (scope, 'shared-run', '{}', STAMP))

    def test_a_workspace_presentation_id_is_refused_in_another_project(self):
        presentation = ('INSERT INTO workspace_presentations(project_id,id,agent_id,workspace_key,created_at,updated_at) '
                        'VALUES(?,?,?,?,?,?)')
        self.insert(presentation, (self.alpha, 'shared-presentation', self.owner, 'plan', STAMP, STAMP))
        self.refused(presentation, (self.beta, 'shared-presentation', self.owner, 'plan', STAMP, STAMP))


if __name__ == '__main__':
    unittest.main()
