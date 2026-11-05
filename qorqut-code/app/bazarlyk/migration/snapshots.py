"""Verified working-tree snapshots for rehearsal and coordinated data cutover."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
from typing import Any

from qorqut_operations.contracts import canonical_bytes, canonical_hash, freeze, plain
from .authority import AuthorityError


SCHEMA_VERSION = 1
IMPORTER_VERSION = "1"
IGNORED_DIRECTORIES = frozenset({".git", ".bazarlyk", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"})
ROOT_ENTRIES = ("CLAUDE.md", "README.md", ".gitignore", ".gitattributes", "context", "learnings", "routines")


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


def _regular_files(directory: Path, top_skip=frozenset()):
    if directory.is_symlink():
        raise AuthorityError("source_symlink", "Migration sources require explicit disposition of symbolic links.")
    if directory.is_file():
        yield directory
        return
    if not directory.exists():
        return
    for parent, directories, files in os.walk(directory, followlinks=False):
        omitted = top_skip if Path(parent) == directory else frozenset()
        directories[:] = sorted(name for name in directories if name not in IGNORED_DIRECTORIES and name not in omitted)
        for name in [*directories, *sorted(files)]:
            path = Path(parent) / name
            if path.is_symlink():
                raise AuthorityError("source_symlink", "Migration sources require explicit disposition of symbolic links.")
            mode = path.stat().st_mode
            if stat.S_ISREG(mode):
                yield path
            elif not stat.S_ISDIR(mode):
                raise AuthorityError("source_special_file", "A migration source contains a nonregular file.")


def _git_head(directory: Path) -> str | None:
    if not (directory / ".git").exists():
        return None
    result = subprocess.run(["git", "-C", str(directory), "rev-parse", "--verify", "HEAD"],
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise AuthorityError("source_repository_invalid", "A source repository has no readable HEAD.")
    return result.stdout.strip()


@dataclass(frozen=True)
class SourceManifest:
    data: Any

    def __post_init__(self):
        data = plain(self.data)
        if (not isinstance(data, dict) or type(data.get("schema_version")) is not int
                or data.get("schema_version") != SCHEMA_VERSION or data.get("importer_version") != IMPORTER_VERSION
                or not isinstance(data.get("source_root"), str) or not Path(data["source_root"]).is_absolute()
                or not isinstance(data.get("files"), list) or not isinstance(data.get("businesses"), list)):
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
        businesses, shapes = set(), set()
        for business in data["businesses"]:
            if not isinstance(business, dict) or not isinstance(business.get("slug"), str):
                raise AuthorityError("snapshot_invalid", "Invalid business discovery metadata.")
            slug = business["slug"]
            hq = _relative(business.get("hq"))
            # Three recognized archive shapes: an offline v1 source archive (<slug>/hq), a previous-layout instance
            # archive (businesses/<slug>/hq) and a current target-instance archive (projects/<slug>/hq). None is
            # translated into another here, and one archive uses one shape. A previous-layout archive can be read and
            # fingerprinted; restoring it into the current layout requires an explicit conversion, never a fallback.
            shape = {(slug, "hq"): "source", ("businesses", slug, "hq"): "previous", ("projects", slug, "hq"): "target"}.get(hq.parts)
            if shape is None or slug in businesses:
                raise AuthorityError("snapshot_invalid", "Duplicate or invalid business discovery metadata.")
            businesses.add(slug)
            shapes.add(shape)
        if len(shapes) > 1:
            raise AuthorityError("snapshot_invalid", "Business discovery metadata mixes source and target path shapes.")
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


def inventory_sources(businesses_root: str | Path) -> SourceManifest:
    """Inspect only shared authored data and discovered business hq trees."""
    root = Path(businesses_root).resolve(strict=True)
    businesses, paths, repositories = [], [], {}
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        hq = entry / "hq"
        if not (hq / "business.yaml").is_file():
            continue
        if entry.is_symlink() or hq.is_symlink():
            raise AuthorityError("source_symlink", "Business discovery cannot follow a symbolic link.")
        businesses.append({"slug": entry.name, "hq": hq.relative_to(root).as_posix()})
        # hq/browser is the isolated browser's live profile: private runtime state, never migration input.
        paths.extend(_regular_files(hq, top_skip=frozenset({"browser"})))
        repositories[hq.relative_to(root).as_posix()] = _git_head(hq)
    for name in ROOT_ENTRIES:
        paths.extend(_regular_files(root / name))
    repositories["."] = _git_head(root)
    files = []
    for path in sorted(set(paths)):
        before = path.stat()
        digest = _digest(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise AuthorityError("source_changed", "A source changed while its inventory was being read.")
        files.append({"path": path.relative_to(root).as_posix(), "size": before.st_size,
                      "mode": stat.S_IMODE(before.st_mode) & 0o777, "sha256": digest})
    return SourceManifest({"schema_version": SCHEMA_VERSION, "importer_version": IMPORTER_VERSION,
        "source_root": str(root), "businesses": businesses, "files": files, "repositories": repositories})


def verify_sources(manifest: SourceManifest) -> None:
    if inventory_sources(manifest.root).fingerprint != manifest.fingerprint:
        raise AuthorityError("source_changed", "Sources changed since rehearsal; repeat the snapshot and reconciliation.")


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


def snapshot_sources(manifest: SourceManifest, destination: str | Path) -> dict:
    """Create a new private backup; callers must freeze writers for final cutover."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise AuthorityError("snapshot_exists", "Migration snapshots are immutable; use a new destination.")
    verify_sources(manifest)
    destination.mkdir(mode=0o700, parents=True)
    os.chmod(destination, 0o700)
    with (destination / "manifest.json").open("xb") as stream:
        os.chmod(stream.name, 0o600)
        stream.write(canonical_bytes(manifest.data) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    archive = destination / "sources.tar"
    with archive.open("xb") as stream:
        os.chmod(archive, 0o600)
        with tarfile.open(fileobj=stream, mode="w:", format=tarfile.PAX_FORMAT) as output:
            for entry in manifest.data["files"]:
                path = manifest.root / entry["path"]
                if path.is_symlink():
                    raise AuthorityError("source_changed", "A source became a symbolic link during snapshot.")
                info = tarfile.TarInfo(entry["path"])
                info.size, info.mode = entry["size"], entry["mode"]
                with path.open("rb") as content:
                    output.addfile(info, content)
        stream.flush()
        os.fsync(stream.fileno())
    verify_snapshot(destination)
    verify_sources(manifest)
    _fsync_directory(destination)
    _fsync_directory(destination.parent)
    return {"path": str(destination), "source_fingerprint": manifest.fingerprint,
            "archive_sha256": _digest(archive), "files": len(manifest.data["files"]),
            "bytes": sum(item["size"] for item in manifest.data["files"])}


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore_snapshot(snapshot: str | Path, destination: str | Path) -> SourceManifest:
    """Restore to an empty isolated directory, never merge over live business data."""
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
