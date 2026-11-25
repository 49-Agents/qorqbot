"""A record reference that names no project gets the project that holds the record, before the call is hashed or planned.

``References`` completes ``{type, id}`` references in an operation's arguments. It writes a project only where the caller
could have named that project itself, leaves every reference that states a project (or ``null`` for Global) as given,
and says nothing about a record it does not complete."""
from dataclasses import replace
import unittest
from uuid import uuid4

from qorqut_operations.application.plans import schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.application.engine import Application
from qorqut_operations.contracts import DomainError, InvocationOptions, plain
from qorqut_operations.records.references import KINDS, Kind, Ref, References

from test_task_project_lookup import Delegation, Harness, STAMP

REFERENCE = {'type': 'object'}
SHAPES = References(
    Ref(('item',)), Ref(('items', '*')), Ref(('changes', 'related'), kind='task', id_key='task_id'),
    Ref(('source',), absent='keep'))


def echo(ctx, args, options, uow):
    return {'arguments': dict(args), 'selected_project_id': ctx.project_id}


class ReferenceCompletionTests(Harness):
    def setUp(self):
        super().setUp()
        properties = {'item': REFERENCE, 'items': {'type': 'array', 'items': REFERENCE}, 'changes': REFERENCE,
                      'source': REFERENCE, 'label': {'type': 'string'}}
        # The sample operations run in Global: where a reference's record is does not select where the call runs.
        self.common = dict(supports_signoff=False, scope_kinds=frozenset({'global'}), global_capability='read_global_context')
        self.registry.register(OperationDescriptor('sample_read', schema(properties), effect_kind='read', read=echo,
                                                   complete_references=SHAPES, **self.common))
        self.in_alpha, self.in_beta, self.in_gamma = (self.create(project)['id'] for project in (self.alpha, self.beta, self.gamma))
        self.in_global = self.create(None)['id']
        self.table = str(uuid4())
        with self.db.transaction() as uow:
            uow.connection.execute(
                'INSERT INTO data_tables(project_id,id,title,columns_json,created_by,created_at,updated_at,table_kind,'
                'crm_config_json) VALUES(?,?,?,?,?,?,?,?,?)',
                (self.beta, self.table, 'Table', '[]', self.owner, STAMP, STAMP, 'general', None))

    def sent(self, arguments, ctx=None, **options):
        result = self.call('sample_read', arguments, ctx=ctx, **options)
        self.assertEqual(result.status, 'ok', result.to_dict())
        return plain(result.data['arguments'])

    def complete(self, arguments, ctx=None, references=SHAPES):
        with self.db.read() as uow:
            return references(self.app, ctx or self.agent_ctx, arguments, uow)

    # -- what is completed -------------------------------------------------------------------------------------------

    def test_a_reference_without_a_project_gets_the_project_that_holds_the_record(self):
        seen = self.sent({'item': {'type': 'task', 'id': self.in_alpha}})
        self.assertEqual(seen['item'], {'type': 'task', 'id': self.in_alpha, 'project_id': self.alpha})

    def test_every_listed_place_is_completed_each_with_its_own_keys(self):
        seen = self.sent({'items': [{'type': 'task', 'id': self.in_beta}, {'type': 'task', 'id': self.in_global}],
                          'changes': {'related': {'task_id': self.in_alpha, 'linked': True}}})
        self.assertEqual(seen['items'], [{'type': 'task', 'id': self.in_beta, 'project_id': self.beta},
                                         {'type': 'task', 'id': self.in_global, 'project_id': None}])
        self.assertEqual(seen['changes'], {'related': {'task_id': self.in_alpha, 'linked': True, 'project_id': self.alpha}})

    def test_the_project_is_not_the_selected_scope(self):
        # The reference is completed; the operation still runs where the caller is.
        result = self.call('sample_read', {'item': {'type': 'task', 'id': self.in_alpha}})
        self.assertEqual((result.status, result.data['selected_project_id']), ('ok', None))
        self.registry.register(OperationDescriptor(
            'sample_project_read', schema({'item': REFERENCE}), effect_kind='read', read=echo, supports_signoff=False,
            scope_kinds=frozenset({'project'}), complete_references=References(Ref(('item',)))))
        selected = self.call('sample_project_read', {'item': {'type': 'task', 'id': self.in_alpha}}, project_id=self.beta)
        self.assertEqual(selected.status, 'ok', selected.to_dict())
        self.assertEqual((selected.data['selected_project_id'], selected.data['arguments']['item']['project_id']),
                         (self.beta, self.alpha))
        unselected = self.call('sample_project_read', {'item': {'type': 'task', 'id': self.in_alpha}})
        self.assertEqual(unselected.error['code'], 'scope_required')

    def test_a_table_and_a_project_are_each_found_their_own_way(self):
        self.registry.register(OperationDescriptor(
            'tables_get', schema({'scope': {'enum': ['project', 'global']}}), effect_kind='read', read=echo,
            supports_signoff=False, scope_kinds=frozenset({'project', 'global'}), global_capability='read_global_context'))
        seen = self.sent({'items': [{'type': 'table', 'id': self.table}, {'type': 'project', 'id': self.beta}]})
        self.assertEqual([item['project_id'] for item in seen['items']], [self.beta, self.beta])
        self.assertEqual(KINDS['signoff'], Kind('signoffs', 'signoffs_get', scope_column='review_project_id'))

    def test_a_kind_whose_read_operation_is_not_installed_is_not_completed(self):
        self.assertFalse(self.registry.known('tables_get'))
        self.assertIsNone(self.sent({'item': {'type': 'table', 'id': self.table}})['item']['project_id'])

    # -- what is left alone ------------------------------------------------------------------------------------------

    def test_a_stated_project_or_global_is_left_exactly_as_given(self):
        for given in (self.alpha, self.beta, self.gamma, None):
            with self.subTest(project_id=given):
                arguments = {'item': {'type': 'task', 'id': self.in_alpha, 'project_id': given}}
                self.assertIs(self.complete(arguments), arguments)
                self.assertEqual(self.sent(arguments), arguments)

    def test_arguments_without_a_reference_are_the_same_object(self):
        arguments = {'label': 'x'}
        self.assertIs(self.complete(arguments), arguments)
        given = {'item': {'type': 'task', 'id': self.in_alpha}}
        self.assertIsNot(self.complete(given), given)
        self.assertEqual(given, {'item': {'type': 'task', 'id': self.in_alpha}})

    # -- nothing outside the caller's reach is written or disclosed ---------------------------------------------------

    def test_a_record_the_caller_cannot_reach_is_completed_like_an_unknown_id(self):
        with self.db.transaction() as uow:
            archived = str(uuid4())
            uow.repository('projects').insert({'id': archived, 'slug': 'delta', 'name': 'Delta', 'workspace': 'delta'})
        owner = self.owner_ctx = replace(self.owner_ctx, allowed_project_ids=self.owner_ctx.allowed_project_ids | {archived})
        in_archived = self.create(archived)['id']
        with self.db.transaction() as uow:
            uow.connection.execute('UPDATE projects SET archived_at=? WHERE id=?', (STAMP, archived))
        unknown = self.sent({'item': {'type': 'task', 'id': str(uuid4())}})['item']['project_id']
        self.assertIsNone(unknown)
        for label, identity, ctx in (('outside the reach', self.in_gamma, None), ('archived', in_archived, owner),
                                     ('in Global', self.in_global, None)):
            with self.subTest(record=label):
                self.assertEqual(self.sent({'item': {'type': 'task', 'id': identity}}, ctx=ctx)['item']['project_id'], unknown)
        self.assertEqual(self.sent({'item': {'type': 'project', 'id': self.gamma}})['item']['project_id'], None)

    def test_a_kind_the_caller_may_not_read_in_the_project_is_not_completed(self):
        self.policy.modes[(self.agent, self.alpha, 'tasks_get')] = 'deny'
        self.assertIsNone(self.sent({'item': {'type': 'task', 'id': self.in_alpha}})['item']['project_id'])
        self.assertEqual(self.sent({'item': {'type': 'task', 'id': self.in_beta}})['item']['project_id'], self.beta)

    def test_a_further_check_of_the_one_record_can_withhold_its_project(self):
        hidden = References(Ref(('item',)), kinds={'task': replace(KINDS['task'], visible=lambda ctx, uow, identity, project: False)})
        self.assertIsNone(self.complete({'item': {'type': 'task', 'id': self.in_alpha}}, references=hidden)['item']['project_id'])

    def test_a_kind_that_is_not_looked_up_reads_as_global(self):
        self.assertEqual(self.sent({'item': {'type': 'browser', 'id': 'main'}})['item'],
                         {'type': 'browser', 'id': 'main', 'project_id': None})
        self.assertEqual(self.sent({'item': {'id': self.in_alpha}})['item'], {'id': self.in_alpha, 'project_id': None})

    def test_a_reference_that_reads_a_missing_key_its_own_way_stays_without_it(self):
        self.assertEqual(self.sent({'source': {'type': 'task', 'id': self.in_gamma}})['source'],
                         {'type': 'task', 'id': self.in_gamma})
        self.assertEqual(self.sent({'source': {'type': 'task', 'id': self.in_alpha}})['source']['project_id'], self.alpha)

    def test_an_owner_session_and_a_delegated_worker_are_completed_only_with_their_bound_project(self):
        session = replace(self.owner_ctx, source='mcp', project_id=self.alpha, owner_session_project_id=self.alpha)
        delegation = Delegation(self.agent, self.alpha, ('tasks_get',), (self.in_alpha, self.in_beta))
        self.app.delegation = delegation
        # A worker that selected nothing is still tied to its assignment's project.
        for label, bound in (('owner session', session), ('delegated worker', delegation.context(self.agent_ctx))):
            for identity, expected in ((self.in_alpha, self.alpha), (self.in_beta, None)):
                with self.subTest(caller=label, task=identity):
                    self.assertEqual(self.complete({'item': {'type': 'task', 'id': identity}}, ctx=bound)['item']['project_id'],
                                     expected)
        self.app.delegation = None
        unbound = self.complete({'item': {'type': 'task', 'id': self.in_alpha}}, ctx=delegation.context(self.agent_ctx))
        self.assertIsNone(unbound['item']['project_id'])

    # -- the call is the explicit one --------------------------------------------------------------------------------

    def test_a_write_is_hashed_and_planned_with_the_full_reference(self):
        planned = []

        def prepare(ctx, args, options, uow):
            planned.append(plain(args))
            raise DomainError('conflict', 'Stopped after the plan saw its arguments')
        descriptor = OperationDescriptor('sample_write', schema({'item': REFERENCE}), prepare=prepare, apply=lambda *a: None,
                                         complete_references=References(Ref(('item',))), **self.common)
        self.registry.register(descriptor)
        short = {'item': {'type': 'task', 'id': self.in_alpha}}
        full = {'item': {'type': 'task', 'id': self.in_alpha, 'project_id': self.alpha}}
        for arguments in (short, full):
            self.assertEqual(self.call('sample_write', arguments).error['code'], 'conflict')
        self.assertEqual(planned, [full, full])
        options = InvocationOptions.from_mapping({'idempotency_key': 'key'})
        with self.db.read() as uow:
            hashes = {Application._input_hash(descriptor, self.app._locate(self.agent_ctx, descriptor, arguments, options, uow)[1],
                                              options, None, None) for arguments in (short, full)}
        self.assertEqual(len(hashes), 1)

    def test_an_internal_worker_is_completed_too(self):
        worker = replace(self.owner_ctx, source='worker')
        self.assertEqual(self.sent({'item': {'type': 'task', 'id': self.in_alpha}}, ctx=worker)['item']['project_id'], self.alpha)


