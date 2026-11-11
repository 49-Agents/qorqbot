"""Explicit offline upgrade for the side_chat Signoff category.

The service must be stopped before the release caller invokes ``upgrade`` with
its database path and selected target assembly. This is never a startup hook.
Only the exact predecessor schema is accepted. Rows, indexes, triggers and
foreign keys are preserved; the table rebuild and target identity commit together.
"""
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3

from qorqut_operations.contracts import DomainError, canonical_bytes
from qorqut_operations.storage.database import APPLICATION_ID
from qorqut_operations.storage.inventory import compile_target, differences, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

OLD = "CHECK(category IN ('internal_change','platform_action','input_request'))"
NEW = "CHECK(category IN ('internal_change','platform_action','input_request','side_chat'))"


def predecessor(assembly):
    """The same selected assembly with only this category addition removed."""
    if sum(sql.count(NEW) for _, statements in assembly.schema for sql in statements) != 1:
        raise ValueError('Target must declare exactly one side-chat category constraint')
    schema = tuple((owner, tuple(sql.replace(NEW, OLD) for sql in statements)) for owner, statements in assembly.schema)
    payload = {'name': assembly.target.name, 'version': assembly.target.version,
        'owners': [{'owner': owner, 'schema': list(statements)} for owner, statements in schema],
        'tables': {name: [list(spec.keys), spec.scope_column, spec.mutable, spec.revision_column]
                   for name, spec in sorted(assembly.tables.items())},
        'immutable_fields': {name: sorted(fields) for name, fields in sorted(assembly.immutable_fields.items())}}
    target = replace(assembly.target, digest=hashlib.sha256(canonical_bytes(payload)).hexdigest())
    return replace(assembly, schema=schema, target=target)


def upgrade(database_path, assembly):
    path = Path(database_path).resolve(strict=True)
    previous = predecessor(assembly)
    expected, old = compile_target(assembly), compile_target(previous)
    connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, isolation_level=None, timeout=5)
    try:
        install_sql_functions(connection)
        connection.execute('PRAGMA foreign_keys=OFF')
        connection.execute('PRAGMA synchronous=FULL')
        connection.execute('PRAGMA temp_store=MEMORY')
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            raise DomainError('invalid_database', 'Not an Operations store')
        identity = connection.execute("SELECT name,version,digest FROM target_identity WHERE id='target'").fetchone()
        objects = read_objects(connection)
        target = assembly.target
        if identity == (target.name, target.version, target.digest) and not differences(expected.objects, objects):
            connection.rollback()
            return {'changed': False}
        target = previous.target
        if identity != (target.name, target.version, target.digest) or differences(old.objects, objects):
            raise DomainError('invalid_database', 'Side-chat upgrade requires the exact predecessor schema')
        count = connection.execute('SELECT count(*) FROM signoffs').fetchone()[0]
        connection.execute('CREATE TEMP TABLE side_chat_signoffs AS SELECT * FROM signoffs')
        connection.execute('DROP TABLE signoffs')
        connection.execute(expected.objects[('table', 'signoffs')][1])
        columns = ','.join('"' + name + '"' for name in expected.columns['signoffs'])
        connection.execute(f'INSERT INTO signoffs({columns}) SELECT {columns} FROM temp.side_chat_signoffs')
        for (kind, _), (owner, sql) in expected.objects.items():
            if owner == 'signoffs' and kind != 'table':
                connection.execute(sql)
        if (connection.execute('SELECT count(*) FROM signoffs').fetchone()[0] != count
                or connection.execute('PRAGMA foreign_key_check').fetchone()
                or differences(expected.objects, read_objects(connection))):
            raise DomainError('invalid_database', 'Side-chat upgrade verification failed')
        connection.execute('DROP TABLE temp.side_chat_signoffs')
        connection.execute("UPDATE target_identity SET digest=? WHERE id='target'", (assembly.target.digest,))
        connection.commit()
        return {'changed': True, 'signoffs': count}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
