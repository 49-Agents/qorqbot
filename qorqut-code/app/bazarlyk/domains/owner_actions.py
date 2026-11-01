"""A closed set of Ordubashy proposals, applied only by exact owner signoff.

Never impersonate a human or dispatch caller-selected operations/endpoints.
Existing human-only APIs retain their checks. Adding an action requires review
of its frozen effects, validation, attribution and approval-time scope checks.
"""

from dataclasses import replace
import json

from bazarlyk.agents.engineering import validate_authority
from bazarlyk.application import OperationDescriptor
from bazarlyk.browser.config import prepare_settings, validate_applied
from bazarlyk.contracts import DomainError, plain
from .common import PlanBuilder, apply_records, require_revision, schema, utc_clock

OPERATION = "owner_action_propose"
ENGINEERING = "engineering_update"
BROWSER = "browser_update"
STORIES = {"story_close": "stories_close", "story_reopen": "stories_reopen"}
NAMES = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 300},
         "maxItems": 100, "uniqueItems": True}


class OwnerActions:
    def __init__(self, root, stories, *, clock=utc_clock):
        self.root, self.stories, self.clock = root, stories, clock

    def register(self, registry):
        actions = {
            ENGINEERING: schema({key: NAMES for key in ("github_organizations", "repositories", "deployment_targets")},
                                ("github_organizations", "repositories", "deployment_targets")),
            BROWSER: schema({"enabled": {"type": "boolean"}, "proxy_server": {"type": "string", "maxLength": 2048},
                             "mode": {"enum": ["isolated-desktop-v1", "legacy"]},
                             "expected_exit_ip": {"type": "string", "maxLength": 45}},
                            ("enabled", "proxy_server")),
            **{action: plain(registry.get(operation).input_schema) for action, operation in STORIES.items()},
        }
        arguments = schema({"action": {"enum": list(actions)}, "parameters": {"type": "object"},
                            "rationale": {"type": "string", "minLength": 1, "maxLength": 4000}},
                           ("action", "parameters", "rationale"))
        arguments["oneOf"] = [{"properties": {"action": {"const": action}, "parameters": fields}}
                              for action, fields in actions.items()]
        registry.register(OperationDescriptor(OPERATION, arguments,
            description="Ordubashy only: propose engineering_update, browser_update, story_close or story_reopen "
                "in an authorized business. Supply expected_revision of the business config or Story, parameters "
                "and rationale. browser_update takes enabled and proxy_server; the explicit isolated desktop mode "
                "adds mode 'isolated-desktop-v1' and a required expected_exit_ip (an IPv4 proxy already assigned to "
                "another business is refused; a mode change needs the browser stopped and unleased). "
                "Always stages an exact-change owner Signoff; never applies on chat consent or "
                "an allow override. No signoff approvals, credentials, shell commands or arbitrary owner actions.",
            prepare=self.prepare, apply=self.apply, validate_targets=self.guard))

    def guard(self, ctx, plan, uow):
        if (ctx.principal_kind != "agent" or ctx.home_business_id is not None
                or ctx.role_artifact_ref is None or ctx.role_artifact_ref.id != "ordubashy"
                or not ctx.business_id or ctx.business_id not in ctx.allowed_business_ids):
            raise DomainError("forbidden", "Only scoped Global Ordubashy can propose delegated owner actions")
        member = uow.repository("business_memberships", business_id=ctx.business_id).get(ctx.principal_id)
        if not member or not member["active"]:
            raise DomainError("forbidden", "Delegated owner actions require current business membership")
        if plan is not None and plan.arguments.get("action") == BROWSER:
            # A Signoff can be approved long after preparation, and another
            # business may have claimed the same proxy IP meanwhile.
            before = uow.repository("businesses").get(ctx.business_id, required=True)["config_json"]
            proposed = next((plain(change.values["config_json"]).get("browser") for change in plan.changes
                             if change.resource.resource_type == "businesses"), None)
            validate_applied(proposed, before.get("browser"), root=self.root, business_id=ctx.business_id, uow=uow)

    def prepare(self, ctx, args, options, uow):
        self.guard(ctx, None, uow)
        if not args["rationale"].strip():
            raise DomainError("validation_error", "Explain the requested owner action")
        action, parameters = args["action"], plain(args["parameters"])
        if action in STORIES:
            plan = self.stories.prepare(STORIES[action], ctx, parameters, options, uow)
            story = self.stories.get(uow, ctx, parameters["story_id"])
            effects = ["State: " + story["state"] + " → " + ("closed" if action == "story_close" else parameters["state"])]
            if action == "story_close":
                effects.append("Outcome: " + parameters["outcome"])
            if parameters.get("state_reason"):
                effects.append("State reason: " + parameters["state_reason"])
            if parameters.get("note"):
                effects.append("Append note: " + parameters["note"])
            preview = {**plain(plan.preview), "title": ("Close" if action == "story_close" else "Reopen") + " Story: " + story["title"],
                       "rationale": args["rationale"], "effects": effects}
            return replace(plan, operation_name=OPERATION, arguments=args, preview=preview)
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
            updated = prepare_settings(parameters, config.get(key), root=self.root, business_id=ctx.business_id, uow=uow)
            effects = ["Updates only saved browser settings. Ports and persona paths are assigned by the service. "
                       "Does not launch/restart Chrome, provision proxy credentials, or authorize browser use."]
            if updated.get("mode") == "isolated-desktop-v1":
                effects.append("Selects the isolated desktop browser: one persistent Chrome in a loopback-only network "
                               "namespace, reachable only through the fixed proxy " + updated["proxy_server"]
                               + " (expected exit " + updated["expected_exit_ip"] + "). The operator provisions its host "
                               "services and credentials separately; a proxy IP already assigned to another business is refused.")
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
        if action in STORIES:
            story = self.stories.get(uow, ctx, plan.arguments["parameters"]["story_id"])
            return {"action": action, "story_id": story["id"], "state": story["state"], "revision": story["revision"]}
        business = uow.repository("businesses").get(ctx.business_id, required=True)
        key = "engineering" if action == ENGINEERING else "browser"
        return {"action": action, "business_id": ctx.business_id, "revision": business["revision"], key: business["config_json"][key]}
