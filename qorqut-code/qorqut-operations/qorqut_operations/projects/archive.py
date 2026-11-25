"""Owner-reviewed archive and owner restore of project resources; nothing is deleted.

Archiving a Project also archives every subproject below it that is not archived yet, and marks each with the Project whose
archiving took it (``archived_with``). Restoring a Project returns exactly the subprojects archived together with it;
subprojects archived on their own earlier stay archived. A subproject is not restored while its parent is archived.
Restore returns only those projects; the supported globals reach them again through their derived scope.
"""

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, PolicyDecision
from qorqut_operations.policy.rules import PolicyRules
from qorqut_operations.project_lifecycle import owner_may_act
from qorqut_operations.projects import hierarchy
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
            prepare=self.prepare_restore, apply=self.apply_restore))
        registry.register(OperationDescriptor(ARCHIVED, schema({}, ()), effect_kind="read", supports_signoff=False,
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", owner_only=True,
            exposure=frozenset({"dashboard"}), description=instruction_text('archive-owner-list-archived-projects-that-can-be-restored'),
            read=self.archived))

    @staticmethod
    def archived(ctx, args, options, uow):
        """Archived projects, newest archival first."""
        rows = uow.connection.execute(
            "SELECT b.id,b.slug,b.name,b.revision,b.archived_at,b.parent_id,b.archived_with FROM projects b "
            "JOIN installation i ON i.id='service' WHERE b.archived_at IS NOT NULL ORDER BY b.archived_at DESC, b.slug")
        return {"items": [{"project_id": row["id"], "slug": row["slug"], "name": row["name"], "revision": row["revision"],
                           "archived_at": row["archived_at"], "parent_id": row["parent_id"],
                           "archived_with": row["archived_with"]} for row in rows]}

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
            if not owner_may_act(uow.connection, installation["owner_principal_id"], project_id):
                raise DomainError("forbidden", "Archive only an active project of the installation owner")
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
            {"archived_at": builder.at, "archived_by": ctx.principal_id, "archived_with": None},
            project_id=None, namespace="global", label=before["name"])
        # The subprojects still active below it go with it, each marked with this archiving.
        below = [row for row in hierarchy.descendants(uow.connection, before["id"]) if row["archived_at"] is None]
        for row in below:
            child = uow.repository("projects").get(row["id"], required=True)
            builder.record("projects", child["id"], child,
                {"archived_at": builder.at, "archived_by": ctx.principal_id, "archived_with": before["id"]},
                project_id=None, namespace="global", label=child["name"])
        builder.effects.extend([
            "Remove " + before["name"] + " (" + before["slug"] + ") from the active project menu and normal project discovery.",
            *(["Archive its " + str(len(below)) + " active subproject" + ("s" if len(below) != 1 else "") + " with it: "
               + ", ".join(row["name"] for row in below) + ". Restoring " + before["name"] + " restores them."] if below else []),
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
            # The project, then each subproject its archiving took along.
            self.release_todos(uow, project["id"], ctx.principal_id)
            for change in plan.changes:
                if (change.kind == "record" and change.resource.resource_type == "projects"
                        and change.resource.resource_id != project["id"]):
                    self.release_todos(uow, change.resource.resource_id, ctx.principal_id)
        saved = uow.repository("projects").get(project["id"], required=True)
        return {"project_id": saved["id"], "archived": True, "revision": saved["revision"]}

    # Restore: the project resources return.
    def restore(self, ctx, arguments, options):
        return self.application.invoke(ctx, RESTORE, arguments, options)

    def prepare_restore(self, ctx, args, options, uow):
        if ctx.project_id is not None or ctx.principal_kind != "human":
            raise DomainError("forbidden", "Project restore is an owner action in Global")
        before = uow.repository("projects").get(args["project_id"], required=True)
        require_revision(before, options.expected_revision)
        if before["archived_at"] is None:
            raise DomainError("project_not_archived", "This project is not archived")
        project_id = before["id"]
        if any(row["archived_at"] is not None for row in hierarchy.ancestors(uow.connection, project_id)):
            raise DomainError("project_archived", "Its parent project is archived; restore the parent first")
        builder = PlanBuilder(ctx, RESTORE, args, "Restore project: " + before["name"],
                              namespace="global", affected_scope={"kind": "project", "project_id": project_id})
        cleared = {"archived_at": None, "archived_by": None, "archived_with": None}
        builder.record("projects", project_id, before, cleared, project_id=None, namespace="global", label=before["name"])
        # Only the subprojects its own archiving took; one archived on its own stays archived, and so does what went with it.
        returning = [row for row in hierarchy.descendants(uow.connection, project_id) if row["archived_with"] == project_id]
        for row in returning:
            child = uow.repository("projects").get(row["id"], required=True)
            builder.record("projects", child["id"], child, cleared, project_id=None, namespace="global", label=child["name"])
        builder.effects.extend([
            f"Return {before['name']} ({before['slug']}) and its records, files and integrations to the active project menu.",
            *([f"Also restore the {len(returning)} subproject{'s' if len(returning) != 1 else ''} archived with it: "
               + ", ".join(row["name"] for row in returning) + "."] if returning else []),
            "No agent is created or prepared and no session starts.",
            "Every active global agent reaches the project again through derived access."])
        return builder.build()

    def apply_restore(self, ctx, plan, uow):
        project_id = plan.arguments["project_id"]
        apply_records(ctx, plan, uow)
        return {"project_id": project_id, "restored": True}
