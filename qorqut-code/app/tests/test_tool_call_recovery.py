"""A rejected tool call says what the tool accepts, commonly guessed argument names are accepted and reported, and a
missing revision is told apart from a stale one (Task writes need none)."""
import asyncio
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4

from qorqut.transport import mcp
from qorqut_operations.application.plans import require_revision, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, InvocationOptions
import test_task_change_records as fixture

ACTOR = NS(principal=NS(id='agent', kind='agent'))


class Caller:
    scope = 'agent'

    def __init__(self, context):
        self.context = context

    def exposes(self, descriptor):
        return True

    def check_operation(self, canonical_name):
        pass

    def validate_options(self, actor, canonical_name, options):
        pass

    def validate_arguments(self, actor, canonical_name, args):
        pass

    def identity(self, actor):
        return ('agent',)

    def invocation_context(self, actor, *, canonical_name, options, native_binding=None):
        return self.context


class Harness(unittest.TestCase):
    """The Task operations of ``test_task_change_records`` behind the MCP transport's single-call path. Only that
    fixture is borrowed, not its tests; the transport acts as the fixture's worker agent."""

    invoke = fixture.TaskChangeRecordTests.invoke
    create = fixture.TaskChangeRecordTests.create

    def setUp(self):
        fixture.TaskChangeRecordTests.setUp(self)
        self.registry.register(OperationDescriptor(
            'sample_list', schema({'kind': {'enum': ['note', 'plan']}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10}},
                                  ('kind',)),
            aliases=('sample_notes',), alias_bindings={'sample_notes': {'kind': 'note'}}, effect_kind='read',
            supports_signoff=False, scope_kinds=frozenset({'global'}), global_capability='read_global_context',
            read=lambda ctx, args, options, uow: {'kind': args['kind']}))
        instructions = NS(render=lambda key, **values: key + ' ' + values['VALUE0'])
        runtime = NS(registry=self.registry, application=self.app,
                     resolver=NS(require_agentic_view=lambda: NS(runtime_instructions=lambda: instructions)))
        self.transport = mcp.MCPTransport(runtime, None, None, caller=Caller(self.worker))

    def call(self, name, arguments):
        return asyncio.run(self.transport._single_result(name, arguments, ACTOR))

    def write(self, name, arguments, **options):
        return self.call(name, {**arguments, 'idempotency_key': str(uuid4()), **options})


