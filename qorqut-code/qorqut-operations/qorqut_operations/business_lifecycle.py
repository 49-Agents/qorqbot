"""The active directory is independent of retained business records and grants."""

from qorqut_operations.contracts import DomainError


def active_business_ids(connection):
    return frozenset(row[0] for row in connection.execute(
        "SELECT id FROM businesses WHERE archived_at IS NULL"))


def in_active_business(column):
    """SQL that holds when ``column`` names no project or a project that is not archived.

    Cross-project reads use it to leave archived projects' records out entirely: an archived project is not current
    work, so its open Tasks, reminders and history are not listed or counted for agents."""
    return (f"NOT EXISTS (SELECT 1 FROM businesses archived_project WHERE archived_project.id={column} "
            "AND archived_project.archived_at IS NOT NULL)")


def is_archived_business(connection, business_id):
    if business_id is None:
        return False
    row = connection.execute("SELECT archived_at FROM businesses WHERE id=?", (business_id,)).fetchone()
    return row is not None and row[0] is not None


def require_active_business(connection, business_id):
    if business_id is not None:
        row = connection.execute("SELECT archived_at FROM businesses WHERE id=?", (business_id,)).fetchone()
        if row is not None and row["archived_at"] is not None:
            raise DomainError("business_archived", "This project is archived and is excluded from active work")
