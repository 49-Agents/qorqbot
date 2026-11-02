"""Owner journal boundary, independent of role grants and signoff overrides."""

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.team_channel import installation_owner_id

JOURNAL_KINDS = frozenset({"global_note", "business_note", "journal"})


def is_owner(ctx, uow):
    return (ctx.principal_kind == "human" and not ctx.delegation_assignment_id
            and ctx.principal_id == installation_owner_id(uow))


def require_owner(ctx, uow):
    if not is_owner(ctx, uow):
        raise DomainError("forbidden", "Only the owner may change the personal journal")


def entries(uow, *, include_archived=False):
    # Retain original IDs, files and business links. Global is the sole journal
    # interface; moving the physical files would break historical references.
    query = "SELECT * FROM documents WHERE kind IN ('global_note','business_note','journal')"
    if not include_archived:
        query += " AND archived=0"
    return [decode_row(row) for row in uow.connection.execute(query)]
