"""Gate A adapter exercise; synthetic host and explicitly supplied gateways only."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from operational_support import BUSINESS, OperationalHost
from bazarlyk.overseer.evidence import EvidenceProvider
from bazarlyk.overseer.coverage import AnalysisCoverage
from qorqut_agentic import AgenticLayer

MODULE = Path(__file__).resolve().parents[3] / 'qorqut-agentic'
ATTR = {'actor_id': 'test-owner', 'actor_kind': 'owner', 'reason': 'synthetic gate check'}

class Access:
    def resolve_selection(self, selection):
        assert selection['mode'] == 'explicit'
        assert selection['namespaces'] == [{'kind': 'business', 'business_id': BUSINESS}]
        return selection
    def authorize_source(self, ref):
        assert ref['namespace'] == {'kind': 'business', 'business_id': BUSINESS}

class Invocation:
    def __init__(self, kind):
        self.ref = {'provider': 'fixture', 'kind': 'owner_message', 'id': str(uuid4()), 'revision': 'synthetic'}
        self.value = {'invocation_key': str(uuid4()), 'reference': self.ref, 'allowed_kinds': [kind],
                      'origin': 'owner', 'owner_principal_id': str(uuid4()), 'conversation_id': str(uuid4()),
                      'executor': {'actor_id': 'synthetic-overseer', 'actor_kind': 'agent'}, 'arguments': {},
                      'source_text': 'Synthetic invocation', 'source_hash': 'synthetic'}
    def resolve_invocation(self, kind, ref):
        assert ref == self.ref and kind in self.value['allowed_kinds']
        return deepcopy(self.value)

def test_actual_source_refs_coverage_and_packaged_facade(tmp_path):
    layer = AgenticLayer.initialize(tmp_path / 'root', tmp_path / 'state',
                                    git_user={'name': 'Synthetic', 'email': 'synthetic@example.invalid'})
    layer.authoring.import_package(ATTR, 'overseer', {'directory': str(MODULE / 'src/qorqut_agentic/resources/overseer')})
    assert layer.catalog.validate()['valid']
    runs = {}
    for kind, identity in [('extraction', 'overseer/extract'), ('review', 'overseer/review'), ('application', 'overseer/apply')]:
        gateway = Invocation(kind)
        result = layer.overseer.start_run(kind, {'reference': gateway.ref}, operation_id='start-' + kind, gateway=gateway)
        run = layer.workflows.get(result['run_id'])['run']
        assert run['workflow_ref'] == identity
        runs[kind] = result['run_id']

    host = OperationalHost(tmp_path / 'host')
    with host.database.transaction() as uow:
        uow.connection.execute('CREATE TABLE IF NOT EXISTS documents(id TEXT,business_id TEXT,kind TEXT)')
        uow.connection.execute('CREATE TABLE IF NOT EXISTS native_captures(agent_id TEXT,provider TEXT,conversation_id TEXT,receipt_id TEXT,captured_length INTEGER)')
    task = host.create_task('Synthetic task')
    host.data('tasks_comment', {'task_id': task['id'], 'text': 'Synthetic correction'}, host.agent(), expected_revision=1)
    action = host.record_action('Synthetic result', task_id=task['id'])
    host.data('actions_correct', {'action_id': action['action_id'], 'text': 'Synthetic revision'}, host.agent(), expected_revision=1)
    archive = tmp_path / 'archive'
    archive.mkdir(mode=0o700)
    runtime = SimpleNamespace(database=host.database, application=host.app, native_chat=None)
    provider = EvidenceProvider(runtime, host.agent(), archive, Access(), overseer_ids={'synthetic-overseer'}, coverage=AnalysisCoverage(layer))
    selection = {'mode': 'explicit', 'namespaces': [{'kind': 'business', 'business_id': BUSINESS}]}
    page = provider.list_changes(selection, limit=50)
    refs = page['items']
    assert {'task', 'task_message', 'action_revision'} <= {ref['kind'] for ref in refs}
    note = next(ref for ref in refs if ref['kind'] == 'task_message')
    edit = next(ref for ref in refs if ref['kind'] == 'action_revision' and ref['revision'] == '2')
    assert provider.get_source(note)['content']['body'] == 'Synthetic correction'
    assert edit['before_ref']['revision'] == '1' and edit['after_ref']['revision'] == '2'
    scope = {'description': 'Synthetic work', 'project_ids': [], 'work_types': [], 'workflow_refs': [], 'conditions': [], 'exceptions': []}
    record = {'schema_version': 1, 'id': str(uuid4()), 'extraction_run_id': runs['extraction'],
              'summary': 'Synthetic proposal', 'change_direction': 'Synthetic change', 'observed_fact': 'Synthetic correction',
              'interpretation': 'Synthetic interpretation', 'scope': scope, 'target_refs': [], 'origin_type': 'inferred_improvement',
              'sources': [{'ref': note, 'role': 'supporting'}, {'ref': edit, 'role': 'contextual'}],
              'created_at': '2026-10-27T00:00:00Z', 'created_by': 'synthetic-overseer'}
    layer.overseer.proposals.record(record, 'proposal')
    assert layer.overseer.proposals.get(record['id']) == record
    receipt = layer.overseer.coverage.record({'schema_version': 1, 'run_id': runs['extraction'], 'frontier': page['frontier'],
        'analyzed_sources': [note], 'pending_sources': [edit], 'gaps': [{'code': 'synthetic_pending'}], 'complete': False, 'next_cursor': None}, 'coverage')
    assert layer.overseer.coverage.get(receipt['id'])['analyzed_sources'] == [note]
    assert layer.overseer.coverage.get(source_refs=[note, edit])['analyzed_sources'] == [note]
    mutated = deepcopy(note); mutated['content_hash'] = 'different'
    assert layer.overseer.coverage.get(source_refs=[mutated])['analyzed_sources'] == []
    again = provider.list_changes(selection, limit=50)
    assert note not in again['items'] and edit in again['items']
