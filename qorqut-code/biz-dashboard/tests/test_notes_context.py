"""Notes Context snapshots through the real application, isolated files and jobs."""
import xml.etree.ElementTree as ET
import time
from uuid import uuid4

from bazarlyk.domains.documents.note_snapshot import snapshot
from test_service_transports import ORIGIN


def post(client, path, body):
    payload = {"idempotency_key": str(uuid4()), **body}
    for attempt in range(30):
        response = client.post('/api/biz/widget/' + path, json=payload, headers=ORIGIN)
        if response.status_code != 503 or response.json().get('error', {}).get('code') != 'busy':
            break
        time.sleep(0.1)
    assert response.status_code < 300, response.text
    return response.json()


def version(note):
    return {"expected_revision": note["revision"], "expected_content_hash": note["content_hash"]}


def test_structured_multiline_snapshot_and_explicit_resubmission(connected):
    service, client = connected
    body = 'Some <free> & text.\n- Send proposal\u2028Include prices\nSeparate free text.\n- Finished\n'
    note = post(client, 'notes', {"title": 'Plan <A>', "content": body, "date": '2026-09-01'})
    note = post(client, 'notes/' + note['id'], {**version(note), "statuses": {"Send proposal\nInclude prices": "in_progress", "Finished": "done"}})
    original_id = note['id']
    assert note['date'] == '2026-09-01'
    post(client, 'notes/' + original_id + '/upload', version(note))
    fresh = next(n for n in client.get('/api/biz/widget/notes').json() if n['id'] == original_id)
    assert fresh['content'] == body
    assert fresh['filing']['state'] == 'queued'
    assert not fresh['changedSinceUpload']
    with service.runtime.database.read() as uow:
        source = uow.repository('documents', business_id=service.fixture.business_id).get(fresh['uploadId'])
        doc = service.runtime.domains.documents.store.view(source, uow)
        assert doc['metadata']['format'] == 'notes-v1'
        root = ET.fromstring(doc['body'])
        assert root.attrib['title'] == 'Plan <A>'
        assert [c.tag for c in root] == ['text', 'item', 'text', 'item', 'text']
        assert root[0].text == 'Some <free> & text.'
        assert root[1].text == 'Send proposal\nInclude prices'
        assert root[1].attrib['status'] == 'in_progress'
        assert root[2].text == 'Separate free text.'
        assert root[3].attrib['status'] == 'done'
    duplicate = client.post('/api/biz/widget/notes/' + original_id + '/upload', json=version(fresh), headers=ORIGIN)
    assert duplicate.status_code == 422, duplicate.text
    pinned = post(client, 'notes/' + original_id, {**version(fresh), 'pinned': True})
    assert not pinned['changedSinceUpload']
    changed = post(client, 'notes/' + original_id, {**version(pinned), 'content': body + 'New information.'})
    assert changed['changedSinceUpload']
    stale = client.post('/api/biz/widget/notes/' + original_id + '/upload', json=version(pinned), headers=ORIGIN)
    assert stale.status_code == 409
    post(client, 'notes/' + original_id + '/upload', version(changed))
    newest = next(n for n in client.get('/api/biz/widget/notes').json() if n['id'] == original_id)
    assert newest['uploadId'] != fresh['uploadId']
    assert not newest['changedSinceUpload']


def test_empty_upload_rejected_and_journal_routes_removed(connected):
    _, client = connected
    note = post(client, 'notes', {})
    response = client.post('/api/biz/widget/notes/' + note['id'] + '/upload', json=version(note), headers=ORIGIN)
    assert response.status_code == 422
    assert client.get('/api/biz/widget/journal').status_code == 404
    assert client.post('/api/biz/widget/journal', json={}, headers=ORIGIN).status_code == 404


def test_snapshot_escapes_fake_closing_tags_and_preserves_empty_blocks():
    root = ET.fromstring(snapshot('- </item><text>fake\u2028\u2028continued\n\nreal', {}))
    assert len(root) == 3
    assert root[0].text == '</item><text>fake\n\ncontinued'
    assert root[1].tag == 'text' and root[1].text is None
    assert root[2].text == 'real'


def test_existing_journal_record_is_retained_in_notes(connected):
    service, client = connected
    body = 'Original journal text.\n- A literal old bullet.\n'
    note = post(client, 'notes', {"title": 'Old entry', "content": body, "date": '2026-08-01'})
    # Simulate a record registered by the previous release, without a live migration.
    with service.runtime.database.transaction() as uow:
        uow.repository('documents', business_id=service.fixture.business_id).update(note['id'],
            {'kind': 'journal'}, expected_revision=note['revision'])
    old = next(n for n in client.get('/api/biz/widget/notes').json() if n['id'] == note['id'])
    assert old['content'] == body and old['date'] == '2026-08-01'
    saved = post(client, 'notes/' + note['id'], {**version(old), 'pinned': True})
    assert saved['kind'] == 'business_note'
    assert saved['id'] == note['id'] and saved['path'] == note['path']
    assert saved['content'] == body
    assert not any(op.name.startswith('journal_') for op in service.runtime.registry.installed())


