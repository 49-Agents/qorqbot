import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.storage.database import Database
from foundation_support import BUSINESS, HUMAN, database_at


def test_populated_relationship_migration_preserves_rows_indexes_and_triggers(tmp_path, monkeypatch):
    migrations = Database.migrations()
    with monkeypatch.context() as old:
        old.setattr(Database, 'migrations', staticmethod(lambda: migrations[:55]))
        database = database_at(tmp_path)
        with database.transaction() as uow:
            for business, source, target, role in ((None, 'task', 'agent', 'assigned_to'), (BUSINESS, 'material', 'topic', 'part_of')):
                uow.repository('relationships', business_id=business).insert({'id': new_id(), 'from_type': source,
                    'from_id': new_id(), 'to_type': target, 'to_id': new_id(), 'role': role, 'created_by': HUMAN})
        with database.read() as uow:
            before = [tuple(row) for row in uow.connection.execute('SELECT * FROM relationships ORDER BY id')]
            schema = [tuple(row) for row in uow.connection.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='relationships' AND type IN ('index','trigger') ORDER BY type,name")]
            captures = uow.connection.execute('SELECT count(*) FROM inbox_changes').fetchone()[0]
    database.initialize()
    database.open_existing()
    with database.read() as uow:
        assert before == [tuple(row) for row in uow.connection.execute('SELECT * FROM relationships ORDER BY id')]
        assert schema == [tuple(row) for row in uow.connection.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='relationships' AND type IN ('index','trigger') ORDER BY type,name")]
        assert captures == uow.connection.execute('SELECT count(*) FROM inbox_changes').fetchone()[0]
        assert not uow.connection.execute('PRAGMA foreign_key_check').fetchall()
    values = {'id': new_id(), 'from_type': 'material', 'from_id': new_id(), 'to_type': 'topic', 'to_id': new_id(), 'role': 'part_of', 'created_by': HUMAN}
    with database.transaction() as uow:
        uow.repository('relationships', business_id=None).insert(values)
    with pytest.raises(DomainError), database.transaction() as uow:
        uow.repository('relationships', business_id=None).insert(values)
    with pytest.raises(DomainError), database.transaction() as uow:
        uow.repository('relationships', business_id=None).insert({**values, 'id': new_id(), 'to_type': 'story'})
