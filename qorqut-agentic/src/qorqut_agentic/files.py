"""Confined filesystem access for the authoring root.

Every operation resolves a root-relative path one component at a time with
``O_NOFOLLOW`` and directory file descriptors, so a symlink anywhere on the path
is rejected rather than followed, even if it appears after a check.
"""

from __future__ import annotations

import errno
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .errors import AgenticError, ConflictError, StateConflictError, UnsafePathError
from .util import sha256_hex

TMP_PREFIX = ".agentic-tmp-"
MAX_PATH_LENGTH = 1024
MAX_COMPONENT_LENGTH = 255
_CHUNK = 1024 * 1024
# A remembered digest is reused only for a file whose last change (mtime and ctime) was at least this long before
# the bytes were hashed. A rewrite inside the filesystem's timestamp granularity can leave the size and both
# timestamps unchanged, so a file that was still "fresh" when hashed is hashed again on every inventory until it
# settles. Two seconds covers coarse kernel clocks and the coarsest common on-disk granularity.
DIGEST_SETTLE_NS = 2_000_000_000
DIGEST_CACHE_LIMIT = 20000


def _identity(info: os.stat_result) -> tuple:
    """Everything a content change must disturb: a write updates mtime and ctime; a replace changes the inode."""
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def split_path(rel: object, *, field: str = "path") -> tuple[str, ...]:
    """Validate a root-relative POSIX path and return its components."""
    if not isinstance(rel, str) or not rel:
        raise UnsafePathError(f"{field} must be a non-empty string", details={"path": rel})
    if len(rel) > MAX_PATH_LENGTH:
        raise UnsafePathError(f"{field} is too long", details={"path": rel[:80]})
    if rel.startswith("/") or "\\" in rel:
        raise UnsafePathError(f"{field} must be a relative POSIX path: {rel!r}", details={"path": rel})
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in rel):
        raise UnsafePathError(f"{field} contains control characters", details={"path": rel})
    parts = tuple(rel.split("/"))
    for part in parts:
        if part in ("", ".", ".."):
            raise UnsafePathError(f"{field} has an empty, '.' or '..' component: {rel!r}", details={"path": rel})
        if len(part) > MAX_COMPONENT_LENGTH:
            raise UnsafePathError(f"{field} has a component that is too long", details={"path": rel[:80]})
        if part.lower() == ".git":
            raise UnsafePathError(f"{field} may not name .git: {rel!r}", details={"path": rel})
        if part.startswith(TMP_PREFIX):
            raise UnsafePathError(f"{field} uses the reserved prefix {TMP_PREFIX}: {rel!r}", details={"path": rel})
    return parts


@dataclass(frozen=True)
class FileEntry:
    sha256: str
    size: int


