"""The one operational human time convention: San Juan, Puerto Rico.

Storage and wire timestamps stay UTC instants. Every operational human date,
hour and day bucket converts through this module. Technical zones (cron
schedules, provider wire formats, receipts, per-metric configuration) keep
their own explicitly labelled zones and are not reinterpreted here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from qorqut_operations.contracts import DomainError

TIMEZONE = "America/Puerto_Rico"
ZONE = ZoneInfo(TIMEZONE)
LABEL = "San Juan time"
REAL_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}))?$")
REAL_TIME_SCHEMA = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}(T\d{2})?$",
                    "description": "Optional San Juan (America/Puerto_Rico) occurrence: YYYY-MM-DD or YYYY-MM-DDTHH "
                                   "(24-hour). No minutes, seconds or offsets. Omit for now."}


def instant(value):
    """Parse a stored UTC/offset timestamp; naive or invalid values return None."""
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else None


def local(value):
    parsed = instant(value)
    return parsed.astimezone(ZONE) if parsed else None


def local_date(value):
    """San Juan calendar date of a timestamp instant, or None when unknown."""
    parsed = local(value)
    return parsed.date().isoformat() if parsed else None


def local_hour(value):
    parsed = local(value)
    return parsed.hour if parsed else None


def today(clock):
    now = clock()
    if isinstance(now, str):
        now = instant(now)
    return now.astimezone(ZONE).date()


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_view(clock):
    """Server instant plus the San Juan local date/hour it represents."""
    now = clock()
    if isinstance(now, str):
        now = instant(now)
    here = now.astimezone(ZONE)
    return {"timezone": TIMEZONE, "label": LABEL, "server_instant": utc_text(now),
            "local_date": here.date().isoformat(), "local_hour": here.hour, "utc_offset": here.strftime("%z")}


def parse_real_time(value, clock):
    """Validate a San Juan day/hour occurrence; it may not be in the future."""
    match = REAL_TIME.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise DomainError("validation_error", "real_time must be YYYY-MM-DD or YYYY-MM-DDTHH in San Juan time",
                          field_errors={"real_time": "invalid format"})
    year, month, day_, hour = match.groups()
    try:
        local_day = date(int(year), int(month), int(day_))
    except ValueError as exc:
        raise DomainError("validation_error", "real_time is not a valid calendar date",
                          field_errors={"real_time": "invalid date"}) from exc
    if hour is not None and not 0 <= int(hour) <= 23:
        raise DomainError("validation_error", "real_time hour must be 00-23", field_errors={"real_time": "invalid hour"})
    now = clock()
    if isinstance(now, str):
        now = instant(now)
    here = now.astimezone(ZONE)
    start = datetime(local_day.year, local_day.month, local_day.day, int(hour or 0), tzinfo=ZONE)
    if local_day > here.date() or (hour is not None and start > here):
        raise DomainError("validation_error", "real_time cannot be in the future (San Juan time)",
                          field_errors={"real_time": "future"})
    return {"real_date": local_day.isoformat(), "real_hour": None if hour is None else int(hour),
            "time_precision": "hour" if hour is not None else "day", "time_zone": TIMEZONE,
            # Index anchor only: the start of that local day/hour, never shown as an exact time.
            "occurred_at": utc_text(start)}


def recorded_time(recorded_at):
    """Occurrence fields for an action that happened at its recording instant."""
    here = local(recorded_at)
    if here is None:
        raise DomainError("validation_error", "Occurrence requires a timestamp with an explicit offset")
    return {"real_date": here.date().isoformat(), "real_hour": here.hour, "time_precision": "instant",
            "time_zone": TIMEZONE, "occurred_at": recorded_at}


def day_bounds(local_day):
    """UTC instants for the start and end of one San Juan calendar day."""
    start = datetime(local_day.year, local_day.month, local_day.day, tzinfo=ZONE)
    return utc_text(start), utc_text(start + timedelta(days=1))
