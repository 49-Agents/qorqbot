"""Project settings: ``business_config_get``, ``business_config_update`` and ``business_config_patch`` over the project's
configuration row (``businesses.config_json``).

The generic part is the engine's: reading the row with its revision, the replacement and the recursive patch
(``merge_settings``), the revision check, the project name, stage, timezone, alias and registered-identity validation, the
registration of the three operations and the plan that records the change. A project's configuration also holds host state
(owner and workspace paths, browser, engineering and owner-session sections) with host authority rules; those
come in through the required ``SettingsHost``, each method called at one fixed point of the flow below. Nothing is discovered
by attribute or module and the host cannot replace the revision check or the generic validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision, schema, utc_clock
from qorqut_operations.contracts import ChangeSet, DomainError, InvocationContext, plain
from qorqut_operations.documents.operations import DocumentSources
from qorqut_operations.projects.settings import merge_settings

MUTATIONS = {
    "business_config_update": ({"config": {"type": "object"}}, ("config",)),
    "business_config_patch": ({"changes": {"type": "object", "minProperties": 1}}, ("changes",)),
}
DESCRIPTIONS = {
    "business_config_patch": "Patch editable settings in the selected project with current expected_revision. Nested objects merge; null removes a value. Unmentioned fields are preserved. Read business_config_get first.",
    "business_config_update": "Replace editable settings in the selected project using the current expected_revision. Preserve unrelated config fields. The owner and scoped Ordubashy may directly manage engineering organizations/repositories/deployment targets and browser settings; other agents cannot. Registered identity, owner, paths and generated browser identity remain protected. Tool overrides still apply.",
}
OPERATIONS = ("business_config_get", *MUTATIONS)
# The registered identity and paths a settings change may not edit.
PROTECTED_KEYS = ("id", "slug", "workspace", "hq_path", "owner_principal_id")


class SettingsHost(Protocol):
    """The host's knowledge of the settings sections it owns, each consulted at one fixed point.

    ``environment_status(uow, business_id)`` is the environment status ``business_config_get`` returns beside the
    configuration, read right after the project row. ``require_patch_authority(ctx, uow)`` refuses (``DomainError``) a
    patch the caller may not make; it runs after the revision check and before the merge, for a patch only (an update is a
    replacement). ``prepare(ctx, value, before_config, args, operation, uow)`` applies the host's protection and
    normalization rules to the proposed configuration ``value`` *in place*, before the generic validation and the plan.
    ``validate_targets(ctx, plan, uow)`` is the whole of the host's check inside the writing transaction that applies the
    plan: fresh authority, protected sections against the row as it is now and the host sections' applied-state rules."""

    def environment_status(self, uow, business_id: str) -> Any: ...

    def require_patch_authority(self, ctx: InvocationContext, uow) -> None: ...

    def prepare(self, ctx: InvocationContext, value: dict[str, Any], before_config: Mapping[str, Any],
                args: Mapping[str, Any], operation: str, uow) -> None: ...

    def validate_targets(self, ctx: InvocationContext, plan: ChangeSet, uow) -> None: ...


class BusinessConfigOperations:
    """``host`` is the required ``SettingsHost``."""

    def __init__(self, *, host, clock=utc_clock):
        if host is None:
            raise ValueError("Project settings need the settings host")
        self.host, self.clock = host, clock

    def register(self, registry):
        registry.register(OperationDescriptor("business_config_get", schema({}), effect_kind="read", supports_signoff=False,
            scope_kinds={"business"}, exposure=frozenset({"mcp", "dashboard", "worker"}),
            read=lambda ctx, args, options, uow: self.read(ctx, args, uow)))
        for name, (properties, required) in MUTATIONS.items():
            registry.register(DocumentSources.mutation_descriptor(name, properties, required,
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                apply=self.apply, validate_targets=self.validate_targets, description=DESCRIPTIONS[name],
                scope_kinds={"business"}, resolve_namespace=None, global_capability=None,
                exposure={"mcp", "dashboard", "worker"}))
        return self

    def read(self, ctx, args, uow):
        row = uow.repository("businesses").get(ctx.business_id, required=True)
        return {"config": row["config_json"], "revision": row["revision"],
                "environment": self.host.environment_status(uow, ctx.business_id)}

    def prepare(self, operation, ctx, args, options, uow):
        builder = PlanBuilder(ctx, operation, args, operation.replace("_", " ").title(), clock=self.clock,
                              namespace="business:" + ctx.business_id, affected_scope={"kind": "business", "business_id": ctx.business_id})
        before = uow.repository("businesses").get(ctx.business_id, required=True)
        require_revision(before, options.expected_revision)
        if operation == "business_config_patch":
            self.host.require_patch_authority(ctx, uow)
            value = merge_settings(before["config_json"], plain(args["changes"]))
        else:
            value = plain(args["config"])
        self.host.prepare(ctx, value, before["config_json"], args, operation, uow)
        name, stage, zone = value.get("name", before["name"]), value.get("stage", before["stage"]), value.get("timezone", before["timezone"])
        if not isinstance(name, str) or not name.strip() or not isinstance(stage, str) or not stage or not isinstance(zone, str):
            raise DomainError("validation_error", "Project name, stage, and timezone must be nonempty text")
        try: ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc: raise DomainError("validation_error", "Unknown project timezone") from exc
        aliases = value.get("aliases", [])
        if (not isinstance(aliases, list) or len(aliases) > 20
                or any(not isinstance(item, str) or not item.strip() or len(item) > 60 for item in aliases)):
            raise DomainError("validation_error", "Project aliases must be a list of at most 20 names of 1-60 characters")
        if any(key in value or key in before["config_json"] for key in PROTECTED_KEYS):
            old = before["config_json"]
            if any(value.get(key) != old.get(key) for key in PROTECTED_KEYS):
                raise DomainError("validation_error", "Registered identity, owner, and paths are not editable project settings")
        builder.record("businesses", ctx.business_id, before, {"config_json": value, "name": name, "stage": stage, "timezone": zone})
        return builder.build()

    def validate_targets(self, ctx, plan, uow):
        self.host.validate_targets(ctx, plan, uow)

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        return self.read(ctx, {}, uow)
