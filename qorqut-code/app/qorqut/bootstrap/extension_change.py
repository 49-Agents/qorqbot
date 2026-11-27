"""The one pending change of a downloadable extension: prepared while the service runs, applied at the next start.

An extension is installed when its folder is in ``<instance>/extensions/<id>/``; the registry finds it at start and
``sync`` records it and copies its plugin part. So a change made while the service is stopped is only a folder move:

- ``stage`` (the job behind an install or update): download the bundle of an index entry, check its checksum and the
  bundle, unpack it into ``extensions/.staging/<sha256>/``, write ``config/extension-change.json`` and ask for the
  restart. A failure is returned as one sentence and changes nothing.
- ``stage_removal``: write the pending removal and ask for the restart. With ``delete_data`` the extension's private
  folder ``private/extensions/<id>/`` is deleted too; what its records created in the store belongs to the owner and stays.
- ``apply_pending`` runs at start before the selection is read. It makes the tables the staged folder declares
  (``extension_tables.make_staged``, a copy of the store first under ``extension-backups/<change id>/``), moves the
  folder that is replaced or removed to ``extension-backups/<change id>/`` and moves the staged folder in, then writes
  the result into the change file. Tables that cannot be made end the change as ``failed``. Each
  step is a rename that the next step can see, so a start that was interrupted finishes the change at the next one.
- ``finish`` runs after ``sync``: it reports the result to the one who asked (``report``, or the reporter the service
  gives) and deletes the change file.

Everything read from the change file is checked before a path is built from it, so a start moves or deletes nothing
outside ``<instance>/extensions/``, its staging folder, ``extension-backups/`` and, for a removal that deletes data,
``private/extensions/<id>/``. A change file that cannot be read as a
valid change is set aside next to itself and the start goes on. A change that ends as ``failed`` first moves back what
it backed up, so the installation is as it was before the change.

This is a path of its own beside ``extension_install``: that coordinator changes the startup selection, the schema and
the agentic layer of today's extensions under its journal, none of which a folder move needs. Its private journal
writing (``_read``, ``_save``) is reused.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import tarfile
import threading
import urllib.request
import uuid

from qorqut_operations.contracts import DomainError

from qorqut.bootstrap import extension_bundle as bundles
from qorqut.bootstrap.extension_install import _read, _save
from qorqut.extensions import EXTENSION_ID
from qorqut.private_files import require_private_directory

LOGGER = logging.getLogger(__name__)
# One change is staged at a time in this process: the check that none waits and the staging are one step, whether the
# owner's click or the job of an approved Signoff asks. The one that comes second finds the change waiting.
_STAGING = threading.Lock()
CHANGE = "extension-change.json"
STAGING = ".staging"
BACKUPS = "extension-backups"
INSTALL, UPDATE, REMOVE = "install", "update", "remove"
ACTIONS = (INSTALL, UPDATE, REMOVE)
DONE, FAILED = "done", "failed"
CHANGE_KEYS = frozenset({"format", "change_id", "action", "id", "version", "sha256", "staged", "requested_by",
                         "signoff_id", "delete_data", "result"})
CHANGE_ID = re.compile(r"^[0-9a-f]{32}$")  # uuid4().hex, as ``_new_change`` makes it
WAITING = "Another extension change is waiting for Qorqbot to restart."
NOT_DELETED = "Its private data was not deleted, because its private folder is not a plain folder."
PARTLY_DELETED = "Its private data could not be deleted completely."
_fault_hook = lambda _phase: None  # tests interrupt a start after a phase


@dataclass(frozen=True)
class Outcome:
    """What ``stage`` or ``stage_removal`` did. ``staged`` is False when nothing changed, and ``note`` says why; when the
    change is staged but no restart could be asked for, ``note`` says it is applied at the next start."""
    staged: bool
    note: str | None = None
    kind: str | None = field(default=None, compare=False)  # when not staged: the kind of reason, for ``not_done``


class _Refused(ValueError):
    """A bundle that cannot be taken, with the kind of reason for the log line."""
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def change_path(paths) -> Path:
    return paths.instance_root / "config" / CHANGE


def staging(paths) -> Path:
    return paths.installed_extensions / STAGING


def _staged_folder(paths, sha256, ext_id) -> Path:
    return staging(paths) / sha256 / ext_id


def _problem(paths, change) -> str | None:
    """Why ``change`` is not a valid change file of this instance, or None."""
    if not isinstance(change, dict) or set(change) != CHANGE_KEYS or type(change["format"]) is not int \
            or change["format"] != 1:
        return "it is not a change file of this version"
    if change["action"] not in ACTIONS:
        return "its action is unknown"
    if not isinstance(change["id"], str) or not EXTENSION_ID.fullmatch(change["id"]):
        return "its id is not an extension id"
    if not isinstance(change["change_id"], str) or not CHANGE_ID.fullmatch(change["change_id"]):
        return "its change id is not one Qorqbot makes"
    if any(change[key] is not None and not isinstance(change[key], str) for key in ("requested_by", "signoff_id")):
        return "it names who asked in a form Qorqbot does not write"
    if type(change["delete_data"]) is not bool:
        return "its delete_data is not true or false"
    result = change["result"]
    if result is not None and not (isinstance(result, dict) and set(result) == {"state", "note"}
                                   and result["state"] in (DONE, FAILED)
                                   and (result["note"] is None or isinstance(result["note"], str))):
        return "its result is not one Qorqbot writes"
    if change["action"] != REMOVE and change["delete_data"]:
        return "an install or update says to delete data"
    if change["action"] == REMOVE:
        if (change["version"], change["sha256"], change["staged"]) != (None, None, None):
            return "a removal names a version, a bundle or a staged folder"
        return None
    if not isinstance(change["sha256"], str) or not bundles.SHA256.fullmatch(change["sha256"]):
        return "its bundle checksum is not a sha256"
    if not isinstance(change["version"], str):
        return "it has no version"
    expected = _staged_folder(paths, change["sha256"], change["id"])
    if not isinstance(change["staged"], str) or Path(change["staged"]).resolve() != expected.resolve():
        return "its staged folder is not the one in the staging folder of this instance"
    return None


def _set_aside(path, reason):
    aside = path.with_name(f"{path.name}.refused-{uuid.uuid4().hex}")
    os.rename(path, aside)
    LOGGER.warning("extension_change_refused reason=%s set_aside=%s", reason, aside.name)


def pending(paths) -> dict | None:
    """The pending change, or None. A file that is not a valid change is set aside, so it never blocks a start."""
    path = change_path(paths)
    if not os.path.lexists(path):
        return None
    try:
        change = _read(path)
    except (OSError, ValueError, DomainError):
        _set_aside(path, "it cannot be read as JSON")
        return None
    problem = _problem(paths, change)
    if problem:
        _set_aside(path, problem)
        return None
    return change


def _new_change(action, ext_id, version, sha256, staged, requested_by, signoff_id, delete_data=False):
    return {"format": 1, "change_id": uuid.uuid4().hex, "action": action, "id": ext_id, "version": version,
            "sha256": sha256, "staged": staged, "requested_by": requested_by, "signoff_id": signoff_id,
            "delete_data": delete_data, "result": None}


def _default_restart(reason):
    from qorqut.service.restart import request_restart
    return request_restart(reason)


def _write(paths, change, restart, reason, undo) -> Outcome:
    """Write the change file and ask for the restart. When either fails, the file and ``undo`` are removed again."""
    path = change_path(paths)
    try:
        config = require_private_directory(paths.instance_root / "config", create=True)
        _save(config / CHANGE, change)
        asked = (restart or _default_restart)(reason)
    except Exception as exc:  # noqa: BLE001 - whatever failed, nothing is left staged
        if os.path.lexists(path):
            path.unlink()
        undo()
        return Outcome(False, f"The change could not be written or the restart could not be asked for: {exc}",
                       f"not_written:{exc.__class__.__name__}")
    if not asked:
        return Outcome(True, "Qorqbot could not be asked to restart, so the change is applied at its next start.")
    return Outcome(True)


def _download(url, target, size):
    """Write the bundle at ``url`` to ``target``; refuse more bytes than the index gives."""
    try:
        with urllib.request.urlopen(url, timeout=60) as response, target.open("xb") as output:
            written = 0
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > size:
                    raise _Refused("too_large", f"The bundle is larger than the {size} bytes the index gives.")
                output.write(chunk)
    except OSError as exc:
        raise _Refused("not_downloaded", f"The bundle could not be downloaded or unpacked: {exc}") from exc


def stage(paths, entry, bundle, *, requested_by=None, signoff_id=None, restart=None) -> Outcome:
    """Download, check and unpack the bundle of an index entry, write the pending install or update and ask for the
    restart. Either all of it is done or nothing is left; the ``Outcome`` says which."""
    with _STAGING:
        return _stage(paths, entry, bundle, requested_by, signoff_id, restart)


def _stage(paths, entry, bundle, requested_by, signoff_id, restart) -> Outcome:
    if pending(paths) is not None:
        return Outcome(False, WAITING, "waiting")
    if not EXTENSION_ID.fullmatch(entry.id) or not bundles.SHA256.fullmatch(bundle.sha256):
        return Outcome(False, "The index entry does not name an extension and a bundle checksum.", "invalid_entry")
    area = staging(paths)
    archive, unpacked = area / f"{bundle.sha256}.tar.gz", area / bundle.sha256

    def undo():
        archive.unlink(missing_ok=True)
        shutil.rmtree(unpacked, ignore_errors=True)
    try:
        undo()  # what an earlier attempt that failed left
        area.mkdir(mode=0o700, parents=True, exist_ok=True)
        _download(bundle.url, archive, bundle.size)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != bundle.sha256:
                raise _Refused("checksum", "The downloaded bundle does not have the checksum the index gives.")
        found = bundles.check_bundle(archive)
        if found:
            raise _Refused("bundle_check", "The bundle does not pass its check: " + " ".join(found))
        header = bundles.unpack(archive, unpacked)
        if (header["id"], header["version"]) != (entry.id, entry.version):
            raise _Refused("other_version",
                           f"The bundle holds {header['id']} {header['version']}, not {entry.id} {entry.version}.")
        if header["platform"] != bundle.platform:
            raise _Refused("other_platform", f"The bundle is for {header['platform']}, not {bundle.platform}.")
        archive.unlink()
    except (OSError, ValueError, tarfile.TarError) as exc:
        undo()
        return Outcome(False, str(exc) if isinstance(exc, ValueError)
                       else f"The bundle could not be downloaded or unpacked: {exc}",
                       getattr(exc, "kind", None) or ("invalid_bundle" if isinstance(exc, ValueError) else "not_unpacked"))
    action = UPDATE if (paths.installed_extensions / entry.id).is_dir() else INSTALL
    change = _new_change(action, entry.id, entry.version, bundle.sha256,
                         str(_staged_folder(paths, bundle.sha256, entry.id)), requested_by, signoff_id)
    return _write(paths, change, restart, f"{action} extension {entry.id} {entry.version}", undo)


def stage_removal(paths, ext_id, *, requested_by=None, signoff_id=None, delete_data=False, restart=None) -> Outcome:
    """Write the pending removal of a downloaded extension and ask for the restart. ``delete_data`` deletes its private
    folder as well."""
    with _STAGING:
        return _stage_removal(paths, ext_id, requested_by, signoff_id, delete_data, restart)


def _stage_removal(paths, ext_id, requested_by, signoff_id, delete_data, restart) -> Outcome:
    if pending(paths) is not None:
        return Outcome(False, WAITING, "waiting")
    if not isinstance(ext_id, str) or not EXTENSION_ID.fullmatch(ext_id) \
            or not (paths.installed_extensions / ext_id).is_dir():
        return Outcome(False, f"No downloaded extension {ext_id!r} is installed.", "not_installed")
    if type(delete_data) is not bool:
        return Outcome(False, "Whether to delete the extension's data is not true or false.", "invalid_request")
    change = _new_change(REMOVE, ext_id, None, None, None, requested_by, signoff_id, delete_data)
    return _write(paths, change, restart, f"remove extension {ext_id}{' and delete its data' if delete_data else ''}",
                  lambda: None)


def _apply(paths, change, selection, owner):
    """The folder moves of one checked change; each can be done again after an interruption. Returns the result."""
    from qorqut.bootstrap.extension_tables import make_staged
    from qorqut.extension_contract import check_folder

    target = paths.installed_extensions / change["id"]
    backup = paths.instance_root / BACKUPS / change["change_id"] / change["id"]

    def failed(note):
        # What was backed up goes back, so a failed change leaves the installation as it was.
        if backup.is_dir() and not os.path.lexists(target):
            os.rename(backup, target)
        return {"state": FAILED, "note": note}

    if change["action"] == REMOVE:
        if target.is_dir():
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.rename(target, backup)
        _fault_hook("moved")
        return {"state": DONE, "note": _delete_data(paths, change["id"]) if change["delete_data"] else None}
    staged = _staged_folder(paths, change["sha256"], change["id"])
    if staged.is_dir():
        findings = check_folder(staged)
        if findings:
            shutil.rmtree(staged.parent)
            return failed("The staged folder breaks the contract: " + findings[0])
        # Its tables are made before the folder is in; done again after an interruption, it finds them made.
        note = make_staged(paths, selection, change["change_id"], staged, change["id"], change["version"],
                           ownership=owner)
        if note is not None:
            shutil.rmtree(staged.parent)
            return failed(note)
        if target.is_dir():
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.rename(target, backup)
        _fault_hook("backed-up")
        os.rename(staged, target)
        _fault_hook("moved")
    elif not target.is_dir() or not _moved_in(target, change):
        # The staged folder is gone and was not moved in by an earlier start.
        return failed("The staged folder is gone, so nothing was changed.")
    if staged.parent.is_dir():
        shutil.rmtree(staged.parent)
    return {"state": DONE, "note": None}


def _delete_data(paths, ext_id):
    """Delete the private folder of ``ext_id``; None, or the sentence saying the data was not deleted or not all of it.
    Only a plain folder at its own place is deleted, never what a link points to. A deletion that fails partway does not
    stop the start: the removal is done and the result says so."""
    from qorqut.extension_private import PrivateFolder
    folder = PrivateFolder(paths.instance_root, ext_id).path
    if not os.path.lexists(folder):
        return None
    if any(part.is_symlink() for part in (folder.parent.parent, folder.parent, folder)) or not folder.is_dir():
        return NOT_DELETED
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        LOGGER.warning("extension_data_not_deleted extension=%s error=%s", ext_id, exc.__class__.__name__)
        return PARTLY_DELETED
    return None


def _moved_in(target, change):
    """Whether the folder at ``target`` is the staged one already moved in: its manifest has the change's version."""
    from qorqut.extension_contract import ContractError, load_manifest
    try:
        return load_manifest(target).version == change["version"]
    except ContractError:
        return False


