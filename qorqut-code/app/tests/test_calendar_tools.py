"""Focused provider contracts and the native private-calendar boundary."""

import json
from types import SimpleNamespace

import httpx
import pytest

from bazarlyk.calendar.operations import CalendarOperations
from bazarlyk.calendar.providers import GoogleCalendar
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id

GOOGLE_CONFIG = {"provider": "google", "email": "owner@example.com", "client_id": "client",
                 "client_secret": "secret", "refresh_token": "refresh"}
FASTMAIL_CONFIG = {"provider": "fastmail", "username": "owner@example.com", "app_password": "private",
                   "identities": ["owner@example.com", "owner@business.example"]}
EVENT = {"title": "Weekly call", "start": "2026-10-15T10:00:00-04:00", "end": "2026-10-15T10:30:00-04:00",
         "timezone": "America/New_York", "attendees": ["guest@example.com"],
         "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"], "reminders": [10]}


class GoogleServer:
    def __init__(self):
        self.rows, self.writes = {}, []

    def __call__(self, req):
        if req.url.host == "oauth2.googleapis.com":
            assert b"refresh_token=refresh" in req.content
            return httpx.Response(200, json={"access_token": "access"})
        assert req.headers["authorization"] == "Bearer access"
        if req.method == "GET" and req.url.path.endswith("/calendars/primary"):
            return httpx.Response(200, json={"id": "owner@example.com", "summary": "Personal"})
        if req.url.path.endswith("/events"):
            if req.method == "GET":
                assert req.url.params["singleEvents"] == "true"
                return httpx.Response(200, json={"items": list(self.rows.values()), "nextPageToken": "page-2"})
            value = json.loads(req.content)
            if value["id"] in self.rows:
                return httpx.Response(409)
            self.writes.append(req)
            row = {**value, "etag": '"1"', "organizer": {"email": "owner@example.com"},
                   "hangoutLink": "https://meet.google.com/test-call", "unknownProviderField": "preserve"}
            self.rows[row["id"]] = row
            return httpx.Response(200, json=row)
        identity = req.url.path.rsplit("/", 1)[-1]
        if identity not in self.rows:
            return httpx.Response(404)
        row = self.rows[identity]
        if req.method == "GET":
            return httpx.Response(200, json=row)
        assert req.headers["if-match"] == row["etag"]
        assert req.url.params["sendUpdates"] == "all"
        self.writes.append(req)
        if req.method == "DELETE":
            del self.rows[identity]
            return httpx.Response(204)
        row.update(json.loads(req.content))
        row["etag"] = '"2"'
        return httpx.Response(200, json=row)


def test_google_create_recurrence_meet_edit_and_delete():
    server = GoogleServer()
    with GoogleCalendar("personal", GOOGLE_CONFIG, transport=httpx.MockTransport(server)) as provider:
        assert provider.calendars()[0]["organizers"] == ["owner@example.com"]
        args = {**EVENT, "calendar_id": "primary", "request_key": "abc123", "google_meet": True}
        event = provider.mutate("calendar_event_create", args)["event"]
        assert event["meet_url"] == "https://meet.google.com/test-call"
        assert event["recurrence"] == EVENT["recurrence"] and event["reminders"] == [10]
        assert server.rows["abc123"]["conferenceData"]["createRequest"]["requestId"] == "abc123"
        assert provider.mutate("calendar_event_create", args)["event"]["id"] == event["id"]
        assert len(server.writes) == 1
        with pytest.raises(DomainError, match="different event"):
            provider.mutate("calendar_event_create", {**args, "title": "Changed under same key"})
        listing = provider.events({"calendar_id": "primary", "start": "2026-10-01", "end": "2026-11-01"})
        assert listing["next_cursor"] == "page-2"
        target = {"calendar_id": "primary", "event_id": event["id"], "revision": event["revision"], "request_key": "edit"}
        edited = provider.mutate("calendar_event_update", {**target, "changes": {"title": "New title"}})["event"]
        assert server.rows["abc123"]["unknownProviderField"] == "preserve"
        assert edited["attendees"][0]["email"] == "guest@example.com"
        with pytest.raises(DomainError, match="Read the event"):
            provider.mutate("calendar_event_delete", target)
        assert provider.mutate("calendar_event_delete", {**target, "revision": edited["revision"]})["deleted"]


