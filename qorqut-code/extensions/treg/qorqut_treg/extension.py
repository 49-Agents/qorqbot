"""The treg extension: ``qorqut_treg.extension:create``.

treg (treg.to) is a hosted gateway to thousands of outside tools behind one key. This extension puts it on the central
Qorqut MCP as four Global read operations (``treg_search``, ``treg_tool_get``, ``treg_lookup``, ``treg_balance``), so
every agent client reaches it the same way and the service, not the agent, decides what may run and what it may cost.

The factory validates the configuration and returns the declaration; it reads no key and contacts nothing.
``install(runtime)`` registers the operations. The owner's key is saved from the dashboard through one owner route and
kept in a private file of the instance.

Selection in the trusted installation configuration::

    "extensions": [{"factory": "qorqut_treg.extension:create", "config": {}}]
"""

from __future__ import annotations

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.contracts import DomainError

from qorqut.extensions import Extension, OwnerRoute

from . import config as settings
from . import operations
from .client import TregClient

ID = "treg"
STATUSES = {"invalid_key": 422}


class Treg(Extension):
    id = ID

    def __init__(self, config, *, client=None):
        self.config = config
        self.client = client or TregClient()
        self.instance_root = self.operations = None

    # -- declarations ----------------------------------------------------------------------------------------------
    def catalog(self):
        return (CatalogContribution(ID, entries=operations.catalog_entries()),)

    def policy_rules(self):
        return {name: operations.policy for name in operations.NAMES}

    def owner_routes(self):
        return (OwnerRoute("POST", ("root", "extensions", ID, "key"), key_route(self)),)

    def error_statuses(self):
        return dict(STATUSES)

    # -- runtime wiring --------------------------------------------------------------------------------------------
    def install(self, runtime):
        self.instance_root = runtime.paths.instance_root
        self.operations = operations.TregOperations(runtime, self).register(runtime.registry)

    # -- the owner's key ---------------------------------------------------------------------------------------------
    def status(self) -> str:
        """``connected`` once a key is saved, otherwise ``not_set_up``. Reads no key and contacts nothing."""
        return "connected" if self.instance_root is not None and settings.has_key(self.instance_root) else "not_set_up"

    def change_key(self, change) -> dict:
        """``{"key": "<pasted key>"}`` checks the key with treg and saves it; ``{"key": null}`` removes the saved key."""
        if not isinstance(change, dict) or set(change) != {"key"}:
            raise DomainError("validation_error", "Send the treg key, or null to remove it")
        if change["key"] is None:
            settings.remove_key(self.instance_root)
        else:
            key = settings.check_key(change["key"])
            self.client.account(key)             # treg refuses a wrong key here, before anything is saved
            settings.save_key(self.instance_root, key)
        return {"status": self.status()}


def key_route(extension):
    """Save or remove the owner's treg key, behind the host's owner, Origin and CSRF checks. The key is never returned."""
    async def handle(api, request, actor, request_id):
        import anyio
        from qorqut_operations.contracts import OperationResult
        from qorqut.transport.http import read_json, response
        if request.headers.get("authorization") is not None:
            raise DomainError("forbidden", "The treg key is set from the owner's dashboard")
        context, _ = api.context(actor)
        with api.runtime.database.read() as uow:
            owner = uow.repository("installation").get("service")
        if context.principal_kind != "human" or not owner or owner["owner_principal_id"] != context.principal_id:
            raise DomainError("forbidden", "Only the installation owner can change the treg key")
        change = await read_json(request, limit=8192)
        data = await anyio.to_thread.run_sync(lambda: extension.change_key(change))
        result = response(OperationResult.ok(data, request_id=request_id), statuses=api.statuses)
        result.headers["Cache-Control"] = "private, no-store"
        return result
    return handle


def create(config=None, **injected):
    """Validate the configuration and return the declaration. ``injected`` is the Python-only test transport
    (``client``); a startup selection passes none."""
    return Treg(settings.from_config(config), **injected)
