"""One ordered offline upgrade over every schema transform a store may still owe.

A transform adds its own objects or, where it says so, changes the definition
of an existing one; a state of the chain differs from the one before it by
exactly that.

Each transform under ``qorqut.migration`` recognises a store that lacks only its
own change. That is exact for a store one transform behind. A store two
transforms behind matches neither module's predecessor, so no single module of
one build can start on it. This command keeps the transforms in the order their
changes were released and derives every state between them from this build's
own composition, with the later transforms taken out. A store at any state of
that chain is recognised and walked to the build's target.

Run ``python -m qorqut.migration.upgrade COMMAND --instance-root ROOT`` with the
target component selection and optional --extensions-config.

``inspect`` is read-only. It reports the state the store is at, the transforms
already applied and still pending, and the rows each applied transform holds.

``apply`` requires exclusive instance ownership. It runs every pending transform
and advances the target identity in one transaction, so a failure at any step
leaves the store exactly as it was.

``revert --to STATE`` requires the same ownership and walks back to ``base`` (no
transform applied) or to a named transform (that one stays applied), also in
one transaction. It refuses to drop rows a transform holds unless
``--discard-rows`` is given.

A store that matches no state of the chain is refused unchanged, with the
differences from each state by name. Normal service startup never runs this
command or upgrades an existing store.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import CORE_OWNER, StorageContribution
from qorqut_operations.storage.core_schema import CORE_SCHEMA
from qorqut_operations.storage.database import APPLICATION_ID
from qorqut_operations.storage.inventory import IDENTITY_TABLE, compile_target, describe, differences, read_objects
from qorqut_operations.storage.sqlfunctions import install_sql_functions

from qorqut.composition.assembly import HostComposition, host_contributions
from qorqut.extensions import NO_EXTENSIONS
from qorqut.migration import queue_indexes, signoff_side_chat, signoff_sources
from qorqut.service.changes import ChangeFeed
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import InstanceOwnership

# Release order, oldest first. A new schema transform is appended here; it provides NAME, OBJECTS (what it adds),
# strip(contribution), forward(connection, objects), backward(connection) and held_rows(connection). One that changes
# the definition of an existing object also names it in CHANGED.
TRANSFORMS = (signoff_sources, queue_indexes, signoff_side_chat)
BASE = 'base'
STATES = (BASE, *(transform.NAME for transform in TRANSFORMS))


def _before(contribution, transforms):
    """``contribution`` as it stood before every one of ``transforms``; None when one of them introduced it."""
    for transform in transforms:
        contribution = transform.strip(contribution)
        if contribution is None:
            break
    return contribution


def chain(extensions=NO_EXTENSIONS):
    """``(assembly, objects)`` for every state, oldest first: entry k is this build's composition with every transform
    after the first k taken out. The last entry is the build's own target."""
    states = []
    for applied in range(len(TRANSFORMS) + 1):
        later = TRANSFORMS[applied:]
        # The engine seeds its own schema, so the transforms see it here and not among the host contributions.
        builder = HostComposition(core_schema=_before(StorageContribution(CORE_OWNER, schema=CORE_SCHEMA), later).schema)
        for contribution in host_contributions(ChangeFeed(), extensions):
            contribution = _before(contribution, later)
            if contribution is not None:
                builder.add(contribution)
        assembly = builder.freeze()
        states.append((assembly, compile_target(assembly).objects))
    for transform, (_, old), (_, new) in zip(TRANSFORMS, states, states[1:]):
        changed = {key for key, value in old.items() if new.get(key) != value}
        if set(new) - set(old) != set(transform.OBJECTS) or changed != set(getattr(transform, 'CHANGED', ())):
            raise DomainError('invalid_database', f'Transform {transform.NAME} must only add or change its own declared objects')
    return states


def recognise(connection, states):
    """The index of the state the store is exactly at; otherwise a refusal naming its differences from each state."""
    if connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
        raise DomainError('invalid_database', 'This is not an Operations database')
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    identity = [tuple(row) for row in connection.execute(f'SELECT name,version,digest FROM {IDENTITY_TABLE}')]
    found, problems = read_objects(connection), {}
    for position, (assembly, objects) in enumerate(states):
        wanted = assembly.target
        differing = differences(objects, found)
        if version != wanted.version:
            differing.append(f'changed user_version (expected {wanted.version}, found {version})')
        if identity != [(wanted.name, wanted.version, wanted.digest)]:
            differing.append(f'changed target identity (expected digest {wanted.digest})')
        if not differing:
            return position
        problems[STATES[position]] = describe(differing)
    raise DomainError('invalid_database', 'The store matches no state of the upgrade chain; no changes made',
                      details={'identity': [list(row) for row in identity], 'user_version': version, 'states': problems})


