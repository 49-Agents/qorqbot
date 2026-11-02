"""Durable creation of an empty directory tree inside the prepared file-effect family.

One ``create_directory_tree`` effect is applied by the same ``FileApplier`` that applies file writes, under the same
receipt, claims, writer admission and recovery. It adds no executor, store or marker file. For one registered target:

1. ``intent``: the receipt-bound staging name is recorded before any filesystem effect.
2. The empty tree is built in the private ``staging_root`` (outside the authored tree, on the destination's filesystem),
   fsynced, and its exact inode identity and metadata are recorded in the receipt progress (``staged``). The database
   commit is the durable record (``synchronous = FULL``).
3. The staging directory is published to the final path with an atomic no-replace rename (Linux ``renameat2`` or Darwin ``renameatx_np``): a preexisting path,
   including an identical empty tree, can never be replaced or adopted. Both parent descriptors are held, so no path
   is re-resolved between the checks and the rename.
4. The published directory is verified against the recorded identity and recorded as ``published``.

Ownership is only ever the recorded identity: the typed layout hash (``directory_layout``) describes a layout and an
identical empty tree made by someone else has the same one. A retry recognizes a published directory only by the exact
recorded inode, metadata and layout. Cleanup removes only the empty directories a receipt proved (never files, never a
recursive erase). When ownership cannot be established the application fails closed with an exact disposition and its
claims remain; nothing unproven is adopted, repaired or deleted.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import os
from pathlib import Path, PurePosixPath
import stat
import sys

from qorqut_operations.contracts import DomainError
from . import safe_io
from .directory_layout import checked_values, layout_hash
from .receipts import ApplyReceipts

PHASES = ("intent", "staged", "published", "abandoned")
UNTOUCHED, STAGED, PUBLISHED, UNCERTAIN = "untouched", "staged", "published", "uncertain"
RENAME_NOREPLACE = 1
SCAN_LIMIT = 256
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class RegisteredDirectoryTarget:
    """A directory the trusted host registers for creation: ``relative_path`` below ``root``, staged in ``staging_root``.

    ``staging_root`` is private runtime state: absolute, outside ``root``'s authored tree and never the target itself."""

    root: Path
    relative_path: str
    staging_root: Path

    def resolve(self) -> Path:
        relative = PurePosixPath(self.relative_path)
        if (not self.relative_path or relative.is_absolute() or ".." in relative.parts or "." in relative.parts
                or "\0" in self.relative_path or "\\" in self.relative_path):
            raise DomainError("validation_error", "Invalid registered directory path")
        root, staging = Path(self.root).absolute(), Path(self.staging_root).absolute()
        if not staging.is_absolute() or ".." in staging.parts or "\0" in str(staging):
            raise DomainError("invalid_configuration", "The directory staging area must be an explicit absolute path")
        with safe_io.directory(root):
            pass
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise DomainError("forbidden", "Registered directory path crosses a symbolic link")
        if current == root or staging == current or current in staging.parents or staging in current.parents or staging == root:
            raise DomainError("invalid_configuration", "The directory staging area overlaps the registered target")
        return current


def _stuck(disposition, message, recovery, **details):
    return DomainError("apply_incomplete", message, details={"disposition": disposition, "recovery": recovery, **details})


def _occupied():
    return DomainError("revision_conflict", "The directory already exists; an existing directory is never adopted",
                       details={"disposition": "destination_occupied"})


def _identity(info) -> dict:
    return {"dev": info.st_dev, "ino": info.st_ino, "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid,
            "gid": info.st_gid, "nlink": info.st_nlink}


