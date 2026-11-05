import copy

import pytest

from qorqut_agentic import AgenticError
from qorqut_agentic.overseer.views import Views
from .test_overseer_decisions import Gateway, binding, revision, setup, uid
from .test_overseer_state import proposal


def test_bounded_queries_filter_and_bind_cursors(layer):
    first, d1 = setup(layer)
    second = copy.deepcopy(first)
    second['id'] = uid()
    layer.overseer.proposals.record(second, 'p2')
    d2 = revision(second)
    d2['created_at'] = '2026-10-27T10:11:00Z'
    layer.overseer.deltas.create(d2, 'd2')
    views = Views(layer)
    page = views.list_deltas(limit=1)
    assert page['items'][0]['id'] == d2['delta_id']
    assert views.list_deltas(limit=1, cursor=page['next_cursor'])['items'][0]['id'] == d1['delta_id']
    with pytest.raises(AgenticError):
        views.list_deltas(decision='undecided', limit=1, cursor=page['next_cursor'])
    with pytest.raises(AgenticError):
        views.list_batches(cursor=page['next_cursor'])
    with pytest.raises(AgenticError):
        views.list_deltas(limit=101)

    b = binding(d1)
    layer.overseer.inputs.bind(b, 'b')
    gateway = Gateway(b)
    layer.overseer.apply_owner_response(b['id'], gateway.call(), operation_id='answer', gateway=gateway)
    assert [x['id'] for x in views.list_deltas(decision='accepted')['items']] == [d1['delta_id']]
    assert [x['id'] for x in views.list_inputs(delta_id=d1['delta_id'])['items']] == [b['id']]
    assert views.list_inputs(delta_id=d2['delta_id'])['items'] == []
    assert views.list_applications(queue='unimplemented')['items'][0]['delta_id'] == d1['delta_id']

    next_revision = copy.deepcopy(d2)
    next_revision.update(revision=2, summary='Updated', created_at='2026-10-27T10:12:00Z')
    layer.overseer.deltas.revise(next_revision, 1, 'revision')
    p = views.list_delta_revisions(d2['delta_id'], limit=1)
    assert p['items'][0]['revision'] == 2
    assert views.list_delta_revisions(d2['delta_id'], limit=1, cursor=p['next_cursor'])['items'][0]['revision'] == 1
    with pytest.raises(AgenticError):
        views.list_delta_revisions(d1['delta_id'], cursor=p['next_cursor'])


def test_saved_batch_and_application_history_pages(layer):
    from qorqut_agentic.overseer.records import canonical

    _, d = setup(layer)
    app_id = uid()
    batch_ids = [uid() for _ in range(3)]
    with layer._ctx.store.transaction() as conn:
        for batch_id in batch_ids:
            batch = {'schema_version': 1, 'id': batch_id, 'run_id': 'synthetic-run',
                     'items': [{'delta_id': d['delta_id'], 'revision': 1, 'ranking_reason': 'Supported.'}],
                     'presentation_ref': None, 'created_at': '2026-10-27T10:00:00Z'}
            conn.execute('INSERT INTO overseer_batches VALUES (?, ?)', (batch_id, canonical(batch)))
        conn.execute('''INSERT INTO delta_applications
            (id,delta_id,accepted_revision,acceptance_ref,phase,external_tasks,merge,checks,created_at,updated_at)
            VALUES (?, ?, 1, '{}', 'merged', '[]', '{"merged_commit":"synthetic"}', '{"state":"failed","receipts":[]}', ?, ?)''',
            (app_id, d['delta_id'], '2026-10-27T10:00:00Z', '2026-10-27T10:00:00Z'))
        for number in (1, 2):
            conn.execute('INSERT INTO overseer_plan_revisions VALUES (?, ?, ?)',
                         (app_id, number, canonical({'revision': number, 'delta_revision': 1})))
            conn.execute('INSERT INTO overseer_application_events VALUES (?, ?, ?, ?)',
                         (uid(), app_id, canonical({'kind': 'synthetic', 'number': number}), '2026-10-27T10:00:00Z'))
    views = Views(layer)
    page = views.list_batches(limit=2)
    assert [x['id'] for x in page['items']] == sorted(batch_ids, reverse=True)[:2]
    assert views.list_batches(limit=2, cursor=page['next_cursor'])['items'][0]['id'] == sorted(batch_ids)[0]
    page = views.list_application_plans(app_id, limit=1)
    assert page['items'][0]['revision'] == 2
    assert views.list_application_plans(app_id, limit=1, cursor=page['next_cursor'])['items'][0]['revision'] == 1
    page = views.list_application_events(app_id, limit=1)
    assert page['items'][0]['record']['number'] == 1
    assert page['items'][0]['id'] and page['items'][0]['created_at'] == '2026-10-27T10:00:00Z'
    assert views.list_application_events(app_id, limit=1, cursor=page['next_cursor'])['items'][0]['record']['number'] == 2
    assert views.list_applications(queue='unimplemented')['items'] == []
    assert views.list_applications()['items'][0]['checks']['state'] == 'failed'


