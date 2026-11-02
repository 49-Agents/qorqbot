"""The Google Calendar extension: ``qorqut_google_calendar.extension:create``.

The factory takes no configuration and returns the immutable declaration; it reads no credential, contacts no Google service,
starts no job and does not need a connected account. The extension contributes one ``CalendarProvider`` through the host's
existing ``calendar_providers()`` seam, the ``google`` connection type. Account settings (OAuth client, refresh token, email)
stay in each connection of the private calendar configuration, exactly as before; this extension adds nothing to it.

Selection in the trusted installation configuration::

    "extensions": [{"factory": "qorqut_google_calendar.extension:create", "config": {}}]
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError

from bazarlyk.extensions import CalendarProvider, Extension

from .calendar import GoogleCalendar

ID = "google_calendar"


class GoogleCalendarExtension(Extension):
    id = ID

    def calendar_providers(self):
        return (CalendarProvider("google", GoogleCalendar),)


def create(config=None):
    """Return the declaration. An empty configuration is the only one: connections are configured per account in the
    private calendar configuration, never here, so credentials placed in the extension configuration are refused."""
    if config is None:
        config = {}
    if not isinstance(config, dict) or config:
        raise DomainError("invalid_installation", "Google Calendar configuration: the extension takes an empty configuration; "
                                                  "connections are in the private calendar configuration")
    return GoogleCalendarExtension()
