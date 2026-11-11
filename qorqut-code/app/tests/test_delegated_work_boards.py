"""Naming a worker as a Task's Worker lists it on that worker's Todo; a board reminds only about work its agent must
move; a result or blocker on delegated work still reaches Ordubashy."""
import json
import sqlite3
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from qorqut.agents.attention import Attention, acts_on
from qorqut.agents.attention_delivery import AttentionRuntime
from qorqut.composition.attention import SCHEMA
from qorqut.domains.tasks.ports import HostBoards, HostRouting
from qorqut_operations.contracts import DomainError, InvocationContext, Principal

# Fixed canonical UUIDs in sorted order, so listings by agent id read COORDINATOR, OWNER, SCOUT, COMPASS, GUEST.
COORDINATOR, OWNER, SCOUT, COMPASS, GUEST = (f'00000000-0000-4000-8000-00000000000{n}' for n in range(1, 6))
WORKERS = {SCOUT, COMPASS}
CREATED = '2026-11-10T09:00:00.000000Z'
REF = {'type': 'task', 'id': 'task-1', 'business_id': None}


def context(identity, kind='agent', **extra):
    return InvocationContext(Principal(identity, kind), global_capabilities={'read_global_context'}, **extra)


def task(status='open', workers=(), identity='task-1', **extra):
    return {'id': identity, 'status': status, 'archived': 0, 'created_at': CREATED, 'title': 'Find suppliers',
            'worker_ids_json': list(workers), 'fields_json': {}, **extra}


class DelegatedWorkBoardTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        for statement in SCHEMA:
            self.conn.execute(statement)
        self.tasks = {}
        self.uow = NS(connection=self.conn, repository=lambda name, business_id=None: NS(
            get=lambda identity, *a, **k: self.tasks.get(identity) if name == 'tasks' else None))
        self.app = NS(current_context=lambda ctx, uow: ctx, authorize=Mock(), registry=NS(get=lambda name: NS(name=name)),
                      policy=NS(context_for=lambda agent, **_: context(agent)),
                      signoffs=NS(sources=NS(project_tasks=lambda ctx, tasks, uow: tasks)))
        self.runtime = NS(application=self.app, focus=NS(inbox=NS()))
        self.attention = Attention(self.runtime)
        self.app.ordubashy_attention = self.attention
        self.attention.task = Mock(side_effect=lambda ctx, ref, uow, **_: self.tasks.get(ref['id']) or task())
        for target, value in (
                ('qorqut.agents.attention.configured_ordubashy', lambda uow: COORDINATOR),
                ('qorqut.management.identity.configured_ordubashy', lambda uow: COORDINATOR),
                ('qorqut.agents.attention.is_active_installation_owner', lambda uow, identity, kind: identity == OWNER),
                ('qorqut.agents.worker_access.is_worker', lambda connection, agent: agent in WORKERS),
                ('qorqut.domains.tasks.ports.ordubashies', lambda uow: {COORDINATOR}),
                ('qorqut_operations.tasks.scope.task_context', lambda ctx, args, uow: ctx)):
            patch(target, value).start()
        self.addCleanup(patch.stopall)

    def boards(self):
        return sorted(row[0] for row in self.conn.execute('SELECT agent_id FROM attention_memberships'))

    def member(self, agent, identity='task-1'):
        self.conn.execute('INSERT INTO attention_memberships(id,agent_id,task_business_id,task_id,task_created_at,created_at) '
                          'VALUES(?,?,NULL,?,?,?)', (agent + ':' + identity, agent, identity, CREATED, CREATED))

    # -- assignment lists the Task on the worker's Todo ----------------------------------------------------------

    def test_coordinator_naming_a_worker_fills_only_that_workers_board(self):
        filled = self.attention.workers_named(context(COORDINATOR), REF, task(), [COMPASS, COORDINATOR, GUEST, SCOUT], self.uow)
        self.assertEqual(filled, [COMPASS, SCOUT])
        self.assertEqual(self.boards(), [SCOUT, COMPASS])
        events = [dict(row) for row in self.conn.execute('SELECT * FROM attention_membership_events ORDER BY agent_id')]
        self.assertEqual([(row['agent_id'], row['actor_id'], row['event_type'], row['rationale']) for row in events],
                         [(SCOUT, COORDINATOR, 'added', 'Named as Worker'), (COMPASS, COORDINATOR, 'added', 'Named as Worker')])
        # The first listing enrols the worker's board, which is what arms its reminders.
        self.assertEqual({row[0]: row[1] for row in self.conn.execute('SELECT agent_id,revision FROM agent_attention')},
                         {COMPASS: 1, SCOUT: 1})
        # Naming the same worker again changes nothing.
        self.assertEqual(self.attention.workers_named(context(COORDINATOR), REF, task(), [SCOUT], self.uow), [])
        self.assertEqual(len(self.boards()), 2)

    def test_only_the_owner_ordubashy_or_the_worker_itself_fills_a_board(self):
        self.assertEqual(self.attention.workers_named(context(SCOUT), REF, task(), [COMPASS, SCOUT], self.uow), [SCOUT])
        self.assertEqual(self.attention.workers_named(context(OWNER, 'human'), REF, task(), [COMPASS], self.uow), [COMPASS])
        self.assertEqual(self.boards(), [SCOUT, COMPASS])

    def test_closed_archived_and_delegated_writes_fill_nothing(self):
        named = lambda ctx, row: self.attention.workers_named(ctx, REF, row, [SCOUT], self.uow)
        self.assertEqual(named(context(COORDINATOR), task('done')), [])
        self.assertEqual(named(context(COORDINATOR), task('cancelled')), [])
        self.assertEqual(named(context(COORDINATOR), task(archived=1)), [])
        self.assertEqual(named(context(COORDINATOR, delegation_binding_id=GUEST, delegation_assignment_id=GUEST, delegation_generation=1), task()), [])
        self.assertEqual(self.boards(), [])

    def test_board_write_policy_and_the_workers_read_access_still_decide(self):
        self.app.authorize.side_effect = DomainError('policy_denied', 'Todo is denied for this caller')
        self.assertEqual(self.attention.workers_named(context(COORDINATOR), REF, task(), [SCOUT], self.uow), [])
        self.app.authorize.side_effect = None

        def read(ctx, ref, uow, **_):
            if ctx.principal_id == SCOUT:
                raise DomainError('forbidden', 'This worker cannot read the Task')
            return task()
        self.attention.task = Mock(side_effect=read)
        self.assertEqual(self.attention.workers_named(context(COORDINATOR), REF, task(), [SCOUT, COMPASS], self.uow), [COMPASS])
        self.assertEqual(self.boards(), [COMPASS])
        # Anything that is not an access refusal is a real fault and fails the write.
        self.attention.task = Mock(side_effect=DomainError('conflict', 'Unexpected'))
        with self.assertRaises(DomainError):
            self.attention.workers_named(context(COORDINATOR), {**REF, 'id': 'task-2'}, task(identity='task-2'), [SCOUT], self.uow)

    def test_host_port_reaches_the_installed_board_and_tolerates_none(self):
        HostBoards(NS()).workers_named(context(COORDINATOR), REF, task(), [SCOUT], self.uow)
        self.assertEqual(self.boards(), [])
        HostBoards(self.app).workers_named(context(COORDINATOR), REF, task(), [SCOUT], self.uow)
        self.assertEqual(self.boards(), [SCOUT])

    # -- a board reminds only about work its agent must move -----------------------------------------------------

    def test_acts_on_reads_who_must_act_next(self):
        self.assertTrue(acts_on(task(workers=[SCOUT, OWNER]), SCOUT, COORDINATOR))
        self.assertTrue(acts_on(task(), SCOUT, COORDINATOR))
        self.assertFalse(acts_on(task(workers=[COORDINATOR]), SCOUT, COORDINATOR))
        self.assertFalse(acts_on({'worker_ids_json': json.dumps([OWNER])}, SCOUT, COORDINATOR))
        # Ordubashy answers for everything on his own board, whoever is named.
        self.assertTrue(acts_on(task(workers=[SCOUT]), COORDINATOR, COORDINATOR))

    def test_worker_board_mode_ignores_tasks_waiting_on_someone_else(self):
        def mode(agent, *rows):
            self.conn.execute('DELETE FROM attention_memberships')
            self.tasks = {row['id']: row for row in rows}
            for row in rows:
                self.member(agent, row['id'])
            return self.attention.board_mode(self.uow, agent)
        self.assertEqual(mode(SCOUT, task('open', [COORDINATOR])), 'empty')
        self.assertEqual(mode(SCOUT, task('blocked', [OWNER])), 'empty')
        self.assertEqual(mode(SCOUT, task('open', [SCOUT])), 'normal')
        self.assertEqual(mode(SCOUT, task('open')), 'normal')
        self.assertEqual(mode(SCOUT, task('working', [COORDINATOR]), task('waiting', [SCOUT], 'task-2')), 'reassess')
        self.assertEqual(mode(SCOUT, task('done', [SCOUT])), 'empty')
        self.assertEqual(mode(COORDINATOR, task('open', [SCOUT])), 'normal')

    def test_reminder_candidates_list_only_the_workers_own_tasks(self):
        self.tasks = {'mine': task('open', [SCOUT], 'mine'), 'unnamed': task('working', (), 'unnamed'),
                      'theirs': task('open', [COORDINATOR], 'theirs'), 'held': task('blocked', [SCOUT], 'held')}
        for identity in self.tasks:
            self.member(SCOUT, identity)
            self.member(COORDINATOR, identity)
        delivery = AttentionRuntime.__new__(AttentionRuntime)
        delivery.runtime, delivery.attention = self.runtime, self.attention
        listed = lambda agent: sorted(row['id'] for row in delivery._snapshot(self.uow, agent, {}, None)['candidates'])
        self.assertEqual(listed(SCOUT), ['mine', 'unnamed'])
        self.assertEqual(listed(COORDINATOR), ['mine', 'theirs', 'unnamed'])

    # -- results and blockers on delegated work reach Ordubashy --------------------------------------------------

    def test_oversight_routes_results_and_blockers_on_delegated_work(self):
        route = lambda row, actor=SCOUT: HostRouting.oversight(self.uow, row, actor)
        for status in ('done', 'cancelled', 'blocked'):
            self.assertEqual(route(task(status, [SCOUT])), {COORDINATOR}, status)
        self.assertEqual(route({'status': 'done', 'worker_ids_json': json.dumps([OWNER, COMPASS])}), {COORDINATOR})
        # Progress on open delegated work, his own change, and work no worker holds add no route.
        for status in ('open', 'working', 'waiting'):
            self.assertEqual(route(task(status, [SCOUT])), set(), status)
        self.assertEqual(route(task('done', [SCOUT]), actor=COORDINATOR), set())
        self.assertEqual(route(task('done', [COORDINATOR, OWNER])), set())
        self.assertEqual(route(task('blocked')), set())

    def test_routes_add_oversight_to_the_mandatory_recipients(self):
        with patch.object(HostRouting, 'mandatory', staticmethod(lambda *a: {COMPASS})), \
                patch('qorqut.domains.tasks.ports.related_subscribers', lambda *a: ['follower']):
            required, optional = HostRouting().routes(self.uow, None, task('blocked', [SCOUT]), SCOUT)
            quiet, _ = HostRouting().routes(self.uow, None, task('working', [SCOUT]), SCOUT)
        self.assertEqual((required, optional), ({COMPASS, COORDINATOR}, ['follower']))
        self.assertEqual(quiet, {COMPASS})


if __name__ == '__main__':
    unittest.main()
