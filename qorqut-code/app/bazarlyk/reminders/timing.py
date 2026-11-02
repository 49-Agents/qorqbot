"""Reminder time handling: one UTC stamp format, and the Routines calendar reused for every recurrence.

Nothing here is a second calendar. Parsing, time zones, slot search and the latest-slot rule come from
``qorqut_operations.background`` (``parse_cron``/``zone``/``next_slot`` and ``RoutineScheduler.due``); this module only
skips repeated daylight-saving wall times when looking *forward*, exactly as ``RoutineScheduler.due`` does looking back."""
from datetime import datetime, timedelta, timezone
import functools

from qorqut_operations.background.routines import RoutineScheduler
from qorqut_operations.background.schedule import RoutineError, next_slot, parse_cron, zone
from qorqut_operations.contracts import DomainError

UTC = timezone.utc


def stamp(value):
    """The one stored timestamp form. Fixed width with microseconds, so text order is time order."""
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_at(value, label="Reminder time"):
    """An ISO timestamp that carries its own offset, as an aware UTC datetime."""
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError()
        return when.astimezone(UTC)
    except (ValueError, OverflowError, AttributeError):
        raise DomainError("validation_error", f"{label} must include a timezone offset") from None


def parse_stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def floor_minute(value):
    return value.astimezone(UTC).replace(second=0, microsecond=0)


def normalize_recurrence(recurrence):
    """Validate one ``{schedule, timezone}`` pair through the Routines parser and return its canonical form."""
    try:
        schedule = " ".join(recurrence["schedule"].split())
        name = recurrence["timezone"]
        parse_cron(schedule)
        zone(name)
    except (RoutineError, KeyError, TypeError, AttributeError) as error:
        raise DomainError("validation_error", f"Invalid recurrence: {error}") from None
    return schedule, name


@functools.lru_cache(maxsize=128)
def _first_slot_after(schedule, name, minute):
    # Slot search walks minute by minute (cost grows with the gap to the next slot), so the answer for one
    # (calendar, minute) is kept between a request's prepare and apply and across reconcile passes.
    tz = zone(name)
    when = datetime.fromisoformat(minute.replace("Z", "+00:00"))
    slot = next_slot(schedule, when, tz)
    while slot.astimezone(tz).fold:
        slot = next_slot(schedule, slot, tz)
    return stamp(slot)


def first_slot_after(schedule, name, after):
    """The first calendar slot strictly after ``after``, skipping a repeated (fold) wall time."""
    try:
        return parse_stamp(_first_slot_after(schedule, name, stamp(floor_minute(after))))
    except RoutineError as error:
        raise DomainError("validation_error", f"Invalid recurrence: {error}") from None


def latest_slot(schedule, name, now):
    """The latest calendar slot at or before ``now``: the Routines scheduler's own rule."""
    try:
        return RoutineScheduler.due({"schedule": schedule, "timezone": name}, floor_minute(now))
    except RoutineError as error:
        raise DomainError("validation_error", f"Invalid recurrence: {error}") from None
