"""Owner journal boundary, independent of role grants and signoff overrides."""

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.team_channel import installation_owner_id

JOURNAL_KINDS = frozenset({"global_note", "business_note", "journal"})
JOURNAL_WRITES = frozenset({'journal_entry_create', 'journal_entry_update', 'journal_entry_archive'})


def is_owner(ctx, uow):
    return (ctx.principal_kind == "human" and not ctx.delegation_assignment_id
            and ctx.principal_id == installation_owner_id(uow))


def require_owner(ctx, uow):
    if not is_owner(ctx, uow):
        raise DomainError("forbidden", "Only the owner may change the personal journal")


def require_writer(ctx, uow, operation):
    """Only explicit journal operations extend the writer boundary to the coordinator."""
    if is_owner(ctx, uow):
        return
    from qorqut_operations.global_agents import is_coordinator
    if (operation in JOURNAL_WRITES and ctx.principal_kind == 'agent'
            and is_coordinator(uow.connection, ctx.principal_id)
            and ctx.business_id is None and ctx.home_business_id is None and not ctx.delegation_assignment_id):
        return
    require_owner(ctx, uow)


def entries(uow, *, include_archived=False):
    # Retain original IDs, files and business links. Global is the sole journal
    # interface; moving the physical files would break historical references.
    query = "SELECT * FROM documents WHERE kind IN ('global_note','business_note','journal')"
    if not include_archived:
        query += " AND archived=0"
    return [decode_row(row) for row in uow.connection.execute(query)]
