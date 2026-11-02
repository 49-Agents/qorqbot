"""The Google Calendar adapter: the ``google`` connection type of the host's ``calendar_*`` operations.

The host keeps the common provider interface and helpers (``bazarlyk.calendar.providers``: ``Provider``, event validation, time
parsing and error mapping); this module adds only Google's native API behavior. A connection is the owner's primary Google
calendar, authorized by the OAuth client credentials and refresh token stored in that connection's private configuration.
Nothing is read or contacted at import: the token exchange happens when a connection is opened (``__enter__``)."""

from datetime import datetime
from urllib.parse import quote

from qorqut_operations.contracts import DomainError

from bazarlyk.calendar.providers import Provider, instant, invalid, validate_event, window

GOOGLE = "https://www.googleapis.com/calendar/v3"


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
                 "provider": "google", "provider_name": "Google", "timezone": row.get("timeZone"), "organizers": [row["id"]], "writable": True}]

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
