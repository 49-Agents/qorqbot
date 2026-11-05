"""Chronology-aware reconciliation and reversible review-queue consolidation."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from bazarlyk.domains.learnings import analysis
from qorqut_operations.contracts import new_id
from foundation_support import BUSINESS, HUMAN
from test_background_learning_analysis import brief
from test_background_programs import runtime, raw_learning
from test_learning_sources import call, question


def candidate(identity, text, tag, at, **extra):
    return {**raw_learning(text), 'id':identity,'text':text,'tag':tag,'bucket':'context',
        'evidence':{'session':identity,'turns':['T1'],'owner_quote':text,'source_at':at},
        'run':'test-run', 'created':'2026-09-10T23:00:00Z',**extra}


def compare(fresh, existing=(), **answer):
    return analysis.dedup(fresh,list(existing),brief(),'test-run',chat=lambda *a:(answer,{}))


def test_same_day_question_becomes_one_fact_with_original_evidence():
    q=candidate('q','How much was refunded?','question','2026-09-10T09:00:00Z')
    a=candidate('a','The newsletter was refunded in full: $60.','fact','2026-09-10T10:00:00Z')
    original=deepcopy(q)
    result,lines,stats,_=compare([q,a],resolutions=[{'earlier':'q','later':'a','kind':'answered','reason':'The owner explicitly answered the amount.'}])
    assert [i['id'] for i in result]==['a'] and not lines and stats['resolved']==1
    assert result[0]['reconciliations'][0]['earlier']==original
    assert q==original and result[0]['evidence']==a['evidence']


@pytest.mark.parametrize('at', ['2026-09-11T10:00:00Z', None, '2026-09-10T08:00:00Z'])
def test_missing_reversed_or_different_day_does_not_silently_remove_question(at):
    q=candidate('q','Which price?','question','2026-09-10T09:00:00Z')
    a=candidate('a','The price is $60.','fact',at)
    result,_,_,_=compare([q,a],resolutions=[{'earlier':'q','later':'a','kind':'answered','reason':'Price answered.'}])
    assert len(result)==2


def test_accepted_lesson_stays_unchanged_with_proposed_update():
    old=candidate('old','Pay Karthik $500.','decision','2026-09-10T09:00:00Z',state='accepted',reviewed=True)
    new=candidate('new','We agreed not to pay for that post.','decision','2026-09-10T10:00:00Z')
    before=deepcopy(old)
    result,lines,_,_=compare([new],[old],resolutions=[{'earlier':'old','later':'new','kind':'updated','reason':'The later agreement replaces the payment plan.'}])
    assert old==before and not lines and result[0]['reconciliations'][0]['automatic'] is False


def test_fresh_conflict_overrides_resolution_and_merge():
    old=candidate('old','Refund remains unresolved.','fact','2026-09-10T09:00:00Z')
    new=candidate('new','Received a partial refund of $60.','fact','2026-09-10T10:00:00Z')
    result,_,_,_=compare([old,new],merges=[{'keep':'new','drop':['old']}],
        contradicts=[{'fresh':'new','existing':'old'}],
        resolutions=[{'earlier':'old','later':'new','kind':'updated','reason':'Partial payment.'}])
    assert len(result)==2 and result[1]['contradicts']==['old']
    assert not result[1].get('reconciliations')


def test_ambiguous_chains_cycles_and_unrelated_scopes_do_not_drop_items():
    items=[candidate(i,i,'fact',f'2026-09-10T0{n}:00:00Z') for n,i in enumerate('abc',1)]
    for pairs in [[('a','b'),('b','a')],[('a','b'),('b','c')],[('a','b'),('a','c')]]:
        result,_,_,_=compare(deepcopy(items),resolutions=[{'earlier':a,'later':b,'kind':'duplicate','reason':'Same claim'} for a,b in pairs])
        # All relations in a chain must be refused, not just its first edge.
        assert len(result)==3
    items[1]['scope']='global'
    result,_,_,_=compare(items,resolutions=[{'earlier':'a','later':'b','kind':'duplicate','reason':'Same claim'}])
    assert len(result)==3


def test_recommendation_duplicates_merge_across_bucket_without_becoming_decision():
    a=candidate('a','Proposal: make app canonical.','idea',None)
    b={**a,'id':'b','bucket':'delta'}
    result,_,_,_=compare([a,b],merges=[{'keep':'a','drop':['b']}])
    assert len(result)==1 and result[0]['tag']=='idea'


@pytest.mark.parametrize('global_scope',[False,True])
@pytest.mark.parametrize('separate_runs',[False,True])
def test_answer_queue_and_keep_both_restore_preserve_evidence(runtime,global_scope,separate_runs):
    scope=None if global_scope else BUSINESS
    texts=['How much was refunded?','The newsletter refund was $60.']
    raws=[]
    def add(index):
        previous_clock=runtime.signoffs.clock
        runtime.signoffs.clock=lambda:f'2026-09-10T{9+index:02d}:00:00Z'
        try:
            question(runtime,global_scope=global_scope,notes=texts[index])
        finally:
            runtime.signoffs.clock=previous_clock
        raw=raw_learning(texts[index]);raw.update(text=texts[index],tag='question' if index==0 else 'fact',bucket='context',scope='global' if global_scope else 'biz')
        runtime.model.answers.append({'items':[raw]})
    def run():
        queued=call(runtime,'learnings_extract',{'passes':1},global_scope=global_scope)
        result=runtime.background.run_one(queued['job_id']);assert result.get('state')=='succeeded',result
    add(0)
    if separate_runs:run()
    add(1)
    def resolution(messages):
        data=json.loads(messages[1]['content'])
        q=next(i for i in data['fresh']+data['existing'] if i['tag']=='question')
        a=next(i for i in data['fresh'] if i['tag']=='fact')
        assert q['source_at']<a['source_at']
        return {'resolutions':[{'earlier':q['id'],'later':a['id'],'kind':'answered','reason':'The later reply answers the refund amount.'}]}
    runtime.model.answers.append(resolution)
    run()
    items=call(runtime,'learnings_list',{},global_scope=global_scope)['items']
    assert len(items)==1 and items[0]['tag']=='fact'
    answer=items[0]
    detail=call(runtime,'learnings_get',{'learning_id':answer['id']},global_scope=global_scope)
    assert len(detail['cores'])==2
    assert answer['reconciliations'][0]['earlier']['text']==texts[0]
    call(runtime,'learnings_keep_both',{'learning_id':answer['id']},global_scope=global_scope,revision=answer['revision'])
    restored=call(runtime,'learnings_list',{},global_scope=global_scope)['items']
    assert len(restored)==2 and {i['tag'] for i in restored}=={'question','fact'}
    with runtime.database.read() as uow:
        assert not uow.repository('learning_decisions',business_id=scope).list()
        assert uow.repository('learning_history',business_id=scope).list()


def test_short_question_answer_pair_across_utc_midnight_stays_together():
    q=candidate('q','Which price?','question','2026-09-10T23:30:00Z')
    a=candidate('a','The price is $60.','fact','2026-09-11T00:15:00Z')
    result,_,_,_=compare([q,a],resolutions=[{'earlier':'q','later':'a','kind':'answered','reason':'The owner answered 45 minutes later.'}])
    assert [i['id'] for i in result]==['a']


def test_question_and_answer_duplicate_aliases_still_resolve_to_existing_answer():
    q=candidate('q','How much?','question','2026-09-10T09:00:00Z',revision=1)
    a=candidate('a','The refund was $60.','fact','2026-09-10T10:00:00Z',revision=1,state='accepted',reviewed=True)
    newq={**q,'id':'new-q'};newa={**a,'id':'new-a','state':'open','reviewed':False}
    result,lines,_,_=compare([newq,newa],[q,a],duplicates=[{'fresh':'new-q','of':'q'},{'fresh':'new-a','of':'a'}],
        resolutions=[{'earlier':'new-q','later':'new-a','kind':'answered','reason':'The later answer supplies the amount.'}])
    assert not result
    assert any(l['kind']=='reconciled' and l['of']=='q' and l['into']=='a' for l in lines)
    assert any(l['kind']=='related' and l['of']=='a' for l in lines)
    assert a['state']=='accepted' and not a.get('reconciliations')


def test_owner_review_during_comparison_prevents_automatic_hiding(runtime):
    question(runtime,global_scope=True,notes='How much was refunded?')
    raw=raw_learning('How much was refunded?');raw.update(text='How much was refunded?',tag='question',bucket='context',scope='global')
    runtime.model.answers.append({'items':[raw]})
    queued=call(runtime,'learnings_extract',{'passes':1},global_scope=True)
    assert runtime.background.run_one(queued['job_id'])['state']=='succeeded'
    old=call(runtime,'learnings_list',{},global_scope=True)['items'][0]
    question(runtime,global_scope=True,notes='The newsletter refund was $60.')
    answer=raw_learning('The newsletter refund was $60.');answer.update(text='The newsletter refund was $60.',tag='fact',bucket='context',scope='global')
    runtime.model.answers.append({'items':[answer]})
    def resolve(messages):
        data=json.loads(messages[1]['content'])
        call(runtime,'learnings_review',{'learning_id':old['id'],'state':'accepted','feedback':'Preserve my question'},global_scope=True,revision=old['revision'])
        return {'resolutions':[{'earlier':old['id'],'later':data['fresh'][0]['id'],'kind':'answered','reason':'Explicit later answer.'}]}
    runtime.model.answers.append(resolve)
    queued=call(runtime,'learnings_extract',{'passes':1},global_scope=True)
    assert runtime.background.run_one(queued['job_id']).get('state')!='succeeded'
    detail=call(runtime,'learnings_get',{'learning_id':old['id']},global_scope=True)['item']
    assert detail['state']=='accepted' and detail['decision']['note']=='Preserve my question'
    assert not detail.get('reconciled_into')
