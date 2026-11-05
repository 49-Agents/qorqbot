"""Deterministic boundary tests for the pure Attention clock; no sleeps, storage or provider calls."""

from copy import deepcopy
from datetime import datetime, timezone

from bazarlyk.agents import attention_clock

STATE_KEYS = {'board_mode', 'board_due_at', 'focus_due_at', 'last_agent_activity_at',
    'last_owner_message_at', 'focus_generation', 'reminder_sequence', 'blocked_reminded_since', 'kind',
    'due_at', 'hook_eligible', 'chat_not_before', 'chat_eligible'}


def at(hour, minute=0, second=0):
    return datetime(2026, 10, 29, hour, minute, second, tzinfo=timezone.utc)


def stored(**values):
    """The persisted clock mapping an isolated synthetic agent starts from."""
    state = {'board_mode': None, 'board_due_at': None, 'focus_due_at': None,
        'last_agent_activity_at': None, 'last_owner_message_at': None, 'focus_generation': 0,
        'reminder_sequence': 0, 'blocked_reminded_since': None}
    state.update(values)
    return state


def instant(value):
    assert value is not None, 'expected a stored timestamp'
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def expect(kind, text, call):
    try:
        call()
    except kind as error:
        assert text in str(error), str(error)
    else:
        raise AssertionError(f'expected {kind.__name__} containing {text!r}')


def test_unfocused_normal_entry_arms_fifteen_minutes_or_the_owner_buffer():
    entry = attention_clock.advance(stored(), now=at(10), mode='normal', focused=False, focus_generation=0)
    assert entry['board_mode'] == 'normal'
    assert entry['board_due_at'] == '2026-10-29T10:15:00.000000Z'
    assert entry['chat_not_before'] == '2026-10-29T10:15:00.000000Z'
    assert entry['kind'] == 'board_normal'
    assert entry['hook_eligible'] is False and entry['chat_eligible'] is False
    assert entry['focus_due_at'] is None and entry['reminder_sequence'] == 0
    buffered = attention_clock.advance(stored(last_owner_message_at='2026-10-29T09:50:00Z'), now=at(10),
        mode='normal', focused=False, focus_generation=0)
    assert instant(buffered['board_due_at']) == at(10, 20)
    owner_now = attention_clock.advance(stored(last_owner_message_at='2026-10-29T10:00:00Z'), now=at(10),
        mode='normal', focused=False, focus_generation=0)
    assert instant(owner_now['board_due_at']) == at(10, 30)


def test_board_deadline_is_kept_until_due_and_both_routes_are_eligible_at_the_boundary():
    armed = stored(board_mode='normal', board_due_at='2026-10-29T10:15:00Z')
    early = attention_clock.advance(armed, now=at(10, 14, 59), mode='normal', focused=False, focus_generation=0)
    assert instant(early['board_due_at']) == at(10, 15)
    assert early['hook_eligible'] is False and early['chat_eligible'] is False
    on_time = attention_clock.advance(armed, now=at(10, 15), mode='normal', focused=False, focus_generation=0)
    assert on_time['hook_eligible'] is True and on_time['chat_eligible'] is True
    assert on_time['kind'] == 'board_normal' and instant(on_time['due_at']) == at(10, 15)


def test_unfocused_normal_decision_examples():
    state = stored(board_mode='normal', board_due_at='2026-10-29T10:30:00Z',
        last_owner_message_at='2026-10-29T10:00:00Z')
    replied = attention_clock.advance(state, now=at(10, 5), mode='normal', focused=False, focus_generation=0,
        event='agent_activity', event_at=at(10, 5))
    assert instant(replied['board_due_at']) == at(10, 30)
    assert instant(replied['last_agent_activity_at']) == at(10, 5)
    acted = attention_clock.advance(replied, now=at(10, 20), mode='normal', focused=False, focus_generation=0,
        event='agent_activity', event_at=at(10, 20))
    assert instant(acted['board_due_at']) == at(10, 35)
    messaged = attention_clock.advance(acted, now=at(10, 25), mode='normal', focused=False, focus_generation=0,
        event='owner_message', event_at=at(10, 25))
    assert instant(messaged['board_due_at']) == at(10, 55)
    assert instant(messaged['last_owner_message_at']) == at(10, 25)
    defaulted = attention_clock.advance(acted, now=at(10, 25), mode='normal', focused=False,
        focus_generation=0, event='owner_message')
    assert instant(defaulted['board_due_at']) == at(10, 55)


