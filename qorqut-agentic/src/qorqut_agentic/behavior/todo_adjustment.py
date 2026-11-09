"""Daily Todo review eligibility, evaluated only by the existing inactivity pass."""
from datetime import datetime, timedelta, timezone

INTERVAL = timedelta(hours=24)
IDLE = timedelta(minutes=15)
OWNER_BUFFER = timedelta(minutes=15)


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Todo adjustment timestamps must have a timezone')
    return parsed.astimezone(timezone.utc)


def due(now, *, session_started_at, last_activity_at=None, last_owner_message_at=None, last_nudge_at=None):
    """Elapsed time, never calendar days; a new or active session must first become idle."""
    deadlines = [instant(session_started_at) + IDLE]
    if last_activity_at:
        deadlines.append(instant(last_activity_at) + IDLE)
    if last_owner_message_at:
        deadlines.append(instant(last_owner_message_at) + OWNER_BUFFER)
    if last_nudge_at:
        deadlines.append(instant(last_nudge_at) + INTERVAL)
    return instant(now) >= max(deadlines)