class SafeTree:
    """Read/write/walk confined to ``root`` (an existing directory)."""

    def __init__(self, root: Path | str):
        self.root = os.fspath(root)
        # rel -> (stat identity, FileEntry) of bytes this process hashed itself; see ``inventory``.
        self._digests: dict[str, tuple[tuple, FileEntry]] = {}

    # -- directory descriptors -------------------------------------------------
    def _open_root(self) -> int:
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    def _open_dir(self, parts: tuple[str, ...], *, create: bool = False) -> int | None:
        """Open the directory ``parts`` beneath the root; ``None`` if absent."""
        fd = self._open_root()
        try:
            for index, name in enumerate(parts):
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        os.close(fd)
                        return None
                    try:
                        os.mkdir(name, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                    os.fsync(fd)
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        kind = self._lstat_kind(fd, name)
                        where = "/".join(parts[: index + 1])
                        if kind == "symlink":
                            raise UnsafePathError(f"path traverses a symlink: {where}", details={"path": where}) from exc
                        raise ConflictError(
                            f"a parent of the path is not a directory: {where}",
                            details={"conflicts": [{"path": where, "reason": "parent_is_file"}]},
                        ) from exc
                    raise
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    @staticmethod
    def _lstat_kind(dir_fd: int, name: str) -> str:
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "missing"
        mode = info.st_mode
        if stat.S_ISLNK(mode):
            return "symlink"
        if stat.S_ISREG(mode):
            return "file"
        if stat.S_ISDIR(mode):
            return "dir"
        return "other"

    # -- inspection ------------------------------------------------------------
    def kind(self, rel: str) -> str:
        """``missing``, ``file``, ``dir``, ``symlink`` or ``other`` (never follows links)."""
        parts = split_path(rel)
        try:
            parent = self._open_dir(parts[:-1])
        except ConflictError:
            return "missing"
        if parent is None:
            return "missing"
        try:
            return self._lstat_kind(parent, parts[-1])
        finally:
            os.close(parent)

    def read(self, rel: str, *, max_bytes: int | None = None) -> bytes | None:
        """Return file bytes, ``None`` when absent. Symlinks and non-files are rejected."""
        parts = split_path(rel)
        try:
            parent = self._open_dir(parts[:-1])
        except ConflictError:
            return None
        if parent is None:
            return None
        try:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise UnsafePathError(f"path is a symlink: {rel}", details={"path": rel}) from exc
                raise
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise UnsafePathError(f"path is not a regular file: {rel}", details={"path": rel})
                if max_bytes is not None and info.st_size > max_bytes:
                    raise AgenticError(
                        f"file is larger than {max_bytes} bytes: {rel}",
                        code="limit_exceeded",
                        details={"path": rel, "size": info.st_size, "limit": max_bytes},
                    )
                chunks = []
                size = 0
                while True:
                    chunk = os.read(fd, _CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise AgenticError(f"file is larger than {max_bytes} bytes: {rel}", code="limit_exceeded",
                                           details={"path": rel, "limit": max_bytes})
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def digest(self, rel: str) -> FileEntry | None:
        data = self.read(rel)
        return None if data is None else FileEntry(sha256_hex(data), len(data))

    def mode(self, rel: str) -> int | None:
        parts = split_path(rel)
        parent = self._open_dir(parts[:-1])
        if parent is None:
            return None
        try:
            try:
                return stat.S_IMODE(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode)
            except FileNotFoundError:
                return None
        finally:
            os.close(parent)

    # -- mutation --------------------------------------------------------------
    def write_atomic(self, rel: str, data: bytes, *, mode: int | None = None) -> None:
        """Create or atomically replace a regular file, durably."""
        parts = split_path(rel)
        parent = self._open_dir(parts[:-1], create=True)
        assert parent is not None
        try:
            name = parts[-1]
            kind = self._lstat_kind(parent, name)
            if kind == "symlink":
                raise UnsafePathError(f"path is a symlink: {rel}", details={"path": rel})
            if kind in ("dir", "other"):
                raise ConflictError(
                    f"path exists and is not a regular file: {rel}",
                    details={"conflicts": [{"path": rel, "reason": "is_directory"}]},
                )
            if mode is None:
                mode = (
                    stat.S_IMODE(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)
                    if kind == "file"
                    else 0o644
                )
            tmp = f"{TMP_PREFIX}{uuid.uuid4().hex}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fchmod(fd, mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=parent)
                except OSError:
                    pass
                raise
            os.fsync(parent)
        finally:
            os.close(parent)

    def delete(self, rel: str, *, prune: bool = True) -> bool:
        """Remove a regular file; prune emptied directories below the first level."""
        parts = split_path(rel)
        parent = self._open_dir(parts[:-1])
        if parent is None:
            return False
        try:
            kind = self._lstat_kind(parent, parts[-1])
            if kind == "missing":
                return False
            if kind != "file":
                raise UnsafePathError(f"refusing to delete a {kind}: {rel}", details={"path": rel})
            os.unlink(parts[-1], dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
        if prune:
            for depth in range(len(parts) - 1, 1, -1):
                grand = self._open_dir(parts[: depth - 1])
                if grand is None:
                    break
                try:
                    os.rmdir(parts[depth - 1], dir_fd=grand)
                    os.fsync(grand)
                except OSError:
                    break
                finally:
                    os.close(grand)
        return True

    def sweep_temp_files(self) -> int:
        """Remove leftover atomic-write temp files (after a crash)."""
        removed = 0

        def visit(fd: int) -> None:
            nonlocal removed
            with os.scandir(fd) as it:
                entries = list(it)
            for entry in entries:
                if entry.name.lower() == ".git":
                    continue
                if entry.name.startswith(TMP_PREFIX) and not entry.is_dir(follow_symlinks=False):
                    try:
                        os.unlink(entry.name, dir_fd=fd)
                        removed += 1
                    except OSError:
                        pass
                elif entry.is_dir(follow_symlinks=False):
                    try:
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                    except OSError:
                        continue
                    try:
                        visit(child)
                    finally:
                        os.close(child)

        fd = self._open_root()
        try:
            visit(fd)
        finally:
            os.close(fd)
        return removed

    # -- traversal -------------------------------------------------------------
    def walk(self, prefix: str = "") -> Iterator[tuple[str, str]]:
        """Yield ``(relative path, kind)`` for files, symlinks and special files.

        ``.git`` and atomic-write temp files are skipped. Directories are not
        yielded. Order is deterministic.
        """
        parts = split_path(prefix) if prefix else ()
        fd = self._open_dir(parts)
        if fd is None:
            return
        try:
            yield from self._walk_fd(fd, parts)
        finally:
            os.close(fd)

    def _walk_fd(self, fd: int, parts: tuple[str, ...]) -> Iterator[tuple[str, str]]:
        with os.scandir(fd) as it:
            entries = sorted(it, key=lambda entry: entry.name)
        for entry in entries:
            name = entry.name
            if name.lower() == ".git" or name.startswith(TMP_PREFIX):
                continue
            rel = "/".join((*parts, name))
            if entry.is_symlink():
                yield rel, "symlink"
            elif entry.is_dir(follow_symlinks=False):
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                except OSError:
                    yield rel, "other"
                    continue
                try:
                    yield from self._walk_fd(child, (*parts, name))
                finally:
                    os.close(child)
            elif entry.is_file(follow_symlinks=False):
                yield rel, "file"
            else:
                yield rel, "other"

    def inventory(self, prefix: str = "") -> tuple[dict[str, FileEntry], dict[str, str]]:
        """Hash every regular file; return ``(files, special)`` where special maps
        symlink/other paths to their kind.

        The tree is always walked and every entry is always ``lstat``-ed. Only the read-and-hash of a file is
        skipped, and only when this process hashed those exact bytes before and the file's device, inode, mode,
        size, mtime and ctime are all unchanged since (``_identity``), and the file had already settled when it was
        hashed (``DIGEST_SETTLE_NS``). Any other file is opened without following links and hashed as before.
        """
        files: dict[str, FileEntry] = {}
        special: dict[str, str] = {}
        parts = split_path(prefix) if prefix else ()
        fd = self._open_dir(parts)
        if fd is None:
            return files, special
        try:
            self._inventory_fd(fd, parts, files, special)
        finally:
            os.close(fd)
        if len(self._digests) > DIGEST_CACHE_LIMIT:
            self._digests.clear()
        return files, special

    def _inventory_fd(self, fd: int, parts: tuple[str, ...], files: dict, special: dict) -> None:
        """``_walk_fd`` with the same order, skips and kinds, digesting each regular file through its directory."""
        with os.scandir(fd) as it:
            entries = sorted(it, key=lambda entry: entry.name)
        for entry in entries:
            name = entry.name
            if name.lower() == ".git" or name.startswith(TMP_PREFIX):
                continue
            rel = "/".join((*parts, name))
            if entry.is_symlink():
                special[rel] = "symlink"
            elif entry.is_dir(follow_symlinks=False):
                try:
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                except OSError:
                    special[rel] = "other"
                    continue
                try:
                    self._inventory_fd(child, (*parts, name), files, special)
                finally:
                    os.close(child)
            elif entry.is_file(follow_symlinks=False):
                digest = self._digest_at(fd, name, rel)
                if digest is not None:
                    files[rel] = digest
            else:
                special[rel] = "other"

    def _digest_at(self, parent: int, name: str, rel: str) -> FileEntry | None:
        """``digest`` for one entry of an already confined directory; ``None`` when the file is gone."""
        try:
            current = _identity(os.stat(name, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            return None
        known = self._digests.get(rel)
        if known is not None and known[0] == current:
            return known[1]  # this exact path was validated when its digest was remembered
        split_path(rel)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise UnsafePathError(f"path is a symlink: {rel}", details={"path": rel}) from exc
            raise
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise UnsafePathError(f"path is not a regular file: {rel}", details={"path": rel})
            chunks = []
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
            hashed_at = time.time_ns()
            after = os.fstat(fd)
        finally:
            os.close(fd)
        entry = FileEntry(sha256_hex(data), len(data))
        identity = _identity(before)
        # Remember only bytes that provably belong to one settled state of the file: unchanged while it was read,
        # exactly as long as its recorded size, and last changed well before the hash was taken.
        if (identity == _identity(after) and len(data) == before.st_size
                and hashed_at - max(before.st_mtime_ns, before.st_ctime_ns) >= DIGEST_SETTLE_NS):
            self._digests[rel] = (identity, entry)
        else:
            self._digests.pop(rel, None)
        return entry


def _legacy_content(tree: SafeTree) -> bool:
    """Inspect legacy scaffolding without following links or reading files."""
    root = tree._open_root()
    count = 0

    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns, info.st_ctime_ns)

    def visit(parent, name, parts, expected=None):
        nonlocal count
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if expected is not None and identity(before) != identity(expected):
            raise StateConflictError("Legacy directory changed during inspection")
        if not stat.S_ISDIR(before.st_mode):
            return True
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            opened = os.fstat(fd)
            if identity(before) != identity(opened):
                raise StateConflictError("Legacy directory changed during inspection")
            with os.scandir(fd) as entries:
                for entry in entries:
                    count += 1
                    if count > 100000 or len(parts) + 1 > 64:
                        raise AgenticError("Legacy directory inspection limit exceeded", code="limit_exceeded")
                    child_parts = (*parts, entry.name)
                    split_path('/'.join(child_parts))
                    if visit(fd, entry.name, child_parts):
                        return True
            if identity(os.fstat(fd)) != identity(opened) or identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(opened):
                raise StateConflictError("Legacy directory changed during inspection")
            return False
        finally:
            os.close(fd)

    try:
        try:
            initial = os.stat('packages', dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            if tree._lstat_kind(root, 'packages') != 'missing':
                raise StateConflictError("Legacy directory changed during inspection")
            return False
        try:
            return visit(root, 'packages', ('packages',), initial)
        except FileNotFoundError as exc:
            raise StateConflictError("Legacy directory changed during inspection") from exc
    finally:
        os.close(root)


class DiskSource:
    """A hashed index of ``prefix`` on disk with verified reads."""

    def __init__(self, tree: SafeTree, prefix: str | None = None):
        self.tree = tree
        self.prefix = prefix
        self.legacy_nonempty = False
        if prefix is None:
            self.legacy_nonempty = _legacy_content(tree)
            self.files, self.special = {}, {}
            for area in ("roles", "operations", "overseer", "plugins", "user-preferences", "packages"):
                files, special = tree.inventory(area)
                self.files.update(files)
                self.special.update(special)
            self.legacy_nonempty = self.legacy_nonempty or any(
                p.startswith("packages/") for p in (*self.files, *self.special))
        else:
            self.files, self.special = tree.inventory(prefix)
        self._cache: dict[str, bytes] = {}

    def read(self, rel: str, *, max_bytes: int | None = None) -> bytes:
        if rel in self._cache:
            data = self._cache[rel]
            if max_bytes is not None and len(data) > max_bytes:
                raise AgenticError(f"file is larger than {max_bytes} bytes: {rel}",
                                   code="limit_exceeded", details={"path": rel, "limit": max_bytes})
            return data
        data = self.tree.read(rel, max_bytes=max_bytes)
        expected = self.files.get(rel)
        if data is None or expected is None or sha256_hex(data) != expected.sha256:
            raise StateConflictError(f"file changed while it was being read: {rel}", details={"path": rel})
        self._cache[rel] = data
        return data


class OverlaySource:
    """A disk source with pending creates/edits (bytes) and deletes (``None``)."""

    def __init__(self, base: DiskSource, changes: dict[str, bytes | None]):
        self.base = base
        self.prefix = base.prefix
        self.legacy_nonempty = base.legacy_nonempty
        self.changes = {rel: data for rel, data in changes.items() if base.prefix is None or rel.startswith(base.prefix + "/")}
        self.files = dict(base.files)
        self.special = dict(base.special)
        for rel, data in self.changes.items():
            if data is None:
                self.files.pop(rel, None)
            else:
                self.files[rel] = FileEntry(sha256_hex(data), len(data))
            self.special.pop(rel, None)

    def read(self, rel: str) -> bytes:
        if rel in self.changes:
            data = self.changes[rel]
            if data is None:
                raise KeyError(rel)
            return data
        return self.base.read(rel)