def test_unfocused_reassess_is_a_fixed_four_hours_with_the_owner_buffer():
    entry = attention_clock.advance(stored(last_owner_message_at='2026-10-29T09:00:00Z'), now=at(10),
        mode='reassess', focused=False, focus_generation=0)
    assert instant(entry['board_due_at']) == at(14)
    assert entry['kind'] == 'board_reassess'
    worked = attention_clock.advance(entry, now=at(11), mode='reassess', focused=False, focus_generation=0,
        event='agent_activity', event_at=at(11))
    assert instant(worked['board_due_at']) == at(14)
    assert instant(worked['last_agent_activity_at']) == at(11)
    messaged = attention_clock.advance(worked, now=at(13, 50), mode='reassess', focused=False,
        focus_generation=0, event='owner_message', event_at=at(13, 50))
    assert instant(messaged['board_due_at']) == at(14, 20)
    early = attention_clock.advance(messaged, now=at(14, 19, 59), mode='reassess', focused=False,
        focus_generation=0)
    assert early['hook_eligible'] is False and early['chat_eligible'] is False
    on_time = attention_clock.advance(messaged, now=at(14, 20), mode='reassess', focused=False,
        focus_generation=0)
    assert on_time['hook_eligible'] is True and on_time['chat_eligible'] is True


def test_actionable_mode_switch_discards_the_old_deadline():
    switching = attention_clock.advance(stored(board_mode='reassess',
        board_due_at='2026-10-29T14:00:00Z', last_owner_message_at='2026-10-29T09:00:00Z'),
        now=at(11, 30), mode='normal', focused=False, focus_generation=0)
    assert instant(switching['board_due_at']) == at(11, 45)
    assert switching['board_mode'] == 'normal' and switching['kind'] == 'board_normal'
    falling = attention_clock.advance(stored(board_mode='normal',
        board_due_at='2026-10-29T10:30:00Z', last_owner_message_at='2026-10-29T10:20:00Z'),
        now=at(10, 25), mode='reassess', focused=False, focus_generation=0)
    assert instant(falling['board_due_at']) == at(14, 25)


def test_empty_board_never_arms_a_work_reminder():
    idle = attention_clock.advance(stored(board_mode='normal', board_due_at='2026-10-29T10:15:00Z'),
        now=at(10, 16), mode='empty', focused=False, focus_generation=0)
    assert idle['board_due_at'] is None and idle['kind'] is None and idle['due_at'] is None
    assert idle['hook_eligible'] is False and idle['chat_eligible'] is False
    active = attention_clock.advance(stored(board_mode='normal', board_due_at='2026-10-29T10:15:00Z'),
        now=at(10, 16), mode='empty', focused=False, focus_generation=0, event='agent_activity',
        event_at=at(10, 16))
    assert active['kind'] is None and active['board_due_at'] is None
    delivered = attention_clock.advance(stored(board_mode='normal',
        board_due_at='2026-10-29T10:15:00Z', reminder_sequence=4, delivered_kind='board_normal'),
        now=at(10, 16), mode='empty', focused=False, focus_generation=0, event='delivered')
    assert delivered['kind'] is None and delivered['board_due_at'] is None
    assert delivered['reminder_sequence'] == 5


