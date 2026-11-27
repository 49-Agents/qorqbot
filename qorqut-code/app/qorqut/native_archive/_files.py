"""Descriptor-relative archive files: a symlink is never followed, ancestors included.

Adapted from the v2.2 donor ``app/qorqut_app/native/transcripts/_files.py`` at
``qorqut-v2.2/qorqut-code`` commit df7daa7fddf387a7e91212f5ed831b32c0735f96 (exact
descriptor-relative traversal from ``/`` that never follows a link, and regular-file checks).
Differences: only the final
component of the archive root may be created (its parent must already exist, so a mistaken
root cannot fabricate a directory tree), writers publish immutable files with ``link`` instead
of replacing, appends use positional writes, and the source is opened with a stable identity
check. Nothing here caches a path or resolves one with ``realpath``.

The folder, file and lock operations are the system layer's
(``qorqut_system.private_files`` and ``qorqut_system.locks``); this module keeps the walk from ``/``
and the archive's own shapes. Folders here are the opened folder numbers its caller keeps.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import PurePosixPath
import stat
import time
from uuid import uuid4

from qorqut_system import locks, private_files

FILE_READ = os.O_RDONLY | os.O_NONBLOCK
TEMP_PREFIX = ".tmp-"


class UnsafePath(Exception):
    """A symlink, special file or non-directory ancestor where a plain entry is required."""


class LockTimeout(Exception):
    pass


def _open_dir(name: str, parent, *, create: bool = False) -> private_files.Folder:
    try:
        return private_files.open_child_folder(parent, name, create=create)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafePath(f"{name!r} is a symlink or not a directory") from error
        raise


def open_directory_chain(path: str, *, create_last: bool = False) -> int:
    """Open ``path`` one component at a time from ``/``; create at most its final component."""
    parts = PurePosixPath(path).parts
    folder = private_files.open_folder("/")
    try:
        for index, part in enumerate(parts[1:], 1):
            if create_last and index == len(parts) - 1:
                child = _open_dir(part, folder, create=True)
            else:
                child = _open_dir(part, folder)
            folder.close()
            folder = child
        return folder.descriptor
    except BaseException:
        folder.close()
        raise


def open_child_directory(parent_fd: int, name: str, *, create: bool = False) -> int:
    folder = _open_dir(name, parent_fd, create=create)
    return folder.descriptor


def _checked_regular(fd: int, name: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise UnsafePath(f"{name!r} is not a regular file")
    return info


def open_regular(dir_fd: int, name: str, flags: int = FILE_READ, mode: int = 0o600) -> int:
    """Open an existing (or, with ``O_CREAT``, new) regular file; refuse symlinks and special files."""
    try:
        fd = private_files.open_entry(dir_fd, name, flags, mode=mode)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENXIO, errno.EISDIR}:
            raise UnsafePath(f"{name!r} is a symlink or special file") from error
        raise
    _checked_regular(fd, name)
    return fd


def read_small(dir_fd: int, name: str, limit: int) -> bytes | None:
    """Whole content of a small regular file, or None when absent."""
    try:
        fd = open_regular(dir_fd, name)
    except FileNotFoundError:
        return None
    try:
        if os.fstat(fd).st_size > limit:
            raise UnsafePath(f"{name!r} is larger than its bound")
        return pread_full(fd, limit + 1, 0)
    finally:
        os.close(fd)


def file_size(dir_fd: int, name: str) -> int:
    try:
        fd = open_regular(dir_fd, name)
    except FileNotFoundError:
        return 0
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)


def pread_full(fd: int, size: int, offset: int) -> bytes:
    """Up to ``size`` bytes at ``offset`` (short only at end of file)."""
    chunks = []
    remaining = size
    while remaining > 0:
        data = os.pread(fd, remaining, offset)
        if not data:
            break
        chunks.append(data)
        offset += len(data)
        remaining -= len(data)
    return chunks[0] if len(chunks) == 1 else b"".join(chunks)


def pwrite_full(fd: int, data: bytes | memoryview, offset: int) -> None:
    view = memoryview(data)
    while view:
        written = os.pwrite(fd, view, offset)
        view = view[written:]
        offset += written


def fsync_directory(fd: int) -> None:
    private_files.sync_folder(fd)


def stage_temporary(dir_fd: int, content: bytes) -> str:
    """Write ``content`` to a private, fsynced temporary file; the caller publishes or discards it."""
    name = TEMP_PREFIX + uuid4().hex
    fd = private_files.open_entry(dir_fd, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=0o600)
    try:
        pwrite_full(fd, content, 0)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        unlink_quietly(dir_fd, name)
        raise
    os.close(fd)
    return name


def publish_immutable(source_fd: int, temporary: str, target_fd: int, name: str) -> None:
    """Make ``temporary`` visible as ``name`` unless ``name`` exists; the temporary is always removed."""
    try:
        private_files.link_entry(source_fd, temporary, name, target_folder=target_fd)
    finally:
        unlink_quietly(source_fd, temporary)
    fsync_directory(target_fd)


def replace_atomic(dir_fd: int, temporary: str, name: str) -> None:
    private_files.rename_entry(dir_fd, temporary, name)
    fsync_directory(dir_fd)


def unlink_quietly(dir_fd: int, name: str) -> None:
    private_files.remove_entry(dir_fd, name, missing_ok=True)


def remove_temporaries(dir_fd: int) -> None:
    for name in private_files.list_entries(dir_fd):
        if name.startswith(TEMP_PREFIX):
            unlink_quietly(dir_fd, name)


def remove_all_files(dir_fd: int) -> None:
    for name in private_files.list_entries(dir_fd):
        unlink_quietly(dir_fd, name)


def exists(dir_fd: int, name: str) -> bool:
    try:
        private_files.stat_entry(dir_fd, name)
    except FileNotFoundError:
        return False
    return True


@contextmanager
def exclusive_lock(dir_fd: int, name: str, timeout: float):
    """Process-safe advisory lock on ``name`` (flock on its own open description, so threads exclude too)."""
    try:
        handle = locks.open_lock(dir_fd, name, create=True)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENXIO, errno.EISDIR}:
            raise UnsafePath(f"{name!r} is a symlink or special file") from error
        raise
    if not stat.S_ISREG(os.fstat(handle.descriptor).st_mode):
        handle.close()
        raise UnsafePath(f"{name!r} is not a regular file")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.acquire(exclusive=True, wait=False)
                break
            except OSError as error:
                if error.errno not in {errno.EAGAIN, errno.EACCES}:
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(name) from None
                time.sleep(0.005)
        yield
    finally:
        handle.close()


class Source:
    """An opened transcript source: a regular file reached without following any symlink.

    The parent directory descriptor stays open so the same name can be checked again after
    reading to prove the path still names the file that was read.
    """

    def __init__(self, parent_fd: int, name: str, fd: int):
        self.parent_fd, self.name, self.fd = parent_fd, name, fd

    @classmethod
    def open(cls, path: str) -> "Source":
        pure = PurePosixPath(path)
        parent_fd = open_directory_chain(str(pure.parent))
        try:
            info = private_files.stat_entry(parent_fd, pure.name)
            if not stat.S_ISREG(info.st_mode):
                raise UnsafePath("the transcript source is a symlink or special file")
            try:
                fd = private_files.open_entry(parent_fd, pure.name, FILE_READ)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENXIO}:
                    raise UnsafePath("the transcript source is a symlink or special file") from error
                raise
            opened = _checked_regular(fd, pure.name)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                os.close(fd)
                raise UnsafePath("the transcript source changed while it was opened")
        except BaseException:
            os.close(parent_fd)
            raise
        return cls(parent_fd, pure.name, fd)

    def stat(self) -> os.stat_result:
        return os.fstat(self.fd)

    def path_identity(self) -> tuple[int, int] | None:
        """Identity of what the source *name* refers to now (None when it vanished or is not plain)."""
        try:
            info = private_files.stat_entry(self.parent_fd, self.name)
        except FileNotFoundError:
            return None
        return (info.st_dev, info.st_ino) if stat.S_ISREG(info.st_mode) else None

    def close(self) -> None:
        os.close(self.fd)
        os.close(self.parent_fd)
