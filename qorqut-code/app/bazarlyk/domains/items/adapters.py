"""The host knowledge behind the Operations ``RelationshipPorts``: the workflow-role guidance, the Material rule for historical
email-message documents, the direct-archive record tables and the Context navigation counts.

The role vocabulary, the Material classification and composed archive adapters and the reviewable edit operations are
Operations' (``qorqut_operations.records.adapters``); this module supplies only what is the host's and assembles the one
``RelationshipPorts``. None of them can replace application policy."""

from qorqut_operations.application.plans import utc_clock
from qorqut_operations.contracts import DomainError
from qorqut_operations.items import UNAVAILABLE
from qorqut_operations.records.adapters import (NOT_A_MATERIAL, REVIEW_EDITS, ROLES, RecordArchive,
                                                RecordMaterials)
from qorqut_operations.relationships import RelationshipPorts

WORKFLOW_ROLES = {"assigned_to": "tasks_assign", "blocked_by": "tasks_update"}
# The record tables that are archived by a plain ``archived`` flag on the row.
DIRECT_RECORD_TABLES = frozenset({"context_entities"})


class HostMaterialRules:
    """The Material classification rule for historical email-message documents: an individual message is not classified.
    No host record has a fixed Material type beyond the raw and content document kinds Operations already fixes."""

    def validate_material(self, ctx, table, row, material, uow):
        if row.get("kind") == "email_message":
            raise DomainError("workflow_required", NOT_A_MATERIAL)

    def fixed_material(self, table, row):
        return False


class HostNavigation:
    """``context_navigation_get``: authorized table counts plus the shared Context's person count.

    The person count comes only from the published graph's ``basic_entity_type=["person"]`` nodes; the graph is one shared
    set, so it does not depend on the business. There is no fallback to the operational people items or the old document
    store: when the graph cannot be read ``counts.person`` is ``null`` (unknown) and ``context`` says why. ``count=0`` is
    reserved for a ready graph that genuinely has no matching node. The result is::

        {"navigation_only": true, "counts": {table: int, person: int | null},
         "context": {"status": "ready" | "not_initialized" | "unavailable", "reason": null | {code, message},
                     "revision": <published revision read> | null}}

    A renderer shows ``person: null`` as unknown (not zero) and may use ``context.status`` / ``context.reason`` to say so."""

    def __init__(self, items, documents):
        self.items, self.documents = items, documents

    def read(self, ctx, args, uow):
        counts = {}
        for kind in ('table',):
            count = 0
            for item in self.items.candidates(ctx, uow, kind=kind):
                try: value = self.items.resolve(ctx, item, uow)
                except DomainError as error:
                    if error.code not in UNAVAILABLE: raise
                    continue
                if not value['raw_source'] and not value['archived']: count += 1
            counts[kind] = count
        counts['person'], context = self._people(ctx, uow)
        return {'navigation_only': True, 'counts': counts, 'context': context}

    def _people(self, ctx, uow):
        reader = getattr(self.documents, "shared_context", None)
        if reader is None:
            return None, {"status": "unavailable", "revision": None,
                          "reason": {"code": "not_configured", "message": "No shared published Context is configured"}}
        self.items.authorize(ctx, "context_find", uow)
        published = reader.count_nodes({"basic_entity_type": ["person"]})
        if published["status"] == "ready":
            return published["count"], {"status": "ready", "reason": None, "revision": published["revision"]}
        return None, {"status": published["status"], "reason": published.get("reason"), "revision": published.get("revision")}


def relationship_ports(items, *, documents, clock=utc_clock) -> RelationshipPorts:
    """The one ``RelationshipPorts`` of this host, over the already built item resolver."""
    return RelationshipPorts(roles=ROLES, workflow_roles=WORKFLOW_ROLES, materials=RecordMaterials(items, host=HostMaterialRules(), clock=clock),
                             archive=RecordArchive(items, documents, direct_record_tables=DIRECT_RECORD_TABLES, clock=clock),
                             navigation=HostNavigation(items, documents), review_edits=REVIEW_EDITS)
