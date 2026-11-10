"""Explicit offline addition of application-recovery queue indexes.

Inspect is read-only. Apply requires exclusive instance ownership and recognizes
only the exact predecessor or target schema, with the selected extensions.
Neither service startup nor a tool call runs this migration.

A release that declares these indexes refuses a store without them, and an
earlier release refuses a store that has them. Revert is the exact inverse of
apply, under the same ownership and checks, for returning to such a release:
the indexes hold no data of their own, so dropping them loses nothing.

This command recognises a store that lacks only these indexes. A store that is
further behind is upgraded with ``python -m qorqut.migration.upgrade``, which
applies the earlier transforms and this one in order.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.database import APPLICATION_ID
from qorqut_operations.storage.inventory import IDENTITY_TABLE, compile_target, differences, read_objects
from qorqut_operations.storage.queue_indexes import OWNER, OBJECTS
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from qorqut.composition.assembly import HostComposition, host_assembly, host_contributions
from qorqut.extensions import NO_EXTENSIONS
from qorqut.service.changes import ChangeFeed
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import InstanceOwnership

NAME = 'queue_indexes'
TRANSFORM = 'v1.3-application-queue-indexes-1'


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    return None if contribution.owner == OWNER else contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    for key in sorted(OBJECTS):
        connection.execute(objects[key][1])


def backward(connection):
    """The exact inverse of ``forward``. The caller owns the transaction."""
    for _, name in sorted(OBJECTS):
        connection.execute(f'DROP INDEX "{name}"')


def held_rows(connection):
    """How many rows ``backward`` would drop: indexes hold none of their own."""
    return 0


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
        raise DomainError('invalid_database', 'Queue optimization must only add its three declared indexes')
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
    raise DomainError('invalid_database', 'Store is not the exact queue-index predecessor or target; no changes made')


def inspect(connection, extensions=NO_EXTENSIONS):
    state = recognise(connection, *compositions(extensions))
    return {'transform': TRANSFORM, 'store': state, 'add_indexes': state == 'predecessor'}


def apply(connection, extensions=NO_EXTENSIONS):
    """Caller holds InstanceOwnership; DDL and target identity advance atomically."""
    predecessor, target = compositions(extensions)
    install_sql_functions(connection)
    connection.execute('BEGIN IMMEDIATE')
    try:
        state = recognise(connection, predecessor, target)
        if state == 'predecessor':
            forward(connection, compile_target(target).objects)
            changed = connection.execute(f'UPDATE {IDENTITY_TABLE} SET digest=? WHERE id=?',
                                         (target.target.digest, 'target')).rowcount
            if changed != 1:
                raise DomainError('invalid_database', 'The target identity could not be advanced')
        if recognise(connection, predecessor, target) != 'target' or connection.execute('PRAGMA foreign_key_check').fetchone():
            raise DomainError('invalid_database', 'Queue indexes or references failed verification')
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
    return {'transform': TRANSFORM, 'schema': 'added' if state == 'predecessor' else 'already_current',
            'target_digest': target.target.digest}


def revert(connection, extensions=NO_EXTENSIONS):
    """Caller holds InstanceOwnership; the indexes and the target identity go back together."""
    predecessor, target = compositions(extensions)
    install_sql_functions(connection)
    connection.execute('BEGIN IMMEDIATE')
    try:
        state = recognise(connection, predecessor, target)
        if state == 'target':
            backward(connection)
            changed = connection.execute(f'UPDATE {IDENTITY_TABLE} SET digest=? WHERE id=?',
                                         (predecessor.target.digest, 'target')).rowcount
            if changed != 1:
                raise DomainError('invalid_database', 'The target identity could not be restored')
        if (recognise(connection, predecessor, target) != 'predecessor'
                or connection.execute('PRAGMA foreign_key_check').fetchone()):
            raise DomainError('invalid_database', 'The predecessor schema or references failed verification')
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
    return {'transform': TRANSFORM, 'schema': 'removed' if state == 'target' else 'already_predecessor',
            'target_digest': predecessor.target.digest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inspect', 'apply', 'revert'])
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
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
                connection.execute('PRAGMA query_only=ON')
                result = inspect(connection, extensions)
        else:
            ownership = InstanceOwnership(paths).acquire()
            try:
                connection = sqlite3.connect(database, isolation_level=None, timeout=30)
                try:
                    result = (apply if options.command == 'apply' else revert)(connection, extensions)
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
