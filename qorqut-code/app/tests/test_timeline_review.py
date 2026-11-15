"""The trial review of timeline filings: held for the owner, applied by itself on approval, stale ones conflict, and the
coordinator's own permission override ends the trial."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import PolicyDecision, plain
from qorqut_operations.signoffs import ABSENT, SignoffPorts
from qorqut_operations.signoffs.service import SignoffService
from qorqut_operations.storage.locks import LockPaths

from qorqut.composition.policy import resolve_operation_policy
from qorqut.domains.timeline import REVIEW_NOTICE, Timeline
from qorqut.signoffs.ports import HostExecution, HostInputSources, HostPeers, HostReferences, HostTasks

from test_single_source import Fixture

DAY = '2026-11-13T10:00:00.000000Z'


class TimelineReviewTests(Fixture):
    def setUp(self):
        super().setUp()
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # The review service over the same store; a decision is the declared standing mode unless a test overrides it.
        self.modes = {}
        self.app.policy.resolve = self.decide
        ports = SignoffPorts(execution=HostExecution(), peers=HostPeers(), tasks=HostTasks(), references=HostReferences(),
                             platform=ABSENT, email=ABSENT, management=ABSENT, focus=ABSENT, sources=HostInputSources())
        self.signoffs = SignoffService(self.db, ports, locks=LockPaths.under(Path(folder.name) / 'locks'))
        self.app.signoffs = self.signoffs
        self.signoffs.bind(self.app)
        self.tool = Timeline(NS(application=self.app, registry=self.registry)).register()
        self.work = self.task(self.lead, 'Pay creators')
        self.item = {'type': 'task', 'id': self.work['id'], 'business_id': None}
        patcher = patch('qorqut.domains.timeline.timestamp', lambda: DAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def decide(self, ctx, descriptor, targets, uow):
        mode = self.modes.get(descriptor.name) or descriptor.default_mode or 'allow'
        return PolicyDecision(mode, 'synthetic', {'global_capability': descriptor.global_capability} if descriptor.global_capability else {})

    def ctx(self, who):
        found = super().ctx(who)
        return replace(found, source='dashboard') if who == self.owner else found

    # -- calls -----------------------------------------------------------------------------------------------------

    def file(self, changes, **more):
        return self.app.invoke(self.ctx(self.lead), 'timeline_file', {'scope': 'global', 'item': self.item, 'changes': changes, **more},
                               {'idempotency_key': str(uuid4())})

    def held(self, changes, **more):
        result = self.file(changes, **more)
        self.assertEqual(result.status, 'signoff_required', result.to_dict())
        return plain(result.signoff)

    def decision(self, signoff, kind, feedback=None):
        with self.db.read() as uow:
            row = self.signoffs._row(self.ctx(self.owner), signoff['id'], uow)
        return self.signoffs.review(self.ctx(self.owner), signoff['id'], decision=kind, proposal_revision=row['proposal_revision'],
                                    payload_hash=row['payload_hash'], expected_status_revision=row['status_revision'],
                                    idempotency_key=str(uuid4()), feedback=feedback)

    def approve(self, signoff):
        result = self.decision(signoff, 'approve')
        if result.status == 'accepted':
            self.signoffs.run_one(signoff['id'])
        return result

    def state_of(self, signoff):
        return self.rows('SELECT state FROM signoffs WHERE id=?', signoff['id'])[0][0]

    def count(self, table='timeline_entries'):
        return self.rows(f'SELECT COUNT(*) FROM {table}')[0][0]

    def add(self, kind, text):
        signoff = self.held([{'action': 'add', 'kind': kind, 'text': text}])
        self.assertEqual(self.approve(signoff).status, 'accepted')
        return self.rows('SELECT id FROM timeline_entries WHERE text=?', text)[0][0]

    def get(self, who, **more):
        return self.app.invoke(self.ctx(who), 'timeline_get', {'scope': 'global', 'item': self.item, **more}, {})

    # -- hold, approve, reject -------------------------------------------------------------------------------------

    def test_a_filing_is_held_and_the_coordinator_is_told_it_went_to_review(self):
        result = self.file([{'action': 'add', 'kind': 'decision', 'text': 'One provider only.'}],
                           sources=[{'quote': 'Pay one provider only.', 'ref': 'thread:1'}])
        self.assertEqual((result.status, result.to_dict()['warnings']), ('signoff_required', [REVIEW_NOTICE]))
        self.assertIn("Submitted for the owner's review", REVIEW_NOTICE)
        self.assertIn('Do not file it again', self.registry.get('timeline_file').description)
        self.assertEqual((self.count(), self.count('timeline_questions')), (0, 0))
        self.assertEqual(self.state_of(plain(result.signoff)), 'pending')

    def test_an_approval_files_it_by_itself_as_the_coordinator(self):
        signoff = self.held([{'action': 'add', 'kind': 'goal', 'text': 'Pay every creator.'},
                             {'action': 'ask', 'text': 'Dollars or local currency?', 'blocking': True}],
                            sources=[{'quote': 'Pay everyone.', 'ref': 'thread:1'}])
        self.assertEqual(self.approve(signoff).status, 'accepted')
        self.assertEqual(self.state_of(signoff), 'applied')
        self.assertEqual(self.rows('SELECT text,created_by,created_at,sources_json FROM timeline_entries'),
                         [('Pay every creator.', self.lead, DAY, '[{"quote": "Pay everyone.", "ref": "thread:1"}]')])
        self.assertEqual(self.rows('SELECT text,created_by FROM timeline_questions'), [('Dollars or local currency?', self.lead)])

    def test_a_rejection_writes_nothing_and_the_filing_ends_rejected(self):
        signoff = self.held([{'action': 'add', 'kind': 'fact', 'text': 'Not true.'}])
        result = self.decision(signoff, 'reject', 'Not true.')
        self.assertEqual(result.status, 'ok', result.to_dict())
        self.assertEqual((self.state_of(signoff), self.count()), ('rejected', 0))
        with self.db.read() as uow:
            self.assertEqual(self.signoffs._row(self.ctx(self.owner), signoff['id'], uow)['proposer_principal_id'], self.lead)

    # -- stale filings ---------------------------------------------------------------------------------------------

    def test_two_filings_removing_one_entry_the_second_conflicts(self):
        entry = self.add('decision', 'Old rule.')
        first = self.held([{'action': 'remove', 'entry_id': entry}])
        second = self.held([{'action': 'remove', 'entry_id': entry, 'reason': 'Again.'}])
        self.assertEqual(self.approve(first).status, 'accepted')
        late = self.decision(second, 'approve')
        self.assertEqual((late.status, self.state_of(second)), ('error', 'conflicted'))
        self.assertEqual(self.rows("SELECT action FROM timeline_entries WHERE removes_id=?", entry), [('remove',)])

    def test_two_filings_answering_one_question_the_second_conflicts(self):
        self.assertEqual(self.approve(self.held([{'action': 'ask', 'text': 'Which currency?', 'blocking': False}])).status, 'accepted')
        question = self.rows('SELECT id FROM timeline_questions')[0][0]
        answer = {'action': 'answer', 'question_id': question, 'kind': 'decision', 'text': 'Dollars.'}
        first, second = self.held([answer]), self.held([{'action': 'drop', 'question_id': question}])
        self.assertEqual(self.approve(first).status, 'accepted')
        self.assertEqual((self.decision(second, 'approve').status, self.state_of(second)), ('error', 'conflicted'))
        self.assertEqual(self.rows('SELECT state FROM timeline_questions'), [('answered',)])

    def test_add_only_filings_apply_in_any_order(self):
        first = self.held([{'action': 'add', 'kind': 'fact', 'text': 'One.'}])
        second = self.held([{'action': 'add', 'kind': 'fact', 'text': 'Two.'}, {'action': 'add', 'kind': 'goal', 'text': 'Goal.'}])
        self.assertEqual((self.approve(second).status, self.approve(first).status), ('accepted', 'accepted'))
        self.assertEqual([self.state_of(first), self.state_of(second)], ['applied', 'applied'])
        self.assertEqual(self.count(), 3)

    # -- ending the trial ------------------------------------------------------------------------------------------

    def test_a_permission_override_for_the_coordinator_files_directly(self):
        registry = self.registry
        declared = resolve_operation_policy('timeline_file', {}, {}, registry, role_id='ordubashy', scope='global')
        self.assertEqual(declared.mode, 'signoff')
        override = resolve_operation_policy('timeline_file', {}, {'timeline_file': 'allow'}, registry, role_id='ordubashy', scope='global')
        self.assertEqual(override.mode, 'allow')
        self.modes['timeline_file'] = 'allow'
        result = self.file([{'action': 'add', 'kind': 'fact', 'text': 'Direct.'}])
        self.assertEqual(result.status, 'ok', result.to_dict())
        self.assertEqual(self.count(), 1)

    # -- what timeline_get shows -----------------------------------------------------------------------------------

    def test_the_coordinator_and_the_owner_see_filings_waiting_and_other_agents_do_not(self):
        self.add('fact', 'Held earlier.')
        waiting = self.held([{'action': 'add', 'kind': 'decision', 'text': 'Pay in dollars.'},
                             {'action': 'ask', 'text': 'Which month?', 'blocking': True}])
        result = self.get(self.lead)
        self.assertEqual(result.status, 'ok', result.to_dict())
        data = plain(result.data)
        self.assertEqual([row['id'] for row in data['waiting_for_review']], [waiting['id']])
        self.assertEqual([line['action'] for line in data['waiting_for_review'][0]['changes']], ['add', 'ask'])
        self.assertIn('do not file them again', data['text'])
        self.assertIn('Add decision: Pay in dollars.', data['text'])
        self.assertIn('Ask (blocking): Which month?', data['text'])
        # The owner's own read (a dashboard reader, not an agent tool) sees them too.
        with self.db.read() as uow:
            self.assertEqual([row['id'] for row in self.tool.waiting(self.ctx(self.owner), self.item, uow)], [waiting['id']])
            self.assertEqual(self.tool.waiting(self.ctx(self.worker), self.item, uow), [])
        other = self.get(self.worker)
        self.assertNotIn('waiting_for_review', plain(other.data) if other.status == 'ok' else {})
        if other.status == 'ok':
            self.assertNotIn('Pay in dollars.', plain(other.data)['text'])
        self.approve(waiting)
        self.assertNotIn('waiting_for_review', plain(self.get(self.lead).data))

    def test_a_pending_filing_on_another_item_is_not_listed(self):
        other = self.task(self.lead, 'Other work')
        self.app.invoke(self.ctx(self.lead), 'timeline_file', {'scope': 'global', 'changes': [{'action': 'add', 'kind': 'fact', 'text': 'x'}],
                        'item': {'type': 'task', 'id': other['id'], 'business_id': None}}, {'idempotency_key': str(uuid4())})
        self.assertNotIn('waiting_for_review', plain(self.get(self.lead).data))

    def test_the_card_carries_the_texts_a_removal_and_an_answer_name(self):
        entry = self.add('fact', 'Hold cleared.')
        self.assertEqual(self.approve(self.held([{'action': 'ask', 'text': 'Which currency?', 'blocking': False}])).status, 'accepted')
        question = self.rows('SELECT id FROM timeline_questions')[0][0]
        signoff = self.held([{'action': 'remove', 'entry_id': entry}, {'action': 'answer', 'question_id': question, 'kind': 'fact', 'text': 'Dollars.'}])
        with self.db.read() as uow:
            preview = self.signoffs._row(self.ctx(self.owner), signoff['id'], uow)['preview_json']
        fields = {field['path']: field['after'] for field in preview['changes'][0]['fields']}
        self.assertEqual((fields['item_title'], fields['item_type']), ('Pay creators', 'task'))
        self.assertEqual(preview['title'], 'File on the timeline of task "Pay creators"')
        self.assertEqual([(line['action'], line.get('text'), line.get('question')) for line in fields['changes']],
                         [('remove', 'Hold cleared.', None), ('answer', 'Dollars.', 'Which currency?')])


if __name__ == '__main__':
    unittest.main()
