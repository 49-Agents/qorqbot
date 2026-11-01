"""A declared job consequence is authority-bound data, not a worker label."""

from uuid import UUID, uuid5

from qorqut_operations.contracts import DomainError, canonical_hash
from qorqut_operations.policy import WorkerProfile
from qorqut_operations.storage.assembly import StorageContribution

# Host tables the exact-output validation reads (``documents``: automatic work may not change mandatory instructions).
BACKGROUND_REQUIRED_TABLES = frozenset({"documents"})


def storage_contribution() -> StorageContribution:
    """The program authority's storage requirements, for the host's assembly. The engine owns ``jobs``,
    ``routines``, ``routine_runs`` and ``routine_slots``; ``documents`` is a host table."""
    return StorageContribution("background", required_tables=BACKGROUND_REQUIRED_TABLES)


class BackgroundAuthority:
    def __init__(self, database, programs):
        self.database, self.programs = database, programs
        programs.bind_authority(self)

    @staticmethod
    def installation(uow):
        row = uow.repository("installation").get("service")
        if row is None:
            raise DomainError("operation_unavailable", "Installation identity has not been migrated")
        return row["installation_id"]

    def worker_id(self, program_id, uow):
        self.programs.require(program_id, available=False)
        return str(uuid5(UUID(self.installation(uow)), "program:" + program_id))

    def register_workers(self, uow, business_ids):
        uow.require_write()
        ids = []
        for program in self.programs:
            identity = self.worker_id(program.id, uow)
            actor = uow.repository("principals").get(identity)
            if actor is None:
                uow.repository("principals").insert({"id": identity, "kind": "system", "display_name": program.id + " worker"})
            elif actor["kind"] != "system":
                raise DomainError("invalid_principal", "Program principal identity is occupied")
            for business_id in business_ids:
                membership = uow.repository("business_memberships", business_id=business_id)
                if membership.get(identity) is None:
                    membership.insert({"principal_id": identity})
            ids.append(identity)
        return ids

    @staticmethod
    def require_worker(principal_id, business_id, uow):
        actor = uow.repository("principals").get(principal_id)
        if not actor or actor["kind"] != "system" or not actor["active"]:
            raise DomainError("invalid_principal", "Registered program worker is missing or inactive",
                              details={'incident_classification':'setup_contract'})
        if business_id:
            member = uow.repository("business_memberships", business_id=business_id).get(principal_id)
            if not member or not member["active"]:
                raise DomainError("forbidden", "Worker is not registered for this business")

    def worker_profiles(self, principal_id, uow):
        for program in self.programs:
            if self.worker_id(program.id, uow) == principal_id:
                self.require_worker(principal_id, None, uow)
                businesses = frozenset(x[0] for x in uow.connection.execute(
                    "SELECT business_id FROM business_memberships WHERE principal_id=? AND active=1", (principal_id,)))
                revision = int(program.definition["digest"][:15], 16) + 1
                return WorkerProfile(program.permissions, businesses, frozenset(program.global_capabilities), revision)
        return None

    def _job(self, context, uow):
        if context.principal_kind != "system" or context.source != "worker" or context.job_id is None:
            raise DomainError("forbidden", "Declared outputs require an internal job context")
        row = uow.repository("jobs", business_id=context.business_id).get(context.job_id, required=True)
        program = self.programs.require(row["kind"])
        self.require_worker(context.principal_id, context.business_id, uow)
        if (row["principal_id"] != context.principal_id or context.principal_id != self.worker_id(program.id, uow)
                or row["initiated_by_principal_id"] != context.initiated_by_principal_id
                or row["definition_ref_json"].get("digest") != program.definition["digest"]):
            raise DomainError("policy_denied", "Job identity or frozen program definition changed")
        if row["state"] not in {"running", "applying", "waiting_signoff", "succeeded", "failed"}:
            raise DomainError("policy_denied", "Job is not authorized to apply outputs")
        result = row["result_json"] or {}
        outputs = result.get("outputs", [])
        if outputs and result.get("outputs_hash") != canonical_hash(outputs):
            raise DomainError("invalid_plan", "Stored job output binding is invalid")
        return row, program, outputs

    def verify_consequence(self, context, descriptor, targets, uow):
        if context.job_id is None:
            return None
        row, program, outputs = self._job(context, uow)
        if descriptor.name in program.discretionary:
            return None
        if not outputs or descriptor.name not in program.permissions:
            return None
        if not any(output["operation"] in program.outputs for output in outputs):
            return None
        for target in targets:
            resource = getattr(target, "resource", target)
            # An action's business membership is part of the canonical action record it
            # belongs to, so declaring action_logs covers it without changing frozen digests.
            membership = resource.resource_type == "action_businesses" and "action_logs" in program.resource_types
            if resource.business_id != row["business_id"] or (resource.resource_type not in program.resource_types and not membership):
                raise DomainError("forbidden", "Job output exceeds its declared resource kinds or business")
        return {"job_id": row["id"], "definition_digest": program.definition["digest"]}

    def validate_plan(self, context, descriptor, plan, uow):
        if context.job_id is None:
            return
        _, program, outputs = self._job(context, uow)
        if descriptor.name in program.discretionary:
            return
        if descriptor.name not in program.outputs:
            raise DomainError("forbidden", "Program cannot originate this business operation")
        matches = [output for output in outputs if output["operation"] == descriptor.name
                   and canonical_hash(output["arguments"]) == canonical_hash(plan.arguments)]
        if not matches:
            raise DomainError("invalid_plan", "Prepared arguments differ from the exact frozen job output")
        if any("expected_revision" in output.get("options", {}) for output in matches):
            source_id = next((plan.arguments[key] for key in ("document_id", "upload_id") if key in plan.arguments), None)
            revisions = {target.revision for target in plan.expected_targets if target.resource.resource_id == source_id
                and target.resource.resource_type == "documents"}
            if not any(output.get("options", {}).get("expected_revision") in revisions for output in matches):
                raise DomainError("invalid_plan", "Prepared source revision differs from the frozen job output")
        self.verify_consequence(context, descriptor, plan.expected_targets, uow)
        for change in plan.changes:
            if change.resource.resource_type == "documents":
                document = uow.repository("documents", business_id=change.resource.business_id).get(change.resource.resource_id)
                if document and document["kind"] in {"pillar", "shared_instruction", "shared_instructions", "mandatory_brief", "agent_instructions"}:
                    raise DomainError("forbidden", "Automatic context work cannot change mandatory instructions")
