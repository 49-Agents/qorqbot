"""Small native calendar surface shared by Ordubashy and the owner dashboard."""

import hashlib
import json
import os
from pathlib import Path
import threading

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, PreviewResource, plain
from qorqut_operations.application.plans import PlanBuilder, schema
from bazarlyk.agents.worker_access import is_worker

CAPABILITY = "manage_calendar"
READS = ("calendar_calendars_list", "calendar_events_list", "calendar_event_get")
WRITES = ("calendar_event_create", "calendar_event_update", "calendar_event_delete", "calendar_event_respond")
TEXT = {"type": "string", "maxLength": 4000}
ID = {"type": "string", "minLength": 1, "maxLength": 2000}
ADDRESS = {"type": "string", "format": "email", "maxLength": 254}
TIME = {"type": "string", "minLength": 10, "maxLength": 80,
        "description": "ISO date for all-day events, or ISO date-time with UTC offset."}
FIELDS = {
    "title": {"type": "string", "minLength": 1, "maxLength": 1000},
    "start": TIME, "end": TIME,
    "timezone": {"type": "string", "maxLength": 100, "description": "IANA timezone, e.g. America/New_York. Required for timed recurring events."},
    "description": TEXT, "location": TEXT,
    "attendees": {"type": "array", "maxItems": 100, "uniqueItems": True, "items": ADDRESS},
    "recurrence": {"type": "array", "maxItems": 20, "items": TEXT,
                   "description": "iCalendar RRULE/EXDATE/RDATE lines; [] removes recurrence. Series edits use the master event ID."},
    "reminders": {"type": "array", "maxItems": 5, "uniqueItems": True,
                  "items": {"type": "integer", "minimum": 0, "maximum": 40320},
                  "description": "Popup reminders in minutes before start; [] disables event reminders."},
    "google_meet": {"type": "boolean", "description": "Create a Google Meet link on a Google event."},
}
TARGET = {"connection_id": ID, "calendar_id": ID}
EVENT = {**TARGET, "event_id": ID}
VERSION = {**EVENT, "revision": ID}
SCHEMAS = {
    READS[0]: schema(),
    READS[1]: schema({**TARGET, "start": TIME, "end": TIME,
                     "cursor": ID, "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                    (*TARGET, "start", "end")),
    READS[2]: schema(EVENT, EVENT),
    WRITES[0]: schema({**TARGET, **FIELDS, "organizer": ADDRESS}, (*TARGET, "title", "start", "end")),
    WRITES[1]: schema({**VERSION, "changes": {**schema(FIELDS), "minProperties": 1}}, (*VERSION, "changes")),
    WRITES[2]: schema(VERSION, VERSION),
    WRITES[3]: schema({**VERSION, "response": {"enum": ["accepted", "declined", "tentative"]},
                      "attendee": ADDRESS}, (*VERSION, "response", "attendee")),
}


class CalendarOperations:
    def __init__(self, runtime, *, config=None, transport=None):
        self.runtime, self.config_override, self.transport = runtime, config, transport
        self.outcomes = {}
        self.lock = threading.Lock()

    def access(self, ctx, uow):
        if ctx.business_id or ctx.home_business_id or CAPABILITY not in ctx.global_capabilities:
            raise DomainError("forbidden", "Calendar tools require the Global calendar grant")
        installation = uow.repository("installation").get("service") or {}
        if ctx.principal_kind == "human" and installation.get("owner_principal_id") == ctx.principal_id:
            return
        if ctx.principal_kind == "agent":
            profile, _ = self.runtime.resolver.profile(ctx.principal, connection=uow.connection)
            from bazarlyk.management import ManagementService
            coordinator = profile.data["role"]["id"] == "ordubashy" and ManagementService._coordinator(uow) == ctx.principal_id
            if (profile.data["status"] == "active" and profile.scope.kind == "ecosystem"
                    and (coordinator or is_worker(uow.connection, ctx.principal_id))):
                return
        raise DomainError("forbidden", "Private calendars belong to the owner, configured Ordubashy and granted global workers")

    def register(self):
        descriptions = {
            READS[0]: "List connected Google/Fastmail calendars and organizer addresses. Credentials are never returned.",
            READS[1]: "Read current events in a date range from one calendar, including recurring occurrences and invitation response status.",
            READS[2]: "Read one current event, including its revision. Use a series_id to read a recurring master before editing the series.",
            WRITES[0]: "Create an event immediately. Attendees receive invitations. Supports recurrence, popup reminders, and Google Meet on Google events.",
            WRITES[1]: "Edit supplied fields immediately; other fields are preserved. Attendees receive provider updates. Use a master ID to edit the whole series.",
            WRITES[2]: "Delete an event immediately. Providers notify guests when cancelling an organized meeting. A master ID deletes the series.",
            WRITES[3]: "Accept, decline or tentatively respond to an invitation as a configured attendee; the provider sends the RSVP.",
        }
        for name in (*READS, *WRITES):
            common = dict(supports_signoff=False, scope_kinds={"global"}, exposure={"mcp", "dashboard"},
                          global_capability=CAPABILITY, resolve_namespace=lambda c, a: "global",
                          description=descriptions[name])
            if name in READS:
                descriptor = OperationDescriptor(name, SCHEMAS[name], effect_kind="read", external_read=True,
                    read=lambda c, a, o, u, op=name: self.read(c, a, u, op), **common)
            else:
                descriptor = OperationDescriptor(name, SCHEMAS[name], effect_kind="compound",
                    prepare=lambda c, a, o, u, op=name: self.prepare(c, a, o, u, op),
                    external=self.external, apply=self.apply,
                    validate_targets=lambda c, p, u: self.access(c, u),
                    replay_validator=lambda c, a, r, u: self.access(c, u), **common)
            self.runtime.registry.register(descriptor)
        return self

    def config(self):
        if self.config_override is not None:
            return self.config_override
        path = Path(os.environ.get("QORQUT_CALENDAR_CONFIG", self.runtime.workspace / "config/calendar.json"))
        if not path.exists():
            return {}
        try:
            if path.stat().st_mode & 0o077:
                raise ValueError("permissions")
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or not isinstance(value.get("connections", {}), dict):
                raise ValueError("shape")
            return value.get("connections", {})
        except (OSError, ValueError):
            raise DomainError("invalid_configuration", "Calendar configuration must be valid private JSON (chmod 600)") from None

    def provider(self, connection_id):
        from .providers import GoogleCalendar, FastmailCalendar
        config = self.config().get(connection_id)
        if not config:
            raise DomainError("not_found", "Calendar connection is not configured")
        provider = {"google": GoogleCalendar, "fastmail": FastmailCalendar}.get(config.get("provider"))
        if not provider:
            raise DomainError("invalid_configuration", "Unsupported calendar provider")
        return provider(connection_id, config, transport=self.transport)

    def read(self, ctx, args, uow, operation):
        self.access(ctx, uow)
        if operation == READS[0]:
            calendars, errors = [], []
            for identity in self.config():
                try:
                    with self.provider(identity) as provider:
                        calendars.extend(provider.calendars())
                except DomainError as exc:
                    errors.append({"connection_id": identity, "code": exc.code, "message": exc.message})
            return {"calendars": calendars, "errors": errors, "configured": bool(self.config())}
        with self.provider(args["connection_id"]) as provider:
            if operation == READS[1]:
                return provider.events(plain(args))
            return {"event": provider.get(args["calendar_id"], args["event_id"])}

    def prepare(self, ctx, args, options, uow, operation):
        self.access(ctx, uow)
        # Reuse the native request key for the provider's create identity.
        key = hashlib.sha256(f"{ctx.principal_id}:{options.idempotency_key}".encode()).hexdigest()
        builder = PlanBuilder(ctx, operation, args, "Calendar change")
        builder.resources.append(PreviewResource("calendar_request", key, "Provider request identity"))
        return builder.build()

    def external(self, ctx, plan):
        with self.lock, self.provider(plan.arguments["connection_id"]) as provider:
            self.outcomes[plan.payload_hash] = provider.mutate(plan.operation_name,
                {**plain(plan.arguments), "request_key": plan.preview["resources"][0]["resource_id"]})

    def apply(self, ctx, plan, uow):
        self.access(ctx, uow)
        return self.outcomes.pop(plan.payload_hash)
