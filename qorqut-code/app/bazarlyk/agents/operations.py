"""Canonical profile reads and deterministic owner-controlled configuration edits."""

from __future__ import annotations

import base64
import difflib
import json
from uuid import UUID

from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import (ChangePreview, ChangeSet, DomainError, ExpectedTarget, FieldChange,
                                         PreparedChange, PreviewResource, Principal, ResourceRef, StructuredPreview,
                                         TextPreview, canonical_bytes, new_id, plain, utc_now)
from bazarlyk.composition.locations import agent_workspace, document_root
from . import plugin_defaults, roles
from .global_scope import with_effective_scope
from .models import AgentProfile, PLUGIN_GRANT_SCHEMA, PROFILE_SCHEMA, REFERENCE_SCHEMA, RETIRED_ROLE_IDS, GLOBAL_CAPABILITIES, scope_schema
from qorqut_operations.document_history.files import seed_document
from qorqut_operations.storage.files import RegisteredFileTarget, file_hash, file_values
from bazarlyk.management.identity import configured_ordubashy, installation_owner, require_protected_control
from .icons import AGENT_ICONS, ICON_IDS
from .global_scope import active_global
from .resolver import effective_tool_policy
from .worker_access import require_worker_manager

UUID_SCHEMA = {"type": "string", "format": "uuid"}
EMAIL_MANAGEMENT_PLUGIN_ID = plugin_defaults.EMAIL_PLUGIN
DEFAULT_EMAIL_MANAGEMENT_CONFIG = plugin_defaults.default_config(EMAIL_MANAGEMENT_PLUGIN_ID)
EDITABLE = {key: value for key, value in PROFILE_SCHEMA["properties"].items()
            if key in {"name", "display_name", "runtime", "role", "scope", "assignment_brief", "plugins", "tool_policy", "presentation"}}
