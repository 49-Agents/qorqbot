"""Task identities: short generated ids, unique across every scope.

A new Task gets ``TASK_ID_LENGTH`` characters from ``TASK_ID_ALPHABET``: lowercase letters and digits without the
look-alikes (no 0/o, 1/l/i), so an id can be read aloud, typed from a screenshot and quoted in chat. Change the length
here and nowhere else; existing ids keep whatever length they were made with.

Storage keys a Task by ``(project_id, id)``, and older installations hold the same id in more than one project (slug ids
and a few UUIDs), so no unique index can say "one Task per id". The writer says it instead: a generated id that is
already used anywhere, archived and moved Tasks included (and any id a relocation recorded), is drawn again, and a caller-supplied id that is already used
anywhere is refused without saying where (``taken``). Ids compare without regard to case, so a lookup that ignores case
never finds two new Tasks for one id. Existing Tasks keep their ids."""

from __future__ import annotations

import secrets

from qorqut_operations.contracts import DomainError

TASK_ID_LENGTH = 5
TASK_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
# Draws before giving up. With a nearly empty id space a repeat is already rare; twenty in a row means something other
# than chance (a broken random source or a full space), which a caller should see rather than wait on.
TASK_ID_ATTEMPTS = 20


def generate() -> str:
    """One random candidate id; ``new_task_id`` is the allocator that checks it."""
    return "".join(secrets.choice(TASK_ID_ALPHABET) for _ in range(TASK_ID_LENGTH))


def taken(connection, identity) -> bool:
    """Whether any Task in any scope (Global, every project, archived included) already has ``identity``, or a relocation
    ever recorded it. A moved Task's id stays reserved even after that Task is gone: history left in the scopes it moved
    out of (relationships, discussion references) names it there, and a new Task with the id would inherit it."""
    return connection.execute("SELECT 1 FROM tasks WHERE id = ? COLLATE NOCASE UNION ALL "
                              "SELECT 1 FROM task_moves WHERE task_id = ? COLLATE NOCASE LIMIT 1", (identity, identity)).fetchone() is not None


def new_task_id(connection) -> str:
    """A new Task id that no Task uses yet, drawn with ``generate``."""
    for _ in range(TASK_ID_ATTEMPTS):
        identity = generate()
        if not taken(connection, identity):
            return identity
    raise DomainError("operation_unavailable", "No free Task id was found; try again", retryable=True)


def in_use() -> DomainError:
    """The refusal of a caller-supplied id that another Task already has. It names no scope: the other Task may be in a
    project the caller cannot read."""
    return DomainError("validation_error", "This Task id is already in use; leave id out to get a new one",
                       field_errors={"id": "already in use"})


__all__ = ["TASK_ID_ALPHABET", "TASK_ID_ATTEMPTS", "TASK_ID_LENGTH", "generate", "in_use", "new_task_id", "taken"]