def test_google_rsvp_and_calendar_identity():
    server = GoogleServer()
    server.rows["invite"] = {"id": "invite", "etag": '"1"', "summary": "Invite", "start": {"date": "2026-11-01"},
        "end": {"date": "2026-11-02"}, "attendees": [{"email": "owner@example.com", "responseStatus": "needsAction"},
                                                    {"email": "other@example.com", "responseStatus": "accepted"}]}
    with GoogleCalendar("personal", GOOGLE_CONFIG, transport=httpx.MockTransport(server)) as provider:
        target = {"calendar_id": "primary", "event_id": "invite", "revision": '"1"', "request_key": "reply",
                  "attendee": "owner@example.com", "response": "accepted"}
        assert provider.get("primary", "invite")["attendees"][0]["status"] == "needsAction"
        assert not server.writes
        result = provider.mutate("calendar_event_respond", target)["event"]
        assert result["attendees"][0]["status"] == "accepted"
        assert result["attendees"][1]["status"] == "accepted"
        with pytest.raises(DomainError, match="organizer"):
            provider.mutate("calendar_event_create", {**EVENT, "calendar_id": "primary", "request_key": "create",
                                                       "organizer": "owner@business.example"})


@pytest.mark.parametrize("all_day", [True, False])
def test_google_rename_existing_event_without_timezone(all_day):
    server = GoogleServer()
    start = {"date": "2026-10-15"} if all_day else {"dateTime": "2026-10-15T10:00:00-04:00"}
    end = {"date": "2026-10-16"} if all_day else {"dateTime": "2026-10-15T10:30:00-04:00"}
    server.rows["existing"] = {"id": "existing", "etag": '"1"', "summary": "Original", "start": start, "end": end}
    with GoogleCalendar("personal", GOOGLE_CONFIG, transport=httpx.MockTransport(server)) as provider:
        event = provider.mutate("calendar_event_update", {"calendar_id": "primary", "event_id": "existing",
            "revision": '"1"', "request_key": "rename", "changes": {"title": "Renamed"}})["event"]
        assert event["title"] == "Renamed"
        assert server.rows["existing"]["start"] == start and server.rows["existing"]["end"] == end


def test_native_owner_tools_http_replay_and_private_authority(tmp_path):
    from starlette.testclient import TestClient
    from bazarlyk.service.web import create_app
    from calendar_support import calendar_instance, create_calendar_agent
    from host_foundation_support import historical_business_agent, register_sources, set_agent_overrides

    with calendar_instance(tmp_path) as instance:
        runtime = instance.runtime
        server = GoogleServer()
        runtime.calendar.config_override = {"personal": GOOGLE_CONFIG}
        runtime.calendar.transport = httpx.MockTransport(server)
        args = {**EVENT, "connection_id": "personal", "calendar_id": "primary"}
        key = new_id()
        web = create_app(runtime, origins=["http://testserver"], manage_lifecycle=False)
        with TestClient(web) as client:
            response = client.post('/api/root/operations/calendar_event_create',
                headers={"Origin": "http://testserver"}, json={**args, "idempotency_key": key})
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "ok", response.text
            again = client.post('/api/root/operations/calendar_event_create',
                headers={"Origin": "http://testserver"}, json={**args, "idempotency_key": key})
            assert again.json()["data"] == response.json()["data"]
        assert len(server.writes) == 1

        # Other Global roles (a worker is a granted role) and historical business-home identities cannot gain private calendars
        # through an explicit allow.
        register_sources(instance, instance.business, "first")
        other_role = create_calendar_agent(instance, "calendar-applier", role="learning-applier")
        set_agent_overrides(instance, other_role.id, {"calendar_calendars_list": "allow"}, {instance.business})
        denied = runtime.application.invoke(runtime.resolver.invocation_context(other_role), "calendar_calendars_list", {})
        assert denied.status == "error" and denied.error["code"] == "forbidden"
        from qorqut_operations.contracts import InvocationContext, Principal
        historical = historical_business_agent(instance, instance.business)
        denied = runtime.application.invoke(InvocationContext(Principal(historical, "agent"), source="mcp"),
                                            "calendar_calendars_list", {})
        assert denied.status == "error" and denied.error["code"] == "inactive_agent"

        manager_principal = create_calendar_agent(instance, "calendar-coordinator")
        with runtime.database.transaction() as uow:
            installation = uow.repository("installation").get("service", required=True)
            uow.repository("installation").update("service", {"management_coordinator_id": manager_principal.id},
                                                   expected_revision=installation["revision"])
        manager = runtime.resolver.invocation_context(manager_principal)
        result = runtime.application.invoke(manager, "calendar_calendars_list", {})
        assert result.status == "ok", result.to_dict()
        assert result.data["calendars"][0]["id"] == "primary"
        # A removed coordinator cannot read a private write receipt from an earlier grant. The roster holds one Ordubashy,
        # so the owner removes it by clearing the selection rather than registering a replacement beside it.
        options = InvocationOptions(idempotency_key=new_id())
        result = runtime.application.invoke(manager, "calendar_event_create", args, options)
        assert result.status == "ok", result.to_dict()
        with runtime.database.transaction() as uow:
            installation = uow.repository("installation").get("service", required=True)
            uow.repository("installation").update("service", {"management_coordinator_id": None},
                                                   expected_revision=installation["revision"])
        assert runtime.application.invoke(manager, "calendar_event_create", args, options).status == "error"


