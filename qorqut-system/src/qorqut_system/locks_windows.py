"""Whole-file locks on Windows: ``msvcrt.locking`` on a lock file, a first version.

The system locks byte ranges, not whole files, so this version locks the first byte. Windows has no shared lock the
way Unix has one, but it has a read lock, which is the same thing for this purpose. A wait is short and repeated
rather than open-ended, because the runtime's lock call itself gives up after about ten seconds; a caller that must
wait longer repeats the call.

A lock file is opened once as a ``LockFile``; the simple callers use ``lock()``.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import msvcrt
import os
from pathlib import Path
import stat

from .errors import PrivateFileError

_NOINHERIT = getattr(os, "O_NOINHERIT", 0)


def _mode(exclusive, wait):
    if exclusive:
        return msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK
    return msvcrt.LK_RLCK if wait else msvcrt.LK_NBRLCK


def _path(target, name):
    if isinstance(target, int):
        raise PrivateFileError("A folder passed as a descriptor has no Windows version")
    base = getattr(target, "path", target)
    return Path(base) / name


class LockFile:
    """An opened lock file: hold it exclusively or shared, waiting or failing at once."""

    def __init__(self, descriptor, identity, target, name):
        self.descriptor = descriptor
        self.identity = identity
        self._target, self._name = target, name

    def acquire(self, *, exclusive=True, wait=True):
        """Take the lock; ``BlockingIOError`` when ``wait`` is false and another holder has it."""
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(self.descriptor, _mode(exclusive, wait), 1)
        except OSError as error:
            raise BlockingIOError(errno.EWOULDBLOCK, os.strerror(errno.EWOULDBLOCK),
                                  str(_path(self._target, self._name))) from error

    def verify(self):
        """Refuse when the name no longer leads to the file this handle opened."""
        named = os.stat(_path(self._target, self._name), follow_symlinks=False)
        if (named.st_dev, named.st_ino) != self.identity:
            raise PrivateFileError("The lock file changed")

    def release(self):
        """Let go of the lock without closing the file."""
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        msvcrt.locking(self.descriptor, msvcrt.LK_UNLCK, 1)

    def close(self):
        """Close the file, which also releases any lock still held on it."""
        os.close(self.descriptor)


def open_descriptor(target, name, *, write=True):
    """Open a lock file and return its descriptor, with no other step."""
    path = _path(target, name)
    return os.open(path, _NOINHERIT | (os.O_RDWR if write else os.O_RDONLY))


def open_lock(target, name, *, create=True, write=True, mode=0o600, regular=False, verify=False, nonblock=False):
    """Open a lock file.

    The file is created when missing and ``create`` is set. ``write`` is false for a shared lock on an existing file.
    ``regular`` refuses anything but a regular file, where the caller checked that before; ``verify`` also refuses a
    name that no longer leads to the opened file. ``nonblock`` opens without waiting where the system has such a
    flag.
    """
    path = _path(target, name)
    flags = _NOINHERIT | (os.O_RDWR if write else os.O_RDONLY)
    if create:
        flags |= os.O_CREAT
    if nonblock:
        flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, mode)
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
    os.lseek(stream.fileno(), 0, os.SEEK_SET)
    try:
        msvcrt.locking(stream.fileno(), _mode(exclusive, wait), 1)
    except OSError as error:
        raise BlockingIOError(errno.EWOULDBLOCK, os.strerror(errno.EWOULDBLOCK), getattr(stream, "name", "")) from error


def release(stream):
    """Let go of the lock on a file the caller has already opened."""
    os.lseek(stream.fileno(), 0, os.SEEK_SET)
    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


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
    """Hold a lock on ``directory/name`` for the caller's block; ``BlockingIOError`` when ``wait`` is false."""
    handle = open_lock(directory, name, create=create, write=exclusive or create, mode=mode)
    try:
        handle.acquire(exclusive=exclusive, wait=wait)
        try:
            yield handle
        finally:
            handle.release()
    finally:
        handle.close()