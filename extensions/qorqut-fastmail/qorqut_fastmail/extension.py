"""The Fastmail extension: ``qorqut_fastmail.extension:create``.

The factory validates the configuration and returns the immutable declaration; it opens no connection, reads no token and
touches no database. ``install(runtime)`` registers the ``fastmail_*`` operations and the one durable mutation program on the
still-open registries; no worker starts there. The extension owns no database table: the retired per-Project mail records
are described and exported by ``qorqut_fastmail.legacy`` / ``qorqut_fastmail.export`` and are not part of a fresh install.

Configuration (all keys documented in ``qorqut_fastmail.config``)::

    {"engine_url": "http://127.0.0.1:8787", "admin_token_file": "/abs/private/token", "account_id": "optional-pinned-account"}

Besides the operations, the extension supplies the ``fastmail`` CalDAV connection type to the host's ``calendar_*``
operations and, for Paper's explicit optional sender route, a narrow ``route_client`` interface."""

from __future__ import annotations

from pathlib import Path

import httpx

from qorqut_operations.application.catalog import CatalogContribution

from bazarlyk.extensions import Assets, CalendarProvider, Extension, OwnerRoute
from bazarlyk.agentic.text import render
from qorqut_operations.contracts import DomainError, PolicyDecision

from .outcome import email_outcome

from . import operations, program
from .calendar import FastmailCalendar
from .client import EngineClient
from .config import from_config

ID = "fastmail"
OWNER_READS = ("fastmail_capabilities_get", "fastmail_message_get", "fastmail_thread_get")
# Stable HTTP statuses for this extension's own refusals; unlisted codes keep the host's defaults.
STATUSES = {"account_mismatch": 409, "request_conflict": 409, "sender_identity_denied": 403}


def sending_policy(name, defaults, overrides, registry, *, role_id, scope, resolve):
    # Exact-content owner review remains mandatory even with an allow override.
    denied = overrides.get(name, defaults.get(name)) == "deny"
    return PolicyDecision("deny" if denied or scope != "global" else "signoff", "email_owner_approval")


def guidance(agentic_view):
    return render(agentic_view, "fastmail", "qorqut_fastmail/resources/email-management/providers/fastmail.md")


class Fastmail(Extension):
    id = ID

    def __init__(self, config, *, client_factory=httpx.Client):
        self.config, self.client_factory = config, client_factory
        self._client = None
        self.runtime = self.operations = self.program = None

    # -- declarations ----------------------------------------------------------------------------------------------
    def catalog(self):
        return (CatalogContribution(ID, entries=operations.catalog_entries()),)

    def calendar_providers(self):
        return (CalendarProvider("fastmail", FastmailCalendar),)

    def global_capabilities(self):
        return (operations.CAPABILITY,)

    def policy_rules(self):
        return {name: sending_policy for name in operations.SENDING}

    def global_discovery_operations(self):
        return tuple(operations.SENDING)

    def signoff_outcomes(self):
        return {name: email_outcome for name in operations.SENDING}

    def operation_guidance(self):
        return {entry.name: guidance for entry in operations.catalog_entries()}

    def assets(self):
        return Assets(Path(__file__).parent / "assets", scripts=("fastmail.js",), styles=("fastmail.css",))

    def owner_routes(self):
        return tuple(OwnerRoute("POST", ("root", "extensions", ID, "operations", name), owner_read_route(name))
                     for name in OWNER_READS)

    def error_statuses(self):
        return dict(STATUSES)

    # -- runtime wiring --------------------------------------------------------------------------------------------
    def install(self, runtime):
        self.runtime = runtime
        self.operations = operations.FastmailOperations(runtime, self)
        self.operations.register(runtime.registry)
        self.program = program.register(runtime.programs, self)
        runtime.application.fastmail = self.operations      # the durable program rechecks authority through this

    def client(self) -> EngineClient:
        """The engine client, built on first use; the token file is read only when a request needs it."""
        if self._client is None:
            self._client = EngineClient(self.config, client_factory=self.client_factory)
        return self._client

    def route_client(self) -> EngineClient:
        """The managed sender-route interface used by Paper's explicit optional route (``route_change`` / ``routes``)."""
        return self.client()


def owner_read_route(name):
    """Exactly the optional conversation reader's Global reads, behind the host's owner/origin checks."""
    async def handle(api, request, actor, request_id):
        import anyio
        from bazarlyk.transport.http import read_json, response, split_options
        context, _ = api.context(actor)
        args, options = split_options(await read_json(request))
        if (options.business_id is not None or options.review_business_id is not None
                or args.pop("scope", "global") != "global"):
            raise DomainError("forbidden", "Fastmail reads are Global and cannot select a project")
        result = await anyio.to_thread.run_sync(lambda: api.runtime.application.invoke(context, name, args, options))
        return response(result, statuses=api.statuses)
    return handle


def create(config=None, **injected):
    """Validate the configuration and return the declaration. ``injected`` are Python-only test transports
    (``client_factory``); a startup selection passes none."""
    return Fastmail(from_config(config), **injected)