def _report(connection, states, position):
    return {'store': STATES[position], 'applied': list(STATES[1:position + 1]), 'pending': list(STATES[position + 1:]),
            'digest': states[position][0].target.digest, 'target_digest': states[-1][0].target.digest,
            'held_rows': {transform.NAME: transform.held_rows(connection) for transform in TRANSFORMS[:position]}}


def _settle(connection, states, position):
    """Advance or restore the target identity to ``position`` and prove the store is exactly that state."""
    changed = connection.execute(f'UPDATE {IDENTITY_TABLE} SET digest=? WHERE id=?',
                                 (states[position][0].target.digest, 'target')).rowcount
    if changed != 1 or recognise(connection, states) != position:
        raise DomainError('invalid_database', f'The store did not reach the state {STATES[position]}; no changes made')


def inspect(connection, extensions=NO_EXTENSIONS):
    states = chain(extensions)
    return _report(connection, states, recognise(connection, states))


def apply(connection, extensions=NO_EXTENSIONS):
    """Caller holds InstanceOwnership. Every pending transform and the target identity advance atomically."""
    states = chain(extensions)
    install_sql_functions(connection)
    connection.execute('BEGIN IMMEDIATE')
    try:
        start = recognise(connection, states)
        for position in range(start + 1, len(states)):
            TRANSFORMS[position - 1].forward(connection, states[position][1])
            _settle(connection, states, position)
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise DomainError('invalid_database', 'References failed verification after the upgrade; no changes made')
        result = _report(connection, states, len(TRANSFORMS))
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
    return {'from': STATES[start], 'added': list(STATES[start + 1:]), **result}


def revert(connection, extensions=NO_EXTENSIONS, *, to, discard_rows=False):
    """Caller holds InstanceOwnership. Walks back to the state ``to``; transforms and the identity go back atomically."""
    if to not in STATES:
        raise DomainError('validation_error', f'Revert goes to one of: {", ".join(STATES)}')
    goal = STATES.index(to)
    states = chain(extensions)
    install_sql_functions(connection)
    connection.execute('BEGIN IMMEDIATE')
    try:
        start = recognise(connection, states)
        if start < goal:
            raise DomainError('validation_error', f'The store is at {STATES[start]}, which is before {to}; revert only walks back')
        dropped = {}
        for position in range(start, goal, -1):
            transform = TRANSFORMS[position - 1]
            dropped[transform.NAME] = transform.held_rows(connection)
            if dropped[transform.NAME] and not discard_rows:
                raise DomainError('validation_error', f'Reverting {transform.NAME} would drop {dropped[transform.NAME]} '
                                  'rows it holds; pass --discard-rows to accept that. No changes made',
                                  details={'held_rows': {transform.NAME: dropped[transform.NAME]}})
            transform.backward(connection)
            _settle(connection, states, position - 1)
        if connection.execute('PRAGMA foreign_key_check').fetchone():
            raise DomainError('invalid_database', 'References failed verification after the revert; no changes made')
        result = _report(connection, states, goal)
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
    return {'from': STATES[start], 'removed': list(STATES[goal + 1:start + 1]), 'dropped_rows': dropped, **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['inspect', 'apply', 'revert'])
    add_instance_arguments(parser)
    parser.add_argument('--extensions-config', type=Path)
    parser.add_argument('--to', choices=STATES, help='revert only: the state to return to')
    parser.add_argument('--discard-rows', action='store_true', help='revert only: drop the rows a reverted transform holds')
    options = parser.parse_args(argv)
    try:
        if options.command == 'revert' and options.to is None:
            raise DomainError('validation_error', f'revert needs --to with one of: {", ".join(STATES)}')
        if options.command != 'revert' and (options.to is not None or options.discard_rows):
            raise DomainError('validation_error', '--to and --discard-rows belong to revert only')
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
                with closing(sqlite3.connect(database, isolation_level=None, timeout=30)) as connection:
                    if options.command == 'apply':
                        result = apply(connection, extensions)
                    else:
                        result = revert(connection, extensions, to=options.to, discard_rows=options.discard_rows)
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
