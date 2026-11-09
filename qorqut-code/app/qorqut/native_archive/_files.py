"""Descriptor-relative archive files: a symlink is never followed, ancestors included.

Adapted from the v2.2 donor ``app/qorqut_app/native/transcripts/_files.py`` at
``qorqut-v2.2/qorqut-code`` commit df7daa7fddf387a7e91212f5ed831b32c0735f96 (exact
descriptor-relative traversal from ``/`` with ``O_NOFOLLOW`` and regular-file checks).
Differences: only the final
component of the archive root may be created (its parent must already exist, so a mistaken
root cannot fabricate a directory tree), writers publish immutable files with ``link`` instead
of replacing, appends use positional writes, and the source is opened with a stable identity
check. Nothing here caches a path or resolves one with ``realpath``.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import PurePosixPath
import stat
import time
from uuid import uuid4

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
TEMP_PREFIX = ".tmp-"


class UnsafePath(Exception):
    """A symlink, special file or non-directory ancestor where a plain entry is required."""


class LockTimeout(Exception):
    pass


def _open_dir(name: str, parent_fd: int) -> int:
    try:
        return os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafePath(f"{name!r} is a symlink or not a directory") from error
        raise


def open_directory_chain(path: str, *, create_last: bool = False) -> int:
    """Open ``path`` one component at a time from ``/``; create at most its final component."""
    parts = PurePosixPath(path).parts
    fd = os.open("/", DIR_FLAGS)
    try:
        for index, part in enumerate(parts[1:], 1):
            created = False
            if create_last and index == len(parts) - 1:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
            child = _open_dir(part, fd)
            if created:
                os.fsync(fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_child_directory(parent_fd: int, name: str, *, create: bool = False) -> int:
    created = False
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
    fd = _open_dir(name, parent_fd)
    if created:
        os.fsync(parent_fd)
    return fd


def _checked_regular(fd: int, name: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise UnsafePath(f"{name!r} is not a regular file")
    return info


def open_regular(dir_fd: int, name: str, flags: int = FILE_READ, mode: int = 0o600) -> int:
    """Open an existing (or, with ``O_CREAT``, new) regular file; refuse symlinks and special files."""
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=dir_fd)
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
    os.fsync(fd)


def stage_temporary(dir_fd: int, content: bytes) -> str:
    """Write ``content`` to a private, fsynced temporary file; the caller publishes or discards it."""
    name = TEMP_PREFIX + uuid4().hex
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
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
        os.link(temporary, name, src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False)
    finally:
        unlink_quietly(source_fd, temporary)
    fsync_directory(target_fd)


def replace_atomic(dir_fd: int, temporary: str, name: str) -> None:
    os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    fsync_directory(dir_fd)


def unlink_quietly(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        pass


def remove_temporaries(dir_fd: int) -> None:
    for name in os.listdir(dir_fd):
        if name.startswith(TEMP_PREFIX):
            unlink_quietly(dir_fd, name)


def remove_all_files(dir_fd: int) -> None:
    for name in os.listdir(dir_fd):
        unlink_quietly(dir_fd, name)


def exists(dir_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


@contextmanager
def exclusive_lock(dir_fd: int, name: str, timeout: float):
    """Process-safe advisory lock on ``name`` (flock on its own open description, so threads exclude too)."""
    fd = open_regular(dir_fd, name, os.O_RDWR | os.O_CREAT)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in {errno.EAGAIN, errno.EACCES}:
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(name) from None
                time.sleep(0.005)
        yield
    finally:
        os.close(fd)


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
            info = os.stat(pure.name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise UnsafePath("the transcript source is a symlink or special file")
            try:
                fd = os.open(pure.name, FILE_READ, dir_fd=parent_fd)
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
            info = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return (info.st_dev, info.st_ino) if stat.S_ISREG(info.st_mode) else None

    def close(self) -> None:
        os.close(self.fd)
        os.close(self.parent_fd)
