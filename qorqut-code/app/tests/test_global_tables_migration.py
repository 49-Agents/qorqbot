from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id
from bazarlyk.storage import database as database_module
from bazarlyk.storage.database import Database
from foundation_support import BUSINESS, HUMAN, OTHER, database_at
from test_service_transports import ServiceFixture

# Located by name so integration renumbering does not change what is under test.
VERSION = int(next(Path(database_module.__file__).with_name('migrations').glob('*_global_general_tables.sql')).name[:3])
STORES = ('data_tables', 'data_rows', 'table_versions')
COLUMNS = json.dumps([{'id': 'name', 'name': 'Name', 'type': 'text'}])

# The definition every business tables.version job carries since schema 103 (digest included), captured from the
# release before Global tables. Jobs queued before an upgrade must still match it byte-for-byte.
BUSINESS_VERSION_DEFINITION = {
    "digest": "630e0315fb892c837c9084042d7c7b5145fbe9b9443613fbfa41dec3bfd844c3", "discretionary": [], "external": True,
    "global_capabilities": [], "id": "tables.version", "outputs": [], "permissions": {}, "resource_types": [],
    "scope_kinds": ["business"], "timeout_seconds": 900, "unavailable_reason": None, "version": 1,
    "input_schema": {"additionalProperties": False, "properties": {"version_id": {"format": "uuid", "type": "string"}},
                     "required": ["version_id"], "type": "object"}}


def seed(database):
    """Business tables of every supported purpose, with rows, history and a pending version."""
    ids = {}
    with database.transaction() as uow:
        for business in (BUSINESS, OTHER):
            for purpose in ('general', 'metrics', 'ledger'):
                table = new_id()
                ids[business, purpose] = table
                uow.repository('data_tables', business_id=business).insert({'id': table, 'title': f'{purpose} {business[-1]}',
                    'columns_json': COLUMNS, 'created_by': HUMAN, 'purpose': purpose})
                cells = {'amount': 250} if purpose == 'ledger' else {'name': 'kept'}
                uow.repository('data_rows', business_id=business).insert({'id': new_id(), 'table_id': table, 'position': 0,
                    'cells_json': json.dumps(cells), 'created_by': HUMAN, 'updated_by': HUMAN})
                uow.repository('table_versions', business_id=business).insert({'id': new_id(), 'table_id': table, 'table_revision': 1,
                    'sequence': len(ids), 'actor_id': HUMAN, 'operation': 'tables_create', 'path': f'hq/tables/{table}.json',
                    'snapshot_json': '{}', 'snapshot_hash': 'h', 'git_commit': None if purpose == 'general' else 'c' * 40})
    return ids


