"""Google Calendar API and Fastmail CalDAV, using provider-native scheduling."""

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from urllib.parse import quote, unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from qorqut_operations.contracts import DomainError

DAV = "DAV:"
CAL = "urn:ietf:params:xml:ns:caldav"
GOOGLE = "https://www.googleapis.com/calendar/v3"
FASTMAIL = "https://caldav.fastmail.com"


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


class GoogleCalendar(Provider):
    def __enter__(self):
        try:
            token = self.request("POST", "https://oauth2.googleapis.com/token", data={
                "client_id": self.config["client_id"], "client_secret": self.config["client_secret"],
                "refresh_token": self.config["refresh_token"], "grant_type": "refresh_token"}).json()["access_token"]
        except KeyError:
            self.client.close()
            raise DomainError("invalid_configuration", "Google Calendar needs OAuth client credentials and a refresh token") from None
        except Exception:
            self.client.close()
            raise
        self.client.headers["Authorization"] = "Bearer " + token
        return self

    def calendars(self):
        # This connection represents the owner's primary Google calendar.
        row = self.request("GET", GOOGLE + "/calendars/primary").json()
        return [{"connection_id": self.identity, "id": "primary", "name": row.get("summary", row["id"]),
                 "provider": "google", "timezone": row.get("timeZone"), "organizers": [row["id"]], "writable": True}]

    @staticmethod
    def base(calendar_id):
        if calendar_id != "primary":
            invalid("This Google connection uses its primary calendar")
        return GOOGLE + "/calendars/primary/events"

    def normalize(self, row, calendar_id):
        start, end = row.get("start", {}), row.get("end", {})
        return self.shape({"id": row["id"], "revision": row.get("etag", ""), "title": row.get("summary", ""),
            "start": start.get("dateTime", start.get("date", "")), "end": end.get("dateTime", end.get("date", "")),
            "timezone": start.get("timeZone", ""), "description": row.get("description", ""), "location": row.get("location", ""),
            "organizer": row.get("organizer", {}).get("email", ""), "status": row.get("status", "confirmed"),
            "attendees": [{"email": a["email"], "status": a.get("responseStatus", "needsAction")} for a in row.get("attendees", []) if "email" in a],
            "recurrence": row.get("recurrence", []), "series_id": row.get("recurringEventId"),
            "reminders": [r["minutes"] for r in row.get("reminders", {}).get("overrides", []) if r.get("method") == "popup"],
            "default_reminders": row.get("reminders", {}).get("useDefault", True),
            "meet_url": row.get("hangoutLink", ""), "conference_status": row.get("conferenceData", {}).get("createRequest", {}).get("status", {}).get("statusCode"),
            "url": row.get("htmlLink", "")}, calendar_id)

    def events(self, args):
        start, end = window(args)
        params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                  "orderBy": "startTime", "maxResults": args.get("limit", 100)}
        if args.get("cursor"):
            params["pageToken"] = args["cursor"]
        data = self.request("GET", self.base(args["calendar_id"]), params=params).json()
        return {"events": [self.normalize(row, args["calendar_id"]) for row in data.get("items", [])],
                "next_cursor": data.get("nextPageToken")}

    def get(self, calendar_id, event_id):
        return self.normalize(self.raw(calendar_id, event_id), calendar_id)

    def raw(self, calendar_id, event_id):
        return self.request("GET", self.base(calendar_id) + "/" + quote(event_id, safe="")).json()

    def body(self, fields, key, previous=None):
        result = {}
        for source, target in (("title", "summary"), ("description", "description"), ("location", "location"), ("recurrence", "recurrence")):
            if source in fields:
                result[target] = fields[source]
        for name in ("start", "end"):
            if name in fields or ("timezone" in fields and previous):
                value = fields.get(name) or previous[name].get("dateTime", previous[name].get("date"))
                parsed = instant(value)
                result[name] = {"dateTime" if isinstance(parsed, datetime) else "date": value}
                if isinstance(parsed, datetime) and fields.get("timezone"):
                    result[name]["timeZone"] = fields["timezone"]
                elif isinstance(parsed, datetime) and previous and previous.get(name, {}).get("timeZone"):
                    result[name]["timeZone"] = previous[name]["timeZone"]
        if "attendees" in fields:
            existing = {a["email"].lower(): a for a in (previous or {}).get("attendees", []) if "email" in a}
            result["attendees"] = [existing.get(email.lower(), {"email": email}) for email in fields["attendees"]]
        if "reminders" in fields:
            result["reminders"] = {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in fields["reminders"]]}
        if fields.get("google_meet") and not (previous or {}).get("conferenceData"):
            result["conferenceData"] = {"createRequest": {"requestId": key, "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        return result

    def mutate(self, operation, args):
        calendar_id, key = args["calendar_id"], args["request_key"]
        base = self.base(calendar_id)
        if operation == "calendar_event_create":
            validate_event(args)
            if args.get("organizer") and args["organizer"].lower() != self.config.get("email", "").lower():
                invalid("Google invitations use the connected Google calendar's organizer")
            body = self.body(args, key)
            body.update(id=key, extendedProperties={"private": {"qorqut_request": self.fingerprint(args)}})
            response = self.request("POST", base, json=body, params={"sendUpdates": "all", "conferenceDataVersion": 1}, allowed=(409,))
            row = response.json() if response.status_code != 409 else self.raw(calendar_id, key)
            if row.get("extendedProperties", {}).get("private", {}).get("qorqut_request") != self.fingerprint(args):
                raise DomainError("idempotency_conflict", "That calendar request key already created a different event")
        else:
            url = base + "/" + quote(args["event_id"], safe="")
            previous = self.raw(calendar_id, args["event_id"])
            if previous.get("etag") != args["revision"]:
                raise DomainError("revision_conflict", "Read the event again before changing it")
            headers = {"If-Match": args["revision"]}
            if operation == "calendar_event_delete":
                self.request("DELETE", url, headers=headers, params={"sendUpdates": "all"})
                return {"deleted": True, "event_id": args["event_id"]}
            if operation == "calendar_event_respond":
                if args["attendee"].lower() != self.config.get("email", "").lower():
                    invalid("Respond using the connected Google account address")
                attendees = previous.get("attendees", [])
                matches = [a for a in attendees if a.get("email", "").lower() == args["attendee"].lower()]
                if not matches:
                    invalid("This address is not an attendee")
                matches[0]["responseStatus"] = args["response"]
                body = {"attendees": attendees}
            else:
                current = self.normalize(previous, calendar_id)
                validate_event({**current, **args["changes"]})
                body = self.body(args["changes"], key, previous)
            row = self.request("PATCH", url, headers=headers, json=body,
                               params={"sendUpdates": "all", "conferenceDataVersion": 1}).json()
        return {"event": self.normalize(row, calendar_id)}


class FastmailCalendar(Provider):
    def __enter__(self):
        try:
            self.client.auth = (self.config["username"], self.config["app_password"])
        except KeyError:
            self.client.close()
            raise DomainError("invalid_configuration", "Fastmail Calendar needs a username and calendar app password") from None
        return self

    @property
    def identities(self):
        return self.config.get("identities", [self.config["username"]])

    @staticmethod
    def url(href):
        url = urljoin(FASTMAIL + "/", href)
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "caldav.fastmail.com" or parsed.query or parsed.fragment
                or any(part in {".", ".."} for part in unquote(parsed.path).split("/"))):
            invalid("Use a Fastmail calendar ID returned by calendar_calendars_list")
        return url

    def event_url(self, calendar_id, event_id):
        base, url = self.url(calendar_id), self.url(event_id)
        if not base.endswith("/") or not url.startswith(base) or "/" in unquote(url[len(base):]) or url == base:
            invalid("Event must belong to the selected calendar")
        return url

    def xml(self, method, url, body, depth="0"):
        response = self.request(method, url, content=body,
                                headers={"Content-Type": "application/xml; charset=utf-8", "Depth": depth})
        try:
            return ET.fromstring(response.content)
        except ET.ParseError:
            raise DomainError("calendar_provider_error", "Invalid CalDAV response") from None

    def propfind(self, url, prop, depth="0"):
        return self.xml("PROPFIND", url, f'<d:propfind xmlns:d="DAV:" xmlns:c="{CAL}"><d:prop>{prop}</d:prop></d:propfind>', depth)

    def calendars(self):
        root = self.propfind(FASTMAIL + "/", "<d:current-user-principal/>")
        principal = root.findtext(f".//{{{DAV}}}current-user-principal/{{{DAV}}}href")
        if not principal:
            raise DomainError("calendar_provider_error", "Fastmail did not return a calendar principal")
        root = self.propfind(self.url(principal), "<c:calendar-home-set/>")
        home = root.findtext(f".//{{{CAL}}}calendar-home-set/{{{DAV}}}href")
        if not home:
            raise DomainError("calendar_provider_error", "Fastmail did not return a calendar home")
        root = self.propfind(self.url(home), "<d:displayname/><d:resourcetype/><d:current-user-privilege-set/>", "1")
        rows = []
        for response in root.findall(f"{{{DAV}}}response"):
            if response.find(f".//{{{CAL}}}calendar") is None:
                continue
            href = response.findtext(f"{{{DAV}}}href")
            rows.append({"connection_id": self.identity, "id": self.url(href), "provider": "fastmail",
                         "name": response.findtext(f".//{{{DAV}}}displayname") or href,
                         "organizers": self.identities, "writable": response.find(f".//{{{DAV}}}write") is not None})
        return rows

    @staticmethod
    def parse(data):
        from icalendar import Calendar
        try:
            return Calendar.from_ical(data)
        except (ValueError, TypeError):
            raise DomainError("calendar_provider_error", "Invalid calendar event") from None

    def normalize(self, component, calendar_id, href, revision):
        def dt(name):
            prop = component.get(name)
            return prop.dt.isoformat() if prop is not None else ""
        attendees = component.get("ATTENDEE", [])
        if not isinstance(attendees, list):
            attendees = [attendees]
        recurrence = []
        for name in ("RRULE", "EXDATE", "RDATE"):
            values = component.get(name, [])
            for value in values if isinstance(values, list) else [values]:
                params = value.params.to_ical().decode()
                recurrence.append(name + (";" + params if params else "") + ":" + value.to_ical().decode())
        reminders = []
        for alarm in component.subcomponents:
            trigger = alarm.get("TRIGGER")
            if alarm.name == "VALARM" and str(alarm.get("ACTION", "")) == "DISPLAY" and trigger and isinstance(trigger.dt, timedelta):
                reminders.append(int(-trigger.dt.total_seconds() / 60))
        start = component.get("DTSTART")
        zone = str(start.params.get("TZID", "")) if start is not None else ""
        if not zone and start is not None and isinstance(start.dt, datetime) and start.dt.utcoffset() == timedelta():
            zone = "UTC"
        end = dt("DTEND")
        if not end and start is not None:
            duration = component.decoded("DURATION", timedelta(days=1) if type(start.dt) is date else timedelta())
            end = (start.dt + duration).isoformat()
        return self.shape({"id": href, "revision": revision, "uid": str(component.get("UID", "")),
            "title": str(component.get("SUMMARY", "")), "start": dt("DTSTART"), "end": end,
            "timezone": zone,
            "description": str(component.get("DESCRIPTION", "")), "location": str(component.get("LOCATION", "")),
            "organizer": str(component.get("ORGANIZER", "")).removeprefix("mailto:"),
            "attendees": [{"email": str(a).removeprefix("mailto:"), "status": str(a.params.get("PARTSTAT", "NEEDS-ACTION")).lower().replace("needs-action", "needsAction")} for a in attendees],
            "recurrence": recurrence, "series_id": href if component.get("RECURRENCE-ID") is not None or recurrence else None,
            "reminders": reminders, "default_reminders": False,
            "status": str(component.get("STATUS", "CONFIRMED")).lower(), "meet_url": ""}, calendar_id)

    def events(self, args):
        start, end = window(args)
        utc = lambda t: t.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        root = self.xml("REPORT", self.url(args["calendar_id"]), f'''<c:calendar-query xmlns:d="DAV:" xmlns:c="{CAL}">
          <d:prop><d:getetag/><c:calendar-data><c:expand start="{utc(start)}" end="{utc(end)}"/></c:calendar-data></d:prop>
          <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"><c:time-range start="{utc(start)}" end="{utc(end)}"/></c:comp-filter></c:comp-filter></c:filter>
          </c:calendar-query>''', "1")
        rows = []
        for response in root.findall(f"{{{DAV}}}response"):
            data = response.findtext(f".//{{{CAL}}}calendar-data")
            if not data:
                continue
            href = self.event_url(args["calendar_id"], response.findtext(f"{{{DAV}}}href"))
            for event in self.parse(data).walk("VEVENT"):
                rows.append(self.normalize(event, args["calendar_id"], href, response.findtext(f".//{{{DAV}}}getetag") or ""))
        rows.sort(key=lambda r: (r["start"], r["id"]))
        try:
            offset = int(args.get("cursor", "0"))
            if offset < 0:
                raise ValueError
        except ValueError:
            invalid("Invalid calendar page cursor")
        limit = args.get("limit", 100)
        return {"events": rows[offset:offset + limit], "next_cursor": str(offset + limit) if len(rows) > offset + limit else None}

    def raw(self, calendar_id, event_id):
        response = self.request("GET", self.event_url(calendar_id, event_id))
        return self.parse(response.content), response.headers.get("etag", "")

    @staticmethod
    def master(document):
        events = document.walk("VEVENT")
        if not events:
            raise DomainError("calendar_provider_error", "Resource contains no event")
        return next((e for e in events if "RECURRENCE-ID" not in e), events[0])

    def get(self, calendar_id, event_id):
        document, revision = self.raw(calendar_id, event_id)
        return self.normalize(self.master(document), calendar_id, event_id, revision)

    def edit(self, document, event, fields):
        from icalendar import Alarm, Calendar, vCalAddress
        if fields.get("google_meet"):
            invalid("Automatic Google Meet links require a Google event; put a supplied meeting link in location for Fastmail")
        for source, name in (("title", "SUMMARY"), ("description", "DESCRIPTION"), ("location", "LOCATION")):
            if source in fields:
                event.pop(name, None)
                event.add(name, fields[source])
        for name in ("start", "end"):
            property_name = "DT" + name.upper()
            if name not in fields and not (fields.get("timezone") and event.get(property_name) is not None):
                continue
            value = instant(fields[name]) if name in fields else event[property_name].dt
            if isinstance(value, datetime) and fields.get("timezone"):
                value = value.astimezone(ZoneInfo(fields["timezone"]))
            event.pop(property_name, None)
            event.add(property_name, value)
            if name == "end":
                event.pop("DURATION", None)
        if "attendees" in fields:
            previous = event.get("ATTENDEE", [])
            previous = previous if isinstance(previous, list) else [previous]
            existing = {str(a).lower(): a for a in previous}
            event.pop("ATTENDEE", None)
            for email in fields["attendees"]:
                address = existing.get("mailto:" + email.lower())
                if address is None:
                    address = vCalAddress("mailto:" + email)
                    address.params.update({"PARTSTAT": "NEEDS-ACTION", "RSVP": "TRUE"})
                event.add("ATTENDEE", address)
        if "recurrence" in fields:
            for name in ("RRULE", "EXDATE", "RDATE"):
                event.pop(name, None)
            if fields["recurrence"]:
                parsed = Calendar.from_ical("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n" + "\r\n".join(fields["recurrence"]) + "\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
                for name, value in self.master(parsed).items():
                    event[name] = value
            else:
                document.subcomponents = [c for c in document.subcomponents if c.name != "VEVENT" or c is event]
        if "reminders" in fields:
            event.subcomponents = [c for c in event.subcomponents if c.name != "VALARM"]
            for minutes in fields["reminders"]:
                alarm = Alarm()
                alarm.add("ACTION", "DISPLAY")
                alarm.add("DESCRIPTION", str(event.get("SUMMARY", "Reminder")))
                alarm.add("TRIGGER", -timedelta(minutes=minutes))
                event.add_component(alarm)

    def mutate(self, operation, args):
        from icalendar import Calendar, Event, vCalAddress
        calendar_id = args["calendar_id"]
        creating = operation == "calendar_event_create"
        if creating:
            validate_event(args)
            organizer = args.get("organizer", self.identities[0])
            if organizer.lower() not in {a.lower() for a in self.identities}:
                invalid("Use a configured Fastmail organizer address")
            url = self.url(calendar_id).rstrip("/") + "/" + args["request_key"] + ".ics"
            document, event = Calendar(), Event()
            document.add("PRODID", "-//Qorqut//Calendar//EN")
            document.add("VERSION", "2.0")
            document.add_component(event)
            event.add("UID", args["request_key"] + "@qorqut")
            event.add("ORGANIZER", vCalAddress("mailto:" + organizer))
            event.add("X-QORQUT-REQUEST", self.fingerprint(args))
            self.edit(document, event, args)
            headers = {"If-None-Match": "*"}
        else:
            url = self.event_url(calendar_id, args["event_id"])
            document, revision = self.raw(calendar_id, args["event_id"])
            if not revision or revision != args["revision"]:
                raise DomainError("revision_conflict", "Read the event again before changing it")
            headers = {"If-Match": revision}
            event = self.master(document)
            if operation == "calendar_event_delete":
                self.request("DELETE", url, headers=headers)
                return {"deleted": True, "event_id": args["event_id"]}
            if operation == "calendar_event_respond":
                if args["attendee"].lower() not in {a.lower() for a in self.identities}:
                    invalid("Respond using a configured Fastmail address")
                attendees = event.get("ATTENDEE", [])
                attendees = attendees if isinstance(attendees, list) else [attendees]
                found = [a for a in attendees if str(a).lower() == "mailto:" + args["attendee"].lower()]
                if not found:
                    invalid("This address is not an attendee")
                found[0].params["PARTSTAT"] = args["response"].upper()
            else:
                fields = args["changes"]
                current = self.normalize(event, calendar_id, url, revision)
                validate_event({**current, **fields})
                # Preserve the series timezone when changing its times.
                fields = {**fields, **({"timezone": current["timezone"]} if current["timezone"] and "timezone" not in fields else {})}
                self.edit(document, event, fields)
                event["SEQUENCE"] = int(event.get("SEQUENCE", 0)) + 1
        event.pop("DTSTAMP", None)
        event.add("DTSTAMP", datetime.now(timezone.utc))
        response = self.request("PUT", url, content=document.to_ical(),
            headers={**headers, "Content-Type": "text/calendar; charset=utf-8"}, allowed=(412,) if creating else ())
        if response.status_code == 412:
            existing, _ = self.raw(calendar_id, url)
            if str(self.master(existing).get("X-QORQUT-REQUEST", "")) != self.fingerprint(args):
                raise DomainError("idempotency_conflict", "That calendar request key already created a different event")
        return {"event": self.get(calendar_id, url)}
