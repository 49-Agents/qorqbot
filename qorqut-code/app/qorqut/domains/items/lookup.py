"""The host's records and rules beside the Operations item lookups: the configured-global agent identity.

``HostLookup`` is the ``LookupHost`` of ``qorqut_operations.records.lookup``: each method is called by the Operations
``RecordItems`` / ``RecordCandidates`` at one fixed point of their flow and grants nothing by itself. The owner-only
journal and excluded working-memory guards, the active-member rules and the authorized domain reads stay in Operations.
The host has no record tables of its own to resolve, name, fingerprint or enumerate, so every hook but ``current_agent``
declines."""

from qorqut.agents.global_scope import active_global


class HostLookup:
    def material_resource(self, ctx, identity, uow):
        return None

    def current_agent(self, uow, row):
        return row["home_business_id"] is None and active_global(uow.connection, row["agent_id"]) is not None

    def fallback_subtype(self, table, row):
        return None

    def relationship_item(self, ctx, item, table, row, uow):
        return item

    def fingerprint_content(self, ctx, table, row, content, uow):
        return False

    def include_candidate(self, ctx, table):
        return True
