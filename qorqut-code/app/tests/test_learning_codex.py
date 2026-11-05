import json

import pytest

from bazarlyk.domains.learnings.analysis import ItemError, build_core, validate_item
from test_background_programs import raw_learning


def core(*records):
    return build_core({'source_id': 'agents/founder/sessions/codex-test.jsonl',
                      'content': '\n'.join(json.dumps(row) for row in records)})


def user(text, identity='owner'):
    return {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {
        'type': 'UserMessage', 'id': identity, 'content': [{'type': 'text', 'text': text}]}}}


def response(role, text):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
        'content': [{'type': 'output_text' if role == 'assistant' else 'input_text', 'text': text}]}}


def test_codex_owner_events_not_injected_user_role_or_duplicate_events():
    event = user('Keep the business qualification.')
    result = core({'type': 'session_meta'}, response('user', 'Injected bootstrap instructions'),
                  event, event, response('user', 'Keep the business qualification.'),
                  response('assistant', 'For Project A only.'))
    assert result['stats']['owner_messages'] == 1
    assert 'Injected bootstrap' not in result['markdown']
    assert 'For Project A only.' in result['markdown']


def test_codex_automatic_boundary_and_credentials():
    result = core({'type': 'session_meta'}, user('Use sk-' + 'x' * 30),
                  user('Your inbox has pending updates. Check it.', 'automatic'),
                  response('assistant', 'A background reply.'))
    assert result['stats']['owner_messages'] == 1
    assert 'x' * 30 not in result['markdown']
    assert '[REDACTED CREDENTIAL]' in result['markdown']
    assert '(no owner message)' in result['markdown']


def test_legacy_codex_user_event_and_no_response_item_fallback():
    result = core({'type': 'session_meta'}, {'type': 'event_msg', 'payload': {
        'type': 'user_message', 'message': 'Always keep quotations.'}},
        response('user', 'Injected context'))
    assert result['stats']['owner_messages'] == 1
    assert 'Injected context' not in result['markdown']
    assert core({'type': 'session_meta'}, response('user', 'Unknown origin'))['stats']['owner_messages'] == 0


def test_request_cannot_confirm_the_agent_response_that_follows_it():
    result = core({'type': 'session_meta'}, user('Please check the portfolio.'),
                  response('assistant', 'There are four businesses.'), user('Yes, four businesses.', 'later'))
    item = {**raw_learning(), 'said_by': 'agent_confirmed_by_owner', 'evidence': {
        'turns': ['T1'], 'owner_quote': 'Please check the portfolio.', 'agent_quote': 'There are four businesses.'}}
    with pytest.raises(ItemError, match='later turn'):
        validate_item(item, result['markdown'])
    item['evidence'].update(turns=['T1', 'T2'], owner_quote='Yes, four businesses.')
    validate_item(item, result['markdown'])
    item['evidence'].update(turns=['T1'])
    with pytest.raises(ItemError, match='cited turns'):
        validate_item(item, result['markdown'])
