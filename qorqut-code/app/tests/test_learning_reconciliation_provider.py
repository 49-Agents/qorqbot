"""Opt-in real provider checks using synthetic question/answer and update pairs."""
import json
import os

import pytest

from bazarlyk.background.learnings import BusinessBrief
from bazarlyk.background.providers import usage_of
from qorqut_operations.contracts import new_id
from bazarlyk.domains.learnings import analysis, sources
from foundation_support import BUSINESS,HUMAN
from test_background_programs import runtime
from test_learning_provider import live_client

pytestmark=pytest.mark.skipif(os.environ.get('QORQUT_TEST_LEARNING_LIVE')!='1',reason='Real provider acceptance is opt-in')


@pytest.mark.parametrize('scenario,earlier,later',[
    ('answered','How much did the newsletter refund amount to?', 'The newsletter refund was $60, covering the entire newsletter purchase.'),
    ('updated','We agreed to pay Karthik $500 for his promotional post.', 'Karthik and I have now agreed that I will not pay for that promotional post. This replaces the earlier $500 agreement.'),
    ('partial','The $160 refund request remains unresolved.', 'I received a partial refund of $60. The remaining $100 is still disputed; the refund request is not settled.'),
])
def test_real_extraction_reconciles_synthetic_chronology(runtime,live_client,scenario,earlier,later):
    client,calls=live_client
    with runtime.database.read() as uow:
        biz=BusinessBrief(uow.repository('businesses').get(BUSINESS),[])
    def chat(system,user,model):
        message,usage=runtime.model.chat([{'role':'system','content':system},{'role':'user','content':user}],
            model=model,response_format={'type':'json_object'},thinking=False,max_tokens=16000,temperature=0.1,timeout_budget=180)
        parsed=json.loads(message['content'])
        return parsed,usage_of(usage)
    fresh=[]
    for index,text in enumerate((earlier,later)):
        packet=sources.contribution('story_note',new_id(),BUSINESS,{'id':HUMAN,'name':'Owner','role':'owner'},
            f'2026-09-10T{9+index:02d}:00:00Z',[{'role':'owner','text':text}],{'title':'Newsletter refund' if scenario!='updated' else 'Promotional post'},resource_id='one-story')
        core=sources.core(packet,biz)
        items,_,errors=analysis.extract(core,biz,'synthetic-run',passes=3,model=client.config.model,chat=chat)
        assert not errors,errors
        fresh.extend(items)
    existing=[]
    if scenario=='answered':
        from test_learning_reconciliation import candidate
        # A question already in the queue must disappear when its answer arrives,
        # even when current extraction would omit that operational question.
        existing=[candidate('pending-question',earlier,'question','2026-09-10T09:00:00Z',revision=1)]
        fresh=[i for i in fresh if i['tag']!='question']
    result,_,stats,_=analysis.dedup(fresh,existing,biz,'synthetic-run',model=client.config.model,chat=chat)
    assert result
    if scenario=='answered':
        assert len(result)==1 and result[0]['tag']=='fact',[(i['text'],i['tag']) for i in result]
        assert any(r['kind']=='answered' and r['automatic'] for r in result[0].get('reconciliations',[])),result
    elif scenario=='updated':
        assert not any('agreed to pay' in i['text'].lower() for i in result),[i['text'] for i in result]
        assert any(r['kind']=='updated' and r['automatic'] for i in result for r in i.get('reconciliations',[])),result
    else:
        assert any('100' in i['text'] and ('disput' in i['text'].lower() or 'unresolved' in i['text'].lower()) for i in result),[i['text'] for i in result]
        assert not any(i['text'].lower().endswith('fully settled.') for i in result)
    print(f'LIVE {scenario}: {len(calls)} calls, {len(result)} candidates; reconciliation={stats}',flush=True)
