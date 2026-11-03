"""Read-only, current-authority lookup for the agent-local native toolkit.

This descriptor never opens a store, runs a tool, or applies an authored edit.
The native process retains its existing OS authority; this is not a sandbox.
"""
from __future__ import annotations

from pathlib import Path
import time
from uuid import UUID

from qorqut_agentic.cli import OPERATIONS
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError

from bazarlyk.agents.global_scope import active_role_holder
from bazarlyk.agents.roles import LEARNING_APPLIER_ROLE
from bazarlyk.management.identity import configured_ordubashy
from bazarlyk.transport.auth import AuthenticatedPrincipal, current_agent_credential


ORDINARY_OPERATIONS = frozenset({
    "catalog.list", "catalog.read_entry", "catalog.read_resource",
    "tools.invoke", "tools.test", "tools.receipt", "tools.reconcile",
    "workflows.start", "workflows.get", "workflows.list", "workflows.supply_arguments",
    "workflows.start_step", "workflows.complete_step", "workflows.pause", "workflows.resume", "workflows.cancel",
})
MANAGEMENT_OPERATIONS = frozenset(OPERATIONS) - ORDINARY_OPERATIONS
CAPABILITY = "manage_agentic_layer"


def validate_context(context):
    """Validate routing only; authority always comes from the current profile."""
    if not isinstance(context, dict) or set(context) - {"project_id", "work_type"}:
        raise DomainError("invalid_request", "Context accepts only project_id and work_type")
    project = context.get("project_id")
    if "project_id" in context:
        try:
            if not isinstance(project, str) or str(UUID(project)) != project:
                raise ValueError
        except (ValueError, AttributeError):
            raise DomainError("invalid_request", "project_id must be a canonical UUID") from None
    work_type = context.get("work_type")
    if "work_type" in context and (not isinstance(work_type, str) or not work_type.strip()
                                    or len(work_type) > 200 or any(ord(c) < 32 for c in work_type)):
        raise DomainError("invalid_request", "work_type must be a nonempty bounded string")
    return context


class AgenticAccess:
    def __init__(self, resolver, view):
        self.resolver, self.view = resolver, view

    def register(self, registry):
        registry.register(OperationDescriptor(
            "agentic_access",
            {"type": "object", "additionalProperties": False, "required": ["operation"], "properties": {
                "operation": {"type": "string", "enum": sorted(OPERATIONS)},
                "project_id": {"type": "string", "format": "uuid"},
                "work_type": {"type": "string", "minLength": 1, "maxLength": 200},
            }},
            description="Read current native toolkit access and routing; performs no toolkit operation.",
            effect_kind="read", supports_signoff=False, exposure=frozenset({"mcp"}),
            scope_kinds=frozenset({"global", "business", "agent_workspace"}),
            global_capability="read_global_context", read=self.read,
        ))

    def read(self, ctx, arguments, options, uow):
        if not isinstance(arguments, dict) or set(arguments) - {"operation", "project_id", "work_type"}:
            raise DomainError("invalid_request", "Access accepts only operation and routing context")
        operation = arguments.get("operation")
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise DomainError("invalid_request", "Unknown or installer-only toolkit operation")
        context = validate_context({key: value for key, value in arguments.items() if key != "operation"})
        if (ctx.principal_kind != "agent" or ctx.source != "mcp" or not ctx.session_registration_id
                or ctx.delegation_binding_id is not None or ctx.delegation_assignment_id is not None):
            raise DomainError("forbidden", "A current full native agent registration is required")
        profile, row = self.resolver.profile(ctx.principal, connection=uow.connection)
        registration = uow.connection.execute("SELECT * FROM machine_registrations WHERE id=?",
                                               (ctx.session_registration_id,)).fetchone()
        if (registration is None or registration["principal_id"] != profile.agent_id
                or Path(registration["workspace"]) != Path(row["workspace"])):
            raise DomainError("registration_conflict", "Native registration does not match the current profile")
        current_agent_credential(uow.connection, AuthenticatedPrincipal(
            ctx.principal, registration["credential_id"], registration["id"]), now=time.time())
        if context.get("project_id") is not None and context["project_id"] not in profile.scope.businesses:
            raise DomainError("forbidden", "Project is outside current native access")
        can_manage = CAPABILITY in profile.scope.global_capabilities and profile.agent_id in {
            configured_ordubashy(uow),
            active_role_holder(uow.connection, LEARNING_APPLIER_ROLE),
        }
        if operation in MANAGEMENT_OPERATIONS and not can_manage:
            raise DomainError("forbidden", "Toolkit management requires the current manager identity and capability")
        if operation.startswith('authoring.') or operation == 'history.restore':
            raise DomainError('owner_input_required',
                'Active agentic changes require a specific owner Signoff and the approved application workflow; use agentic_change_propose')
        if operation.startswith('overseer.') and profile.agent_id != active_role_holder(uow.connection, LEARNING_APPLIER_ROLE):
            raise DomainError('forbidden', 'Use shared agentic_change tools to submit and review your direct instructions')
        selection = self.view.selection(profile, **context)
        layer = self.view.layer
        return {"operation": operation, "actor": {"actor_id": profile.agent_id, "actor_kind": "agent"},
                "selection": selection, "root": str(layer.root), "state": str(layer.state),
                "instance_id": layer.instance_id, "can_manage": can_manage,
                "profile_revision": profile.revision, "registration_id": registration["id"],
                "attention_required": profile.agent_id == configured_ordubashy(uow)}