def state(uow):
    columns = {store: ','.join('"' + row[1] + '"' for row in uow.connection.execute(f'PRAGMA table_info({store})')) for store in STORES}
    rows = {store: [tuple(row) for row in uow.connection.execute(f'SELECT {columns[store]} FROM {store} ORDER BY 1,2')] for store in STORES}
    schema = [tuple(row) for row in uow.connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN ('data_tables','data_rows','table_versions') "
        "AND type IN ('index','trigger') AND name NOT LIKE '%global%' AND name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    return rows, schema


@pytest.fixture
def upgraded(tmp_path, monkeypatch):
    migrations = Database.migrations()
    with monkeypatch.context() as old:
        old.setattr(Database, 'migrations', staticmethod(lambda: migrations[:VERSION - 1]))
        database = database_at(tmp_path)
        ids = seed(database)
        with database.read() as uow:
            before = state(uow)
            captures = uow.connection.execute('SELECT count(*) FROM inbox_changes').fetchone()[0]
    database.initialize()
    database.open_existing()
    return database, ids, before, captures


def test_migration_preserves_business_data_indexes_and_triggers(upgraded):
    database, _, (rows, schema), captures = upgraded
    with database.read() as uow:
        assert (rows, schema) == state(uow)
        assert captures == uow.connection.execute('SELECT count(*) FROM inbox_changes').fetchone()[0]
        assert not uow.connection.execute('PRAGMA foreign_key_check').fetchall()
        assert uow.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        names = {row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {'ledger_amount_integer', 'ledger_rows_no_delete', 'ledger_rows_no_update', 'financial_purpose_immutable',
            'inbox_capture_data_rows_insert', 'inbox_capture_data_tables_insert'} <= names


def test_business_financial_and_ledger_guards_remain(upgraded):
    database, ids, _, _ = upgraded
    ledger = ids[BUSINESS, 'ledger']
    for statement, arguments in (
            ("UPDATE data_rows SET cells_json='{\"amount\":1}' WHERE table_id=?", (ledger,)),
            ('DELETE FROM data_rows WHERE table_id=?', (ledger,)),
            ("UPDATE data_tables SET purpose='general' WHERE id=?", (ledger,))):
        with pytest.raises(DomainError), database.transaction() as uow:
            uow.connection.execute(statement, arguments)
    with pytest.raises(DomainError), database.transaction() as uow:
        uow.repository('data_rows', business_id=BUSINESS).insert({'id': new_id(), 'table_id': ledger, 'position': 1,
            'cells_json': '{"amount":1.5}', 'created_by': HUMAN, 'updated_by': HUMAN})
    with pytest.raises(DomainError), database.transaction() as uow:
        uow.repository('data_tables', business_id=BUSINESS).insert({'id': new_id(), 'title': 'Second ledger',
            'columns_json': COLUMNS, 'created_by': HUMAN, 'purpose': 'ledger'})
    with database.transaction() as uow:
        uow.repository('data_tables', business_id=OTHER).insert({'id': new_id(), 'title': 'Other business general',
            'columns_json': COLUMNS, 'created_by': HUMAN})


def test_global_stores_enforce_scope_uniqueness_and_parents(upgraded):
    database, ids, _, _ = upgraded
    table = new_id()
    with database.transaction() as uow:
        uow.repository('data_tables', business_id=None).insert({'id': table, 'title': 'Global', 'columns_json': COLUMNS, 'created_by': HUMAN})
        uow.repository('data_rows', business_id=None).insert({'id': 'row', 'table_id': table, 'position': 0,
            'cells_json': '{}', 'created_by': HUMAN, 'updated_by': HUMAN})
        uow.repository('table_versions', business_id=None).insert({'id': 'version', 'table_id': table, 'table_revision': 1,
            'sequence': 99, 'actor_id': HUMAN, 'operation': 'tables_create', 'path': f'global/tables/{table}.json',
            'snapshot_json': '{}', 'snapshot_hash': 'h'})

    def rejected(store, values, business=None):
        with pytest.raises(DomainError), database.transaction() as uow:
            uow.repository(store, business_id=business).insert(values)

    row = {'table_id': table, 'cells_json': '{}', 'created_by': HUMAN, 'updated_by': HUMAN}
    version = {'actor_id': HUMAN, 'operation': 'tables_update', 'path': 'p', 'snapshot_json': '{}', 'snapshot_hash': 'h'}
    # NULL keys are distinct to SQLite, so Global uniqueness rests on the dedicated indexes.
    rejected('data_tables', {'id': table, 'title': 'Again', 'columns_json': COLUMNS, 'created_by': HUMAN})
    rejected('data_rows', {**row, 'id': 'row', 'position': 1})
    rejected('data_rows', {**row, 'id': 'other', 'position': 0})
    rejected('table_versions', {**version, 'id': 'version', 'table_id': table, 'table_revision': 2, 'sequence': 100})
    rejected('table_versions', {**version, 'id': 'again', 'table_id': table, 'table_revision': 1, 'sequence': 101})
    # Global scope has no purposes beyond general and no upload references.
    for purpose in ('metrics', 'ledger', 'recurring', 'reconciliations'):
        rejected('data_tables', {'id': new_id(), 'title': purpose, 'columns_json': COLUMNS, 'created_by': HUMAN, 'purpose': purpose})
    rejected('data_rows', {**row, 'id': 'uploaded', 'position': 2, 'source_upload_id': new_id()})
    # A Global child cannot attach to a missing table or a business table, nor the reverse.
    rejected('data_rows', {**row, 'id': 'missing', 'table_id': new_id(), 'position': 3})
    rejected('data_rows', {**row, 'id': 'crossed', 'table_id': ids[BUSINESS, 'general'], 'position': 3})
    rejected('table_versions', {**version, 'id': 'crossed', 'table_id': ids[BUSINESS, 'general'], 'table_revision': 5, 'sequence': 102})
    rejected('data_rows', {**row, 'id': 'crossed', 'table_id': table, 'position': 3}, business=BUSINESS)
    rejected('table_versions', {**version, 'id': 'crossed', 'table_id': table, 'table_revision': 5, 'sequence': 103}, business=BUSINESS)
    # A referenced Global table cannot be deleted or renumbered, and a row cannot be re-scoped.
    for statement in ('DELETE FROM data_tables WHERE business_id IS NULL', "UPDATE data_tables SET id='moved' WHERE business_id IS NULL",
                      "UPDATE data_rows SET table_id='missing' WHERE business_id IS NULL"):
        with pytest.raises(DomainError), database.transaction() as uow:
            uow.connection.execute(statement)
    with pytest.raises(DomainError), database.transaction() as uow:
        uow.connection.execute('UPDATE data_rows SET business_id=? WHERE business_id IS NULL', (BUSINESS,))
    # Business and Global identifiers are independent namespaces.
    with database.transaction() as uow:
        uow.repository('data_tables', business_id=BUSINESS).insert({'id': table, 'title': 'Same id, business', 'columns_json': COLUMNS, 'created_by': HUMAN})
    with database.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM data_tables WHERE id=?', (table,)).fetchone()[0] == 2
        assert not uow.connection.execute('PRAGMA foreign_key_check').fetchall()


def test_migration_is_recorded_without_gaps(upgraded):
    database, *_ = upgraded
    with database.read() as uow:
        assert [row[0] for row in uow.connection.execute('SELECT version FROM schema_migrations ORDER BY version')] == \
            list(range(1, len(Database.migrations()) + 1))
        assert uow.connection.execute('PRAGMA user_version').fetchone()[0] == len(Database.migrations())



def test_business_history_jobs_queued_before_the_upgrade_publish_after_it(tmp_path, monkeypatch):
    """Real business jobs queued on schema 104 keep their frozen definition and run, or recover, on the new schema and runtime."""
    migrations = Database.migrations()
    with monkeypatch.context() as old:
        old.setattr(Database, 'migrations', staticmethod(lambda: migrations[:VERSION - 1]))
        # The service of that release declared no table created by a later migration (Telegram 106/107, Agent Viewbox 108).
        later = ('telegram_', 'agent_viewbox_')
        old.setattr(database_module, 'TABLES', {name: spec for name, spec in database_module.TABLES.items() if not name.startswith(later)})
        s = ServiceFixture(tmp_path, full=True)
    subprocess.run(['git', 'init', '-q', str(s.fixture.root)], check=True)
    database, ctx = s.fixture.db, s.fixture.ctx

    def invoke(context, operation, arguments, revision=None):
        result = s.runtime.application.invoke(context, operation, arguments, InvocationOptions(expected_revision=revision, idempotency_key=new_id()))
        assert result.status == 'ok', result.to_dict()
        return result.to_dict()['data']

    with database.read() as uow:
        assert uow.connection.execute('PRAGMA user_version').fetchone()[0] == VERSION - 1
    created = invoke(ctx, 'tables_create', {'title': 'Before upgrade', 'columns': json.loads(COLUMNS)})
    edited = invoke(ctx, 'tables_rows_write', {'table_id': created['table']['id'], 'rows': [{'cells': {'name': 'queued'}}]}, revision=1)
    jobs = [created['job_id'], edited['job_id']]
    with database.transaction() as uow:
        # The second job was claimed when the old service stopped: recovery, not a fresh run, must finish it.
        uow.connection.execute("UPDATE jobs SET state='running',attempts=1,claimed_by='stopped-before-upgrade',lease_until='2000-01-01T00:00:00Z' WHERE id=?", (jobs[1],))
    with database.read() as uow:
        queued = [dict(row) for row in uow.connection.execute('SELECT id,kind,business_id,state,definition_ref_json FROM jobs WHERE id IN (?,?) ORDER BY created_at,rowid', jobs)]
    assert [(row['kind'], row['business_id'], row['state']) for row in queued] == [
        ('tables.version', s.fixture.business_id, 'queued'), ('tables.version', s.fixture.business_id, 'running')]
    assert all(json.loads(row['definition_ref_json']) == BUSINESS_VERSION_DEFINITION for row in queued)

    database.initialize()
    database.open_existing()
    with database.read() as uow:
        # The Global tables migration and every later one (Telegram 106/107 and Agent Viewbox 108 follow it in this integration).
        assert uow.connection.execute('PRAGMA user_version').fetchone()[0] == len(Database.migrations()) >= VERSION
    programs = s.runtime.background.programs
    assert {k: v for k, v in programs.get('tables.version').items() if k != 'validate'} == BUSINESS_VERSION_DEFINITION
    for job in jobs:
        result = s.runtime.background.run_one(job)
        assert result['state'] == 'succeeded', result
    with database.read() as uow:
        states = [(row['state'], row['attempts'], json.loads(row['definition_ref_json'])['digest']) for row in uow.connection.execute(
            'SELECT state,attempts,definition_ref_json FROM jobs WHERE id IN (?,?) ORDER BY created_at,rowid', jobs)]
        published = [(row['git_commit'] is not None, row['snapshot_json']) for row in uow.connection.execute('SELECT git_commit,snapshot_json FROM table_versions')]
    assert [attempts for _, attempts, _ in states] == [1, 2], 'The claimed job is recovered as a second attempt'
    assert [state for state, _, _ in states] == ['succeeded', 'succeeded'] and {digest for _, _, digest in states} == {BUSINESS_VERSION_DEFINITION['digest']}
    assert published == [(True, None), (True, None)]
    count = subprocess.run(['git', '-C', str(s.fixture.root), 'rev-list', '--count', 'refs/heads/qorqut-tables'], check=True, capture_output=True, text=True).stdout.strip()
    assert count == '2'
    # The upgraded runtime keeps publishing business tables under the original program, and Global tables under their own.
    later = invoke(ctx, 'tables_rows_write', {'table_id': created['table']['id'], 'rows': [{'cells': {'name': 'after'}}]}, revision=2)
    shared = invoke(replace(ctx, business_id=None, review_business_id=None), 'tables_create', {'scope': 'global', 'title': 'After upgrade', 'columns': json.loads(COLUMNS)})
    for job in (later['job_id'], shared['job_id']):
        assert s.runtime.background.run_one(job)['state'] == 'succeeded'
    with database.read() as uow:
        kinds = {row['id']: row['kind'] for row in uow.connection.execute('SELECT id,kind FROM jobs')}
    assert (kinds[later['job_id']], kinds[shared['job_id']]) == ('tables.version', 'tables.global-version')
