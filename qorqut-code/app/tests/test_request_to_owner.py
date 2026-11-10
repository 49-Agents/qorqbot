"""The public Task kind rename preserves stored records and exact-once retries."""
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4
from unittest.mock import patch

from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed
from qorqut.transport.mcp_presentation import present
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import DomainError, InvocationContext, OperationResult, PolicyDecision, Principal
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks.kinds import project_result
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations
from qorqut_operations.tasks.ports import TaskPorts


class RequestToOwnerTests(unittest.TestCase):
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
        self.ctx = InvocationContext(Principal(self.owner, 'human'), global_capabilities={'read_global_context'})
        self.registry = OperationRegistry(TASK_ENTRIES)
        self.app = Application(self.db, self.registry, policy=NS(resolve=lambda *a: PolicyDecision('allow', 'test')))
        noop = lambda *a, **k: None
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), lookup)],
                             candidates=NS(candidates=lambda *a: ()))
        ports = TaskPorts(assignees=NS(protect=noop, owner_directed=lambda *a: False,
            worker_excluded=lambda *a: False), focus=NS(schema={}, creation_access=noop, present_created=noop),
            incidents=NS(link_task=noop), references=NS(types=('task',), host_types=(), resolve=noop, derived=noop),
            routing=NS(routes=lambda *a: ((), ()), snooze_routes=lambda *a: ()))
        self.tasks = TaskOperations(self.app, items, ports=ports)
        for descriptor in self.tasks.descriptors():
            self.registry.register(descriptor)

    def invoke(self, operation, arguments, key=None, revision=None):
        options = {'idempotency_key': key} if key else {}
        if revision is not None:
            options['expected_revision'] = revision
        return self.app.invoke(self.ctx, operation, arguments, options)

    def create(self, kind='request_to_owner', key='create', **extra):
        return self.invoke('tasks_create', {'title': 'Review the draft', 'kind': kind,
            'assignee_ids': [self.owner], **extra}, key)

    def counts(self):
        with self.db.read() as uow:
            return {table: uow.connection.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                    for table in ('tasks', 'thread_messages', 'inbox_events', 'idempotency_receipts')}

    def test_alias_retry_preserves_receipt_storage_and_events(self):
        # Simulate a receipt produced before the public rename.
        with patch('qorqut_operations.tasks.operations.public_task', side_effect=lambda task: task):
            old = self.create('owner_request')
        self.assertEqual(old.status, 'ok', old.to_dict())
        self.assertEqual(old.data['task']['kind'], 'request_to_owner')
        counts = self.counts()
        with self.db.read() as uow:
            receipt = dict(uow.connection.execute('SELECT * FROM idempotency_receipts').fetchone())
            self.assertIn('owner_request', receipt['result_json'])
            self.assertNotIn('request_to_owner', receipt['result_json'])
            self.assertEqual(uow.connection.execute('SELECT kind FROM tasks').fetchone()[0], 'owner_request')
        new = self.create('request_to_owner')
        self.assertEqual(new.to_dict(), old.to_dict())
        self.assertEqual(counts, self.counts())
        with self.db.read() as uow:
            self.assertEqual(dict(uow.connection.execute('SELECT * FROM idempotency_receipts').fetchone()), receipt)
        conflict = self.create('ordinary')
        self.assertEqual(conflict.error['code'], 'idempotency_conflict')

    def test_create_validation_and_default_are_unchanged(self):
        for kind in ('owner_request', 'request_to_owner'):
            failed = self.invoke('tasks_create', {'title': 'Owner action', 'kind': kind}, kind)
            self.assertEqual(failed.error['code'], 'validation_error')
            self.assertIn('explicitly assign', failed.error['message'])
        ordinary = self.invoke('tasks_create', {'title': 'Owner asked the agent to research'}, 'ordinary')
        self.assertEqual(ordinary.status, 'ok', ordinary.to_dict())
        self.assertEqual(ordinary.data['task']['kind'], 'ordinary')
        invalid = self.create('not_a_kind', key='invalid')
        self.assertEqual(invalid.error['code'], 'validation_error')

    def test_update_and_all_read_formats_use_public_spelling(self):
        created = self.create('ordinary')
        self.assertEqual(created.status, 'ok', created.to_dict())
        task = created.data['task']
        args = {'task_id': task['id'], 'changes': {'kind': 'request_to_owner'}}
        updated = self.invoke('tasks_update', args, 'update', task['revision'])
        self.assertEqual(updated.status, 'ok', updated.to_dict())
        legacy_retry = self.invoke('tasks_update', {**args, 'changes': {'kind': 'owner_request'}},
                                   'update', task['revision'])
        self.assertEqual(updated.to_dict(), legacy_retry.to_dict())
        for operation, arguments, row_key in [('tasks_get', {'task_id': task['id']}, 'task'),
                                              ('tasks_list', {}, 'tasks')]:
            result = self.invoke(operation, arguments)
            self.assertEqual(result.status, 'ok', result.to_dict())
            for detail in ('compact', 'full', 'text'):
                data = present(operation, result.to_dict(), detail)['data'][row_key]
                self.assertEqual((data[0] if isinstance(data, list) else data)['kind'], 'request_to_owner')

    def test_binding_preserves_authored_text_and_old_plan_arguments(self):
        arguments = {'title': 'owner_request is source text', 'kind': 'request_to_owner',
                     'note': 'owner_request', 'fields': {'kind': 'request_to_owner'}}
        bound = self.registry.bind_arguments('tasks_create', arguments)
        legacy = self.registry.bind_arguments('tasks_create', {**arguments, 'kind': 'owner_request'})
        self.assertEqual(bound, legacy)
        self.assertEqual(bound['note'], arguments['note'])
        self.assertEqual(bound['fields'], arguments['fields'])
        self.assertEqual(arguments['kind'], 'request_to_owner')
        with self.assertRaises(DomainError) as error:
            self.registry.bind_arguments('tasks_update', {'task_id': str(uuid4()),
                'changes': {'kind': 'request_to_owner'}, 'unknown_field': True})
        self.assertEqual(error.exception.code, 'validation_error')

    def test_direct_task_dto_is_canonical_for_embedded_readers(self):
        created = self.create()
        with self.db.read() as uow:
            value = self.tasks.read('tasks_get', self.ctx, {'task_id': created.data['task']['id']}, uow)
        self.assertEqual(value['task']['kind'], 'request_to_owner')

    def test_typed_projection_preserves_authored_and_historical_content(self):
        old = OperationResult.ok({'task': {'kind': 'owner_request', 'fields': {'kind': 'owner_request'},
            'notes': [{'body': 'owner_request', 'kind': 'owner_request'}]},
            'payload': {'kind': 'owner_request'}})
        new = project_result(old)
        self.assertEqual(new.data['task']['kind'], 'request_to_owner')
        self.assertEqual(new.data['task']['fields']['kind'], 'owner_request')
        self.assertEqual(new.data['task']['notes'], old.data['task']['notes'])
        self.assertEqual(new.data['payload'], old.data['payload'])
        self.assertEqual(old.data['task']['kind'], 'owner_request')


if __name__ == '__main__':
    unittest.main()
