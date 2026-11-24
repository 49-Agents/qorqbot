"""A real store with the real routine scheduler and background service over one synthetic Global program.

``build`` gives the database, the program registry, authority, routine operations, scheduler and background service;
``routine`` saves an active every-minute routine for the program; ``WriteUnits`` counts the write units a database opens.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from qorqut_operations.application import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.background.authority import BackgroundAuthority
from qorqut_operations.background.programs import Program, ProgramRegistry, ProgramResult
from qorqut_operations.background.routines import RoutineScheduler
from qorqut_operations.background.service import BackgroundService
from qorqut_operations.contracts import PolicyDecision, new_id
from qorqut_operations.routines.operations import RoutineOperations
from qorqut_operations.storage.database import Database
from qorqut_operations.storage.locks import LockPaths

from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed

PROGRAM = 'example.idle_check'
WRITES = ('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'BEGIN IMMEDIATE')


class Policy:
    def resolve(self, ctx, descriptor, targets, uow):
        return PolicyDecision('allow', 'test', {})


class WriteUnits:
    """Counts the write units a database opens, by entry point and operation, and the write statements it runs.

    ``write_if_needed`` opens a unit without SQLite's writer and takes it with the first write statement, so a unit of
    it that runs no write statement never held the writer; the statements are counted to tell the two apart."""

    def __init__(self, database):
        self.opened, self.statements = [], []
        for name in ('transaction', 'write_if_needed'):
            original = getattr(database, name)

            def entry(*args, _name=name, _original=original, **kwargs):
                self.opened.append((_name, kwargs.get('operation')))
                return _original(*args, **kwargs)
            setattr(database, name, entry)
        acquire = database._acquire

        def traced():
            connection = acquire()
            connection.set_trace_callback(self._statement)
            return connection
        database._acquire = traced

    def _statement(self, sql):
        if sql.lstrip().upper().startswith(WRITES):
            self.statements.append(sql.split(None, 2)[:2])

    def take(self):
        taken = (self.opened, self.statements)
        self.opened, self.statements = [], []
        return taken


def build(root, *, clock=None, run=None):
    clock = clock or (lambda: datetime(2026, 11, 1, 9, 0, 30, tzinfo=timezone.utc))
    database = Database(root / 'operations.sqlite', assembly=host_assembly(ChangeFeed())).initialize_target()
    owner = new_id()
    programs = ProgramRegistry()
    calls = []

    def default_run(control, arguments):
        calls.append(arguments)
        return ProgramResult()
    programs.register(Program(PROGRAM, 1, {'type': 'object', 'additionalProperties': False}, run or default_run,
                              scope_kinds=('global',)))
    programs.freeze()
    authority = BackgroundAuthority(database, programs)
    with database.transaction() as uow:
        uow.repository('principals').insert({'id': owner, 'kind': 'human', 'display_name': 'owner'})
        uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': owner})
        authority.register_workers(uow)
    application = Application(database, OperationRegistry(), policy=Policy())
    routines = RoutineOperations(programs, authority, clock=clock)
    scheduler = RoutineScheduler(routines, database)
    background = BackgroundService(application, programs, authority, locks=LockPaths.under(root / 'locks'),
                                   scheduler=scheduler, clock=clock)
    return SimpleNamespace(database=database, programs=programs, authority=authority, routines=routines,
                           scheduler=scheduler, background=background, application=application, calls=calls, clock=clock)


def routine(fixture, schedule='* * * * *'):
    identity = new_id()
    with fixture.database.transaction() as uow:
        uow.repository('routines', project_id=None).insert({'namespace': 'global', 'id': identity, 'project_id': None,
            'name': 'Example', 'scope': 'global', 'target': 'root', 'schedule': schedule, 'timezone': 'UTC',
            'status': 'active', 'job_kind': PROGRAM, 'arguments_json': {}, 'timeout_seconds': 60, 'metadata_json': {}})
    return identity


def rows(database, sql, *args):
    with database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, args)]
