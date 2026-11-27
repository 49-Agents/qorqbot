"""Keyed: the test extension for set-up kind ``key`` (docs/EXTENSION-CONTRACT.md).

The owner types a token on the Keyed card; ``KeySetup`` keeps it in the extension's private folder. ``keyed_status``
answers whether a token is saved, never the token. ``Ticker`` is its background service: it counts while it runs, which
Qorqbot allows only while Keyed is ready and on for at least one agent. The factory and ``install`` read no key, start
nothing and contact nothing.
"""

from __future__ import annotations

import threading

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.plans import schema
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.contracts import DomainError

from qorqut.extension_contract import SetupField
from qorqut.extension_setup import KeySetup
from qorqut.extensions import Extension, ServiceDeclaration

ID = "keyed"
STATUS_TOOL = "keyed_status"
FIELDS = (SetupField("token", "Token", True),)
# The one token the check accepts, so tests need no outside service.
TEST_TOKEN = "keyed-test-token"
EXPOSURE = frozenset({"mcp"})
DESCRIPTION = "Whether the owner has saved the Keyed token. The token itself is never shown."


def check(values):
    """Accepts only the test token; the refusal names no value."""
    if values["token"] != TEST_TOKEN:
        raise DomainError("validation_error", "Keyed did not accept this token.")


class Ticker:
    """Counts once per ``interval`` seconds while it runs."""

    def __init__(self, interval=1.0):
        self.interval, self.ticks = interval, 0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="keyed-ticker", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            self.ticks += 1

    def stop(self, timeout=None):
        """Returns only once the thread has ended; raises ``TimeoutError`` while it still runs, as a stop proof needs."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise TimeoutError("The Keyed ticker did not stop in time.")
        self._thread = None

    def running(self):
        return self._thread is not None and self._thread.is_alive()


class Keyed(Extension):
    id = ID

    def __init__(self, *, interval=1.0):
        self.handler = None
        self.ticker = Ticker(interval)

    def catalog(self):
        return (CatalogContribution(ID, entries=(CatalogEntry(STATUS_TOOL, False, "read", EXPOSURE),)),)

    def install(self, runtime):
        self.handler = KeySetup(runtime.extension_private(ID), FIELDS, check)
        runtime.registry.register(OperationDescriptor(
            STATUS_TOOL, schema({"scope": {"const": "global"}}), effect_kind="read", supports_signoff=False,
            scope_kinds={"global"}, exposure=EXPOSURE,
            resolve_namespace=lambda ctx, args: "global", global_capability="read_global_context", default_mode="allow",
            description=DESCRIPTION, read=lambda ctx, args, options, uow: self.status()))

    def status(self):
        return {"token_saved": self.handler is not None and self.handler.status().state == "ready"}

    def setup_handler(self):
        return self.handler

    def services(self):
        return (ServiceDeclaration("keyed_ticker", self.ticker, start_before="background", stop_before="background",
                                   running=lambda service: service.running()),)


def create(config=None):
    if config:
        raise DomainError("invalid_installation", "Keyed takes no configuration.")
    return Keyed()
