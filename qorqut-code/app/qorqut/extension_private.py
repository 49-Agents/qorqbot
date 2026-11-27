"""One private folder per extension: ``<instance>/private/extensions/<id>/`` (contract section 10, rule 10).

An extension keeps its keys and its own data here, through the owner-private file rules of ``private_files``: 0700
directories and 0600 regular files owned by the service user, no symlinks. Constructing a ``PrivateFolder`` creates and
reads nothing, so a dormant extension leaves nothing on disk; the folders are made on the first ``write`` or
``folder(create=True)``.

A name is one plain segment. Nothing here logs, and no error names a file, a path or a value: an error raised here
carries a fixed message and no chained cause, so a traceback cannot show a path either.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import uuid

from qorqut_operations.contracts import DomainError

from .extensions import ASSET_NAME, EXTENSION_ID
from .private_files import fsync_directory, read_private_file, require_private_directory, write_private_file

# ``read_private_file`` always reads within a bound; this is its own default. ``write`` keeps to it, so whatever is
# saved here can be read back.
MAX_BYTES = 65536


def _refuse(message):
    return DomainError("invalid_configuration", message)


def _quietly(call, *args, **kwargs):
    """Run a private-file helper and raise its refusal again without the chained OS error (which names the path)."""
    try:
        return call(*args, **kwargs)
    except DomainError as problem:
        raise DomainError(problem.code, problem.message) from None
    except OSError:
        raise _refuse("An extension private file cannot be used") from None


class PrivateFolder:
    """The private folder of one extension."""

    def __init__(self, instance_root, extension_id):
        if not isinstance(extension_id, str) or not EXTENSION_ID.fullmatch(extension_id):
            raise _refuse("An extension id is a lowercase name")
        root = Path(instance_root)
        if not root.is_absolute():
            raise _refuse("The instance folder must be an absolute path")
        self.extension_id = extension_id
        self._chain = (root / "private", root / "private" / "extensions", root / "private" / "extensions" / extension_id)

    @property
    def path(self) -> Path:
        return self._chain[-1]

    @staticmethod
    def _name(name):
        # A plain name never starts with a dot, so it never meets the temporary files ``write`` uses.
        if not isinstance(name, str) or not ASSET_NAME.fullmatch(name):
            raise _refuse("An extension private file name is one plain name")
        return name

    def _present(self) -> bool:
        """Whether the folder exists; each folder on the way must then be private."""
        if not all(os.path.lexists(directory) for directory in self._chain):
            return False
        for directory in self._chain:
            _quietly(require_private_directory, directory)
        return True

    def _make(self) -> Path:
        for directory in self._chain:
            _quietly(require_private_directory, directory, create=True)
        return self.path

    def exists(self, name) -> bool:
        """Whether ``name`` is a regular file in the folder. One ``lstat``; nothing is read."""
        try:
            info = os.lstat(self.path / self._name(name))
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode)

    def read(self, name, *, max_bytes=MAX_BYTES) -> bytes | None:
        """The bytes of ``name``, or None when it (or the folder) is absent. A file that is not private is refused."""
        name = self._name(name)
        if not self._present() or not os.path.lexists(self.path / name):
            return None
        return _quietly(read_private_file, self.path / name, max_bytes=max_bytes)

    def write(self, name, data: bytes) -> None:
        """Replace ``name`` with ``data``: a new private temporary file, an atomic rename, the folder synced. Data larger
        than ``MAX_BYTES`` is refused before anything is written."""
        name = self._name(name)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise _refuse("Extension private data is bytes")
        if len(bytes(data)) > MAX_BYTES:
            raise _refuse("Extension private data is larger than a private file may be")
        directory = self._make()
        temporary = directory / f".{name}-{uuid.uuid4().hex}"
        try:
            _quietly(write_private_file, directory, temporary.name, bytes(data))
            _quietly(os.replace, temporary, directory / name)
            _quietly(fsync_directory, directory)
        finally:
            temporary.unlink(missing_ok=True)

    def remove(self, name) -> None:
        """Remove ``name``; nothing happens when it is absent. A folder is not removed here."""
        name = self._name(name)
        if not self._present() or not os.path.lexists(self.path / name):
            return
        if stat.S_ISDIR(_quietly(os.lstat, self.path / name).st_mode):
            raise _refuse("An extension private folder is not removed as a file")
        _quietly(os.unlink, self.path / name)
        _quietly(fsync_directory, self.path)

    def folder(self, name, *, create=False) -> Path:
        """A private sub-folder for a helper's data. Without ``create`` it must already exist and be private."""
        name = self._name(name)
        if create:
            self._make()
        elif not self._present():
            raise _refuse("An extension private folder is missing")
        return _quietly(require_private_directory, self.path / name, create=create)
