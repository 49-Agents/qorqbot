"""The common calendar-provider interface and event helpers.

Every calendar connection type is an adapter class supplied by a selected extension through the host seam (``Extension.calendar_providers``):
Google Calendar by ``qorqut_google_calendar``, Fastmail CalDAV by ``qorqut_fastmail``. An adapter subclasses ``Provider`` and uses the
validation and parsing helpers here; the host owns permissions, Signoff/idempotency handling and the operation schemas."""

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from qorqut_operations.contracts import DomainError


def invalid(message):
    raise DomainError("validation_error", message)


def instant(value):
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result
    except (ValueError, TypeError):
        invalid("Use an ISO date or a date-time with a UTC offset")


def validate_event(fields):
    if fields.get("timezone"):
        try:
            ZoneInfo(fields["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            invalid("Use an IANA timezone such as America/New_York")
    if "start" in fields and "end" in fields:
        start, end = instant(fields["start"]), instant(fields["end"])
        if type(start) is not type(end) or end <= start:
            invalid("Event end must follow start, with matching date/date-time types")
        if fields.get("recurrence") and isinstance(start, datetime) and not fields.get("timezone"):
            invalid("Timed recurring events require a timezone")
    for line in fields.get("recurrence", []):
        if "\n" in line or "\r" in line or line.split(":", 1)[0].split(";", 1)[0] not in {"RRULE", "EXDATE", "RDATE"}:
            invalid("Recurrence accepts RRULE, EXDATE and RDATE lines")


def window(args):
    def at(value):
        parsed = instant(value)
        return parsed if isinstance(parsed, datetime) else datetime.combine(parsed, datetime.min.time(), timezone.utc)
    start, end = at(args["start"]), at(args["end"])
    if not start < end <= start + timedelta(days=366):
        invalid("List events in a range of up to 366 days")
    return start, end


class Provider:
    def __init__(self, identity, config, *, transport=None):
        self.identity, self.config = identity, config
        self.client = httpx.Client(timeout=30, transport=transport, follow_redirects=False)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def request(self, method, url, *, allowed=(), **kwargs):
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise DomainError("provider_unavailable", "Calendar provider did not respond. Read current state before retrying a change with the same request key.") from None
        if response.status_code not in allowed and not 200 <= response.status_code < 300:
            code = {401: "calendar_auth_required", 403: "forbidden", 404: "not_found", 412: "revision_conflict"}.get(response.status_code, "calendar_provider_error")
            raise DomainError(code, f"Calendar provider returned HTTP {response.status_code}")
        if len(response.content) > 8 * 1024 * 1024:
            raise DomainError("result_too_large", "Calendar response is too large; use a shorter date range")
        return response

    def shape(self, event, calendar_id):
        return {**event, "connection_id": self.identity, "calendar_id": calendar_id}

    def fingerprint(self, args):
        return hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