class ValidationErrorTests(Harness):
    def test_unknown_argument_lists_what_the_tool_accepts(self):
        result = self.call('tasks_list', {'keyword': 'suppliers'})
        self.assertEqual(result['status'], 'error')
        error = result['error']
        self.assertEqual((error['code'], error['message']), ('validation_error', 'Operation arguments are invalid'))
        self.assertIn("'keyword' was unexpected", error['field_errors']['$'])
        accepted = error['details']['arguments']
        # The schema the agent calls: the operation's own arguments and the transport's options of a read.
        self.assertEqual(accepted, {'accepted': sorted(mcp.tool_schema(self.registry, 'tasks_list')['properties']),
                                    'required': []})
        for name in ('search', 'statuses', 'limit', 'project_id', 'response_detail', 'if_read_token'):
            self.assertIn(name, accepted['accepted'])
        self.assertNotIn('expected_revision', accepted['accepted'])
        self.assertNotIn('keyword', accepted['accepted'])
        # The hint is kept and names the operation; the summary did not need the instruction catalog.
        self.assertEqual(result['agent_hint'], 'tool-validation-recovery ["tasks_list"]')

    def test_missing_required_and_failing_fields_carry_their_limits(self):
        result = self.write('tasks_complete', {'status': 'finished'})
        accepted = result['error']['details']['arguments']
        self.assertEqual(accepted['required'], ['idempotency_key', 'task_id'])
        self.assertIn('expected_revision', accepted['accepted'])
        # Only the fields that failed are described: the bad enum value and the missing required argument.
        self.assertEqual(set(accepted['fields']), {'status', 'task_id'})
        self.assertEqual(accepted['fields']['status'], {'enum': ['done', 'cancelled']})
        self.assertEqual(accepted['fields']['task_id']['type'], 'string')

    def test_nested_field_and_numeric_limits(self):
        task = self.create()
        nested = self.write('tasks_update', {'task_id': task['id'], 'changes': {'status': 'doing'}})
        fields = nested['error']['details']['arguments']['fields']
        self.assertEqual(list(fields), ['changes.status'])
        self.assertIn('working', fields['changes.status']['enum'])
        limited = self.call('sample_list', {'kind': 'plan', 'limit': 40})
        self.assertEqual(limited['error']['details']['arguments']['fields'],
                         {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10}})

    def test_alias_tool_is_described_with_its_own_binding(self):
        ok = self.call('sample_notes', {})
        self.assertEqual((ok['status'], ok['data']), ('ok', {'kind': 'note'}))
        result = self.call('sample_notes', {'kind': 'plan', 'limit': 0})
        error = result['error']
        self.assertEqual(error['field_errors'], {'kind': 'fixed by alias'})
        accepted = error['details']['arguments']
        # The alias fixes ``kind``: it is not required of the caller and its one allowed value is stated.
        self.assertEqual(accepted['required'], [])
        self.assertEqual(accepted['fields'], {'kind': {'enum': ['note', 'plan'], 'const': 'note'}})
        self.assertEqual(self.call('sample_list', {})['error']['details']['arguments']['required'], ['kind'])
        self.assertEqual(result['agent_hint'], 'tool-validation-recovery ["sample_list"]')

    def test_other_errors_and_successes_are_left_alone(self):
        self.assertNotIn('details', self.call('tasks_list', 'not an object')['error'])
        missing = self.write('tasks_complete', {'task_id': str(uuid4())})
        self.assertEqual(missing['error']['code'], 'not_found')
        self.assertNotIn('details', missing['error'])
        self.assertNotIn('error', self.call('tasks_list', {}))

    def test_summary_survives_an_unreadable_instruction_catalog(self):
        def unavailable():
            raise DomainError('agentic_unavailable', 'Recovery required')
        self.transport.runtime.resolver = NS(require_agentic_view=unavailable)
        result = self.call('tasks_list', {'keyword': 'suppliers'})
        self.assertNotIn('agent_hint', result)
        self.assertIn('search', result['error']['details']['arguments']['accepted'])

    def test_summary_is_small_and_truncates_long_enums(self):
        summary = mcp.accepted_arguments(
            {'properties': {'code': {'enum': list(range(200))}, 'other': {'type': 'string'}}, 'required': ['code']},
            {'code': '999 is not one of [...]'})
        self.assertEqual(summary['fields'], {'code': {'enum': list(range(50)), 'enum_truncated': True}})
        branched = mcp.accepted_arguments(
            {'properties': {'limit': {}}, 'oneOf': [{'properties': {'a': {}}, 'required': ['a']}, {'required': ['b']}]},
            {'$': "{} is not valid under any of the given schemas"})
        self.assertEqual(branched, {'accepted': ['a', 'limit'], 'required': [], 'required_one_of': [['a'], ['b']]})


