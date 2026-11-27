"""Owner-private files on Windows: a first version, not shipped and not hardened.

Windows has no mode bits. This version refuses symbolic links and junctions, keeps private data inside the current
user's own profile folder, and compares the file it opened with the file it checked before opening. Reading the
file's own access rules is left to the Windows step of the work; nothing here runs in a delivered installation.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
import shutil
from pathlib import Path
import secrets
import stat

from .errors import PrivateFileError, UnsupportedSystem

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_NOINHERIT = getattr(os, "O_NOINHERIT", 0)
_BINARY = getattr(os, "O_BINARY", 0)


def _refuse(message):
    return PrivateFileError(message)


class Folder:
    """An opened folder: every later access is relative to it.

    Windows has no calls relative to an open folder, so this version holds the folder's path. Feature code passes the
    object from function to function and never a descriptor.
    """

    def __init__(self, path):
        self.path = Path(path)

    def close(self):
        pass


def _path(folder, name=None):
    if isinstance(folder, int):
        raise UnsupportedSystem("A folder passed as a descriptor has no Windows version")
    base = folder.path if isinstance(folder, Folder) else Path(folder)
    return base / name if name is not None else base


def open_folder(folder, name=None, *, create=False, mode=DIRECTORY_MODE, follow_link=False):
    """Open a folder for relative access, creating it (with its parents) private when asked.

    ``name`` opens that folder inside an already opened one, so a caller walks a tree without building a path and
    without following a link. ``follow_link`` opens the folder named by its own path even when that path is a
    symbolic link, for a root the caller already holds; every name below it is still refused."""
    path = _path(folder, name)
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
    info = os.stat(path) if follow_link else os.lstat(path)
    if (not follow_link and _is_link(info)) or not stat.S_ISDIR(info.st_mode):
        raise _refuse("A private directory must be a real directory, not a symlink")
    return Folder(path)


def stat_folder(folder):
    """The identity of the open folder itself."""
    return os.lstat(_path(folder))


def list_entries(folder):
    """The names in the open folder."""
    return os.listdir(_path(folder))


def walk_entries(folder):
    """The names in the folder one at a time, so a caller that stops at its own bound does not list the rest."""
    scanned = os.scandir(_path(folder))
    with scanned:
        for entry in scanned:
            yield entry.name


def stat_entry(folder, name):
    """The identity of ``name`` inside the open folder, without following a final link."""
    return os.lstat(_path(folder, name))


def open_read(folder, name, *, nonblock=False):
    """Open a file inside the folder for reading.

    ``nonblock`` has no Windows form: the version here has no non-blocking read of a pipe to offer, so it opens the
    file it checked."""
    path = _path(folder, name)
    named = os.lstat(path)
    if _is_link(named) or not stat.S_ISREG(named.st_mode):
        raise _refuse("A private file must be a regular file")
    return os.open(path, os.O_RDONLY | _NOINHERIT | _BINARY)


def create_file(folder, name, *, mode=FILE_MODE):
    """Create a new file in the folder exclusively and return its open descriptor, for a caller that writes it in
    pieces; an existing file is never replaced."""
    path = _path(folder, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOINHERIT | _BINARY, mode)
    if os.name == "nt":                       # no ``O_NOFOLLOW``: refuse a link that appeared between the checks
        named = os.lstat(path)
        if _is_link(named):
            os.close(descriptor)
            raise _refuse("A private file must be a regular file")
    return descriptor


def create_entry(folder, name, data, *, mode=FILE_MODE, sync=True):
    """Create a new file in the folder exclusively; an existing file is never replaced."""
    path = _path(folder, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOINHERIT | _BINARY, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        if sync:
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replace_entry(folder, name, data, *, mode=FILE_MODE, sync=True, prefix=None, suffix="", token_bytes=8,
                  chmod=True):
    """Write a new file beside ``name``, flush it, then swap it in atomically."""
    path = _path(folder, name)
    temporary = path.parent / ((prefix or "." + name + ".") + secrets.token_hex(8))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOINHERIT | _BINARY, mode)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
            if sync:
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def ensure_entry(folder, name, *, mode=FILE_MODE):
    """Make sure a private file exists, keeping what a file that is there already holds."""
    path = _path(folder, name)
    try:
        named = os.lstat(path)
    except FileNotFoundError:
        named = None
    if named is not None and _is_link(named):
        raise _refuse("A private file must be a real file, not a symlink")
    os.close(os.open(path, os.O_RDWR | os.O_CREAT | _NOINHERIT | _BINARY, mode))


def append_entry(folder, name, data, *, mode=FILE_MODE):
    """Append to a file in the folder, creating it private when it is missing."""
    descriptor = os.open(_path(folder, name), os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOINHERIT | _BINARY, mode)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def remove_entry(folder, name, *, missing_ok=False):
    """Remove a file inside the folder."""
    try:
        _path(folder, name).unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise


def sync_folder(folder):
    """Windows does not flush a folder; a created file is flushed through its own handle."""
    if not _path(folder).is_dir():
        raise _refuse("A private directory is missing")


def is_mine(info):
    """Whether a stat belongs to the user this process runs as; its access rules are read in the Windows step."""
    return True


def is_private(info):
    """Whether a stat holds no group or other access; the first version can only refuse links."""
    return not _is_link(info)


def is_private_and_mine(info):
    """Whether a file holds no access outside this user; the first version can only refuse links."""
    return is_private(info) and is_mine(info)


def ownership_of(info):
    """The user and group numbers that own the file a stat describes; a Windows stat names none, so both are zero."""
    return getattr(info, "st_uid", 0), getattr(info, "st_gid", 0)


def mode_of(info, *, mask=0o777):
    """The permission bits of a stat, as ``set_private`` writes them, limited to ``mask``."""
    return stat.S_IMODE(info.st_mode) & mask


def owner_of(info):
    """The user number that owns a stat; the first version reports what its own stats carry, because reading an
    object's security settings is left to the Windows step."""
    return info.st_uid


