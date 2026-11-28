"""Owner-private files on Unix: 0600 regular files in 0700 directories owned by this process user.

Symlinks, non-regular files and group/world-accessible secrets are rejected. Nothing here logs or returns a path
or a value to an agent; callers keep the bytes in memory only for as long as they need them.
"""

from __future__ import annotations

from contextlib import contextmanager


import os
import shutil
from pathlib import Path
import secrets
import stat
import sys

from .errors import PrivateFileError, UnsupportedSystem

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
# The flag that makes a rename refuse an existing name: Linux ``renameat2`` calls it RENAME_NOREPLACE, Darwin
# ``renameatx_np`` calls the same bit RENAME_EXCL.
RENAME_NOREPLACE = 1
RENAME_EXCL = 0x00000004


def _refuse(message):
    return PrivateFileError(message)


class Folder:
    """An opened folder: every later access is relative to it.

    The Unix version holds the folder's descriptor. Feature code passes this object from function to function and
    never the number, because the Windows version holds a path instead.
    """

    def __init__(self, descriptor):
        self.descriptor = descriptor

    def close(self):
        os.close(self.descriptor)


def _descriptor(folder):
    """The folder's descriptor: a Folder, or one already opened by another helper during the move."""
    return folder.descriptor if isinstance(folder, Folder) else folder


def _relative(folder, name):
    """``name`` and its folder descriptor, or the full path when the folder is one; for ``os.*`` calls."""
    if isinstance(folder, (str, os.PathLike)):
        return Path(folder) / name, None
    return name, _descriptor(folder)


def open_folder(folder, name=None, *, create=False, mode=DIRECTORY_MODE, follow_link=False):
    """Open a folder for relative access, creating it (with its parents) private when asked.

    ``name`` opens that folder inside an already opened one, so a caller walks a tree without building a path and
    without following a link. ``follow_link`` opens the folder named by its own path even when that path is a
    symbolic link, for a root the caller already holds; every name below it is still refused."""
    flags = os.O_RDONLY | os.O_DIRECTORY | (0 if follow_link else os.O_NOFOLLOW)
    if name is None:
        path = Path(folder)
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=mode)
        return Folder(os.open(path, flags))
    path, directory = _relative(folder, name)
    if create and directory is None:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
    return Folder(os.open(path, flags, dir_fd=directory))


def stat_folder(folder):
    """The identity of the open folder itself."""
    if isinstance(folder, (str, os.PathLike)):
        return os.stat(folder, follow_symlinks=False)
    return os.fstat(_descriptor(folder))


def list_entries(folder):
    """The names in the open folder."""
    return os.listdir(folder if isinstance(folder, (str, os.PathLike)) else _descriptor(folder))


def walk_entries(folder):
    """The names in the folder one at a time, so a caller that stops at its own bound does not list the rest."""
    scanned = os.scandir(folder if isinstance(folder, (str, os.PathLike)) else _descriptor(folder))
    with scanned:
        for entry in scanned:
            yield entry.name


def stat_entry(folder, name):
    """The identity of ``name`` inside the open folder, without following a final link."""
    path, directory = _relative(folder, name)
    return os.stat(path, dir_fd=directory, follow_symlinks=False)


def open_read(folder, name, *, nonblock=False):
    """Open a file inside the folder for reading, without following a final symlink.

    ``nonblock`` opens a pipe or a device without waiting for the other end, so a reader that accepts only a regular
    file never hangs on one it did not expect."""
    path, directory = _relative(folder, name)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if nonblock:
        flags |= getattr(os, "O_NONBLOCK", 0)
    return os.open(path, flags, dir_fd=directory)


def create_file(folder, name, *, mode=FILE_MODE):
    """Create a new file in the folder exclusively and return its open descriptor, for a caller that writes it in
    pieces; an existing file is never replaced."""
    path, directory = _relative(folder, name)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory)


def create_entry(folder, name, data, *, mode=FILE_MODE, sync=True):
    """Create a new file in the folder exclusively; an existing file is never replaced."""
    path, directory = _relative(folder, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            if sync:
                stream.flush()
                os.fsync(stream.fileno())
    finally:
        if descriptor is not None:
            os.close(descriptor)


def replace_entry(folder, name, data, *, mode=FILE_MODE, sync=True, prefix=None, suffix="", token_bytes=8,
                  chmod=True):
    """Write a new file beside ``name``, flush it, then swap it in atomically.

    A failure before the swap changes nothing. ``prefix`` starts the temporary name when given, so a caller can find
    and remove its own leftover files.
    """
    path, directory = _relative(folder, name)
    temporary = (prefix or "." + name + ".") + secrets.token_bytes(token_bytes).hex() + suffix
    source = temporary if directory is not None else path.parent / temporary
    try:
        descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode,
                             dir_fd=directory)
        try:
            if chmod:
                os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(data)
                if sync:
                    stream.flush()
                    os.fsync(stream.fileno())
        finally:
            if descriptor is not None:
                os.close(descriptor)
        os.replace(source, path, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(source, dir_fd=directory)
        except FileNotFoundError:
            pass


def ensure_entry(folder, name, *, mode=FILE_MODE):
    """Make sure a private file exists, keeping what a file that is there already holds."""
    path, directory = _relative(folder, name)
    os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, mode, dir_fd=directory))


