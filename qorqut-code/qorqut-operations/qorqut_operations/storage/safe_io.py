"""Host file effects anchored to directory descriptors, without symlink traversal.

Business processes can rename their writable directories concurrently. A path
check followed by ordinary path I/O cannot enforce a host filesystem boundary.
Walk every component with O_NOFOLLOW and keep the parent descriptor through the
effect, including temporary-file creation, rename, cleanup and directory fsync.
"""

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat

from qorqut_operations.contracts import DomainError

_ANY = object()
# Interrupted replacements leave files named with this prefix. It is the retained deployed-v1 on-disk contract
# (v1 recovery sweeps match it), so it stays byte-for-byte; a recovery sweep imports this constant rather than
# repeating the literal.
TEMPORARY_PREFIX = ".bazarlyk-"


def _parts(path):
    path = Path(path).absolute()
    if ".." in path.parts or "\0" in str(path):
        raise DomainError("forbidden", "File path contains an invalid directory component")
    return path.parts[1:]


@contextmanager
def directory(path, *, create=False):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in _parts(path):
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise DomainError("forbidden", "File path crosses a symbolic link or non-directory") from None
        raise
    finally:
        os.close(descriptor)


@contextmanager
def parent(path, *, create=False):
    path = Path(path).absolute()
    if not _parts(path):
        raise DomainError("forbidden", "An ordinary file path is required")
    with directory(path.parent, create=create) as descriptor:
        yield descriptor, path.name


@contextmanager
def _file(descriptor, name, *, private=False):
    try:
        file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=descriptor)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise DomainError("forbidden", "Managed target is a symbolic link") from None
        raise
    with os.fdopen(file, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise DomainError("forbidden", "Managed target is not an ordinary file")
        if private and info.st_mode & 0o077:
            raise DomainError("forbidden", "Private file must have mode 0600")
        yield stream


def _read(descriptor, name, *, limit=None, private=False):
    with _file(descriptor, name, private=private) as stream:
        content = stream.read(-1 if limit is None else limit + 1)
        if limit is not None and len(content) > limit:
            raise DomainError("invalid_configuration", "Managed file exceeds its size limit")
        return content


def read_bytes(path, *, limit=None, private=False):
    with parent(path) as (descriptor, name):
        return _read(descriptor, name, limit=limit, private=private)


def _existing(descriptor, name):
    try:
        return _read(descriptor, name)
    except FileNotFoundError:
        return None


def _hash(content):
    return None if content is None else hashlib.sha256(content).hexdigest()


def file_hash(path):
    try:
        with parent(path) as (descriptor, name):
            with _file(descriptor, name) as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def atomic_write(path, content, *, mode=0o600, expected_hash=_ANY, enforce_mode=False):
    with parent(path, create=True) as (descriptor, name):
        before = _existing(descriptor, name)
        if expected_hash is not _ANY and _hash(before) != expected_hash:
            raise DomainError("revision_conflict", "Managed file changed before replacement")
        if before == content and (not enforce_mode or
                stat.S_IMODE(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode) == mode):
            return False
        temporary = TEMPORARY_PREFIX + secrets.token_hex(16)
        file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                       mode, dir_fd=descriptor)
        try:
            with os.fdopen(file, "wb") as stream:
                stream.write(content)
                os.fchmod(stream.fileno(), mode)
                stream.flush()
                os.fsync(stream.fileno())
            if expected_hash is not _ANY and _hash(_existing(descriptor, name)) != expected_hash:
                raise DomainError("revision_conflict", "Managed file changed while preparing replacement")
            # Both names resolve against the same retained directory, even if
            # its former path has been renamed and replaced with a symlink.
            os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            os.fsync(descriptor)
        finally:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
    return True


def unlink(path, *, expected_hash=_ANY, missing_ok=False):
    try:
        with parent(path) as (descriptor, name):
            if expected_hash is not _ANY and _hash(_existing(descriptor, name)) != expected_hash:
                raise DomainError("revision_conflict", "Managed file changed before deletion")
            os.unlink(name, dir_fd=descriptor)
            os.fsync(descriptor)
    except FileNotFoundError:
        if not missing_ok:
            raise


def mkdir(path):
    with directory(path, create=True):
        pass


def remove_empty_tree(path):
    """Remove only empty managed directories; never follow links or delete files."""
    def clear(descriptor):
        for name in os.listdir(descriptor):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=descriptor)
            try:
                clear(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)

    with directory(path) as descriptor:
        clear(descriptor)
    with parent(path) as (descriptor, name):
        os.rmdir(name, dir_fd=descriptor)
