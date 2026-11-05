from dataclasses import replace

from qorqut_operations.contracts import new_id
from foundation_support import BUSINESS, HUMAN, context
from test_background_programs import runtime
from test_learning_sources import call


def candidate(runtime):
    runtime.policy.human_global_capabilities = {HUMAN: {'read_global_context'}}
    identity = new_id()
    with runtime.database.transaction() as uow:
        uow.repository('learning_candidates', business_id=None).insert({'id': identity,
            'candidate_json': {'text': 'The owner prefers a short weekly summary for Project A.',
                               'bucket': 'context', 'scope': 'global', 'tag': 'fact'}})
    return identity


def test_global_acceptance_files_once_without_business_or_mandatory_changes(runtime):
    identity = candidate(runtime)
    accepted = call(runtime, 'learnings_review', {'learning_id': identity, 'state': 'accepted',
        'feedback': 'Only Project A; not a general reporting rule.'}, global_scope=True, revision=1)['item']
    filing = accepted['filing']
    assert filing['upload_id'] and filing['job_id']
    runtime.model.answers.append({'topics': [{'key': 'preferences', 'title': 'Preferences', 'slug': 'preferences'}],
        'notes': [{'title': 'Project A reports', 'topic_key': 'preferences', 'paragraph_ids': [0],
                   'summary': 'Project A reporting preference.'}]})
    result = runtime.background.run_one(filing['job_id'])
    assert result.get('state') == 'succeeded', result
    with runtime.database.read() as uow:
        followups = uow.repository('jobs', business_id=None).list(filters={'state': 'queued'})
    assert {job['kind'] for job in followups} == {'context.summarize', 'context.map'}
    for job in followups:
        runtime.model.answers.append('Project A reporting preference.' if job['kind'] == 'context.summarize' else 'Project A\n- Short weekly summary')
        result = runtime.background.run_one(job['id'])
        assert result.get('state') == 'succeeded', result
    with runtime.database.read() as uow:
        docs = uow.repository('documents', business_id=None).list()
        note = next(row for row in docs if row['kind'] == 'context_note')
        view = runtime.domains.documents.store.view(note, uow)
        assert 'Only Project A' in view['body'] and view['metadata']['topic_id']
        assert (runtime.root / note['path']).is_file()
        assert not uow.repository('documents', business_id=BUSINESS).list()
        assert not uow.repository('tasks', business_id=None).list()
    shared = call(runtime, 'context_get', {'scope': 'shared', 'document_id': note['id']}, global_scope=True)['document']
    assert 'Only Project A' in shared['body']
    again = call(runtime, 'learnings_review', {'learning_id': identity, 'state': 'accepted'}, global_scope=True, revision=1)['item']
    assert again['filing'] == filing
    assert not call(runtime, 'learnings_list', {}, global_scope=True)['items']


def test_interrupted_global_acceptance_uses_existing_intake_recovery(runtime):
    identity = candidate(runtime)
    runtime.file_applier.after_replace = lambda resource: (_ for _ in ()).throw(OSError('Injected interruption'))
    ctx = replace(context(), business_id=None, review_business_id=None)
    result = runtime.application.invoke(ctx, 'learnings_review', {'learning_id': identity, 'state': 'accepted'},
        {'expected_revision': 1, 'idempotency_key': new_id()})
    assert result.status == 'error'
    status = call(runtime, 'learnings_intake_status', {'learning_id': identity}, global_scope=True)
    assert status['pending'] and not status['filing']
    runtime.file_applier.after_replace = None
    result = runtime.background.learnings.resume_intake(ctx, identity)
    assert result.status == 'ok', result.to_dict()


def test_rejected_global_context_does_not_file(runtime):
    identity = candidate(runtime)
    item = call(runtime, 'learnings_review', {'learning_id': identity, 'state': 'rejected'}, global_scope=True, revision=1)['item']
    assert not item['filing']
    with runtime.database.read() as uow:
        assert not uow.repository('jobs', business_id=None).list()


def test_failed_global_filing_can_be_retried_by_owner(runtime):
    identity = candidate(runtime)
    item = call(runtime, 'learnings_review', {'learning_id': identity, 'state': 'accepted'}, global_scope=True, revision=1)['item']
    runtime.model.answers.extend([{'topics': [], 'notes': []}] * 3)
    runtime.background.run_one(item['filing']['job_id'])
    with runtime.database.read() as uow:
        upload = uow.repository('documents', business_id=None).get(item['filing']['upload_id'])
        assert upload['metadata_json']['status'] == 'failed'
    call(runtime, 'context_retry', {'upload_id': upload['id']}, global_scope=True, revision=upload['revision'])
    with runtime.database.read() as uow:
        assert uow.repository('documents', business_id=None).get(upload['id'])['metadata_json']['status'] == 'pending'
