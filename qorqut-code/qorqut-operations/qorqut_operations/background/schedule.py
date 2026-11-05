"""Preserved tested cron and UTC/DST slot semantics from the routine utility."""

from datetime import datetime, timedelta, timezone
import functools
import re
from zoneinfo import ZoneInfo

UTC = timezone.utc

class RoutineError(ValueError):
    pass

def now_utc():
    return datetime.now(UTC)

def stamp(instant=None):
    return (instant or now_utc()).astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

def instant(value):
    d = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if d.tzinfo is None:
        raise RoutineError("timestamp must include a timezone")
    return d.astimezone(UTC)

@functools.lru_cache(maxsize=256)
def parse_cron(when):
    if not isinstance(when, str) or len(when.split()) != 5:
        raise RoutineError("when must be a five-field cron expression")
    fields = []
    for field, (low, high) in zip(when.split(), ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))):
        values = set()
        for term in field.split(","):
            match = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", term)
            if not match:
                raise RoutineError(f"invalid cron field: {field}")
            base, step = match.groups()
            if step and base != "*" and "-" not in base:
                raise RoutineError("steps require * or a range")
            step = int(step or 1)
            a, b = (low, high) if base == "*" else (map(int, base.split("-")) if "-" in base else (int(base), int(base)))
            if step < 1 or not low <= a <= b <= high:
                raise RoutineError(f"cron field out of range: {field}")
            values.update(range(a, b + 1, step))
        if high == 7:
            values = {v % 7 for v in values}
        fields.append(frozenset(values))
    return tuple(fields), tuple(f.startswith("*") for f in when.split())

def _matches(parsed, local):
    (minute, hour, dom, month, dow), wild = parsed
    day, weekday = local.day in dom, (local.weekday() + 1) % 7 in dow
    day_ok = day and weekday if wild[2] or wild[4] else day or weekday
    return local.minute in minute and local.hour in hour and local.month in month and day_ok

def matches(when, local):
    return _matches(parse_cron(when), local)

def zone(tz):
    if isinstance(tz, str):
        try:
            return ZoneInfo(tz)
        except (KeyError, ValueError) as exc:
            raise RoutineError(f"invalid timezone: {tz}") from exc
    return tz

def _slot(when, at, tz, direction):
    parsed, tz = parse_cron(when), zone(tz)
    current = instant(at).replace(second=0, microsecond=0)
    if direction > 0:
        current += timedelta(minutes=1)
    # Eight years includes Feb 29 across the non-leap century (2096 -> 2104).
    # UTC traversal is intentional: nonexistent local times never match and
    # repeated wall times remain visible to the scheduler's deduplication.
    for _ in range(8 * 366 * 24 * 60):
        local = current.astimezone(tz)
        if _matches(parsed, local):
            return current
        current += timedelta(minutes=direction)
    raise RoutineError("cron has no matching slot within eight years")

def next_slot(when, after, tz):
    return _slot(when, after, tz, 1)

def prev_slot(when, before, tz):
    return _slot(when, before, tz, -1)