def test_explicit_journal_migration_keeps_source_bytes_and_literal_free_text(connected):
    from bazarlyk.migration.journal_notes import migrate
    service, client = connected
    text = '- literal dash\n\n⸻\n\u2063b\u2063 literal token <tag>\n'
    note = post(client, 'notes', {'title': 'Literal journal', 'content': text, 'date': '2026-08-03'})
    with service.runtime.database.transaction() as uow:
        repo = uow.repository('documents', business_id=service.fixture.business_id)
        repo.update(note['id'], {'kind': 'journal'}, expected_revision=note['revision'])
        before = repo.get(note['id'])
        original = service.runtime.domains.documents.store.read_bytes(before, uow)
    preview = migrate(service.runtime)
    assert preview['entries'] == 1
    result = migrate(service.runtime, apply=True)
    assert result['verified'] == 1
    assert migrate(service.runtime, apply=True)['entries'] == 0
    migrated = next(n for n in client.get('/api/biz/widget/notes').json() if n['id'] == note['id'])
    assert 'plainText' not in migrated
    assert migrated['content'] == text and migrated['title'] == note['title']
    assert migrated['date'] == '2026-08-03'
    with service.runtime.database.read() as uow:
        after = uow.repository('documents', business_id=service.fixture.business_id).get(note['id'])
        assert service.runtime.domains.documents.store.read_bytes(after, uow) == original
    post(client, 'notes/' + note['id'] + '/upload', version(migrated))
    migrated = next(n for n in client.get('/api/biz/widget/notes').json() if n['id'] == note['id'])
    with service.runtime.database.read() as uow:
        uploaded = uow.repository('documents', business_id=service.fixture.business_id).get(migrated['uploadId'])
        root = ET.fromstring(service.runtime.domains.documents.store.view(uploaded, uow)['body'])
        assert root[0].tag == 'item' and root[0].text == 'literal dash'
        assert root[2].tag == 'divider'
    edited = post(client, 'notes/' + note['id'], {**version(migrated), 'content': text + '- another literal line'})
    assert edited['content'] == text + '- another literal line'


def test_notes_and_context_show_actual_job_failure_and_retry(connected):
    import json
    service, client = connected
    service.runtime.background.stop()
    note = post(client, 'notes', {"title": "Filing status", "content": "Some notes."})
    post(client, 'notes/' + note['id'] + '/upload', version(note))
    def get(path):
        for attempt in range(30):
            result = client.get(path)
            if result.status_code != 503 or result.json().get("error", {}).get("code") != "busy":
                return result
            time.sleep(0.1)
        return result
    def fresh():
        return next(n for n in get('/api/biz/widget/notes').json() if n['id'] == note['id'])
    uploaded = fresh()
    job_id = uploaded['filing']['job_id']
    for job_state, expected in [('running', 'running'), ('applying', 'running'), ('waiting_signoff', 'waiting_signoff'), ('failed', 'failed')]:
        with service.runtime.database.transaction() as uow:
            uow.connection.execute("UPDATE jobs SET state=?,error_json=? WHERE id=?", (job_state,
                json.dumps({'code': 'validation_error', 'message': 'Document has a different registered kind'}) if job_state == 'failed' else None, job_id))
        current = fresh()
        assert current['filing']['state'] == expected
        response = get('/api/biz/widget/context')
        assert response.status_code == 200, response.text
        inbox = next(i for i in response.json()['inbox'] if i['document_id'] == uploaded['uploadId'])
        assert inbox['status'] == expected
        assert inbox['failed'] == (expected == 'failed')
    assert current['filing']['retryable'] and inbox['retryable']
    assert inbox['error'] == current['filing']['error'] == 'Document has a different registered kind'
    post(client, 'context/retry', {'id': uploaded['uploadId'], 'expected_revision': current['filing']['revision']})
    assert fresh()['filing']['state'] == 'queued'
    assert fresh()['filing']['error'] is None
    inbox = next(i for i in get('/api/biz/widget/context').json()['inbox'] if i['document_id'] == uploaded['uploadId'])
    assert inbox['status'] == 'queued' and not inbox['failed'] and not inbox['retryable']
