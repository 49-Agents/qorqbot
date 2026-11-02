"""The host's registration of the owner-only document-history reads.

The readers are generic and live in ``qorqut_operations.document_history.reads``; this host adapter supplies what only the host
knows: the learning sources (each resolved through its own repository, with a deleted row keeping its history only in the scope
that owned it) and the private-memory exclusion (working-memory history is read with ``working_memory_get`` alone, which keeps
its source-visibility checks). The shared Context's history is its published Git graph and is read with ``context_object`` and
``context_changes``, not here."""

from __future__ import annotations

from qorqut_operations.contracts import DomainError
from qorqut_operations.document_history.reads import DocumentHistory as DocumentHistoryReads
from qorqut_operations.document_history.text import latest, namespace
from bazarlyk.storage import history

SOURCE_KINDS = ("document", "learning_candidate", "learning_decision")
SQL_SOURCES = history.SOURCE_KINDS


def learning_source(kind):
    """The resolver of one learning source: (namespace, history document ID, current domain revision)."""
    table = SQL_SOURCES[kind]

    def resolve(ctx, source_id, uow):
        space = namespace(ctx.business_id)
        row = uow.repository(table, business_id=ctx.business_id).get(source_id)
        prefix = history.SQL_DOCUMENTS[table][0]
        document_id = f"{prefix}:{source_id}"
        if row is None:
            # A deleted row keeps its history only in the scope that owned it.
            if latest(uow.connection, space, document_id) is None:
                raise DomainError("not_found", "Document not found")
            return space, document_id, None
        return space, history.sql_document_id(table, row), row.get("revision")
    return resolve


class DocumentHistory(DocumentHistoryReads):
    def __init__(self, documents):
        super().__init__(documents.store, source_resolvers={kind: learning_source(kind) for kind in SOURCE_KINDS[1:]},
                         private_kinds={history.WORKING_MEMORY: "Working-memory history is read with working_memory_get"})
