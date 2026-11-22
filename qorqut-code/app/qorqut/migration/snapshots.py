"""Verified working-tree snapshots for rehearsal and coordinated data cutover."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tarfile
from typing import Any

from qorqut_operations.contracts import canonical_hash, freeze, plain
from .authority import AuthorityError


SCHEMA_VERSION = 1
IMPORTER_VERSION = "1"


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _relative(value: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise AuthorityError("snapshot_invalid", "Snapshot paths must be strings.")
    path = PurePosixPath(value)
    if (not value or "\\" in value or "\0" in value
            or path.is_absolute() or ".." in path.parts or path.as_posix() != value
            or value == "."):
        raise AuthorityError("snapshot_invalid", "Snapshot contains an invalid relative path.")
    return path


@dataclass(frozen=True)
class SourceManifest:
    data: Any

    def __post_init__(self):
        data = plain(self.data)
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                or data.get("schema_version") != SCHEMA_VERSION or data.get("importer_version") != IMPORTER_VERSION
                or not isinstance(data.get("source_root"), str) or not Path(data["source_root"]).is_absolute()
                or not isinstance(data.get("files"), list) or not isinstance(data.get("projects"), list)):
            raise AuthorityError("snapshot_invalid", "Unsupported source manifest.")
        names = set()
        for item in data["files"]:
            if not isinstance(item, dict):
                raise AuthorityError("snapshot_invalid", "Source manifest entries must be objects.")
            name = str(_relative(item.get("path")))
            if (name in names or type(item.get("size")) is not int or item["size"] < 0
                    or type(item.get("mode")) is not int or item["mode"] & ~0o777
                    or not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64
                    or any(char not in "0123456789abcdef" for char in item["sha256"])):
                raise AuthorityError("snapshot_invalid", "Invalid or duplicate source manifest entry.")
            names.add(name)
        projects = set()
        for project in data["projects"]:
            if not isinstance(project, dict) or not isinstance(project.get("slug"), str):
                raise AuthorityError("snapshot_invalid", "Invalid project discovery metadata.")
            slug = project["slug"]
            # The one recognized shape: the project's folder, ``projects/<slug>``.
            if _relative(project.get("root")).parts != ("projects", slug) or slug in projects:
                raise AuthorityError("snapshot_invalid", "Duplicate or invalid project discovery metadata.")
            projects.add(slug)
        data["files"].sort(key=lambda item: item["path"])
        object.__setattr__(self, "data", freeze(data))

    @property
    def fingerprint(self) -> str:
        return canonical_hash(self.data)

    @property
    def root(self) -> Path:
        return Path(self.data["source_root"])

    def to_dict(self) -> dict:
        return plain(self.data)


def _read_manifest(snapshot: Path) -> SourceManifest:
    try:
        if snapshot.is_symlink() or (snapshot / "manifest.json").is_symlink():
            raise ValueError
        return SourceManifest(json.loads((snapshot / "manifest.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AuthorityError("snapshot_invalid", "The saved source manifest is invalid.") from exc


def verify_snapshot(snapshot: str | Path) -> SourceManifest:
    snapshot = Path(snapshot)
    manifest = _read_manifest(snapshot)
    expected = {item["path"]: item for item in manifest.data["files"]}
    seen = set()
    archive = snapshot / "sources.tar"
    if archive.is_symlink():
        raise AuthorityError("snapshot_invalid", "Snapshot archive cannot be a symbolic link.")
    try:
        with tarfile.open(archive, mode="r:") as source:
            for member in source:
                name = str(_relative(member.name))
                entry = expected.get(name)
                if (name in seen or entry is None or not member.isfile()
                        or member.size != entry["size"] or member.mode != entry["mode"]):
                    raise AuthorityError("snapshot_invalid", "Snapshot entries do not match the source manifest.")
                stream = source.extractfile(member)
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                if digest.hexdigest() != entry["sha256"]:
                    raise AuthorityError("snapshot_invalid", "Snapshot content does not match its source hash.")
                seen.add(name)
        if seen != set(expected):
            raise AuthorityError("snapshot_invalid", "Snapshot is missing source files.")
    except (tarfile.TarError, OSError) as exc:
        raise AuthorityError("snapshot_invalid", "Snapshot archive cannot be verified.") from exc
    return manifest


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore_snapshot(snapshot: str | Path, destination: str | Path) -> SourceManifest:
    """Restore to an empty isolated directory, never merge over live project data."""
    snapshot, destination = Path(snapshot), Path(destination).absolute()
    manifest = verify_snapshot(snapshot)
    if destination.is_symlink() or destination.exists() and any(destination.iterdir()):
        raise AuthorityError("restore_not_empty", "Restore requires an empty separate directory.")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tarfile.open(snapshot / "sources.tar", mode="r:") as archive:
        for member in archive:
            target = destination / _relative(member.name)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise AuthorityError("snapshot_invalid", "Restore path escapes its destination.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                source = archive.extractfile(member)
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(target, member.mode & 0o777)
    for entry in manifest.data["files"]:
        if _digest(destination / entry["path"]) != entry["sha256"]:
            raise AuthorityError("snapshot_invalid", "Restored file does not match the source snapshot.")
    return manifest
