"""Task identities: short generated ids, unique across every scope.

A new Task gets ``TASK_ID_LENGTH`` characters from ``TASK_ID_ALPHABET``: lowercase letters and digits without the
look-alikes (no 0/o, 1/l/i), so an id can be read aloud, typed from a screenshot and quoted in chat. Change the length
here and nowhere else; existing ids keep whatever length they were made with.

Storage keys a Task by ``(business_id, id)``, and older installations hold the same id in more than one project (slug ids
and a few UUIDs), so no unique index can say "one Task per id". The writer says it instead: a generated id that is
already used anywhere, archived and moved Tasks included, is drawn again, and a caller-supplied id that is already used
anywhere is refused without saying where (``taken``). Ids compare without regard to case, so a lookup that ignores case
never finds two new Tasks for one id. Existing Tasks keep their ids."""

from __future__ import annotations

import re
import secrets

from qorqut_operations.contracts import DomainError

TASK_ID_LENGTH = 5
TASK_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
# Draws before giving up. With a nearly empty id space a repeat is already rare; twenty in a row means something other
# than chance (a broken random source or a full space), which a caller should see rather than wait on.
TASK_ID_ATTEMPTS = 20
# A token of written text that may be a quoted Task id: drawn from the alphabet, as long as a generated id is or has been,
# standing alone, and holding both a letter and a digit. An ordinary word or a plain number never matches, so it is never
# mistaken for an id; the price is that an id that happens to be all letters or all digits goes unnoticed in text.
MENTION = re.compile(rf"(?<![0-9A-Za-z_-])(?=[{TASK_ID_ALPHABET}]*[2-9])(?=[{TASK_ID_ALPHABET}]*[a-z])"
                     rf"[{TASK_ID_ALPHABET}]{{5,{max(5, TASK_ID_LENGTH)}}}(?![0-9A-Za-z_-])", re.IGNORECASE)
MENTIONS_CHECKED = 200


def generate() -> str:
    """One random candidate id; ``new_task_id`` is the allocator that checks it."""
    return "".join(secrets.choice(TASK_ID_ALPHABET) for _ in range(TASK_ID_LENGTH))


def taken(connection, identity) -> bool:
    """Whether any Task in any scope (Global, every project, archived included) already has ``identity``."""
    return connection.execute("SELECT 1 FROM tasks WHERE id = ? COLLATE NOCASE LIMIT 1", (identity,)).fetchone() is not None


def new_task_id(connection) -> str:
    """A new Task id that no Task uses yet, drawn with ``generate``."""
    for _ in range(TASK_ID_ATTEMPTS):
        identity = generate()
        if not taken(connection, identity):
            return identity
    raise DomainError("operation_unavailable", "No free Task id was found; try again", retryable=True)


def mentioned(connection, text) -> bool:
    """Whether ``text`` quotes the id of an existing Task (see ``MENTION``). A UUID is found by its form instead
    (``owner_text``); a slug id reads as words and is not looked for."""
    tokens = sorted({token.lower() for token in MENTION.findall(text)})[:MENTIONS_CHECKED]
    if not tokens:
        return False
    marks = ",".join("?" * len(tokens))
    return connection.execute(f"SELECT 1 FROM tasks WHERE lower(id) IN ({marks}) LIMIT 1", tokens).fetchone() is not None


def in_use() -> DomainError:
    """The refusal of a caller-supplied id that another Task already has. It names no scope: the other Task may be in a
    project the caller cannot read."""
    return DomainError("validation_error", "This Task id is already in use; leave id out to get a new one",
                       field_errors={"id": "already in use"})


__all__ = ["MENTION", "TASK_ID_ALPHABET", "TASK_ID_ATTEMPTS", "TASK_ID_LENGTH", "generate", "in_use", "mentioned", "new_task_id",
           "taken"]
