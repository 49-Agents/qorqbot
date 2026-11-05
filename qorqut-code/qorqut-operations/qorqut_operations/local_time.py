"""The installation's operational timezone, selected before the process starts.

Storage and wire timestamps stay UTC instants. Every operational human date,
hour and day bucket converts through this module. Technical zones (cron
schedules, provider wire formats, receipts, per-metric configuration) keep
their own explicitly labelled zones and are not reinterpreted here.
"""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from datetime import date, datetime, timedelta, timezone
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qorqut_operations.contracts import DomainError

TIMEZONE = os.environ.get("QORQUT_TIMEZONE", "UTC")
try:
    ZONE = ZoneInfo(TIMEZONE)
except (ValueError, ZoneInfoNotFoundError) as exc:
    raise ValueError("QORQUT_TIMEZONE must name an installed IANA timezone, for example UTC") from exc
LABEL = TIMEZONE + " time"
REAL_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}))?$")
REAL_TIME_SCHEMA = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}(T\d{2})?$",
                    "description": instruction_text('local-time-optional-occurrence-in-the-installation-timezone-returned')}


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


def local(value, zone=None):
    parsed = instant(value)
    return parsed.astimezone(zone or ZONE) if parsed else None


def local_date(value, zone=None):
    """Operational calendar date of an instant, or None when unknown."""
    parsed = local(value, zone)
    return parsed.date().isoformat() if parsed else None


def local_hour(value, zone=None):
    parsed = local(value, zone)
    return parsed.hour if parsed else None


def today(clock):
    now = clock()
    if isinstance(now, str):
        now = instant(now)
    return now.astimezone(ZONE).date()


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_view(clock):
    """Server instant plus the configured local date/hour it represents."""
    now = clock()
    if isinstance(now, str):
        now = instant(now)
    here = now.astimezone(ZONE)
    return {"timezone": TIMEZONE, "label": LABEL, "server_instant": utc_text(now),
            "local_date": here.date().isoformat(), "local_hour": here.hour, "utc_offset": here.strftime("%z")}


def _exists(wall):
    # Attaching ZoneInfo alone accepts imaginary times. A UTC round trip verifies the wall fields.
    return wall.astimezone(timezone.utc).astimezone(ZONE).replace(tzinfo=None) == wall.replace(tzinfo=None)


def _day_start(local_day):
    """First real minute of a local date, including zones whose DST gap starts at midnight."""
    midnight = datetime(local_day.year, local_day.month, local_day.day, tzinfo=ZONE)
    for minutes in range(24 * 60):
        candidate = midnight + timedelta(minutes=minutes)
        if _exists(candidate):
            return candidate
    raise DomainError("validation_error", "This calendar date does not exist in " + TIMEZONE,
                      field_errors={"real_time": "nonexistent date"})


def parse_real_time(value, clock):
    """Validate an operational day/hour. DST folds select the first occurrence; gaps are refused."""
    match = REAL_TIME.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise DomainError("validation_error", "real_time must be YYYY-MM-DD or YYYY-MM-DDTHH in " + TIMEZONE,
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
    start = (_day_start(local_day) if hour is None else
             datetime(local_day.year, local_day.month, local_day.day, int(hour), tzinfo=ZONE, fold=0))
    if not _exists(start):
        raise DomainError("validation_error", "This local hour does not exist in " + TIMEZONE,
                          field_errors={"real_time": "nonexistent hour"})
    if local_day > here.date() or (hour is not None and start.astimezone(timezone.utc) > now.astimezone(timezone.utc)):
        raise DomainError("validation_error", "real_time cannot be in the future (" + TIMEZONE + ")",
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
    """UTC bounds of one local day, which may be 23 or 25 hours across DST."""
    return utc_text(_day_start(local_day)), utc_text(_day_start(local_day + timedelta(days=1)))
