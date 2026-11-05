"""Disposable real HTTP runtime with in-memory Google and Fastmail providers."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import tempfile

import httpx
import uvicorn

from bazarlyk.calendar.providers import GoogleCalendar, FastmailCalendar
from bazarlyk.service.web import create_app
from bazarlyk.transport.compatibility import DashboardCompatibility
from test_calendar_tools import GoogleServer, FastmailServer, GOOGLE_CONFIG, FASTMAIL_CONFIG, CALENDAR
from calendar_support import calendar_instance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dashboard-root', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1', help='Preview bind address; default is loopback')
    parser.add_argument('--port', type=int, default=0, help='Preview port; default chooses a free port')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='calendar-browser-v12-') as temporary, socket.socket() as listener, \
            calendar_instance(Path(temporary)) as fixture:
        google, fastmail = GoogleServer(), FastmailServer()
        transport = httpx.MockTransport(lambda request: fastmail(request) if request.url.host == 'caldav.fastmail.com' else google(request))
        fixture.runtime.calendar.config_override = {'personal': GOOGLE_CONFIG, 'business': FASTMAIL_CONFIG}
        fixture.runtime.calendar.transport = transport
        start = datetime.now(timezone.utc).replace(hour=14, minute=0, second=0, microsecond=0)
        event = {'title': 'Personal appointment', 'start': start.isoformat(), 'end': (start+timedelta(minutes=30)).isoformat(),
                 'timezone': 'UTC', 'request_key': 'personal'}
        with GoogleCalendar('personal', GOOGLE_CONFIG, transport=transport) as provider:
            provider.mutate('calendar_event_create', {**event, 'calendar_id': 'primary'})
        with FastmailCalendar('business', FASTMAIL_CONFIG, transport=transport) as provider:
            provider.mutate('calendar_event_create', {**event, 'calendar_id': CALENDAR, 'request_key': 'business',
                'title': 'Business invitation', 'attendees': ['owner@example.com']})
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.host,args.port)); listener.listen(128)
        origin = 'http://' + args.host + ':' + str(listener.getsockname()[1])
        app = create_app(fixture.runtime, dashboard_root=args.dashboard_root, origins=[origin], manage_lifecycle=False,
                         compatibility=DashboardCompatibility(fixture.runtime, background=fixture.runtime.background))
        print(json.dumps({'url': origin}), flush=True)
        uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False)).run(sockets=[listener])


if __name__ == '__main__':
    main()
