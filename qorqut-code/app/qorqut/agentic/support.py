"""JSON-safe records, strict paths, and explicit read-only source access."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from posixpath import join as posix_join
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time

import yaml


MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 15
ROOT_RESOURCE_EXCLUDES = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".cache",
    ".claude", ".agents", ".codex", ".credentials", "runtime", "sessions", "credentials",
    "secrets", "credentials.json", "secrets.json",
})


def private_resource(name):
    return (name in ROOT_RESOURCE_EXCLUDES or name.startswith(".env")
            or name.endswith((".pem", ".key", ".token", ".sqlite", ".sqlite3", ".db", ".log")))


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

    def __init__(self, source_id, root, owner, enabled, diagnostics, *, export_component=None, manifest_digest=None, installed_distribution=None):
        self.id, self.root = identifier(source_id), absolute(root)
        self.owner, self.enabled, self.diagnostics = owner, enabled, diagnostics
        self.used, self.classified = {}, set()
        self.commit = self.repo_prefix = None
        self.clean = None
        self.export_component, self.manifest_digest = export_component, manifest_digest
        self.installed_distribution = installed_distribution
        if installed_distribution is not None:
            self._git_root, self._git_filters = None, None
            return
        if export_component is not None:
            self.commit = export_component["commit"]
            self.repo_prefix = ""
            self._git_root, self._git_filters = None, None
            return
        self._git_root = next((parent for parent in (self.root, *self.root.parents)
                               if (parent / ".git").exists()), None)
        self._git_filters = None
        try:
            self.commit = self.git("rev-parse", "HEAD").strip()
            self.repo_prefix = self.git("rev-parse", "--show-prefix").strip()
            # Restrict the cleanliness check to the supplied root (roles may be a subdirectory).
            self.clean = not bool(self.git("status", "--porcelain", "--untracked-files=all", "--", ".").strip())
            if not self.clean:
                self.issue("dirty_source", "Source must be a clean committed snapshot")
        except (OSError, subprocess.CalledProcessError):
            self.issue("unversioned_snapshot", "No Git commit; provenance uses the supplied snapshot digest",
                       severity="info" if self.id.startswith("custom-") or self.id == "email-guidance" else "error")

    def git(self, *args):
        if self._git_root is None:
            raise FileNotFoundError("No source Git repository")
        if self._git_filters is None:
            # Reading config does not execute a filter. Collect every effective driver,
            # including source-local includes, then override all executable settings.
            names = self._git_run("config", "--null", "--name-only", "--get-regexp",
                                  r"^filter\..*\.(clean|smudge|process|required)$", allowed=(0, 1))
            drivers = sorted({name.rsplit(".", 1)[0] for name in names.split("\0") if name})
            self._git_filters = [(f"{driver}.{key}", value) for driver in drivers for key, value in (
                ("clean", ""), ("smudge", ""), ("process", ""), ("required", "false"))]
        return self._git_run(*args)

    def _git_run(self, *args, allowed=(0,)):
        from qorqut import native_paths
        executable = (native_paths.executable("git") if native_paths.root() else shutil.which("git", path=os.defpath))
        if executable is None:
            raise FileNotFoundError("Git is unavailable")
        # No inherited GIT_DIR/WORK_TREE/INDEX/CONFIG/OBJECT_DIRECTORY or PATH.
        environment = {"PATH": os.defpath, "HOME": os.devnull, "XDG_CONFIG_HOME": os.devnull,
                       "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull,
                       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ATTR_NOSYSTEM": "1",
                       "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
        # Separate key/value environment fields preserve even unusual valid driver names.
        environment["GIT_CONFIG_COUNT"] = str(len(self._git_filters or []))
        for index, (key, value) in enumerate(self._git_filters or []):
            environment[f"GIT_CONFIG_KEY_{index}"] = key
            environment[f"GIT_CONFIG_VALUE_{index}"] = value
        from qorqut.native_paths import environment as native_environment
        environment = native_environment(environment)
        command = [executable, "--no-pager", "-c", f"core.hooksPath={os.devnull}",
                   "-c", "core.fsmonitor=false", "-c", f"core.attributesFile={os.devnull}",
                   "-c", "diff.external=",
                   "-C", str(self.root), f"--work-tree={self._git_root}", *args]
        chunks, total = {"stdout": [], "stderr": []}, 0
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=environment, start_new_session=True) as process:
            deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
            try:
                with selectors.DefaultSelector() as selector:
                    for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                        selector.register(pipe, selectors.EVENT_READ, name)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ImportFailure("source_git_timeout", "Source Git inspection timed out")
                        for key, _ in selector.select(remaining):
                            chunk = os.read(key.fileobj.fileno(), min(65536, MAX_GIT_OUTPUT_BYTES - total + 1))
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            total += len(chunk)
                            if total > MAX_GIT_OUTPUT_BYTES:
                                raise ImportFailure("source_git_output_limit", "Source Git output exceeds its limit")
                            chunks[key.data].append(chunk)
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise ImportFailure("source_git_timeout", "Source Git inspection timed out") from exc
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            if process.returncode not in allowed:
                raise subprocess.CalledProcessError(process.returncode, command)
        return b"".join(chunks["stdout"]).decode("utf-8", "surrogateescape")

    def issue(self, code, message, path=None, severity="error", **extra):
        self.diagnostics.append(dict(code=code, message=message, source_id=self.id,
                                     path=path, severity=severity, **extra))

    def exists(self, rel):
        relative(rel)
        if self.installed_distribution is not None and not rel.startswith('qorqut_' + self.id + '/'):
            return False
        parts = rel.split("/")
        try:
            with self._directory(parts[:-1]) as parent:
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ImportFailure("unsafe_path", "Source symlinks are not allowed", path=rel)
                return True
        except FileNotFoundError:
            return False

    @contextmanager
    def _directory(self, parts=()):
        """Open the root and descendants component by component, never following links."""
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for component in (*self.root.parts[1:], *parts):
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ImportFailure("unsafe_path", "Source directory is a symlink or not a directory") from exc
            raise
        finally:
            os.close(descriptor)

    def read(self, rel):
        parts = relative(rel).split("/")
        try:
            with self._directory(parts[:-1]) as parent:
                descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                                     dir_fd=parent)
                try:
                    before = os.fstat(descriptor)
                    if not stat.S_ISREG(before.st_mode):
                        raise ImportFailure("unsafe_path", "Source asset is not a regular file", path=rel)
                    if before.st_size > MAX_FILE_BYTES:
                        raise ImportFailure("oversized_resource", "Source file exceeds the byte limit", path=rel)
                    chunks, size = [], 0
                    while chunk := os.read(descriptor, min(65536, MAX_FILE_BYTES - size + 1)):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise ImportFailure("oversized_resource", "Source file exceeds the byte limit", path=rel)
                        chunks.append(chunk)
                    after = os.fstat(descriptor)
                    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
                    if signature(before) != signature(after) or size != after.st_size:
                        raise ImportFailure("source_changed", "Source file changed while reading", path=rel)
                    data = b"".join(chunks)
                finally:
                    os.close(descriptor)
        except FileNotFoundError as exc:
            raise ImportFailure("missing_resource", "Required source file is missing", path=rel) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ImportFailure("unsafe_path", "Source symlinks are not allowed", path=rel) from exc
            raise
        self.used[rel] = digest(data)
        if self.installed_distribution is not None and self.installed_distribution["distribution_files"].get(rel) != self.used[rel]:
            raise ImportFailure("installed_source_changed", "Instruction source differs from its wheel RECORD", path=rel)
        self.classified.add(rel)
        return data

    def tree(self, rel, *, root_resources=False):
        if rel:
            relative(rel)
        elif not root_resources:
            raise ImportFailure("unsafe_path", "An empty tree path requires an explicit resource root")
        found = []

        def walk(descriptor, prefix):
            for name in sorted(os.listdir(descriptor)):
                if root_resources and private_resource(name):
                    continue
                item = relative(posix_join(prefix, name))
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    found.append(item)
                elif stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                    dir_fd=descriptor)
                    try:
                        walk(child, item)
                    finally:
                        os.close(child)
                else:
                    raise ImportFailure("unsafe_path", "Source assets must be ordinary files or directories", path=item)

        try:
            with self._directory(rel.split("/") if rel else ()) as descriptor:
                walk(descriptor, rel)
        except FileNotFoundError as exc:
            raise ImportFailure("missing_resource", "Required source directory is missing", path=rel) from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ImportFailure("unsafe_path", "Source directory changed to a symlink", path=rel) from exc
            raise
        return sorted(found)

    def record(self):
        export = (dict(provenance_kind="committed_export", tree=self.export_component["tree"],
                       snapshot_sha256=self.export_component["snapshot_sha256"], source_manifest_sha256=self.manifest_digest)
                  if self.export_component is not None else {})
        if self.installed_distribution is not None:
            export = dict(provenance_kind="installed_distribution", **{key: value for key, value in self.installed_distribution.items() if key != "root"})
        return dict(id=self.id, owner=self.owner, enabled=self.enabled, commit=self.commit, root=str(self.root), **export,
                    repository_prefix=self.repo_prefix, clean=self.clean,
                    files=dict(sorted(self.used.items())), snapshot_digest=digest(self.used))