def _lstat(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def snapshot(fd) -> tuple[dict, list]:
    """The directories (relative path -> identity) below an open directory and anything else found, without following links."""
    entries, unexpected = {}, []

    def visit(descriptor, relative):
        entries[relative] = _identity(os.fstat(descriptor))
        for name in sorted(os.listdir(descriptor)):
            child = name if relative == "." else relative + "/" + name
            if len(entries) + len(unexpected) >= SCAN_LIMIT:
                unexpected.append(child)
                continue
            info = _lstat(descriptor, name)
            if info is None or not stat.S_ISDIR(info.st_mode):
                unexpected.append(child)
                continue
            try:
                sub = os.open(name, _FLAGS, dir_fd=descriptor)
            except OSError:
                unexpected.append(child)
                continue
            try:
                visit(sub, child)
            finally:
                os.close(sub)

    visit(fd, ".")
    return entries, unexpected


def proof_from(fd) -> list:
    """The exact identity proof of the tree under an open directory: one sorted record per directory."""
    entries, unexpected = snapshot(fd)
    if unexpected:
        raise _stuck("unexpected_contents", "The directory tree holds entries beyond its prepared layout",
                     "inspect_and_remove_staging")
    return [{"path": path, **entries[path]} for path in sorted(entries)]


def matches(fd, proof) -> str:
    """``match`` when the open directory is exactly the recorded tree, else why not: ``replaced`` or ``unexpected_contents``."""
    recorded = {item["path"]: {key: value for key, value in item.items() if key != "path"} for item in proof}
    entries, unexpected = snapshot(fd)
    root = entries["."]
    if (root["dev"], root["ino"]) != (recorded["."]["dev"], recorded["."]["ino"]):
        return "replaced"
    return "match" if not unexpected and entries == recorded else "unexpected_contents"


@contextmanager
def _chain(path, *, create):
    """An open directory descriptor for an absolute path, walked with O_NOFOLLOW; created components are made durable."""
    path = Path(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, _FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                os.fsync(descriptor)
                child = os.open(component, _FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise DomainError("forbidden", "Directory path crosses a symbolic link or non-directory") from None
        raise
    finally:
        os.close(descriptor)


@contextmanager
def _parent(path, *, create):
    with _chain(Path(path).parent, create=create) as descriptor:
        yield descriptor, Path(path).name


def _rename_noreplace(source_fd, source, destination_fd, destination):
    name, flag = ("renameatx_np", 0x00000004) if sys.platform == "darwin" else ("renameat2", RENAME_NOREPLACE)
    function = getattr(ctypes.CDLL(None, use_errno=True), name, None)
    if function is None:
        raise DomainError("invalid_configuration", "This host cannot publish a directory without replacing")
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), flag) != 0:
        code = ctypes.get_errno()
        if code in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP):
            raise DomainError("invalid_configuration", "This filesystem cannot publish a directory without replacing")
        raise OSError(code, os.strerror(code))


def observe_directory(target: RegisteredDirectoryTarget) -> str | None:
    """The typed layout hash of what exists at the target, or ``None`` when nothing does.

    Anything beyond directories is part of the hash, so only an exactly empty layout equals a prepared ``after_hash``. This
    describes the tree; it never proves who made it."""
    path = target.resolve()
    try:
        with _parent(path, create=False) as (parent, name):
            info = _lstat(parent, name)
            if info is None:
                return None
            if stat.S_ISLNK(info.st_mode):
                raise DomainError("forbidden", "Registered directory target is a symbolic link")
            if not stat.S_ISDIR(info.st_mode):
                return layout_hash((), (".",))
            descriptor = os.open(name, _FLAGS, dir_fd=parent)
            try:
                entries, unexpected = snapshot(descriptor)
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        return None
    return layout_hash((path for path in entries if path != "."), unexpected)


