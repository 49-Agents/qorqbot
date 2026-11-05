"""A closed set of Ordubashy proposals, applied only by exact owner signoff.

Never impersonate a human or dispatch caller-selected operations/endpoints.
Existing human-only APIs retain their checks. Adding an action requires review
of its frozen effects, validation, attribution and approval-time scope checks.
"""

import json

from bazarlyk.agents.engineering import validate_authority
from bazarlyk.agents.global_scope import reaches_business
from qorqut_operations.application import OperationDescriptor
from bazarlyk.browser.config import prepare_settings, validate_applied
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision, schema, utc_clock

OPERATION = "owner_action_propose"
ENGINEERING = "engineering_update"
BROWSER = "browser_update"
NAMES = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 300},
         "maxItems": 100, "uniqueItems": True}


class OwnerActions:
    def __init__(self, paths, *, clock=utc_clock):
        self.paths, self.clock = paths, clock

    def register(self, registry):
        actions = {
            ENGINEERING: schema({key: NAMES for key in ("github_organizations", "repositories", "deployment_targets")},
                                ("github_organizations", "repositories", "deployment_targets")),
            BROWSER: schema({"enabled": {"type": "boolean"}, "proxy_server": {"type": "string", "maxLength": 2048},
                             "mode": {"enum": ["isolated-desktop-v1", "legacy"]},
                             "expected_exit_ip": {"type": "string", "maxLength": 45}},
                            ("enabled", "proxy_server")),
        }
        arguments = schema({"action": {"enum": list(actions)}, "parameters": {"type": "object"},
                            "rationale": {"type": "string", "minLength": 1, "maxLength": 4000}},
                           ("action", "parameters", "rationale"))
        arguments["oneOf"] = [{"properties": {"action": {"const": action}, "parameters": fields}}
                              for action, fields in actions.items()]
        registry.register(OperationDescriptor(OPERATION, arguments,
            description="Ordubashy only: propose engineering_update or browser_update "
                "in an authorized project. Supply expected_revision of the project config, parameters "
                "and rationale. browser_update takes enabled and proxy_server; the explicit isolated desktop mode "
                "adds mode 'isolated-desktop-v1' and a required expected_exit_ip (an IPv4 proxy already assigned to "
                "another project is refused; a mode change needs the browser stopped and unleased). "
                "Always stages an exact-change owner Signoff; never applies on chat consent or "
                "an allow override. No signoff approvals, credentials, shell commands or arbitrary owner actions.",
            prepare=self.prepare, apply=self.apply, validate_targets=self.guard))

    def guard(self, ctx, plan, uow):
        if (ctx.principal_kind != "agent" or ctx.home_business_id is not None
                or ctx.role_artifact_ref is None or ctx.role_artifact_ref.id != "ordubashy"
                or not ctx.business_id or ctx.business_id not in ctx.allowed_business_ids):
            raise DomainError("forbidden", "Only scoped Global Ordubashy can propose delegated owner actions")
        # Derived reach of the active global, not an agent membership row.
        if not reaches_business(uow.connection, ctx.principal_id, ctx.business_id):
            raise DomainError("forbidden", "Delegated owner actions require current project reach")
        if plan is not None and plan.arguments.get("action") == BROWSER:
            # A Signoff can be approved long after preparation, and another
            # business may have claimed the same proxy IP meanwhile.
            before = uow.repository("businesses").get(ctx.business_id, required=True)["config_json"]
            proposed = next((plain(change.values["config_json"]).get("browser") for change in plan.changes
                             if change.resource.resource_type == "businesses"), None)
            validate_applied(proposed, before.get("browser"), paths=self.paths, business_id=ctx.business_id, uow=uow)

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
        elif action == BROWSER:
            key = "browser"
            updated = prepare_settings(parameters, config.get(key), paths=self.paths, business_id=ctx.business_id, uow=uow)
            effects = ["Updates only saved browser settings. Ports and persona paths are assigned by the service. "
                       "Does not launch/restart Chrome, provision proxy credentials, or authorize browser use."]
            if updated.get("mode") == "isolated-desktop-v1":
                effects.append("Selects the isolated desktop browser: one persistent Chrome in a loopback-only network "
                               "namespace, reachable only through the fixed proxy " + updated["proxy_server"]
                               + " (expected exit " + updated["expected_exit_ip"] + "). The operator provisions its host "
                               "services and credentials separately; a proxy IP already assigned to another project is refused.")
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
        key = "engineering" if action == ENGINEERING else "browser"
        return {"action": action, "business_id": ctx.business_id, "revision": business["revision"], key: business["config_json"][key]}
