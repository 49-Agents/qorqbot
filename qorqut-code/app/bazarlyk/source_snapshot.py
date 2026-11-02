"""Pure format-1 export integrity contract, shared by export, prepare and import.

This is a byte snapshot, not a signature or a working-tree cleanliness assertion.
No provider or host runtime imports are permitted here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_MANIFEST = 4 * 1024 * 1024
MAX_FILE = 128 * 1024 * 1024
MAX_FILES = 100_000
MAX_TOTAL = 2 * 1024 * 1024 * 1024


class SnapshotError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value)
            or any(p in {"", ".", ".."} or p.lower() == ".git" for p in value.split("/"))):
        raise SnapshotError("Unsafe export path: " + repr(value))
    return value


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise SnapshotError("Export paths must be explicit and absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def validate_manifest(value):
    if (not isinstance(value, dict) or set(value) != {"format", "components"} or type(value["format"]) is not int
            or value["format"] != 1 or not isinstance(value["components"], list) or not 1 <= len(value["components"]) <= 1000):
        raise SnapshotError("Expected a format 1 SOURCE-MANIFEST.json")
    names, destinations = set(), set()
    for c in value["components"]:
        if not isinstance(c, dict) or "snapshot_sha256" not in c:
            raise SnapshotError("Export lacks snapshot integrity data; re-export with the current export-source.py")
        if set(c) != {"name", "destination", "commit", "tree", "snapshot_sha256"}:
            raise SnapshotError("Invalid source component fields")
        if not isinstance(c["name"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", c["name"]) or c["name"] in names:
            raise SnapshotError("Invalid or duplicate component name")
        dest = relative(c["destination"])
        if dest == "SOURCE-MANIFEST.json" or dest.startswith("SOURCE-MANIFEST.json/") or dest in destinations:
            raise SnapshotError("Invalid or duplicate component destination")
        for key, size in (("commit", 40), ("tree", 40), ("snapshot_sha256", 64)):
            if not isinstance(c[key], str) or not re.fullmatch(r"[0-9a-f]{" + str(size) + r"}", c[key]):
                raise SnapshotError("Invalid " + key + " in source component " + c["name"])
        names.add(c["name"])
        destinations.add(dest)
    return value


def snapshot_sha256(root, *, exclude=()):
    """Hash sorted path/kind/content/mode records, without following any symlink.

    Exclusions are relative descendant component roots, validated as directories.
    File descriptors pin the directories and reject replacements with symlinks.
    """
    excludes = {relative(p) for p in exclude}
    records, total, entries = [], 0, 0
    root = Path(root)
    def walk(fd, prefix=""):
        nonlocal total, entries
        for name in sorted(os.listdir(fd)):
            entries += 1
            path = relative(prefix + name)
            if entries > MAX_FILES or len(path.split("/")) > 64 or len(path) > 4096:
                raise SnapshotError("Export exceeds snapshot path bounds")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if path in excludes:
                if not stat.S_ISDIR(info.st_mode):
                    raise SnapshotError("Nested component is not a real directory: " + path)
                continue
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                try:
                    walk(child, path + "/")
                finally:
                    os.close(child)
            elif stat.S_ISLNK(info.st_mode):
                target = os.readlink(name, dir_fd=fd)
                resolved = os.path.normpath(os.path.join(os.path.dirname(path), target))
                if not target or os.path.isabs(target) or resolved == ".." or resolved.startswith("../"):
                    raise SnapshotError("Symlink escapes component: " + path)
                if any(resolved == p or resolved.startswith(p + "/") for p in excludes):
                    raise SnapshotError("Symlink crosses nested ownership: " + path)
                records.append({"path": path, "kind": "symlink", "target": target})
            elif stat.S_ISREG(info.st_mode):
                file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
                try:
                    before = os.fstat(file)
                    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE:
                        raise SnapshotError("Unsupported or oversized export file: " + path)
                    sha, size = hashlib.sha256(), 0
                    while chunk := os.read(file, min(65536, MAX_FILE - size + 1)):
                        size += len(chunk)
                        if size > MAX_FILE:
                            raise SnapshotError("Oversized export file: " + path)
                        sha.update(chunk)
                    after = os.fstat(file)
                    signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mode, s.st_mtime_ns, s.st_ctime_ns)
                    if signature(before) != signature(after) or size != before.st_size:
                        raise SnapshotError("Export changed while reading: " + path)
                    total += size
                    records.append({"path": path, "kind": "file", "sha256": sha.hexdigest(), "executable": bool(before.st_mode & 0o111)})
                finally:
                    os.close(file)
            else:
                raise SnapshotError("Unsupported export file type: " + path)
            if len(records) > MAX_FILES or total > MAX_TOTAL:
                raise SnapshotError("Export exceeds snapshot bounds")
    fd = _directory(root)
    try:
        walk(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(canonical(sorted(records, key=lambda r: r["path"]))).hexdigest()


def component_digest(root, component, components):
    dest = component["destination"]
    descendants = [c["destination"][len(dest) + 1:] for c in components if c["destination"].startswith(dest + "/")]
    # A descendant may have intervening path components; all must be real directories.
    for child in descendants:
        fd = _directory(Path(root) / dest / child)
        os.close(fd)
    return snapshot_sha256(Path(root) / dest, exclude=descendants)


def validate_export(root):
    """Read the bounded public manifest and verify every component before use."""
    root = Path(root)
    directory = _directory(root)
    try:
        fd = os.open("SOURCE-MANIFEST.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise SnapshotError("SOURCE-MANIFEST.json must be a regular file")
            raw = handle.read(MAX_MANIFEST + 1)
    finally:
        os.close(directory)
    if len(raw) > MAX_MANIFEST:
        raise SnapshotError("Source manifest exceeds its bound")
    manifest = validate_manifest(json.loads(raw, object_pairs_hook=unique_object))
    for c in manifest["components"]:
        if component_digest(root, c, manifest["components"]) != c["snapshot_sha256"]:
            raise SnapshotError("Export snapshot changed for " + c["name"] + "; re-export and prepare again")
    return manifest, hashlib.sha256(raw).hexdigest()
