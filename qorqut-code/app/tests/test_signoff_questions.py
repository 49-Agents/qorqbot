"""Grouped questions use one owner decision, one transaction and one delivery."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from starlette.requests import Request
from qorqut_operations.contracts import DomainError, Principal, canonical_hash, new_id, plain
from qorqut_operations.signoffs.inputs import validate_form, validate_response
from qorqut_operations.signoffs.questions import validate_answers
from qorqut.transport.http import HTTPAPI
from qorqut.transport.mcp_presentation import present
import test_signoff_sources as fixtures


def form():
    choices = [{'id': 'yes', 'label': 'Yes', 'explanation': 'Proceed with this option.'},
               {'id': 'no', 'label': 'No', 'explanation': 'Choose another approach.'}]
    return {'schema_version': 2, 'kind': 'question', 'title': 'Plan the workshop',
            'explanation': 'Confirm the location and materials together.', 'questions': [
                {'id': 'location', 'prompt': 'Use the main room?', 'selection_mode': 'single', 'choices': deepcopy(choices)},
                {'id': 'materials', 'prompt': 'Which materials?', 'selection_mode': 'multiple', 'choices': deepcopy(choices), 'notes_required': True},
                {'id': 'details', 'prompt': 'Anything else?', 'selection_mode': 'none', 'required': False}]}


def answers():
    return [{'question_id': 'location', 'selected_choices': ['yes'], 'notes': ''},
            {'question_id': 'materials', 'selected_choices': ['yes', 'no'], 'notes': '  Bring both.\nKeep this spacing.  '},
            {'question_id': 'details', 'selected_choices': [], 'notes': ''}]


class QuestionValidationTests(unittest.TestCase):
    def test_defaults_order_and_repeated_choice_ids_are_question_local(self):
        normalized = validate_form(form())
        self.assertEqual(normalized['response_policy'], 'owner_only')
        self.assertTrue(normalized['questions'][0]['required'])
        self.assertFalse(normalized['questions'][0]['notes_required'])
        result = validate_answers(normalized, list(reversed(answers())))
        self.assertEqual([a['question_id'] for a in result], ['location', 'materials', 'details'])
        self.assertEqual(result[1]['selected_choices'], ['no', 'yes'])
        self.assertEqual(result[1]['notes'], answers()[1]['notes'])
        self.assertEqual(result[2]['status'], 'skipped')

    def test_written_alternative_and_optional_notes_rule(self):
        f, a = form(), answers()
        f['questions'][2]['notes_required'] = True
        a[0].update(selected_choices=[], notes='Use the garden instead.')
        self.assertEqual(validate_answers(validate_form(f), a)[0]['notes'], a[0]['notes'])
        a[1]['notes'] = ' '
        with self.assertRaises(DomainError) as error:
            validate_answers(validate_form(f), a)
        self.assertEqual(set(error.exception.details['field_errors']), {'materials'})

    def test_invalid_forms_fail_closed(self):
        variants = []
        for changes in ({'schema_version': 3}, {'response_policy': 'management_allowed'}, {'selection_mode': 'single'},
                        {'kind': 'draft_review'}, {'questions': []}, {'questions': form()['questions'] * 4}):
            variants.append({**form(), **changes})
        f = form(); f['questions'][1]['id'] = 'location'; variants.append(f)
        f = form(); f['questions'][0]['choices'][1]['id'] = 'yes'; variants.append(f)
        f = form(); f['questions'][0]['selection_mode'] = 'none'; variants.append(f)
        f = form(); f['questions'][0]['choices'] = []; variants.append(f)
        f = form(); f['questions'][0]['required'] = 'yes'; variants.append(f)
        f = form(); f['questions'][0]['prompt'] = ' '; variants.append(f)
        for variant in variants:
            with self.subTest(form=variant), self.assertRaises(DomainError):
                validate_form(variant)

    def test_invalid_answer_sets_fail_closed(self):
        variants = [None, [], answers()[:2], answers() + [answers()[0]]]
        for field, value in [('question_id', 'unknown'), ('selected_choices', ['unknown']), ('selected_choices', ['yes', 'no']),
                             ('selected_choices', ['yes', 'yes']), ('notes', None), ('notes', 'x' * 8001), ('extra', True)]:
            a = answers(); a[0][field] = value; variants.append(a)
        a = answers(); a[2] = a[0]; variants.append(a)
        a = answers(); a[0]['selected_choices'] = []; variants.append(a)
        a = answers(); a[2]['selected_choices'] = ['yes']; variants.append(a)
        for variant in variants:
            with self.subTest(answers=variant), self.assertRaises(DomainError):
                validate_answers(validate_form(form()), variant)

    def test_aggregate_and_unicode_byte_limits(self):
        f = form(); f['questions'] = [{'id': f'q{i}', 'prompt': 'Details?', 'selection_mode': 'none'} for i in range(5)]
        f = validate_form(f)
        a = [{'question_id': q['id'], 'selected_choices': [], 'notes': 'x' * 6400} for q in f['questions']]
        self.assertEqual(len(validate_answers(f, a)), 5)
        a[0]['notes'] += 'x'
        with self.assertRaises(DomainError): validate_answers(f, a)
        for answer in a: answer['notes'] = '😀' * 4000
        with self.assertRaises(DomainError): validate_answers(f, a)
        f = form(); f['explanation'] = '😀' * 8000
        for q in f['questions']: q['explanation'] = '😀' * 8000
        with self.assertRaises(DomainError): validate_form(f)

    def test_legacy_gateway_rejects_grouped_answers(self):
        with self.assertRaises(DomainError): validate_response(validate_form(form()), 'yes', ['yes'])
        from qorqut.overseer.inputs import create_input_request
        binding = {'id': 'test', 'request_key': 'test', 'owner_scope': {'kind': 'global', 'business_id': None}}
        with self.assertRaisesRegex(DomainError, 'single question'):
            create_input_request(None, None, binding, form(), 'test')


class QuestionSubmissionTests(unittest.TestCase):
    invoke = fixtures.SignoffSourceTests.invoke
    create = fixtures.SignoffSourceTests.create
    task = fixtures.SignoffSourceTests.task

    def setUp(self):
        fixtures.SignoffSourceTests.setUp(self)
        self.form = form()

    def respond(self, s, *, context=None, **changes):
        body = {'proposal_revision': s['proposal_revision'], 'payload_hash': s['payload_hash'],
                'expected_status_revision': s['status_revision'], 'idempotency_key': new_id(), 'answers': answers(), **changes}
        return self.signoffs.respond(context or self.ctx, s['id'], **body)

    def saved(self, s):
        return self.invoke('signoffs_get', {'signoff_id': s['id']})['signoff']

    def test_one_final_decision_releases_source_and_preserves_every_answer(self):
        s = self.create()['signoff']
        self.assertTrue(self.task()['input_needed'])
        invalid = self.respond(s, answers=answers()[:1])
        self.assertEqual(invalid.error['code'], 'validation_error')
        self.assertEqual(self.saved(s)['note_count'], 0)
        self.assertEqual(self.saved(s)['status_revision'], s['status_revision'])
        self.assertTrue(self.task()['input_needed'])
        result = self.respond(s)
        self.assertEqual(result.status, 'ok', result.error)
        final = plain(result.data)['signoff']
        self.assertEqual(final['state'], 'answered')
        self.assertEqual(final['status_revision'], s['status_revision'] + 1)
        self.assertEqual(final['note_count'], 3)
        self.assertFalse(self.task()['input_needed'])
        self.assertEqual(final['decision']['schema_version'], 2)
        self.assertEqual(final['decision']['answers'][1]['notes'], answers()[1]['notes'])
        notes = self.invoke('signoffs_notes_list', {'signoff_id': s['id']})['notes']
        self.assertEqual([n['id'] for n in notes], [a['message_id'] for a in final['decision']['answers']])
        self.assertIn('Which materials?', notes[1]['body'])
        self.assertIn('Selected: Yes, No', notes[1]['body'])
        self.assertIn('Not answered (optional)', notes[2]['body'])
        for detail in ('compact', 'text', 'full'):
            compact = present('signoffs_get', {'status': 'ok', 'data': {'signoff': final}}, detail)
            self.assertEqual(compact['data']['signoff']['decision'], final['decision'])
            self.assertEqual(compact['data']['signoff']['input'], final['input'])

    def test_receipt_replay_reordering_and_conflicting_second_submission(self):
        s, key = self.create()['signoff'], new_id()
        result = self.respond(s, idempotency_key=key)
        replay = self.respond(s, idempotency_key=key, answers=list(reversed(answers())))
        self.assertEqual(replay.status, 'ok', replay.error)
        self.assertEqual(plain(result.data)['signoff']['decision'], plain(replay.data)['signoff']['decision'])
        changed = answers(); changed[0]['notes'] = 'Different answer'
        self.assertEqual(self.respond(s, idempotency_key=key, answers=changed).status, 'error')
        self.assertEqual(self.respond(s, answers=changed).status, 'error')
        self.assertEqual(self.saved(s)['note_count'], 3)

    def test_all_bindings_and_legacy_fields_are_checked_before_writing(self):
        s = self.create()['signoff']
        for changes in ({'proposal_revision': 2}, {'payload_hash': 'wrong'}, {'expected_status_revision': 99},
                        {'notes': ''}, {'selected_choices': []}, {'draft_values': None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.respond(s, **changes).status, 'error')
                self.assertEqual(self.saved(s)['note_count'], 0)
                self.assertEqual(self.saved(s)['state'], 'pending')

    def test_failed_note_rolls_back_entire_submission_and_retry_succeeds(self):
        s, key = self.create()['signoff'], new_id()
        original, calls = self.signoffs.notes.decision_message, []
        def failing(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2: raise DomainError('validation_error', 'Synthetic write failure')
            return original(*args, **kwargs)
        with patch.object(self.signoffs.notes, 'decision_message', side_effect=failing):
            self.assertEqual(self.respond(s, idempotency_key=key).status, 'error')
        self.assertEqual(self.saved(s)['note_count'], 0)
        self.assertEqual(self.saved(s)['state'], 'pending')
        self.assertTrue(self.task()['input_needed'])
        self.assertEqual(self.respond(s, idempotency_key=key).status, 'ok')

    def test_concurrent_done_clicks_record_only_one_response(self):
        s = self.create()['signoff']
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.respond, s) for _ in range(2)]
            results = [future.result() for future in futures]
        self.assertEqual(sorted(result.status for result in results), ['error', 'ok'])
        self.assertEqual(self.saved(s)['note_count'], 3)
        self.assertEqual(self.saved(s)['status_revision'], s['status_revision'] + 1)

    def test_cancelled_forms_and_saved_decisions_cannot_be_answered_again(self):
        s = self.create()['signoff']
        self.invoke('signoffs_input_cancel', {'signoff_id': s['id']})
        self.assertEqual(self.respond(s).status, 'error')
        self.assertEqual(self.saved(s)['note_count'], 0)
        s = self.create(key='final')['signoff']
        self.assertEqual(self.respond(s).status, 'ok')
        with self.assertRaises(DomainError), self.db.transaction() as uow:
            uow.connection.execute("UPDATE signoffs SET decision_json='{}' WHERE id=?", (s['id'],))
        self.assertEqual(self.saved(s)['decision']['answers'][1]['notes'], answers()[1]['notes'])

    def test_owner_and_review_scope_are_required(self):
        s = self.create()['signoff']
        stranger = new_id()
        with self.db.transaction() as uow:
            uow.repository('principals').insert({'id': stranger, 'kind': 'human', 'display_name': 'Another human'})
        contexts = [replace(self.ctx, principal=Principal(stranger, 'human')),
                    replace(self.ctx, principal=Principal(self.owner, 'agent')),
                    replace(self.ctx, business_id=self.business, review_business_id=self.business)]
        for context in contexts:
            with self.subTest(context=context): self.assertEqual(self.respond(s, context=context).status, 'error')
        self.assertEqual(self.saved(s)['state'], 'pending')
        project = self.invoke('signoffs_input_create', {'request_key': 'project', 'form': self.form}, business=self.business)['signoff']
        outside = replace(self.ctx, business_id=new_id(), review_business_id=None)
        self.assertEqual(self.respond(project, context=outside).status, 'error')
        self.assertEqual(self.respond(project, context=replace(self.ctx, business_id=self.business, review_business_id=self.business)).status, 'ok')
        # The installation owner's Global queue deliberately includes authorized project Signoffs.
        another = self.invoke('signoffs_input_create', {'request_key': 'project-2', 'form': self.form}, business=self.business)['signoff']
        self.assertEqual(self.respond(another).status, 'ok')

    def test_worker_receives_one_response_event_and_no_intermediate_notes(self):
        worker = new_id()
        with self.db.transaction() as uow:
            uow.repository('principals').insert({'id': worker, 'kind': 'agent', 'display_name': 'Example worker'})
            uow.repository('agent_profiles', business_id=None).insert({'agent_id': worker, 'principal_id': worker,
                'namespace': 'ecosystem', 'name': 'example-worker', 'workspace': 'example-worker',
                'profile_json': {'status': 'active', 'role': {'id': 'global-worker'},
                                 'scope': {'kind': 'ecosystem', 'global_capabilities': ['read_global_context']}}})
        ctx = replace(self.ctx, principal=Principal(worker, 'agent'), source='mcp', profile_revision=1)
        self.policy.context_for = lambda principal_id, business_id=None, review_business_id=None, **_: replace(
            ctx if principal_id == worker else self.ctx, business_id=business_id, review_business_id=review_business_id)
        result = self.app.invoke(ctx, 'signoffs_input_create', {'scope': 'global', 'request_key': 'worker', 'form': self.form}, {'idempotency_key': new_id()})
        self.assertEqual(result.status, 'ok', result.error)
        s = plain(result.data)['signoff']
        self.assertEqual(self.respond(s, answers=[]).status, 'error')
        with self.db.read() as uow:
            self.assertEqual(uow.connection.execute('SELECT count(*) FROM inbox_events WHERE source_id=?', (s['id'],)).fetchone()[0], 0)
        self.assertEqual(self.respond(s).status, 'ok')
        with self.db.read() as uow:
            events = uow.connection.execute('SELECT e.event_type FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id WHERE e.source_id=? AND d.agent_id=?', (s['id'], worker)).fetchall()
            self.assertEqual([e[0] for e in events], ['response_submitted'])

    def test_owner_http_route_accepts_the_complete_payload(self):
        s = self.create()['signoff']
        body = json.dumps({'proposal_revision': s['proposal_revision'], 'payload_hash': s['payload_hash'],
            'expected_status_revision': s['status_revision'], 'idempotency_key': new_id(), 'answers': answers()}).encode()
        async def receive(): return {'type': 'http.request', 'body': body, 'more_body': False}
        request = Request({'type': 'http', 'method': 'POST', 'headers': [(b'content-type', b'application/json')]}, receive)
        response = asyncio.run(HTTPAPI.signoff_route(NS(runtime=NS(signoffs=self.signoffs)), request, self.ctx, ['signoffs', s['id'], 'respond']))
        self.assertEqual(response.status_code, 200, response.body)
        self.assertEqual(json.loads(response.body)['data']['signoff']['state'], 'answered')

    def test_legacy_hash_receipt_and_draft_versions_remain_compatible(self):
        self.form = {'kind': 'question', 'title': 'One question', 'explanation': 'Answer it.', 'selection_mode': 'none', 'notes_required': True}
        s, key = self.create(source=False)['signoff'], new_id()
        body = {'proposal_revision': s['proposal_revision'], 'payload_hash': s['payload_hash'],
                'expected_status_revision': s['status_revision'], 'idempotency_key': key, 'notes': 'Legacy answer'}
        self.assertEqual(self.signoffs.respond(self.ctx, s['id'], **body).status, 'ok')
        self.assertEqual(self.signoffs.respond(self.ctx, s['id'], **body).status, 'ok')
        expected = canonical_hash({'id': s['id'], 'proposal_revision': s['proposal_revision'], 'payload_hash': s['payload_hash'],
                                   'revision': s['status_revision'], 'notes': 'Legacy answer', 'selected': []})
        with self.db.read() as uow:
            from qorqut_operations.storage.receipts import IdempotencyReceipts
            self.assertTrue(IdempotencyReceipts(uow).lookup(self.owner, 'global', 'signoffs_respond', key, expected))
        self.assertEqual(self.saved(s)['note_count'], 1)
        self.form = {**self.form, 'kind': 'draft_review', 'draft_fields': [{'id': 'body', 'label': 'Body', 'value': 'Initial', 'required': True}]}
        d = self.create(key='draft', source=False)['signoff']
        binding = {'proposal_revision': d['proposal_revision'], 'payload_hash': d['payload_hash'], 'expected_status_revision': d['status_revision'], 'idempotency_key': new_id()}
        saved = self.signoffs.save_draft(self.ctx, d['id'], values={'body': 'Edited'}, **binding)
        self.assertEqual(saved.status, 'ok', saved.error)
        updated = plain(saved.data)['signoff']
        binding.update(expected_status_revision=updated['status_revision'], idempotency_key=new_id())
        result = self.signoffs.respond(self.ctx, d['id'], notes='Final feedback', draft_values={'body': 'Returned'}, **binding)
        self.assertEqual(result.status, 'ok', result.error)
        self.assertEqual(plain(result.data)['signoff']['decision']['draft_values'], {'body': 'Returned'})


if __name__ == '__main__': unittest.main()
