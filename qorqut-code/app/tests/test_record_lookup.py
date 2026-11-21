"""An operation that names one existing record by its id runs where that record is, from the id alone.

Tables and rows, CRM entries, Routines, Reminders, Relationships, Learnings and Notes/documents take the generic
``RecordLookup`` hook. Finding a record's project never lets a caller do or learn anything it could not by selecting that
project itself: the hook looks only in the scopes the caller reaches and may read, says nothing about anything else, and a
selection always stands."""
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:
    SERVICE = False

from qorqut.domains.learnings.operations import LearningOperations
from qorqut.reminders.service import ReminderService
from qorqut_operations.background import BackgroundAuthority, Program, ProgramRegistry
from qorqut_operations.contracts import InvocationContext, OperationResult, Principal
from qorqut_operations.crm.operations import CRMOperations
from qorqut_operations.documents.library import SourceLibrary
from qorqut_operations.documents.operations import LOCATED_DOCUMENTS, DocumentSources
from qorqut_operations.documents.store import DocumentStore
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.records.locate import RecordLookup
from qorqut_operations.relationships.operations import RelationshipOperations
from qorqut_operations.relationships.ports import RelationshipPorts, ReviewEdit
from qorqut_operations.routines.operations import RoutineOperations
from qorqut_operations.tables.operations import TableOperations

from test_task_project_lookup import Harness, STAMP

CRM = json.dumps({'stages': [{'id': 'lead', 'name': 'Lead'}], 'columns': []})
LATER = '2031-01-01T00:00:00Z'
WIRED = {
    'tables': ('tables_get', 'tables_rows_list', 'tables_history', 'tables_update', 'tables_rows_write', 'tables_archive',
               'tables_thread_get', 'tables_comment', 'tables_follow'),
    'crm': ('crm_entries_list', 'crm_entries_add', 'crm_entries_update', 'crm_entries_remove'),
    'routines': ('routines_get', 'routines_update', 'routines_delete', 'routines_pause', 'routines_resume', 'routines_run',
                 'routines_stop', 'routines_runs', 'routines_costs'),
    'reminders': ('reminders_get', 'reminders_update', 'reminders_snooze', 'reminders_dismiss', 'reminders_cancel'),
    'relationships': ('relationships_get', 'relationships_update', 'relationships_remove'),
    'learnings': ('learnings_get', 'learnings_decide', 'learnings_review', 'learnings_edit', 'learnings_keep_both',
                  'learnings_intake_status', 'learnings_review_get'),
}
UNWIRED = ('tables_list', 'tables_create', 'tables_history_retry', 'tables_move_to_project', 'routines_list', 'routines_create',
           'routines_preview', 'reminders_create', 'reminders_list', 'relationships_list', 'relationships_create',
           'context_items_get', 'learnings_list', 'learnings_extract', 'notes_list', 'notes_create', 'context_get',
           'context_topics_move', 'context_notes_move')


@dataclass
class Case:
    """One family: how to make a record in a scope, and a read and a write that name it by its id."""
    name: str
    make: object
    read: tuple          # (operation, lambda id -> arguments)
    write: tuple         # (operation, lambda id -> arguments, options)
    caller: str = 'agent'
    write_caller: str = 'agent'
    stated: bool = False  # Global is stated through the ``scope`` argument
    selected_refuses: bool = True
    refusal_write: bool = False  # the write is refused the same way with or without a project (a note's target rule)


