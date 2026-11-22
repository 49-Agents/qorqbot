"""The record references of reminders, Focus, pinned threads, timelines, project links, Chat and private conversations name a
record by type and id; the project that holds it is found from the id.

Each shape is the real descriptor of its operation, registered over the Task fixture of ``test_task_project_lookup``: its
schema takes the reference without ``project_id``, and ``Application._locate`` hands the operation the full reference."""
from dataclasses import replace
from types import SimpleNamespace as NS
import unittest
from uuid import uuid4

from qorqut.domains.projects.compose import ProjectRecords
from qorqut.domains.thread_pins import ThreadPins
from qorqut.domains.timeline import Timeline
from qorqut.inbox.attention import AttentionBoard
from qorqut.inbox.focus import FocusService
from qorqut.inbox.private_conversations import PrivateConversations
from qorqut.native_chat.user_input import UserInput
from qorqut.reminders.service import ReminderService
from qorqut_operations.contracts import InvocationOptions
from qorqut_operations.records.references import Kind

from test_task_project_lookup import Harness


class OwnerReferenceTests(Harness):
    def setUp(self):
        super().setUp()
        app, registry = self.app, self.registry
        runtime = NS(application=app, registry=registry, paths=NS(runtime='/nonexistent'))
        ReminderService(NS(application=app, inbox=NS(clock=None), selection=NS(extensions=()), focus=None)).register()
        ThreadPins(runtime).register()
        Timeline(runtime).register()
        UserInput(runtime).register()
        PrivateConversations(NS(app=app, domains=NS(), clock=None)).register()
        FocusService(NS(app=app, domains=NS(items=NS(kinds=('task', 'table', 'signoff', 'project')))), None).register()
        ProjectRecords(app, NS(kinds=())).register(registry)
        self.in_alpha, self.in_beta, self.in_gamma = (self.create(project)['id'] for project in (self.alpha, self.beta, self.gamma))
        self.in_global = self.create(None)['id']
        self.unknown = str(uuid4())

    def completed(self, operation, arguments, ctx=None):
        """The arguments the operation sees for a caller's call: schema-valid as sent, completed before anything else."""
        descriptor = self.registry.get(operation)
        descriptor.validate(arguments)
        with self.db.read() as uow:
            return self.app._locate(ctx or self.agent_ctx, descriptor, arguments, InvocationOptions(), uow)[1]

    def task(self, identity, **more):
        return {'type': 'task', 'id': identity, **more}

    def check_task(self, operation, build, read, *, absent=None):
        """A Task in a reachable project is completed; a Global Task, an unknown id and a Task outside the reach read alike
        (Global, or no key where the shape reads a missing key its own way); a stated project or null is left as given."""
        with self.subTest(operation=operation, case='project'):
            self.assertEqual(read(self.completed(operation, build(self.task(self.in_alpha)))),
                             self.task(self.in_alpha, project_id=self.alpha))
        unknown = read(self.completed(operation, build(self.task(self.unknown))))
        self.assertEqual(unknown, self.task(self.unknown) if absent else self.task(self.unknown, project_id=None))
        for label, identity in (('global', self.in_global), ('outside reach', self.in_gamma)):
            with self.subTest(operation=operation, case=label):
                self.assertEqual(read(self.completed(operation, build(self.task(identity)))),
                                 {**unknown, 'id': identity})
        for stated in (self.alpha, self.beta, self.gamma, None):
            with self.subTest(operation=operation, case='stated', project_id=stated):
                given = build(self.task(self.in_alpha, project_id=stated))
                self.assertEqual(self.completed(operation, given), given)

    # -- reminders ---------------------------------------------------------------------------------------------------

    def test_a_reminder_link_names_its_record_by_type_and_id(self):
        self.check_task('reminders_create', lambda link: {'text': 'Look at it', 'linked_item': link}, lambda a: a['linked_item'])
        self.check_task('reminders_update', lambda link: {'reminder_id': str(uuid4()), 'revision': 1, 'linked_item': link},
                        lambda a: a['linked_item'])
        cleared = {'text': 'Look at it', 'linked_item': None}
        self.assertEqual(self.completed('reminders_create', cleared), cleared)

    # -- Focus -------------------------------------------------------------------------------------------------------

    def test_focus_cards_and_attention_references_name_their_record_by_type_and_id(self):
        self.check_task('focus_show', lambda card: {'scope': 'global', 'consumer_id': str(uuid4()), 'expected_focus_revision': 0,
                                                   'status': 'On it', 'items': [{**card, 'reason': 'Because'}]},
                        lambda a: {k: v for k, v in a['items'][0].items() if k != 'reason'})
        for field in ('source', 'context'):
            self.check_task('focus_present', lambda ref, field=field: {'scope': 'global', field: ref} if field == 'source' else
                            {'scope': 'global', 'source': ref, 'context': ref}, lambda a, field=field: a[field])
        self.check_task('focus_present', lambda ref: {'scope': 'global', 'source': ref, 'references': [ref]},
                        lambda a: a['references'][0])
        self.check_task('focus_edit', lambda ref: {'scope': 'global', 'item_id': 'card', 'expected_card_revision': 1,
                                                  'changes': {'references': [ref]}}, lambda a: a['changes']['references'][0])
        project = self.completed('focus_present', {'scope': 'global', 'source': {'type': 'project', 'id': self.alpha}})
        self.assertEqual(project['source'], {'type': 'project', 'id': self.alpha, 'project_id': self.alpha})

    def test_a_focus_target_is_completed_only_when_a_project_holds_its_record(self):
        def target(value, **more):
            return {'scope': 'global', 'reference': {'key': 'k', 'title': 'T', 'target': value}, **more}
        self.check_task('focus_present', target, lambda a: a['reference']['target'], absent=True)
        edit = lambda value: {'scope': 'global', 'item_id': 'card', 'expected_card_revision': 1, 'changes': {'target': value}}
        self.check_task('focus_edit', edit, lambda a: a['changes']['target'], absent=True)
        for opening in ({'type': 'url', 'url': 'https://example.com'}, {'type': 'desktop'}):
            self.assertEqual(self.completed('focus_present', target(opening))['reference']['target'], opening)
        self.assertEqual(AttentionBoard.named({'type': 'task', 'id': 'x'}), {'type': 'task', 'id': 'x', 'project_id': None})
        self.assertEqual(AttentionBoard.named({'type': 'url', 'url': 'https://example.com'}),
                         {'type': 'url', 'url': 'https://example.com'})
        self.assertEqual(AttentionBoard.named({'type': 'task', 'id': 'x', 'project_id': self.alpha}),
                         {'type': 'task', 'id': 'x', 'project_id': self.alpha})

    # -- pinned threads ----------------------------------------------------------------------------------------------

    def test_a_pinned_thread_names_its_record_by_type_and_id(self):
        self.check_task('thread_pins_set', lambda source: {'scope': 'global', 'source': source, 'pinned': True},
                        lambda a: a['source'])
        reference = self.completed('thread_pins_set', {'scope': 'global', 'source': {'type': 'reference', 'id': 'card'},
                                                        'pinned': True})['source']
        self.assertEqual(reference, {'type': 'reference', 'id': 'card', 'project_id': None})

    # -- timelines ---------------------------------------------------------------------------------------------------

    def test_a_timeline_item_names_its_record_by_type_and_id(self):
        self.check_task('timeline_get', lambda item: {'scope': 'global', 'item': item}, lambda a: a['item'])
        self.check_task('timeline_file', lambda item: {'scope': 'global', 'item': item,
                                                      'changes': [{'action': 'drop', 'question_id': 'q'}]}, lambda a: a['item'])
        self.check_task('timeline_file_answer', lambda item: {'scope': 'global', 'signoff_id': str(uuid4()), 'kind': 'fact',
                                                             'text': 'Yes', 'item': item}, lambda a: a['item'])
        for operation, build in (('timeline_get', lambda item: {'scope': 'global', 'item': item}),
                                 ('timeline_file_answer', lambda item: {'scope': 'global', 'signoff_id': str(uuid4()),
                                                                        'kind': 'fact', 'text': 'Yes', 'item': item})):
            with self.subTest(operation=operation):
                seen = lambda item: self.completed(operation, build(item))['item']
                self.assertEqual(seen({'type': 'project', 'id': self.alpha}), {'type': 'project', 'id': self.alpha, 'project_id': self.alpha})
                self.assertEqual(seen({'type': 'project', 'id': self.gamma}), {'type': 'project', 'id': self.gamma, 'project_id': None})
                self.assertEqual(seen({'type': 'project', 'id': self.unknown}), {'type': 'project', 'id': self.unknown, 'project_id': None})
                self.assertEqual(seen({'type': 'global', 'id': 'global'}), {'type': 'global', 'id': 'global', 'project_id': None})
        answer = {'scope': 'global', 'signoff_id': str(uuid4()), 'kind': 'fact', 'text': 'Yes'}
        self.assertEqual(self.completed('timeline_file_answer', answer), answer)

    # -- project links -----------------------------------------------------------------------------------------------

    def test_a_project_link_names_its_item_by_type_and_id(self):
        for operation in ('project_item_get', 'project_items_link'):
            self.check_task(operation, lambda item: {'project_id': self.alpha, 'scope': 'project', 'item': item}, lambda a: a['item'])
            seen = self.completed(operation, {'project_id': self.alpha, 'item': {'type': 'project', 'id': self.beta}})
            self.assertEqual(seen['item'], {'type': 'project', 'id': self.beta, 'project_id': self.beta})
            self.assertEqual(seen['project_id'], self.alpha)

    def test_a_project_link_looks_up_every_kind_whose_id_names_one_record(self):
        for operation in ('project_item_get', 'project_items_link'):
            kinds = self.registry.get(operation).complete_references.kinds
            self.assertEqual(set(kinds), {'task', 'table', 'signoff', 'action', 'routine', 'entity', 'person', 'topic',
                                          'material', 'reminder'})
            self.assertEqual(kinds['reminder'], Kind('reminders', 'owner_reminders_list'))
            self.assertIsNotNone(kinds['signoff'].visible)
        # An id that names no record of the kind reads as Global, which is what an explicit null states.
        for kind in ('action', 'reminder', 'material', 'person', 'topic', 'entity'):
            item = self.completed('project_item_get', {'project_id': self.alpha, 'item': {'type': kind, 'id': self.in_alpha}})['item']
            self.assertEqual(item, {'type': kind, 'id': self.in_alpha, 'project_id': None})

    # -- Chat --------------------------------------------------------------------------------------------------------

    def test_a_chat_reference_finds_a_record_in_a_reachable_project_and_keeps_every_other_as_sent(self):
        ctx = replace(self.agent_ctx, global_capabilities={'read_global_context'})
        build = lambda reference: {'message': 'Look', 'references': [reference]}
        self.check_task('request_user_input', build, lambda a: a['references'][0], absent=True)
        for reference in ({'type': 'browser', 'id': 'main'}, {'type': 'delta', 'id': str(uuid4())},
                          {'type': 'tab', 'id': f'{uuid4()}:{"A" * 32}'}, {'type': 'signoff', 'id': str(uuid4())}):
            with self.subTest(reference=reference['type']):
                self.assertEqual(self.completed('request_user_input', build(reference), ctx)['references'], [reference])
        project = self.completed('request_user_input', build({'type': 'project', 'id': self.alpha}))['references'][0]
        self.assertEqual(project, {'type': 'project', 'id': self.alpha, 'project_id': self.alpha})
        self.assertEqual(self.completed('request_user_input', build({'type': 'project', 'id': self.gamma}))['references'][0],
                         {'type': 'project', 'id': self.gamma})
        many = {'message': 'Look', 'references': [self.task(self.in_alpha), self.task(self.in_global), self.task(self.in_beta)]}
        self.assertEqual([r.get('project_id', 'none') for r in self.completed('request_user_input', many)['references']],
                         [self.alpha, 'none', self.beta])

    # -- private conversations ---------------------------------------------------------------------------------------

    def test_a_private_conversation_source_is_completed_in_global_and_left_alone_with_a_selected_project(self):
        for operation, extra in (('owner_conversation_get', {}), ('owner_conversation_related', {}), ('owner_conversation_peers', {}),
                                 ('owner_conversation_send', {'text': 'Hello'}), ('owner_conversation_mark_read', {'sequence': 0})):
            build = lambda source, extra=extra: {'scope': 'global', 'source': source, **extra}
            self.check_task(operation, build, lambda a: a['source'], absent=True)
            selected = replace(self.agent_ctx, project_id=self.beta)
            for source in (self.task(self.in_alpha), self.task(self.in_beta), self.task(self.in_global)):
                with self.subTest(operation=operation, selected=source['id']):
                    sent = {'source': source, **extra}
                    self.assertEqual(self.completed(operation, sent, selected), sent)
        project = self.completed('owner_conversation_get', {'scope': 'global', 'source': {'type': 'project', 'id': self.alpha}})
        self.assertEqual(project['source'], {'type': 'project', 'id': self.alpha, 'project_id': self.alpha})
        reference = {'scope': 'global', 'source': {'type': 'reference', 'id': 'card'}}
        self.assertEqual(self.completed('owner_conversation_get', reference), reference)


if __name__ == '__main__':
    unittest.main()
