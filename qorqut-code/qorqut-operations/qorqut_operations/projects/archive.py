"""Owner-reviewed archive and owner restore of project resources; nothing is deleted.

Projects have no resident agents. Restore returns only the project: historical
agent identities stay inactive, and the supported globals reach it again through
their derived scope without profile or membership writes.
"""

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, ExpectedTarget, PolicyDecision, ResourceRef, canonical_hash
from qorqut_operations.policy.rules import PolicyRules
from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision, schema, UUID, TEXT

OPERATION = "business_archive"
RESTORE = "business_restore"
ARCHIVED = "business_archived_list"


def archive_approval_rule(name, defaults, overrides, registry, *, role_id, scope, resolve):
    """The archive proposal is Ordubashy's alone, needs a grant (the override, else the role default) and is always a Signoff.

    A ``before_override`` rule: no grant, or a deny, is denied; an ``allow`` grant still cannot remove the owner's approval."""
    if name == OPERATION:
        granted = overrides.get(name, defaults.get(name, "deny")) != "deny"
        return PolicyDecision("signoff" if role_id == "ordubashy" and granted else "deny",
                              "business_archive_owner_approval")


# The feature's contribution to the host's ordered rule set (name-disjoint from every other before_override rule).
POLICY_RULES = PolicyRules(before_override=(archive_approval_rule,))


