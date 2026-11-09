"""Pure attention-clock arithmetic shared by the coordinator and worker Todo boards.

``advance`` is a reducer over one agent's stored clock mapping: it receives the stored clock fields, the
current board mode and focus, and at most one genuine event, then returns the complete mapping to persist
plus the delivery advice for the occurrence in progress. It is stdlib-only (no database, HTTP, threading
or service imports) so the runtime can call it under any lock and tests can advance months of synthetic
time without sleeping.

Stored mapping (timestamps are UTC ISO-8601 strings or ``None``):

- ``board_mode``: the board mode already accounted for (``empty``/``normal``/``reassess``).
- ``board_due_at``: next Unfocused board occurrence; ``None`` while Focused or with no unfinished work.
- ``focus_due_at``: next Focused progress occurrence; ``None`` whenever Unfocused.
- ``last_agent_activity_at``/``last_owner_message_at``: monotonic event watermarks.
- ``focus_generation``: the focus generation already accounted for.
- ``reminder_sequence``: delivered occurrences already acknowledged.
- ``blocked_reminded_since``: start of the blocking episode whose detachment nudge was delivered.

The temporary ``delivered_kind`` input key names the occurrence that a ``delivered`` event consumed and is
never returned. ``focus_changed`` announces a focus set, replacement or clear; arriving Unfocused it also
rebuilds the board deadline from ``now`` even if a stale board deadline survived in the mapping. Every
``delivered`` receipt advances ``reminder_sequence`` once, so the runtime owns receipt deduplication.
``due_at`` is the occurrence's deadline and the eligibility flags compare it with ``now``; a future
deadline is still reported so the runtime can schedule it.

Timing rules:

- Unfocused ``normal``: entry and mode switches arm ``max(now + 3m, last_owner + 30m)``. Genuine agent
  activity in the same mode extends to ``max(D, activity + 3m)``; an owner message extends to
  ``max(D, message + 30m)``. A delivered board reminder rearms from the delivery time (3m) subject to
  the owner buffer.
- Unfocused ``reassess``: entry and mode switches arm ``max(now + 30m, last_owner + 30m)``; activity does
  not move it, an owner message still extends it to ``max(D, message + 30m)``, and a delivery rearms 30
  minutes later subject to the same buffer. ``empty`` arms nothing, so Done/Cancelled work cannot keep a
  reminder running.
- Focused: a new generation arms ``focus_started_at + 5m`` and each delivered focus reminder rearms
  ``delivery + 5m``. Activity and owner messages never postpone the hook; chat becomes eligible at
  ``max(due + 3m, last_owner + 30m)``.
- Focused with a blocked workflow episode only exposes the detachment occurrence at
  ``blocked_since + 30m``; ordinary focus and board delivery is suspended, an owner message neither
  resets nor postpones that clock, and a delivered detachment is remembered for that episode only. The
  marker survives an unfocused gap so the same unresolved episode is never nudged twice; clearing the
  episode while Focused, or replacing it with a new start, resets it.

Unfocusing or changing focus invalidates the old focus deadline and rebuilds the board deadline from the
current moment, so accumulated deadlines are never replayed. Older duplicate activity is inert. The
reducer only advises delivery: it never changes focus, resumes a workflow or starts a model turn.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

__all__ = ('advance',)

BOARD_MODES = ('empty', 'normal', 'reassess')
EVENTS = (None, 'agent_activity', 'owner_message', 'delivered', 'focus_changed')
BOARD_INTERVAL = {'normal': timedelta(minutes=3), 'reassess': timedelta(minutes=30)}
BOARD_KIND = {'normal': 'board_normal', 'reassess': 'board_reassess'}
BOARD_KINDS = frozenset(BOARD_KIND.values())
OCCURRENCE_KINDS = BOARD_KINDS | {'focus', 'workflow_detach'}
OWNER_MESSAGE_BUFFER = timedelta(minutes=30)
FOCUS_INTERVAL = timedelta(minutes=5)
FOCUS_CHAT_GRACE = timedelta(minutes=3)
WORKFLOW_DETACH_GRACE = timedelta(minutes=30)
_STORED_TIMESTAMPS = ('board_due_at', 'focus_due_at', 'last_agent_activity_at', 'last_owner_message_at',
    'blocked_reminded_since')


def _instant(value, field):
    """Parse a stored or caller-supplied UTC moment; ``None`` stays ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            raise ValueError(f'{field} must be an ISO-8601 UTC timestamp') from None
    if not isinstance(value, datetime):
        raise ValueError(f'{field} must be a datetime or an ISO-8601 UTC timestamp')
    if value.tzinfo is None:
        raise ValueError(f'{field} must be timezone-aware')
    return value.astimezone(timezone.utc)


def _stamp(value):
    """Return the repository's canonical UTC ISO form, or ``None``."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _count(value, field):
    """Return a non-negative integer counter, defaulting an absent value to zero."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{field} must be a non-negative integer')
    return value


def _previous(state):
    """Normalize the stored mapping without keeping a reference to caller values."""
    if not isinstance(state, Mapping):
        raise TypeError('state must be a mapping of the stored clock fields')
    mode = state.get('board_mode')
    if mode is not None and mode not in BOARD_MODES:
        raise ValueError(f'state board_mode must be one of {BOARD_MODES}')
    previous = {'board_mode': mode,
        'focus_generation': _count(state.get('focus_generation'), 'state focus_generation'),
        'reminder_sequence': _count(state.get('reminder_sequence'), 'state reminder_sequence')}
    previous.update({name: _instant(state.get(name), name) for name in _STORED_TIMESTAMPS})
    return previous


