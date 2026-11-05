"""P04 wiring against accepted P05 in an isolated overlay, with synthetic host authority.

Run separately with accepted P05 src and test root before P04's module on PYTHONPATH.
The module and Git/check operations are real; owner response and host source policy
are explicit fakes, covered separately by the real-host Signoff/access tests.
"""
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from starlette.datastructures import QueryParams

import qorqut_agentic.overseer
P04_VIEWS = Path(__file__).resolve().parents[3] / 'qorqut-agentic/src/qorqut_agentic/overseer'
if str(P04_VIEWS) not in qorqut_agentic.overseer.__path__:
    qorqut_agentic.overseer.__path__.append(str(P04_VIEWS))
from qorqut_agentic import AgenticError
from qorqut_agentic.overseer.views import Views
from tests.conftest import make_layer, git, sha
from tests.test_overseer_contracts import install_test_workflows, InvocationGateway, plan
from tests.test_overseer_decisions import Gateway, binding, revision, uid
from tests.test_overseer_state import proposal
from bazarlyk.overseer import executor, operations, http
from bazarlyk.overseer.provenance import content_hash
from bazarlyk.overseer.reads import OverseerReads


class Database:
    @contextlib.contextmanager
    def read(self):
        yield None


def application(layer, prefix, path):
    p = proposal(); p['id'] = uid()
    layer.overseer.proposals.record(p, prefix + '-proposal')
    delta = revision(p)
    layer.overseer.deltas.create(delta, prefix + '-delta')
    b = binding(delta)
    layer.overseer.inputs.bind(b, prefix + '-review')
    gateway = Gateway(b)
    app_id = layer.overseer.apply_owner_response(b['id'], gateway.call(), operation_id=prefix + '-accept', gateway=gateway)['application_id']
    invocation = InvocationGateway('application', {'application_id': app_id})
    layer.overseer.start_run('application', {'reference': invocation.ref}, operation_id=prefix + '-start', gateway=invocation)
    saved_plan = plan(delta)
    saved_plan['changes'] = [{'kind': 'file', 'action': 'edit', 'path': path, 'description': 'Synthetic change', 'expected_sha256': sha('before\n')}]
    saved_plan['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(), 'resource_hashes': {path: sha('before\n')}}
    saved = layer.overseer.applications.save_plan(app_id, saved_plan, expected_revision=2, operation_id=prefix + '-plan')
    b = binding(delta, 'plan', {'go': 'proceed'})
    b['targets'].update(application_id=app_id, application_revision=saved['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(b, prefix + '-plan-binding')
    gateway = Gateway(b, 'go')
    layer.overseer.apply_owner_response(b['id'], gateway.call(), operation_id=prefix + '-agree', gateway=gateway)
    return app_id, delta


def test_p05_satisfaction_reconciliation_and_real_event_history(tmp_path, monkeypatch):
    import bazarlyk.overseer.evidence as evidence_module
    from qorqut_operations.contracts import DomainError
    layer = make_layer(tmp_path)
    install_test_workflows(layer, tmp_path)
    for path in ('a.md', 'b.md', 'c.md', 'd.md'):
        (layer.root / path).write_text('before\n')
    git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'Synthetic rules')
    first, _ = application(layer, 'a', 'a.md')
    second, delta_b = application(layer, 'b', 'b.md')
    third, delta_c = application(layer, 'c', 'c.md')
    fourth, delta_d = application(layer, 'd', 'd.md')
    ctx = SimpleNamespace(principal_id='synthetic-current-overseer')
    runtime = SimpleNamespace(database=Database(), agentic_layer=layer, paths=SimpleNamespace(overseer_archive=tmp_path))
    runtime.overseer = operations.OverseerOperations(runtime)
    monkeypatch.setattr(operations, 'current_overseer', lambda *a, **k: (ctx, None))
    monkeypatch.setattr(executor, 'current_overseer', lambda *a, **k: (ctx, None))
    runtime.overseer._check_delta = lambda context, value: value
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path/'worktrees', states_root=tmp_path/'states',
        external_task_gateway=None, satisfaction_gateway=executor.SatisfactionGateway(runtime))
    draft = apps.prepare(first, expected_revision=apps.get(first)['revision'], operation_id='a-prepare')
    draft_root = Path(draft['execution']['worktree_path'])
    (draft_root/'a.md').write_text('after\n')
    git(draft_root, 'add', 'a.md'); git(draft_root, 'commit', '-m', 'Synthetic draft')
    candidate = apps.prepare_merge(first, expected_revision=draft['application']['revision'], operation_id='a-candidate')
    merged = apps.publish(first, expected_revision=candidate['application']['revision'], operation_id='a-publish')
    checked = apps.record_checks(first, phase='post_merge', expected_revision=merged['application']['revision'], operation_id='a-check')
    assert checked['check_run']['state'] == 'passed'
    receipt = {'provider': 'qorqut_agentic', 'kind': 'check_receipt', 'application_id': first,
               'check_run_id': checked['check_run']['id'], 'check_id': checked['check_run']['receipts'][0]['check_id']}

    content = {'message': 'Synthetic source content'}
    fixture = json.loads(Path('/home/alp/qorqbot-v1.3/execution/overseer-v1/fixtures/contracts-v1.json').read_text())
    source = {**fixture['proposals'][0]['sources'][0]['ref'], 'content_hash': content_hash(content)}
    calls = []
    permitted = {'source': True, 'receipt': True}
    class SyntheticSourceProvider:
        def __init__(self, *args, **kwargs): pass
        def get_source(self, ref):
            assert executor._current_context.get() is ctx
            calls.append(('source', ref))
            if not permitted['source']: raise DomainError('forbidden', 'Synthetic source revoked')
            return {'content': content}
    monkeypatch.setattr(evidence_module, 'EvidenceProvider', SyntheticSourceProvider)
    original_receipt = apps.get_check_receipt
    def read_receipt(ref):
        assert executor._current_context.get() is ctx
        calls.append(('receipt', ref))
        if not permitted['receipt']: raise DomainError('forbidden', 'Synthetic receipt access revoked')
        return original_receipt(ref)
    monkeypatch.setattr(apps, 'get_check_receipt', read_receipt)

    def saved_answer(app_id, delta, prefix):
        app = apps.get(app_id)
        with executor.acting_as(ctx):
            inspected = apps.inspect_satisfaction(app_id, source_refs=[source], check_receipt_refs=[receipt],
                explanation='Synthetic source and real successful check', expected_revision=app['revision'], operation_id=prefix + '-inspect')
        b = binding(delta, 'close_satisfied', {'done': 'close_satisfied'})
        b['targets'].update(application_id=app_id, application_revision=app['revision'], plan_revision=1)
        b['satisfaction_inspection_id'] = inspected['inspection']['id']
        b['satisfaction_evidence'] = inspected['inspection']['evidence']
        layer.overseer.inputs.bind(b, prefix + '-bind')
        gateway = Gateway(b, 'done')
        monkeypatch.setattr(operations, 'OwnerInputGateway', lambda runtime, context: gateway)
        return b, {'binding_id': b['id'], 'response': gateway.call(), 'operation_id': prefix + '-answer'}

    _, answer = saved_answer(second, delta_b, 'allowed')
    sentinel = object()
    with executor.acting_as(sentinel):
        assert runtime.overseer.mutate('overseer_input_reconcile', ctx, answer)['status'] == 'applied'
        assert executor._current_context.get() is sentinel
    assert executor._current_context.get() is None and apps.get(second)['outcome'] == 'already_satisfied'
    assert [kind for kind, _ in calls].count('source') == 2
    assert [kind for kind, _ in calls].count('receipt') == 2

    b, answer = saved_answer(third, delta_c, 'revoked')
    permitted['source'] = False
    with executor.acting_as(sentinel):
        with pytest.raises(AgenticError) as error:
            runtime.overseer.mutate('overseer_input_reconcile', ctx, answer)
        assert executor._current_context.get() is sentinel
    assert error.value.code == 'evidence_unavailable' and executor._current_context.get() is None
    assert layer.overseer.inputs.get(b['id'], include_response=True)['result']['status'] == 'evidence_unavailable'
    assert apps.get(third)['phase'] == 'planning'
    permitted['source'] = True
    with pytest.raises(AgenticError) as replay:
        runtime.overseer.mutate('overseer_input_reconcile', ctx, answer)
    assert replay.value.code == 'evidence_unavailable' and apps.get(third)['phase'] == 'planning'
    assert executor._current_context.get() is None
    b, answer = saved_answer(fourth, delta_d, 'revoked-receipt')
    permitted['receipt'] = False
    with executor.acting_as(sentinel):
        with pytest.raises(AgenticError) as error:
            runtime.overseer.mutate('overseer_input_reconcile', ctx, answer)
        assert executor._current_context.get() is sentinel
    assert error.value.code == 'evidence_unavailable' and executor._current_context.get() is None
    assert layer.overseer.inputs.get(b['id'], include_response=True)['result']['status'] == 'evidence_unavailable'
    assert apps.get(fourth)['phase'] == 'planning'

    reader = OverseerReads.__new__(OverseerReads)
    reader.runtime, reader.context, reader.views = runtime, ctx, Views(layer)
    reader._delta = lambda identity, revision=None: (layer.overseer.deltas.get(identity, revision=revision), [])
    reader.discussion = lambda *args, **kwargs: reader._page()
    with layer._ctx.store.reader() as conn:
        saved = [dict(row) for row in conn.execute('SELECT id,created_at,record FROM overseer_application_events WHERE application_id=? ORDER BY rowid', (first,))]
    all_events, cursor = [], None
    while True:
        page = reader.events(first, limit=2, cursor=cursor)
        assert not page['gaps']
        all_events.extend(page['items'])
        cursor = page['next_cursor']
        if cursor is None: break
    assert [event['id'] for event in all_events] == [row['id'] for row in saved]
    assert [event['at'] for event in all_events] == [row['created_at'] for row in saved]
    assert [event['kind'] for event in all_events] == [json.loads(row['record'])['type'] for row in saved]
    assert len({event['id'] for event in all_events}) == len(saved) >= 9
    execution_events = [e for e in all_events if e['kind'] in ('prepare_intent', 'prepared_draft', 'candidate')]
    assert execution_events and all(e['plan_revision'] == 1 for e in execution_events)
    assert 'worktree_path' not in json.dumps(all_events) and 'state_path' not in json.dumps(all_events)
    assert str(tmp_path) not in json.dumps(all_events)
    with pytest.raises(AgenticError):
        cursor = reader.events(first, limit=1)['next_cursor']
        reader.events(second, limit=1, cursor=cursor)

    monkeypatch.setattr(http, 'OverseerReads', lambda runtime, context: reader)
    async def route(parts, query=''):
        request = SimpleNamespace(method='GET', query_params=QueryParams(query))
        return await http.route(runtime, request, ctx, parts, 'synthetic-request')
    response = anyio.run(route, ['overseer', 'applications', first, 'events'], 'limit=2')
    body = json.loads(response.body)['data']
    assert [event['id'] for event in body['items']] == [e['id'] for e in all_events[:2]]
    response = anyio.run(route, ['overseer', 'applications', first])
    detail = json.loads(response.body)['data']
    assert [event['id'] for event in detail['events']['items']] == [e['id'] for e in all_events]
