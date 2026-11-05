"""The owner-only Context usage read ``context_filing_costs``: recorded call facts, their aggregates and the collection coverage.

One registration (the previous reader and its dependence on the retired context-base cost ledger are gone). The request is a
closed object; the answer carries the usage ledger's calls and SQL-backed aggregates for the selected call-start dates plus a
separate coverage inventory of every reserved Context event. It reads only the operational database: it never invokes the
collector, reads private Context files or contacts a model or provider, so it shows the last recorded collection facts and
promises no live totals. Native thread and turn IDs and bounded error text stay on this owner-only read.
"""

from __future__ import annotations

import sqlite3

from qorqut_context.integration import usage as context_usage
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import IDENTITY, schema
from qorqut_operations.contracts import DomainError

from qorqut_context.integration.usage.coverage import collection_coverage

FORMAT = "qorqut-context-usage-v1"
OPERATION = "context_filing_costs"
OPTIONS = {
    "input_id": IDENTITY,
    "start_date": {"type": "string", "format": "date"},
    "end_date": {"type": "string", "format": "date"},
    "limit": {"type": "integer", "minimum": 1, "maximum": context_usage.MAX_PAGE},
    "cursor": {"type": "string", "maxLength": 1024},
}
DESCRIPTION = ("Owner view: recorded Context model-call usage across all projects for an optional input and an inclusive UTC "
               "date range of call start times: calls with exact USD where a charge was recorded (unknown stays null), "
               "aggregates, and a separate coverage inventory of reserved Context events and their last scans. A known "
               "subtotal, not final totals, from the last recorded collection; reading never collects.")


class ContextUsageOperations:
    def register(self, registry):
        registry.register(OperationDescriptor(OPERATION, schema(OPTIONS), effect_kind="read", supports_signoff=False,
            exposure={"dashboard", "worker"}, owner_only=True, description=DESCRIPTION, scope_kinds=frozenset({"global"}),
            global_capability="read_global_context", resolve_namespace=lambda _c, _a: "global", read=self.read))
        return self

    def read(self, ctx, args, options, uow):
        if ctx.principal_kind != "human":
            raise DomainError("forbidden", "Context usage is an owner view")
        owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
        if not owner or owner["owner_principal_id"] != ctx.principal_id:
            raise DomainError("forbidden", "Only the installation owner may read Context usage")
        input_id = args.get("input_id")
        try:
            if input_id is not None and uow.connection.execute("SELECT 1 FROM context_inputs WHERE id=?", (input_id,)).fetchone() is None:
                raise DomainError("not_found", "Context input does not exist")
            page = context_usage.list_calls(uow, input_id=input_id, start_date=args.get("start_date"), end_date=args.get("end_date"),
                                            limit=args.get("limit", 50), cursor=args.get("cursor"))
            inventory = collection_coverage(uow.connection, input_id)
        except sqlite3.OperationalError as error:
            raise DomainError("operation_unavailable", "The Context usage ledger is not installed on this installation") from error
        return {"format": FORMAT, "currency": "USD", "calls": [call.as_dict() for call in page.calls],
                "next_cursor": page.next_cursor, "aggregates": page.aggregates.as_dict(), "coverage": inventory,
                "usage_totals_final": False, "cost_is_known_subtotal": True}