def set_private(target, *, mode=FILE_MODE):
    """Remove group and other access from a path; descriptor access rules are set in the Windows step."""
    if isinstance(target, int):
        return
    os.chmod(target, mode)


@contextmanager
def private_umask(mode=0o077):
    """Windows has no mask: what the block creates takes its access rules from its folder, set in the Windows step."""
    yield


def make_private_directory(path, *, mode=DIRECTORY_MODE, parents=False):
    """Create a folder when it is missing and make it private; an existing folder is kept and made private.

    ``parents`` also makes the folders above it that are missing, as the call sites that prepare a run folder did.
    A Windows folder has no mode bits; the access rules are set in the Windows step."""
    path = Path(path)
    path.mkdir(mode=mode, parents=parents, exist_ok=True)
    return path


def user_key():
    """A short, stable name for the user this process runs as; for a per-user file name."""
    return os.environ.get("USERNAME") or os.environ.get("USERPROFILE") or "user"


def _inside_profile(path):
    profile = os.environ.get("USERPROFILE")
    if not profile:
        raise _refuse("A private directory must sit inside the user's profile folder")
    try:
        Path(path).relative_to(profile)
    except ValueError:
        raise _refuse("A private directory must sit inside the user's profile folder") from None


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def _same_object(before, after):
    return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def read_link(folder, name=None):
    """What a link points at, exactly as the system writes it; a missing link raises ``OSError``.

    ``name`` reads a link inside an opened folder, so a walk of a tree reads one without building a path."""
    return os.readlink(_path(folder, name))


def make_link(target, link, *, target_is_directory=False):
    """Make ``link`` point at ``target``; an existing link is never replaced."""
    os.symlink(target, link, target_is_directory=target_is_directory)


def require_private_directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute():
        raise _refuse("Private directories must be absolute paths")
    if create and not os.path.lexists(path):
        path.mkdir(mode=DIRECTORY_MODE, parents=True)
    info = os.lstat(path)
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise _refuse("A private directory must be a real directory, not a symlink")
    _inside_profile(path)
    return path