def test_board_delivery_rearms_from_the_delivery_subject_to_the_owner_buffer():
    normal = attention_clock.advance(stored(board_mode='normal', board_due_at='2026-10-29T10:15:00Z',
        last_owner_message_at='2026-10-29T10:00:00Z', reminder_sequence=2, delivered_kind='board_normal'),
        now=at(10, 16), mode='normal', focused=False, focus_generation=0, event='delivered')
    assert instant(normal['board_due_at']) == at(10, 31)
    assert normal['reminder_sequence'] == 3
    assert normal['kind'] == 'board_normal' and instant(normal['due_at']) == at(10, 31)
    buffered = attention_clock.advance(stored(board_mode='normal', board_due_at='2026-10-29T10:15:00Z',
        last_owner_message_at='2026-10-29T10:10:00Z', delivered_kind='board_normal'), now=at(10, 16),
        mode='normal', focused=False, focus_generation=0, event='delivered')
    assert instant(buffered['board_due_at']) == at(10, 40)
    reassess = attention_clock.advance(stored(board_mode='reassess',
        board_due_at='2026-10-29T14:00:00Z', last_owner_message_at='2026-10-29T09:00:00Z',
        delivered_kind='board_reassess'), now=at(14, 1), mode='reassess', focused=False, focus_generation=0,
        event='delivered')
    assert instant(reassess['board_due_at']) == at(18, 1)
    stale = attention_clock.advance(stored(board_mode='normal', board_due_at='2026-10-29T10:15:00Z',
        delivered_kind='focus'), now=at(10, 16), mode='normal', focused=False, focus_generation=0,
        event='delivered')
    assert instant(stale['board_due_at']) == at(10, 15)
    assert stale['reminder_sequence'] == 1


def test_repeated_or_late_activity_never_extends_again():
    state = stored(board_mode='normal', board_due_at='2026-10-29T10:55:00Z',
        last_agent_activity_at='2026-10-29T10:20:00Z', last_owner_message_at='2026-10-29T10:25:00Z')
    repeated = attention_clock.advance(state, now=at(10, 30), mode='normal', focused=False, focus_generation=0,
        event='agent_activity', event_at=at(10, 20))
    assert instant(repeated['board_due_at']) == at(10, 55)
    assert instant(repeated['last_agent_activity_at']) == at(10, 20)
    late = attention_clock.advance(state, now=at(10, 30), mode='normal', focused=False, focus_generation=0,
        event='agent_activity', event_at=at(10, 10))
    assert instant(late['board_due_at']) == at(10, 55)
    assert instant(late['last_agent_activity_at']) == at(10, 20)
    repeated_owner = attention_clock.advance(state, now=at(10, 30), mode='normal', focused=False,
        focus_generation=0, event='owner_message', event_at=at(10, 25))
    assert instant(repeated_owner['board_due_at']) == at(10, 55)
    older_owner = attention_clock.advance(state, now=at(10, 30), mode='normal', focused=False,
        focus_generation=0, event='owner_message', event_at=at(10, 10))
    assert instant(older_owner['board_due_at']) == at(10, 55)
    assert instant(older_owner['last_owner_message_at']) == at(10, 25)
    newer = attention_clock.advance(state, now=at(11), mode='normal', focused=False, focus_generation=0,
        event='agent_activity', event_at=at(11))
    assert instant(newer['board_due_at']) == at(11, 15)


