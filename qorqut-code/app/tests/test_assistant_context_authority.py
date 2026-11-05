"""Owner assistants can maintain context, not permissions or owner identity."""
import pytest

from test_person_details import call, person
from test_service_transports import ServiceFixture


def core_document(service):
    with service.fixture.db.read() as uow:
        return next(row for row in uow.repository("documents", business_id=service.fixture.business_id).list()
                    if row["metadata_json"].get("bootstrap_key") == "product")


@pytest.mark.parametrize('role', ['ordubashy', 'founder'])
def test_context_creation_and_updates_apply_with_current_scope_and_overrides(tmp_path, role):
    s = ServiceFixture(tmp_path, full=True)
    actor = s.fixture.create('context-assistant', role, scope={
        'kind': 'ecosystem', 'allowed_business_ids': [s.fixture.business_id],
        'global_capabilities': ['read_global_context']})
    row = core_document(s)
    args = {'document_id': row['id'], 'body': '# Product\n\n## Agent Brief\nVerified product direction.\n',
            'expected_content_hash': row['content_hash']}
    result = call(s, 'context_document_update', args, actor=actor, revision=row['revision'])
    assert result.status == 'ok', result.to_dict()
    assert call(s, 'context_document_update', args, actor=actor, revision=row['revision']).status == 'error'
    topic = call(s, 'context_topics_create', {'title': 'Setup evidence', 'slug': 'setup-evidence'}, actor=actor)
    assert topic.status == 'ok', topic.to_dict()
    topic_id = topic.data['document']['id']
    note = call(s, 'context_notes_create', {'topic_id': topic_id, 'title': 'Decision', 'body': 'Observed evidence'}, actor=actor)
    assert note.status == 'ok', note.to_dict()
    created = call(s, 'context_people_create', {'name': 'Teammate', 'affiliation': 'internal',
        'contact': {'email': 'person@example.org'}}, actor=actor)
    assert created.status == 'ok', created.to_dict()
    identity = created.data['document']['id']
    edited = call(s, 'context_people_update', {'document_id': identity,
        'changes': {'affiliation': 'external', 'summary': 'External collaborator'}}, actor=actor, revision=1)
    assert edited.status == 'ok', edited.to_dict()
    assert person(s, identity)['affiliation'] == 'external'
    # Human contact classification is context, not membership/owner authority.
    with s.fixture.db.read() as u:
        member = u.repository('business_memberships', business_id=s.fixture.business_id).get(s.fixture.owner.id)
    result = call(s, 'context_people_affiliation_set', {'document_id': s.fixture.owner.id,
                  'affiliation': 'external'}, actor=actor, revision=member['revision'])
    assert result.status == 'ok', result.to_dict()
    with s.fixture.db.read() as u:
        after = u.repository('business_memberships', business_id=s.fixture.business_id).get(s.fixture.owner.id)
        assert (after['active'], after['is_owner']) == (member['active'], member['is_owner'])
        assert not u.connection.execute('SELECT 1 FROM signoffs').fetchone()

    for mode, status in [('deny', 'error'), ('signoff', 'signoff_required')]:
        s.fixture.update(actor, {'tool_policy': {'overrides': {'context_people_affiliation_set': mode,
                                                             'context_document_update': mode}}})
        row = core_document(s)
        result = call(s, 'context_document_update', {'document_id': row['id'], 'body': 'Proposed edit',
            'expected_content_hash': row['content_hash']}, actor=actor, revision=row['revision'])
        assert result.status == status, result.to_dict()
        result = call(s, 'context_people_affiliation_set', {'document_id': identity,
                      'affiliation': 'internal'}, actor=actor, revision=2)
        assert result.status == status, result.to_dict()
        assert person(s, identity)['affiliation'] == 'external'
    if role == 'founder':
        s.fixture.update(actor, {'scope': {'kind': 'ecosystem', 'allowed_business_ids': [],
                                         'global_capabilities': ['read_global_context']}})
        result = call(s, 'context_topics_create', {'title': 'After handoff', 'slug': 'not-authorized'}, actor=actor)
        assert result.status == 'error'