class DirectoryEffects:
    """The ``create_directory_tree`` half of ``FileApplier``: it records progress in the same apply receipt."""

    def __init__(self, database, phase: Callable[[object, str], None]):
        self.database, self.phase = database, phase

    # -- receipt progress ----------------------------------------------------------------------------------------
    def entry(self, receipt_id, change) -> dict:
        with self.database.read() as uow:
            return dict(ApplyReceipts(uow).get(receipt_id)["progress_json"].get(ApplyReceipts.progress_key(change.resource), {}))

    def _record(self, receipt_id, change, value):
        with self.database.transaction() as uow:
            ApplyReceipts(uow).record_progress(receipt_id, ApplyReceipts.progress_key(change.resource), value)

    # -- inspection ------------------------------------------------------------------------------------------------
    @staticmethod
    def _inspect(parent, name, proof):
        """``absent``, ``match`` or why the entry is not the recorded tree."""
        info = _lstat(parent, name)
        if info is None:
            return "absent"
        if not stat.S_ISDIR(info.st_mode):
            return "replaced"
        try:
            descriptor = os.open(name, _FLAGS, dir_fd=parent)
        except OSError:
            return "replaced"
        try:
            return matches(descriptor, proof)
        finally:
            os.close(descriptor)

    def _places(self, target, state):
        """What stands at the staging name and at the destination, against the recorded proof."""
        destination = target.resolve()
        proof = state["identity"]
        try:
            with _chain(target.staging_root, create=False) as staging:
                staged = self._inspect(staging, state["staging"], proof)
        except FileNotFoundError:
            staged = "absent"
        try:
            with _parent(destination, create=False) as (parent, leaf):
                placed = self._inspect(parent, leaf, proof)
        except FileNotFoundError:
            placed = "absent"
        return staged, placed

    def condition(self, receipt_id, change, target) -> str:
        """Without changing anything: ``untouched`` (nothing of this receipt exists), ``staged`` (its proven unpublished tree),
        ``published`` (its proven tree at the destination) or ``uncertain``."""
        state = self.entry(receipt_id, change)
        phase = state.get("phase")
        if phase in (None, "abandoned"):
            return UNTOUCHED
        if phase == "intent":
            try:
                with _chain(target.staging_root, create=False) as staging:
                    return UNCERTAIN if _lstat(staging, state["staging"]) is not None else UNTOUCHED
            except FileNotFoundError:
                return UNTOUCHED
        staged, placed = self._places(target, state)
        if phase == "published":
            return PUBLISHED if placed == "match" and staged == "absent" else UNCERTAIN
        if staged == "match" and placed in ("absent", "replaced"):
            return STAGED
        return PUBLISHED if staged == "absent" and placed == "match" else UNCERTAIN

    def verify(self, target, state):
        """Re-prove a published directory against its recorded identity; any difference is a conflict."""
        if state.get("phase") != "published" or not state.get("identity"):
            raise DomainError("apply_incomplete", "The directory has no recorded publication")
        _, placed = self._places(target, state)
        if placed != "match":
            raise DomainError("revision_conflict", "The published directory no longer matches its recorded identity",
                              details={"disposition": "target_replaced" if placed != "unexpected_contents" else "unexpected_contents"})

    # -- apply -------------------------------------------------------------------------------------------------------
    def apply(self, receipt_id, change, target):
        layout = checked_values(change.values)
        destination = target.resolve()
        state = self.entry(receipt_id, change)
        phase = state.get("phase")
        if phase == "published":
            self.verify(target, state)
            return
        if phase == "abandoned":
            raise _occupied()
        if phase is None:
            self._absent_or_occupied(destination)
            name = f"{receipt_id}-{ApplyReceipts.progress_key(change.resource)[:16]}"
            self._record(receipt_id, change, {"phase": "intent", "staging": name})
            self.phase(change.resource, "intent")
            self._stage(receipt_id, change, target, destination, name, layout)
            return
        if phase == "intent":
            self._resume_intent(receipt_id, change, target, destination, state, layout)
            return
        if phase != "staged" or not state.get("identity"):
            raise _stuck("unrecognized_progress", "The directory progress is not recognized", "inspect_receipt")
        self._resume_staged(receipt_id, change, target, destination, state)

    @staticmethod
    def _absent_or_occupied(destination):
        try:
            with _parent(destination, create=False) as (parent, name):
                if _lstat(parent, name) is not None:
                    raise _occupied()
        except FileNotFoundError:
            pass

    def _resume_intent(self, receipt_id, change, target, destination, state, layout):
        """Intent without a proof: only a clean absence of the staging tree is safe to continue from."""
        name = state["staging"]
        try:
            with _chain(target.staging_root, create=False) as staging:
                present = _lstat(staging, name) is not None
        except FileNotFoundError:
            present = False
        if present:
            raise _stuck("unproven_staging", "Directory staging was interrupted before its identity was recorded; it cannot be "
                         "proven, adopted or removed", "inspect_and_remove_staging", staging=name, destination=target.relative_path)
        self._absent_or_occupied(destination)
        self._stage(receipt_id, change, target, destination, name, layout)

    def _stage(self, receipt_id, change, target, destination, name, layout):
        with _chain(target.staging_root, create=True) as staging, _parent(destination, create=True) as (parent, leaf):
            if os.fstat(staging).st_dev != os.fstat(parent).st_dev:
                raise DomainError("invalid_configuration", "The directory staging area is not on the destination filesystem")
            if _lstat(parent, leaf) is not None:
                raise _occupied()
            try:
                os.mkdir(name, 0o700, dir_fd=staging)
            except FileExistsError:
                raise _stuck("unproven_staging", "A staging directory already exists without a recorded identity",
                             "inspect_and_remove_staging", staging=name, destination=target.relative_path) from None
            root = os.open(name, _FLAGS, dir_fd=staging)
            try:
                try:
                    self._build(root, layout)
                except BaseException:
                    self._discard_own(staging, name, root, layout)
                    raise
                os.fsync(staging)
                self.phase(change.resource, "built")
                proof = proof_from(root)
                self._record(receipt_id, change, {"phase": "staged", "staging": name, "identity": proof,
                                                  "after_hash": change.values["after_hash"]})
                self.phase(change.resource, "staged")
                self._publish(receipt_id, change, staging, name, parent, leaf, proof, layout)
            finally:
                os.close(root)

    @staticmethod
    def _build(root, layout):
        opened = {"": root}
        try:
            for path in layout:
                head, _, tail = path.rpartition("/")
                os.mkdir(tail, 0o700, dir_fd=opened[head])
                opened[path] = os.open(tail, _FLAGS, dir_fd=opened[head])
            for path in sorted(opened, reverse=True):
                os.fsync(opened[path])
        finally:
            for path, descriptor in opened.items():
                if path:
                    os.close(descriptor)

    @staticmethod
    def _discard_own(staging, name, root, layout):
        """A build that fails in this process removes exactly the empty directories this process just made (best effort)."""
        for path in sorted(layout, reverse=True):
            head, _, tail = path.rpartition("/")
            try:
                with _opened(root, head) as holder:
                    os.rmdir(tail, dir_fd=holder)
            except FileNotFoundError:
                continue
            except OSError:
                return
        try:
            os.rmdir(name, dir_fd=staging)
        except OSError:
            pass

    def _publish(self, receipt_id, change, staging, name, parent, leaf, proof, layout):
        try:
            _rename_noreplace(staging, name, parent, leaf)
        except FileExistsError:
            self._remove_proven(receipt_id, change, staging, name, proof, layout)
            raise _occupied() from None
        os.fsync(parent)
        os.fsync(staging)
        self.phase(change.resource, "renamed")
        self._record_published(receipt_id, change, parent, leaf, name, proof)

    def _record_published(self, receipt_id, change, parent, leaf, name, proof):
        placed = self._inspect(parent, leaf, proof)
        if placed != "match":
            raise _stuck("target_replaced" if placed != "unexpected_contents" else "unexpected_contents",
                         "The published directory does not match its recorded identity", "inspect_destination",
                         destination=leaf)
        self._record(receipt_id, change, {"phase": "published", "staging": name, "identity": proof, "verified": True,
                                          "after_hash": change.values["after_hash"]})
        self.phase(change.resource, "published")

    def _resume_staged(self, receipt_id, change, target, destination, state):
        staged, placed = self._places(target, state)
        proof, name = state["identity"], state["staging"]
        layout = checked_values(change.values)
        if staged == "match" and placed == "absent":
            with _chain(target.staging_root, create=False) as staging, _parent(destination, create=True) as (parent, leaf):
                if os.fstat(staging).st_dev != os.fstat(parent).st_dev:
                    raise DomainError("invalid_configuration", "The directory staging area is not on the destination filesystem")
                self._publish(receipt_id, change, staging, name, parent, leaf, proof, layout)
        elif staged == "match":
            with _chain(target.staging_root, create=False) as staging:
                self._remove_proven(receipt_id, change, staging, name, proof, layout)
            raise _occupied()
        elif staged == "absent" and placed == "match":
            with _parent(destination, create=False) as (parent, leaf):
                self._record_published(receipt_id, change, parent, leaf, name, proof)
        elif staged == "absent" and placed == "absent":
            raise _stuck("staging_missing", "The recorded staging directory is gone and nothing was published",
                         "inspect_staging_and_destination", staging=name, destination=target.relative_path)
        elif staged == "absent":
            raise _stuck("target_replaced" if placed == "replaced" else "unexpected_contents",
                         "The destination is not the directory this application published",
                         "inspect_destination", destination=target.relative_path)
        else:
            raise _stuck("staging_changed", "The recorded staging directory changed before publication",
                         "inspect_and_remove_staging", staging=name, destination=target.relative_path)

    def _remove_proven(self, receipt_id, change, staging, name, proof, layout):
        """Remove the receipt-proven, still empty staging tree: exactly the recorded directories, nothing else."""
        if self._inspect(staging, name, proof) != "match":
            raise _stuck("staging_changed", "The recorded staging directory changed; it will not be removed",
                         "inspect_and_remove_staging", staging=name)
        root = os.open(name, _FLAGS, dir_fd=staging)
        try:
            for path in sorted(layout, reverse=True):
                head, _, tail = path.rpartition("/")
                with _opened(root, head) as holder:
                    os.rmdir(tail, dir_fd=holder)
        finally:
            os.close(root)
        os.rmdir(name, dir_fd=staging)
        os.fsync(staging)
        self._record(receipt_id, change, {"phase": "abandoned"})
        self.phase(change.resource, "abandoned")

    # -- release ---------------------------------------------------------------------------------------------------
    def release(self, receipt_id, change, target):
        """Prove that nothing of this receipt remains (removing a proven unpublished tree) or raise ``apply_incomplete``."""
        condition = self.condition(receipt_id, change, target)
        if condition == STAGED:
            state = self.entry(receipt_id, change)
            with _chain(target.staging_root, create=False) as staging:
                self._remove_proven(receipt_id, change, staging, state["staging"], state["identity"], checked_values(change.values))
        elif condition != UNTOUCHED:
            raise DomainError("apply_incomplete", "Application has file effects; claims must remain for recovery")


@contextmanager
def _opened(root, relative):
    """A descriptor for ``relative`` below an open directory (the directory itself when empty), opened with O_NOFOLLOW."""
    if not relative:
        yield root
        return
    held = []
    try:
        descriptor = root
        for part in relative.split("/"):
            descriptor = os.open(part, _FLAGS, dir_fd=descriptor)
            held.append(descriptor)
        yield descriptor
    finally:
        for descriptor in held:
            os.close(descriptor)