class Fixture(Harness):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Delta is archived: no caller reaches it, but the Global and project records in it exist.
        self.delta = str(uuid4())
        with self.db.transaction() as uow:
            uow.repository('projects').insert({'id': self.delta, 'slug': 'delta', 'name': 'Delta', 'workspace': 'delta'})
            uow.connection.execute('UPDATE projects SET archived_at=? WHERE id=?', (STAMP, self.delta))
        capabilities = frozenset({'read_global_context', 'manage_global_routines'})
        with self.db.transaction() as uow:
            row = uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (self.agent,)).fetchone()
            profile = json.loads(row[0])
            profile['scope']['global_capabilities'] = sorted(capabilities)
            uow.connection.execute('UPDATE agent_profiles SET profile_json=? WHERE agent_id=?', (json.dumps(profile), self.agent))
        self.agent_ctx = replace(self.agent_ctx, global_capabilities=capabilities)
        self.owner_ctx = replace(self.owner_ctx, allowed_project_ids=frozenset({self.alpha, self.beta, self.gamma}),
                                 global_capabilities=capabilities)
        self.seen = []
        decide = self.policy.resolve

        def resolve(context, descriptor, targets, uow):
            self.seen.append((descriptor.name, context.project_id))
            return decide(context, descriptor, targets, uow)
        self.policy.resolve = resolve
        self.policy.context_for = lambda principal, project_id=None, review_project_id=None, uow=None: replace(
            self.owner_ctx if principal == self.owner else self.agent_ctx, project_id=project_id,
            review_project_id=review_project_id)
        noop = lambda *a, **k: None
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution('test', ('task',), lookup)], candidates=NS(candidates=lambda *a: ()))
        self.tables = TableOperations(self.db, self.root, lambda project, path: self.root)
        self.crm = CRMOperations(self.app, self.tables, self.tasks, items)
        self.tables.bind_crm(self.crm)
        self.tables.register(self.registry)
        self.crm.register(self.registry)
        for descriptor in RelationshipOperations(items, RelationshipPorts(
                roles={'concerns': ('Concerns', 'Concerned by')}, workflow_roles={},
                materials=NS(matches=lambda *a: True, prepare=noop, apply=noop, filters={}, material_types=('document',), tags={}),
                archive=NS(prepare=noop, apply=noop), navigation=NS(read=noop),
                review_edits={'tasks_update': ReviewEdit('task', 'task_id')})).descriptors():
            self.registry.register(descriptor)
        programs = ProgramRegistry()
        programs.register(Program('probe.project', 1, {'type': 'object'}, lambda *a: None))
        authority = BackgroundAuthority(self.db, programs)
        for descriptor in RoutineOperations(programs, authority).descriptors():
            self.registry.register(descriptor)
        ReminderService(NS(application=self.app, inbox=NS(clock=None), selection=NS(extensions=()), focus=None)).register()
        LearningOperations(self.app, programs, authority).register()
        store = DocumentStore(self.db, operations_root=self.root,
                              document_root=lambda project, path: self.root / (project['slug'] if project else '_global'))
        self.documents = DocumentSources(store, SourceLibrary(store))
        self.documents.register(self.registry, self.app)
        self.cases = self.build_cases()

    # -- records -----------------------------------------------------------------------------------------------------

    def sql(self, statement, parameters=()):
        with self.db.transaction() as uow:
            uow.connection.execute(statement, parameters)

    def table(self, scope, kind='general'):
        identity = str(uuid4())
        self.sql('INSERT INTO data_tables(project_id,id,title,columns_json,created_by,created_at,updated_at,table_kind,'
                 'crm_config_json) VALUES(?,?,?,?,?,?,?,?,?)',
                 (scope, identity, 'Table', '[]', self.owner, STAMP, STAMP, kind, CRM if kind == 'crm' else None))
        return identity

    def edge(self, scope):
        identity = str(uuid4())
        self.sql('INSERT INTO relationships(project_id,id,from_type,from_id,to_type,to_id,role,context,created_at,updated_at,created_by) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (scope, identity, 'task', 'one', 'task', 'two', 'concerns', '', STAMP, STAMP, self.owner))
        return identity

    def routine(self, scope):
        identity = str(uuid4())
        self.sql('INSERT INTO routines(namespace,project_id,id,name,scope,target,schedule,timezone,status,job_kind,arguments_json,'
                 'timeout_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (scope or 'global', scope, identity, 'Routine', 'project' if scope else 'global', 'project' if scope else 'root',
                  '0 9 * * *', 'UTC', 'active', 'probe.project', '{}', 60, STAMP, STAMP))
        return identity

    def reminder(self, scope):
        identity = str(uuid4())
        self.sql('INSERT INTO reminders(id,project_id,creator_id,authority_principal_id,recipient_id,text,state,next_at,created_at,'
                 'updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (identity, scope, self.agent, self.agent, self.agent, 'Call', 'scheduled',
                                                              '2030-01-01T00:00:00Z', STAMP, STAMP))
        return identity

    def learning(self, scope):
        identity = str(uuid4())
        self.sql('INSERT INTO learning_candidates(project_id,id,candidate_json,created_at,updated_at) VALUES(?,?,?,?,?)',
                 (scope, identity, json.dumps({'bucket': 'context', 'scope': 'project', 'tag': 'x', 'text': 'Note', 'created': STAMP}),
                  STAMP, STAMP))
        return identity

    def note(self, scope):
        identity, body = str(uuid4()), b'# Brief\n'
        slug = {self.alpha: 'alpha', self.beta: 'beta', self.gamma: 'gamma', self.delta: 'delta', None: '_global'}[scope]
        (self.root / slug / 'notes').mkdir(parents=True, exist_ok=True)
        (self.root / slug / 'notes' / (identity + '.md')).write_bytes(body)
        self.sql('INSERT INTO documents(id,project_id,namespace,kind,path,content_hash,metadata_json,owning_agent_id,created_at,updated_at) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?)',
                 (identity, scope, scope or 'global', 'project_owned_note' if scope else 'agent_note', f'notes/{identity}.md',
                  'sha256:' + hashlib.sha256(body).hexdigest(), json.dumps({'title': 'Brief'}), None if scope else self.agent,
                  STAMP, STAMP))
        return identity

    def build_cases(self):
        update = {'body': 'Changed.'}
        return {
            'tables': Case('tables', self.table, ('tables_get', lambda i: {'table_id': i}),
                           ('tables_follow', lambda i: {'table_id': i, 'following': True}, {})),
            'crm': Case('crm', lambda scope: self.table(scope, 'crm'), ('crm_entries_list', lambda i: {'table_id': i}),
                        ('tables_follow', lambda i: {'table_id': i, 'following': True}, {})),
            'relationships': Case('relationships', self.edge, ('relationships_get', lambda i: {'relationship_id': i}),
                                  ('relationships_remove', lambda i: {'relationship_id': i}, {'expected_revision': 1})),
            'routines': Case('routines', self.routine, ('routines_get', lambda i: {'routine_id': i}),
                             ('routines_pause', lambda i: {'routine_id': i}, {'expected_revision': 1}), stated=True),
            'reminders': Case('reminders', self.reminder, ('reminders_get', lambda i: {'reminder_id': i}),
                              ('reminders_snooze', lambda i: {'reminder_id': i, 'revision': 1, 'at': LATER}, {}),
                              selected_refuses=False),
            'learnings': Case('learnings', self.learning, ('learnings_get', lambda i: {'learning_id': i}),
                              ('learnings_review', lambda i: {'learning_id': i, 'state': 'rejected'}, {'expected_revision': 1}),
                              write_caller='owner'),
            'notes': Case('notes', self.note, ('notes_get', lambda i: {'document_id': i}),
                          ('notes_update', lambda i: {'document_id': i, **update}, {'expected_revision': 1}), refusal_write=True),
        }

    # -- calls -------------------------------------------------------------------------------------------------------

    def context(self, who):
        return self.owner_ctx if who == 'owner' else self.agent_ctx

    def invoke(self, case, kind, identity, who=None, **options):
        """The read or the write of a case, named by ``identity``, as the case's own caller."""
        operation, arguments = case.read[:2] if kind == 'read' else case.write[:2]
        who = who or (case.caller if kind == 'read' else case.write_caller)
        extra = {} if kind == 'read' else {'idempotency_key': str(uuid4()), **case.write[2]}
        self.seen.clear()
        return self.call(operation, arguments(identity), ctx=self.context(who), **{**extra, **options})

    def ran_in(self, operation):
        """The project each authorization of ``operation`` was decided in, as the engine selected it."""
        return [project for name, project in self.seen if name == operation][-1:]

    @staticmethod
    def answer(result):
        error = result.error or {}
        return result.status, error.get('code'), error.get('message'), 'details' in error

    def refused(self, result, code='not_found'):
        self.assertEqual(result.status, 'error', result.to_dict())
        self.assertEqual(result.error['code'], code, result.to_dict())
        self.assertNotIn('details', result.error)

    def unknown(self, case, kind, **options):
        return self.invoke(case, kind, str(uuid4()), **options)


