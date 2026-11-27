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
- ``copy_for``: whether a full copy of the store is made first, and which copies stay. Today one copy per attempt (a
  start, or a staged change), each under a name of its own, and every copy stays.

Each transaction first runs its statements in a transaction that is rolled back, so statements that do not fit the rows
already in the store cost no copy.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
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


def copy_for(paths, attempt):
    """The copies of the store this attempt makes before its tables are made (``attempt`` is the change id of a staged
    change, or ``tables`` at an ordinary start), as the callable ``_apply_transition`` asks when a copy is due: it
    answers the path of a new copy, or None when none is to be made.

    Today one copy per attempt, written by the first transaction that reaches its copy, under a new name in
    ``extension-backups/<attempt>/``; the later transactions of the same attempt make none. Every copy stays: an attempt
    interrupted after its copy leaves it, and the next one makes its own."""
    made = []

    def wanted():
        if made and made[-1].exists():
            return None
        folder = paths.instance_root / BACKUPS / attempt
        for part in (folder.parent, folder):
            part.mkdir(mode=0o700, exist_ok=True)
        made.append(folder / f"{uuid.uuid4().hex}.sqlite3")
        return made[-1]
    return wanted


NOT_COPIED = "the copy of the database that is made first could not be written"
NOT_FIT_ROWS = "this version's changes to its tables do not fit the data already in them"
STATEMENT_FAILED = "a change to its tables failed in the database"


def note_of(exc) -> str:
    """The sentence a person reads when tables could not be made, from the kind of the failure: never the text of an
    inner error. ``exc`` is the ``DomainError`` or ``OSError`` raised while making them."""
    from qorqut.extension_registry import TABLES_REFUSED
    if isinstance(exc, OSError):
        return TABLES_REFUSED.format(NOT_COPIED)
    known = {"tables_do_not_fit_rows": NOT_FIT_ROWS, "tables_statement_failed": STATEMENT_FAILED}
    return TABLES_REFUSED.format(known[exc.code]) if exc.code in known else TABLES_REFUSED.format(exc.message)


def _not_made(ext_id, exc):
    """The log line of tables that could not be made: the kind and the type of the fault, never its text."""
    LOGGER.warning("extension_tables_not_made extension=%s code=%s error=%s", ext_id,
                   getattr(exc, "code", None) or "os_error", type(exc).__name__)


def make_one(paths, selection, ext_id, row, *, attempt=AT_START, copy=None, ownership=None):
    """Make the tables of one row in the store, holding the instance (taken here, or the caller's). ``copy`` is the
    attempt's ``copy_for``; without one this call is an attempt of its own. Returns the transition's report; raises
    ``DomainError`` (or ``OSError`` when the copy cannot be written) when they cannot be made, and the store is then as it
    was."""
    from qorqut.bootstrap import agentic_migration as storage
    from qorqut.composition.extension_tables import recorded
    transition, inside, readback = storage.tables_transition(selection, recorded(paths.database), (row,))
    phases = {}
    report = storage.apply_tables(paths, transition, inside=inside, readback=readback,
                                  backup=copy or copy_for(paths, attempt), ownership=ownership, phases=phases)
    LOGGER.warning("extension_tables_made extension=%s version=%s applied=%s %s", ext_id, row["version"],
                   "yes" if report["applied"] else "no",
                   " ".join(f"{name}_seconds={seconds}" for name, seconds in phases.items()))
    return report


def make(paths, selection, loaded) -> dict:
    """Make the tables of every due extension (``due``), one transaction each, the attempt's copy made once. Answers
    ``{id: note}`` for each one whose tables could not be made; the caller refuses those. With nothing due nothing is
    read, locked or written."""
    refused, copy = {}, copy_for(paths, AT_START)
    for ext_id, row in due(loaded).items():
        try:
            make_one(paths, selection, ext_id, row, copy=copy)
        except (DomainError, OSError) as exc:
            _not_made(ext_id, exc)
            refused[ext_id] = note_of(exc)
    return refused


# -- a staged change --------------------------------------------------------------------------------------------------
_CHILD = ("import json,sys; sys.path[:0]=json.loads(sys.argv[1]); "
          "from qorqut.bootstrap.extension_tables import _declare; _declare(sys.argv[2], sys.argv[3], sys.argv[4])")
