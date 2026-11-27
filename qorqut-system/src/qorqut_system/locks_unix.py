"""Whole-file locks on Unix: ``flock`` on a lock file, exclusive or shared, waiting or failing at once.

A lock file is opened once as a ``LockFile`` so a caller can check which file it opened, take the lock later, or hold
it through a block; the simple callers use ``lock()``. No descriptor number crosses a function boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from .errors import PrivateFileError


def _flags(exclusive, wait):
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    return operation if wait else operation | fcntl.LOCK_NB


def _open_at(target, name, flags, mode):
    """Open ``name`` in ``target``, which is a folder object, a folder descriptor or a path."""
    if isinstance(target, int):
        return os.open(name, flags, mode, dir_fd=target)
    descriptor = getattr(target, "descriptor", None)
    if descriptor is not None:
        return os.open(name, flags, mode, dir_fd=descriptor)
    return os.open(Path(target) / name, flags, mode)


def _stat_at(target, name):
    """Stat ``name`` in ``target`` without following a final link."""
    if isinstance(target, int):
        return os.stat(name, dir_fd=target, follow_symlinks=False)
    descriptor = getattr(target, "descriptor", None)
    if descriptor is not None:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    return os.stat(Path(target) / name, follow_symlinks=False)


class LockFile:
    """An opened lock file: hold it exclusively or shared, waiting or failing at once.

    ``identity`` is the ``(device, inode)`` of the file this handle opened, so ``verify`` can tell whether the name
    now leads to that same file rather than to one that replaced it."""

    def __init__(self, descriptor, identity, target, name):
        self.descriptor = descriptor
        self.identity = identity
        self._target, self._name = target, name

    def acquire(self, *, exclusive=True, wait=True):
        """Take the lock; ``BlockingIOError`` when ``wait`` is false and another holder has it."""
        fcntl.flock(self.descriptor, _flags(exclusive, wait))

    def verify(self):
        """Refuse when the name no longer leads to the file this handle opened."""
        named = _stat_at(self._target, self._name)
        if (named.st_dev, named.st_ino) != self.identity:
            raise PrivateFileError("The lock file changed")

    def release(self):
        """Let go of the lock without closing the file."""
        fcntl.flock(self.descriptor, fcntl.LOCK_UN)

    def close(self):
        """Close the file, which also releases any lock still held on it."""
        os.close(self.descriptor)


def open_descriptor(target, name, *, write=True):
    """Open a lock file without following a final link and return its descriptor, with no other step.

    For a caller that takes the lock itself and only needs the old open: the file is never created, and nothing is
    checked or remembered about it."""
    flags = os.O_CLOEXEC | (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW
    return _open_at(target, name, flags, 0o600)


def open_lock(target, name, *, create=True, write=True, mode=0o600, regular=False, verify=False, nonblock=False):
    """Open a lock file without following a final link.

    The file is created when missing and ``create`` is set. ``write`` is false for a shared lock on an existing file,
    which needs only read access. ``regular`` refuses anything but a regular file, where the caller checked that
    before; ``verify`` also refuses a name that no longer leads to the opened file. ``nonblock`` opens without
    waiting, as the callers that check the opened file do today.
    """
    flags = os.O_CLOEXEC | (os.O_RDWR if write else os.O_RDONLY)
    if create:
        flags |= os.O_CREAT
    if nonblock:
        flags |= os.O_NONBLOCK
    descriptor = _open_at(target, name, flags | os.O_NOFOLLOW, mode)
    try:
        opened = os.fstat(descriptor)
        if regular and not stat.S_ISREG(opened.st_mode):
            raise PrivateFileError("A lock file must be a regular file")
        handle = LockFile(descriptor, (opened.st_dev, opened.st_ino), target, name)
        if verify:
            handle.verify()
    except BaseException:
        os.close(descriptor)
        raise
    return handle


def acquire(stream, *, exclusive=True, wait=True):
    """Take the lock on a file the caller has already opened; ``BlockingIOError`` when it fails at once."""
    fcntl.flock(stream.fileno(), _flags(exclusive, wait))


def release(stream):
    """Let go of the lock on a file the caller has already opened."""
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def held(stream, *, exclusive=True, wait=True):
    """Hold the lock on a file the caller has already opened, for the caller's block."""
    acquire(stream, exclusive=exclusive, wait=wait)
    try:
        yield
    finally:
        release(stream)


@contextmanager
def lock(directory, name, *, exclusive=True, wait=True, create=True, mode=0o600):
    """Hold a lock on ``directory/name`` for the caller's block; ``BlockingIOError`` when ``wait`` is false.

    The file is created when it does not exist yet and ``create`` is set. The caller owns the folder's privacy through
    the private-files operations; a shared lock on an existing file opens it read-only.
    """
    handle = open_lock(directory, name, create=create, write=exclusive or create, mode=mode)
    try:
        handle.acquire(exclusive=exclusive, wait=wait)
        try:
            yield handle
        finally:
            handle.release()
    finally:
        handle.close()