"""Small workspace-type registry and presentation-only agent workspace tools.

Presentation state is kept per selected business and agent; it is not where the agent lives. An agent presents only as an
active supported global that reaches the selected active business now, with no agent membership read.

Core knows Focus and the generic presentation fields (item, status, attention). A workspace type an installed extension adds
declares its own presentation fields and callbacks: ``fields`` are merged into the operation schemas, ``normalize`` shapes the
stored presentation on read and before an update, ``select`` interprets its own fields in an update and ``validate`` checks
the result. A field no selected type declares is not in the schema, and a type rejects fields it did not declare.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from bazarlyk.agents import roles
from bazarlyk.agents.global_scope import active_global, reaches_business
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.application.plans import IDENTITY, PlanBuilder, UUID, apply_records, require_revision, schema, utc_clock

KEY = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"}
TEXT = {"type": "string", "maxLength": 2000}
ITEM = schema({"type": IDENTITY, "id": IDENTITY, "label": {"type": "string", "maxLength": 500}}, ("type", "id"))
# The presentation fields every workspace type shares; a type's own fields come from its declaration.
SELECTION = {"item": {"anyOf": [ITEM, {"type": "null"}]}}
GENERIC_ARGUMENTS = frozenset({"workspace", "business_id", "status", "attention", "reason", "urgent", "blocker", *SELECTION})


@dataclass(frozen=True)
class WorkspaceType:
    """One renderer the host can present; it never owns the workspace's content."""

    key: str
    display_name: str
    supported_roles: frozenset[str]
    renderer: str
    presentation_actions: tuple[str, ...]
    description: str = ""
    fields: Mapping = field(default_factory=dict)
    normalize: Callable | None = None
    select: Callable | None = None
    validate: Callable | None = None

    def __post_init__(self):
        clash = set(self.fields) & GENERIC_ARGUMENTS
        if clash or not all(isinstance(name, str) and name.replace("_", "").isalnum() for name in self.fields):
            raise ValueError("Workspace type fields must be new plain names: " + ", ".join(sorted(map(str, clash))))
        for hook in (self.normalize, self.select, self.validate):
            if hook is not None and not callable(hook):
                raise ValueError("Workspace type callbacks must be callable")

    def view(self) -> dict:
        return {"key": self.key, "display_name": self.display_name,
                "supported_roles": sorted(self.supported_roles), "renderer": self.renderer,
                "presentation_actions": list(self.presentation_actions), "description": self.description}


# Focus keeps its own operations and authorization; the registry only records
# that the host knows how to render it and which role it belongs to.
WORKSPACE_TYPES = (
    WorkspaceType("focus", "Focus", frozenset({roles.ORDUBASHY_ROLE, roles.GLOBAL_WORKER_ROLE}), "focus_board",
        ("focus_show", "focus_clear", "focus_browser_input", "focus_present", "focus_edit",
         "focus_move", "focus_set_archived", "focus_reply"),
        "Shared owner board for Ordubashy and global workers. Ordering remains owner-only."),
)

# Roles that may see the workspace tools at all; the type still gates each call. An installed extension's type must stay
# within these roles, because the tool-level default (composition/policy.py) allows exactly them.
WORKSPACE_ROLES = frozenset().union(*(item.supported_roles for item in WORKSPACE_TYPES))


class WorkspaceRegistry:
    def __init__(self, types=WORKSPACE_TYPES):
        types = tuple(types)
        self._types = {item.key: item for item in types}
        if len(self._types) != len(types) or not self._types:
            raise ValueError("Workspace types require unique non-empty keys")
        if any(item.supported_roles - WORKSPACE_ROLES for item in types):
            raise ValueError("A workspace type supports a role the workspace tools do not allow")
        declared = {}
        for item in types:
            for name, declaration in item.fields.items():
                if declared.setdefault(name, declaration) != declaration:
                    raise ValueError(f"Workspace field {name} is declared differently by two types")
        self.fields = dict(declared)

    def get(self, key) -> WorkspaceType:
        item = self._types.get(key) if isinstance(key, str) else None
        if item is None:
            raise DomainError("not_found", "Unknown workspace type")
        return item

    def all(self) -> tuple[WorkspaceType, ...]:
        return tuple(self._types[key] for key in sorted(self._types))

    def for_role(self, role_id) -> tuple[WorkspaceType, ...]:
        return tuple(item for item in self.all() if role_id in item.supported_roles)


