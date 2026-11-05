"""The Fastmail CalDAV calendar adapter, moved from core with the provider seam (synthetic CalDAV transport only)."""

from xml.sax.saxutils import escape

import httpx
import pytest
from icalendar import Calendar
from qorqut_operations.contracts import DomainError

from qorqut_fastmail.calendar import FastmailCalendar

FASTMAIL_CONFIG = {"provider": "fastmail", "username": "owner@example.com", "app_password": "private",
                   "identities": ["owner@example.com", "owner@business.example"]}
CALENDAR = "https://caldav.fastmail.com/dav/calendars/user/owner@example.com/main/"
EVENT = {"title": "Weekly call", "start": "2026-10-15T10:00:00-04:00", "end": "2026-10-15T10:30:00-04:00",
         "timezone": "America/New_York", "attendees": ["guest@example.com"],
         "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"], "reminders": [10]}


class FastmailServer:
    def __init__(self):
        self.rows, self.revisions, self.writes = {}, {}, []

    def __call__(self, req):
        assert req.headers["authorization"].startswith("Basic ")
        path = str(req.url)
        if req.method == "PROPFIND":
            if req.url.path == "/":
                return httpx.Response(404)
            body = req.content.decode()
            if "current-user-principal" in body:
                prop = '<d:current-user-principal><d:href>/dav/principals/user/owner@example.com/</d:href></d:current-user-principal>'
            elif "calendar-home-set" in body:
                prop = '<c:calendar-home-set><d:href>/dav/calendars/user/owner@example.com/</d:href></c:calendar-home-set>'
            else:
                prop = '<d:displayname>Business</d:displayname><d:resourcetype><c:calendar/></d:resourcetype><d:current-user-privilege-set><d:privilege><d:write/></d:privilege></d:current-user-privilege-set>'
            return httpx.Response(207, text=f'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>{CALENDAR}</d:href><d:propstat><d:prop>{prop}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>')
        if req.method == "REPORT":
            assert b'c:expand start=' in req.content
            rows = ''.join(f'<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop><d:getetag>{escape(self.revisions[href])}</d:getetag><c:calendar-data>{escape(data.decode())}</c:calendar-data></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>' for href, data in self.rows.items())
            return httpx.Response(207, text=f'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{rows}</d:multistatus>')
        if req.method == "PUT":
            if req.headers.get("if-none-match") and path in self.rows:
                return httpx.Response(412)
            if path in self.rows:
                assert req.headers["if-match"] == self.revisions[path]
            self.writes.append(req)
            self.rows[path] = req.content
            self.revisions[path] = '"' + str(len(self.writes)) + '"'
            return httpx.Response(201)
        if path not in self.rows:
            return httpx.Response(404)
        if req.method == "GET":
            return httpx.Response(200, content=self.rows[path], headers={"etag": self.revisions[path]})
        if req.method == "DELETE":
            assert req.headers["if-match"] == self.revisions[path]
            del self.rows[path]
            self.writes.append(req)
            return httpx.Response(204)
        raise AssertionError(req.method)


def test_fastmail_discovers_calendars_from_dav_endpoint():
    server = FastmailServer()
    requests = []

    def transport(req):
        requests.append((req.method, req.url.path, req.headers.get("Depth")))
        return server(req)

    with FastmailCalendar("work", FASTMAIL_CONFIG, transport=httpx.MockTransport(transport)) as provider:
        assert provider.calendars() == [{"connection_id": "work", "id": CALENDAR,
            "provider": "fastmail", "name": "Business", "organizers": FASTMAIL_CONFIG["identities"],
            "writable": True}]
    assert requests == [
        ("PROPFIND", "/dav/", "0"),
        ("PROPFIND", "/dav/principals/user/owner@example.com/", "0"),
        ("PROPFIND", "/dav/calendars/user/owner@example.com/", "1"),
    ]


@pytest.mark.parametrize("property_name", ["current-user-principal", "calendar-home-set"])
def test_fastmail_rejects_foreign_discovery_urls(property_name):
    server = FastmailServer()
    requests = []

    def transport(req):
        requests.append(req)
        assert req.url.host == "caldav.fastmail.com"
        if property_name in req.content.decode():
            namespace = "DAV:" if property_name == "current-user-principal" else "urn:ietf:params:xml:ns:caldav"
            return httpx.Response(207, text=f'<d:multistatus xmlns:d="DAV:" xmlns:p="{namespace}">'
                f'<p:{property_name}><d:href>https://evil.example/private/</d:href>'
                f'</p:{property_name}></d:multistatus>')
        return server(req)

    with FastmailCalendar("work", FASTMAIL_CONFIG, transport=httpx.MockTransport(transport)) as provider:
        with pytest.raises(DomainError, match="Use a Fastmail calendar ID"):
            provider.calendars()
    assert len(requests) == (1 if property_name == "current-user-principal" else 2)