# The kinds of fault the child may answer besides those of ``extension_registry.LOAD_FAULTS``.
TABLES_FAULT, TABLES_FORM, MANIFEST = "tables_fault", "tables_form", "manifest"
NO_ANSWER = "the process that loads it ended without an answer"
NO_INTERPRETER = "Qorqbot knows no Python to load it with"
TABLES_FORM_NOTE = "its tables are declared in a form Qorqbot cannot record"
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")


def _declare(folder, others, channel) -> None:
    """In the child process: load the code part of ``folder`` as a start loads it, with ``others`` (the code folders
    of the other installed extensions, as a start has them) on the import path, and write the answer to the file
    descriptor ``channel``: ``{"row": ...}`` (the canonical row of its declared tables, or None) or ``{"fault": kind,
    "detail": ...}``. Only data is written, never a sentence or the text of an exception. The process then ends at
    once, so neither a thread nor an exit handler of the loaded code keeps it alive or writes after the answer."""
    channel = int(channel)
    os.set_inheritable(channel, False)
    answer = {"fault": "fault", "detail": None}
    try:
        from qorqut import extension_contract as contract
        from qorqut.extension_registry import CODE_FOLDER
        # The path is the calling process's without the code folder of any extension, then what a start has.
        sys.path[:] = [entry for entry in sys.path
                       if not (Path(entry).name == CODE_FOLDER and (Path(entry).parent / contract.MANIFEST).is_file())]
        sys.path.extend(json.loads(others))
        answer = _answer(Path(folder).resolve())
    except BaseException as exc:  # noqa: BLE001 - whatever the loaded code raised, only its type name is answered
        answer = {"fault": "fault", "detail": type(exc).__name__}
    finally:
        try:
            for stream in (sys.stdout, sys.stderr):
                stream.flush()
        except Exception:  # noqa: BLE001 - the answer is written whatever the streams do
            pass
        data = json.dumps(answer).encode("utf-8")
        while data:
            data = data[os.write(channel, data):]
        os.close(channel)
        os._exit(0)


def _answer(folder) -> dict:
    from qorqut import extension_contract as contract
    from qorqut.composition.extension_tables import row_of
    from qorqut.extension_registry import Found, _loaded
    try:
        manifest = contract.load_manifest(folder)
    except contract.ContractError:
        return {"fault": MANIFEST, "detail": None}
    made, problem = _loaded(Found(manifest.id, folder, manifest.delivery, manifest))
    if made is None:
        return {"fault": problem[0], "detail": problem[1]}
    try:
        declared = tuple(made.extension.tables())
    except Exception as exc:  # noqa: BLE001 - only the type name is answered
        return {"fault": TABLES_FAULT, "detail": type(exc).__name__}
    if not declared:
        return {"row": None}
    try:
        return {"row": row_of(manifest.id, manifest.version, declared)}
    except DomainError:
        return {"fault": TABLES_FORM, "detail": None}


def _code_folders(paths, ext_id) -> list[str]:
    """The ``code/`` folders a start has on its import path besides the one of ``ext_id``: each other extension folder
    with a code part, in the order a start finds them."""
    from qorqut import extension_contract as contract
    from qorqut.extension_registry import CODE_FOLDER, extension_roots
    return [str(folder / CODE_FOLDER) for root, _ in extension_roots(paths) if root.is_dir()
            for folder in sorted(root.iterdir())
            if folder.name != ext_id and (folder / contract.MANIFEST).is_file() and (folder / CODE_FOLDER).is_dir()]


def _sentence(answer, manifest) -> tuple[str | None, str | None]:
    """``(kind, finding)`` of a fault answer, written here from the kind alone; ``(None, None)`` for an answer that is
    not one the child writes."""
    from qorqut.extensions import Extension
    from qorqut.extension_registry import LOAD_FAULTS, NOT_LOADED, TABLES_REFUSED, load_finding
    kind, detail = answer.get("fault"), answer.get("detail")
    if set(answer) != {"fault", "detail"} or not isinstance(kind, str):
        return None, None
    if kind in ("fault", TABLES_FAULT):
        named = detail if isinstance(detail, str) and _TYPE_NAME.fullmatch(detail) else "an error"
        return kind, NOT_LOADED.format(named)
    if kind == "parts":
        known = {name for name in vars(Extension) if not name.startswith("_")}
        if not (isinstance(detail, list) and detail and all(isinstance(name, str) and name in known for name in detail)):
            return None, None
        return kind, load_finding(kind, manifest.id, sorted(detail))
    if kind == "other_module":
        return kind, load_finding(kind, manifest.id, manifest.factory.partition(":")[0])
    if kind in LOAD_FAULTS and detail is None:
        return kind, load_finding(kind, manifest.id)
    if kind == TABLES_FORM and detail is None:
        return kind, TABLES_REFUSED.format(TABLES_FORM_NOTE)
    if kind == MANIFEST and detail is None:
        return kind, NOT_LOADED.format("its manifest could not be read")
    return None, None


