"""Opt-in contrast checks for precision AND recall; not a population estimate."""

import json
import os
from types import SimpleNamespace

import pytest

from bazarlyk.domains.learnings import analysis, verification
from test_background_programs import raw_learning, runtime
from test_learning_provider import live_client


pytestmark = pytest.mark.skipif(os.environ.get('QORQUT_TEST_LEARNING_LIVE') != '1',
                               reason='Real verification provider test is opt-in')


def test_real_verifier_distinguishes_one_offs_from_standing_policy(runtime, live_client):
    cases = [
        ('Send this installation task to the coder now.', [None]),
        ('From now on, always send installation tasks directly to the coder.', ['delta']),
        # Ambiguous durability is acceptable; promotion to universal policy is not.
        ('For Project Cedar, rename the product folders to Willow.', [None, 'context']),
        ('For all future agent workspaces, enforce lowercase folder names in the bootstrap tool.', ['delta']),
        ('For this planning session, list the options before discussing tradeoffs.', [None, 'context']),
        ('Whenever we plan together in future, list options before discussing tradeoffs.', ['delta']),
        ('For Project Cedar, require three signed customers before funding the build.', ['context']),
        ('Add a setting to the agent runner that requires my confirmation before every deployment.', ['delta']),
        ('We have decided that Project Cedar will use Willow as its permanent public product name.', ['context']),
    ]
    client, calls = live_client
    def chat(system, payload, model):
        message, usage = runtime.model.chat([
            {'role': 'system', 'content': system}, {'role': 'user', 'content': payload}],
            model=model, response_format={'type': 'json_object'}, thinking=False,
            max_tokens=16000, timeout_budget=180, temperature=0.1)
        return json.loads(message['content']), usage
    results = []
    for index, (quote, expected) in enumerate(cases):
        core = analysis.build_core({'source_id': 'synthetic', 'session_id': str(index),
            'content': json.dumps({'type': 'user', 'message': {'content': quote}})},
            SimpleNamespace(id=None, slug='Global', root=''))
        brief = SimpleNamespace(id=None, header=lambda: {}, agents=lambda: [])
        items, _, errors = analysis.extract(core, brief, 'contrast', passes=1,
            chat=lambda *args: ({'items': [{**raw_learning(quote), 'text': quote, 'scope': 'global'}]}, {}))
        assert not errors and len(items) == 1
        verified, audit = verification.verify(items, core, client.config.model, chat=chat)
        actual = verified[0]['bucket'] if verified else None
        results.append({'case': index, 'expected': expected, 'actual': actual,
                        'reason': audit[0]['reason']})
    print(json.dumps({'model': client.config.model, 'calls': len(calls), 'contrasts': results}), flush=True)
    assert all(row['actual'] in row['expected'] for row in results), results
