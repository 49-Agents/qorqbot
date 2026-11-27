"""The record types of installed extensions, read by the host from the extension's copy in the agentic layer: a type exists
while its extension is in effect for some agent, an agent is offered the types of the extensions in effect for it, and a
broken copy or a type id two extensions declare gives no type and one log line (docs/EXTENSION-CONTRACT.md section 9)."""
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import uuid4

from test_extension_registry import APPLIER, ORDUBASHY, StoreCase
from test_task_change_records import ports
from qorqut.extension_records import HostRecordTypes
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import InvocationContext, PolicyDecision, Principal, plain
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.tasks.operations import TASK_ENTRIES, TaskOperations
from qorqut_operations.tasks.record_types import NO_RECORD_TYPES, RecordType

VALUES = {'met_at': 'Lisbon, Web Summit', 'closeness': 'friendly', 'last_contact': '2026-11-20'}
OWNER = SimpleNamespace(agent_id=None)


class ExtensionRecordTypeTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.owner = str(uuid4())
        with self.database.transaction() as uow:
            uow.connection.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) "
                                   "VALUES(?,'human','Owner','now','now')", (self.owner,))
            uow.connection.execute("INSERT INTO installation(id,installation_id,owner_principal_id,created_at,updated_at) "
                                   "VALUES('service',?,?,'now','now')", (str(uuid4()), self.owner))

    def provider(self, **arguments):
        self.found = self.start(**arguments)
        return HostRecordTypes(self.found)

    def types(self, provider, caller=None):
        with self.database.read() as uow:
            active = provider.active(uow)
            offered = provider.for_caller(caller or OWNER, uow)
        return active.ids(), offered.ids()

    def switch(self, on, agent=ORDUBASHY, ext_id='network'):
        self.write(lambda uow: self.found.switch(ext_id, agent, on, uow=uow))

    def copy(self, name):
        """A second extension holding the example's record types."""
        folder = self.downloaded / name
        shutil.copytree(self.downloaded / 'network', folder)
        shutil.move(folder / 'plugin' / 'skills' / 'network', folder / 'plugin' / 'skills' / name)
        skill = folder / 'plugin' / 'skills' / name / 'SKILL.md'
        skill.write_text(skill.read_text().replace('qorqut-plugin-network', f'qorqut-plugin-{name}'))
        self.manifest(folder, lambda data: data.update(id=name, name=name.title()))

    def tasks(self, provider):
        """The Task service over this store with the provider, and a call as the owner in Global."""
        registry = OperationRegistry(TASK_ENTRIES)
        app = Application(self.database, registry, policy=SimpleNamespace(resolve=lambda *a: PolicyDecision('allow', 'test')))
        noop = lambda *a, **k: None
        items = ItemResolver(app, [ItemContribution('test', ('task',), SimpleNamespace(
            resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop))], candidates=SimpleNamespace(candidates=lambda *a: ()))
        tasks = TaskOperations(app, items, ports=ports())
        for descriptor in tasks.descriptors():
            registry.register(descriptor)
        tasks.record_types = provider
        ctx = InvocationContext(Principal(self.owner, 'human'), global_capabilities={'read_global_context'})
        return lambda operation, arguments: app.invoke(ctx, operation, {'scope': 'global', **arguments},
                                                       {} if operation.endswith(('_get', '_list')) else {'idempotency_key': str(uuid4())})

    def ok(self, result):
        self.assertEqual(result.status, 'ok', result.to_dict())
        return result.data

    def refused(self, result, fragment):
        self.assertEqual((result.status, (result.error or {}).get('code')), ('error', 'validation_error'), result.to_dict())
        self.assertIn(fragment, result.error['message'])

    def test_a_type_exists_while_its_extension_is_in_effect_for_some_agent(self):
        self.place()
        provider = self.provider()
        self.assertEqual(self.types(provider), ((), ()))
        self.switch(True)
        worker = SimpleNamespace(agent_id=APPLIER)
        self.assertEqual(self.types(provider), (('connection',), ('connection',)))
        self.assertEqual(self.types(provider, SimpleNamespace(agent_id=ORDUBASHY)), (('connection',), ('connection',)))
        self.assertEqual(self.types(provider, worker), (('connection',), ()))
        with self.database.read() as uow:
            listed = plain(provider.for_caller(OWNER, uow).public())
        self.assertEqual(listed, [{'id': 'connection', 'label': 'Connection', 'plural': 'Connections', 'based_on': 'customer',
                                   'extension': 'network', 'fields': [
                                       {'id': 'met_at', 'label': 'Where we met', 'type': 'string'},
                                       {'id': 'closeness', 'label': 'Closeness', 'type': 'enum',
                                        'values': ['close', 'friendly', 'distant']},
                                       {'id': 'last_contact', 'label': 'Last contact', 'type': 'date'}]}])
        self.switch(False)
        self.assertEqual(self.types(provider), ((), ()))

    def test_a_record_made_meanwhile_keeps_its_type_once_switched_off(self):
        self.place()
        provider = self.provider()
        invoke = self.tasks(provider)
        self.refused(invoke('tasks_create', {'title': 'Ayla', 'record_type': 'connection'}), 'Unknown record type: connection')
        self.switch(True)
        task = self.ok(invoke('tasks_create', {'title': 'Ayla Demir', 'record_type': 'connection', 'fields': dict(VALUES)}))['task']
        self.assertEqual([item['id'] for item in self.ok(invoke('tasks_config_get', {}))['record_types']], ['connection'])
        self.switch(False)
        self.assertEqual(plain(self.ok(invoke('tasks_config_get', {}))['record_types']), [])
        shown = self.ok(invoke('tasks_get', {'task_id': task['id']}))['task']
        self.assertEqual((shown['task_type'], shown['record_type'], dict(shown['fields'])), ('customer', 'connection', VALUES))
        self.ok(invoke('tasks_update', {'task_id': task['id'], 'changes': {'title': 'Ayla Demir Yilmaz'}}))
        self.refused(invoke('tasks_update', {'task_id': task['id'], 'changes': {'fields': {**VALUES, 'closeness': 'close'}}}),
                     'Unknown custom task field: closeness')
        self.refused(invoke('tasks_create', {'title': 'Ben', 'record_type': 'connection'}), 'Unknown record type: connection')
        self.assertEqual([item['id'] for item in self.ok(invoke('tasks_list', {'record_type': 'connection'}))['tasks']], [task['id']])

    def test_a_broken_copy_gives_no_type_and_one_log_line(self):
        self.place()
        provider = self.provider()
        self.switch(True)
        copy = self.layer.root / 'plugins' / 'network' / 'records.yaml'
        copy.write_text(copy.read_text().replace('type: date', 'type: moment'))
        told = []
        self.found.subscribe(told.append)
        with mock.patch.object(self.found, 'refuse', wraps=self.found.refuse) as refuse, \
                mock.patch.object(self.found, 'plugin_file', wraps=self.found.plugin_file) as files, \
                self.assertLogs('qorqut.extension_records', 'WARNING') as logged:
            self.assertEqual(self.types(provider), ((), ()))
            self.assertEqual(self.types(provider), ((), ()))
            self.assertEqual(self.types(provider, SimpleNamespace(agent_id=ORDUBASHY)), ((), ()))
        self.assertEqual(len(logged.output), 1)
        self.assertIn('network', logged.output[0])
        # The registry refused it once, with the finding as its note, and told of the agent it left; a later read does
        # not read the refused copy again.
        self.assertEqual(refuse.call_count, 1)
        self.assertEqual(refuse.call_args.args[0], 'network')
        self.assertEqual(files.call_count, 1)
        [entry] = self.found.list()
        self.assertEqual((entry.status, entry.note), ('needs_attention', refuse.call_args.args[1]))
        self.assertEqual([(change.extension, change.affected) for changes in told for change in changes], [('network', (ORDUBASHY,))])
        copy.write_text(': not yaml [')
        self.assertEqual(self.types(provider), ((), ()))

    def test_a_failing_refusal_still_gives_no_type_and_the_call_goes_on(self):
        self.place()
        provider = self.provider()
        self.switch(True)
        copy = self.layer.root / 'plugins' / 'network' / 'records.yaml'
        copy.write_text(copy.read_text().replace('type: date', 'type: moment'))
        with mock.patch.object(self.found, 'refuse', side_effect=RuntimeError('store')), \
                self.assertLogs('qorqut.extension_records', 'WARNING') as logged:
            self.assertEqual(self.types(provider), ((), ()))
            self.assertEqual(self.types(provider), ((), ()))
        self.assertEqual(len(logged.output), 2)  # the fault, and the refusal that failed

    def test_two_extensions_with_one_type_id_give_it_to_neither(self):
        # Two folders declaring one type id are refused when found; two layer copies can still come to, by a later edit.
        self.place()
        self.copy('contacts')
        records = self.downloaded / 'contacts' / 'plugin' / 'records.yaml'
        records.write_text(records.read_text().replace('id: connection', 'id: contact'))
        provider = self.provider()
        self.assertEqual([entry.status for entry in self.found.list()], ['ready', 'ready'])
        self.switch(True)
        self.switch(True, ext_id='contacts')
        self.assertEqual(self.types(provider), (('contact', 'connection'), ('contact', 'connection')))  # in the registry's order
        copy = self.layer.root / 'plugins' / 'contacts' / 'records.yaml'
        copy.write_text(copy.read_text().replace('id: contact', 'id: connection'))
        with self.assertLogs('qorqut.extension_records', 'WARNING') as logged:
            self.assertEqual(self.types(provider), ((), ()))
            self.assertEqual(self.types(provider), ((), ()))
        self.assertEqual(len(logged.output), 2)  # one line for each of the two extensions
        self.assertTrue(all("'connection'" in line for line in logged.output))
        # Neither is refused: both stay ready and in effect.
        self.assertEqual([(entry.status, entry.agents) for entry in self.found.list()], [('ready', (ORDUBASHY,))] * 2)

    def test_whatever_goes_wrong_gives_no_types_and_one_log_line(self):
        self.place()
        provider = self.provider()
        self.switch(True)
        invoke = self.tasks(provider)
        broken = {'read': mock.patch.object(self.found, 'plugin_file', side_effect=OSError('gone')),
                  'parse': mock.patch('qorqut.extension_records.read_record_types', side_effect=RuntimeError('parser')),
                  'build': mock.patch.object(RecordType, 'from_entry', side_effect=KeyError('fields')),
                  'registry': mock.patch.object(self.found, 'list', side_effect=RuntimeError('store'))}
        for name, patch in broken.items():
            with self.subTest(name), patch, self.assertLogs('qorqut.extension_records', 'WARNING') as logged:
                provider._parsed.clear()
                self.assertEqual(self.types(provider), ((), ()))
                self.assertEqual(self.types(provider), ((), ()))
                # No Tasks call fails because of it: the type is simply not there.
                self.refused(invoke('tasks_create', {'title': 'Ayla', 'record_type': 'connection'}), 'Unknown record type')
                self.ok(invoke('tasks_create', {'title': 'Plain'}))
                self.assertEqual(plain(self.ok(invoke('tasks_config_get', {}))['record_types']), [])
            self.assertEqual(len(logged.output), 1, logged.output)
        provider._parsed.clear()
        self.assertEqual(self.types(provider), (('connection',), ('connection',)))

    def test_one_registry_read_per_unit_of_work(self):
        self.place()
        provider = self.provider()
        self.switch(True)
        invoke = self.tasks(provider)
        with mock.patch.object(self.found, 'list', wraps=self.found.list) as listed, \
                mock.patch.object(self.found, 'plugin_file', wraps=self.found.plugin_file) as files:
            self.ok(invoke('tasks_config_get', {}))
            self.assertEqual((listed.call_count, files.call_count), (1, 1))
            listed.reset_mock(), files.reset_mock()
            # The prepare and the applying transaction read once each, however often the Task code asks.
            task = self.ok(invoke('tasks_create', {'title': 'Ayla Demir', 'record_type': 'connection',
                                                   'fields': dict(VALUES)}))['task']
            self.assertEqual((listed.call_count, files.call_count), (2, 2))
            listed.reset_mock(), files.reset_mock()
            self.ok(invoke('tasks_update', {'task_id': task['id'], 'changes': {'title': 'Ayla'}}))
            self.assertEqual((listed.call_count, files.call_count), (2, 2))
            with self.database.read() as uow:
                for _ in range(3):
                    provider.active(uow), provider.for_caller(OWNER, uow), provider.for_caller(SimpleNamespace(agent_id=APPLIER), uow)
            self.assertEqual((listed.call_count, files.call_count), (3, 3))

    def test_with_no_extension_folder_it_gives_what_no_types_give(self):
        provider = self.provider()
        self.assertEqual(self.found.found, ())
        invoke = self.tasks(provider)
        with mock.patch.object(self.found, 'list') as listed, mock.patch.object(self.found, 'plugin_file') as files, \
                mock.patch.object(self.found, 'on_for') as on_for:
            self.ok(invoke('tasks_create', {'title': 'Plain'}))
            self.assertEqual(plain(self.ok(invoke('tasks_config_get', {}))['record_types']), [])
            uow = SimpleNamespace(connection=None)  # nothing may be read
            self.assertIs(provider.active(uow), NO_RECORD_TYPES)
            self.assertIs(provider.for_caller(OWNER, uow), NO_RECORD_TYPES)
        self.assertEqual((listed.call_count, files.call_count, on_for.call_count), (0, 0, 0))
        with self.database.read() as uow:
            for found in (provider.active(uow), provider.for_caller(OWNER, uow),
                          provider.for_caller(SimpleNamespace(agent_id=ORDUBASHY), uow)):
                self.assertEqual((found.ids(), found.public()), (NO_RECORD_TYPES.ids(), NO_RECORD_TYPES.public()))
                self.assertEqual(found.fields_for(None, {'a': {'type': 'int'}}), NO_RECORD_TYPES.fields_for(None, {'a': {'type': 'int'}}))


if __name__ == '__main__':
    unittest.main()
