"""Descriptor-anchored reads of one regular file beneath the operations root.

Every path component is opened with ``O_NOFOLLOW`` relative to its parent
descriptor, starting from the root directory, so a symlink anywhere below the
root is refused (the documented policy) and a component swapped for a symlink
while walking cannot redirect the open outside the root. The final file is
checked with ``lstat`` before it is opened (so a device or FIFO is never
opened), opened non-blocking without becoming a controlling terminal, and
checked again with ``fstat`` on the opened descriptor. Previews and downloads
read that descriptor; nothing reopens the pathname.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone

from qorqut_operations.contracts import DomainError

CHUNK = 64 * 1024
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


def missing():
    return DomainError("not_found", "No file exists at this path (it may have been moved, renamed or deleted)")


def symlink():
    return DomainError("symlink_not_followed",
                       "This path passes through a symbolic link; the viewer never follows symbolic links")


def not_regular(kind="not a regular file"):
    return DomainError("not_a_regular_file", f"This path is {kind}; only regular files can be viewed")


def unreadable():
    return DomainError("file_unreadable", "The file exists but the service cannot read it")


def _describe(mode):
    return ("a directory" if stat.S_ISDIR(mode) else "a device, pipe or socket"
            if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)
            else "not a regular file")


def _lstat(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        raise missing() from None
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise missing() from None
        if exc.errno in {errno.EACCES, errno.EPERM}:
            raise unreadable() from None
        raise


def _open(parent, name, flags):
    try:
        return os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM}:
            raise unreadable() from None
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            # Either a symlink (refused) or a non-directory where a directory is needed (no such path).
            if stat.S_ISLNK(_lstat(parent, name).st_mode):
                raise symlink() from None
            raise missing() from None
        if exc.errno in {errno.ENOENT, errno.ENAMETOOLONG}:
            raise missing() from None
        raise


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@contextmanager
def open_directory(root, relative):
    """Hold a normalized directory path open, refusing links at every component."""
    parent = _open(None, root, _DIRECTORY)
    try:
        for name in relative.split("/") if relative else ():
            child = _open(parent, name, _DIRECTORY)
            os.close(parent)
            parent = child
        yield parent
    finally:
        os.close(parent)


class OpenedFile:
    """An opened, verified regular file; owns its descriptor until ``close``."""

    def __init__(self, stream, relative, info):
        self.stream, self.relative = stream, relative
        self.name = relative.rsplit("/", 1)[-1]
        self.size, self.modified_at = info.st_size, iso(info.st_mtime)

    def read_preview(self, limit):
        """At most ``limit`` bytes from the start, and whether the file continued past them."""
        self.stream.seek(0)
        data = self.stream.read(limit + 1)
        return data[:limit], len(data) > limit

    def sha256(self, limit):
        """Digest of the whole file, or None when it is larger than ``limit`` (never reads past it)."""
        if self.size > limit:
            return None
        self.stream.seek(0)
        digest, remaining = hashlib.sha256(), limit + 1
        while remaining > 0:
            chunk = self.stream.read(min(CHUNK, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        return None if remaining <= 0 else digest.hexdigest()

    def chunks(self):
        """The file as it was when opened: at most its opened size, read from the opened descriptor."""
        self.stream.seek(0)
        remaining = self.size
        try:
            while remaining > 0:
                chunk = self.stream.read(min(CHUNK, remaining))
                if not chunk:
                    return  # The file shrank; the short body tells the client the download is incomplete.
                remaining -= len(chunk)
                yield chunk
        finally:
            self.close()

    def close(self):
        self.stream.close()


def open_regular(root, relative):
    """Open ``relative`` (a normalized root-relative POSIX path) beneath ``root`` without following symlinks."""
    names = relative.split("/")
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in names[:-1]:
            child = _open(parent, name, _DIRECTORY)
            os.close(parent)
            parent = child
        info = _lstat(parent, names[-1])
        if stat.S_ISLNK(info.st_mode):
            raise symlink()
        if not stat.S_ISREG(info.st_mode):
            raise not_regular(_describe(info.st_mode))
        descriptor = _open(parent, names[-1], _FILE)
    finally:
        os.close(parent)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise not_regular(_describe(opened.st_mode))
    except BaseException:
        os.close(descriptor)
        raise
    return OpenedFile(os.fdopen(descriptor, "rb"), relative, opened)