def test_fastmail_identity_recurrence_reminders_and_preserved_fields():
    server = FastmailServer()
    with FastmailCalendar("work", FASTMAIL_CONFIG, transport=httpx.MockTransport(server)) as provider:
        assert provider.calendars()[0]["organizers"] == FASTMAIL_CONFIG["identities"]
        args = {**EVENT, "calendar_id": CALENDAR, "request_key": "event", "organizer": "owner@business.example"}
        event = provider.mutate("calendar_event_create", args)["event"]
        assert event["organizer"] == "owner@business.example"
        assert event["recurrence"] == EVENT["recurrence"] and event["reminders"] == [10]
        assert event["timezone"] == "America/New_York"
        assert provider.events({"calendar_id": CALENDAR, "start": "2026-10-01", "end": "2026-11-01"})["events"][0]["id"] == event["id"]
        provider.mutate("calendar_event_create", args)
        assert len(server.writes) == 1
        document = Calendar.from_ical(server.rows[event["id"]])
        document.walk("VEVENT")[0].add("X-APPLE-TEST", "preserved")
        server.rows[event["id"]] = document.to_ical()
        target = {"calendar_id": CALENDAR, "event_id": event["id"], "revision": event["revision"], "request_key": "edit"}
        changed = provider.mutate("calendar_event_update", {**target, "changes": {"title": "New title", "timezone": "UTC"}})["event"]
        assert changed["title"] == "New title"
        assert changed["timezone"] == "UTC" and changed["start"].endswith("14:00:00+00:00")
        assert b"X-APPLE-TEST:preserved" in server.rows[event["id"]]
        assert changed["attendees"][0]["status"] == "needsAction"
        assert provider.mutate("calendar_event_delete", {**target, "revision": changed["revision"]})["deleted"]


def test_fastmail_pending_invite_rsvp_and_no_meet_side_effect():
    server = FastmailServer()
    with FastmailCalendar("work", FASTMAIL_CONFIG, transport=httpx.MockTransport(server)) as provider:
        with pytest.raises(DomainError, match="Google Meet"):
            provider.mutate("calendar_event_create", {**EVENT, "calendar_id": CALENDAR, "request_key": "bad", "google_meet": True})
        assert not server.writes
        args = {**EVENT, "calendar_id": CALENDAR, "request_key": "invite", "attendees": ["owner@example.com"]}
        event = provider.mutate("calendar_event_create", args)["event"]
        before = len(server.writes)
        assert provider.get(CALENDAR, event["id"])["attendees"][0]["status"] == "needsAction"
        assert len(server.writes) == before
        response = provider.mutate("calendar_event_respond", {"calendar_id": CALENDAR, "event_id": event["id"],
            "revision": event["revision"], "request_key": "reply", "attendee": "owner@example.com", "response": "accepted"})
        assert response["event"]["attendees"][0]["status"] == "accepted"


def test_fastmail_rename_all_day_event_without_timezone():
    server = FastmailServer()
    with FastmailCalendar("work", FASTMAIL_CONFIG, transport=httpx.MockTransport(server)) as provider:
        event = provider.mutate("calendar_event_create", {"calendar_id": CALENDAR, "request_key": "all-day",
            "title": "Original", "start": "2026-10-15", "end": "2026-10-16"})["event"]
        changed = provider.mutate("calendar_event_update", {"calendar_id": CALENDAR, "event_id": event["id"],
            "revision": event["revision"], "request_key": "rename", "changes": {"title": "Renamed"}})["event"]
        assert changed["title"] == "Renamed"
        assert changed["start"] == "2026-10-15" and changed["end"] == "2026-10-16"


def test_provider_errors_are_sanitized_and_foreign_caldav_urls_rejected():
    with FastmailCalendar("work", FASTMAIL_CONFIG,
            transport=httpx.MockTransport(lambda req: httpx.Response(401, text="private-token"))) as provider:
        with pytest.raises(DomainError, match="HTTP 401") as exc:
            provider.calendars()
        assert "private-token" not in str(exc.value)
        for url in ("https://evil.example/event.ics", CALENDAR + "%2e%2e/secret.ics", CALENDAR + "folder%2fevent.ics"):
            with pytest.raises(DomainError):
                provider.get(CALENDAR, url)


def test_the_connect_helper_saves_a_fastmail_connection_privately_and_prints_no_credential(tmp_path, monkeypatch, capsys):
    import json
    from qorqut_fastmail import calendar_connect
    answers = iter(["owner@example.com", "owner@example.com, owner@business.example"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "app-password-value")
    path = tmp_path / "private/calendar.json"
    calendar_connect.main(["--config", str(path), "--name", "business"])
    saved = json.loads(path.read_text())["connections"]["business"]
    assert saved == {"provider": "fastmail", "username": "owner@example.com", "app_password": "app-password-value",
                     "identities": ["owner@example.com", "owner@business.example"]}
    assert path.stat().st_mode & 0o777 == 0o600
    shown = capsys.readouterr()
    assert "app-password-value" not in shown.out + shown.err and "No events were created" in shown.out
