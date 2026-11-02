"""Integration example: a host saves a project brief and completes a task.

Hypothetical host tables live beside the history tables in one in-memory
database. Each host update and its history entries share one transaction, so
they commit or roll back together. This is not an installed Qorqut adapter.

Authorization is the host's job: before calling the library, the host checks
that the actor may edit this scope's brief or complete this task, and obtains
any owner confirmation its policy requires. The library records the result and
does not approve anything.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from qorqut_business_docs_versioning import HistoryStore, initialize_schema

BUSINESS_ID = "7f1c2d9e-0000-4000-8000-000000000001"
NAMESPACE = f"business:{BUSINESS_ID}"


@contextmanager
def transaction(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def now():
    return datetime.now(timezone.utc).isoformat()


def save_brief(connection, history, content, actor_id, expected_version, event_id):
    # The host has already authorized actor_id for this scope before this call.
    saved = history.save_document(
        NAMESPACE, "project-brief", content, actor_id,
        expected_version=expected_version, reason="Brief edit",
    )
    connection.execute(
        "UPDATE businesses SET brief_version = ?, brief_updated_at = ? WHERE id = ?",
        (saved.version, saved.created_at, BUSINESS_ID),
    )
    history.append_event(
        BUSINESS_ID, event_id, "document.updated", actor_id, f"Project brief saved as version {saved.version}",
        subject_type="document", subject_id="project-brief", occurred_at=saved.created_at,
        data={"namespace": NAMESPACE, "version": saved.version},
    )
    return saved


def complete_task(connection, history, task_id, actor_id, event_id):
    # Only a task of this business that the host actually updated gets a completion event.
    row = connection.execute(
        "SELECT title FROM tasks WHERE id = ? AND business_id = ?", (task_id, BUSINESS_ID)
    ).fetchone()
    if row is None:
        raise LookupError(f"task {task_id!r} not found in business {BUSINESS_ID}")
    title = row[0]
    completed_at = now()
    updated = connection.execute(
        "UPDATE tasks SET status = 'done' WHERE id = ? AND business_id = ?", (task_id, BUSINESS_ID)
    )
    if updated.rowcount != 1:
        raise LookupError(f"task {task_id!r} was not updated in business {BUSINESS_ID}")
    history.append_event(
        BUSINESS_ID, event_id, "task.completed", actor_id, f"Completed: {title}",
        subject_type="task", subject_id=task_id, occurred_at=completed_at,
    )


def create_example_database():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    with transaction(connection):
        connection.execute(
            "CREATE TABLE businesses (id TEXT PRIMARY KEY, name TEXT, brief_version INTEGER, brief_updated_at TEXT)"
        )
        connection.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, business_id TEXT, title TEXT, status TEXT)")
        connection.execute("INSERT INTO businesses (id, name) VALUES (?, 'Example Co')", (BUSINESS_ID,))
        connection.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, 'open')",
            [("task-1", BUSINESS_ID, "Draft outline"), ("task-2", BUSINESS_ID, "Review notes")],
        )
        initialize_schema(connection)
    return connection, HistoryStore(connection)


def main():
    connection, history = create_example_database()

    # Committed: brief version 1 plus a task completion, each with its activity event.
    with transaction(connection):
        save_brief(connection, history, "# Brief\n\nShip the first release.\n", "agent:assistant", 0, "evt-brief-1")
    with transaction(connection):
        complete_task(connection, history, "task-1", "agent:assistant", "evt-task-1")

    # Rolled back: a brief edit and a second completion fail together, leaving no trace.
    try:
        with transaction(connection):
            save_brief(
                connection, history, "# Brief\n\nShip the next release.\n", "agent:assistant", 1, "evt-brief-2"
            )
            complete_task(connection, history, "task-2", "agent:assistant", "evt-task-2")
            raise RuntimeError("host failure after the history writes")
    except RuntimeError as error:
        print(f"rolled back: {error}")

    latest = history.get_document(NAMESPACE, "project-brief")
    brief_version = connection.execute("SELECT brief_version FROM businesses").fetchone()[0]
    statuses = dict(connection.execute("SELECT id, status FROM tasks ORDER BY id"))
    print(f"brief: history version {latest.version}, host brief_version {brief_version}")
    print(f"tasks: {statuses}")
    for event in history.list_events(BUSINESS_ID).events:
        print(f"event #{event.sequence} {event.kind}: {event.summary}")

    assert latest.version == brief_version == 1
    assert statuses == {"task-1": "done", "task-2": "open"}
    assert [e.event_id for e in history.list_events(BUSINESS_ID).events] == ["evt-task-1", "evt-brief-1"]
    connection.close()


if __name__ == "__main__":
    main()
