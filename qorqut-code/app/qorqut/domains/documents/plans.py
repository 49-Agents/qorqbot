"""Agent plans and shared instructions: the host workflows over registered documents.

Plans (``plans_list``, ``plans_get``, ``plans_set``) are an agent's frozen legacy plan until its private working memory
replaces it, and shared instructions are the one Global instruction document; both depend on the host's working memory and
capabilities, so they stay host workflows. Every document write goes through the source service's store and helpers, passed
explicitly, so there is no second mutation path."""

from __future__ import annotations

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PAGE, UUID, page, rows, schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.documents.materials import classification, matches
from qorqut_operations.documents.operations import DocumentSources
from qorqut_operations.documents.serialization import BODY, HASH

PLAN_OPERATIONS = ("plans_list", "plans_get", "plans_set", "shared_instructions_update")
READS = {"plans_list": PAGE, "plans_get": {"agent_id": UUID}}
MUTATIONS = {
    "plans_set": ({"body": BODY, "expected_content_hash": HASH, "agent_id": UUID}, ("body",)),
    "shared_instructions_update": ({"document_id": UUID, "body": BODY, "expected_content_hash": HASH}, ("document_id", "body")),
}
INSTRUCTION_KINDS = {"context", "shared_instruction", "shared_instructions"}


class PlanOperations:
    """``sources`` is the Operations source service; ``working_memory`` is the host's ``WorkingMemory`` (required: a plan of an
    agent with private memory is read and frozen through it)."""

    def __init__(self, sources, working_memory):
        if working_memory is None:
            raise TypeError("Plans need the host's working memory")
        self.sources, self.store, self.memory = sources, sources.store, working_memory

    def register(self, registry):
        for name, properties in READS.items():
            registry.register(OperationDescriptor(name, schema(properties), effect_kind="read", supports_signoff=False,
                scope_kinds={"business", "agent_workspace"}, resolve_namespace=DocumentSources.own_namespace,
                exposure=frozenset({"mcp", "dashboard", "worker"}),
                read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow)))
        for name, (properties, required) in MUTATIONS.items():
            instruction = name == "shared_instructions_update"
            registry.register(DocumentSources.mutation_descriptor(name, properties, required,
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                apply=self.sources.apply, validate_targets=self.sources.validate_targets,
                scope_kinds={"global"} if instruction else {"business", "agent_workspace"},
                resolve_namespace=None if instruction else DocumentSources.own_namespace,
                global_capability="manage_shared_instructions" if instruction else None,
                exposure={"mcp", "dashboard", "worker"}))
        return self

    def _plan(self, ctx, uow, agent_id=None):
        profile = self.sources.active_agent(ctx, uow, agent_id)
        found = [row for row in rows(uow, "documents", profile["home_business_id"])
                 if row["kind"] == "plan" and row["owning_agent_id"] == profile["agent_id"]]
        if len(found) > 1: raise DomainError("invalid_configuration", "Agent has multiple registered plans")
        return profile, found[0] if found else None

    def read(self, operation, ctx, args, uow):
        if operation == "plans_get":
            profile, row = self._plan(ctx, uow, args.get("agent_id"))
            current = self.memory.row(profile['agent_id'], profile['home_business_id'], uow)
            if current:
                if ctx.business_id != current['business_id']:
                    raise DomainError('scope_required', 'Select the agent home scope to read migrated plans, or use working_memory_get in the current project')
                self.memory.access(ctx, {'agent_id': profile['agent_id']}, uow)
                self.memory.sources.authorize(ctx, 'working_memory_get', uow)
                return {'document': self.memory.view(ctx, current, uow), 'migrated': True}
            return {"document": self.store.view(row, uow) if row else None}
        found = [row for row in self.store.collection(ctx, uow, kinds={"plan"}) if matches(classification(row), args)]
        selected, cursor = page(found, args, ctx.business_id, operation)
        return {"items": [self.sources.present(self.store.view(row, uow), row, ctx, uow) for row in selected], "next_cursor": cursor}

    def prepare(self, operation, ctx, args, options, uow):
        if operation == "plans_set":
            builder = self.sources.builder(operation, ctx, args, namespace=DocumentSources.own_namespace(ctx, args))
            profile, before = self._plan(ctx, uow, args.get("agent_id"))
            self.sources.active_agent(ctx, uow, profile["agent_id"], write=True)
            if self.memory.row(profile['agent_id'], profile['home_business_id'], uow):
                raise DomainError('plan_migrated', 'Use working_memory_get/update with current revision, hash and evidence tokens; the legacy plan is frozen')
            prefix, business_id = self.sources.agent_prefix(profile, uow)
            self.store.write(builder, uow, kind="plan", before=before, path=prefix + "/PLAN.md", body=args["body"],
                metadata=(before or {}).get("metadata_json", {}), agent_id=profile["agent_id"], business_id=business_id,
                namespace=builder.namespace if business_id is None else None, expected_revision=options.expected_revision,
                expected_content_hash=args.get("expected_content_hash"))
        elif operation == "shared_instructions_update":
            builder = self.sources.builder(operation, ctx, args, namespace="global")
            row = self.store.get(ctx, args["document_id"], uow, kinds=INSTRUCTION_KINDS, write=True)
            self.sources.note_target(ctx, args, row)
            if row["business_id"] is not None or row["owning_agent_id"] is not None:
                raise DomainError("forbidden", "Shared instructions must be a registered global document")
            namespace = builder.namespace if row["business_id"] is None else None
            self.sources.edit_document(builder, uow, row, args, options, namespace=namespace)
        else:
            raise DomainError("operation_unavailable", "Document operation is not implemented")
        return self.sources.finalize(builder, uow, operation, args)