def read_private_file(path, *, max_bytes=65536, directory_check=True):
    """Read a private regular file; the parent must be private too."""
    path = Path(path)
    if not path.is_absolute():
        raise _refuse("Private files must be absolute paths")
    if directory_check:
        require_private_directory(path.parent)
    try:
        named = os.lstat(path)
    except OSError as exc:
        raise _refuse("A private file cannot be opened (missing or a symlink)") from exc
    if _is_link(named) or not stat.S_ISREG(named.st_mode):
        raise _refuse("A private file must be a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | _NOINHERIT | _BINARY)
    except OSError as exc:
        raise _refuse("A private file cannot be opened (missing or a symlink)") from exc
    try:
        info = os.fstat(fd)
        if not _same_object(named, info):
            raise _refuse("A private file changed while it was opened")
        if not stat.S_ISREG(info.st_mode):
            raise _refuse("A private file must be a regular file")
        if info.st_size > max_bytes:
            raise _refuse("A private file is larger than its documented bound")
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            raise _refuse("A private file is larger than its documented bound")
        return data
    finally:
        os.close(fd)


def fsync_directory(path):
    # Windows does not flush a folder. A created file is flushed through its own handle before it is renamed.
    if not Path(path).is_dir():
        raise _refuse("A private directory is missing")


def open_private_file(folder, name, *, append=False):
    """Open (or create) a private file in the folder for writing; the caller closes the stream. A first version."""
    path = _path(folder, name)
    try:
        named = os.lstat(path)
    except FileNotFoundError:
        named = None
    except OSError as exc:
        raise _refuse("A private file cannot be opened (missing or a symlink)") from exc
    if named is not None and (_is_link(named) or not stat.S_ISREG(named.st_mode)):
        raise _refuse("A private file must be a regular file")
    flags = os.O_WRONLY | os.O_CREAT | _NOINHERIT | _BINARY | (os.O_APPEND if append else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, FILE_MODE), "ab" if append else "wb")


def write_private_file(directory, name, data):
    """Create a new file exclusively, flush it and its folder. Existing files are never replaced."""
    directory = require_private_directory(directory)
    if os.sep in name or name in {"", ".", ".."}:
        raise _refuse("Invalid private file name")
    target = directory / name
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOINHERIT | _BINARY)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    os.close(fd)
    fsync_directory(directory)
    return target


def open_child_folder(folder, name, *, create=False, mode=DIRECTORY_MODE, sync=True):
    """Open a folder inside the folder, creating it private when asked."""
    path = _path(folder, name)
    if create:
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            pass
    info = os.lstat(path)
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise _refuse("A private directory must be a real directory, not a symlink")
    return Folder(path)


def make_folder_path(path, *, mode=DIRECTORY_MODE, parents=False):
    """Make the folder at ``path`` when it is missing, with the mode the caller asked for and no other step."""
    Path(path).mkdir(mode=mode, parents=parents, exist_ok=True)


def make_folder(folder, name, *, mode=DIRECTORY_MODE):
    """Make a folder inside the folder, refusing an existing name."""
    _path(folder, name).mkdir(mode=mode)


def remove_folder(folder, name, *, missing_ok=False):
    """Remove an empty folder inside the folder."""
    try:
        _path(folder, name).rmdir()
    except FileNotFoundError:
        if not missing_ok:
            raise


def remove_tree(folder, name, *, ignore_errors=False):
    """Remove a folder inside the folder with everything it holds; the folder itself is not removed."""
    shutil.rmtree(_path(folder, name), ignore_errors=ignore_errors)


def open_entry(folder, name, flags, *, mode=FILE_MODE):
    """Open a name inside the folder with the caller's own flags, refusing a final link or junction."""
    path = _path(folder, name)
    try:
        named = os.lstat(path)
    except FileNotFoundError:
        named = None
    if named is not None and _is_link(named):
        raise _refuse("A private file must be a real file, not a symlink")
    return os.open(path, flags | _NOINHERIT | _BINARY, mode)


def rename_entry(folder, source, target):
    """Give the name ``source`` in the folder the new name ``target``; an existing target is replaced."""
    os.replace(_path(folder, source), _path(folder, target))


def rename_entry_exclusive(folder, source, target, *, target_folder=None):
    """Give ``source`` in ``folder`` the new name ``target`` in ``target_folder``; an existing target is refused.

    Windows never replaces an existing name when it renames, so the first version needs nothing beyond ``os.rename``.
    """
    target_folder = folder if target_folder is None else target_folder
    os.rename(_path(folder, source), _path(target_folder, target))


def link_entry(folder, source, target, *, target_folder=None, follow_symlinks=False):
    """Make ``source`` in ``folder`` appear under the name ``target`` in ``target_folder``.

    The target folder is ``folder`` itself unless another one is given. An existing target is refused.
    ``follow_symlinks`` is accepted so the shapes match; the first Windows version links the entry it is given.
    """
    target_folder = folder if target_folder is None else target_folder
    os.link(_path(folder, source), _path(target_folder, target))
