"""Making the tables of an extension's own in the store, at start, before the store is opened.

``make`` runs inside ``build_runtime`` (``make_tables=True``) between loading the code parts and opening the store. For
each extension whose declared tables add to what the store records (``extension_registry.declared_tables``), one
transaction (``agentic_migration.tables_transition`` through ``_apply_transition``) runs the new statements, writes the
record row, reads the record back and compares its digest, under the instance lifetime lock. Each extension is its own
transaction, so a refusal of one leaves the others and the store as they were; the refused one needs attention.

Two choices that wait for the owner each live in one function here:

- ``due``: which loaded extensions get their tables at this start. Today every loaded one whose declaration adds.
- ``copy_for``: whether a full copy of the store is made first, and which copies stay. Today a copy every time, each
  attempt under a name of its own, and every copy stays.
"""
from __future__ import annotations

import logging
from pathlib import Path
import uuid

from qorqut_operations.contracts import DomainError

LOGGER = logging.getLogger(__name__)
BACKUPS = "extension-backups"
AT_START = "tables"


def due(loaded) -> dict:
    """``{id: row}`` of the loaded code parts whose tables are made at this start: each one whose declaration adds to
    the store's record (``Loaded.tables``)."""
    return {ext_id: item.tables for ext_id, item in sorted(loaded.items()) if item.tables is not None}


def copy_for(paths, attempt) -> Path:
    """Where this attempt's full copy of the store is written before its tables are made: a new name in
    ``extension-backups/<attempt>/`` (``attempt`` is the change id of a staged change, or ``tables`` at an ordinary
    start). A copy is made every time, and every copy stays; an attempt that was interrupted after its copy leaves it,
    and the next one makes its own."""
    folder = paths.instance_root / BACKUPS / attempt
    for part in (folder.parent, folder):
        part.mkdir(mode=0o700, exist_ok=True)
    return folder / f"{uuid.uuid4().hex}.sqlite3"


def make_one(paths, selection, ext_id, row, *, attempt=AT_START, ownership=None):
    """Make the tables of one row in the store, holding the instance (taken here, or the caller's). Returns the
    transition's report; raises ``DomainError`` when they cannot be made, and the store is then as it was."""
    from qorqut.bootstrap import agentic_migration as storage
    from qorqut.composition.extension_tables import recorded
    transition, inside, readback = storage.tables_transition(selection, recorded(paths.database), (row,))
    phases = {}
    report = storage.apply_tables(paths, transition, inside=inside, readback=readback,
                                  backup=copy_for(paths, attempt), ownership=ownership, phases=phases)
    LOGGER.warning("extension_tables_made extension=%s version=%s applied=%s %s", ext_id, row["version"],
                   "yes" if report["applied"] else "no",
                   " ".join(f"{name}_seconds={seconds}" for name, seconds in phases.items()))
    return report


def make(paths, selection, loaded) -> dict:
    """Make the tables of every due extension (``due``). Answers ``{id: finding}`` for each one whose tables could not
    be made; the caller refuses those. With nothing due nothing is read, locked or written."""
    from qorqut.extension_registry import TABLES_REFUSED
    refused = {}
    for ext_id, row in due(loaded).items():
        try:
            make_one(paths, selection, ext_id, row)
        except DomainError as exc:
            LOGGER.warning("extension_tables_not_made extension=%s code=%s", ext_id, exc.code)
            refused[ext_id] = TABLES_REFUSED.format(exc.message)
    return refused
