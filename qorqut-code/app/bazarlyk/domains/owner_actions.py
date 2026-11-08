"""A closed set of Ordubashy proposals, applied only by exact owner signoff.

Never impersonate a human or dispatch caller-selected operations/endpoints.
Existing human-only APIs retain their checks. Adding an action requires review
of its frozen effects, validation, attribution and approval-time scope checks.
"""

from qorqut_agentic.behavior.tool_help import text as instruction_text

import json

from bazarlyk.agents.engineering import validate_authority
from bazarlyk.agents.global_scope import reaches_business
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision, schema, utc_clock

OPERATION = "owner_action_propose"
ENGINEERING = "engineering_update"
NAMES = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 300},
         "maxItems": 100, "uniqueItems": True}


class OwnerActions:
    def __init__(self, paths, *, clock=utc_clock):
        self.paths, self.clock = paths, clock

    def register(self, registry):
        actions = {
            ENGINEERING: schema({key: NAMES for key in ("github_organizations", "repositories", "deployment_targets")},
                                ("github_organizations", "repositories", "deployment_targets")),
        }
        arguments = schema({"action": {"enum": list(actions)}, "parameters": {"type": "object"},
                            "rationale": {"type": "string", "minLength": 1, "maxLength": 4000}},
                           ("action", "parameters", "rationale"))
        arguments["oneOf"] = [{"properties": {"action": {"const": action}, "parameters": fields}}
                              for action, fields in actions.items()]
        registry.register(OperationDescriptor(OPERATION, arguments,
            description=instruction_text('owner-actions-ordubashy-only-propose-engineering-update-in-an'),
            prepare=self.prepare, apply=self.apply, validate_targets=self.guard))

    def guard(self, ctx, plan, uow):
        if (ctx.principal_kind != "agent" or ctx.home_business_id is not None
                or ctx.role_artifact_ref is None or ctx.role_artifact_ref.id != "ordubashy"
                or not ctx.business_id or ctx.business_id not in ctx.allowed_business_ids):
            raise DomainError("forbidden", "Only scoped Global Ordubashy can propose delegated owner actions")
        # Derived reach of the active global, not an agent membership row.
        if not reaches_business(uow.connection, ctx.principal_id, ctx.business_id):
            raise DomainError("forbidden", "Delegated owner actions require current project reach")

    def prepare(self, ctx, args, options, uow):
        self.guard(ctx, None, uow)
        if not args["rationale"].strip():
            raise DomainError("validation_error", "Explain the requested owner action")
        action, parameters = args["action"], plain(args["parameters"])
        before = uow.repository("businesses").get(ctx.business_id, required=True)
        require_revision(before, options.expected_revision)
        config = plain(before["config_json"])
        if action == ENGINEERING:
            validate_authority(parameters)
            key, updated = "engineering", parameters
            effects = ["GitHub organizations authorize all repositories in each listed organization. "
                       "Repository entries authorize those explicit repositories. Empty deployment targets grant no standing deployment authority."]
        else:
            raise DomainError("validation_error", "Unsupported delegated owner action")
        builder = PlanBuilder(ctx, OPERATION, args, "Update " + before["name"] + " " + key + " settings", clock=self.clock)
        builder.record("businesses", ctx.business_id, before, {"config_json": {**config, key: updated}}, label=before["name"])
        # The existing Signoff UI renders effects, not the structured field diff.
        # The actual values must be visible independently of agent-authored prose.
        prior = config.get(key) or {}
        effects.extend(field + ": " + json.dumps(prior.get(field), ensure_ascii=False)
                       + " → " + json.dumps(value, ensure_ascii=False)
                       for field, value in updated.items() if prior.get(field) != value)
        builder.effects.extend(effects)
        return builder.build()

    def apply(self, ctx, plan, uow):
        self.guard(ctx, plan, uow)
        apply_records(ctx, plan, uow)
        action = plan.arguments["action"]
        business = uow.repository("businesses").get(ctx.business_id, required=True)
        key = "engineering"
        return {"action": action, "business_id": ctx.business_id, "revision": business["revision"], key: business["config_json"][key]}
