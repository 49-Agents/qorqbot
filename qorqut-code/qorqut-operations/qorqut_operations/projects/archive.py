"""Owner-reviewed archive and owner restore of project resources; nothing is deleted.

Restore returns only the project: historical
agent identities stay inactive, and the supported globals reach it again through
their derived scope without profile or membership writes.
"""

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, ExpectedTarget, PolicyDecision, ResourceRef, canonical_hash
from qorqut_operations.policy.rules import PolicyRules
from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision, schema, UUID, TEXT

OPERATION = "project_archive"
RESTORE = "project_restore"
ARCHIVED = "project_archived_list"


def archive_approval_rule(name, defaults, overrides, registry, *, role_id, scope, resolve):
    """The archive proposal is Ordubashy's alone, needs a grant (the override, else the role default) and is always a Signoff.

    A ``before_override`` rule: no grant, or a deny, is denied; an ``allow`` grant still cannot remove the owner's approval."""
    if name == OPERATION:
        granted = overrides.get(name, defaults.get(name, "deny")) != "deny"
        return PolicyDecision("signoff" if role_id == "ordubashy" and granted else "deny",
                              "project_archive_owner_approval")


# The feature's contribution to the host's ordered rule set (name-disjoint from every other before_override rule).
POLICY_RULES = PolicyRules(before_override=(archive_approval_rule,))


