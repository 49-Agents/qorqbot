from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import new_id, plain
from bazarlyk.inbox.attention import FIELDS
from bazarlyk.inbox.focus import FocusService
from bazarlyk.inbox.presentation import action_parts
from bazarlyk.inbox.service import InboxService
from bazarlyk.domains.relationships.items import ContextItems
from test_management_backend import install_coordinator
from test_signoffs import scenario


def test_founder_action_links_are_typed_and_text_stays_plain():
    assert action_parts('Review [the page](https://example.com/a_(b)) and flag issues.') == [
        {'type':'text','text':'Review '},
        {'type':'link','text':'the page','url':'https://example.com/a_(b)'},
        {'type':'text','text':' and flag issues.'},
    ]
    assert action_parts('') == []
    assert action_parts('<script>alert(1)</script>') == [{'type':'text','text':'<script>alert(1)</script>'}]


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'data:text/html,hello',
                               'https://user:secret@example.com', 'mailto:owner@example.com'])
def test_unsafe_or_non_web_links_are_not_link_tags(url):
    assert all(part['type']=='text' for part in action_parts(f'Open [the page]({url})'))


def test_signoff_focus_name_flags_do_not_change_approval(scenario):
    s=scenario
    install_coordinator(s)
    fake=SimpleNamespace(app=s.app,clock=lambda:datetime.now(timezone.utc),
        business_member=InboxService.business_member,
        domains=SimpleNamespace(stories=SimpleNamespace(relationships=SimpleNamespace(items=ContextItems(None,s.app)))))
    FocusService(fake,s.service).register()
    owner=replace(s.owner_context,business_id=None,review_business_id=None)
    proposal=s.propose()
    before=s.detail(proposal)

    def call(op,args):
        result=s.app.invoke(owner,op,{'scope':'global',**args},{'idempotency_key':new_id()})
        assert result.status=='ok',result.to_dict()
        return plain(result.data)

    card=call('focus_present',{'source':{'type':'signoff','id':proposal['id'],'business_id':s.business},
        'display_name':'Approve change','blocker':True,'urgent':True,'requested_response':'Approve or reject.'})['item']
    assert card['display_name']=='Approve change' and card['blocker'] is True and card['urgent'] is True
    card=call('focus_edit',{'item_id':card['id'],'expected_card_revision':card['revision'],
        'changes':{'display_name':'','urgent':False}})['item']
    assert card['display_name']=='' and card['urgent'] is False and card['blocker'] is True
    assert s.detail(proposal)==before
    assert 'recommendation' not in FIELDS and 'recommendation' not in card
    bad=s.app.invoke(owner,'focus_edit',{'scope':'global','item_id':card['id'],
        'expected_card_revision':card['revision'],'changes':{'recommendation':'Removed'}},{'idempotency_key':new_id()})
    assert bad.status=='error'


def install_board(s):
    fake=SimpleNamespace(app=s.app,clock=lambda:datetime.now(timezone.utc),
        business_member=InboxService.business_member,
        domains=SimpleNamespace(stories=SimpleNamespace(relationships=SimpleNamespace(items=ContextItems(None,s.app)))))
    FocusService(fake,s.service).register()


def test_decided_signoff_leaves_focus_and_replacement_inherits_card(scenario):
    s=scenario
    install_coordinator(s)
    install_board(s)
    owner=replace(s.owner_context,business_id=None,review_business_id=None)
    def call(op,args):
        result=s.app.invoke(owner,op,{'scope':'global',**args},{'idempotency_key':new_id()})
        assert result.status=='ok',result.to_dict()
        return plain(result.data)
    def present(proposal,**framing):
        return call('focus_present',{'source':{'type':'signoff','id':proposal['id'],'business_id':s.business},**framing})['item']
    def active():
        return {c['source']['id']:c for c in call('focus_get',{})['items']}
    approved,rejected,answered_later=s.propose('approve me'),s.propose('reject me'),s.propose('keep me')
    for proposal in (approved,rejected,answered_later): present(proposal)
    assert s.decide(approved).status in {'ok','accepted'}
    assert s.decide(rejected,'reject',feedback='No').status=='ok'
    assert set(active())=={answered_later['id']}
    archived=call('focus_get',{'include_archived':True})['items']
    assert {c['source']['id'] for c in archived if c['archived']}=={approved['id'],rejected['id']}
    # A replacement proposal takes over the card's framing and position.
    card=present(answered_later,display_name='Launch copy',requested_response='Approve the copy',urgent=True)
    replacement=s.propose('revised',options={'supersedes_signoff_id':answered_later['id'],
        'expected_superseded_status_revision':answered_later['status_revision']})
    board=active()
    assert set(board)=={replacement['id']}
    inherited=board[replacement['id']]
    assert (inherited['display_name'],inherited['requested_response'],inherited['urgent'],inherited['position'])==(
        'Launch copy','Approve the copy',True,card['position'])
    # A revision submitted after a rejection returns with the rejected card's framing.
    assert s.decide(replacement,'reject',feedback='Shorter').status=='ok'
    assert active()=={}
    revision=s.propose('shorter',options={'supersedes_signoff_id':replacement['id'],
        'expected_superseded_status_revision':s.detail(replacement)['status_revision']})
    assert active()[revision['id']]['display_name']=='Launch copy'


def test_archive_resolved_signoffs_cleans_cards_decided_before_release(scenario):
    s=scenario
    install_coordinator(s)
    install_board(s)
    owner=replace(s.owner_context,business_id=None,review_business_id=None)
    decided,pending=s.propose('decided'),s.propose('pending')
    assert s.decide(decided).status in {'ok','accepted'}
    for proposal in (decided,pending):
        result=s.app.invoke(owner,'focus_present',{'scope':'global','source':{'type':'signoff','id':proposal['id'],'business_id':s.business}},{'idempotency_key':new_id()})
        assert result.status=='ok',result.to_dict()
    with s.db.transaction() as u:
        assert len(s.app.attention.archive_resolved_signoffs(u))==1
    with s.db.transaction() as u:
        assert s.app.attention.archive_resolved_signoffs(u)==[]
    items=s.app.invoke(owner,'focus_get',{'scope':'global'}).data['items']
    assert [c['source']['id'] for c in items]==[pending['id']]
