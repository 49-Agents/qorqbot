"""Owner edits preserve exact approval, immutable history and send-once semantics."""
from dataclasses import replace
import pytest
from qorqut_operations.contracts import ChangeSet, new_id, plain
from whatsapp_signoff_support import Setup, CHAT, TEXT

@pytest.fixture
def wa(tmp_path):
    setup = Setup(tmp_path)
    try:
        yield setup
    finally:
        setup.close()

def edit(wa, original, text='Changed message', *, actor=None, key=None, changes=None):
    return wa.runtime.signoffs.amend(actor or wa.owner(), original['id'],
        changes=changes if changes is not None else {'text': text},
        proposal_revision=original['proposal_revision'], payload_hash=original['payload_hash'],
        expected_status_revision=original['status_revision'], idempotency_key=key or new_id())

def test_owner_edit_replaces_frozen_proposal_without_sending_and_only_new_text_is_sent(wa):
    original = plain(wa.detail(wa.propose().signoff['id']))
    assert original['editable_fields'] == ['text']
    key = new_id()
    result = edit(wa, original, key=key)
    assert result.status == 'ok', result.to_dict()
    revised = result.data['signoff']
    assert revised['state'] == 'pending' and revised['proposal_revision'] == 2
    assert revised['id'] != original['id'] and revised['payload_hash'] != original['payload_hash']
    assert revised['preview']['owner_edit']['by'] == wa.owner().principal_id
    assert revised['supersedes_id'] == original['id']
    old = plain(wa.detail(original['id']))
    assert old['preview'] == original['preview'] and old['superseded_by_id'] == revised['id']
    assert not wa.connector.sends and wa.job_count() == 0
    replay = edit(wa, original, key=key)
    assert replay.data['replayed'] and replay.data['signoff']['id'] == revised['id']
    assert edit(wa, original, 'Different body', key=key).error['code'] == 'idempotency_conflict'
    assert edit(wa, original).error['code'] == 'stale_signoff'
    assert wa.approve(original['id']).status == 'error'
    assert wa.approve(revised['id']).status == 'accepted'
    wa.runtime.signoffs.run_one(revised['id'])
    job_id = wa.detail(revised['id'])['apply_result']['data']['job_id']
    assert wa.runtime.background.run_one(job_id)['state'] == 'succeeded'
    assert wa.connector.sends[0][1:3] == (CHAT, 'Changed message')
    assert wa.runtime.background.run_one(job_id) is None and len(wa.connector.sends) == 1

@pytest.mark.parametrize('case', ['agent', 'mcp_owner', 'recipient', 'blank', 'large', 'stale', 'decided', 'policy', 'account'])
def test_refused_edits_have_no_effect(wa, case):
    original = plain(wa.detail(wa.propose().signoff['id']))
    kwargs = {}
    if case == 'agent': kwargs['actor'] = wa.agent_context()
    if case == 'mcp_owner': kwargs['actor'] = replace(wa.owner(), source='mcp')
    if case == 'recipient': kwargs['changes'] = {'chat_id': 'other', 'text': 'Changed'}
    if case == 'blank': kwargs['text'] = ' '
    if case == 'large': kwargs['text'] = 'é' * 8193
    if case == 'stale': original['status_revision'] += 1
    if case == 'decided': assert wa.approve(original['id']).status == 'accepted'
    if case == 'policy': wa.grant('deny')
    if case == 'account': wa.bind(wa.agent, account_ref='b' * 64)
    result = edit(wa, original, **kwargs)
    assert result.status == 'error', result.to_dict()
    assert not wa.detail(original['id'])['superseded_by_id']
    assert wa.job_count() == 0 and not wa.connector.sends