def test_focus_entry_arms_ten_minutes_and_delays_only_chat():
    entry = attention_clock.advance(stored(), now=at(10), mode='normal', focused=True, focus_generation=1,
        focus_started_at=at(10))
    assert instant(entry['focus_due_at']) == at(10, 10)
    assert entry['board_due_at'] is None
    assert entry['kind'] == 'focus' and instant(entry['due_at']) == at(10, 10)
    assert entry['hook_eligible'] is False and entry['chat_eligible'] is False
    assert instant(entry['chat_not_before']) == at(10, 13)
    early = attention_clock.advance(entry, now=at(10, 9, 59), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10))
    assert early['hook_eligible'] is False and early['chat_eligible'] is False
    due = attention_clock.advance(entry, now=at(10, 10), mode='normal', focused=True, focus_generation=1,
        focus_started_at=at(10))
    assert due['hook_eligible'] is True and due['chat_eligible'] is False
    chat = attention_clock.advance(entry, now=at(10, 13), mode='normal', focused=True, focus_generation=1,
        focus_started_at=at(10))
    assert chat['hook_eligible'] is True and chat['chat_eligible'] is True
    buffered = attention_clock.advance(stored(last_owner_message_at='2026-10-29T09:50:00Z'), now=at(10),
        mode='normal', focused=True, focus_generation=1, focus_started_at=at(10))
    assert instant(buffered['focus_due_at']) == at(10, 10)
    assert instant(buffered['chat_not_before']) == at(10, 20)
    without_start = attention_clock.advance(stored(), now=at(10), mode='normal', focused=True,
        focus_generation=3)
    assert instant(without_start['focus_due_at']) == at(10, 10)


def test_focus_activity_and_owner_messages_never_postpone_the_hook():
    state = stored(board_mode='normal', focus_due_at='2026-10-29T10:10:00Z', focus_generation=1,
        last_owner_message_at='2026-10-29T09:00:00Z')
    acted = attention_clock.advance(state, now=at(10, 20), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), event='agent_activity', event_at=at(10, 20))
    assert instant(acted['focus_due_at']) == at(10, 10)
    assert instant(acted['last_agent_activity_at']) == at(10, 20)
    assert acted['hook_eligible'] is True and acted['chat_eligible'] is True
    assert instant(acted['chat_not_before']) == at(10, 13)
    messaged = attention_clock.advance(acted, now=at(10, 25), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), event='owner_message', event_at=at(10, 25))
    assert instant(messaged['focus_due_at']) == at(10, 10)
    assert instant(messaged['chat_not_before']) == at(10, 55)
    assert messaged['hook_eligible'] is True and messaged['chat_eligible'] is False


def test_focus_delivery_rearms_ten_minutes_after_the_actual_delivery():
    state = stored(board_mode='normal', focus_due_at='2026-10-29T10:10:00Z', focus_generation=1,
        reminder_sequence=5, delivered_kind='focus')
    delivered = attention_clock.advance(state, now=at(10, 12), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), event='delivered')
    assert instant(delivered['focus_due_at']) == at(10, 22)
    assert delivered['reminder_sequence'] == 6
    assert delivered['kind'] == 'focus' and instant(delivered['due_at']) == at(10, 22)
    late = attention_clock.advance(state, now=at(10, 14), mode='normal', focused=True, focus_generation=1,
        focus_started_at=at(10), event='delivered', event_at=at(10, 14))
    assert instant(late['focus_due_at']) == at(10, 24)


def test_unfocus_clears_the_focus_clock_and_rebuilds_the_board_from_now():
    focused = stored(board_mode='normal', focus_due_at='2026-10-29T10:10:00Z', focus_generation=2,
        last_owner_message_at='2026-10-29T09:00:00Z')
    cleared = attention_clock.advance(focused, now=at(12), mode='normal', focused=False, focus_generation=2,
        event='focus_changed')
    assert cleared['focus_due_at'] is None
    assert instant(cleared['board_due_at']) == at(12, 15)
    assert cleared['kind'] == 'board_normal'
    removed = attention_clock.advance(focused, now=at(12), mode='reassess', focused=False,
        focus_generation=2)
    assert instant(removed['board_due_at']) == at(16)
    assert removed['kind'] == 'board_reassess'
    silent = attention_clock.advance(focused, now=at(12), mode='empty', focused=False, focus_generation=2)
    assert silent['board_due_at'] is None and silent['kind'] is None


