"""New Tasks get short generated ids that no Task in any scope has; older ids keep working."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock
from uuid import uuid4

import jsonschema

from qorqut.composition.assembly import host_assembly
from qorqut.native_chat import references
from qorqut.overseer import operations as overseer_operations
from qorqut.service.changes import ChangeFeed
from qorqut.transport.http import HTTPAPI
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, PolicyDecision, Principal
from qorqut_operations.discussion.threads import owner_post, reads_as_owner_post
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.records.associations import TASK_IDS
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks import identity
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations
from qorqut_operations.tasks.ports import TaskPorts

STAMP = '2026-11-10T12:00:00Z'
LEGACY_UUID = str(uuid4())


def ports():
    noop = lambda *a, **k: None
    return TaskPorts(assignees=NS(protect=noop, owner_directed=lambda *a: False, worker_excluded=lambda *a: False),
                     incidents=NS(link_task=noop),
                     references=NS(types=('task',), host_types=(), resolve=noop, derived=noop),
                     routing=NS(routes=lambda *a: ((), ()), snooze_routes=lambda *a: ()))


class ShortTaskIdTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Database(Path(temporary.name) / 'operations.sqlite3',
                           assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.first, self.second = str(uuid4()), str(uuid4()), str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('principals').insert(dict(id=self.owner, kind='human', display_name='Owner', active=1,
                                                     created_at=STAMP, updated_at=STAMP))
            uow.repository('installation').insert(dict(id='service', installation_id=str(uuid4()),
                                                       owner_principal_id=self.owner, created_at=STAMP, updated_at=STAMP))
            for project, slug in ((self.first, 'first'), (self.second, 'second')):
                uow.repository('projects').insert({'id': project, 'name': slug.title() + ' project', 'slug': slug,
                                                     'workspace': slug, 'hq_path': 'hq-' + slug})
                uow.repository('project_memberships', project_id=project).insert({'principal_id': self.owner,
                                                                                    'is_owner': True})
            # What older installations hold: a UUID, a slug id repeated in two projects, and ids already in the new
            # format in another project, in Global and on an archived Task.
            legacy = ((self.first, LEGACY_UUID, False), (self.first, 'seo-autopilot', False),
                      (self.second, 'seo-autopilot', False), (self.second, 'abcde', False), (None, 'fghjk', False),
                      (self.first, 'mnpqr', True), (self.second, 'k7m2p', False))
            for project, task_id, archived in legacy:
                uow.repository('tasks', project_id=project).insert({'id': task_id, 'title': 'Legacy ' + task_id,
                                                                      'status': 'open', 'created_by': self.owner,
                                                                      'archived': archived})
        self.ctx = InvocationContext(Principal(self.owner, 'human'), source='dashboard',
                                     global_capabilities=frozenset({'read_global_context'}),
                                     allowed_project_ids=frozenset({self.first, self.second}))
        self.registry = OperationRegistry(TASK_ENTRIES)
        self.app = Application(self.db, self.registry, policy=NS(resolve=lambda *a: PolicyDecision('allow', 'test')))
        noop = lambda *a, **k: None
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), lookup)], candidates=NS(candidates=lambda *a: ()))
        self.tasks = TaskOperations(self.app, items, ports=ports())
        for descriptor in self.tasks.descriptors():
            self.registry.register(descriptor)

    def invoke(self, operation, arguments, project=None, key=None, revision=None):
        options = {'idempotency_key': key or str(uuid4())}
        if revision is not None:
            options['expected_revision'] = revision
        ctx = replace(self.ctx, project_id=project)
        return self.app.invoke(ctx, operation, {**({'scope': 'global'} if project is None else {}), **arguments}, options)

    def create(self, project=None, **extra):
        created = self.invoke('tasks_create', {'title': 'Find suppliers', **extra}, project)
        self.assertEqual(created.status, 'ok', created.to_dict())
        return created.data['task']

    def ids(self, task_id):
        with self.db.read() as uow:
            return [row[0] for row in uow.connection.execute('SELECT project_id FROM tasks WHERE id=? ORDER BY project_id',
                                                             (task_id,))]

    def test_generated_ids_are_short_and_use_the_unambiguous_alphabet(self):
        self.assertEqual(identity.TASK_ID_LENGTH, 5)
        self.assertEqual(identity.TASK_ID_ALPHABET, '23456789abcdefghjkmnpqrstuvwxyz')
        self.assertEqual(len(set(identity.TASK_ID_ALPHABET)), 31)
        self.assertFalse(set('01ilo') & set(identity.TASK_ID_ALPHABET))
        for _ in range(500):
            candidate = identity.generate()
            self.assertEqual(len(candidate), 5)
            self.assertTrue(set(candidate) <= set(identity.TASK_ID_ALPHABET), candidate)
        for project in (self.first, None):
            task = self.create(project)
            self.assertEqual(len(task['id']), 5)
            self.assertTrue(set(task['id']) <= set(identity.TASK_ID_ALPHABET), task['id'])
            self.assertEqual(self.ids(task['id']), [project])
        # The length is one constant: a longer id needs no other change.
        with mock.patch.object(identity, 'TASK_ID_LENGTH', 6):
            self.assertEqual(len(self.create(self.second)['id']), 6)

    def test_a_generated_id_used_in_any_scope_is_drawn_again(self):
        # Taken in the other project, in Global, by an archived Task, and (ignoring case) by the other project again.
        draws = iter(['abcde', 'fghjk', 'mnpqr', 'ABCDE', 'stuvw'])
        with mock.patch.object(identity, 'generate', side_effect=lambda: next(draws)):
            task = self.create(self.first)
        self.assertEqual(task['id'], 'stuvw')
        self.assertEqual(self.ids('abcde'), [self.second])
        # A source that only ever repeats a used id ends with a clear, retryable refusal instead of a loop.
        with mock.patch.object(identity, 'generate', return_value='abcde') as generate:
            refused = self.invoke('tasks_create', {'title': 'Another'}, self.first)
        self.assertEqual(refused.error['code'], 'operation_unavailable')
        self.assertTrue(refused.error['retryable'])
        self.assertEqual(generate.call_count, identity.TASK_ID_ATTEMPTS)

    def test_an_explicit_id_used_in_another_scope_is_refused_without_naming_it(self):
        for project, task_id in ((self.first, 'abcde'), (self.first, 'fghjk'), (None, LEGACY_UUID),
                                  (self.second, 'mnpqr'), (self.first, 'ABCDE')):
            refused = self.invoke('tasks_create', {'title': 'Copy', 'id': task_id}, project)
            self.assertEqual(refused.status, 'error', (project, task_id))
            self.assertEqual(refused.error['code'], 'validation_error')
            self.assertEqual(refused.error['field_errors'], {'id': 'already in use'})
            shown = str(refused.to_dict())
            for secret in (self.first, self.second, 'First project', 'Second project', 'Legacy'):
                self.assertNotIn(secret, shown)
        # The same project keeps its earlier answer, which names the current revision.
        same = self.invoke('tasks_create', {'title': 'Copy', 'id': 'seo-autopilot'}, self.first)
        self.assertEqual(same.error['code'], 'revision_conflict')
        self.assertEqual(self.ids('seo-autopilot'), sorted([self.first, self.second]))
        # An unused id is still accepted as given.
        self.assertEqual(self.create(self.second, id='chosen-id')['id'], 'chosen-id')

    def test_repeating_a_create_returns_the_same_task(self):
        for extra in ({}, {'id': 'kept-id'}):
            key = str(uuid4())
            first = self.invoke('tasks_create', {'title': 'Once', **extra}, self.first, key=key)
            again = self.invoke('tasks_create', {'title': 'Once', **extra}, self.first, key=key)
            self.assertEqual(first.status, 'ok', first.to_dict())
            self.assertEqual(again.status, 'ok', again.to_dict())
            self.assertEqual(again.data['task']['id'], first.data['task']['id'])
            self.assertEqual(self.ids(first.data['task']['id']), [self.first])

    def test_apply_checks_again_inside_the_writing_transaction(self):
        ctx = replace(self.ctx, project_id=self.first)
        options = InvocationOptions.from_mapping({'idempotency_key': str(uuid4())})
        with mock.patch.object(identity, 'generate', return_value='vwxyz'):
            with self.db.read() as uow:
                plan = self.tasks.prepare('tasks_create', ctx, {'title': 'Raced'}, options, uow)
                self.tasks.validate_prepared(ctx, plan, uow)
        explicit = None
        with self.db.read() as uow:
            explicit = self.tasks.prepare('tasks_create', ctx, {'title': 'Raced', 'id': 'named-id'}, options, uow)
        # Another write takes both ids in the other project after preparation.
        with self.db.transaction() as uow:
            for task_id in ('vwxyz', 'named-id'):
                uow.repository('tasks', project_id=self.second).insert({'id': task_id, 'title': 'Faster', 'status': 'open',
                                                                         'created_by': self.owner})
        with self.db.transaction() as uow:
            with self.assertRaises(DomainError) as generated:
                self.tasks.validate_prepared(ctx, plan, uow)
            with self.assertRaises(DomainError) as named:
                self.tasks.validate_prepared(ctx, explicit, uow)
        self.assertEqual((generated.exception.code, generated.exception.retryable), ('revision_conflict', True))
        self.assertEqual((named.exception.code, named.exception.field_errors), ('validation_error', {'id': 'already in use'}))

    def test_legacy_uuid_and_slug_ids_still_read_and_update(self):
        for task_id in (LEGACY_UUID, 'seo-autopilot'):
            read = self.invoke('tasks_get', {'task_id': task_id}, self.first)
            self.assertEqual(read.status, 'ok', read.to_dict())
            task = read.data['task']
            updated = self.invoke('tasks_update', {'task_id': task_id, 'changes': {'title': 'Renamed ' + task_id}},
                                  self.first, revision=task['revision'])
            self.assertEqual(updated.status, 'ok', updated.to_dict())
            self.assertEqual(updated.data['task']['id'], task_id)
            commented = self.invoke('tasks_comment', {'task_id': task_id, 'text': 'Still here.'}, self.first)
            self.assertEqual(commented.status, 'ok', commented.to_dict())
        # The same slug in the other project is a different Task and is untouched.
        other = self.invoke('tasks_get', {'task_id': 'seo-autopilot'}, self.second)
        self.assertEqual(other.data['task']['title'], 'Legacy seo-autopilot')
        # A new child of a legacy Task gets a short id and keeps the legacy parent.
        child = self.create(self.first, parent_id='seo-autopilot', blocked_by=[LEGACY_UUID])
        self.assertEqual(len(child['id']), identity.TASK_ID_LENGTH)
        self.assertEqual((child['parent']['id'], list(child['blocked_by'])), ('seo-autopilot', [LEGACY_UUID]))

    def test_owner_text_may_quote_a_short_task_id(self):
        # A short id reads like a word, so owner-facing text is not searched for one: the UUID and short-hash rules
        # are the whole check, and nothing is looked up (that would tell whether a hidden Task has the id).
        task = self.create(self.first)
        current = self.invoke('tasks_get', {'task_id': task['id']}, self.first).data['task']['revision']
        quoted = self.invoke('tasks_update', {'task_id': task['id'], 'changes': {'status_line': 'Waiting on k7m2p.'}},
                             self.first, revision=current)
        self.assertEqual(quoted.status, 'ok', quoted.to_dict())
        owner_post('Done: see k7m2p for the follow-up.')
        self.assertTrue(reads_as_owner_post('Moved to k7m2p'))
        with self.assertRaises(DomainError):
            owner_post('Done: see ' + LEGACY_UUID)

    def move(self, source, destination, task_id):
        ctx = replace(self.ctx, project_id=source)
        with self.db.read() as uow:
            inspection = self.tasks.relocation.inspect(ctx, uow, source, destination, [task_id])
        if not inspection.can_move:
            return inspection.blockers
        with self.db.transaction() as uow:
            self.tasks.relocation.apply(ctx, uow, source, destination, [task_id], inspection.fingerprint(self.owner),
                                        move_id=str(uuid4()), at=STAMP)
        return []

    def test_a_moved_task_keeps_its_short_id_and_it_stays_taken(self):
        task = self.create(None, title='Move me')
        self.assertEqual(self.move(None, self.first, task['id']), [])
        self.assertEqual(self.ids(task['id']), [self.first])
        refused = self.invoke('tasks_create', {'title': 'Copy', 'id': task['id']}, self.second)
        self.assertEqual(refused.error['field_errors'], {'id': 'already in use'})
        refused = self.invoke('tasks_create', {'title': 'Copy', 'id': task['id']}, None)
        self.assertEqual(refused.error['field_errors'], {'id': 'already in use'})
        # An older id held in two projects still cannot be moved onto its twin; the move reports it before writing.
        blockers = self.move(self.first, self.second, 'seo-autopilot')
        self.assertIn('task_id_exists', [blocker['code'] for blocker in blockers])
        self.assertEqual(self.ids('seo-autopilot'), sorted([self.first, self.second]))

    def test_task_id_path_segments_are_one_metric_route(self):
        for task_id in ('k7m2p', 'seo-autopilot', LEGACY_UUID):
            self.assertEqual(HTTPAPI._route_pattern(['project', 'shop', 'task', task_id]), '/api/project/{slug}/task/{id}')
            self.assertEqual(HTTPAPI._route_pattern(['project', 'shop', 'task', task_id, 'done']), '/api/project/{slug}/task/{id}/…')
        self.assertEqual(HTTPAPI._route_pattern(['project', 'shop', 'tasks']), '/api/project/{slug}/tasks')
        self.assertEqual(HTTPAPI._route_pattern(['project', 'shop', 'context', 'file']), '/api/project/{slug}/context/file')
        self.assertEqual(HTTPAPI._route_pattern(['root', 'operations', 'tasks_get']), '/api/root/operations/tasks_get')


class TaskIdContractTests(unittest.TestCase):
    """Input contracts that name a Task take any stored Task id, while the records they sit beside stay UUIDs."""

    IDS = ('k7m2p', 'seo-autopilot', LEGACY_UUID)

    def test_chat_reference_cards_take_any_task_id(self):
        for task_id in self.IDS:
            shown = references.normalize('Look', [{'type': 'task', 'id': task_id, 'project_id': LEGACY_UUID}])
            self.assertEqual(shown['references'][0]['id'], task_id)
        for bad in ('', ' ', 'k7m 2p', 'k7m2p\n', 'a' * 201, 7):
            with self.assertRaises(references.InputError, msg=repr(bad)):
                references.normalize('Look', [{'type': 'task', 'id': bad}])
        with self.assertRaises(references.InputError):  # a Signoff is still named by its UUID
            references.normalize('Look', [{'type': 'signoff', 'id': 'k7m2p', 'project_id': LEGACY_UUID}])

    def test_task_associations_and_overseer_links_take_any_task_id(self):
        checker = jsonschema.FormatChecker()
        ref = lambda task_id: {'provider': 'operations', 'kind': 'task', 'id': task_id, 'revision': '1',
                               'namespace': {'kind': 'global', 'project_id': None}}
        for task_id in self.IDS:
            jsonschema.validate([task_id], TASK_IDS, format_checker=checker)
            jsonschema.validate(ref(task_id), overseer_operations._TASK_REF, format_checker=checker)
        for bad in ([''], ['a' * 201], ['k7m2p', 'k7m2p']):
            with self.assertRaises(jsonschema.ValidationError, msg=repr(bad)):
                jsonschema.validate(bad, TASK_IDS, format_checker=checker)


if __name__ == '__main__':
    unittest.main()