class BusinessArchive:
    """``reset_terminals(business_row, agent_rows)`` is the host's terminal reset. It is optional at construction because the
    real terminal service is built after the domains and assigned to ``reset_terminals`` then, never supplied by a caller;
    until it is set, an approved archive is refused with ``operation_unavailable``."""

    def __init__(self, application, *, reset_terminals=None):
        self.application = application
        self.reset_terminals = reset_terminals

    def register(self, registry):
        registry.register(OperationDescriptor(OPERATION,
            schema({"business_id": {**UUID, "description": "Project to archive; the invocation and human review remain in Global."},
                    "rationale": TEXT}, ("business_id", "rationale")),
            scope_kinds=frozenset({"global"}), global_capability="read_global_context",
            description="Ordubashy: propose archiving an owner project in Global. Always requires an urgent human Signoff, even with an allow override. Supply its current expected_revision. Approval hides the project from normal discovery and resets its terminal sessions. Keeps all records, folders, domains and mailboxes.",
            prepare=self.prepare, apply=self.apply, validate_targets=self.guard))
        registry.register(OperationDescriptor(RESTORE,
            schema({"business_id": UUID, "rationale": TEXT}, ("business_id",)),
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", owner_only=True,
            supports_signoff=False, exposure=frozenset({"dashboard"}),
            description="Owner: restore an archived project's resources to the active project menu. No agent is created, reactivated or prepared; historical agents stay inactive. Every active global agent reaches the restored project again through derived access.",
            prepare=self.prepare_restore, apply=self.apply_restore, validate_targets=self.validate_restore))
        registry.register(OperationDescriptor(ARCHIVED, schema({}, ()), effect_kind="read", supports_signoff=False,
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", owner_only=True,
            exposure=frozenset({"dashboard"}), description="Owner: list archived projects that can be restored.",
            read=self.archived))
        self.application.register_target_resolver("business_archive_agents", self.snapshot)

    @staticmethod
    def archived(ctx, args, options, uow):
        """Archived projects the installation owner still owns, newest archival first."""
        rows = uow.connection.execute(
            "SELECT b.id,b.slug,b.name,b.revision,b.archived_at FROM businesses b JOIN installation i ON i.id='service' "
            "JOIN business_memberships m ON m.business_id=b.id AND m.principal_id=i.owner_principal_id "
            "WHERE b.archived_at IS NOT NULL AND m.active=1 AND m.is_owner=1 ORDER BY b.archived_at DESC, b.slug")
        return {"items": [{"business_id": row["id"], "slug": row["slug"], "name": row["name"], "revision": row["revision"],
                           "archived_at": row["archived_at"]} for row in rows]}

    @staticmethod
    def agents(uow, business_id):
        return [dict(row) for row in uow.connection.execute(
            "SELECT agent_id,name,workspace,revision,home_business_id FROM agent_profiles "
            "WHERE home_business_id=? ORDER BY agent_id", (business_id,))]

    def snapshot(self, ctx, resource, uow):
        return {"content_hash": canonical_hash(self.agents(uow, resource.resource_id))}

    @staticmethod
    def guard(ctx, plan, uow):
        if ctx.business_id is not None or ctx.home_business_id is not None:
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
            business_id = plan.arguments["business_id"]
            member = uow.repository("business_memberships", business_id=business_id).get(installation["owner_principal_id"])
            if not member or not member["active"] or not member["is_owner"]:
                raise DomainError("forbidden", "Archive only a project currently owned by the installation owner")
            if (ctx.role_artifact_ref and ctx.role_artifact_ref.id == "ordubashy"
                    and business_id not in ctx.allowed_business_ids):
                raise DomainError("forbidden", "Project is outside Ordubashy's current scope")

    def prepare(self, ctx, args, options, uow):
        self.guard(ctx, None, uow)
        if not args["rationale"].strip():
            raise DomainError("validation_error", "Explain why the project should be archived")
        before = uow.repository("businesses").get(args["business_id"], required=True)
        require_revision(before, options.expected_revision)
        if before["archived_at"] is not None:
            raise DomainError("business_archived", "This project is already archived")
        builder = PlanBuilder(ctx, OPERATION, args, "Archive project: " + before["name"],
                              namespace="global", affected_scope={"kind": "business", "business_id": before["id"]})
        builder.record("businesses", before["id"], before,
            {"archived_at": builder.at, "archived_by": ctx.principal_id}, business_id=None, namespace="global", label=before["name"])
        target = ResourceRef("business_archive_agents", before["id"], None, "global")
        agents = self.agents(uow, before["id"])
        builder.targets[target] = ExpectedTarget(target, exists=True, content_hash=canonical_hash(agents))
        builder.policy_effect(target, OPERATION, "Reset this project's registered terminal sessions")
        builder.effects.extend([
            "Remove " + before["name"] + " (" + before["slug"] + ") from the active project menu, agent directory and normal project discovery.",
            "Reset project terminal sessions, stopping their running agents: " + (", ".join(row["name"] for row in agents) or "none registered") + ".",
            "Retain project records and files. No domain, mailbox or external-service changes."])
        plan = builder.build()
        self.guard(ctx, plan, uow)
        return plan

    def apply(self, ctx, plan, uow):
        self.guard(ctx, plan, uow)
        if self.reset_terminals is None:
            raise DomainError("operation_unavailable", "Project terminal reset is not installed")
        business = uow.repository("businesses").get(plan.arguments["business_id"], required=True)
        # Called only by the exact approved application. Database CAS prevents
        # business/profile changes during this bounded, idempotent terminal reset.
        self.reset_terminals(business, self.agents(uow, business["id"]))
        apply_records(ctx, plan, uow)
        saved = uow.repository("businesses").get(business["id"], required=True)
        return {"business_id": saved["id"], "archived": True, "revision": saved["revision"]}

    # Restore: the project resources return; no agent identity is touched.
    def restore(self, ctx, arguments, options):
        return self.application.invoke(ctx, RESTORE, arguments, options)

    def prepare_restore(self, ctx, args, options, uow):
        if ctx.business_id is not None or ctx.home_business_id is not None or ctx.principal_kind != "human":
            raise DomainError("forbidden", "Project restore is an owner action in Global")
        before = uow.repository("businesses").get(args["business_id"], required=True)
        require_revision(before, options.expected_revision)
        if before["archived_at"] is None:
            raise DomainError("business_not_archived", "This project is not archived")
        installation = uow.repository("installation").get("service", required=True)
        member = uow.repository("business_memberships", business_id=before["id"]).get(installation["owner_principal_id"])
        if not member or not member["active"] or not member["is_owner"]:
            raise DomainError("forbidden", "Restore only a project owned by the installation owner")
        business_id = before["id"]
        builder = PlanBuilder(ctx, RESTORE, args, "Restore project: " + before["name"],
                              namespace="global", affected_scope={"kind": "business", "business_id": business_id})
        builder.record("businesses", business_id, before, {"archived_at": None, "archived_by": None},
                                      business_id=None, namespace="global", label=before["name"])
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
        business_id = plan.arguments["business_id"]
        apply_records(ctx, plan, uow)
        return {"business_id": business_id, "restored": True}
