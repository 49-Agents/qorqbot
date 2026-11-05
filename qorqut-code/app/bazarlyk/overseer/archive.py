"""Private immutable source snapshots and separately authorized Overseer retention."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from qorqut_operations.contracts import DomainError


def _bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DomainError("validation_error", "Material must be JSON serializable") from exc


def _digest(data):
    return hashlib.sha256(data).hexdigest()


class PrivateArchive:
    """A configured owner-private root, source gate and optional retention gate."""
    def __init__(self, root, authorize, authorize_retention=None):
        self.root = Path(root)
        if not self.root.is_absolute() or ".." in self.root.parts or os.path.realpath(self.root) != str(self.root):
            raise DomainError("invalid_configuration", "Archive root must be an explicit absolute non-symlink path")
        if not callable(authorize):
            raise TypeError("Current access validation is required")
        try:
            info = self.root.stat()
        except OSError as exc:
            raise DomainError("invalid_configuration", "Private archive root is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise DomainError("invalid_configuration", "Private archive root must be owner-private")
        self.authorize, self.authorize_retention = authorize, authorize_retention

    def _path(self, key, suffix=".json"):
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise DomainError("validation_error", "Invalid archive reference")
        return self.root / (key + suffix)

    @staticmethod
    def _run_ref(run_ref):
        if not isinstance(run_ref, dict) or set(run_ref) != {"provider", "agent_id", "conversation_id", "run_id", "namespace"}:
            raise DomainError("validation_error", "A registered Overseer conversation reference is required")
        namespace = run_ref["namespace"]
        if (run_ref["provider"] != "qorqut_overseer" or not isinstance(run_ref["agent_id"], str) or not run_ref["agent_id"]
                or not isinstance(run_ref["conversation_id"], str) or not run_ref["conversation_id"]
                or run_ref["run_id"] is not None and (not isinstance(run_ref["run_id"], str) or not run_ref["run_id"])
                or not isinstance(namespace, dict) or set(namespace) != {"kind", "business_id"}
                or not (namespace["kind"] == "global" and namespace["business_id"] is None
                        or namespace["kind"] == "business" and isinstance(namespace["business_id"], str) and namespace["business_id"])):
            raise DomainError("validation_error", "Invalid Overseer conversation reference")

    def _retention(self, run_ref, action):
        self._run_ref(run_ref)
        if not callable(self.authorize_retention):
            raise DomainError("forbidden", "Current Overseer retention authority is required")
        self.authorize_retention(run_ref, action=action)

    def capture(self, source_ref, records, source_refs, operation_id):
        """Capture a source; this path is never used for Overseer discussion."""
        if not isinstance(source_ref, dict) or not isinstance(records, list) or not isinstance(source_refs, list):
            raise DomainError("validation_error", "Source records and references are required")
        self.authorize(source_ref)
        for ref in source_refs:
            self.authorize(ref)
        return self._put({"schema_version": 1, "kind": "source", "run_ref": source_ref,
                          "source_refs": source_refs}, records, operation_id)

    def capture_retention(self, run_ref, records, source_refs, operation_id):
        if not isinstance(records, list) or not isinstance(source_refs, list):
            raise DomainError("validation_error", "Presentation records and source references are required")
        self._retention(run_ref, "capture")
        for ref in source_refs:
            self.authorize(ref)
        return self._put({"schema_version": 1, "kind": "retention", "purpose": "presentation", "overseer_origin": True,
                          "run_ref": run_ref, "source_refs": source_refs}, records, operation_id)

    def _write_new(self, path, data):
        if os.path.lexists(path):
            if self._read(path) != data:
                raise DomainError("operation_conflict", "Archive operation was reused with different material")
            return
        fd, temporary = tempfile.mkstemp(prefix=".capture-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _put(self, manifest, records, operation_id):
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 200:
            raise DomainError("validation_error", "A bounded operation ID is required")
        body = _bytes(records)
        manifest = {**manifest, "body_hash": _digest(body)}
        data = _bytes(manifest)
        key = _digest(operation_id.encode("utf-8"))
        lock_path = self.root / ".lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(lock_path, flags, 0o600), "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            # If interrupted after body but before the manifest, an exact retry
            # finishes the immutable publication; a conflicting retry refuses.
            self._write_new(self._path(key, ".body"), body)
            self._write_new(self._path(key), data)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return {"provider": "overseer_archive", "id": key, "content_hash": _digest(data)}

    @staticmethod
    def _read(path):
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                return stream.read()
        except OSError as exc:
            raise DomainError("evidence_unavailable", "Archived material is unavailable") from exc

    def _load(self, reference):
        if not isinstance(reference, dict) or set(reference) != {"provider", "id", "content_hash"} or reference.get("provider") != "overseer_archive":
            raise DomainError("validation_error", "Invalid archive reference")
        data = self._read(self._path(reference.get("id")))
        if _digest(data) != reference.get("content_hash"):
            raise DomainError("evidence_unavailable", "Archived material failed integrity verification")
        try:
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError("manifest")
            return value
        except (ValueError, TypeError) as exc:
            raise DomainError("evidence_unavailable", "Archived material is invalid") from exc

    def _body(self, reference, manifest):
        data = self._read(self._path(reference["id"], ".body"))
        if _digest(data) != manifest.get("body_hash"):
            raise DomainError("evidence_unavailable", "Archived material failed integrity verification")
        try:
            records = json.loads(data)
            if not isinstance(records, list):
                raise ValueError("body")
            return {**manifest, "records": records}
        except (ValueError, TypeError) as exc:
            raise DomainError("evidence_unavailable", "Archived material is invalid") from exc

    def get(self, reference):
        manifest = self._load(reference)
        if manifest.get("kind") != "source":
            raise DomainError("forbidden", "Retained Overseer material is not extraction evidence")
        try:
            self.authorize(manifest["run_ref"])
            for ref in manifest["source_refs"]:
                self.authorize(ref)
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError("evidence_unavailable", "Archived material is invalid") from exc
        return self._body(reference, manifest)

    def get_retention(self, reference):
        manifest = self._load(reference)
        if manifest.get("kind") != "retention":
            raise DomainError("evidence_unavailable", "Retained discussion is unavailable")
        try:
            self._retention(manifest["run_ref"], "read")
            for ref in manifest["source_refs"]:
                self.authorize(ref)
        except (ValueError, KeyError, TypeError) as exc:
            raise DomainError("evidence_unavailable", "Archived material is invalid") from exc
        return self._body(reference, manifest)
