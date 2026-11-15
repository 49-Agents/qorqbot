"""Shared instructions: the one Global instruction document, a host workflow over registered documents.

It depends on the host's capabilities (``manage_shared_instructions``), so it stays a host workflow. Every document write goes
through the source service's store and helpers, passed explicitly, so there is no second mutation path."""

from __future__ import annotations

from qorqut_operations.application.plans import UUID
from qorqut_operations.contracts import DomainError
from qorqut_operations.documents.operations import DocumentSources
from qorqut_operations.documents.serialization import BODY, HASH

OPERATION = "shared_instructions_update"
PROPERTIES = {"document_id": UUID, "body": BODY, "expected_content_hash": HASH}
INSTRUCTION_KINDS = {"context", "shared_instruction", "shared_instructions"}


class SharedInstructions:
    """``sources`` is the Operations source service."""

    def __init__(self, sources):
        self.sources, self.store = sources, sources.store

    def register(self, registry):
        registry.register(DocumentSources.mutation_descriptor(OPERATION, PROPERTIES, ("document_id", "body"),
            prepare=self.prepare, apply=self.sources.apply, validate_targets=self.sources.validate_targets,
            scope_kinds={"global"}, resolve_namespace=None, global_capability="manage_shared_instructions",
            exposure={"mcp", "dashboard", "worker"}))
        return self

    def prepare(self, ctx, args, options, uow):
        builder = self.sources.builder(OPERATION, ctx, args, namespace="global")
        row = self.store.get(ctx, args["document_id"], uow, kinds=INSTRUCTION_KINDS, write=True)
        self.sources.note_target(ctx, args, row)
        if row["business_id"] is not None or row["owning_agent_id"] is not None:
            raise DomainError("forbidden", "Shared instructions must be a registered global document")
        namespace = builder.namespace if row["business_id"] is None else None
        self.sources.edit_document(builder, uow, row, args, options, namespace=namespace)
        return self.sources.finalize(builder, uow, OPERATION, args)