def apply_pending(paths, selection=None) -> None:
    """Apply the pending extension change of the instance at ``paths``, holding the instance, before the selection is
    read. A change already applied, whose result waits for ``finish``, is left as it is; a file that is not a valid
    change is set aside and the start goes on.

    ``selection`` answers today's extension selection; it is called only when a staged folder's tables are made, and
    without it the installation's own selection is read then."""
    change = pending(paths)
    if change is None or change["result"] is not None:
        return
    from qorqut.service.ownership import InstanceOwnership
    if selection is None:
        from qorqut.extensions import installed_selection as selection

    owner = InstanceOwnership(paths).acquire()
    try:
        change["result"] = _apply(paths, change, selection, owner)
        _fault_hook("applied")
        _save(change_path(paths), change)
        LOGGER.info("extension_change_applied action=%s extension=%s state=%s", change["action"], change["id"],
                    change["result"]["state"])
    finally:
        owner.release()


def _what(action, delete_data):
    """The action as the log lines write it: a removal says whether it deletes the private data."""
    return f"{action} delete_data={'yes' if delete_data else 'no'}" if action == REMOVE else action


def report(change) -> None:
    """Write how the change ended to the log. The service gives ``finish`` the reporter that also tells the agent who
    asked (``domains/extension_tools.py``)."""
    # A warning, as the service's log holds warnings and above only: the one line a change leaves at the start after it.
    LOGGER.warning("extension_change_finished action=%s extension=%s state=%s requested_by=%s",
                   _what(change["action"], change.get("delete_data")), change["id"], change["result"]["state"],
                   change.get("requested_by") or "")


def not_done(action, ext_id, requested_by, kind, delete_data=False) -> None:
    """The one line a change refused or failed before the restart leaves: what, who asked, and the kind of reason (a
    word, never a typed value or the text of a file)."""
    LOGGER.warning("extension_change_not_done action=%s extension=%s requested_by=%s reason=%s",
                   _what(action, delete_data), ext_id, requested_by or "", kind or "unknown")


def finish(paths, report=report) -> dict | None:
    """After ``sync``: report an applied change and delete its file; returns it, or None when there was none."""
    change = pending(paths)
    if change is None or change["result"] is None:
        return None
    report(change)
    change_path(paths).unlink()
    return change
