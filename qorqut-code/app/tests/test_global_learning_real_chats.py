"""Explicit opt-in: real saved chats and DeepSeek, all mutations in pytest's DB.

QORQUT_REAL_CHAT_PATHS is a JSON array of saved Founder/Ordubashy session paths.
QORQUT_REAL_CHAT_EVIDENCE is a private directory outside the repository.
Uses QORQUT_LEARNING_ENV_FILE through the existing live-client fixture.
Never point pytest's --basetemp at operational data.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from bazarlyk.domains.common import rows
from bazarlyk.domains.learnings.analysis import build_core
from foundation_support import BUSINESS
from test_background_programs import runtime
from test_global_chat_learning import global_agent
from test_learning_provider import live_client
from test_learning_sources import call


pytestmark = pytest.mark.skipif(
    os.environ.get('QORQUT_TEST_REAL_GLOBAL_CHATS') != '1', reason='Private real-chat evaluation is opt-in')


def test_real_global_chats_extract_replay_and_file(runtime, live_client):
    max_usd = float(os.environ.get('QORQUT_REAL_CHAT_MAX_USD', '2.0'))
    paths = [Path(path).resolve(strict=True) for path in json.loads(os.environ['QORQUT_REAL_CHAT_PATHS'])]
    evidence = Path(os.environ['QORQUT_REAL_CHAT_EVIDENCE']).resolve()
    assert not evidence.is_relative_to(Path(__file__).resolve().parents[1])
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence.chmod(0o700)
    assert 2 <= len(paths) <= 10 and len(set(paths)) == len(paths)
    identities = {role: global_agent(runtime, role, count=0)[0] for role in ('founder', 'ordubashy')}
    report = {'model': live_client[0].config.model, 'sources': [], 'calls': live_client[1]}
    for path in paths:
        role = path.parent.parent.name
        assert role in identities and path.parent.name == 'sessions'
        content = path.read_bytes()
        session = path.stem.removeprefix('codex-')
        source = {'session_id': session, 'source_id': f'agents/{role}/sessions/{path.name}',
                  'agent_id': identities[role], 'agent_kind': role, 'content': content.decode(),
                  'transcript_hash': hashlib.sha256(content).hexdigest()}
        runtime.sources.append(source)
        pruned = build_core(source, None, identities[role], role)
        report['sources'].append({key: value for key, value in source.items() if key != 'content'})
        (evidence / (session + '.core.json')).write_text(json.dumps(pruned, indent=2))
    try:
        queued = call(runtime, 'learnings_extract', {'passes': 3, 'max_usd': max_usd, 'max_sessions': 10}, global_scope=True)
        report['extraction'] = runtime.background.run_one(queued['job_id'])
        with runtime.database.read() as uow:
            report['runs'] = uow.repository('learning_runs', business_id=None).list()
            report['candidates'] = rows(uow, 'learning_candidates', None)
            assert not uow.repository('learning_candidates', business_id=BUSINESS).list()
        assert report['extraction']['state'] == 'succeeded', report['extraction']
        run = report['runs'][0]['result_json']
        # A real model can produce inadmissible quotes. The pipeline must reject
        # them and leave the affected source retryable, not mark it consumed.
        with runtime.database.read() as uow:
            scanned = uow.repository('learning_scan_state', business_id=None).list()
        failed = {error['source'] for error in run['errors'] if 'source' in error}
        assert run['processed'] == len(paths) - len(failed), run
        assert not failed.intersection(row['session_id'] for row in scanned
                                       if row['state_json'].get('status', 'complete') == 'complete')
        assert live_client[1], 'No actual model calls'
        if failed:
            retry = call(runtime, 'learnings_extract', {'passes': 3, 'max_usd': max_usd}, global_scope=True)
            report['retry'] = runtime.background.run_one(retry['job_id'])
            assert report['retry']['state'] == 'succeeded', report['retry']
            with runtime.database.read() as uow:
                report['runs'] = uow.repository('learning_runs', business_id=None).list()
                report['candidates'] = rows(uow, 'learning_candidates', None)
                scanned = uow.repository('learning_scan_state', business_id=None).list()
            assert len(scanned) == len(paths) and all(row['state_json'].get('status') == 'complete' for row in scanned), \
                'Some sources still need extraction retry; inspect private report'
        before = len(live_client[1])
        replay = call(runtime, 'learnings_extract', {'passes': 3, 'max_usd': max_usd}, global_scope=True)
        assert runtime.background.run_one(replay['job_id'])['state'] == 'succeeded'
        assert len(live_client[1]) == before
        report['replay_no_calls'] = True
        item = next(row for row in report['candidates'] if row['candidate_json']['bucket'] == 'context')
        accepted = call(runtime, 'learnings_review', {'learning_id': item['id'], 'state': 'accepted',
            'feedback': 'Isolated evaluation only. Preserve all business and occasion qualifications.'},
            global_scope=True, revision=item['revision'])['item']
        report['test_only_accepted_id'] = item['id']
        report['filing'] = runtime.background.run_one(accepted['filing']['job_id'])
        assert report['filing']['state'] == 'succeeded', report['filing']
        with runtime.database.read() as uow:
            followups = uow.repository('jobs', business_id=None).list(filters={'state': 'queued'})
        report['followups'] = []
        for job in followups:
            result = runtime.background.run_one(job['id'])
            report['followups'].append(result)
            assert result.get('state') == 'succeeded', result
        with runtime.database.read() as uow:
            docs = uow.repository('documents', business_id=None).list()
            report['documents'] = [runtime.domains.documents.store.view(doc, uow) for doc in docs]
            assert any(doc['kind'] == 'context_note' for doc in docs)
            assert not uow.repository('documents', business_id=BUSINESS).list()
            assert not uow.repository('tasks', business_id=None).list()
    finally:
        if 'documents' not in report:
            with runtime.database.read() as uow:
                report['documents'] = [runtime.domains.documents.store.view(doc, uow)
                    for doc in rows(uow, 'documents', None)]
        (evidence / 'report.json').write_text(json.dumps(report, indent=2, default=str))