def append_entry(folder, name, data, *, mode=FILE_MODE):
    """Append to a file in the folder, creating it private when it is missing."""
    path, directory = _relative(folder, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, mode, dir_fd=directory)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def remove_entry(folder, name, *, missing_ok=False):
    """Remove a file inside the folder, without following a symlink."""
    path, directory = _relative(folder, name)
    try:
        os.unlink(path, dir_fd=directory)
    except FileNotFoundError:
        if not missing_ok:
            raise


def sync_folder(folder):
    """Flush the folder's own entry changes to the disk."""
    if isinstance(folder, (str, os.PathLike)):
        fsync_directory(folder)
        return
    os.fsync(_descriptor(folder))


def is_mine(info):
    """Whether a stat belongs to the user this process runs as."""
    return info.st_uid == os.geteuid()


def is_private(info):
    """Whether a stat holds no group or other access."""
    return not info.st_mode & 0o077


def is_private_and_mine(info):
    """Whether a file belongs to this user and holds no group or other access."""
    return is_private(info) and is_mine(info)


def ownership_of(info):
    """The user and group numbers that own the file a stat describes, for a record kept about it."""
    return info.st_uid, info.st_gid


def mode_of(info, *, mask=0o777):
    """The permission bits of a stat, as ``set_private`` writes them, limited to ``mask``."""
    return stat.S_IMODE(info.st_mode) & mask


def owner_of(info):
    """The user number that owns a stat, as the system reports it."""
    return info.st_uid


def set_private(target, *, mode=FILE_MODE):
    """Remove group and other access from an open file descriptor or from a path."""
    if isinstance(target, int):
        os.fchmod(target, mode)
    else:
        os.chmod(target, mode)


@contextmanager
def private_umask(mode=0o077):
    """Make everything created inside the block private, whatever the caller's own mask is.

    For a caller that hands a path to another library (Git, the Context engine, the store), which then creates files
    this code never opens; the previous mask is put back however the block ends, as the call sites did by hand.
    """
    previous = os.umask(mode)
    try:
        yield
    finally:
        os.umask(previous)


def make_private_directory(path, *, mode=DIRECTORY_MODE, parents=False):
    """Create a folder when it is missing and make it private; an existing folder is kept and made private.

    ``parents`` also makes the folders above it that are missing, as the call sites that prepare a run folder did.
    Nothing is checked about the owner or the access of the folder that results: this is the plain creation the
    callers had before the move, not ``require_private_directory``."""
    path = Path(path)
    path.mkdir(mode=mode, parents=parents, exist_ok=True)
    os.chmod(path, mode)
    return path


def user_key():
    """A short, stable name for the user this process runs as; for a per-user file name."""
    return str(os.getuid())


def read_link(folder, name=None):
    """What a link points at, exactly as the system writes it; a missing link raises ``OSError``.

    ``name`` reads a link inside an opened folder, so a walk of a tree reads one without building a path."""
    if name is None:
        return os.readlink(folder)
    path, directory = _relative(folder, name)
    return os.readlink(path, dir_fd=directory)


def make_link(target, link, *, target_is_directory=False):
    """Make ``link`` point at ``target``; an existing link is never replaced."""
    os.symlink(target, link, target_is_directory=target_is_directory)


def require_private_directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute():
        raise _refuse("Private directories must be absolute paths")
    if create and not os.path.lexists(path):
        path.mkdir(mode=DIRECTORY_MODE, parents=True)
        os.chmod(path, DIRECTORY_MODE)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise _refuse("A private directory is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _refuse("A private directory must be a real directory, not a symlink")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise _refuse("A private directory must be owned by the service user with mode 0700")
    return path


def read_private_file(path, *, max_bytes=65536, directory_check=True):
    """Read a private regular file without following a final symlink; the parent must be private too."""
    path = Path(path)
    if not path.is_absolute():
        raise _refuse("Private files must be absolute paths")
    if directory_check:
        require_private_directory(path.parent)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise _refuse("A private file cannot be opened (missing or a symlink)") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _refuse("A private file must be a regular file")
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise _refuse("A private file must be owned by the service user with mode 0600")
        if info.st_size > max_bytes:
            raise _refuse("A private file is larger than its documented bound")
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            raise _refuse("A private file is larger than its documented bound")
        return data
    finally:
        os.close(fd)


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def open_private_file(folder, name, *, append=False):
    """Open (or create) a 0600 file in the folder for writing, without following a final symlink.

    The file is created when missing and cut back to nothing unless ``append`` keeps what is there; the caller closes
    the stream."""
    path, directory = _relative(folder, name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, FILE_MODE, dir_fd=directory), "ab" if append else "wb")