class RecordLookupTests(Fixture):
    # -- nothing selected --------------------------------------------------------------------------------------------

    def test_a_read_with_nothing_selected_runs_in_the_records_project(self):
        for name, case in self.cases.items():
            for project in (self.alpha, self.beta):
                with self.subTest(family=name, project=project):
                    result = self.invoke(case, 'read', case.make(project))
                    self.assertEqual(result.status, 'ok', result.to_dict())
                    self.assertEqual(self.ran_in(case.read[0]), [project])

    def test_a_write_with_nothing_selected_runs_in_the_records_project_like_the_explicit_call(self):
        for name, case in self.cases.items():
            if case.refusal_write:
                continue
            with self.subTest(family=name):
                identity, key = case.make(self.beta), str(uuid4())
                located = self.invoke(case, 'write', identity, idempotency_key=key)
                self.assertEqual(located.status, 'ok', located.to_dict())
                if case.write_caller == 'agent':
                    self.assertEqual(self.ran_in(case.write[0])[:1], [self.beta])
                self.assertEqual(self.receipts(key), ['project:' + self.beta])
                # The explicit call is the same call: the same key replays the same receipt and result.
                explicit = self.invoke(case, 'write', identity, idempotency_key=key, project_id=self.beta)
                self.assertEqual(explicit.status, 'ok', explicit.to_dict())
                self.assertEqual(self.receipts(key), ['project:' + self.beta])

    def test_a_note_write_is_the_explicit_call_and_keeps_the_target_rule(self):
        case = self.cases['notes']
        identity = case.make(self.alpha)
        located = self.invoke(case, 'write', identity)
        explicit = self.invoke(case, 'write', identity, project_id=self.alpha)
        self.assertEqual(located.error, explicit.error)
        self.assertEqual((located.error['code'], located.error['message']),
                         ('validation_error', 'Project notes require target_project_id'))
        # Naming the project in the argument is a selection: the rule that the target equals the owning project stands.
        wrong = self.call('notes_update', {'document_id': identity, 'body': 'x', 'target_project_id': self.beta}, ctx=self.agent_ctx,
                          idempotency_key=str(uuid4()), expected_revision=1)
        self.assertEqual(wrong.error['code'], 'not_found')
        self.assertEqual(wrong.error['details'], {'id': identity, 'project_id': self.alpha, 'project_name': 'Alpha'})

    def test_a_record_in_global_is_found(self):
        for name, case in self.cases.items():
            with self.subTest(family=name):
                identity = case.make(None)
                result = self.invoke(case, 'read', identity)
                self.assertEqual(result.status, 'ok', result.to_dict())
                self.assertEqual(self.ran_in(case.read[0]), [None])
                if case.refusal_write:
                    continue
                key = str(uuid4())
                written = self.invoke(case, 'write', identity, idempotency_key=key)
                self.assertEqual(written.status, 'ok', written.to_dict())
                self.assertEqual(self.receipts(key), ['global'])

    def test_a_routine_in_global_has_the_global_scope_stated_for_it(self):
        case = self.cases['routines']
        identity, key = case.make(None), str(uuid4())
        located = self.invoke(case, 'write', identity, idempotency_key=key)
        self.assertEqual(located.status, 'ok', located.to_dict())
        # The call is the explicit ``scope=global`` call in every respect: its key replays its receipt.
        explicit = self.call('routines_pause', {'routine_id': identity, 'scope': 'global'}, ctx=self.agent_ctx,
                             idempotency_key=key, expected_revision=1)
        self.assertEqual(explicit.status, 'ok', explicit.to_dict())
        self.assertEqual(self.receipts(key), ['global'])
        # With no Global capability the same call is the ordinary scope_required one.
        weak = replace(self.agent_ctx, global_capabilities=frozenset({'read_global_context'}))
        self.refused(self.call('routines_get', {'routine_id': identity}, ctx=weak), 'scope_required')

    # -- nothing outside the caller's reach is searched or disclosed ---------------------------------------------------

    def test_a_record_the_caller_may_not_reach_answers_like_an_unknown_id(self):
        for name, case in self.cases.items():
            for kind in ('read', 'write'):
                if kind == 'write' and case.refusal_write:
                    continue
                owner = (case.caller if kind == 'read' else case.write_caller) == 'owner'
                for label, scope in (('outside the reach', self.gamma), ('archived', self.delta)):
                    if owner and scope == self.gamma:
                        continue  # the owner reaches every active project
                    with self.subTest(family=name, kind=kind, project=label):
                        hidden = self.invoke(case, kind, case.make(scope))
                        unknown = self.unknown(case, kind)
                        self.assertEqual(self.answer(hidden), self.answer(unknown))
                        self.assertNotIn(self.gamma, json.dumps(hidden.to_dict()))
                        self.assertNotIn(self.delta, json.dumps(hidden.to_dict()))
                        self.assertNotEqual(self.ran_in(case.read[0] if kind == 'read' else case.write[0]), [scope])

    def test_a_family_the_caller_may_not_read_in_a_project_is_not_searched_there(self):
        for name, case in self.cases.items():
            if name == 'reminders':
                continue  # a reminder is loaded by its id alone and reached by its recipient
            with self.subTest(family=name):
                identity = case.make(self.beta)
                self.policy.modes[(self.agent, self.beta, case_read_operation(name))] = 'deny'
                hidden = self.invoke(case, 'read', identity, who='agent')
                self.assertEqual(self.answer(hidden), self.answer(self.unknown(case, 'read', who='agent')))
                self.policy.modes.clear()

    def test_a_reminder_the_caller_cannot_locate_gets_the_ordinary_answer(self):
        case = self.cases['reminders']
        for scope in (self.delta, self.gamma):
            with self.subTest(project=scope):
                hidden = self.invoke(case, 'write', case.make(scope))
                self.assertEqual(self.answer(hidden), self.answer(self.unknown(case, 'write')))

    # -- a selection always wins -------------------------------------------------------------------------------------

    def test_a_selected_project_stands_and_says_where_a_record_in_reach_is(self):
        for name, case in self.cases.items():
            with self.subTest(family=name):
                identity = case.make(self.beta)
                result = self.invoke(case, 'read', identity, project_id=self.alpha)
                if not case.selected_refuses:
                    self.assertEqual(result.status, 'ok', result.to_dict())
                    continue
                self.assertEqual((result.status, result.error['code']), ('error', 'not_found'), result.to_dict())
                self.assertEqual(result.error['details'], {'id': identity, 'project_id': self.beta, 'project_name': 'Beta'})
                self.assertIn('Beta', result.error['message'])
                # The same record with its own project selected is served.
                self.assertEqual(self.invoke(case, 'read', identity, project_id=self.beta).status, 'ok')
            with self.subTest(family=name, record='global'):
                if not case.selected_refuses:
                    continue
                identity = case.make(None)
                found = self.invoke(case, 'read', identity, project_id=self.alpha)
                if name == 'notes':
                    self.assertEqual(found.status, 'ok', found.to_dict())  # a selected project serves Global documents
                else:
                    self.assertEqual(found.error['details'], {'id': identity, 'project_id': None, 'project_name': 'Global'})

    def test_a_selected_project_gives_the_ordinary_answer_for_a_record_it_cannot_reach(self):
        for name, case in self.cases.items():
            with self.subTest(family=name):
                for scope in (self.gamma, self.delta):
                    hidden = self.invoke(case, 'read', case.make(scope), project_id=self.alpha)
                    self.assertEqual(self.answer(hidden), self.answer(self.unknown(case, 'read', project_id=self.alpha)))
                    self.assertNotIn('details', hidden.error or {})

    def test_a_stated_scope_is_a_selection_too(self):
        for name in ('tables', 'relationships', 'routines'):
            case = self.cases[name]
            with self.subTest(family=name):
                identity = case.make(self.beta)
                result = self.invoke(case, 'read', identity, who='agent', **{})
                self.assertEqual(result.status, 'ok')
                operation, arguments = case.read[0], case.read[1](identity)
                stated = self.call(operation, {**arguments, 'scope': 'global'}, ctx=self.agent_ctx)
                self.assertEqual(stated.status, 'error', stated.to_dict())
                self.assertEqual(stated.error['details'], {'id': identity, 'project_id': self.beta, 'project_name': 'Beta'})

    # -- other callers -----------------------------------------------------------------------------------------------

    def test_the_dashboard_is_located_under_the_same_rules_as_an_agent(self):
        for name, case in self.cases.items():
            with self.subTest(family=name):
                identity = case.make(self.alpha)
                if name == 'reminders':
                    self.sql('UPDATE reminders SET recipient_id=? WHERE id=?', (self.owner, identity))
                self.assertEqual(self.invoke(case, 'read', identity, who='owner').status, 'ok')
                self.assertEqual(self.ran_in(case.read[0]), [self.alpha])
                if name == 'reminders':
                    continue  # the dashboard owner is not the recipient of the agent's reminders
                outside = self.invoke(case, 'read', case.make(self.delta), who='owner')
                self.assertEqual(self.answer(outside), self.answer(self.unknown(case, 'read', who='owner')))

    def test_an_internal_worker_is_never_located(self):
        worker = replace(self.owner_ctx, source='worker')
        for name, case in self.cases.items():
            with self.subTest(family=name):
                identity = case.make(self.alpha)
                operation, arguments = case.read[0], case.read[1](identity)
                with patch.object(RecordLookup, '__call__', side_effect=AssertionError('lookup for a worker')):
                    result = self.call(operation, arguments, ctx=worker)
                self.assertNotEqual(self.ran_in(operation), [self.alpha])

    def test_an_owner_session_and_a_delegated_worker_are_not_looked_up_elsewhere(self):
        session = NS(owner_session_project_id=self.alpha, delegation_assignment_id=None, project_id=None)
        delegated = NS(owner_session_project_id=None, delegation_assignment_id=str(uuid4()), project_id=None)
        for name, case in self.cases.items():
            descriptor = self.registry.get(case.read[0])
            identity = case.make(self.alpha)
            for who, ctx in (('owner session', session), ('delegated worker', delegated)):
                with self.subTest(family=name, caller=who):
                    arguments = case.read[1](identity)
                    with self.db.read() as uow, patch.object(RecordLookup, 'place', side_effect=AssertionError('looked up')):
                        self.assertEqual(descriptor.locate_scope(ctx, arguments, uow), (arguments, None))

    def test_an_owner_session_reads_only_its_bound_project(self):
        session = replace(self.owner_ctx, source='mcp', project_id=self.alpha, review_project_id=self.alpha,
                          allowed_project_ids=frozenset({self.alpha}), owner_session_project_id=self.alpha)
        case = self.cases['tables']
        mine, elsewhere = case.make(self.alpha), case.make(self.beta)
        self.assertEqual(self.call('tables_get', {'table_id': mine}, ctx=session).status, 'ok')
        self.refused(self.call('tables_get', {'table_id': elsewhere}, ctx=session))

    # -- exact ids and prepared plans --------------------------------------------------------------------------------

    def test_only_the_exact_id_is_matched(self):
        for name, case in self.cases.items():
            if name in ('notes', 'reminders'):
                continue
            with self.subTest(family=name):
                identity = case.make(self.alpha)
                for form in (str.upper, lambda text: text[:8]):
                    other = form(str(uuid4()))
                    self.assertEqual(self.answer(self.invoke(case, 'read', form(identity))),
                                     self.answer(self.invoke(case, 'read', other)))

    def test_a_staged_write_pins_the_located_project_for_approval(self):
        edge = self.edge(self.alpha)
        self.policy.modes[(self.agent, self.alpha, 'relationships_remove')] = 'signoff'
        staged = []

        def stage(context, descriptor, plan, options, uow):
            staged.append((context, plan))
            return OperationResult(context.request_id, 'signoff_required', signoff={'id': str(uuid4())})
        self.app.signoffs = NS(stage=stage)
        result = self.call('relationships_remove', {'relationship_id': edge}, ctx=self.agent_ctx, expected_revision=1,
                           idempotency_key=str(uuid4()))
        self.assertEqual(result.status, 'signoff_required', result.to_dict())
        context, plan = staged[0]
        self.assertEqual((context.project_id, plan.target_namespace, plan.review_project_id),
                         (self.alpha, 'project:' + self.alpha, self.alpha))
        self.assertNotIn('project_id', plan.arguments)
        proposer = replace(self.agent_ctx, project_id=self.alpha, review_project_id=self.alpha)
        with patch.object(RecordLookup, '__call__', side_effect=AssertionError('lookup during approval')):
            with self.db.read() as uow:
                self.app.validate_prepared(proposer, plan, uow, require_approval=False)

    # -- what takes the hook ---------------------------------------------------------------------------------------------

    def test_every_operation_that_names_one_record_takes_the_hook_and_the_others_do_not(self):
        for family, names in WIRED.items():
            for name in names:
                with self.subTest(operation=name):
                    self.assertIsInstance(self.registry.get(name).locate_scope, RecordLookup)
        for name in LOCATED_DOCUMENTS:
            with self.subTest(operation=name):
                self.assertIsInstance(self.registry.get(name).locate_scope, RecordLookup)
        for name in UNWIRED:
            try:
                descriptor = self.registry.get(name)
            except Exception:
                continue
            with self.subTest(operation=name):
                self.assertNotIsInstance(descriptor.locate_scope, RecordLookup)

    def test_each_hook_reads_its_family_with_that_familys_read_operation(self):
        reads = {'tables': 'tables_get', 'crm': 'tables_get', 'routines': 'routines_get', 'reminders': 'reminders_get',
                 'relationships': 'relationships_get', 'learnings': 'learnings_get'}
        for family, read in reads.items():
            for name in WIRED[family]:
                with self.subTest(operation=name):
                    self.assertEqual(self.registry.get(name).locate_scope.family.read_operation, read)
        self.assertEqual({self.registry.get(name).locate_scope.family.read_operation for name in LOCATED_DOCUMENTS},
                         {'notes_get', 'content_get', 'context_people_get', 'context_get'})

    def test_the_description_says_the_id_alone_is_enough(self):
        for name in ('tables_get', 'routines_get', 'reminders_get', 'relationships_get', 'learnings_get', 'notes_get'):
            with self.subTest(operation=name):
                self.assertIn('Name the record by its id alone', self.registry.get(name).description)