class ProjectArchive:
    """``release_todos(uow, project_id, actor_id)`` is the host's Todo boards, assigned after construction. It runs inside
    the archive's own write and takes the project's Tasks off every agent's board, so no board keeps pointing at work
    that is no longer current. A host without Todo boards leaves it unset."""

    def __init__(self, application, *, release_todos=None):
        self.application = application
        self.release_todos = release_todos

    def register(self, registry):
        registry.register(OperationDescriptor(OPERATION,
            schema({"project_id": {**UUID, "description": instruction_text('archive-project-to-archive-the-invocation-and-human-review')},
                    "rationale": TEXT}, ("project_id", "rationale")),
            scope_kinds=frozenset({"global"}), global_capability="read_global_context",
            description=instruction_text('archive-ordubashy-propose-archiving-an-owner-project-in-global'),
            prepare=self.prepare, apply=self.apply, validate_targets=self.guard))
        registry.register(OperationDescriptor(RESTORE,
            schema({"project_id": UUID, "rationale": TEXT}, ("project_id",)),
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", owner_only=True,
            supports_signoff=False, exposure=frozenset({"dashboard"}),
            description=instruction_text('archive-owner-restore-an-archived-project-s-resources-to'),
            prepare=self.prepare_restore, apply=self.apply_restore, validate_targets=self.validate_restore))
        registry.register(OperationDescriptor(ARCHIVED, schema({}, ()), effect_kind="read", supports_signoff=False,
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", owner_only=True,
            exposure=frozenset({"dashboard"}), description=instruction_text('archive-owner-list-archived-projects-that-can-be-restored'),
            read=self.archived))
        self.application.register_target_resolver("project_archive_agents", self.snapshot)

    @staticmethod
    def archived(ctx, args, options, uow):
        """Archived projects the installation owner still owns, newest archival first."""
        rows = uow.connection.execute(
            "SELECT b.id,b.slug,b.name,b.revision,b.archived_at FROM projects b JOIN installation i ON i.id='service' "
            "JOIN project_memberships m ON m.project_id=b.id AND m.principal_id=i.owner_principal_id "
            "WHERE b.archived_at IS NOT NULL AND m.active=1 AND m.is_owner=1 ORDER BY b.archived_at DESC, b.slug")
        return {"items": [{"project_id": row["id"], "slug": row["slug"], "name": row["name"], "revision": row["revision"],
                           "archived_at": row["archived_at"]} for row in rows]}

    def snapshot(self, ctx, resource, uow):
        return {"content_hash": canonical_hash([])}

    @staticmethod
    def guard(ctx, plan, uow):
        if ctx.project_id is not None:
            raise DomainError("forbidden", "Project archival is proposed and reviewed in Global")
        installation = uow.repository("installation").get("service", required=True)
        if ctx.principal_kind == "human":
            allowed = ctx.principal_id == installation["owner_principal_id"]
        else:
            allowed = (ctx.principal_kind == "agent" and ctx.role_artifact_ref is not None
                       and ctx.role_artifact_ref.id == "ordubashy")
        if not allowed:
            raise DomainError("forbidden", "Only the installation owner or Ordubashy can propose archival")
        if plan is not None:
            project_id = plan.arguments["project_id"]
            member = uow.repository("project_memberships", project_id=project_id).get(installation["owner_principal_id"])
            if not member or not member["active"] or not member["is_owner"]:
                raise DomainError("forbidden", "Archive only a project currently owned by the installation owner")
            if (ctx.role_artifact_ref and ctx.role_artifact_ref.id == "ordubashy"
                    and project_id not in ctx.allowed_project_ids):
                raise DomainError("forbidden", "Project is outside Ordubashy's current scope")

    def prepare(self, ctx, args, options, uow):
        self.guard(ctx, None, uow)
        if not args["rationale"].strip():
            raise DomainError("validation_error", "Explain why the project should be archived")
        before = uow.repository("projects").get(args["project_id"], required=True)
        require_revision(before, options.expected_revision)
        if before["archived_at"] is not None:
            raise DomainError("project_archived", "This project is already archived")
        builder = PlanBuilder(ctx, OPERATION, args, "Archive project: " + before["name"],
                              namespace="global", affected_scope={"kind": "project", "project_id": before["id"]})
        builder.record("projects", before["id"], before,
            {"archived_at": builder.at, "archived_by": ctx.principal_id}, project_id=None, namespace="global", label=before["name"])
        target = ResourceRef("project_archive_agents", before["id"], None, "global")
        builder.targets[target] = ExpectedTarget(target, exists=True, content_hash=canonical_hash([]))
        builder.policy_effect(target, OPERATION, "Reset this project's registered terminal sessions")
        builder.effects.extend([
            "Remove " + before["name"] + " (" + before["slug"] + ") from the active project menu, agent directory and normal project discovery.",
            "Take this project's tasks off every agent's Todo board. Restoring the project does not put them back.",
            "Retain project records and files. No domain, mailbox or external-service changes."])
        plan = builder.build()
        self.guard(ctx, plan, uow)
        return plan

    def apply(self, ctx, plan, uow):
        self.guard(ctx, plan, uow)
        project = uow.repository("projects").get(plan.arguments["project_id"], required=True)
        apply_records(ctx, plan, uow)
        if self.release_todos is not None:
            self.release_todos(uow, project["id"], ctx.principal_id)
        saved = uow.repository("projects").get(project["id"], required=True)
        return {"project_id": saved["id"], "archived": True, "revision": saved["revision"]}

    # Restore: the project resources return; no agent identity is touched.
    def restore(self, ctx, arguments, options):
        return self.application.invoke(ctx, RESTORE, arguments, options)

    def prepare_restore(self, ctx, args, options, uow):
        if ctx.project_id is not None or ctx.principal_kind != "human":
            raise DomainError("forbidden", "Project restore is an owner action in Global")
        before = uow.repository("projects").get(args["project_id"], required=True)
        require_revision(before, options.expected_revision)
        if before["archived_at"] is None:
            raise DomainError("project_not_archived", "This project is not archived")
        installation = uow.repository("installation").get("service", required=True)
        member = uow.repository("project_memberships", project_id=before["id"]).get(installation["owner_principal_id"])
        if not member or not member["active"] or not member["is_owner"]:
            raise DomainError("forbidden", "Restore only a project owned by the installation owner")
        project_id = before["id"]
        builder = PlanBuilder(ctx, RESTORE, args, "Restore project: " + before["name"],
                              namespace="global", affected_scope={"kind": "project", "project_id": project_id})
        builder.record("projects", project_id, before, {"archived_at": None, "archived_by": None},
                                      project_id=None, namespace="global", label=before["name"])
        builder.effects.extend([
            f"Return {before['name']} ({before['slug']}) and its records, files and integrations to the active project menu.",
            "No agent is created, reactivated or prepared and no session starts; historical agents stay inactive.",
            "Every active global agent reaches the project again through derived access, without membership changes."])
        return builder.build()

    @staticmethod
    def validate_restore(ctx, plan, uow):
        if any(item.action == "restore_roster" for item in plan.changes):
            raise DomainError("invalid_plan", "Projects own resources only; prepare the restore again")

    def apply_restore(self, ctx, plan, uow):
        self.validate_restore(ctx, plan, uow)
        project_id = plan.arguments["project_id"]
        apply_records(ctx, plan, uow)
        return {"project_id": project_id, "restored": True}