# A new worker reads the shared Context and reaches every business like any global; further access is chosen explicitly.
WORKER_DEFAULT_SCOPE = {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": ["read_global_context"]}
# Agent, role and shared-instruction administration and the old peer authority never pass to a worker; every other
# capability may be delegated up to what the manager holds.
WORKER_BARRED_CAPABILITIES = frozenset({"manage_ecosystem_agents", "coordinate_businesses", "manage_delegated_assignments",
                                        "publish_roles", "publish_plugins", "manage_shared_instructions", "manage_agentic_layer"})
MODE_RANK = {"deny": 0, "signoff": 1, "allow": 2}


def published_email_management_pin(connection):
    """Exact reference to the newest published email-management v3 artifact, or None."""
    return plugin_defaults.published_pin(connection, EMAIL_MANAGEMENT_PLUGIN_ID)


def published_role_pin(connection, role_id):
    """Exact reference to the newest published version of a role artifact, or None."""
    rows = connection.execute("SELECT version, digest FROM artifacts WHERE kind='role' AND artifact_id=?", (role_id,)).fetchall()
    newest = max(rows, key=lambda row: tuple(int(part) for part in row["version"].split(".")), default=None)
    return None if newest is None else {"id": role_id, "version": newest["version"], "digest": newest["digest"]}


class AgentOperations:
    def __init__(self, resolver):
        self.resolver = resolver

    def _default_grants(self, role_id, scope, uow):
        return plugin_defaults.default_grants(self.resolver, uow.connection, role_id, scope)

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
            raise DomainError("retired_role", "This role is retired; only the supported global roles operate")
        if (role_id == roles.GLOBAL_WORKER_ROLE and operation != "agents_deactivate"
                and "manage_agentic_layer" in profile.scope.global_capabilities):
            raise DomainError("forbidden", "Toolkit management cannot be granted to a worker")

    @staticmethod
    def _validate_roster(operation, profile, before, uow):
        """An active profile is one supported global: ecosystem scope, no business home, one per singleton role.

        Deactivation stays available for every profile, including historical business-home ones.
        """
        if profile.data["status"] != "active":
            return
        if profile.scope.kind != "ecosystem" or profile.scope.business_id is not None:
            raise DomainError("invalid_profile", "Projects own resources only; agents are global with ecosystem scope")
        role_id = profile.role.id
        if role_id not in roles.SUPPORTED_GLOBAL_ROLES:
            raise DomainError("invalid_profile", "Active agents use one of the supported global roles: "
                              + ", ".join(sorted(roles.SUPPORTED_GLOBAL_ROLES)))
        if role_id in roles.REPEATABLE_GLOBAL_ROLES:
            return
        others = [row["agent_id"] for row in uow.connection.execute(
            "SELECT agent_id, profile_json FROM agent_profiles WHERE status='active' AND home_business_id IS NULL "
            "AND agent_id<>? ORDER BY agent_id", (profile.agent_id,)) if roles.profile_role(row) == role_id]
        if others:
            raise DomainError("roster_conflict", f"An active {role_id} agent is already registered",
                              details={"agent_ids": others})

    @staticmethod
    def _validate_icon(profile, before):
        """A worker's newly chosen or changed icon is one of the bundled catalog's; a stored icon is never rewritten."""
        icon = profile.data["presentation"].get("icon")
        if (profile.role.id != roles.GLOBAL_WORKER_ROLE or icon is None
                or (before is not None and before.data["presentation"].get("icon") == icon)):
            return
        if icon not in ICON_IDS:
            raise DomainError("validation_error", "Choose a worker icon listed by agents_icons_list",
                              field_errors={"presentation.icon": "Not a bundled icon"})

    @staticmethod
    def _require_creation_authority(ctx, role_id, uow):
        """The installation owner establishes any global; the configured Ordubashy adds only workers."""
        if ctx.principal_kind == "human" and ctx.principal_id == installation_owner(uow):
            return
        if (role_id == roles.GLOBAL_WORKER_ROLE and ctx.principal_kind == "agent"
                and ctx.principal_id == configured_ordubashy(uow, require_codex=False)):
            return
        raise DomainError("forbidden", "Only the owner can establish a missing global agent; Ordubashy adds workers")

    @staticmethod
    def _default_runtime(role_id, uow):
        """Ordubashy runs Codex; a worker starts on the runtime of the configured Ordubashy."""
        if role_id == roles.ORDUBASHY_ROLE:
            return "codex"
        manager = active_global(uow.connection, configured_ordubashy(uow, require_codex=False))
        return manager["runtime"] if role_id == roles.GLOBAL_WORKER_ROLE and manager else "claude-code"

    def _require_manager_authority(self, ctx, operation, profile, before, uow):
        """An agent manager reaches only workers, and never grants one more core authority than it holds itself.

        The whole proposed effective grant is compared, not the edited field: a core tool or capability may be kept or
        reduced, but no tool mode may exceed both the manager's own effective mode and what the worker already held, and
        no capability is added that the manager does not hold. Plugin tools are not compared: a worker needs its own valid
        pin and keeps the plugin's own source and Signoff rules. The owner is exempt. Called when preparing, applying and
        executing a queued plan, each time against current profiles."""
        if ctx.principal_kind != "agent" or profile.data["status"] != "active":
            return
        if profile.role.id != roles.GLOBAL_WORKER_ROLE:
            raise DomainError("forbidden", "A manager agent can only configure global workers")
        barred = sorted(set(profile.scope.global_capabilities) & WORKER_BARRED_CAPABILITIES)
        if barred:
            raise DomainError("forbidden", "A worker cannot hold agent-administration or owner-level capabilities",
                              details={"capabilities": barred})
        manager, _ = self.resolver.profile(ctx.principal, connection=uow.connection)
        added = set(profile.scope.global_capabilities) - set(before.scope.global_capabilities if before else ())
        if added - set(manager.scope.global_capabilities):
            raise DomainError("forbidden", "A worker cannot be granted a capability the manager does not hold",
                              details={"capabilities": sorted(added - set(manager.scope.global_capabilities))})
        held = self._grants(manager, uow)
        kept = self._grants(before, uow) if before else {}
        excess = sorted({key[0] for key, rank in self._grants(profile, uow).items()
                         if rank > max(held.get(key, 0), kept.get(key, 0))})
        if excess:
            raise DomainError("forbidden", "A worker cannot be granted more than the manager's own effective authority",
                              details={"operations": excess})

    def _grants(self, profile, uow):
        """Rank of every available core tool mode of a profile, per operation and Global table scope.

        No packages are passed, so plugin-provided tools are left out of the comparison."""
        role = self.resolver.catalog.get("role", plain(profile.data["role"]), connection=uow.connection)
        return {(row["canonical_operation"], scope): MODE_RANK[mode]
                for row in effective_tool_policy(profile, role, self.resolver.registry) if row["available"]
                for scope, mode in {"": row["effective_mode"], **row.get("scope_modes", {})}.items()}

    def _require_target_authority(self, ctx, operation, identity, uow):
        """Dev stays owner-only; an agent caller manages only an active global worker."""
        require_protected_control(uow, ctx.principal_id, ctx.principal_kind, [identity])
        if ctx.principal_kind == "agent":
            require_worker_manager(ctx, uow, identity)

    def register(self, registry, application=None):
        if application:
            from .briefing import WorkBriefing
            self.resolver.work_briefing = WorkBriefing(application)
        read_schemas = {
            "agent_config_get": {"business_id": UUID_SCHEMA}, "agents_list": {"include_inactive": {"type": "boolean"}, "scope": {"enum": ["business", "global"]}},
            "agents_get": {"agent_id": UUID_SCHEMA}, "permissions_get": {"agent_id": UUID_SCHEMA},
            "roles_list": {}, "roles_get": {"role": REFERENCE_SCHEMA}, "plugins_list": {}, "agents_icons_list": {},
        }
        for name, properties in read_schemas.items():
            required = ["agent_id"] if name == "agents_get" else ["role"] if name == "roles_get" else []
            registry.register(OperationDescriptor(name, {"type": "object", "properties": properties, "required": required},
                description=("Read your current identity, role, grants and project orientation. Select business_id only within your allowed scope. Reading never acknowledges context." if name == "agent_config_get" else
                             "List the bundled icons a worker can be given as presentation.icon, as {icons: [{id, label}]}. Choose an ID from this list; presentation replaces the whole object, so repeat any color to keep it." if name == "agents_icons_list" else ""),
                effect_kind="read", supports_signoff=False, scope_kinds=frozenset({"business", "global", "agent_workspace"}),
                global_capability="read_global_context",
                resolve_namespace=self.roster_namespace if name == "agents_list" else None,
                read=lambda ctx, args, options, uow, operation=name: self.read(operation, ctx, args, options, uow)))
        editable = {**EDITABLE, "scope": scope_schema(GLOBAL_CAPABILITIES | frozenset(getattr(registry, "extension_global_capabilities", ())))}
        mutations = {
            "agents_create": ({**editable, "runtime": {"enum": ["claude-code", "codex"]}}, []),
            "agents_update": ({"agent_id": UUID_SCHEMA, "changes": {"type": "object", "additionalProperties": False, "properties": editable, "minProperties": 1}}, ["agent_id", "changes"]),
            "agents_deactivate": ({"agent_id": UUID_SCHEMA}, ["agent_id"]),
            "permissions_set": ({"agent_id": UUID_SCHEMA, "overrides": PROFILE_SCHEMA["properties"]["tool_policy"]["properties"]["overrides"]}, ["agent_id", "overrides"]),
            "plugins_enable": ({"agent_id": UUID_SCHEMA, "plugin": PLUGIN_GRANT_SCHEMA}, ["agent_id", "plugin"]),
            "plugins_disable": ({"agent_id": UUID_SCHEMA, "plugin_id": {"type": "string"}}, ["agent_id", "plugin_id"]),
        }
        for name, (properties, required) in mutations.items():
            registry.register(OperationDescriptor(name,
                {"type": "object", "properties": {**properties, "rationale": {"type": "string", "maxLength": 2000}}, "required": required},
                description=("Establish a supported global agent with ecosystem scope. The owner creates ordubashy or learning-applier (at most one active each) or any number of global-worker agents; retired roles cannot be registered or switched to; name, display_name and assignment_brief are required. Ordubashy creates global workers only: name and assignment_brief suffice, role defaults to the newest published global-worker, display_name to the name, runtime to Ordubashy's, and scope to ecosystem with shared-context read access; no plugins are granted unless selected. Projects have no resident agents. Every global reaches every active project; a saved allowed_business_ids list is ignored. When plugins is omitted, only Ordubashy receives the published email-management guidance package and every other agent gets none; an explicit list, even empty, replaces the defaults. Ordubashy can grant a worker only core permissions and capabilities he holds himself and never administrative powers; published plugins are selected independently and keep their own permission checks." if name == "agents_create" else ""),
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
            raise DomainError("scope_required", "Select a permitted project or explicitly request the Global roster")
        return "business:" + ctx.business_id

    def mutation_namespace(self, ctx, arguments):
        if "agent_id" in arguments:
            with self.resolver.database.read() as uow:
                row = uow.connection.execute("SELECT home_business_id FROM agent_profiles WHERE agent_id=?", (arguments["agent_id"],)).fetchone()
            if row is not None and row["home_business_id"] is None:
                return "global"
        # A creation without a scope is a worker, which is always ecosystem-wide.
        if arguments.get("scope", {} if "agent_id" in arguments else WORKER_DEFAULT_SCOPE).get("kind") == "ecosystem":
            return "global"
        return "business:" + ctx.business_id if ctx.business_id else "global"

    def _access(self, ctx, row):
        if row is None:
            raise DomainError("not_found", "Agent profile does not exist")
        home = row["home_business_id"]
        if home is not None:
            if home != ctx.business_id or home not in ctx.allowed_business_ids:
                raise DomainError("forbidden", "Agent is outside the selected project")
        elif ctx.home_business_id is not None or not ctx.global_capabilities:
            raise DomainError("forbidden", "Ecosystem agent requires explicit global scope")

    def owner_config(self, ctx, agent_id):
        with self.resolver.database.read() as uow:
            membership = uow.repository("business_memberships", business_id=ctx.business_id).get(ctx.principal_id) if ctx.business_id else None
            if ctx.principal_kind != "human" or not membership or not membership["active"] or not membership["is_owner"]:
                raise DomainError("forbidden", "Configuration inspection requires the selected project owner")
            row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
            self._access(ctx, row)
        return self.resolver.resolve_agent_config(Principal(agent_id, "agent"), business_id=ctx.business_id).to_dict()

    def profile_view(self, row, connection):
        value = json.loads(row["profile_json"])
        if row["status"] == "retired":
            label = value.get("display_name") if isinstance(value, dict) else None
            return {"agent_id": row["agent_id"], "name": row["name"],
                "display_name": label if isinstance(label, str) and label else row["name"], "status": "retired",
                "profile_revision": row["revision"], "archival_record": True}
        profile = AgentProfile.from_dict(value, self.resolver.registry)
        if row["status"] == "active" and row["home_business_id"] is None:
            # The computed read projection: every active business, never the saved list.
            profile = with_effective_scope(profile, connection)
        result = profile.to_dict()
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
        if operation == "agents_icons_list":
            return {"icons": [{"id": icon, "label": label} for icon, label in AGENT_ICONS]}
        if operation == "roles_get":
            package = self.resolver.catalog.get("role", args["role"], connection=uow.connection)
            return {"role": {**package.ref, "manifest": plain(package.manifest), "brief": package.files[package.manifest["brief"]].decode()}}
        if operation == "agents_list":
            if args.get("scope") == "global":
                rows = uow.connection.execute("SELECT * FROM agent_profiles WHERE home_business_id IS NULL AND (? OR status='active') ORDER BY name,agent_id", (bool(args.get("include_inactive")),))
                return {"agents": [self.profile_view(row, uow.connection) for row in rows]}
            if ctx.business_id is None or ctx.business_id not in ctx.allowed_business_ids:
                raise DomainError("scope_required", "Select a permitted project to read its roster")
            rows = uow.connection.execute("SELECT * FROM agent_profiles WHERE home_business_id=? AND (? OR status='active') ORDER BY name,agent_id", (ctx.business_id, bool(args.get("include_inactive"))))
            return {"agents": [self.profile_view(row, uow.connection) for row in rows]}
        identity = args.get("agent_id", ctx.agent_id)
        row = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()
        self._access(ctx, row)
        if operation == "agents_get":
            return {"agent": self.profile_view(row, uow.connection)}
        if row["status"] == "retired":
            raise DomainError("inactive_agent", "Retired archival records have no active permission configuration")
        profile = AgentProfile.from_dict(json.loads(row["profile_json"]), self.resolver.registry, stored=True)
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
                raise DomainError("invalid_scope", "Initial document project is not registered")
        else:
            row = None
        return RegisteredFileTarget(document_root(self.resolver.paths, row, path), path)

    def resolve_file_snapshot(self, ctx, resource, uow):
        digest = file_hash(self.resolve_file_target(resource).resolve())
        return {"content_hash": digest} if digest is not None else None

    def prepare(self, operation, ctx, args, options, uow):
        creating = operation == "agents_create"
        before, old = None, None
        default_selection = False
        default_grants = []
        if creating:
            # An omitted role is a new worker: the newest published global-worker package.
            role = args.get("role") or published_role_pin(uow.connection, roles.GLOBAL_WORKER_ROLE)
            if role is None:
                raise DomainError("role_unavailable", "No global-worker role package is published")
            self._require_creation_authority(ctx, role["id"], uow)
            worker = role["id"] == roles.GLOBAL_WORKER_ROLE
            scope = args.get("scope") or (WORKER_DEFAULT_SCOPE if worker else {"kind": "business", "business_id": ctx.business_id})
            if role["id"] in RETIRED_ROLE_IDS:
                raise DomainError("retired_role", "This role is retired; only the supported global roles operate")
            if scope["kind"] != "ecosystem":
                raise DomainError("invalid_profile", "Projects own resources only; agents are global with ecosystem scope")
            required = {"name", "assignment_brief"} if worker else {"name", "display_name", "assignment_brief"}
            if not required <= set(args):
                raise DomainError("validation_error", "A Global agent requires name, display_name and assignment_brief"
                                  "; a worker requires name and assignment_brief")
            if ctx.home_business_id is not None or not set(scope["global_capabilities"]).issubset(ctx.global_capabilities):
                raise DomainError("forbidden", "New ecosystem profile cannot exceed current granted scope")
            # Business access is derived; the saved list is never authority and stays empty.
            scope = {**scope, "allowed_business_ids": []}
            home = None
            name, assignment = args["name"], args["assignment_brief"]
            display_name = args.get("display_name", name)
            presentation = dict(args.get("presentation", {}))
            if role["id"] == "global-worker":
                presentation.setdefault("icon", "bot")
            # Omitted plugins select the defaults; an explicit list, even empty, replaces them.
            default_selection = "plugins" not in args
            if default_selection:
                default_grants = self._default_grants(role["id"], scope, uow)
            plugins = args.get("plugins", json.loads(json.dumps(default_grants)))
            identity = new_id()
            # The registered identity and home scope select the workspace; the name never does.
            workspace = agent_workspace(self.resolver.paths, identity, home)
            values = {"schema_version": 2, "agent_id": identity, "profile_revision": 1, "name": name,
                      "display_name": display_name, "status": "active", "runtime": args.get("runtime") or self._default_runtime(role["id"], uow),
                      "role": role,
                      "scope": scope, "assignment_brief": assignment, "plugins": plugins,
                      "tool_policy": args.get("tool_policy", {"overrides": {}}), "presentation": presentation, "created_at": utc_now()}
        else:
            identity = args["agent_id"]
            old = uow.connection.execute("SELECT * FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()
            self._access(ctx, old)
            # Profile, grant, runtime and retirement edits can disrupt Dev's session: owner only.
            self._require_target_authority(ctx, operation, identity, uow)
            if old["status"] != "active":
                raise DomainError("inactive_agent", "Inactive profiles cannot be edited through active agent operations")
            if options.expected_revision != old["revision"]:
                raise DomainError("revision_conflict", "Current profile revision is required", current_revision=old["revision"])
            before = AgentProfile.from_dict(json.loads(old["profile_json"]), self.resolver.registry, stored=True)
            if before.data["status"] != "active":
                raise DomainError("inactive_agent", "Inactive profiles cannot be edited through active agent operations")
            values = before.to_dict()
            values["profile_revision"] += 1
            home = (uow.connection.execute("SELECT slug FROM businesses WHERE id=?", (old["home_business_id"],)).fetchone()
                    if old["home_business_id"] else None)
            workspace = agent_workspace(self.resolver.paths, identity, home)
            if operation == "agents_update":
                values.update(args["changes"])
                if "scope" in args["changes"] and values["scope"].get("kind") == "ecosystem" and before.scope.kind == "ecosystem":
                    # Callers echo the computed projection; the saved list is inert and never rewritten.
                    values["scope"] = {**values["scope"], "allowed_business_ids": list(before.scope.allowed_business_ids)}
            elif operation == "permissions_set":
                values["tool_policy"] = {"overrides": args["overrides"]}
            elif operation == "plugins_enable":
                values["plugins"] = [item for item in values["plugins"] if item["id"] != args["plugin"]["id"]] + [args["plugin"]]
            elif operation == "plugins_disable":
                values["plugins"] = [item for item in values["plugins"] if item["id"] != args["plugin_id"]]
            else:
                self._retirement_ready(identity, before, uow)
                values["status"] = "inactive"
        profile = AgentProfile.from_dict(values, self.resolver.registry)
        self._validate_role_lifecycle(operation, profile)
        self._validate_roster(operation, profile, before, uow)
        if profile.data['status'] == 'active':
            self._validate_role_runtime(profile)
        self.resolver.packages_for_profile(profile, connection=uow.connection)
        self._require_manager_authority(ctx, operation, profile, before, uow)
        self._validate_icon(profile, before)
        if before and before.scope.to_dict() != profile.scope.to_dict():
            if (before.scope.kind != profile.scope.kind or before.scope.business_id != profile.scope.business_id
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
        documents, effective = (), None
        if profile.data["status"] == "active":
            proposed = (profile, {"workspace": str(workspace)})
            snapshot = self.resolver._snapshot(Principal(identity, "agent"), business_id, proposed=proposed)
            documents, effective = snapshot["documents"].values(), self.resolver._compose(snapshot)
        # Deactivation composes no orientation, so a historical business-home profile can still be closed. Business access is
        # derived in each transaction: no business row is an input of a global profile change, and none is pinned.
        for document in documents:
            expected.append(ExpectedTarget(ResourceRef("documents", document["id"], document["business_id"], document["namespace"]), revision=document["revision"], content_hash=document["content_hash"]))
        result = {"agent_id": identity, "profile_revision": profile.revision, "overrides": plain(profile.data["tool_policy"]["overrides"]), "effective_config_id": effective.effective_config_id if effective else None}
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
        if profile.data["status"] != "active":
            # Agent business access is derived, never granted by membership: no new
            # rows are written, and retirement only closes rows left from earlier models.
            for membership in uow.connection.execute(
                    "SELECT * FROM business_memberships WHERE principal_id=? AND active=1 ORDER BY business_id", (identity,)):
                membership_ref = ResourceRef("business_memberships", identity, membership["business_id"], membership["business_id"])
                expected.append(ExpectedTarget(membership_ref, revision=membership["revision"]))
                changes.append(PreparedChange("record", membership_ref, "update_membership", {"active": False}))
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
                if child != operation:
                    effects.append((child, "plugins." + plugin_id, previous_plugins.get(plugin_id), next_plugins.get(plugin_id)))
        for child, field, prior, after in effects:
            changes.append(PreparedChange("record", resource, "profile_effect", {"field": field, "before": prior, "after": after}, operation_name=child))
        preview_resource = PreviewResource("agent_profiles", identity, profile.data["display_name"])
        fields = [FieldChange(key, old_data.get(key), value) for key, value in profile.to_dict().items() if old_data.get(key) != value]
        fields.append(FieldChange("effective_permissions", effective_tool_policy(before, self.resolver.catalog.get("role", before.data["role"]), self.resolver.registry) if before else None, effective.to_dict()["tools"] if effective else None))
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
        profile = AgentProfile.from_dict(plan.changes[0].values["profile"], self.resolver.registry)
        if plan.operation_name != "agents_create":
            self._require_target_authority(ctx, plan.operation_name, profile.agent_id, uow)
        else:
            self._require_creation_authority(ctx, profile.role.id, uow)
        self._validate_role_lifecycle(plan.operation_name, profile)
        self._validate_initial_defaults(plan, uow)
        self._recheck(ctx, plan, profile, uow)
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
        businesses = profile.scope.businesses | with_effective_scope(profile, uow.connection).scope.businesses
        for business_id in sorted(businesses):
            open_tasks = [row["id"] for row in uow.connection.execute("SELECT t.id,t.status FROM tasks t WHERE t.business_id=? AND t.archived=0 AND EXISTS (SELECT 1 FROM relationships e WHERE e.business_id=t.business_id AND e.from_type='task' AND e.from_id=t.id AND e.role='assigned_to' AND e.to_type='agent' AND e.to_id=?)", (business_id, identity)) if row["status"] not in closed]
            if open_tasks:
                raise DomainError("agent_has_open_tasks", "Reassign or complete outstanding tasks before retiring this agent", details={"business_id": business_id, "task_ids": open_tasks})

    def _recheck(self, ctx, plan, profile, uow):
        """The roster and manager rules against the profiles as they are now, when validating and when applying."""
        row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (profile.agent_id,)).fetchone()
        before = AgentProfile.from_dict(json.loads(row["profile_json"]), self.resolver.registry, stored=True) if row else None
        self._validate_roster(plan.operation_name, profile, before, uow)
        self._require_manager_authority(ctx, plan.operation_name, profile, before, uow)

    def apply(self, ctx, plan, uow):
        self._validate_initial_defaults(plan, uow)
        change = plan.changes[0]
        profile = AgentProfile.from_dict(change.values["profile"], self.resolver.registry)
        identity = profile.agent_id
        business_id = profile.scope.business_id
        repository = uow.repository("agent_profiles", business_id=business_id)
        expectations = {target.resource: target for target in plan.expected_targets}
        if change.action != "create_profile":
            self._require_target_authority(ctx, plan.operation_name, identity, uow)
        else:
            self._require_creation_authority(ctx, profile.role.id, uow)
        self._recheck(ctx, plan, profile, uow)
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
                seed_document(uow.connection, row, base64.b64decode(content),
                                      author="system:" + plan.operation_name, operation=plan.operation_name, plan=plan)
            elif document.action == "insert_membership":
                raise DomainError("invalid_plan", "Agent project access is derived; prepare this change again")
            elif document.action == "update_membership":
                uow.repository("business_memberships", business_id=document.resource.business_id).update(identity, plain(document.values), expected_revision=expectations[document.resource].revision)
            elif document.action == "revoke_credential":
                uow.repository("credentials").update(document.resource.resource_id, plain(document.values), expected_revision=expectations[document.resource].revision)
        return plain(change.values["result"])