def test_changed_focus_generation_replaces_the_old_focus_deadline():
    delivered = stored(board_mode='normal', focus_due_at='2026-10-29T10:22:00Z', focus_generation=1)
    changed = attention_clock.advance(delivered, now=at(11), mode='normal', focused=True,
        focus_generation=2, focus_started_at=at(11), event='focus_changed')
    assert instant(changed['focus_due_at']) == at(11, 10)
    assert changed['focus_generation'] == 2
    without_start = attention_clock.advance(delivered, now=at(11), mode='normal', focused=True,
        focus_generation=2)
    assert instant(without_start['focus_due_at']) == at(11, 10)
    reaffirmed = attention_clock.advance(delivered, now=at(10, 20), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), event='focus_changed')
    assert instant(reaffirmed['focus_due_at']) == at(10, 22)


def test_blocked_episode_suspends_ordinary_reminders_and_arms_detachment_at_thirty_minutes():
    focused = stored(board_mode='normal', focus_due_at='2026-10-29T10:10:00Z', focus_generation=1,
        last_owner_message_at='2026-10-29T09:00:00Z')
    blocked = attention_clock.advance(focused, now=at(10, 20), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), blocked_since=at(10, 5))
    assert blocked['kind'] == 'workflow_detach'
    assert instant(blocked['due_at']) == at(10, 35)
    assert blocked['hook_eligible'] is False and blocked['chat_eligible'] is False
    assert blocked['board_due_at'] is None
    assert instant(blocked['focus_due_at']) == at(10, 10)
    assert blocked['blocked_reminded_since'] is None
    early = attention_clock.advance(blocked, now=at(10, 34, 59), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), blocked_since=at(10, 5))
    assert early['hook_eligible'] is False and early['chat_eligible'] is False
    expiry = attention_clock.advance(blocked, now=at(10, 35), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), blocked_since=at(10, 5))
    assert expiry['hook_eligible'] is True and expiry['chat_eligible'] is True
    assert instant(expiry['chat_not_before']) == at(10, 35)
    messaged = attention_clock.advance(expiry, now=at(10, 40), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), blocked_since=at(10, 5), event='owner_message',
        event_at=at(10, 40))
    assert instant(messaged['due_at']) == at(10, 35)
    assert messaged['chat_eligible'] is True and messaged['hook_eligible'] is True
    repeated = attention_clock.advance(expiry, now=at(11), mode='normal', focused=True, focus_generation=1,
        focus_started_at=at(10), blocked_since=at(10, 5))
    assert instant(repeated['due_at']) == at(10, 35)


def test_delivered_detachment_is_suppressed_for_that_episode_only():
    state = stored(board_mode='normal', focus_due_at='2026-10-29T10:10:00Z', focus_generation=1,
        reminder_sequence=7, delivered_kind='workflow_detach')
    delivered = attention_clock.advance(state, now=at(10, 36), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), blocked_since=at(10, 5), event='delivered')
    assert delivered['blocked_reminded_since'] == '2026-10-29T10:05:00.000000Z'
    assert delivered['kind'] is None and delivered['due_at'] is None
    assert delivered['hook_eligible'] is False and delivered['chat_eligible'] is False
    assert delivered['reminder_sequence'] == 8
    later = attention_clock.advance(delivered, now=at(11, 30), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), blocked_since=at(10, 5))
    assert later['kind'] is None and later['due_at'] is None
    assert later['blocked_reminded_since'] == '2026-10-29T10:05:00.000000Z'
    detached = attention_clock.advance(later, now=at(11, 31), mode='normal', focused=False,
        focus_generation=1)
    assert detached['kind'] == 'board_normal' and instant(detached['board_due_at']) == at(11, 46)
    assert detached['blocked_reminded_since'] == '2026-10-29T10:05:00.000000Z'
    refocused = attention_clock.advance(detached, now=at(11, 45), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(11, 45), blocked_since=at(10, 5))
    assert refocused['kind'] is None and refocused['due_at'] is None
    assert instant(refocused['focus_due_at']) == at(11, 55)
    cleared = attention_clock.advance(refocused, now=at(11, 46), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(11, 45), blocked_since=None)
    assert cleared['blocked_reminded_since'] is None
    assert cleared['kind'] == 'focus' and instant(cleared['due_at']) == at(11, 55)
    replaced = attention_clock.advance(delivered, now=at(12), mode='normal', focused=True,
        focus_generation=1, focus_started_at=at(10), blocked_since=at(12))
    assert replaced['blocked_reminded_since'] is None
    assert replaced['kind'] == 'workflow_detach' and instant(replaced['due_at']) == at(12, 30)
    assert replaced['hook_eligible'] is False and replaced['chat_eligible'] is False