def test_admitted_run_requires_one_linked_canonical_reservation(layer):
    from qorqut_agentic.overseer.records import canonical

    views = Views(layer)
    admitted = {'state': 'linked', 'kind': 'review', 'run_id': 'run_synthetic',
                'runner_operation_id': 'runner-synthetic',
                'invocation': {'conversation_id': 'chat-synthetic', 'executor': {'actor_id': 'agent-synthetic'}}}
    reservation = {**admitted, 'state': 'reserved', 'run_id': 'run_reserved'}
    with layer._ctx.store.transaction() as conn:
        conn.execute('INSERT INTO overseer_operations VALUES (?, ?, ?, ?)',
                     ('run:canonical', 'fingerprint', canonical(admitted), 'synthetic'))
        conn.execute('INSERT INTO overseer_operations VALUES (?, ?, ?, ?)',
                     ('run:reserved', 'fingerprint', canonical(reservation), 'synthetic'))
        conn.execute('INSERT INTO overseer_operations VALUES (?, ?, ?, ?)',
                     ('noncanonical', 'fingerprint', canonical({**admitted, 'run_id': 'run_generic'}), 'synthetic'))
    assert views.get_admitted_run('run_synthetic') == admitted
    assert views.get_admitted_run('run_reserved') is None
    assert views.get_admitted_run('run_generic') is None
    assert views.get_admitted_run('run_unknown') is None
    with layer._ctx.store.transaction() as conn:
        conn.execute('INSERT INTO overseer_operations VALUES (?, ?, ?, ?)',
                     ('run:duplicate', 'fingerprint', canonical(admitted), 'synthetic'))
    with pytest.raises(AgenticError) as error:
        views.get_admitted_run('run_synthetic')
    assert error.value.code == 'evidence_unavailable'


def test_binding_enumeration_is_id_order_with_filter_bound_cursor(layer):
    _, delta = setup(layer)
    records = [binding(delta) for _ in range(3)]
    for n, item in enumerate(records):
        layer.overseer.inputs.bind(item, 'bind-' + str(n))
    views = Views(layer)
    page = views.list_inputs(delta_id=delta['delta_id'], limit=2)
    assert [x['id'] for x in page['items']] == sorted(x['id'] for x in records)[:2]
    assert views.list_inputs(delta_id=delta['delta_id'], limit=2, cursor=page['next_cursor'])['items'][0]['id'] == max(x['id'] for x in records)
    with pytest.raises(AgenticError):
        views.list_inputs(run_id='synthetic-run', cursor=page['next_cursor'])


def test_uncreated_review_request_is_reported_for_its_exact_revision(layer):
    _, delta = setup(layer)
    pending = binding(delta)
    request_ref = pending['request_ref']
    pending['request_ref'] = None
    layer.overseer.inputs.bind(pending, 'pending-input')
    views = Views(layer)
    assert views.has_unbound_review_input(delta['delta_id'], 1)
    assert not views.has_unbound_review_input(delta['delta_id'], 2)
    layer.overseer.inputs.bind({**pending, 'request_ref': request_ref}, 'attached-input')
    assert not views.has_unbound_review_input(delta['delta_id'], 1)


def test_delta_application_matches_current_exact_acceptance(layer):
    from qorqut_agentic.overseer.records import canonical
    _, delta = setup(layer)
    b = binding(delta)
    layer.overseer.inputs.bind(b, 'binding-current')
    gateway = Gateway(b)
    accepted = layer.overseer.apply_owner_response(b['id'], gateway.call(), operation_id='answer-current', gateway=gateway)
    app_id = accepted['application_id']
    views = Views(layer)
    assert views.get_delta_application(delta['delta_id'])['id'] == app_id
    with layer._ctx.store.transaction() as conn:
        original_ref = conn.execute('SELECT acceptance_ref FROM delta_applications WHERE id=?', (app_id,)).fetchone()[0]
        conn.execute('UPDATE delta_applications SET acceptance_ref=? WHERE id=?', ('{"unmatched":true}', app_id))
    assert views.get_delta_application(delta['delta_id']) is None
    with layer._ctx.store.transaction() as conn:
        conn.execute('UPDATE delta_applications SET acceptance_ref=? WHERE id=?', (original_ref, app_id))
    draft = copy.deepcopy(delta)
    draft.update(revision=2, summary='Draft revision', created_at='2026-10-27T10:12:00Z')
    layer.overseer.deltas.revise(draft, 1, 'draft')
    assert views.get_delta_application(delta['delta_id'])['id'] == app_id
    with layer._ctx.store.transaction() as conn:
        conn.execute("UPDATE delta_applications SET phase='complete' WHERE id=?", (app_id,))
        conn.execute('''INSERT INTO delta_applications
            (id,delta_id,accepted_revision,acceptance_ref,phase,external_tasks,checks,created_at,updated_at)
            VALUES (?, ?, 1, ?, 'complete', '[]', '{}', ?, ?)''',
            (uid(), delta['delta_id'], canonical({'other': 'historical'}), '2026-10-27T10:00:00Z', '2026-10-27T10:00:00Z'))
    assert views.get_delta_application(delta['delta_id'])['id'] == app_id
    page = views.list_applications(delta_id=delta['delta_id'], limit=1)
    assert page['next_cursor'] and views.list_applications(delta_id=delta['delta_id'], limit=1, cursor=page['next_cursor'])['items']
    with pytest.raises(AgenticError):
        views.list_applications(cursor=page['next_cursor'])
    with layer._ctx.store.transaction() as conn:
        row = conn.execute('SELECT acceptance_ref FROM delta_applications WHERE id=?', (app_id,)).fetchone()
        conn.execute('''INSERT INTO delta_applications
            (id,delta_id,accepted_revision,acceptance_ref,phase,external_tasks,checks,created_at,updated_at)
            VALUES (?, ?, 1, ?, 'complete', '[]', '{}', ?, ?)''',
            (uid(), delta['delta_id'], row['acceptance_ref'], '2026-10-27T10:01:00Z', '2026-10-27T10:01:00Z'))
    with pytest.raises(AgenticError) as error:
        views.get_delta_application(delta['delta_id'])
    assert error.value.code == 'evidence_unavailable'
