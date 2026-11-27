"""Bounded owner uploads, copied once into the selected agent's downloads.

The request key owns one directory. A sealed receipt makes retries reuse the
same bytes; filenames never select a directory or overwrite another upload.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from qorqut_operations.contracts import DomainError
from qorqut_system import locks, private_files
from qorqut.file_viewer.paths import escape_label, viewer_url

MAX_BYTES = 25 * 1024 * 1024
KEY = re.compile(r"[A-Za-z0-9._:-]{8,120}")
READ = os.O_RDONLY | os.O_NONBLOCK


def validate(key, name, data):
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise DomainError("validation_error", "A stable upload key is required")
    if (not isinstance(name, str) or name in ("", ".", "..") or len(name.encode("utf-8")) > 255
            or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name)):
        raise DomainError("validation_error", "Choose a file with a valid filename")
    if len(data) > MAX_BYTES:
        raise DomainError("payload_too_large", "Files must be 25 MB or smaller")


@contextmanager
def directory(root, workspace, identity):
    root, workspace = Path(root), Path(workspace)
    try:
        parts = workspace.relative_to(root).parts
    except ValueError:
        raise DomainError("forbidden", "The agent workspace is outside this instance") from None
    if not parts or any(p in (".", "..") for p in parts):
        raise DomainError("forbidden", "Invalid agent workspace")
    folder = None
    try:
        folder = private_files.open_folder(root)
        for part in (*parts, "downloads", identity):
            child = private_files.open_child_folder(folder, part, create=True, sync=False)
            folder.close()
            folder = child
        yield folder
    except OSError as error:
        raise DomainError("file_unavailable", "The file could not be saved in the agent's downloads") from error
    finally:
        if folder is not None:
            folder.close()


def read(folder, name, limit):
    try:
        fd = private_files.open_entry(folder, name, READ)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise DomainError("file_unavailable", "The saved attachment is unavailable")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise DomainError("file_unavailable", "The saved attachment is unavailable")
        return value
    finally:
        os.close(fd)


def create(folder, name, data):
    temporary = ".incoming-" + uuid.uuid4().hex
    fd = private_files.open_entry(folder, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # Link is exclusive: an existing file is never overwritten, including
        # one left after a crash between saving the bytes and the receipt.
        private_files.link_entry(folder, temporary, name)
        private_files.sync_folder(folder)
    finally:
        private_files.remove_entry(folder, temporary)


def save(root, profile, principal_id, binding, key, name, data):
    validate(key, name, data)
    identity = uuid.uuid5(uuid.NAMESPACE_URL, json.dumps([principal_id, profile["agent_id"], key])).hex
    # Keep ordinary filenames. Escape the few characters that interfere with
    # local Markdown targets or the existing viewer's path normalization.
    filename = re.sub(r"[%<>]", "_", name)
    if filename in (".receipt.json", ".upload.lock"):
        filename = "file-" + filename
    digest = hashlib.sha256(data).hexdigest()
    expected = {"agent_id": profile["agent_id"], "principal_id": principal_id, "binding": binding,
                "name": name, "filename": filename, "sha256": digest, "size": len(data)}
    with directory(root, profile["workspace"], identity) as parent:
        lock = locks.open_lock(parent, ".upload.lock", nonblock=True)
        try:
            if not stat.S_ISREG(os.fstat(lock.descriptor).st_mode):
                raise DomainError("file_unavailable", "The attachment is unavailable")
            try:
                lock.acquire(exclusive=True, wait=False)
            except BlockingIOError:
                raise DomainError("busy", "This file is still being saved; retry shortly", retryable=True) from None
            receipt = read(parent, ".receipt.json", 16384)
            if receipt is not None and json.loads(receipt) != expected:
                raise DomainError("idempotency_conflict", "This upload key already belongs to another file")
            saved = read(parent, filename, MAX_BYTES)
            if saved is not None and hashlib.sha256(saved).hexdigest() != digest:
                raise DomainError("idempotency_conflict", "The saved attachment has changed")
            if saved is None:
                if receipt is not None:
                    raise DomainError("file_unavailable", "The saved attachment was moved or deleted")
                create(parent, filename, data)
            if receipt is None:
                create(parent, ".receipt.json", json.dumps(expected, sort_keys=True).encode())
        finally:
            lock.close()
    path = Path(profile["workspace"]) / "downloads" / identity / filename
    relative = path.relative_to(root).as_posix()
    return {"id": identity, "name": name, "size": len(data), "path": str(path), "url": viewer_url(path=relative),
            "reference": f"[{escape_label(name)}](<{path}>)"}
