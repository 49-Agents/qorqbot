"""The active directory is independent of retained business records and grants."""

from qorqut_operations.contracts import DomainError


def active_business_ids(connection):
    return frozenset(row[0] for row in connection.execute(
        "SELECT id FROM businesses WHERE archived_at IS NULL"))


def require_active_business(connection, business_id):
    if business_id is not None:
        row = connection.execute("SELECT archived_at FROM businesses WHERE id=?", (business_id,)).fetchone()
        if row is not None and row["archived_at"] is not None:
            raise DomainError("business_archived", "This project is archived and is excluded from active work")