def write_private_file(directory, name, data):
    """Create a new 0600 file exclusively, fsync it and its directory. Existing files are never replaced."""
    directory = require_private_directory(directory)
    if os.sep in name or name in {"", ".", ".."}:
        raise _refuse("Invalid private file name")
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(directory / name)
        except OSError:
            pass
        raise
    os.close(fd)
    fsync_directory(directory)
    return directory / name


def open_child_folder(folder, name, *, create=False, mode=DIRECTORY_MODE, sync=True):
    """Open a folder inside the folder, creating it private when asked; a creation flushes the parent."""
    path, directory = _relative(folder, name)
    created = False
    if create:
        try:
            os.mkdir(path, mode, dir_fd=directory)
            created = True
        except FileExistsError:
            pass
    child = Folder(os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory))
    if created and sync:
        sync_folder(folder)
    return child


def make_folder_path(path, *, mode=DIRECTORY_MODE, parents=False):
    """Make the folder at ``path`` when it is missing, with the mode the caller asked for and no other step."""
    Path(path).mkdir(mode=mode, parents=parents, exist_ok=True)


def make_folder(folder, name, *, mode=DIRECTORY_MODE):
    """Make a folder inside the folder, refusing an existing name."""
    path, directory = _relative(folder, name)
    os.mkdir(path, mode, dir_fd=directory)


def remove_folder(folder, name, *, missing_ok=False):
    """Remove an empty folder inside the folder."""
    path, directory = _relative(folder, name)
    try:
        os.rmdir(path, dir_fd=directory)
    except FileNotFoundError:
        if not missing_ok:
            raise


def remove_tree(folder, name, *, ignore_errors=False):
    """Remove a folder inside the folder with everything it holds; the folder itself is not removed."""
    path, directory = _relative(folder, name)
    shutil.rmtree(path, ignore_errors=ignore_errors, dir_fd=directory)


def open_entry(folder, name, flags, *, mode=FILE_MODE):
    """Open a name inside the folder with the caller's own flags, without following a final symlink."""
    path, directory = _relative(folder, name)
    return os.open(path, flags | os.O_NOFOLLOW, mode, dir_fd=directory)


def rename_entry(folder, source, target):
    """Give the name ``source`` in the folder the new name ``target``; an existing target is replaced."""
    if isinstance(folder, (str, os.PathLike)):
        os.rename(Path(folder) / source, Path(folder) / target)
        return
    directory = _descriptor(folder)
    os.rename(source, target, src_dir_fd=directory, dst_dir_fd=directory)


def rename_entry_exclusive(folder, source, target, *, target_folder=None):
    """Give ``source`` in ``folder`` the new name ``target`` in ``target_folder``; an existing target is refused.

    The target folder is ``folder`` itself unless another one is given. Both folders must be open: the call that
    refuses to replace an existing name works on open folders only, so a path is refused here. A name that already
    exists raises ``FileExistsError``; anything else the system refuses during the rename is passed on as ``OSError``
    with the system's own code, and a host or filesystem that has no such call raises ``UnsupportedSystem``.
    """
    target_folder = folder if target_folder is None else target_folder
    if isinstance(folder, (str, os.PathLike)) or isinstance(target_folder, (str, os.PathLike)):
        raise UnsupportedSystem("A rename that never replaces needs both folders open")
    import ctypes
    name, flag = ("renameatx_np", RENAME_EXCL) if sys.platform == "darwin" else ("renameat2", RENAME_NOREPLACE)
    function = getattr(ctypes.CDLL(None, use_errno=True), name, None)
    if function is None:
        raise UnsupportedSystem("This host has no rename that refuses an existing name")
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(_descriptor(folder), os.fsencode(source), _descriptor(target_folder), os.fsencode(target), flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def link_entry(folder, source, target, *, target_folder=None, follow_symlinks=False):
    """Make ``source`` in ``folder`` appear under the name ``target`` in ``target_folder``.

    The target folder is ``folder`` itself unless another one is given. An existing target is refused, and the link
    is made on the entry itself, so a symbolic link is linked and never followed. ``follow_symlinks`` links what a
    symbolic ``source`` points at instead, for the callers that link a file they have just written themselves.
    """
    target_folder = folder if target_folder is None else target_folder
    if isinstance(folder, (str, os.PathLike)) and isinstance(target_folder, (str, os.PathLike)):
        os.link(Path(folder) / source, Path(target_folder) / target, follow_symlinks=follow_symlinks)
        return
    os.link(source, target, src_dir_fd=_descriptor(folder), dst_dir_fd=_descriptor(target_folder),
            follow_symlinks=follow_symlinks)