class StubCalendar:
    """A stand-in adapter for the provider seam: the host calls it only through the Provider interface."""

    def __init__(self, connection_id, config, transport=None):
        self.connection_id = connection_id

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def calendars(self):
        return [{"connection_id": self.connection_id, "id": "stub-main", "provider": "fastmail", "name": "Stub",
                 "organizers": ["owner@example.com"], "writable": True}]


def coordinator_context(instance, name="calendar-coordinator"):
    from calendar_support import create_calendar_agent
    from host_foundation_support import register_sources
    register_sources(instance, instance.business, "first")
    principal = create_calendar_agent(instance, name)
    with instance.runtime.database.transaction() as uow:
        installation = uow.repository("installation").get("service", required=True)
        uow.repository("installation").update("service", {"management_coordinator_id": principal.id},
                                               expected_revision=installation["revision"])
    return instance.runtime.resolver.invocation_context(principal)


def test_a_connection_type_no_extension_supplies_is_unavailable_and_google_keeps_working(tmp_path):
    from calendar_support import calendar_instance
    with calendar_instance(tmp_path) as instance:
        runtime = instance.runtime
        assert runtime.calendar.providers == {}
        runtime.calendar.config_override = {"personal": GOOGLE_CONFIG, "work": FASTMAIL_CONFIG}
        runtime.calendar.transport = httpx.MockTransport(GoogleServer())
        manager = coordinator_context(instance)
        listing = runtime.application.invoke(manager, "calendar_calendars_list", {})
        assert listing.status == "ok", listing.to_dict()
        assert [item["id"] for item in listing.data["calendars"]] == ["primary"]
        assert [(item["connection_id"], item["code"]) for item in listing.data["errors"]] == [("work", "provider_unavailable")]
        assert "private" not in json.dumps(listing.to_dict())            # the connection's credentials are never reported
        events = runtime.application.invoke(manager, "calendar_events_list", {
            "connection_id": "work", "calendar_id": "x", "start": "2026-10-01", "end": "2026-11-01"})
        assert events.status == "error" and events.error["code"] == "provider_unavailable"


def test_a_selected_extension_supplies_its_connection_type_through_the_seam(tmp_path):
    from bazarlyk.extensions import CalendarProvider, Extension, select
    from calendar_support import calendar_instance

    class Supplier(Extension):
        id = "stubcal"

        def calendar_providers(self):
            return (CalendarProvider("fastmail", StubCalendar),)

    with calendar_instance(tmp_path, extensions=select(instances=[Supplier()])) as instance:
        runtime = instance.runtime
        assert runtime.calendar.providers == {"fastmail": StubCalendar}
        runtime.calendar.config_override = {"personal": GOOGLE_CONFIG, "work": FASTMAIL_CONFIG}
        runtime.calendar.transport = httpx.MockTransport(GoogleServer())
        listing = runtime.application.invoke(coordinator_context(instance), "calendar_calendars_list", {})
        assert listing.status == "ok", listing.to_dict()
        assert sorted(item["id"] for item in listing.data["calendars"]) == ["primary", "stub-main"]
        assert list(listing.data["errors"]) == []


def test_an_extension_cannot_replace_the_core_google_provider():
    from bazarlyk.extensions import CalendarProvider
    with pytest.raises(DomainError):
        CalendarProvider("google", StubCalendar)


def test_unconfigured_connections_are_clear_and_private_config_not_exposed(tmp_path):
    runtime = SimpleNamespace(workspace=tmp_path)
    operations = CalendarOperations(runtime)
    assert operations.config() == {}
    (tmp_path / "config").mkdir()
    path = tmp_path / "config/calendar.json"
    path.write_text(json.dumps({"connections": {"personal": GOOGLE_CONFIG}}))
    path.chmod(0o644)
    with pytest.raises(DomainError, match="chmod 600"):
        operations.config()
    path.chmod(0o600)
    assert operations.config()["personal"]["provider"] == "google"


def test_connection_save_preserves_other_accounts_and_protects_credentials(tmp_path):
    from bazarlyk.calendar.connect import save_connection
    path = tmp_path / 'private/calendar.json'
    save_connection(path, 'personal', GOOGLE_CONFIG)
    save_connection(path, 'business', FASTMAIL_CONFIG)
    assert path.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(path.read_text())['connections']) == {'personal', 'business'}
