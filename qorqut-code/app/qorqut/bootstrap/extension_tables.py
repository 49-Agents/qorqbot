"""Making the tables of an extension's own in the store, at start, before the store is opened.

``make`` runs inside ``build_runtime`` (``make_tables=True``) between loading the code parts and opening the store. For
each extension whose declared tables add to what the store records (``extension_registry.declared_tables``), one
transaction (``agentic_migration.tables_transition`` through ``_apply_transition``) runs the new statements, writes the
record row, reads the record back and compares its digest, under the instance lifetime lock. Each extension is its own
transaction, so a refusal of one leaves the others and the store as they were; the refused one needs attention.

A staged install or update (``extension_change``) makes its tables before its folder is moved in (``make_staged``). Its
code is loaded in a process of its own (``declared``), so nothing of the staged folder is imported into the service
process; the canonical row it prints is judged against the store's record here.

Two choices that wait for the owner each live in one function here:

- ``due``: which loaded extensions get their tables at this start. Today every loaded one whose declaration adds.
- ``copy_for``: whether a full copy of the store is made first, and which copies stay. Today a copy every time, each
  attempt under a name of its own, and every copy stays.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import json
import logging
from pathlib import Path
import subprocess
import sys
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


# -- a staged change --------------------------------------------------------------------------------------------------
_CHILD = ("import json,sys; sys.path[:0]=json.loads(sys.argv[1]); "
          "from qorqut.bootstrap.extension_tables import _declare; _declare(sys.argv[2])")
NO_ANSWER = "the process that loads it ended without an answer"


def _declare(folder) -> None:
    """In the child process: load the code part of ``folder`` as a start loads it and print ``{"row": ...}`` (the
    canonical row of its declared tables, or None when it declares none) or ``{"finding": ...}``. What the code
    writes to standard output while it loads goes to standard error, so the one line printed is the answer."""
    from qorqut import extension_contract as contract
    from qorqut.extension_registry import CODE_FOLDER, Found, NOT_LOADED, _load, declared_tables
    folder = Path(folder).resolve()
    # The path is the service's: the code folder of an extension loaded there (an older version of this one) is left
    # out, so only the staged folder's code can be imported.
    sys.path[:] = [entry for entry in sys.path
                   if not (Path(entry).name == CODE_FOLDER and (Path(entry).parent / contract.MANIFEST).is_file())]
    with redirect_stdout(sys.stderr):
        try:
            manifest = contract.load_manifest(folder)
        except contract.ContractError as exc:
            answer = {"finding": NOT_LOADED.format(type(exc).__name__)}
        else:
            item = Found(manifest.id, folder, manifest.delivery, manifest)
            made, finding = _load(item)
            if made is not None:
                row, finding = declared_tables(item, made.extension, ())
            answer = {"finding": finding} if finding is not None else {"row": row}
    print(json.dumps(answer))


def declared(folder) -> tuple[dict | None, str | None]:
    """``(row or None, finding or None)`` for the code part of ``folder``, loaded in a process of its own with this
    process's import path: the canonical row of the tables it declares (None when it declares none), or why its code
    could not be loaded."""
    from qorqut.extension_registry import NOT_LOADED
    try:
        result = subprocess.run([sys.executable, "-I", "-B", "-c", _CHILD, json.dumps(sys.path), str(folder)],
                                capture_output=True, text=True, check=False)
        answer = json.loads(result.stdout.splitlines()[-1]) if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, ValueError):
        answer = None
    if isinstance(answer, dict) and set(answer) == {"finding"} and isinstance(answer["finding"], str):
        return None, answer["finding"]
    if isinstance(answer, dict) and set(answer) == {"row"} and (answer["row"] is None or isinstance(answer["row"], dict)):
        return answer["row"], None
    return None, NOT_LOADED.format(NO_ANSWER)


def make_staged(paths, selection, change_id, folder, ext_id, version, *, ownership=None) -> str | None:
    """Make the tables a staged folder declares, before it is moved in: None when there is nothing to make or they were
    made, else the note the change fails with (the store is then as it was). ``selection`` is called for today's
    selection only when tables are made. The copy goes under the change id (``copy_for``)."""
    from qorqut import extension_contract as contract
    from qorqut.composition.extension_tables import ADDS, contribution_of, judge, recorded
    from qorqut.extension_registry import TABLES_REFUSED
    if contract.load_manifest(folder).factory is None:
        return None
    row, finding = declared(folder)
    if finding is not None:
        return finding
    if row is None:
        return None
    try:
        if row.get("id") != ext_id:
            raise DomainError("invalid_installation", f"its code declares tables for {row.get('id')!r}")
        verdict, row = judge(ext_id, version, (contribution_of(row),), recorded(paths.database))
        if verdict == ADDS:
            make_one(paths, selection(), ext_id, row, attempt=change_id, ownership=ownership)
    except DomainError as exc:
        LOGGER.warning("extension_tables_not_made extension=%s code=%s", ext_id, exc.code)
        return TABLES_REFUSED.format(exc.message)
    return None