def case_read_operation(name):
    """The read operation whose authority decides where a family may be read."""
    return {'crm': 'tables_get', 'notes': 'notes_get'}.get(name, name + '_get')


@unittest.skipUnless(SERVICE, 'Starlette is not installed')
class RootRouteTests(Fixture):
    """The dashboard's root route takes these operations with the id alone, through the real ``HTTPAPI`` dispatch."""

    def setUp(self):
        super().setUp()
        from qorqut.transport.http import HTTPAPI
        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = NS(database=self.db, require_authority=lambda: None, application=self.app, registry=self.registry,
                         policy=self.policy)
        api.compatibility, api.guards = None, ()
        api.authentication = NS(installation_owner=lambda: (NS(id=self.owner, display_name='Owner'),))
        api.origins = NS(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = NS(record=lambda *args, **options: None)
        api._report_transport_success = lambda *args, **options: None
        api._report_transport_failure = lambda *args, **options: None

        async def handler(request):
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route('/api/{path:path}', handler, methods=['GET', 'POST'])]))
        self.addCleanup(self.client.close)

    def post(self, operation, body):
        return self.client.post('/api/root/operations/' + operation, content=json.dumps(body),
                                headers={'Content-Type': 'application/json'}).json()

    def test_a_table_is_found_from_its_id_alone(self):
        identity = self.table(self.alpha)
        found = self.post('tables_get', {'table_id': identity})
        self.assertEqual((found['status'], found['data']['table']['project_id']), ('ok', self.alpha), found)
        # A stated Global scope is a selection: the record is not there.
        stated = self.post('tables_get', {'table_id': identity, 'scope': 'global'})
        self.assertEqual(stated['error']['code'], 'not_found', stated)
        self.assertEqual(stated['error']['details']['project_id'], self.alpha)
        # A Global table is served either way, and the route still forces Global for an operation that names no table.
        general = self.table(None)
        self.assertEqual(self.post('tables_get', {'table_id': general})['data']['table']['project_id'], None)
        self.assertEqual(self.post('tables_get', {'table_id': general, 'scope': 'global'})['status'], 'ok')
        self.assertEqual(self.post('tables_list', {})['status'], 'ok')

    def test_the_route_still_refuses_a_project_option_and_a_project_scope(self):
        identity = self.table(self.alpha)
        for operation, body in (('tables_get', {'table_id': identity, 'project_id': self.alpha}),
                                ('routines_get', {'routine_id': self.routine(self.alpha), 'review_project_id': self.alpha}),
                                ('relationships_get', {'relationship_id': self.edge(self.alpha), 'project_id': self.alpha})):
            with self.subTest(operation=operation):
                refused = self.post(operation, body)
                self.assertEqual(refused['error']['code'], 'forbidden', refused)
        scoped = self.post('tables_get', {'table_id': identity, 'scope': 'project'})
        self.assertEqual(scoped['error']['code'], 'forbidden', scoped)

    def test_the_other_families_are_accepted_without_a_project(self):
        for operation, body in (('routines_get', {'routine_id': self.routine(self.alpha)}),
                                ('routines_runs', {'routine_id': self.routine(self.beta)}),
                                ('relationships_get', {'relationship_id': self.edge(self.beta)}),
                                ('learnings_get', {'learning_id': self.learning(self.alpha)}),
                                ('notes_get', {'document_id': self.note(self.alpha)})):
            with self.subTest(operation=operation):
                result = self.post(operation, body)
                self.assertEqual(result['status'], 'ok', result)
        # A routine's runs still refuse a stated scope that is not Global; an unknown routine gives the ordinary answer.
        refused = self.post('routines_runs', {'routine_id': self.routine(self.alpha), 'scope': 'project'})
        self.assertEqual(refused['error']['code'], 'forbidden', refused)
        unknown = self.post('routines_get', {'routine_id': str(uuid4())})
        self.assertEqual(unknown['error']['code'], 'scope_required', unknown)

    def test_a_record_outside_the_owners_reach_is_not_found_without_detail(self):
        archived = self.post('tables_get', {'table_id': self.table(self.delta)})
        unknown = self.post('tables_get', {'table_id': str(uuid4())})
        self.assertEqual((archived['error']['code'], archived['error']['message']),
                         (unknown['error']['code'], unknown['error']['message']))
        self.assertNotIn('details', archived['error'])


if __name__ == '__main__':
    unittest.main()
