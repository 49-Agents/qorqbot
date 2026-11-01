"""Canonical profile reads and deterministic owner-controlled configuration edits."""

from __future__ import annotations

from qorqut_operations.departments import department_id, profile_department

import base64
import difflib
import json
from uuid import UUID

from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget, FieldChange,
                                         PreparedChange, PreviewResource, Principal, ResourceRef, StructuredPreview,
                                         TextPreview, canonical_bytes, new_id, plain, utc_now)
from bazarlyk.composition.locations import agent_workspace, document_root
from . import marketing_plugins, roles
from .models import AgentProfile, PLUGIN_GRANT_SCHEMA, PROFILE_SCHEMA, REFERENCE_SCHEMA, RETIRED_ROLE_IDS, SCOPE_SCHEMA
from bazarlyk.storage import history
from qorqut_operations.storage.files import RegisteredFileTarget, file_hash, file_values
from bazarlyk.management.identity import require_protected_control
from .global_peers import protect_peer_control
from .resolver import effective_tool_policy

UUID_SCHEMA = {"type": "string", "format": "uuid"}
EMAIL_MANAGEMENT_PLUGIN_ID = marketing_plugins.EMAIL_PLUGIN
DEFAULT_EMAIL_MANAGEMENT_CONFIG = marketing_plugins.default_config(EMAIL_MANAGEMENT_PLUGIN_ID)
EDITABLE = {key: value for key, value in PROFILE_SCHEMA["properties"].items()
            if key in {"name", "display_name", "runtime", "role", "scope", "assignment_brief", "plugins", "tool_policy", "presentation"}}


def published_email_management_pin(connection):
    """Exact reference to the newest published email-management v2 artifact, or None."""
    return marketing_plugins.published_pin(connection, EMAIL_MANAGEMENT_PLUGIN_ID)