class ArgumentAliasTests(Harness):
    def test_tasks_list_query_is_search(self):
        self.create()
        self.invoke('tasks_create', {'title': 'Unrelated'})
        result = self.call('tasks_list', {'query': 'suppliers', 'response_detail': 'full'})
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual([task['title'] for task in result['data']['tasks']], ['Find suppliers'])
        self.assertEqual(result['renamed_arguments'], [{'given': 'query', 'used': 'search'}])
        # A call that used the real name carries no note.
        self.assertNotIn('renamed_arguments', self.call('tasks_list', {'search': 'suppliers'}))

    def test_both_names_are_rejected_as_before(self):
        result = self.call('tasks_list', {'query': 'suppliers', 'search': 'suppliers'})
        self.assertEqual(result['error']['code'], 'validation_error')
        self.assertIn("'query' was unexpected", result['error']['field_errors']['$'])
        self.assertNotIn('renamed_arguments', result)

    def test_tasks_comment_body_and_note_are_text(self):
        task = self.create()
        for given in ('body', 'note'):
            with self.subTest(given=given):
                result = self.write('tasks_comment', {'task_id': task['id'], given: 'Found three candidates.'})
                self.assertEqual(result['status'], 'ok', result)
                self.assertEqual(result['renamed_arguments'], [{'given': given, 'used': 'text'}])
        with self.db.read() as uow:
            bodies = [row['body'] for row in uow.connection.execute(
                "SELECT body FROM thread_messages WHERE item_id=? AND kind='comment'", (task['id'],))]
        self.assertEqual(bodies, ['Found three candidates.'] * 2)
        for arguments in ({'body': 'One.', 'text': 'Two.'}, {'body': 'One.', 'note': 'Two.'}):
            rejected = self.write('tasks_comment', {'task_id': task['id'], **arguments})
            self.assertEqual(rejected['error']['code'], 'validation_error', arguments)
            self.assertNotIn('renamed_arguments', rejected)

    def test_idempotent_replay_sees_the_real_name(self):
        task = self.create()
        key = str(uuid4())
        first = self.call('tasks_comment', {'task_id': task['id'], 'body': 'Once.', 'idempotency_key': key})
        again = self.call('tasks_comment', {'task_id': task['id'], 'text': 'Once.', 'idempotency_key': key})
        self.assertEqual((first['status'], again['status']), ('ok', 'ok'))
        self.assertEqual(first['data']['task']['revision'], again['data']['task']['revision'])
        with self.db.read() as uow:
            count = uow.connection.execute("SELECT COUNT(*) FROM thread_messages WHERE item_id=? AND kind='comment'",
                                           (task['id'],)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_rename_is_reported_on_a_rejected_call_too(self):
        result = self.call('tasks_list', {'query': 'suppliers', 'keyword': 'x'})
        self.assertEqual(result['renamed_arguments'], [{'given': 'query', 'used': 'search'}])
        self.assertIn("'keyword' was unexpected", result['error']['field_errors']['$'])
        self.assertNotIn("'query'", result['error']['field_errors']['$'])

    def test_every_listed_alias_renames_only_an_unambiguous_synonym(self):
        # The operations' real argument names, as their descriptors declare them.
        real = {'tasks_list': {'search', 'limit'}, 'tasks_comment': {'task_id', 'text', 'summary'},
                'owner_conversation_send': {'source', 'text', 'summary'},
                'fastmail_messages_search': {'q', 'from', 'to', 'subject', 'limit'},
                'fastmail_reply': {'account_id', 'reply_message_id', 'body', 'subject', 'to'},
                'tables_rows_list': {'table_id', 'query', 'limit'},
                'conversation_history_search': {'query', 'days', 'limit'}}
        self.assertEqual(set(mcp.ARGUMENT_ALIASES), set(real))
        for operation, aliases in mcp.ARGUMENT_ALIASES.items():
            descriptor = NS(name=operation, input_schema={'properties': {name: {} for name in real[operation]}})
            for given, used in aliases.items():
                with self.subTest(operation=operation, given=given):
                    self.assertIn(used, real[operation])
                    self.assertNotIn(given, real[operation])
                    self.assertEqual(mcp.rename_arguments(descriptor, {given: 'value', 'limit': 5}),
                                     ({'limit': 5, used: 'value'}, [{'given': given, 'used': used}]))
                    both = {given: 'value', used: 'other'}
                    self.assertEqual(mcp.rename_arguments(descriptor, both), (both, []))
        for operation in ('tasks_list', 'tasks_comment'):
            properties = self.registry.get(operation).input_schema['properties']
            for given, used in mcp.ARGUMENT_ALIASES[operation].items():
                self.assertIn(used, properties)
                self.assertNotIn(given, properties)

    def test_names_the_operation_or_transport_defines_are_never_renamed(self):
        defined = NS(name='tasks_list', input_schema={'properties': {'search': {}, 'query': {}}})
        self.assertEqual(mcp.rename_arguments(defined, {'query': 'x'}), ({'query': 'x'}, []))
        absent = NS(name='tasks_list', input_schema={'properties': {'limit': {}}})
        self.assertEqual(mcp.rename_arguments(absent, {'query': 'x'}), ({'query': 'x'}, []))
        other = NS(name='tasks_get', input_schema={'properties': {'search': {}}})
        self.assertEqual(mcp.rename_arguments(other, {'query': 'x'}), ({'query': 'x'}, []))
        # A guess with no equivalent is not dropped: it is still rejected, with the accepted names.
        result = self.call('tasks_list', {'account_id': 'x'})
        self.assertIn("'account_id' was unexpected", result['error']['field_errors']['$'])
        self.assertNotIn('renamed_arguments', result)


class RevisionTests(Harness):
    def test_missing_and_mismatched_revisions_say_which(self):
        row = {'revision': 7}
        with self.assertRaises(DomainError) as missing:
            require_revision(row, None)
        with self.assertRaises(DomainError) as stale:
            require_revision(row, 6)
        with self.assertRaises(DomainError) as named:
            require_revision(row, None, argument='merge_revision')
        for raised in (missing, stale, named):
            self.assertEqual((raised.exception.code, raised.exception.current_revision), ('revision_conflict', 7))
        self.assertEqual(missing.exception.message, "expected_revision is required: pass the record's current revision (7)")
        self.assertEqual(stale.exception.message, 'The record changed: its revision is now 7, not 6')
        self.assertTrue(named.exception.message.startswith('merge_revision is required'))
        require_revision(row, 7)
        require_revision(None, None)
        with self.assertRaises(DomainError) as gone:
            require_revision(None, 7)
        self.assertEqual((gone.exception.code, gone.exception.message), ('revision_conflict', 'Resource no longer exists'))

    def test_other_records_still_require_the_revision(self):
        config = self.call('tasks_config_get', {})['data']['config']
        # Nothing is stored yet, so the first write creates the record and has no revision to name.
        created = self.write('tasks_config_update', {'config': config})
        self.assertEqual(created['status'], 'ok', created)
        with self.db.read() as uow:
            stored = uow.repository('project_configs', project_id=None).get('tasks')['revision']
        error = self.write('tasks_config_update', {'config': config})['error']
        self.assertEqual((error['code'], error['current_revision']), ('revision_conflict', stored))
        self.assertEqual(error['message'], f"expected_revision is required: pass the record's current revision ({stored})")
        stale = self.write('tasks_config_update', {'config': config}, expected_revision=stored + 1)['error']
        self.assertEqual((stale['code'], stale['current_revision']), ('revision_conflict', stored))
        self.assertEqual(stale['message'], f'The record changed: its revision is now {stored}, not {stored + 1}')
        current = self.write('tasks_config_update', {'config': config}, expected_revision=stored)
        self.assertEqual(current['status'], 'ok', current)

    def test_task_writes_apply_without_a_revision(self):
        task = self.create()
        revision = task['revision']
        for operation, arguments in (
                ('tasks_update', {'changes': {'due': '2026-12-01'}}),
                ('tasks_assign', {'assignee_ids': [self.agent]}),
                ('tasks_delegate', {'worker_id': self.agent, 'brief': 'Call the three candidates.'}),
                ('tasks_comment', {'text': 'Called two.'}),
                ('tasks_complete', {})):
            with self.subTest(operation=operation):
                result = self.write(operation, {'task_id': task['id'], **arguments, 'response_detail': 'full'})
                self.assertEqual(result['status'], 'ok', result)
                self.assertGreater(result['data']['task']['revision'], revision)
                revision = result['data']['task']['revision']
        with self.db.read() as uow:
            stored = uow.repository('tasks', project_id=None).get(task['id'])
        self.assertEqual((stored['status'], stored['due'], stored['revision']), ('done', '2026-12-01', revision))

    def test_a_supplied_revision_is_still_checked(self):
        task = self.create()
        moved = self.write('tasks_comment', {'task_id': task['id'], 'text': 'First.', 'response_detail': 'full'})
        current = moved['data']['task']['revision']
        for operation, arguments in (
                ('tasks_update', {'changes': {'due': '2026-12-01'}}),
                ('tasks_assign', {'assignee_ids': [self.agent]}),
                ('tasks_delegate', {'worker_id': self.agent}),
                ('tasks_comment', {'text': 'Second.'}),
                ('tasks_complete', {})):
            with self.subTest(operation=operation):
                stale = self.write(operation, {'task_id': task['id'], **arguments}, expected_revision=task['revision'])
                error = stale['error']
                self.assertEqual((error['code'], error['current_revision']), ('revision_conflict', current))
                self.assertEqual(error['message'], f"The record changed: its revision is now {current}, not {task['revision']}")
        kept = self.write('tasks_update', {'task_id': task['id'], 'changes': {'due': '2026-12-01'}}, expected_revision=current)
        self.assertEqual(kept['status'], 'ok', kept)

    def test_a_plan_prepared_without_a_revision_still_pins_the_task_it_read(self):
        task = self.create()
        arguments = {'task_id': task['id'], 'changes': {'due': '2026-12-01'}}
        with self.db.read() as uow:
            plan = self.tasks.prepare('tasks_update', self.ctx, arguments, InvocationOptions(), uow)
        pinned = {target.resource.resource_id: target.revision for target in plan.expected_targets
                  if target.resource.resource_type == 'tasks'}
        self.assertEqual(pinned, {task['id']: task['revision']})
        # The Task moves between preparation and application, as it can while a change waits for a Signoff.
        moved = self.invoke('tasks_comment', {'task_id': task['id'], 'text': 'Meanwhile.'}, ctx=self.worker)
        self.assertEqual(moved.status, 'ok', moved.to_dict())
        with self.db.transaction() as uow:
            with self.assertRaises(DomainError) as raised:
                self.app.check_targets(self.ctx, plan, uow)
        self.assertEqual(raised.exception.code, 'revision_conflict')

    def test_tool_schema_states_the_revision_rule_without_requiring_it(self):
        for operation in ('tasks_update', 'tasks_complete', 'tasks_assign', 'tasks_delegate', 'tasks_comment',
                          'tasks_config_update'):
            tool = mcp.tool_schema(self.registry, operation)
            self.assertNotIn('expected_revision', tool['required'], operation)
            described = tool['properties']['expected_revision']
            self.assertEqual((described['type'], described['minimum']), ('integer', 1))
            self.assertIn('Optional for Task writes', described['description'])
            self.assertIn('Required when editing other existing records', described['description'])
        self.assertNotIn('expected_revision', mcp.tool_schema(self.registry, 'tasks_list')['properties'])


if __name__ == '__main__':
    unittest.main()