def _arm(mode, moment, owner_at):
    """Arm a board occurrence from a moment, preserving the owner-message buffer."""
    due = moment + BOARD_INTERVAL[mode]
    if owner_at is not None:
        due = max(due, owner_at + OWNER_MESSAGE_BUFFER)
    return due


def advance(state, *, now, mode, focused, focus_generation, focus_started_at=None, blocked_since=None,
        event=None, event_at=None):
    """Return the next stored clock mapping plus the current occurrence's delivery advice.

    ``state`` is the stored mapping (see the module docstring); ``now`` is the current UTC moment and
    ``event_at`` the genuine source or delivery timestamp of ``event`` (defaulting to ``now``). ``mode`` is
    the board mode derived from unfinished board work, ``focused`` the current attention state and
    ``focus_generation`` the caller's focus generation. ``focus_started_at`` is the start of the current
    focus episode and ``blocked_since`` the start of the focused workflow's unresolved blocking episode.
    """
    if mode not in BOARD_MODES:
        raise ValueError(f'mode must be one of {BOARD_MODES}')
    if event not in EVENTS:
        raise ValueError(f'event must be one of {EVENTS}')
    current = _instant(now, 'now')
    if current is None:
        raise ValueError('now is required')
    moment = current if event_at is None else _instant(event_at, 'event_at')
    previous = _previous(state)
    generation = _count(focus_generation, 'focus_generation')
    focus_start = _instant(focus_started_at, 'focus_started_at')
    blocked = _instant(blocked_since, 'blocked_since')
    delivered = None
    if event == 'delivered':
        delivered = state.get('delivered_kind')
        if delivered not in OCCURRENCE_KINDS:
            raise ValueError('delivered requires state delivered_kind to name the occurrence that was delivered')

    # Watermarks only move forwards, so a repeated or late source timestamp cannot extend anything again.
    agent_at = previous['last_agent_activity_at']
    owner_at = previous['last_owner_message_at']
    activity = event == 'agent_activity' and (agent_at is None or moment > agent_at)
    message = event == 'owner_message' and (owner_at is None or moment > owner_at)
    if activity:
        agent_at = moment
    if message:
        owner_at = moment
    sequence = previous['reminder_sequence'] + (1 if event == 'delivered' else 0)

    # A blocking episode only exists while Focused. Its delivery marker belongs to that exact episode and
    # survives an unfocused gap, so refocusing the same unresolved episode cannot nudge a second time;
    # clearing the episode while Focused, or replacing it with a new start, resets the marker.
    episode = blocked if focused else None
    marker = previous['blocked_reminded_since']
    if focused and (episode is None or marker != episode):
        marker = None

    board_due = focus_due = None
    if focused:
        if generation != previous['focus_generation'] or previous['focus_due_at'] is None:
            focus_due = (focus_start or current) + FOCUS_INTERVAL
        else:
            focus_due = previous['focus_due_at']
        if delivered == 'focus' and episode is None:
            focus_due = moment + FOCUS_INTERVAL
        # The board clock is paused while focused; returning Unfocused rebuilds it from the current moment.
    elif mode != 'empty':
        if (previous['board_mode'] != mode or previous['board_due_at'] is None
                or event == 'focus_changed'):
            board_due = _arm(mode, current, owner_at)
        else:
            board_due = previous['board_due_at']
            if activity and mode == 'normal':
                board_due = max(board_due, moment + BOARD_INTERVAL['normal'])
            if message:
                board_due = max(board_due, moment + OWNER_MESSAGE_BUFFER)
            if delivered in BOARD_KINDS:
                board_due = _arm(mode, moment, owner_at)

    if focused and episode is not None:
        if delivered == 'workflow_detach':
            marker = episode
        if marker == episode:
            kind, due_at, chat_not_before = None, None, None
        else:
            kind = 'workflow_detach'
            due_at = episode + WORKFLOW_DETACH_GRACE
            chat_not_before = due_at
    elif focused:
        kind, due_at = 'focus', focus_due
        chat_not_before = None
        if focus_due is not None:
            chat_not_before = focus_due + FOCUS_CHAT_GRACE
            if owner_at is not None:
                chat_not_before = max(chat_not_before, owner_at + OWNER_MESSAGE_BUFFER)
    else:
        kind, due_at = BOARD_KIND.get(mode), board_due
        chat_not_before = board_due

    return {'board_mode': mode,
        'board_due_at': _stamp(board_due),
        'focus_due_at': _stamp(focus_due),
        'last_agent_activity_at': _stamp(agent_at),
        'last_owner_message_at': _stamp(owner_at),
        'focus_generation': generation,
        'reminder_sequence': sequence,
        'blocked_reminded_since': _stamp(marker),
        'kind': kind,
        'due_at': _stamp(due_at),
        'hook_eligible': due_at is not None and current >= due_at,
        'chat_not_before': _stamp(chat_not_before),
        'chat_eligible': chat_not_before is not None and current >= chat_not_before}