class RecordKindTests(Harness):
    """Every kind of record whose id names it in the whole store is completed the same way, each by its own read rule."""

    READS = ('actions_get', 'routines_get', 'context_items_get', 'context_get', 'notes_get')

    def setUp(self):
        super().setUp()
        for name in self.READS:
            self.registry.register(OperationDescriptor(
                name, schema({'scope': {'enum': ['project', 'global']}}), effect_kind='read', read=echo,
                supports_signoff=False, scope_kinds=frozenset({'project', 'global'}), global_capability='read_global_context'))
        self.references = References(Ref(('item',)))

    def project_of(self, kind, identity, ctx=None):
        with self.db.read() as uow:
            completed = self.references(self.app, ctx or self.agent_ctx, {'item': {'type': kind, 'id': identity}}, uow)
        return completed['item']['project_id']

    def insert(self, sql, *values):
        with self.db.transaction() as uow:
            uow.connection.execute(sql, values)

    def action(self, project, *linked):
        identity = str(uuid4())
        self.insert('INSERT INTO action_logs(project_id,id,category,action_text,recorded_by,origin,occurred_at,created_at,'
                    'updated_at) VALUES(?,?,?,?,?,?,?,?,?)', project, identity, 'project-operational', 'Sent the offer',
                    self.owner, 'manual', STAMP, STAMP, STAMP)
        for link in linked:
            self.insert('INSERT INTO action_projects(action_id,project_id,created_at) VALUES(?,?,?)', identity, link, STAMP)
        return identity

    def document(self, project, kind, agent=None):
        identity = str(uuid4())
        self.insert('INSERT INTO documents(id,project_id,namespace,owning_agent_id,kind,path,created_at,updated_at) '
                    'VALUES(?,?,?,?,?,?,?,?)', identity, project, project or 'global', agent, kind, kind + '/' + identity,
                    STAMP, STAMP)
        return identity

    def test_an_action_is_completed_with_the_project_that_stores_it_when_it_is_linked_there(self):
        linked, loose, elsewhere = self.action(self.alpha, self.alpha), self.action(self.alpha), self.action(self.gamma, self.gamma)
        self.assertEqual(self.project_of('action', linked), self.alpha)
        for identity in (loose, elsewhere, self.action(None, self.alpha), str(uuid4())):
            self.assertIsNone(self.project_of('action', identity))

    def test_a_routine_and_an_entity_are_completed(self):
        routine, entity = str(uuid4()), str(uuid4())
        self.insert('INSERT INTO routines(namespace,project_id,id,name,scope,target,schedule,timezone,status,job_kind,'
                    'arguments_json,timeout_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    self.beta, self.beta, routine, 'Weekly', 'project', 'agent', '0 9 * * 1', 'UTC', 'active', 'prompt', '{}',
                    60, STAMP, STAMP)
        self.insert('INSERT INTO context_entities(project_id,id,label,created_at,updated_at) VALUES(?,?,?,?,?)',
                    self.beta, entity, 'Acme', STAMP, STAMP)
        self.assertEqual((self.project_of('routine', routine), self.project_of('entity', entity)), (self.beta, self.beta))
        self.policy.modes[(self.agent, self.beta, 'context_items_get')] = 'deny'
        self.assertEqual((self.project_of('routine', routine), self.project_of('entity', entity)), (self.beta, None))

    def test_a_context_document_is_completed_as_the_item_type_it_is(self):
        person, topic, material = (self.document(self.alpha, kind) for kind in ('context_person', 'context_topic', 'context_upload'))
        self.assertEqual([self.project_of(kind, identity) for kind, identity in
                          (('person', person), ('topic', topic), ('material', material))], [self.alpha] * 3)
        for kind, identity in (('topic', person), ('material', topic), ('person', material), ('task', person)):
            with self.subTest(named=kind):
                self.assertIsNone(self.project_of(kind, identity))

    def test_a_document_is_completed_by_the_read_operation_of_its_stored_kind(self):
        note, upload = self.document(self.alpha, 'agent_note'), self.document(self.alpha, 'context_upload')
        self.policy.modes[(self.agent, self.alpha, 'notes_get')] = 'deny'
        self.assertEqual((self.project_of('material', note), self.project_of('material', upload)), (None, self.alpha))
        self.policy.modes[(self.agent, self.alpha, 'context_get')] = 'deny'
        self.assertIsNone(self.project_of('material', upload))

    def test_the_journal_is_the_owners_and_working_memory_is_no_item(self):
        journal, memory = self.document(self.alpha, 'journal'), self.document(self.alpha, 'working_memory', agent=self.agent)
        self.assertIsNone(self.project_of('material', journal))
        self.assertEqual(self.project_of('material', journal, ctx=self.owner_ctx), self.alpha)
        for ctx in (self.agent_ctx, self.owner_ctx):
            self.assertIsNone(self.project_of('material', memory, ctx=ctx))

    def test_a_kind_out_of_reach_or_in_global_reads_as_any_unknown_id(self):
        outside, in_global = self.document(self.gamma, 'context_topic'), self.document(None, 'context_topic')
        self.assertEqual([self.project_of('topic', identity) for identity in (outside, in_global, str(uuid4()))], [None] * 3)


if __name__ == '__main__':
    unittest.main()