class AgentOperations:
    def __init__(self, resolver):
        self.resolver = resolver

    def _default_grants(self, role_id, scope, uow):
        return marketing_plugins.default_grants(self.resolver, uow.connection, role_id, scope)

    def _validate_initial_defaults(self, plan, uow):
        change = plan.changes[0]
        if "default_plugin_grants" not in change.values:
            return
        profile = change.values["profile"]
        grants = plain(change.values["default_plugin_grants"])
        if (plan.operation_name != "agents_create" or change.action != "create_profile"
                or "plugins" in plan.arguments or plain(profile["plugins"]) != grants):
            raise DomainError("invalid_plan", "Prepared initial grant defaults do not match the initial profile")
        if self._default_grants(profile["role"]["id"], plain(profile["scope"]), uow) != grants:
            raise DomainError("revision_conflict", "The published plugin defaults changed; prepare agent creation again")

    @staticmethod
    def _validate_role_runtime(profile):
        if profile.data["role"]["id"] == roles.ORDUBASHY_ROLE and profile.data["runtime"] != "codex":
            raise DomainError("invalid_profile", "Ordubashy profiles require the Codex runtime")

    @staticmethod
    def _validate_role_lifecycle(operation, profile):
        """Keep retired role references readable while fencing active recreation."""
        role_id = profile.data["role"]["id"]
        if role_id in RETIRED_ROLE_IDS and not (operation == "agents_deactivate" and profile.data["status"] == "inactive"):
            raise DomainError("retired_role", "This role is retired; businesses use the business-agent and dev roles")

    @staticmethod
    def _validate_roster(operation, profile, before, uow):
        """At most one active profile per business role; each keeps its fixed name, title and home."""
        business_id = profile.scope.business_id
        if business_id is None:
            return
        role_id = profile.role.id
        if profile.data["status"] == "active" and role_id not in roles.BUSINESS_ROLES:
            raise DomainError("invalid_profile", "A business's agents use the business-agent or dev role")
        if operation == "agents_create":
            if roles.business_role_agent(uow.connection, business_id, role_id) is not None:
                raise DomainError("roster_conflict", f"This business already has its {roles.role_display_name(role_id)} agent")
            return
        if before is None:
            return
        if before.scope.business_id != business_id:
            raise DomainError("forbidden", "A business agent cannot move to another business")
        if before.role.id != role_id and (before.role.id in roles.BUSINESS_ROLES or role_id in roles.BUSINESS_ROLES):
            raise DomainError("invalid_profile", "A business agent keeps its role; create the other peer instead")
        if role_id in roles.BUSINESS_ROLES and operation == "agents_update" and (
                before.data["name"] != profile.data["name"] or (
                    before.data["display_name"] != profile.data["display_name"]
                    and profile.data["display_name"] != roles.role_display_name(role_id))):
            raise DomainError("invalid_profile", "A business agent keeps its profile name and fixed display name")
        if profile.data["status"] != "active" and before.data["status"] == "active":
            business = uow.connection.execute("SELECT archived_at FROM businesses WHERE id=?", (business_id,)).fetchone()
            others = [row for row in roles.active_home_profiles(uow.connection, business_id)
                      if row["agent_id"] != profile.agent_id and roles.profile_role(row) == before.role.id]
            if business is not None and business["archived_at"] is None and before.role.id in roles.BUSINESS_ROLES and not others:
                raise DomainError("roster_conflict", "A business keeps its Onbashy and Dev agents; archive the business instead")

    def register(self, registry, application=None):
        if application:
            from .briefing import WorkBriefing
            self.resolver.work_briefing = WorkBriefing(application)
        read_schemas = {
            "agent_config_get": {"business_id": UUID_SCHEMA}, "agents_list": {"include_inactive": {"type": "boolean"}, "scope": {"enum": ["business", "global"]}},
            "agents_get": {"agent_id": UUID_SCHEMA}, "permissions_get": {"agent_id": UUID_SCHEMA},
            "roles_list": {}, "roles_get": {"role": REFERENCE_SCHEMA}, "plugins_list": {},
        }
        for name, properties in read_schemas.items():
            required = ["agent_id"] if name == "agents_get" else ["role"] if name == "roles_get" else []
            registry.register(OperationDescriptor(name, {"type": "object", "properties": properties, "required": required},
                description=("Read your current identity, role, grants and business orientation. Select business_id only within your allowed scope. Reading never acknowledges context." if name == "agent_config_get" else ""),
                effect_kind="read", supports_signoff=False, scope_kinds=frozenset({"business", "global", "agent_workspace"}),
                global_capability="read_global_context",
                resolve_namespace=self.roster_namespace if name == "agents_list" else None,
                read=lambda ctx, args, options, uow, operation=name: self.read(operation, ctx, args, options, uow)))
        mutations = {
            "agents_create": ({**EDITABLE, "scope": SCOPE_SCHEMA, "runtime": {"enum": ["claude-code", "codex"]}}, ["role"]),
            "agents_update": ({"agent_id": UUID_SCHEMA, "changes": {"type": "object", "additionalProperties": False, "properties": EDITABLE, "minProperties": 1}}, ["agent_id", "changes"]),
            "agents_deactivate": ({"agent_id": UUID_SCHEMA}, ["agent_id"]),
            "permissions_set": ({"agent_id": UUID_SCHEMA, "overrides": PROFILE_SCHEMA["properties"]["tool_policy"]["properties"]["overrides"]}, ["agent_id", "overrides"]),
            "plugins_enable": ({"agent_id": UUID_SCHEMA, "plugin": PLUGIN_GRANT_SCHEMA}, ["agent_id", "plugin"]),
            "plugins_enable_delegated": ({"agent_id": UUID_SCHEMA, "plugin": PLUGIN_GRANT_SCHEMA}, ["agent_id", "plugin"]),
            "plugins_disable": ({"agent_id": UUID_SCHEMA, "plugin_id": {"type": "string"}}, ["agent_id", "plugin_id"]),
        }
        for name, (properties, required) in mutations.items():
            registry.register(OperationDescriptor(name,
                {"type": "object", "properties": {**properties, "rationale": {"type": "string", "maxLength": 2000}}, "required": required},
                description=("Create a Global agent (name, display_name, assignment_brief required), or a missing business peer: the business-agent role (Onbashy) or the dev role (Dev), each with its fixed name and display name. When plugins is omitted, business agents and Global Marketing get every published marketing plugin, Dev gets none and other agents with business access the email toolkit; an explicit list, even empty, replaces the defaults. Explicit plugin selections retain their separate permission checks." if name == "agents_create" else
                    "Scoped coordinator: apply an exact owner-defined mailbox_grant_delegations entry from business_config_get. Requires its exact plugin pin/config, recipient role and unexpired ceiling; no general capability or permission delegation. Use plugins_enable for a grant outside that ceiling." if name == 'plugins_enable_delegated' else ""),
                effect_kind="compound" if name == "agents_create" else "records", scope_kinds=frozenset({"business", "global"}),
                global_capability="manage_ecosystem_agents",
                resolve_namespace=self.mutation_namespace,
                prepare=lambda ctx, args, options, uow, operation=name: self.prepare(operation, ctx, args, options, uow),
                apply=self.apply, validate_targets=self.validate_targets))
        if application:
            self.register_targets(application)

    def register_targets(self, application):
        application.register_target_resolver("agent_names", self.resolve_name)
        application.register_target_resolver("agent_bootstrap_files", self.resolve_file_snapshot)

    @staticmethod
    def roster_namespace(ctx, arguments):
        if arguments.get("scope", "business") == "global":
            if ctx.business_id is not None or ctx.home_business_id is not None:
                raise DomainError("forbidden", "Select Global to read its agent roster")
            return "global"
        if ctx.business_id is None:
            raise DomainError("scope_required", "Select a permitted business or explicitly request the Global roster")
        return "business:" + ctx.business_id

    def mutation_namespace(self, ctx, arguments):
        if "agent_id" in arguments:
            with self.resolver.database.read() as uow:
                row = uow.connection.execute("SELECT home_business_id FROM agent_profiles WHERE agent_id=?", (arguments["agent_id"],)).fetchone()
            if row is not None and row["home_business_id"] is None:
                return "global"
        if arguments.get("scope", {}).get("kind") == "ecosystem":
            return "global"
        return "business:" + ctx.business_id if ctx.business_id else "global"

    def _access(self, ctx, row):
        if row is None:
            raise DomainError("not_found", "Agent profile does not exist")
        home = row["home_business_id"]
        if home is not None:
            if home != ctx.business_id or home not in ctx.allowed_business_ids:
                raise DomainError("forbidden", "Agent is outside the selected business")
        elif ctx.home_business_id is not None or not ctx.global_capabilities:
            raise DomainError("forbidden", "Ecosystem agent requires explicit global scope")

    def owner_config(self, ctx, agent_id):
        with self.resolver.database.read() as uow:
            membership = uow.repository("business_memberships", business_id=ctx.business_id).get(ctx.principal_id) if ctx.business_id else None
            if ctx.principal_kind != "human" or not membership or not membership["active"] or not membership["is_owner"]:
                raise DomainError("forbidden", "Configuration inspection requires the selected business owner")
            row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
            self._access(ctx, row)
        return self.resolver.resolve_agent_config(Principal(agent_id, "agent"), business_id=ctx.business_id).to_dict()

    def profile_view(self, row):
        value = json.loads(row["profile_json"])
        if row["status"] == "retired":
            label = value.get("display_name") if isinstance(value, dict) else None
            return {"agent_id": row["agent_id"], "name": row["name"],
                "display_name": label if isinstance(label, str) and label else row["name"], "status": "retired",
                "profile_revision": row["revision"], "archival_record": True}
        result = AgentProfile.from_dict(value, self.resolver.registry).to_dict()
        result["presentation"] = {**result.get("presentation", {}), "department": profile_department(result)}
        return result

    def read(self, operation, ctx, args, options, uow):
        if operation == "agent_config_get":
            if ctx.agent_id is None:
                raise DomainError("invalid_principal", "Self configuration requires an authenticated agent")
            config = self.resolver.resolve_agent_config(ctx.principal,
                business_id=args.get("business_id", ctx.business_id)).to_dict()
            result = {"effective_config": config}
            # MCP compact/context projections discard the live briefing. Avoid
            # assembling it first: its domain reads can be slow or unavailable
            # even when the mandatory configuration is healthy.
            if not options.omit_work_briefing:
                result["work_briefing"] = self.resolver.briefing(ctx.principal, config)
            return result
        if operation in {"roles_list", "plugins_list"}:
            kind = "role" if operation == "roles_list" else "plugin"
            # Retired package bytes remain available to exact historical pins, but
            # the owner must not be offered an overlapping obsolete capability.
            retired = sorted(RETIRED_ROLE_IDS if kind == "role" else roles.RETIRED_PLUGIN_IDS)
            rows = uow.connection.execute("SELECT kind,artifact_id,version,digest FROM artifacts WHERE kind=? "
                f"AND artifact_id NOT IN ({','.join('?' * len(retired))}) ORDER BY artifact_id,version",
                (kind, *retired))
            return {"roles" if kind == "role" else "plugins": [{"id": row["artifact_id"], "version": row["version"], "digest": row["digest"]} for row in rows]}
        if operation == "roles_get":
            package = self.resolver.catalog.get("role", args["role"], connection=uow.connection)
            return {"role": {**package.ref, "manifest": plain(package.manifest), "brief": package.files[package.manifest["brief"]].decode()}}
        if operation == "agents_list":
            if args.get("scope") == "global":
                rows = uow.connection.execute("SELECT * FROM agent_profiles WHERE home_business_id IS NULL AND (? OR status='active') ORDER BY name,agent_id", (bool(args.get("include_inactive")),))
                return {"agents": [self.profile_view(row) for row in rows]}
            if ctx.business_id is None or ctx.business_id not in ctx.allowed_business_ids:
                raise DomainError("scope_required", "Select a permitted business to read its roster")
            rows = uow.connection.execute("SELECT * FROM agent_profiles WHERE home_business_id=? AND (? OR status='active') ORDER BY name,agent_id", (ctx.business_id, bool(args.get("include_inactive"))))
            return {"agents": [self.profile_view(row) for row in rows]}
        identity = args.get("agent_id", ctx.agent_id)
        row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()
        self._access(ctx, row)
        if operation == "agents_get":
            return {"agent": self.profile_view(row)}
        if row["status"] == "retired":
            raise DomainError("inactive_agent", "Retired archival records have no active permission configuration")
        profile = AgentProfile.from_dict(json.loads(row["profile_json"]), self.resolver.registry)
        role = self.resolver.catalog.get("role", profile.data["role"], connection=uow.connection)
        return {"agent_id": identity, "profile_revision": profile.revision,
                "overrides": plain(profile.data["tool_policy"]["overrides"]), "tools": effective_tool_policy(profile, role, self.resolver.registry)}

    def resolve_name(self, ctx, resource, uow):
        try:
            namespace, name = json.loads(resource.resource_id)
        except (ValueError, TypeError) as exc:
            raise DomainError("invalid_plan", "Invalid profile name expectation") from exc
        return uow.connection.execute("SELECT revision,agent_id FROM agent_profiles WHERE namespace=? AND name=? AND status='active'", (namespace, name)).fetchone()

    def resolve_file_target(self, resource):
        if resource.resource_type != "agent_bootstrap_files":
            raise DomainError("invalid_plan", "Not a registered agent bootstrap file")
        try:
            agent_id, leaf = json.loads(resource.resource_id)
            if str(UUID(agent_id)) != agent_id or leaf != "PLAN.md":
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise DomainError("invalid_plan", "Invalid initial agent document reference") from exc
        path = f"agents/{agent_id}/{leaf}"
        if resource.business_id:
            with self.resolver.database.read() as uow:
                row = uow.connection.execute("SELECT slug FROM businesses WHERE id=?", (resource.business_id,)).fetchone()
            if row is None:
                raise DomainError("invalid_scope", "Initial document business is not registered")
        else:
            row = None
        return RegisteredFileTarget(document_root(self.resolver.paths, row, path), path)

    def resolve_file_snapshot(self, ctx, resource, uow):
        digest = file_hash(self.resolve_file_target(resource).resolve())
        return {"content_hash": digest} if digest is not None else None

    def prepare(self, operation, ctx, args, options, uow):
        if operation == 'plugins_enable_delegated':
            from .delegation import require
            require(self, ctx, args, uow)
        creating = operation == "agents_create"
        before, old = None, None
        default_selection = False
        default_grants = []
        if creating:
            scope = args.get("scope") or {"kind": "business", "business_id": ctx.business_id}
            if args["role"]["id"] in RETIRED_ROLE_IDS:
                raise DomainError("retired_role", "This role is retired; businesses use the business-agent and dev roles")
            if scope["kind"] == "business":
                if scope["business_id"] != ctx.business_id or scope["business_id"] not in ctx.allowed_business_ids:
                    raise DomainError("forbidden", "New agent must belong to the selected permitted business")
                role_id = args["role"]["id"]
                if role_id not in roles.BUSINESS_ROLES:
                    raise DomainError("invalid_profile", "A business's agents use the business-agent or dev role")
                if args.get("name", roles.role_name(role_id)) != roles.role_name(role_id) or "display_name" in args:
                    raise DomainError("validation_error", f"This business agent's name is {roles.role_name(role_id)}; its display name is {roles.role_display_name(role_id)}")
                if roles.business_role_agent(uow.connection, scope["business_id"], role_id) is not None:
                    raise DomainError("roster_conflict", f"This business already has its {roles.role_display_name(role_id)} agent")
                home = uow.connection.execute("SELECT slug FROM businesses WHERE id=?", (scope["business_id"],)).fetchone()
                name, display_name = roles.role_name(role_id), roles.role_display_name(role_id)
                assignment = args.get("assignment_brief", roles.role_assignment(role_id))
                presentation = args.get("presentation", {"department": roles.role_department(role_id)})
            else:
                if not {"name", "display_name", "assignment_brief"} <= set(args):
                    raise DomainError("validation_error", "A Global agent requires name, display_name and assignment_brief")
                if ctx.home_business_id is not None or not set(scope["allowed_business_ids"]).issubset(ctx.allowed_business_ids) or not set(scope["global_capabilities"]).issubset(ctx.global_capabilities):
                    raise DomainError("forbidden", "New ecosystem profile cannot exceed current granted scope")
                home = None
                name, display_name, assignment = args["name"], args["display_name"], args["assignment_brief"]
                presentation = args.get("presentation", {})
            # Omitted plugins select the defaults; an explicit list, even empty, replaces them.
            default_selection = "plugins" not in args
            if default_selection:
                default_grants = self._default_grants(args["role"]["id"], scope, uow)
            plugins = args.get("plugins", json.loads(json.dumps(default_grants)))
            identity = new_id()
            # The registered identity and home scope select the workspace; the name never does.
            workspace = agent_workspace(self.resolver.paths, identity, home)
            values = {"schema_version": 2, "agent_id": identity, "profile_revision": 1, "name": name,
                      "display_name": display_name, "status": "active", "runtime": args.get("runtime", "codex" if args['role']['id'] == roles.ORDUBASHY_ROLE else "claude-code"), "role": args["role"],
                      "scope": scope, "assignment_brief": assignment, "plugins": plugins,
                      "tool_policy": args.get("tool_policy", {"overrides": {}}), "presentation": presentation, "created_at": utc_now()}
        else:
            identity = args["agent_id"]
            old = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()
            self._access(ctx, old)
            # Profile, grant, runtime and retirement edits can disrupt Dev's session: owner only.
            protect_peer_control(ctx, identity, uow)
            require_protected_control(uow, ctx.principal_id, ctx.principal_kind, [identity])
            if old["status"] != "active":
                raise DomainError("inactive_agent", "Inactive profiles cannot be edited through active agent operations")
            if options.expected_revision != old["revision"]:
                raise DomainError("revision_conflict", "Current profile revision is required", current_revision=old["revision"])
            before = AgentProfile.from_dict(json.loads(old["profile_json"]), self.resolver.registry)
            if before.data["status"] != "active":
                raise DomainError("inactive_agent", "Inactive profiles cannot be edited through active agent operations")
            values = before.to_dict()
            values["profile_revision"] += 1
            home = (uow.connection.execute("SELECT slug FROM businesses WHERE id=?", (old["home_business_id"],)).fetchone()
                    if old["home_business_id"] else None)
            workspace = agent_workspace(self.resolver.paths, identity, home)
            if operation == "agents_update":
                values.update(args["changes"])
            elif operation == "permissions_set":
                values["tool_policy"] = {"overrides": args["overrides"]}
            elif operation in {"plugins_enable", "plugins_enable_delegated"}:
                values["plugins"] = [item for item in values["plugins"] if item["id"] != args["plugin"]["id"]] + [args["plugin"]]
            elif operation == "plugins_disable":
                values["plugins"] = [item for item in values["plugins"] if item["id"] != args["plugin_id"]]
            else:
                self._retirement_ready(identity, before, uow)
                values["status"] = "inactive"
        if operation == "agents_create" or operation == "agents_update" and "presentation" in args["changes"]:
            presentation = values.get("presentation", {})
            if "department" in presentation:
                values["presentation"] = {**presentation, "department": department_id(presentation["department"])}
        profile = AgentProfile.from_dict(values, self.resolver.registry)
        self._validate_role_lifecycle(operation, profile)
        self._validate_roster(operation, profile, before, uow)
        if profile.data['status'] == 'active':
            self._validate_role_runtime(profile)
        self.resolver.packages_for_profile(profile, connection=uow.connection)
        if before and before.scope.to_dict() != profile.scope.to_dict():
            if (before.scope.kind != profile.scope.kind or before.scope.business_id != profile.scope.business_id
                or not profile.scope.businesses.issubset(ctx.allowed_business_ids)
                or not set(profile.scope.global_capabilities).issubset(ctx.global_capabilities)):
                raise DomainError("forbidden", "A scope edit cannot transfer the agent or exceed current authority")
        business_id = profile.scope.business_id
        namespace = business_id or "ecosystem"
        expected = [ExpectedTarget(ResourceRef("agent_profiles", identity, business_id, namespace), revision=before.revision if before else None, exists=not creating)]
        if creating or before.data["name"] != profile.data["name"]:
            name_ref = ResourceRef("agent_names", canonical_bytes([namespace, profile.data["name"]]).decode(), business_id, namespace)
            if self.resolve_name(ctx, name_ref, uow):
                raise DomainError("duplicate_agent_name", "An active agent already uses this name in the selected scope")
            expected.append(ExpectedTarget(name_ref, exists=False))
        proposed = (profile, {"workspace": str(workspace)})
        snapshot = self.resolver._snapshot(Principal(identity, "agent"), business_id, proposed=proposed)
        effective = self.resolver._compose(snapshot)
        for document in snapshot["documents"].values():
            expected.append(ExpectedTarget(ResourceRef("documents", document["id"], document["business_id"], document["namespace"]), revision=document["revision"], content_hash=document["content_hash"]))
        for business in snapshot["businesses"]:
            expected.append(ExpectedTarget(ResourceRef("businesses", business["id"], business["id"]), revision=business["revision"]))
        result = {"agent_id": identity, "profile_revision": profile.revision, "overrides": plain(profile.data["tool_policy"]["overrides"]), "effective_config_id": effective.effective_config_id}
        resource = ResourceRef("agent_profiles", identity, business_id, namespace)
        profile_values = {"profile": profile.to_dict(), "workspace": str(workspace), "result": result}
        if default_selection:
            profile_values["default_plugin_grants"] = default_grants
        changes = [PreparedChange("record", resource, "create_profile" if creating else "replace_profile", profile_values)]
        principal_ref = ResourceRef("principals", identity, business_id, namespace)
        principal_row = uow.repository("principals").get(identity)
        expected.append(ExpectedTarget(principal_ref, revision=principal_row["revision"] if principal_row else None, exists=principal_row is not None))
        principal_values = {"display_name": profile.data["display_name"], "active": profile.data["status"] == "active"}
        if creating:
            principal_values.update({"id": identity, "kind": "agent", "created_at": profile.data["created_at"]})
        changes.append(PreparedChange("record", principal_ref, "insert_principal" if creating else "update_principal", principal_values))
        membership_ids = profile.scope.businesses | (before.scope.businesses if before else frozenset())
        for allowed in sorted(membership_ids):
            membership_ref = ResourceRef("business_memberships", identity, allowed, allowed)
            membership = uow.repository("business_memberships", business_id=allowed).get(identity)
            active = profile.data["status"] == "active" and allowed in profile.scope.businesses
            if membership is None or bool(membership["active"]) != active:
                expected.append(ExpectedTarget(membership_ref, revision=membership["revision"] if membership else None, exists=membership is not None))
                member_values = {"active": active}
                if membership is None:
                    member_values.update({"business_id": allowed, "principal_id": identity, "is_owner": False, "created_at": profile.data["created_at"]})
                changes.append(PreparedChange("record", membership_ref, "update_membership" if membership else "insert_membership", member_values))
        if profile.data["status"] != "active":
            for credential in uow.repository("credentials").list(filters={"principal_id": identity}, limit=200):
                if credential["revoked_at"] is None:
                    credential_ref = ResourceRef("credentials", credential["id"], business_id, namespace)
                    expected.append(ExpectedTarget(credential_ref, revision=credential["revision"]))
                    changes.append(PreparedChange("record", credential_ref, "revoke_credential", {"revoked_at": utc_now()}))
        if creating:
            plan_ref = ResourceRef("agent_bootstrap_files", canonical_bytes([identity, "PLAN.md"]).decode(), business_id, namespace)
            target = self.resolve_file_target(plan_ref).resolve()
            if target.exists() or workspace.exists():
                raise DomainError("workspace_conflict", "A new agent requires an unused registered folder")
            plan_bytes = b"# Plan\n\n## Goal\nUnknown: first working goal has not been recorded.\n\n## Now\nNo tasks selected yet.\n"
            document_id = new_id()
            expected += [ExpectedTarget(plan_ref, exists=False), ExpectedTarget(ResourceRef("documents", document_id, business_id, namespace), exists=False)]
            changes += [PreparedChange("file", plan_ref, "write", file_values(plan_bytes, before_hash=None)),
                PreparedChange("record", ResourceRef("documents", document_id, business_id, namespace), "register_plan",
                    {"id": document_id, "business_id": business_id, "namespace": namespace, "owning_agent_id": identity,
                     "kind": "plan", "path": f"agents/{identity}/PLAN.md", "content_hash": file_values(plan_bytes, before_hash=None)["after_hash"],
                     "metadata_json": {"authored": True}})]
        old_data = before.to_dict() if before else {}
        previous_plugins = {item["id"]: item for item in old_data.get("plugins", [])}
        next_plugins = {item["id"]: item for item in profile.to_dict()["plugins"]}
        effects = []
        if old_data.get("tool_policy", {"overrides": {}}) != profile.to_dict()["tool_policy"] and operation != "permissions_set":
            effects.append(("permissions_set", "tool_policy", old_data.get("tool_policy", {"overrides": {}}), profile.to_dict()["tool_policy"]))
        # Initial published-role selection is inherent in agents_create. Later
        # role changes still require the separate permissions_set authority.
        if before and old_data["role"] != profile.to_dict()["role"]:
            effects.append(("permissions_set", "role", old_data.get("role"), profile.to_dict()["role"]))
        if before and old_data["scope"] != profile.to_dict()["scope"]:
            effects.append(("permissions_set", "scope", old_data["scope"], profile.to_dict()["scope"]))
        for plugin_id in sorted(previous_plugins.keys() | next_plugins.keys()):
            if previous_plugins.get(plugin_id) != next_plugins.get(plugin_id):
                # Default grants are inherent in agents_create; explicit selections are not.
                if next_plugins.get(plugin_id) in default_grants:
                    continue
                child = "plugins_enable" if plugin_id in next_plugins else "plugins_disable"
                if child != operation and not (operation == 'plugins_enable_delegated' and child == 'plugins_enable'):
                    effects.append((child, "plugins." + plugin_id, previous_plugins.get(plugin_id), next_plugins.get(plugin_id)))
        for child, field, prior, after in effects:
            changes.append(PreparedChange("record", resource, "profile_effect", {"field": field, "before": prior, "after": after}, operation_name=child))
        preview_resource = PreviewResource("agent_profiles", identity, profile.data["display_name"])
        fields = [FieldChange(key, old_data.get(key), value) for key, value in profile.to_dict().items() if old_data.get(key) != value]
        fields.append(FieldChange("effective_permissions", effective_tool_policy(before, self.resolver.catalog.get("role", before.data["role"]), self.resolver.registry) if before else None, effective.to_dict()["tools"]))
        if profile.data["status"] != "active":
            fields.append(FieldChange("credentials", "active scoped credentials", "revoked; history and documents preserved"))
        previews = [StructuredPreview(preview_resource, tuple(fields))]
        resources = [preview_resource]
        if creating:
            plan_resource = PreviewResource("documents", document_id, f"{profile.data['name']}/PLAN.md")
            plan_text = plan_bytes.decode()
            resources.append(plan_resource)
            previews.append(TextPreview(plan_resource, "", plan_text,
                "".join(difflib.unified_diff([], plan_text.splitlines(keepends=True), fromfile="/dev/null", tofile="PLAN.md"))))
        return ChangeSet(operation, 1, ctx.principal_id, "business:" + business_id if business_id else "global", args,
            {"kind": "global" if profile.scope.kind == "ecosystem" else "business", "business_ids": sorted(profile.scope.businesses), "agent_id": identity},
            tuple(expected), tuple(changes), ChangePreview(f"{operation.replace('_', ' ').title()}: {profile.data['display_name']}", args.get("rationale", ""),
                {"kind": "global" if profile.scope.kind == "ecosystem" else "business", "business_ids": sorted(profile.scope.businesses)}, tuple(resources), tuple(previews),
                ("Current database profile changes only when applied. Project preparation reconciles generated files without launching a session.",)),
            review_business_id=options.review_business_id or ctx.review_business_id or business_id, proposer_agent_id=ctx.agent_id)

    def validate_targets(self, ctx, plan, uow):
        from bazarlyk.catalog.packages import content_hash, relative_path
        if plan.operation_name == 'plugins_enable_delegated':
            from .delegation import require
            require(self, ctx, plain(plan.arguments), uow)
        profile = AgentProfile.from_dict(plan.changes[0].values["profile"], self.resolver.registry)
        if plan.operation_name != "agents_create":
            protect_peer_control(ctx, profile.agent_id, uow)
            require_protected_control(uow, ctx.principal_id, ctx.principal_kind, [profile.agent_id])
        self._validate_role_lifecycle(plan.operation_name, profile)
        self._validate_initial_defaults(plan, uow)
        self._recheck_roster(plan, profile, uow)
        self._validate_role_runtime(profile)
        self.resolver.packages_for_profile(profile, connection=uow.connection)
        for expectation in plan.expected_targets:
            resource = expectation.resource
            if resource.resource_type != "documents" or not expectation.exists:
                continue
            row = uow.repository("documents", business_id=resource.business_id).get(resource.resource_id, required=True)
            business = uow.repository("businesses").get(resource.business_id, required=True) if resource.business_id else None
            base = document_root(self.resolver.paths, business, row["path"])
            path = base / relative_path(row["path"])
            if row["availability"] != "ready" or path.is_symlink() or not base.resolve().is_relative_to(self.resolver.paths.operations) or not path.resolve().is_relative_to(base.resolve()):
                raise DomainError("document_conflict", "Reviewed mandatory source is no longer available at its registered path")
            try:
                from qorqut_operations.storage.safe_io import read_bytes
                digest = content_hash(read_bytes(path))
            except OSError as exc:
                raise DomainError("document_conflict", "Reviewed mandatory source is no longer readable") from exc
            if digest.removeprefix("sha256:") != (expectation.content_hash or "").removeprefix("sha256:"):
                raise DomainError("document_conflict", "Reviewed mandatory source changed outside the canonical document path")

    def _retirement_ready(self, identity, profile, uow):
        from qorqut_operations.tasks import CLOSED_STATES

        closed = set(CLOSED_STATES)
        for business_id in profile.scope.businesses:
            open_tasks = [row["id"] for row in uow.connection.execute("SELECT t.id,t.status FROM tasks t WHERE t.business_id=? AND t.archived=0 AND EXISTS (SELECT 1 FROM relationships e WHERE e.business_id=t.business_id AND e.from_type='task' AND e.from_id=t.id AND e.role='assigned_to' AND e.to_type='agent' AND e.to_id=?)", (business_id, identity)) if row["status"] not in closed]
            if open_tasks:
                raise DomainError("agent_has_open_tasks", "Reassign or complete outstanding tasks before retiring this agent", details={"business_id": business_id, "task_ids": open_tasks})

    def _recheck_roster(self, plan, profile, uow):
        change = plan.changes[0]
        row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (profile.agent_id,)).fetchone()
        before = AgentProfile.from_dict(json.loads(row["profile_json"]), self.resolver.registry) if row else None
        self._validate_roster(plan.operation_name, profile, before, uow)

    def apply(self, ctx, plan, uow):
        self._validate_initial_defaults(plan, uow)
        change = plan.changes[0]
        profile = AgentProfile.from_dict(change.values["profile"], self.resolver.registry)
        identity = profile.agent_id
        business_id = profile.scope.business_id
        repository = uow.repository("agent_profiles", business_id=business_id)
        expectations = {target.resource: target for target in plan.expected_targets}
        if change.action != "create_profile":
            protect_peer_control(ctx, identity, uow)
            require_protected_control(uow, ctx.principal_id, ctx.principal_kind, [identity])
        self._recheck_roster(plan, profile, uow)
        for effect in plan.changes:
            values = plain(effect.values)
            revision = expectations[effect.resource].revision
            if effect.action == "insert_principal":
                uow.repository("principals").insert(values)
            elif effect.action == "update_principal":
                uow.repository("principals").update(effect.resource.resource_id, values, expected_revision=revision)
        if change.action == "create_profile":
            repository.insert({"agent_id": identity, "principal_id": identity, "home_business_id": business_id,
                "namespace": business_id or "ecosystem", "name": profile.data["name"], "workspace": change.values["workspace"],
                "status": profile.data["status"], "profile_json": profile.to_dict(), "revision": profile.revision, "created_at": profile.data["created_at"]})
        elif change.action == "replace_profile":
            if profile.data["status"] != "active":
                self._retirement_ready(identity, profile, uow)
            repository.update(identity, {"name": profile.data["name"], "status": profile.data["status"], "profile_json": profile.to_dict()}, expected_revision=profile.revision - 1)
        else:
            raise DomainError("invalid_plan", "Unexpected profile application action")
        for document in plan.changes:
            if document.action == "register_plan":
                row = uow.repository("documents", business_id=business_id).insert(plain(document.values))
                content = next(effect.values["content_b64"] for effect in plan.changes
                               if effect.kind == "file" and effect.resource.resource_type == "agent_bootstrap_files")
                history.seed_document(uow.connection, row, base64.b64decode(content),
                                      author="system:" + plan.operation_name, operation=plan.operation_name, plan=plan)
            elif document.action == "insert_membership":
                uow.repository("business_memberships", business_id=document.resource.business_id).insert(plain(document.values))
            elif document.action == "update_membership":
                uow.repository("business_memberships", business_id=document.resource.business_id).update(identity, plain(document.values), expected_revision=expectations[document.resource].revision)
            elif document.action == "revoke_credential":
                uow.repository("credentials").update(document.resource.resource_id, plain(document.values), expected_revision=expectations[document.resource].revision)
        return plain(change.values["result"])
