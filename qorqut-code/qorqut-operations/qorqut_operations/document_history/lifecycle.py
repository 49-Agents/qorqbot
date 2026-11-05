"""Document availability around a file receipt.

The engine's receipts call these at the points the deployed receipts touched the ``documents`` table: claiming a document
for application, marking it conflicted, ready again after a verified application, or released unapplied. Documents declare
this lifecycle required, so a composition without it is refused."""

from __future__ import annotations

RESOURCE_TYPE = "documents"


class DocumentLifecycle:
    @staticmethod
    def _availability(uow, resource, value):
        uow.connection.execute("UPDATE documents SET availability = ? WHERE id = ?", (value, resource.resource_id))

    def applying(self, uow, resource):
        self._availability(uow, resource, "applying")

    def conflicted(self, uow, resource):
        self._availability(uow, resource, "conflicted")

    def applied(self, uow, resource):
        uow.completed_file_documents.add((resource.business_id, resource.resource_id))
        self._availability(uow, resource, "ready")

    def released(self, uow, resource):
        self._availability(uow, resource, "ready")