def test_unfocused_state_ignores_a_blocked_episode():
    result = attention_clock.advance(stored(board_mode='normal',
        blocked_reminded_since='2026-10-29T09:00:00Z'), now=at(10, 20), mode='normal', focused=False,
        focus_generation=0, blocked_since=at(10, 5))
    assert result['kind'] == 'board_normal'
    assert instant(result['board_due_at']) == at(10, 35)
    # The receipt marker is an episode delivery marker, not a clock: it is retained across the gap.
    assert result['blocked_reminded_since'] == '2026-10-29T09:00:00.000000Z'


def test_advance_returns_new_state_and_never_persists_delivered_kind():
    original = stored(board_mode='normal', board_due_at='2026-10-29T10:15:00Z',
        last_owner_message_at='2026-10-29T10:00:00Z', reminder_sequence=3, delivered_kind='board_normal')
    snapshot = deepcopy(original)
    result = attention_clock.advance(original, now=at(10, 16), mode='normal', focused=False,
        focus_generation=0, event='delivered')
    assert original == snapshot
    assert result is not original and set(result) == STATE_KEYS
    assert 'delivered_kind' not in result
    assert attention_clock.advance(original, now=at(10, 16), mode='normal', focused=False,
        focus_generation=0, event='delivered') == result
    quiet = attention_clock.advance(original, now=at(10, 16), mode='normal', focused=False,
        focus_generation=0)
    assert 'delivered_kind' not in quiet
    assert instant(quiet['board_due_at']) == at(10, 15) and quiet['reminder_sequence'] == 3


def test_iso_inputs_normalize_and_invalid_inputs_raise():
    string_now = attention_clock.advance(stored(), now='2026-10-29T10:00:00Z', mode='normal',
        focused=False, focus_generation=0)
    assert string_now['board_due_at'] == '2026-10-29T10:15:00.000000Z'
    offset = attention_clock.advance(stored(), now='2026-10-29T12:00:00+02:00', mode='normal',
        focused=False, focus_generation=0)
    assert offset['board_due_at'] == string_now['board_due_at']
    expect(ValueError, 'timezone-aware', lambda: attention_clock.advance(stored(),
        now=datetime(2026, 10, 29, 10), mode='normal', focused=False, focus_generation=0))
    expect(ValueError, 'mode', lambda: attention_clock.advance(stored(), now=at(10), mode='blocked',
        focused=False, focus_generation=0))
    expect(ValueError, 'event', lambda: attention_clock.advance(stored(), now=at(10), mode='normal',
        focused=False, focus_generation=0, event='heartbeat'))
    expect(ValueError, 'delivered', lambda: attention_clock.advance(stored(), now=at(10), mode='normal',
        focused=False, focus_generation=0, event='delivered'))
    expect(ValueError, 'delivered', lambda: attention_clock.advance(stored(delivered_kind='detach'),
        now=at(10), mode='normal', focused=False, focus_generation=0, event='delivered'))
    expect(ValueError, 'focus_generation', lambda: attention_clock.advance(stored(), now=at(10),
        mode='normal', focused=False, focus_generation=-1))
    expect(ValueError, 'focus_generation', lambda: attention_clock.advance(stored(focus_generation='one'),
        now=at(10), mode='normal', focused=False, focus_generation=0))
    expect(ValueError, 'board_mode', lambda: attention_clock.advance(stored(board_mode='blocked'),
        now=at(10), mode='normal', focused=False, focus_generation=0))
    expect(TypeError, 'mapping', lambda: attention_clock.advance(None, now=at(10), mode='normal',
        focused=False, focus_generation=0))