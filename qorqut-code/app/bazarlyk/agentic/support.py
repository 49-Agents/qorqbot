"""JSON-safe records, strict paths, and explicit read-only source access."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

import yaml


class ImportFailure(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def encoded(value):
    return base64.b64encode(value).decode("ascii")


def decoded(value):
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ImportFailure("invalid_content", "Expected base64 file bytes") from exc


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value):
        raise ImportFailure("invalid_id", "Expected a lowercase package or entry ID", value=value)
    return value


def relative(value):
    if (not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or PurePosixPath(value).is_absolute()
            or any(part in {"", ".", "..", ".git", ".env", "__pycache__", ".venv", "venv", "node_modules"}
                   for part in value.split("/"))):
        raise ImportFailure("unsafe_path", "Expected a safe relative source path", path=value)
    if any(part.endswith((".pem", ".key", ".token")) or part in {"credentials.json", "secrets.json"}
           for part in value.split("/")):
        raise ImportFailure("unsafe_path", "Credential files are not source assets", path=value)
    return value


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ImportFailure("unsafe_path", "Source roots must be explicit absolute paths", path=str(path))
    for ancestor in [*reversed(path.parents), path]:
        if ancestor.is_symlink():
            raise ImportFailure("unsafe_path", "Symlink source roots are not allowed", path=str(path))
    if not path.is_dir():
        raise ImportFailure("missing_source", "Source root is not a directory", path=str(path))
    return path


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key, value in node.value:
        name = loader.construct_object(key, deep=deep)
        if name in result:
            raise ImportFailure("invalid_manifest", "Duplicate YAML key", key=str(name))
        result[name] = loader.construct_object(value, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(data):
    try:
        value = yaml.load(data.decode("utf-8"), Loader=UniqueLoader)
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ImportFailure("invalid_manifest", "Invalid UTF-8 YAML", reason=str(exc)) from exc
    if not isinstance(value, dict):
        raise ImportFailure("invalid_manifest", "Native manifest must be a mapping")
    return value


class Source:
    """Reads only an explicitly supplied root; never follows filesystem links."""

    def __init__(self, source_id, root, owner, enabled, diagnostics):
        self.id, self.root = identifier(source_id), absolute(root)
        self.owner, self.enabled, self.diagnostics = owner, enabled, diagnostics
        self.used, self.classified = {}, set()
        self.commit = self.repo_prefix = None
        self.clean = None
        try:
            self.commit = self.git("rev-parse", "HEAD").strip()
            self.repo_prefix = self.git("rev-parse", "--show-prefix").strip()
            # Restrict the cleanliness check to the supplied root (roles may be a subdirectory).
            self.clean = not bool(self.git("status", "--porcelain", "--untracked-files=all", "--", ".").strip())
            if not self.clean:
                self.issue("dirty_source", "Source must be a clean committed snapshot")
        except (OSError, subprocess.CalledProcessError):
            self.issue("unversioned_snapshot", "No Git commit; provenance uses the supplied snapshot digest",
                       severity="info" if self.id.startswith("custom-") else "error")

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.root), *args], check=True,
                                capture_output=True, text=True, timeout=15,
                                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        return result.stdout

    def issue(self, code, message, path=None, severity="error", **extra):
        self.diagnostics.append(dict(code=code, message=message, source_id=self.id,
                                     path=path, severity=severity, **extra))

    def path(self, rel):
        relative(rel)
        current = self.root
        for part in rel.split("/"):
            current = current / part
            if current.is_symlink():
                raise ImportFailure("unsafe_path", "Source symlinks are not allowed", path=rel)
        return current

    def exists(self, rel):
        return self.path(rel).exists()

    def read(self, rel):
        path = self.path(rel)
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise ImportFailure("missing_resource", "Required source file is missing", path=rel)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ImportFailure("oversized_resource", "Source file exceeds 16 MiB", path=rel)
        data = path.read_bytes()
        self.used[rel] = digest(data)
        self.classified.add(rel)
        return data

    def tree(self, rel):
        start = self.path(rel)
        if not start.is_dir():
            raise ImportFailure("missing_resource", "Required source directory is missing", path=rel)
        found = []
        for base, dirs, files in os.walk(start, followlinks=False):
            for name in sorted(dirs + files):
                path = Path(base) / name
                item = path.relative_to(self.root).as_posix()
                relative(item)
                if path.is_symlink():
                    raise ImportFailure("unsafe_path", "Source symlinks are not allowed", path=item)
                if path.is_file():
                    found.append(item)
                elif not path.is_dir():
                    raise ImportFailure("unsafe_path", "Source assets must be ordinary files", path=item)
        return sorted(found)

    def record(self):
        return dict(id=self.id, owner=self.owner, enabled=self.enabled, commit=self.commit,
                    repository_prefix=self.repo_prefix, clean=self.clean,
                    files=dict(sorted(self.used.items())), snapshot_digest=digest(self.used))
