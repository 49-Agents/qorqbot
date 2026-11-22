"""``openrouter_status``: whether the owner has connected OpenRouter, for the coordinator.

One Global read for the configured Ordubashy. It tells the coordinator whether OpenRouter is connected and what that
turns on, so it can offer the connection during set-up and confirm it afterwards. It never returns the key. When a key
is saved it asks OpenRouter for that key's own limit and use, and reports them in US dollars.

Nothing is stored: no table, no job and no copy of an answer."""

from __future__ import annotations

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import schema
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.contracts import DomainError, PolicyDecision, plain

from . import config as settings

STATUS = "openrouter_status"
NAMES = (STATUS,)
EXPOSURE = frozenset({"mcp"})
CAPABILITY = "read_global_context"      # the capability every registered global agent holds; ``access`` narrows it
ROLES = frozenset({"ordubashy"})
USES = ("Voice input: the owner speaks a Chat message instead of typing it.",)
HOW_TO_CONNECT = ("The owner opens Settings, Connections, finds OpenRouter under Extensions and presses Connect. "
                  "OpenRouter opens in their browser, they sign in or create an account there and approve, and the "
                  "key is saved on this computer. An OpenRouter key can also be pasted in the same place. When Global "
                  "Settings already holds a key, connect it yourself with settings_set (setting extension_key). Never "
                  "ask for the key in Chat.")
DESCRIPTION = ("Whether the owner has connected OpenRouter, what it is used for here, and how the owner connects it. "
               "Once connected it also reports the key's spending limit and use in US dollars. It never returns the key.")


def catalog_entries() -> tuple[CatalogEntry, ...]:
    return tuple(CatalogEntry(name, False, "read", EXPOSURE) for name in NAMES)


class OpenRouterOperations:
    def __init__(self, runtime, extension):
        self.runtime, self.extension = runtime, extension

    def access(self, ctx, uow):
        """The configured Ordubashy, in the Global scope. Resolved from the authoritative profile, never from a caller
        field; a role grant or a policy override cannot widen this."""
        refusal = DomainError("forbidden", "OpenRouter status is for Ordubashy")
        if ctx.principal_kind != "agent" or ctx.business_id is not None or ctx.home_business_id is not None:
            raise refusal
        from qorqut.management import ManagementService
        profile, _ = self.runtime.resolver.profile(ctx.principal, connection=uow.connection)
        if (profile.data["status"] != "active" or profile.scope.kind != "ecosystem"
                or profile.data["role"]["id"] != "ordubashy" or ManagementService._coordinator(uow) != ctx.principal_id):
            raise refusal

    def register(self, registry):
        registry.register(OperationDescriptor(
            STATUS, schema(), effect_kind="read", supports_signoff=False, scope_kinds={"global"}, exposure=EXPOSURE,
            resolve_namespace=lambda c, a: "global", global_capability=CAPABILITY, external_read=True, default_mode="allow",
            description=DESCRIPTION, read=lambda ctx, args, options, uow: self.run(ctx, uow)))
        return self

    def run(self, ctx, uow):
        self.access(ctx, uow)
        return plain(self.status())

    def status(self):
        key = settings.read_key(self.extension.instance_root)
        if key is None:
            return {"status": "not_set_up", "used_for": list(USES), "how_to_connect": HOW_TO_CONNECT}
        result = {"status": "connected", "used_for": list(USES)}
        try:
            result["key"] = self.extension.client.key_info(key)
        except DomainError as problem:
            # A saved key OpenRouter no longer accepts is the owner's to replace; an unreachable service is not a status.
            if problem.code != "openrouter_key_refused":
                raise
            result.update(status="key_refused", how_to_connect=HOW_TO_CONNECT)
        return result


def policy(name, defaults, overrides, registry, *, role_id, scope, resolve):
    """Before any override: the tool exists for Ordubashy only. For Ordubashy the ordinary policy decides (allowed by
    default; the owner can still deny it in the agent's settings)."""
    return None if role_id in ROLES else PolicyDecision("deny", "openrouter_roles")
