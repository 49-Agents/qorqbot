"""Small Linux dirfd boundary: all filesystem access stays under explicit roots."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path, PurePosixPath
import stat
from uuid import uuid4

from .types import AdapterError

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
GENERATED_FILE_LIMIT = 1024 * 1024


def validate_generated_size(content: bytes):
    if len(content) > GENERATED_FILE_LIMIT:
        raise AdapterError("Generated output exceeds the subsequent read limit")


class ManagedTree:
    def __init__(self, root: Path):
        if not root.is_absolute() or ".." in root.parts:
            raise AdapterError("Managed root must be absolute")
        self.root = root
        fd = os.open("/", DIRECTORY)
        try:
            for part in root.parts[1:]:
                child = os.open(part, DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        os.close(self.fd)

    @property
    def identity(self) -> tuple[int, int]:
        info = os.fstat(self.fd)
        return info.st_dev, info.st_ino

    @contextmanager
    def parent(self, relative: str, *, create=False):
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise AdapterError("Expected a managed relative path")
        fd = os.dup(self.fd)
        try:
            for part in path.parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                child = os.open(part, DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, path.name
        finally:
            os.close(fd)

    @contextmanager
    def open_read(self, relative: str):
        with self.parent(relative) as (fd, name):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise AdapterError("Managed source must be a regular file")
            stream = os.fdopen(file_fd, "rb")
        except BaseException:
            os.close(file_fd)
            raise
        with stream:
            yield stream

    def read(self, relative: str, limit=GENERATED_FILE_LIMIT) -> bytes | None:
        try:
            with self.open_read(relative) as stream:
                content = stream.read(limit + 1)
        except FileNotFoundError:
            return None
        if len(content) > limit:
            raise AdapterError("Generated configuration exceeds read limit")
        return content

    def atomic_write(self, relative: str, content: bytes):
        validate_generated_size(content)
        with self.parent(relative, create=True) as (fd, name):
            tmp = ".adapter-" + uuid4().hex
            out = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                with os.fdopen(out, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Replacement never follows a symlink; caller checked authored bytes under lock.
                os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                try:
                    os.unlink(tmp, dir_fd=fd)
                except FileNotFoundError:
                    pass

    @contextmanager
    def lock(self, relative: str):
        with self.parent(relative, create=True) as (fd, name):
            lock = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise AdapterError("Lock must be a regular file")
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock)

    def files(self, relative: str):
        """Yield regular files only; never follow a directory or file symlink."""
        def visit(fd, prefix):
            for name in sorted(os.listdir(fd)):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                path = prefix + "/" + name
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, DIRECTORY, dir_fd=fd)
                    try:
                        yield from visit(child, path)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    yield path
                elif stat.S_ISLNK(info.st_mode):
                    raise AdapterError("Symlink in managed source tree: " + path)

        try:
            with self.parent(relative) as (fd, name):
                directory = os.open(name, DIRECTORY, dir_fd=fd)
        except FileNotFoundError:
            return
        try:
            yield from visit(directory, relative)
        finally:
            os.close(directory)
