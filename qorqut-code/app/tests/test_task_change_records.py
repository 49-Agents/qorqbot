"""A Task write says what it changed, a comment needs no prior read, and newly named Workers reach the boards port."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4

from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut.transport.mcp_presentation import present
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import DomainError, InvocationContext, PolicyDecision, Principal
from qorqut_operations.inbox.service import ENTRY_TEXT, InboxService
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.roles import GLOBAL_WORKER_ROLE
from qorqut_operations.storage.assembly import AssemblyError
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks import events
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations
from qorqut_operations.tasks.overview import TaskOverview
from qorqut_operations.tasks.ports import TaskPorts


def ports(**extra):
    noop = lambda *a, **k: None
    return TaskPorts(**{**dict(
        assignees=NS(protect=noop, owner_directed=lambda *a: False, worker_excluded=lambda *a: False),
        focus=NS(schema={}, creation_access=noop, present_created=noop), incidents=NS(link_task=noop),
        references=NS(types=('task',), host_types=(), resolve=noop, derived=noop),
        routing=NS(routes=lambda *a: ((), ()), snooze_routes=lambda *a: ())), **extra})


class TaskChangeRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'operations.sqlite3',
                           assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner = str(uuid4())
        stamp = '2026-11-10T12:00:00Z'
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.owner, kind='human', display_name='Owner',
                active=1, created_at=stamp, updated_at=stamp))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                owner_principal_id=self.owner, created_at=stamp, updated_at=stamp))
            # Registered global workers: the one kind of agent that is assignable and addressable in Global.
            self.agent, self.reader = str(uuid4()), str(uuid4())
            for identity, name in ((self.agent, 'Scout'), (self.reader, 'Compass')):
                uow.repository('principals').insert(dict(id=identity, kind='agent', display_name=name,
                    active=1, created_at=stamp, updated_at=stamp))
                uow.repository('agent_profiles', business_id=None).insert(dict(agent_id=identity, principal_id=identity,
                    home_business_id=None, namespace='ecosystem', name=name.lower(), workspace='/tmp/' + name.lower(),
                    status='active', profile_json={'status': 'active', 'role': {'id': GLOBAL_WORKER_ROLE}, 'scope': {
                        'kind': 'ecosystem', 'business_id': None, 'global_capabilities': ['read_global_context']}},
                    created_at=stamp, updated_at=stamp))
        self.ctx = InvocationContext(Principal(self.owner, 'human'), global_capabilities={'read_global_context'})
        self.worker = InvocationContext(Principal(self.agent, 'agent'), global_capabilities={'read_global_context'},
                                        profile_revision=1)
        self.named = []
        self.registry = OperationRegistry(TASK_ENTRIES)
        self.app = Application(self.db, self.registry, policy=NS(resolve=lambda *a: PolicyDecision('allow', 'test')))
        noop = lambda *a, **k: None
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), lookup)],
                             candidates=NS(candidates=lambda *a: ()))
        boards = NS(workers_named=lambda ctx, item, task, worker_ids, uow: self.named.append((item['id'], list(worker_ids))))
        # Every Task event is addressed to a second agent, so a write by the first leaves it a pending event to read.
        routing = NS(routes=lambda *a: ((self.reader,), ()), snooze_routes=lambda *a: ())
        self.tasks = TaskOperations(self.app, items, ports=ports(boards=boards, routing=routing))
        for descriptor in self.tasks.descriptors():
            self.registry.register(descriptor)

    def invoke(self, operation, arguments, key=None, revision=None, ctx=None):
        options = {'idempotency_key': key or str(uuid4())}
        if revision is not None:
            options['expected_revision'] = revision
        return self.app.invoke(ctx or self.ctx, operation, arguments, options)

    def create(self, **extra):
        created = self.invoke('tasks_create', {'title': 'Find suppliers', **extra})
        self.assertEqual(created.status, 'ok', created.to_dict())
        return created.data['task']

    def system_entries(self, task_id):
        with self.db.read() as uow:
            return [(row['body'], json.loads(row['change_json'])) for row in uow.connection.execute(
                "SELECT body,change_json FROM thread_messages WHERE item_id=? AND kind='system' ORDER BY created_at,id",
                (task_id,))]

    def test_comment_needs_no_revision_but_honours_one(self):
        task = self.create()
        comment = lambda text, **options: self.invoke('tasks_comment', {'task_id': task['id'], 'text': text},
                                                      ctx=self.worker, **options)
        first = comment('Found three candidates.')
        self.assertEqual(first.status, 'ok', first.to_dict())
        # The comment still moves the Task's revision, so a field write prepared against the old one conflicts.
        self.assertEqual(first.data['task']['revision'], task['revision'] + 1)
        stale = comment('Second.', revision=task['revision'])
        self.assertEqual(stale.error['code'], 'revision_conflict')
        current = comment('Second.', revision=first.data['task']['revision'])
        self.assertEqual(current.status, 'ok', current.to_dict())
        for operation, arguments in (('tasks_update', {'task_id': task['id'], 'changes': {'due': '2026-12-01'}}),
                                     ('tasks_complete', {'task_id': task['id']})):
            unread = self.invoke(operation, arguments)
            self.assertEqual(unread.error['code'], 'revision_conflict', operation)

    def test_system_entry_names_what_changed(self):
        task = self.create()
        updated = self.invoke('tasks_update', {'task_id': task['id'], 'changes': {
            'due': '2026-12-01', 'worker_ids': [self.owner], 'overview': 'Three candidates so far.',
            'title': 'Find and vet suppliers'}}, revision=task['revision'])
        self.assertEqual(updated.status, 'ok', updated.to_dict())
        done = self.invoke('tasks_complete', {'task_id': task['id']}, revision=updated.data['task']['revision'])
        self.assertEqual(done.status, 'ok', done.to_dict())
        (body, change), (closing, closed) = self.system_entries(task['id'])
        self.assertEqual(body, 'Title changed. Due set to 2026-12-01. Workers: added Owner. Overview updated.')
        # Authored text is reported as changed, never copied into the system line.
        self.assertNotIn('vet', body)
        self.assertNotIn('candidates', body)
        self.assertEqual(change, {'from': None, 'to': None, 'details': True,
                                  'fields': ['title', 'due', 'workers', 'overview']})
        # Completion alone is a state change: the completion stamp is not a second, unnamed detail.
        self.assertEqual(closing, 'State changed from open to done.')
        self.assertEqual(closed, {'from': 'open', 'to': 'done', 'details': False, 'fields': []})

    def test_unnamed_detail_keeps_the_generic_sentence(self):
        before = {'row': {'status': 'open', 'title': 'A', 'kind': 'ordinary'}, 'assignees': [], 'blockers': [], 'tags': []}
        after = {'row': {'status': 'open', 'title': 'A', 'kind': 'owner_request'}, 'assignees': [], 'blockers': [], 'tags': []}
        self.assertEqual(events.describe(before, after),
                         ('Task details changed.', {'from': None, 'to': None, 'details': True, 'fields': []}))
        many = {**after, 'row': {**after['row'], 'kind': 'ordinary'}, 'tags': [f'tag-{n}' for n in range(8)]}
        body, change = events.describe(before, many)
        self.assertEqual(body, 'Tags: added tag-0, tag-1, tag-2, tag-3, tag-4 and 3 more.')
        self.assertEqual(change['fields'], ['tags'])

    def test_boards_port_hears_only_newly_named_workers(self):
        task = self.create(worker_ids=[self.owner])
        self.assertEqual(self.named, [(task['id'], [self.owner])])
        untouched = self.invoke('tasks_update', {'task_id': task['id'], 'changes': {'due': '2026-12-01'}},
                                revision=task['revision'])
        cleared = self.invoke('tasks_update', {'task_id': task['id'], 'changes': {'worker_ids': []}},
                              revision=untouched.data['task']['revision'])
        self.assertEqual(cleared.status, 'ok', cleared.to_dict())
        self.assertEqual(len(self.named), 1)
        renamed = self.invoke('tasks_update', {'task_id': task['id'], 'changes': {'worker_ids': [self.owner]}},
                              revision=cleared.data['task']['revision'])
        self.assertEqual(renamed.status, 'ok', renamed.to_dict())
        self.assertEqual(self.named[1:], [(task['id'], [self.owner])])

    def test_boards_port_is_optional_and_checked(self):
        self.assertIsNone(ports().boards)
        with self.assertRaises(AssemblyError):
            ports(boards=NS())

    def test_pending_task_event_carries_its_thread_entries(self):
        task = self.create()
        long_note = 'Checked the registry. ' * 60
        started = self.invoke('tasks_update', {'task_id': task['id'], 'changes': {'status': 'working'}, 'note': long_note},
                              revision=task['revision'], ctx=self.worker)
        self.assertEqual(started.status, 'ok', started.to_dict())
        with self.db.read() as uow:
            event = uow.connection.execute("SELECT * FROM inbox_events WHERE source_type='tasks' AND source_id=? "
                                           "ORDER BY id DESC LIMIT 1", (task['id'],)).fetchone()
            entries = InboxService.task_entries(None, event, uow)
            other = InboxService.task_entries(None, {**dict(event), 'source_type': 'signoffs'}, uow)
        self.assertEqual(other, [])
        self.assertEqual(entries[0], {'kind': 'system', 'text': 'State changed from open to working.'})
        self.assertEqual(entries[1], {'kind': 'note', 'text': long_note.strip()[:ENTRY_TEXT], 'truncated': True, 'by': 'Scout'})
        shown = present('inbox_list', {'status': 'ok', 'data': {'items': [
            {'id': 1, 'source_type': 'tasks', 'source_id': task['id'], 'entries': entries, 'visibility_json': '{}'}]}})
        self.assertEqual(shown['data']['items'][0]['entries'], entries)

    def test_holders_view_groups_open_work_by_who_must_act_next(self):
        held = self.create(worker_ids=[self.owner])
        loose = self.create()
        closed = self.create(worker_ids=[self.owner])
        self.invoke('tasks_complete', {'task_id': closed['id']}, revision=closed['revision'])
        customer = self.create(task_type='customer')
        working = self.invoke('tasks_update', {'task_id': held['id'], 'changes': {'status': 'working'}},
                              revision=held['revision']).data['task']
        later = datetime.now(timezone.utc) + timedelta(days=2)
        overview = TaskOverview(NS(clock=lambda: later, project_input=lambda ctx, tasks, uow: tasks),
                                NS(authorize=lambda *a, **k: None))
        ctx = self.worker
        with self.db.read() as uow:
            owner = uow.repository('principals').get(self.owner)
            result = overview.holders(ctx, {}, uow, None, owner)
            recent = overview.holders(ctx, {'updated_since': later.isoformat()}, uow, None, owner)
            with self.assertRaises(DomainError) as refused:
                overview.holders(ctx, {'assignee': 'owner'}, uow, None, owner)
        self.assertEqual(refused.exception.code, 'validation_error')
        self.assertEqual(result['counts'], {'tasks': 2, 'stale': 1, 'unassigned': 1})
        by_kind = {group['holder']['kind']: group for group in result['holders']}
        self.assertEqual(list(by_kind), ['owner', 'unassigned'])
        self.assertEqual(by_kind['owner']['counts'], {'open': 0, 'working': 1, 'blocked': 0, 'waiting': 0, 'stale': 1, 'total': 1})
        self.assertEqual([(task['id'], task['status'], task.get('stale')) for task in by_kind['owner']['tasks']],
                         [(working['id'], 'working', True)])
        # Untouched backlog is not marked stale, and neither a closed Task nor a Customer record is open work.
        self.assertEqual([(task['id'], task.get('stale')) for task in by_kind['unassigned']['tasks']], [(loose['id'], None)])
        listed = {task['id'] for group in result['holders'] for task in group['tasks']}
        self.assertFalse({closed['id'], customer['id']} & listed)
        self.assertEqual(recent['counts']['tasks'], 0)


if __name__ == '__main__':
    unittest.main()