def declared(folder, *, others=()) -> tuple[dict | None, str | None]:
    """``(row or None, finding or None)`` for the code part of ``folder``, loaded in a process of its own with this
    process's import path and ``others`` (``_code_folders``): the canonical row of the tables it declares (None when it
    declares none), or why its code could not be loaded. The child answers on a pipe of its own and only with data;
    every sentence is written here, and a failure is logged with the kind of fault and a type name only."""
    from qorqut import extension_contract as contract
    from qorqut.extension_registry import NOT_LOADED
    manifest = contract.load_manifest(folder)

    def failed(kind, finding, detail=""):
        LOGGER.warning("extension_code_not_loaded extension=%s kind=%s error=%s", manifest.id, kind, detail)
        return None, finding
    if not sys.executable:
        return failed("no_interpreter", NOT_LOADED.format(NO_INTERPRETER))
    reading, writing = os.pipe()
    try:
        try:
            child = subprocess.Popen(
                [sys.executable, "-I", "-B", "-c", _CHILD, json.dumps(sys.path), str(folder), json.dumps(list(others)),
                 str(writing)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(writing,))
        finally:
            os.close(writing)
        with os.fdopen(reading, "rb") as stream:
            reading = None
            received = stream.read()
        child.wait()
        answer = json.loads(received) if child.returncode == 0 and received else None
    except (OSError, ValueError, subprocess.SubprocessError):
        answer = None
    finally:
        if reading is not None:
            os.close(reading)
    if isinstance(answer, dict) and set(answer) == {"row"} and (answer["row"] is None or isinstance(answer["row"], dict)):
        return answer["row"], None
    kind, finding = _sentence(answer, manifest) if isinstance(answer, dict) else (None, None)
    if finding is None:
        return failed("no_answer", NOT_LOADED.format(NO_ANSWER))
    detail = answer["detail"] if kind in ("fault", TABLES_FAULT) and isinstance(answer["detail"], str) \
        and _TYPE_NAME.fullmatch(answer["detail"]) else ""
    return failed(kind, finding, detail)


KEPT = "The tables it has in the database stay."


def make_staged(paths, selection, change_id, folder, ext_id, version, *, ownership=None) -> str | None:
    """Make the tables a staged folder declares, before it is moved in: None when there is nothing to make or they were
    made, else the note the change fails with. Before a commit the database is then as it was. When the database
    already holds tables of the extension (this change made them before it failed, or an earlier version or install
    did), the note says they stay. ``selection`` is called for today's selection only when tables are made; a selection
    that cannot be read is not a fault of the change, so it stops the start and the change stays pending. The copy goes
    under the change id (``copy_for``)."""
    from qorqut import extension_contract as contract
    from qorqut.composition.extension_tables import ADDS, contribution_of, judge, owner_of, recorded
    if contract.load_manifest(folder).factory is None:
        return None

    def failed(note):
        held = any(getattr(item, "owner", None) == owner_of(ext_id) for item in recorded(paths.database))
        return f"{note} {KEPT}" if held else note
    row, finding = declared(folder, others=_code_folders(paths, ext_id))
    if finding is not None:
        return failed(finding)
    if row is None:
        return None
    try:
        if row.get("id") != ext_id:
            raise DomainError("invalid_installation", f"its code declares tables for {row.get('id')!r}")
        verdict, row = judge(ext_id, version, (contribution_of(row),), recorded(paths.database))
    except DomainError as exc:
        _not_made(ext_id, exc)
        return failed(note_of(exc))
    if verdict != ADDS:
        return None
    chosen = selection()
    try:
        make_one(paths, chosen, ext_id, row, attempt=change_id, ownership=ownership)
    except (DomainError, OSError) as exc:
        _not_made(ext_id, exc)
        return failed(note_of(exc))
    return None
