"""Opt-in real DeepSeek comparison, without any production queue changes."""
import json
import os

import pytest

from bazarlyk.domains.learnings import analysis
from test_background_learning_analysis import brief
from test_background_programs import runtime
from test_learning_provider import live_client
from test_learning_supersession import pair


pytestmark = pytest.mark.skipif(os.environ.get('QORQUT_TEST_LEARNING_LIVE') != '1',
                               reason='Real provider acceptance is opt-in')


def test_real_supersession_distinguishes_resolution_from_partial_or_unrelated(runtime, live_client):
    client, calls = live_client
    outcomes = []
    def chat(system, user, model):
        message, usage = runtime.model.chat([{'role': 'system', 'content': system},
            {'role': 'user', 'content': user}], model=model, response_format={'type': 'json_object'},
            thinking=False, max_tokens=16000, temperature=0.1, timeout_budget=180)
        return json.loads(message['content']), usage
    for label, wording, expected in [
        ('full', 'Karthik has paid contract C42 in full.', True),
        ('partial', 'Karthik has paid only half of contract C42; the other half is still unpaid.', False),
        ('different', 'Karthik has paid contract C99 in full. C42 is a different contract.', False),
        ('promise', 'Karthik promised to pay contract C42 next week; payment has not been received.', False),
    ]:
        old, new = pair()
        new['text'] = new['evidence']['owner_quote'] = wording
        _, lines, _, _ = analysis.dedup([new], [old], brief(), 'live-test', model=client.config.model,
            chat=chat, supersession_mode='apply')
        actual = any(line['kind'] == 'superseded' for line in lines)
        outcomes.append({'case': label, 'expected': expected, 'superseded': actual})
    print(json.dumps({'model': client.config.model, 'calls': len(calls), 'outcomes': outcomes}), flush=True)
    assert all(row['expected'] == row['superseded'] for row in outcomes), outcomes
