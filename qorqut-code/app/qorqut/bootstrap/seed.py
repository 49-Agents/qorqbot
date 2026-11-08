"""Trusted one-time registration helpers for the shared instruction documents and the initial seed application.

Project creation is not here: the one creator is the Global ``business_create`` operation (``domains/business_creation``).
This module does not scaffold a project, write a workspace or provision any repository. The three initial shared
instructions are package resources of the Agentic layer; a registered
document is the authoritative editable record afterwards, and a changed packaged seed never overwrites it.
"""

from __future__ import annotations

from qorqut_agentic.behavior.sources import seed_bytes

from qorqut.catalog.packages import content_hash
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.document_history.files import seed_document
from qorqut_operations.storage.files import FileApplier
from qorqut_operations.storage.receipts import ApplyReceipts
from .preparation import _write_file


# Global authored shared instructions live under operations/files; the published Context graph is operations/context-graph (engine-owned).
INSTRUCTIONS = "files/instructions"
# (bootstrap key, path relative to the packaged ``instructions/`` resources and to ``files/instructions``).
INSTRUCTION_SEEDS = (("foundation", "agent-foundation.md"), ("ecosystem-agent", "operating/ecosystem-agent.md"),
                     ("ecosystem", "ecosystem.md"))


def instruction_seed(relative: str) -> bytes:
    """The packaged initial bytes of one shared instruction; there is no sibling checkout or environment-selected root."""
    return seed_bytes("host", "app/qorqut/bootstrap/instructions/" + relative)


def register_context_sources(database, paths):
    """Register the initial shared instructions once; existing indexed bytes must agree.

    The registered document is looked up before any seed is materialized. Only an unregistered document whose path is
    absent receives the packaged initial bytes. An existing registered document is never rewritten and is never
    recreated from the packaged seed: a missing file, or a file and record that disagree, needs explicit reconciliation
    before seeding, and is refused with nothing written."""
    result = []
    for key, relative in INSTRUCTION_SEEDS:
        document_path = f"{INSTRUCTIONS}/{relative}"
        path = paths.operations / document_path
        with database.transaction() as uow:
            repository = uow.repository("documents", business_id=None)
            existing = repository.list(filters={"namespace": "global", "path": document_path}, limit=1)
            if existing and not path.exists():
                raise DomainError("document_conflict", "Registered context file is missing and requires explicit reconciliation before seeding")
            if not existing and not path.exists():
                _write_file(path, instruction_seed(relative))
            body = path.read_bytes()
            digest = content_hash(body)
            if existing:
                row = existing[0]
                if row["content_hash"].removeprefix("sha256:") != digest.removeprefix("sha256:") or row["metadata_json"].get("bootstrap_key") != key:
                    raise DomainError("document_conflict", "Registered context requires explicit reconciliation before seeding")
            else:
                row = repository.insert({"id": new_id(), "namespace": "global", "path": document_path,
                    "kind": "shared_instruction", "content_hash": digest,
                    "metadata_json": {"bootstrap_key": key, "authored": True}})
                seed_document(uow.connection, row, body, author="system:register_context_sources",
                                      operation="register_context_sources")
            result.append(row)
    return result


def apply_initial_plan(database, ctx, plan, operations, file_resolver, locks):
    """Trusted one-time seed application through the shared durable file protocol."""
    with database.transaction() as uow:
        receipt = ApplyReceipts(uow).create(plan)
    FileApplier(database, file_resolver, locks=locks).apply(receipt["id"])
    with database.transaction() as uow:
        result = operations.apply(ctx, plan, uow)
        ApplyReceipts(uow).finish(receipt["id"], result)
    return result
