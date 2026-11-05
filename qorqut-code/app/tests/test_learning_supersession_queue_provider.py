"""Explicit opt-in: read the real pending queue, never apply model outputs."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3

import pytest

from bazarlyk.background.learnings import BusinessBrief
from bazarlyk.domains.learnings import analysis
from test_background_programs import runtime
from test_learning_provider import live_client


pytestmark = pytest.mark.skipif(os.environ.get('QORQUT_TEST_REAL_LEARNING_QUEUE') != '1',
                               reason='Private queue/provider preview is opt-in')


def test_real_pending_queue_preview_without_mutations(runtime, live_client):
    path = Path(os.environ['QORQUT_LEARNING_QUEUE_DB']).resolve(strict=True)
    output = Path(os.environ['QORQUT_LEARNING_QUEUE_EVIDENCE']).resolve()
    assert not output.is_relative_to(Path(__file__).resolve().parents[1])
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output.chmod(0o700)
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        existing = [{**json.loads(row['candidate_json']), 'revision': row['revision'], 'state': 'open', 'reviewed': False}
            for row in db.execute("SELECT c.* FROM learning_candidates c LEFT JOIN learning_decisions d "
                "ON d.candidate_id=c.id AND d.business_id IS c.business_id WHERE c.business_id IS NULL "
                "AND d.candidate_id IS NULL AND c.work_state IS NULL AND c.filing_json='{}' "
                "AND json_extract(c.candidate_json,'$.reconciled_into') IS NULL ORDER BY c.id")]
    before = deepcopy(existing)
    client, calls = live_client
    def chat(system, user, model):
        message, usage = runtime.model.chat([{'role': 'system', 'content': system},
            {'role': 'user', 'content': user}], model=model, response_format={'type': 'json_object'},
            thinking=False, max_tokens=16000, temperature=0.1, timeout_budget=180)
        return json.loads(message['content']), usage
    brief = BusinessBrief({'id': None, 'slug': 'Global', 'hq_path': '', 'name': 'Global',
                           'stage': '', 'config_json': {}}, [])
    items, lines, stats, usage = analysis.dedup([], existing, brief, 'read-only-preview',
        model=client.config.model, chat=chat, supersession_mode='preview')
    assert not items and existing == before and len(calls) == 1
    assert not any(line['kind'] == 'superseded' for line in lines)
    report = {'model': client.config.model, 'calls': calls, 'queue_count': len(existing),
              'stats': stats, 'usage': usage, 'proposals': lines, 'existing': existing}
    (output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'model': client.config.model, 'queue_count': len(existing), 'stats': stats,
                      'output_relations': len(lines), 'applied': 0}), flush=True)
