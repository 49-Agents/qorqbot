"""The Context publication of source documents: the filing fields composed over an upload read.

Source documents (``qorqut_operations.documents``) know nothing of Context filing. This module holds the one current-source
filing projection and applies it to a readable upload in the two places a read is composed: an unscoped read of the source
service (``present_view``) and an item of the scoped source library (``present_item``). Every reader of an upload's filing takes
it from here, so there is no second interpretation of the queue, publication or completion."""

from __future__ import annotations

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.repositories import decode_row

from .filing import PRESENTED_KINDS


class ContextPublication:
    """``filing`` is the host's ``ContextFiling`` (its one current-source projection), or None when filing is not composed."""

    def __init__(self, filing=None):
        self.filing = filing

    def source_filing(self, uow, ctx, row):
        """The one current-source filing projection of a raw upload, or None when the document is not one or filing is not composed."""
        if row["kind"] not in PRESENTED_KINDS or self.filing is None:
            return None
        return self.filing.document_filing(uow, ctx, row)[0]

    def job_filing(self, uow, ctx, job):
        """The Context filing a registered job is bound to, or None (also when filing is not composed)."""
        return None if self.filing is None else self.filing.job_filing(uow, ctx, job)

    def present_view(self, view, row, ctx, uow):
        """Add the applicable current filing and the status derived from it to a readable upload."""
        if row["kind"] in PRESENTED_KINDS and self.filing is not None:
            filing, status = self.filing.document_filing(uow, ctx, row)
            view["filing"] = filing
            view["metadata"] = {**view["metadata"], "status": status}
        return view

    def present_item(self, item, row, ctx, uow):
        """A raw upload's library summary with its current-source ``filing`` and the ``filing_status`` summary derived from it.

        The projection is the same one the unscoped reads attach, taken from the registered row (never the relationship-projected
        copy the library selects). A source whose file is unreadable or unfinished has no projection (``filing`` and
        ``filing_status`` are null): that is an unavailable source, not an unfiled or processed one. Other documents are unchanged."""
        if self.filing is None or row["kind"] not in PRESENTED_KINDS:
            return item
        filing = None
        registered = decode_row(uow.connection.execute("SELECT * FROM documents WHERE id=?", (row["id"],)).fetchone())
        if registered is not None and registered["availability"] == "ready":
            try:
                filing = self.filing.document_filing(uow, ctx, registered)[0]
            except DomainError as error:
                if error.code not in {"document_unavailable", "invalid_document", "busy"}:
                    raise
            except OSError:
                pass
        return {**item, "filing": filing, "filing_status": None if filing is None else filing["status"]}
