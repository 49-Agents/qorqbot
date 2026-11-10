"""Explicit offline addition of explicit Signoff response dependencies.

Run ``python -m qorqut.migration.signoff_sources inspect|apply --instance-root ROOT``
with the target component selection and optional --extensions-config. Inspect is
read-only. Apply requires exclusive instance ownership, recognises only the exact
predecessor or target composition, and commits DDL and target identity together.
Normal service startup never invokes this command or upgrades an existing store.

This command recognises a store that lacks only this change. A store that is
further behind is upgraded with ``python -m qorqut.migration.upgrade``, which
applies this transform and the later ones in order and can also revert them.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.database import APPLICATION_ID
from qorqut_operations.storage.inventory import IDENTITY_TABLE, compile_target, differences, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from qorqut.composition.assembly import HostComposition, host_assembly, host_contributions
from qorqut.extensions import NO_EXTENSIONS
from qorqut.service.changes import ChangeFeed
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import InstanceOwnership

NAME = 'signoff_sources'
TRANSFORM = 'v1.3-signoff-sources-1'
TABLE = 'signoff_source_links'
OBJECTS = {('table', TABLE), ('index', TABLE + '_source'), ('trigger', 'signoff_source_state'),
    *(('trigger', TABLE + '_' + event) for event in ('insert', 'update', 'delete')),
    *(('trigger', 'inbox_capture_' + TABLE + '_' + event) for event in ('insert', 'update', 'delete'))}


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is not StorageContribution:
        return contribution
    if contribution.owner == 'signoff-sources':
        return None
    if contribution.owner == 'operational_feed':
        return replace(contribution,
            schema=tuple(sql for sql in contribution.schema if 'inbox_capture_' + TABLE + '_' not in sql),
            required_tables=contribution.required_tables - {TABLE})
    return contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    connection.execute(objects[('table', TABLE)][1])
    for kind, name in sorted(OBJECTS - {('table', TABLE)}):
        connection.execute(objects[(kind, name)][1])


def backward(connection):
    """The exact inverse of ``forward``, dropping whatever rows the table holds. The caller owns the transaction and
    decides whether those rows may go. ``signoff_source_state`` sits on ``signoffs``, so it does not go with the table."""
    for kind, name in sorted(OBJECTS - {('table', TABLE)}):
        connection.execute(f'DROP {kind.upper()} "{name}"')
    connection.execute(f'DROP TABLE "{TABLE}"')


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]


def compositions(extensions=NO_EXTENSIONS):
    target = host_assembly(ChangeFeed(), extensions)
    builder = HostComposition()
    for contribution in host_contributions(ChangeFeed(), extensions):
        contribution = strip(contribution)
        if contribution is not None:
            builder.add(contribution)
    predecessor = builder.freeze()
    old, new = compile_target(predecessor).objects, compile_target(target).objects
    if set(new) - set(old) != OBJECTS or any(new.get(key) != value for key, value in old.items()):
        raise DomainError('invalid_database', 'Signoff source upgrade must only add its binding table, index and triggers')
    return predecessor, target


def recognise(connection, predecessor, target):
    if connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
        raise DomainError('invalid_database', 'This is not an Operations database')
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    identity = [tuple(row) for row in connection.execute(f'SELECT name,version,digest FROM {IDENTITY_TABLE}')]
    found = read_objects(connection)
    for name, assembly in [('predecessor', predecessor), ('target', target)]:
        wanted = assembly.target
        if (identity == [(wanted.name, wanted.version, wanted.digest)] and version == wanted.version
                and not differences(compile_target(assembly).objects, found)):
            return name
    raise DomainError('invalid_database', 'The store does not match the exact Signoff source predecessor or target; no changes made')


def inspect(connection, extensions=NO_EXTENSIONS):
    state = recognise(connection, *compositions(extensions))
    return {'transform': TRANSFORM, 'store': state, 'add_source_links': state == 'predecessor'}


def apply(connection, extensions=NO_EXTENSIONS):
    """Caller holds InstanceOwnership. Atomic, additive and repeatable."""
    predecessor, target = compositions(extensions)
    install_sql_functions(connection)
    connection.execute('BEGIN IMMEDIATE')
    try:
        state = recognise(connection, predecessor, target)
        if state == 'predecessor':
            forward(connection, compile_target(target).objects)
            updated = connection.execute(f'UPDATE {IDENTITY_TABLE} SET digest=? WHERE id=?', (target.target.digest, 'target')).rowcount
            if updated != 1:
                raise DomainError('invalid_database', 'The target identity could not be advanced')
        if recognise(connection, predecessor, target) != 'target' or connection.execute('PRAGMA foreign_key_check').fetchone():
            raise DomainError('invalid_database', 'Signoff source schema or references failed verification')
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
    return {'transform': TRANSFORM, 'schema': 'added' if state == 'predecessor' else 'already_current', 'target_digest': target.target.digest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inspect', 'apply'])
    add_instance_arguments(parser)
    parser.add_argument('--extensions-config', type=Path)
    options = parser.parse_args(argv)
    try:
        paths = instance_paths_from_arguments(options, required=())
        extensions = NO_EXTENSIONS
        if options.extensions_config is not None:
            from qorqut.extensions import select
            from qorqut.installation import extensions_configuration
            extensions = select(extensions_configuration(options.extensions_config)['extensions'])
        database = Path(paths.database).resolve(strict=True)
        if options.command == 'inspect':
            connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            try:
                connection.execute('PRAGMA query_only=ON')
                result = inspect(connection, extensions)
            finally:
                connection.close()
        else:
            ownership = InstanceOwnership(paths).acquire()
            try:
                connection = sqlite3.connect(database, isolation_level=None, timeout=30)
                try:
                    result = apply(connection, extensions)
                finally:
                    connection.close()
            finally:
                ownership.release()
        response, status = {'ok': True, 'result': result}, 0
    except DomainError as error:
        response, status = {'ok': False, 'error': error.to_dict()}, 1
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        response, status = {'ok': False, 'error': {'code': 'invalid_request', 'message': str(error)}}, 1
    sys.stdout.write(json.dumps(response) + '\n')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