class WorkspaceService:
    """Presentation state for a permitted workspace; never the workspace's content."""

    def __init__(self, application, *, types=(), clock=utc_clock):
        self.app, self.clock = application, clock
        self.workspaces = WorkspaceRegistry((*WORKSPACE_TYPES, *types))

    def register(self, registry):
        common = dict(supports_signoff=False, scope_kinds=frozenset({"business"}),
            resolve_namespace=lambda ctx, args: "business:" + ctx.business_id if ctx.business_id else "global")
        # An explicit args business_id (a global agent's own selection, or the
        # owner naming another agent's business) routes just as ctx.business_id does.
        business_common = dict(supports_signoff=False, scope_kinds=frozenset({"business"}),
            resolve_namespace=lambda ctx, args: "business:" + (ctx.business_id or args.get("business_id"))
                if (ctx.business_id or args.get("business_id")) else "global")
        registry.register(OperationDescriptor("workspace_types",
            schema({"role_id": {"type": "string", "maxLength": 64}}), effect_kind="read",
            **{**common, "scope_kinds": frozenset({"business", "global"}),
               "global_capability": "read_global_context"},
            description="Read the installed workspace types with each type's supported roles, renderer and "
                "presentation actions. A small registry, not an arbitrary UI builder.",
            read=lambda ctx, args, options, uow: self.read_types(ctx, args, uow)))
        registry.register(OperationDescriptor("workspace_get",
            schema({"workspace": KEY, "business_id": {"anyOf": [UUID, {"type": "null"}]},
                    "agent_id": {"anyOf": [UUID, {"type": "null"}]}}, ("workspace",)), effect_kind="read", **business_common,
            description="Read stored workspace presentation state. Agents read their own; the installation owner "
                "may read a selected agent's. This never reads workspace content.",
            read=lambda ctx, args, options, uow: self.read(ctx, args, uow)))
        selection = {**SELECTION, **self.workspaces.fields}
        operations = {
            "workspace_present": ({**selection, "status": {"type": "string", "maxLength": 2000},
                "attention": {"anyOf": [schema({"reason": TEXT, "urgent": {"type": "boolean"},
                    "blocker": {"type": "boolean"}}, ("reason",)), {"type": "null"}]}},
                (),
                "Present a permitted workspace to its renderer with its optional selection fields, item and status. "
                "Stores presentation state only; content edits always use the owning feature's operations."),
            "workspace_select": (selection, (), "Select what is shown in the presented workspace. "
                "Stores presentation state only and changes no content."),
            "workspace_attention": ({"reason": TEXT, "urgent": {"type": "boolean"}, "blocker": {"type": "boolean"}},
                ("reason",), "Ask for owner attention on the presented workspace with a short reason. "
                "Presentation state only; this does not create a Focus card, task or signoff."),
        }
        for name, (fields, required, description) in operations.items():
            registry.register(OperationDescriptor(name, schema({"workspace": KEY,
                "business_id": {"anyOf": [UUID, {"type": "null"}]}, **fields}, ("workspace", *required)),
                **business_common, description=description,
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                apply=self.apply))

    def access(self, ctx, uow, *, workspace_key=None, business_id=None, tool=None):
        """Resolve one authorized agent, its role and one active business."""
        if ctx.principal_kind != "agent":
            raise DomainError("forbidden", "Workspace presentation requires an agent")
        stored = active_global(uow.connection, ctx.agent_id)
        if stored is None:
            raise DomainError("forbidden", "Workspace presentation requires an active global agent")
        role = stored["role"]["id"]
        selected = business_id or ctx.business_id
        if selected is None:
            raise DomainError("validation_error", "Select one project workspace")
        if selected not in ctx.allowed_business_ids or not reaches_business(uow.connection, ctx.agent_id, selected):
            raise DomainError("not_found", "Workspace project is unavailable")
        if workspace_key is not None:
            item = self.workspaces.get(workspace_key)
            if tool is not None and tool not in item.presentation_actions:
                raise DomainError("unsupported_workspace",
                    f"{tool} does not drive the {item.key} workspace; use its declared actions")
            if role not in item.supported_roles:
                raise DomainError("forbidden", "This workspace type is not available to your role")
        return role, selected

    def row(self, agent_id, business_id, workspace_key, uow):
        stored = uow.connection.execute(
            "SELECT * FROM workspace_presentations WHERE business_id=? AND agent_id=? AND workspace_key=?",
            (business_id, agent_id, workspace_key)).fetchone()
        if stored is None:
            return None
        return {**dict(stored), "presentation_json": json.loads(stored["presentation_json"] or "{}")}

    def presentation(self, key, stored) -> dict:
        """The stored presentation as its type normalizes it (a copy; core-only types store it as it is)."""
        normalize = self.workspaces.get(key).normalize
        return normalize(dict(stored or {})) if normalize else dict(stored or {})

    def view(self, row) -> dict:
        presentation = self.presentation(row["workspace_key"], row["presentation_json"])
        return {"id": row["id"], "business_id": row["business_id"], "agent_id": row["agent_id"],
                "workspace": row["workspace_key"], "revision": row["revision"], **presentation,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def read_types(self, ctx, args, uow):
        role_id = args.get("role_id")
        selected = self.workspaces.for_role(role_id) if role_id else self.workspaces.all()
        return {"types": [item.view() for item in selected], "role_id": role_id}

    def read(self, ctx, args, uow):
        key = args["workspace"]
        self.workspaces.get(key)
        if ctx.principal_kind == "agent":
            role, selected = self.access(ctx, uow, workspace_key=key, business_id=args.get("business_id"))
            row = self.row(ctx.agent_id, selected, key, uow)
            return {"workspace": self.view(row) if row else None, "agent_id": ctx.agent_id,
                    "business_id": selected, "role_id": role}
        installation = uow.repository("installation").get("service")
        if ctx.principal_kind != "human" or not installation or installation["owner_principal_id"] != ctx.principal_id:
            raise DomainError("forbidden", "Reading another agent's workspace requires the installation owner")
        agent_id, business_id = args.get("agent_id"), args.get("business_id")
        if not agent_id or not business_id:
            raise DomainError("validation_error", "Select the agent and project to read")
        member = uow.repository("business_memberships", business_id=business_id).get(ctx.principal_id)
        if not member or not member["active"] or business_id not in ctx.allowed_business_ids:
            raise DomainError("not_found", "Workspace project is unavailable")
        row = self.row(agent_id, business_id, key, uow)
        return {"workspace": self.view(row) if row else None, "agent_id": agent_id, "business_id": business_id}

    def prepare(self, op, ctx, args, options, uow):
        key = args["workspace"]
        _, selected = self.access(ctx, uow, workspace_key=key, tool=op, business_id=args.get("business_id"))
        row = self.row(ctx.agent_id, selected, key, uow)
        if row and options.expected_revision is not None:
            require_revision(row, options.expected_revision)
        elif not row and options.expected_revision not in (None, 0):
            raise DomainError("revision_conflict", "This workspace has no stored presentation yet")
        namespace = "business:" + selected
        builder = PlanBuilder(ctx, op, args, "Present " + key.replace("_", " "), clock=self.clock,
            namespace=namespace, affected_scope={"kind": "business", "business_id": selected})
        item = self.workspaces.get(key)
        unsupported = sorted(set(args) - GENERIC_ARGUMENTS - set(item.fields))
        if unsupported:
            raise DomainError("validation_error", f"The {key} workspace does not take: " + ", ".join(unsupported))
        presentation = self.presentation(key, row["presentation_json"] if row else {})
        if item.select is not None:
            presentation, args = item.select(presentation, dict(args))
        for name in ("item", *item.fields):
            if name in args:
                presentation[name] = args[name]
        if op == "workspace_present":
            if "status" in args:
                presentation["status"] = args["status"].strip()
            if "attention" in args:
                presentation["attention"] = args["attention"]
        elif op == "workspace_attention":
            presentation["attention"] = {"reason": args["reason"].strip(),
                "urgent": bool(args.get("urgent")), "blocker": bool(args.get("blocker"))}
        if item.validate is not None:
            item.validate(presentation)
        identity = row["id"] if row else new_id()
        values = {"agent_id": ctx.agent_id, "workspace_key": key,
                  "presentation_json": plain(presentation), "updated_at": builder.at}
        if row is None:
            values["id"] = identity
        builder.record("workspace_presentations", identity, row, values,
            business_id=selected, namespace=namespace, label=key)
        return builder.build()

    def apply(self, ctx, plan, uow):
        self.access(ctx, uow, workspace_key=plan.arguments["workspace"],
            tool=plan.operation_name, business_id=plan.arguments.get("business_id"))
        applied = apply_records(ctx, plan, uow)
        return {"workspace": self.view(applied[-1])}
