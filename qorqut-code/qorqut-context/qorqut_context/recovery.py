"""Authoritative capture, verification and explicit offline relocated restore of one Context.

A Context is a Git repository plus a separate runtime directory that together hold its authority:
the published graph, source and event history; the pending draft and its durable input; the execution
attempts and usage receipts; and every Git object they reference. This module captures that pair as one
self-contained component, verifies a capture without trusting the live pair, and restores it into new
locations. See CORE_CONTRACTS.md, "Recovery capture and relocated restore".

* ``capture`` runs only under a writer barrier supplied by the caller (``WriterBarrier``) and additionally
  takes the Context's own non-blocking writer locks. It checks the barrier and a fingerprint of every
  source file before and after copying, refuses anything it cannot classify, and publishes the capture
  directory only when it is complete and has verified itself.
* ``verify`` rebuilds the Git side in a private scratch repository and re-runs the engine's own format,
  event-integrity and pending-job checks. It needs neither the source nor the barrier and changes nothing.
* ``restore`` takes explicit new repository/runtime locations, never paths from the capture, journals its
  phases, stages and validates both sides, rebinds only the two location ownership markers, and installs
  the runtime before the repository so a half-installed pair cannot be opened. It never starts a driver,
  model, publication, recovery or index rebuild.

Derived data is limited to what the owner can rebuild: the search index (``SQLiteIndex``), the Git index
and checkout (rebuilt from the captured checkout revision), Git's template files and accelerators, and
lock files. Everything else must be classified; an unknown file or ref-less object source fails the
capture instead of being dropped.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Mapping, Protocol

from . import controlled
from .engine import Context, atomic_write, check_ownership, file_lock
from .git_store import FORMAT_VERSION, PUBLIC_REF, READABLE_VERSIONS, Git, Snapshot
from .schema import Conflict, ContextError, encode
from . import questions

FORMAT = "qorqut-context-recovery"
VERSION = 1
COMPONENT = "qorqut-context"
RESTORE_FORMAT = "qorqut-context-restore"
MANIFEST = "manifest.json"
PACK = "git/objects.pack"
LOG_ROOT = "git/logs"
RUNTIME_ROOT = "runtime"
MARKERS = {"repository": ".git/qorqut-context/runtime.json", "runtime": "context.json"}
LOCKS = frozenset({"qorqut-context/writer.lock", "qorqut-context/propagation.lock"})  # beneath the Git directory

OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
EVENT = re.compile(r"[a-z0-9]{5}")
ATTEMPT = re.compile(r"[0-9a-f]{32}")
# Keys of the engine's own pending/attempt/receipt records whose value, when it is an object name, the
# retained state needs to exist; any other object-looking string is included only if it resolves.
REQUIRED_OBJECT_KEYS = frozenset({"base_revision", "published_commit", "expected_revision",
                                  "expected_previous_commit", "commit"})
CONFIG_KEYS = frozenset({"core.repositoryformatversion", "core.filemode", "core.bare", "core.logallrefupdates",
                         "core.ignorecase", "core.precomposeunicode", "core.symlinks"})
GIT_TOP = frozenset({"objects", "refs", "logs", "hooks", "info", "branches", "qorqut-context"})
PHASES = ("started", "staged", "bound", "runtime_installed", "repository_installed", "complete")


class RecoveryError(ContextError):
    """A capture, verification or restore refusal; ``code`` is stable and ``retryable`` marks transient causes."""

    def __init__(self, code, message, *, retryable=False, details=None):
        super().__init__(message)
        self.code, self.retryable, self.details = code, retryable, details or {}


class WriterBarrier(Protocol):
    """Proof, supplied by the host, that every writer of this Context is stopped or fenced right now.

    ``verify_active`` is called before and after a capture and must raise unless the barrier is still
    proven. A missing method or any exception is a refusal; a returned mapping is recorded in the manifest as
    evidence and any other return value is ignored. The library never turns a boolean into proof.
    """

    def verify_active(self) -> Mapping | None: ...


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def _location(value, label):
    if not isinstance(value, (str, os.PathLike)):
        raise RecoveryError("invalid_request", f"{label} must be a path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
        raise RecoveryError("invalid_request", f"{label} must be an absolute path without traversal")
    if Path(os.path.realpath(path)) != path:
        raise RecoveryError("invalid_request", f"{label} must not cross a symbolic link")
    return path


def _inside(path, base):
    return path == base or base in path.parents


def _pair(repository, runtime, labels=("repository", "runtime")):
    repository, runtime = _location(repository, labels[0]), _location(runtime, labels[1])
    if _inside(repository, runtime) or _inside(runtime, repository):
        raise RecoveryError("invalid_request", "The repository and runtime must be separate locations")
    return repository, runtime


def _git(root):
    return Git(root)


def _read_git(root, *args, data=None, env=None, code="source_invalid"):
    try:
        return _git(root).run(*args, data=data, env=env)
    except ContextError as error:
        raise RecoveryError(code, str(error)) from error


def _barrier(barrier):
    check = getattr(barrier, "verify_active", None)
    if not callable(check):
        raise RecoveryError("barrier_unproven", "Capture requires a writer barrier that can prove it is still active")
    try:
        evidence = check()
    except RecoveryError:
        raise
    except Exception as error:
        raise RecoveryError("barrier_unproven", "The writer barrier is not proven active") from error
    if isinstance(evidence, Mapping):
        try:
            json.dumps(evidence, sort_keys=True)
        except (TypeError, ValueError):
            return {}
        return dict(evidence)
    return {}


def _entries(base, label, *, skip=frozenset()):
    """Every entry beneath ``base`` (relative posix path to ``lstat``); links and special files are refused."""
    failure = []

    def onerror(error):
        failure.append(error)

    result = {}
    for parent, directories, names in os.walk(base, followlinks=False, onerror=onerror):
        directories[:] = sorted(name for name in directories if not (Path(parent) == base and name in skip))
        for name in [*directories, *sorted(names)]:
            if Path(parent) == base and name in skip:
                continue
            path = Path(parent) / name
            info = os.lstat(path)
            relative = path.relative_to(base).as_posix()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise RecoveryError("unclassified_authority", f"{label} contains a link or special file: {relative}")
            result[relative] = info
    if failure:
        raise RecoveryError("source_unreadable", f"{label} cannot be read completely: {failure[0]}")
    return result


def _classify_git(relative, is_dir):
    """What a path in the Git directory is. Only the named classes are accepted; the rest is refused."""
    parts = relative.split("/")
    top = parts[0]
    if is_dir:
        return "dir" if top in GIT_TOP else "unknown"
    if relative.endswith(".lock") and relative not in LOCKS:
        return "busy"
    if relative == "HEAD":
        return "head"
    if relative == "config":
        return "config"
    if relative in ("description", "index"):
        return "derived"
    if relative == "packed-refs" or top == "refs":
        return "ref"
    if top == "logs":
        return "log"
    if top == "hooks":
        return "derived" if len(parts) == 2 and parts[1].endswith(".sample") else "unknown"
    if relative == "info/exclude":
        return "derived"
    if top == "qorqut-context":
        if relative == "qorqut-context/runtime.json":
            return "marker"
        return "lock" if relative in LOCKS else "unknown"
    if top == "objects":
        name = parts[-1]
        if relative in ("objects/info/alternates", "objects/info/http-alternates"):
            return "forbidden"
        if len(parts) == 3 and re.fullmatch(r"[0-9a-f]{2}", parts[1]):
            if re.fullmatch(r"[0-9a-f]{38}|[0-9a-f]{62}", name):
                return "object"
            return "busy" if name.startswith("tmp_obj_") else "unknown"
        if len(parts) == 3 and parts[1] == "pack":
            if re.fullmatch(r"pack-[0-9a-f]+\.(pack|idx)", name):
                return "object"
            if name.startswith("tmp_"):
                return "busy"
            if name.endswith((".keep", ".promisor", ".mtimes")):
                return "forbidden"
            if name.endswith((".rev", ".bitmap")) or name == "multi-pack-index":
                return "derived"
        if relative in ("objects/info/packs", "objects/info/commit-graph") or relative.startswith("objects/info/commit-graphs/"):
            return "derived"
    return "unknown"


def _classify_runtime(relative, is_dir, index_name, locks=frozenset()):
    parts = relative.split("/")
    name = parts[-1]
    if is_dir:
        if parts[0] == "events":
            return "dir" if len(parts) == 1 or (len(parts) == 2 and EVENT.fullmatch(parts[1])) else "unknown"
        if parts[0] == "questions":
            return "dir" if len(parts) == 1 else "unknown"
        if parts[0] == "runs":
            if len(parts) == 1:
                return "dir"
            if not EVENT.fullmatch(parts[1]):
                return "unknown"
            return "dir" if len(parts) == 2 or ATTEMPT.fullmatch(parts[2]) else "unknown"
        return "unknown"
    if relative == "context.json":
        return "marker"
    if relative == "writer.lock":
        return "lock"
    if relative == "pending.json":
        return "authority"
    if name.startswith(".write-"):
        return "transient"
    if parts[0] == "events":
        return "authority" if len(parts) == 3 and EVENT.fullmatch(parts[1]) and name == "input.bin" else "unknown"
    if parts[0] == "questions":
        return "authority" if len(parts) == 2 and re.fullmatch(r"[a-z0-9]{5}\.json", name) else "unknown"
    if parts[0] == "runs":
        return "authority" if len(parts) >= 4 and EVENT.fullmatch(parts[1]) and ATTEMPT.fullmatch(parts[2]) else "unknown"
    if len(parts) == 1:
        if name in locks:
            return "lock"
        if name == index_name or name.startswith(index_name + "-"):
            return "derived"
        if re.fullmatch(r"search-.+\.sqlite3(-journal|-wal|-shm)?", name):
            return "derived"
        if name.startswith("git-index-"):
            return "transient"
    return "unknown"


def _git_directory(repository):
    git_dir = repository / ".git"
    try:
        info = os.lstat(git_dir)
    except OSError as error:
        raise RecoveryError("source_invalid", "The repository has no readable .git directory") from error
    if not stat.S_ISDIR(info.st_mode):
        raise RecoveryError("not_self_contained",
                            "The Git directory is not inside the repository (a .git file or link points elsewhere)")
    return git_dir


class _Source:
    """One read-only view of the live pair: classified entries and the fingerprint that detects change."""

    def __init__(self, repository, runtime, index_name, locks=frozenset()):
        self.repository, self.runtime, self.locks = repository, runtime, frozenset(locks)
        self.git = _entries(_git_directory(repository), "The Git directory")
        self.runtime_entries = _entries(runtime, "The runtime")
        self.worktree = _entries(repository, "The checkout", skip=frozenset({".git"}))
        self.git_class = {rel: _classify_git(rel, stat.S_ISDIR(info.st_mode)) for rel, info in self.git.items()}
        self.runtime_class = {rel: _classify_runtime(rel, stat.S_ISDIR(info.st_mode), index_name, self.locks)
                              for rel, info in self.runtime_entries.items()}
        for rel in self.locks & set(self.runtime_class):
            # A host lock is only an advisory flock target: a file that holds bytes is not one and stays unclassified.
            if self.runtime_class[rel] == "lock" and self.runtime_entries[rel].st_size:
                self.runtime_class[rel] = "unknown"
        for code, kinds, message in (
                ("source_changing", {"busy"}, "A writer left an in-progress file; retry when it has finished"),
                ("not_self_contained", {"forbidden"}, "The repository depends on storage outside its Git directory"),
                ("unclassified_authority", {"unknown"}, "A file is neither Context authority nor owner-proven derived data")):
            found = sorted([f".git/{rel}" for rel, kind in self.git_class.items() if kind in kinds] +
                           [f"runtime/{rel}" for rel, kind in self.runtime_class.items() if kind in kinds])
            if found:
                raise RecoveryError(code, message, retryable=code == "source_changing", details={"paths": found[:20]})

    def of(self, area, kind):
        classes, entries = ((self.git_class, self.git) if area == "git" else (self.runtime_class, self.runtime_entries))
        return sorted(rel for rel, value in classes.items() if value == kind and not stat.S_ISDIR(entries[rel].st_mode))

    def fingerprint(self, state):
        """Stable over everything a writer could change: every non-lock, non-derived entry and the refs/pending state."""
        def row(area, rel, info):
            if stat.S_ISDIR(info.st_mode):
                return [area, rel, "dir"]  # a directory's mtime moves when a lock file appears; its files are listed
            return [area, rel, "file", info.st_size, info.st_mtime_ns, info.st_ino]
        rows = [row("git", rel, info) for rel, info in sorted(self.git.items())
                if self.git_class[rel] not in ("lock",)]
        rows += [row("runtime", rel, info) for rel, info in sorted(self.runtime_entries.items())
                 if self.runtime_class[rel] not in ("lock", "derived", "transient")]
        rows += [row("worktree", rel, info) for rel, info in sorted(self.worktree.items())]
        return _digest({"files": rows, "state": state})


def _git_config(repository):
    path = repository / ".git/config"
    try:
        output = _read_git(repository, "config", "--file", str(path), "--list", "--null")
    except RecoveryError as error:
        raise RecoveryError("source_invalid", "The Git configuration cannot be read") from error
    keys = {}
    for item in output.split(b"\0"):
        if item:
            key, _, value = item.decode("utf-8", errors="replace").partition("\n")
            keys[key] = value
    extra = sorted(set(keys) - CONFIG_KEYS)
    if extra or keys.get("core.repositoryformatversion") != "0" or keys.get("core.bare") != "false":
        raise RecoveryError("unclassified_authority", "The Git configuration holds settings the Context did not create",
                            details={"keys": extra})
    return keys


def _check_exclude(repository):
    path = repository / ".git/info/exclude"
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                raise RecoveryError("unclassified_authority", "The Git exclude file hides paths from the Context checkout")


def _head(repository):
    text = (repository / ".git/HEAD").read_text(encoding="utf-8", errors="replace")
    if text != f"ref: {PUBLIC_REF}\n":
        raise RecoveryError("source_invalid", f"HEAD does not name {PUBLIC_REF}")


def _refs(repository):
    output = _read_git(repository, "for-each-ref", "--format=%(refname)%00%(objectname)%00%(objecttype)")
    refs = []
    for line in output.decode("utf-8").splitlines():
        name, oid, kind = line.split("\0")
        refs.append({"name": name, "oid": oid, "type": kind})
    refs.sort(key=lambda item: item["name"])
    if PUBLIC_REF not in {item["name"] for item in refs}:
        raise RecoveryError("source_invalid", f"The published ref {PUBLIC_REF} does not exist")
    return refs


def _walk_json(value, key, found):
    if isinstance(value, dict):
        for name, member in value.items():
            _walk_json(member, name, found)
    elif isinstance(value, list):
        for member in value:
            _walk_json(member, key, found)
    elif isinstance(value, str) and OID.fullmatch(value):
        found.append((value, key in REQUIRED_OBJECT_KEYS))


def _runtime_references(runtime, source):
    """Object names held by retained runtime state: ``{oid: (required, [paths])}``."""
    result = {}
    for rel in source.of("runtime", "authority"):
        if not (rel == "pending.json" or (rel.startswith("runs/") and rel.endswith(".json"))):
            continue
        try:
            value = json.loads((runtime / rel).read_bytes())
        except (OSError, ValueError) as error:
            raise RecoveryError("source_invalid", f"Retained runtime evidence is unreadable or invalid: {rel}") from error
        found = []
        _walk_json(value, None, found)
        for oid, required in found:
            entry = result.setdefault(oid, [False, []])
            entry[0] = entry[0] or required
            if rel not in entry[1] and len(entry[1]) < 8:
                entry[1].append(rel)
    return result


def _object_types(repository, oids):
    """``{oid: type}`` for the objects that exist; absent names are not in the result."""
    if not oids:
        return {}
    output = _read_git(repository, "cat-file", "--batch-check", data="".join(oid + "\n" for oid in oids).encode())
    result = {}
    for line in output.decode().splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1] != "missing":
            result[fields[0]] = fields[1]
    return result


def _closure(root, tips):
    output = _read_git(root, "rev-list", "--objects", "--stdin", data="".join(tip + "\n" for tip in tips).encode(),
                       code="object_missing")
    return {line.split(" ", 1)[0] for line in output.decode().splitlines() if line}


def _closure_digest(objects):
    return hashlib.sha256(("\n".join(sorted(objects)) + "\n").encode()).hexdigest()


def _tips(refs, roots):
    return sorted({item["oid"] for item in refs} | {item["oid"] for item in roots})


def _status(repository):
    """Staged paths and a refusal for every external edit: the checkout may only lag the index, never differ from it."""
    output = _read_git(repository, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--no-renames",
                       "--untracked-files=all", "--ignored")
    staged = []
    for entry in output.split(b"\0"):
        if not entry:
            continue
        flags, name = entry[:2].decode(), entry[3:].decode("utf-8", errors="replace")
        if flags in ("??", "!!") or flags[1] != " ":
            raise RecoveryError("source_dirty", "The Context checkout has external edits; preserve and resolve them first",
                                details={"path": name})
        staged.append(name)
    return staged


def _checkout_revision(repository, head, job, staged):
    candidates = [head] + ([job["base_revision"]] if job and job.get("base_revision") not in (None, head) else [])
    for candidate in candidates:
        if not _read_git(repository, "--no-optional-locks", "diff-index", "--cached", "--name-only", candidate).strip():
            return candidate
    raise RecoveryError("source_dirty", "The Context checkout does not match a published revision",
                        details={"staged": staged[:20]})


def _validate_job(job, snapshot, input_bytes):
    """The engine's own current-format and execution-binding checks of a pending job, plus its durable input.

    A job whose event is already published only awaits cleanup (its input may already be gone); any other
    job needs its sealed input.
    """
    Context._require_current_format(job)
    controlled.binding(job)
    if job.get("input_digest") is not None and job["id"] not in snapshot.events:
        content = input_bytes(job["id"])
        if content is None or hashlib.sha256(content).hexdigest() != job["input_digest"]:
            raise RecoveryError("source_invalid", "The pending event's durable input is missing or does not match its digest")


def _check_outbox(snapshot, runtime, code):
    """Each undelivered question payload must be a well-formed current-format entry of an event published at its commit."""
    directory = runtime / "questions"
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        try:
            value = json.loads(path.read_bytes())
            event_id = path.stem
            if (not isinstance(value, dict) or set(value) != {"format_version", "event_id", "published_commit", "questions"}
                    or value["format_version"] != FORMAT_VERSION or value["event_id"] != event_id
                    or snapshot.event_commits.get(event_id) != value["published_commit"]):
                raise ValueError("identity")
            questions.entries(value["questions"])
        except (OSError, ValueError, KeyError, ContextError) as error:
            raise RecoveryError(code, f"The question outbox entry is invalid or does not match a published event: {path.name}") from error


def _snapshot(root, label):
    try:
        git = _git(root)
        return git, Snapshot(git, git.resolve())
    except (ContextError, ValueError, KeyError) as error:
        raise RecoveryError("source_invalid", f"{label} failed the engine's format and event-integrity checks: {error}") from error


def _copy_stable(source, destination, mode):
    """Copy one file, refusing if it changes while being read; returns ``(bytes, sha256)``."""
    before = os.lstat(source)
    digest = hashlib.sha256()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(source, "rb") as reader, os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as writer:
        for block in iter(lambda: reader.read(1 << 20), b""):
            digest.update(block)
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
    after = os.lstat(source)
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RecoveryError("source_changed", f"A source file changed while being captured: {source.name}", retryable=True)
    return before.st_size, digest.hexdigest()


def _marker(path, key, expected, relative):
    """The marker record, or a refusal when it is missing or names another location."""
    try:
        present = check_ownership(path, {key: expected})
    except Conflict as error:
        raise RecoveryError("invalid_ownership", str(error)) from error
    if not present:
        raise RecoveryError("invalid_ownership", f"The ownership marker is missing: {path}")
    return {"path": relative, "value": {key: expected}, "sha256": _sha256(path)}


def _lock_names(names):
    """The host's advisory lock files in the runtime root; each is a plain single name, never an engine file."""
    names = frozenset(names)
    for name in names:
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.lock", name)
                or name == "writer.lock"):
            raise RecoveryError("invalid_request", "A runtime lock must be a plain single-segment *.lock name")
    return names


def capture(repository, runtime, destination, *, barrier, runtime_locks=(), clock=_now):
    """Capture the Context pair into the new directory ``destination``; returns the manifest summary.

    The destination appears only complete: it is built beside itself, verified, and then renamed.
    ``runtime_locks`` names the host's own advisory lock files directly in the runtime (for example the queue
    driver's): each is empty by definition, is not captured, is recorded in the manifest, and is held while it
    exists so a running host writer is a refusal. Any other unknown file in the pair still fails the capture.
    """
    locks = _lock_names(runtime_locks)
    repository, runtime = _pair(repository, runtime)
    destination = _location(destination, "capture destination")
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise RecoveryError("invalid_request", "The capture destination must be a new directory in an existing folder")
    for base, name in ((repository, "repository"), (runtime, "runtime")):
        if not base.is_dir() or _inside(destination, base):
            raise RecoveryError("invalid_request", f"The {name} must exist and must not contain the capture")
    _git_directory(repository)
    before = _barrier(barrier)
    temporary = Path(tempfile.mkdtemp(prefix=".context-capture-", dir=destination.parent))
    try:
        markers = {"repository": _marker(repository / MARKERS["repository"], "runtime_root", str(runtime), MARKERS["repository"]),
                   "runtime": _marker(runtime / MARKERS["runtime"], "context_root", str(repository), MARKERS["runtime"])}
        index_name = Context(repository, runtime).index.path.name
        with ExitStack() as stack:
            # Propagation -> repository -> runtime, the order the pipeline and engine use. A held lock is a running writer.
            try:
                for lock in (repository / ".git/qorqut-context/propagation.lock",
                             repository / ".git/qorqut-context/writer.lock", runtime / "writer.lock",
                             *(runtime / name for name in sorted(locks) if (runtime / name).is_file())):
                    stack.enter_context(file_lock(lock))
            except Conflict as error:
                raise RecoveryError("writer_active", "A Context writer or propagation is running", retryable=True) from error
            source = _Source(repository, runtime, index_name, locks)
            manifest = _capture(repository, runtime, temporary, source, index_name, markers, barrier, before, clock)
        data = encode(manifest)
        atomic_write(temporary / MANIFEST, data)
        verify(temporary, expected_manifest_sha256=hashlib.sha256(data).hexdigest())
        _fsync_directory(temporary)
        os.rename(temporary, destination)
        _fsync_directory(destination.parent)
        return summary(manifest, hashlib.sha256(data).hexdigest())
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _capture(repository, runtime, target, source, index_name, markers, barrier, barrier_before, clock):
    _head(repository)
    _check_exclude(repository)
    config = _git_config(repository)
    refs = _refs(repository)
    head = next(item["oid"] for item in refs if item["name"] == PUBLIC_REF)
    reachable = _closure(repository, _tips(refs, []))      # a missing object is reported as such, before the engine reads it
    git, snapshot = _snapshot(repository, "The published history")
    job = None
    if (runtime / "pending.json").exists():
        job = json.loads((runtime / "pending.json").read_bytes())
        try:
            _validate_job(job, snapshot, lambda event: _read_optional(runtime / "events" / event / "input.bin"))
        except RecoveryError:
            raise
        except ContextError as error:
            raise RecoveryError("source_invalid", f"The pending job failed the engine's checks: {error}") from error
    _check_outbox(snapshot, runtime, "source_invalid")
    staged = _status(repository)
    checkout = _checkout_revision(repository, head, job, staged)
    references = _runtime_references(runtime, source)
    existing = _object_types(repository, sorted(references))
    missing = sorted(oid for oid, (required, _) in references.items() if required and oid not in existing)
    if missing:
        raise RecoveryError("object_missing", "Retained runtime state names Git objects the repository does not have",
                            details={"objects": missing[:20]})
    roots = [{"oid": oid, "type": existing[oid], "referenced_by": references[oid][1]}
             for oid in sorted(existing) if oid not in reachable]
    closure = reachable | _closure(repository, _tips(refs, roots)) if roots else reachable
    state = {"refs": refs, "head": head, "checkout": checkout, "roots": [item["oid"] for item in roots],
             "pending": _sha256(runtime / "pending.json") if job else None}
    fingerprint_before = source.fingerprint(state)

    (target / "git").mkdir(mode=0o700)
    pack = target / PACK
    with open(pack, "xb") as handle:
        try:
            git.stream("pack-objects", "--revs", "--stdout", output=handle,
                       data="".join(tip + "\n" for tip in _tips(refs, roots)).encode())
        except ContextError as error:
            raise RecoveryError("object_missing", f"The object closure cannot be packed: {error}") from error
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(pack, 0o600)
    logs = []
    for rel in source.of("git", "log"):
        size, sha = _copy_stable(repository / ".git" / rel, target / LOG_ROOT / rel[len("logs/"):], 0o600)
        logs.append({"path": rel[len("logs/"):], "bytes": size, "sha256": sha})
    files, directories = [], []
    for rel in sorted(source.runtime_entries):
        kind = source.runtime_class[rel]
        info = source.runtime_entries[rel]
        if kind == "dir":
            directories.append({"path": rel, "mode": stat.S_IMODE(info.st_mode)})
        elif kind == "authority":
            size, sha = _copy_stable(runtime / rel, target / RUNTIME_ROOT / rel, 0o600)
            files.append({"path": rel, "bytes": size, "sha256": sha, "mode": stat.S_IMODE(info.st_mode)})
    derived = ([{"area": "git", "path": rel, "reason": "template, accelerator or index Git rebuilds from the objects"}
                for rel in source.of("git", "derived")] +
               [{"area": "runtime", "path": rel, "owner": "qorqut_context.search.SQLiteIndex",
                 "reason": "disposable search index; rebuilt from the published revision"}
                for rel in source.of("runtime", "derived")])
    transient = [{"area": "runtime", "path": rel, "bytes": source.runtime_entries[rel].st_size}
                 for rel in source.of("runtime", "transient")]

    after_source = _Source(repository, runtime, index_name, source.locks)
    if after_source.fingerprint({"refs": _refs(repository), "head": head, "checkout": checkout,
                                 "roots": [item["oid"] for item in roots],
                                 "pending": _sha256(runtime / "pending.json") if job else None}) != fingerprint_before:
        raise RecoveryError("source_changed", "The Context changed while it was being captured", retryable=True)
    barrier_after = _barrier(barrier)
    return {
        "format": FORMAT, "format_version": VERSION,
        "component": {"name": COMPONENT, "context_format": snapshot.format_version, "published_ref": PUBLIC_REF},
        "captured_at": clock(),
        "source": {"repository": str(repository), "runtime": str(runtime), "ownership": markers},
        "cutoffs": {"published_commit": head, "events": len(snapshot.events),
                    "pending_event": job["id"] if job else None,
                    "usage_receipts": sum(1 for item in files if "/calls/" in item["path"])},
        "git": {"head": PUBLIC_REF, "published_commit": head, "checkout_revision": checkout, "refs": refs,
                "runtime_only_roots": roots,
                "closure": {"objects": len(closure), "sha256": _closure_digest(closure)},
                "pack": {"path": PACK, "bytes": pack.stat().st_size, "sha256": _sha256(pack)},
                "logs": logs, "config": {key: config[key] for key in sorted(config)}},
        "runtime": {"files": files, "directories": directories, "locks": sorted(source.locks),
                    "pending": ({"event_id": job["id"], "phase": job.get("phase"), "base_revision": job.get("base_revision")}
                                if job else None)},
        "derived": derived, "transient": transient,
        "barrier": {"before": barrier_before, "after": barrier_after},
        "fingerprint": {"before": fingerprint_before, "after": fingerprint_before},
        "complete": True,
    }


def _read_optional(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def summary(manifest, manifest_sha256):
    """The compact, host-storable description of one capture (every field is also in the manifest)."""
    return {"component": COMPONENT, "format": manifest["format"], "format_version": manifest["format_version"],
            "manifest_sha256": manifest_sha256, "captured_at": manifest["captured_at"],
            "source_repository": manifest["source"]["repository"], "source_runtime": manifest["source"]["runtime"],
            "published_commit": manifest["git"]["published_commit"], "events": manifest["cutoffs"]["events"],
            "pending_event": manifest["cutoffs"]["pending_event"],
            "closure_sha256": manifest["git"]["closure"]["sha256"], "closure_objects": manifest["git"]["closure"]["objects"],
            "runtime_files": len(manifest["runtime"]["files"]), "complete": manifest["complete"]}


def _manifest_shape(manifest):
    try:
        if (manifest["format"] != FORMAT or manifest["format_version"] != VERSION or manifest["complete"] is not True
                or set(manifest["component"]) != {"name", "context_format", "published_ref"}
                or manifest["component"]["name"] != COMPONENT or manifest["component"]["published_ref"] != PUBLIC_REF
                or type(manifest["component"]["context_format"]) is not int
                or manifest["component"]["context_format"] not in READABLE_VERSIONS
                or manifest["fingerprint"]["before"] != manifest["fingerprint"]["after"]):
            raise ValueError("format")
        for key in ("repository", "runtime"):
            if not isinstance(manifest["source"][key], str) or not os.path.isabs(manifest["source"][key]):
                raise ValueError("source")
        ownership = manifest["source"]["ownership"]
        if (ownership["repository"]["value"] != {"runtime_root": manifest["source"]["runtime"]}
                or ownership["runtime"]["value"] != {"context_root": manifest["source"]["repository"]}):
            raise ValueError("ownership")
        git = manifest["git"]
        if (git["head"] != PUBLIC_REF or git["published_commit"] != next(i["oid"] for i in git["refs"] if i["name"] == PUBLIC_REF)
                or not OID.fullmatch(git["checkout_revision"])):
            raise ValueError("git")
        for item in (*git["logs"], *manifest["runtime"]["files"]):
            if (".." in Path(item["path"]).parts or Path(item["path"]).is_absolute() or not isinstance(item["bytes"], int)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise ValueError("artifact")
        locks = manifest["runtime"]["locks"]
        if not isinstance(locks, list) or locks != sorted(set(locks)):
            raise ValueError("locks")
        try:
            _lock_names(locks)
        except RecoveryError as error:
            raise ValueError("locks") from error
        return manifest
    except (KeyError, TypeError, ValueError, StopIteration) as error:
        raise RecoveryError("invalid_capture", "The capture manifest is not a complete current-format manifest") from error


def _expected_files(manifest):
    return ({MANIFEST, PACK} | {f"{LOG_ROOT}/{item['path']}" for item in manifest["git"]["logs"]}
            | {f"{RUNTIME_ROOT}/{item['path']}" for item in manifest["runtime"]["files"]})


def verify(directory, *, expected_manifest_sha256):
    """Verify a capture against the manifest digest the caller recorded; returns ``summary`` and changes nothing.

    Rebuilds the Git side in a private scratch repository: the pack must index strictly, every ref must
    resolve, the closure must equal the manifest's, and the engine's own format, event-integrity and
    pending-job checks must pass.
    """
    directory = _location(directory, "capture directory")
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256):
        raise RecoveryError("invalid_request", "Verification requires the recorded manifest digest")
    try:
        if not directory.is_dir():
            raise RecoveryError("invalid_capture", "The capture directory is missing")
        data = (directory / MANIFEST).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_manifest_sha256:
            raise RecoveryError("invalid_capture", "The capture manifest does not match its recorded digest")
        manifest = _manifest_shape(json.loads(data))
        found = {rel for rel, info in _entries(directory, "The capture").items() if stat.S_ISREG(info.st_mode)}
        expected = _expected_files(manifest)
        if found != expected:
            raise RecoveryError("invalid_capture", "The capture holds missing or unexpected files",
                                details={"missing": sorted(expected - found)[:20], "unexpected": sorted(found - expected)[:20]})
        for item in manifest["runtime"]["files"]:
            path = directory / RUNTIME_ROOT / item["path"]
            if path.stat().st_size != item["bytes"] or _sha256(path) != item["sha256"]:
                raise RecoveryError("invalid_capture", f"A captured runtime file was modified: {item['path']}")
        for item in manifest["git"]["logs"]:
            path = directory / LOG_ROOT / item["path"]
            if path.stat().st_size != item["bytes"] or _sha256(path) != item["sha256"]:
                raise RecoveryError("invalid_capture", f"A captured Git log was modified: {item['path']}")
        pack = manifest["git"]["pack"]
        if (directory / PACK).stat().st_size != pack["bytes"] or _sha256(directory / PACK) != pack["sha256"]:
            raise RecoveryError("invalid_capture", "The captured object pack was modified")
        with tempfile.TemporaryDirectory(prefix="qorqut-context-verify-") as scratch:
            repository = Path(scratch) / "repository"
            _materialize_git(directory, manifest, repository, checkout=False)
            _check_closure(repository, manifest)
            _git_content(repository, lambda event: _read_optional(directory / RUNTIME_ROOT / "events" / event / "input.bin"),
                         directory / RUNTIME_ROOT / "pending.json", manifest)
        return summary(manifest, expected_manifest_sha256)
    except RecoveryError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RecoveryError("invalid_capture", f"The capture cannot be read as a complete component: {error}") from error


def _materialize_git(directory, manifest, repository, *, checkout):
    """Build the captured Git state in ``repository`` (a new empty directory): objects, refs, logs, optional checkout."""
    repository.mkdir(mode=0o700, parents=True, exist_ok=True)
    git = _git(repository)
    try:
        git.run("init", "-b", "main")
        git.run("index-pack", "--stdin", "--strict", data=(directory / PACK).read_bytes())
        for item in manifest["git"]["refs"]:
            if _object_types(repository, [item["oid"]]).get(item["oid"]) != item["type"]:
                raise RecoveryError("invalid_capture", f"The captured pack lacks the object of {item['name']}")
            git.run("update-ref", item["name"], item["oid"])
        git.run("symbolic-ref", "HEAD", PUBLIC_REF)
    except ContextError as error:
        if isinstance(error, RecoveryError):
            raise
        raise RecoveryError("invalid_capture", f"The captured objects are not a valid self-contained repository: {error}") from error
    logs = repository / ".git" / "logs"
    shutil.rmtree(logs, ignore_errors=True)
    for item in manifest["git"]["logs"]:
        target = logs / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(directory / LOG_ROOT / item["path"], target)
    if checkout:
        try:
            git.checkout(None, manifest["git"]["checkout_revision"])
        except ContextError as error:
            raise RecoveryError("restore_incomplete", f"The captured checkout cannot be reproduced: {error}") from error


def _check_closure(repository, manifest):
    git = manifest["git"]
    refs = _refs(repository)
    if [{k: item[k] for k in ("name", "oid", "type")} for item in refs] != git["refs"]:
        raise RecoveryError("invalid_capture", "The restored refs differ from the manifest")
    present = _object_types(repository, [item["oid"] for item in git["runtime_only_roots"]])
    for item in git["runtime_only_roots"]:
        if present.get(item["oid"]) != item["type"]:
            raise RecoveryError("object_missing", "An object held only by retained runtime state is missing",
                                details={"object": item["oid"]})
    closure = _closure(repository, _tips(refs, git["runtime_only_roots"]))
    if len(closure) != git["closure"]["objects"] or _closure_digest(closure) != git["closure"]["sha256"]:
        raise RecoveryError("invalid_capture", "The object closure differs from the manifest")


def _git_content(repository, input_bytes, pending_path, manifest):
    """Strict object check, engine event integrity and the pending job's format; read-only."""
    try:
        _git(repository).run("fsck", "--strict", "--no-dangling")
    except ContextError as error:
        raise RecoveryError("invalid_capture", f"The repository failed the strict object check: {error}") from error
    _, snapshot = _snapshot(repository, "The captured history")
    if snapshot.format_version != manifest["component"]["context_format"]:
        raise RecoveryError("invalid_capture", "The manifest's Context format differs from the captured head snapshot")
    _check_outbox(snapshot, pending_path.parent, "invalid_capture")
    pending = manifest["runtime"]["pending"]
    if pending is not None or pending_path.exists():
        try:
            job = json.loads(pending_path.read_bytes())
            _validate_job(job, snapshot, input_bytes)
        except (OSError, ValueError, ContextError) as error:
            if isinstance(error, RecoveryError):
                raise
            raise RecoveryError("invalid_capture", f"The pending job failed the engine's checks: {error}") from error
        if pending is None or (pending["event_id"], pending["phase"], pending["base_revision"]) != (
                job["id"], job.get("phase"), job.get("base_revision")):
            raise RecoveryError("invalid_capture", "The pending job differs from the manifest")


def check_pair(repository, runtime, manifest):
    """Read-only proof that a restored pair is the captured one: ownership, formats, integrity and exact hashes.

    Takes no lock and creates nothing, so it can run before any driver exists. Returns a report.
    """
    repository, runtime = _pair(repository, runtime)
    markers = {"repository": _marker(repository / MARKERS["repository"], "runtime_root", str(runtime), MARKERS["repository"]),
               "runtime": _marker(runtime / MARKERS["runtime"], "context_root", str(repository), MARKERS["runtime"])}
    index_name = Context(repository, runtime).index.path.name
    source = _Source(repository, runtime, index_name, manifest["runtime"]["locks"])
    _head(repository)
    _check_closure(repository, manifest)
    _git_content(repository, lambda event: _read_optional(runtime / "events" / event / "input.bin"),
                 runtime / "pending.json", manifest)
    git = manifest["git"]
    staged = _status(repository)
    if _checkout_revision(repository, git["published_commit"],
                          {"base_revision": git["checkout_revision"]}, staged) != git["checkout_revision"]:
        raise RecoveryError("restore_incomplete", "The checkout differs from the captured checkout revision")
    for item in manifest["runtime"]["files"]:
        path = runtime / item["path"]
        if not path.is_file() or path.stat().st_size != item["bytes"] or _sha256(path) != item["sha256"]:
            raise RecoveryError("restore_incomplete", f"A runtime file differs from the capture: {item['path']}")
    if set(source.of("runtime", "authority")) != {item["path"] for item in manifest["runtime"]["files"]}:
        raise RecoveryError("restore_incomplete", "The runtime holds authority files the capture does not")
    return {"repository": str(repository), "runtime": str(runtime), "published_commit": git["published_commit"],
            "checkout_revision": git["checkout_revision"], "ownership": markers,
            "closure_sha256": git["closure"]["sha256"], "runtime_files": len(manifest["runtime"]["files"])}


def _empty_target(path, label):
    if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
        raise RecoveryError("invalid_target", f"The {label} target must be a new or empty directory")


def _restore_lock(target):
    return target.parent / f".{target.name}.context-restore.lock"


def _runtime_digest(runtime):
    rows = []
    for rel, info in sorted(_entries(runtime, "The staged runtime").items()):
        if stat.S_ISREG(info.st_mode):
            rows.append([rel, info.st_size, _sha256(runtime / rel)])
    return _digest(rows)


class _Restore:
    """One journaled restore; ``checkpoint`` is a no-op seam for focused interruption tests."""

    def __init__(self, directory, manifest, manifest_sha256, repository, runtime):
        self.directory, self.manifest, self.manifest_sha256 = directory, manifest, manifest_sha256
        self.repository, self.runtime = repository, runtime
        self.journal_path = repository.parent / f".{repository.name}.context-restore.json"
        self.staging = {"repository": repository.parent / f".{repository.name}.restoring",
                        "runtime": runtime.parent / f".{runtime.name}.restoring"}
        # One lock per explicit target, keyed only by the path: restores that share either location contend on it.
        self.locks = {"repository": _restore_lock(repository), "runtime": _restore_lock(runtime)}

    def checkpoint(self, phase):
        """Fault-injection seam."""

    def _journal(self):
        if self.journal_path.is_symlink():
            raise RecoveryError("restore_conflict", "The restore journal is a symbolic link; it was left untouched")
        try:
            value = json.loads(self.journal_path.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise RecoveryError("restore_conflict", "The restore journal is unreadable; it was left untouched") from error
        if (not isinstance(value, dict) or value.get("format") != RESTORE_FORMAT
                or value.get("phase") not in PHASES):
            raise RecoveryError("restore_conflict", "The restore journal is not a current-format journal; it was left untouched")
        return value

    def _save(self, journal, phase, **evidence):
        journal["phase"] = phase
        journal["evidence"].update(evidence)
        atomic_write(self.journal_path, encode(journal))

    def _separate(self):
        """Refuse a pair whose capture, targets, journal, locks or staging are, contain or sit inside one another."""
        named = [("capture", self.directory), ("repository", self.repository), ("runtime", self.runtime),
                 ("journal", self.journal_path), ("repository lock", self.locks["repository"]),
                 ("runtime lock", self.locks["runtime"]), ("repository staging", self.staging["repository"]),
                 ("runtime staging", self.staging["runtime"])]
        for index, (first, one) in enumerate(named):
            for second, other in named[index + 1:]:
                if _inside(one, other) or _inside(other, one):
                    raise RecoveryError("invalid_target", f"The restore {first} and {second} locations overlap")

    @contextmanager
    def _locked(self):
        """Hold both target locks, nonblocking and in one stable order, for the whole restore including its final check."""
        with ExitStack() as stack:
            for path in sorted(set(self.locks.values()), key=str):
                try:
                    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                except OSError as error:
                    raise RecoveryError("restore_conflict", "A restore lock location is not usable; it was left untouched") from error
                stack.callback(os.close, descriptor)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise RecoveryError("restore_conflict", "A restore lock location is not a regular file; it was left untouched")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RecoveryError("restore_conflict", "Another restore owns this location", retryable=True) from error
            yield

    def run(self):
        self._separate()
        if not self.journal_path.exists():
            _empty_target(self.repository, "repository")      # refuse before anything is written
            _empty_target(self.runtime, "runtime")
        for path in (self.repository.parent, self.runtime.parent):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._locked():
            return self._run()

    def _identity(self):
        return {"format": RESTORE_FORMAT, "version": VERSION, "manifest_sha256": self.manifest_sha256,
                "target": {"repository": str(self.repository), "runtime": str(self.runtime)},
                "staging": {key: str(path) for key, path in self.staging.items()}}

    def _run(self):
        journal = self._journal()
        identity = self._identity()
        if journal is not None and journal["phase"] == "complete" and not (self.repository.exists() or self.runtime.exists()):
            journal = None  # A finished restore whose targets were since removed on purpose: stale evidence, not an owner.
        if journal is not None and {key: journal.get(key) for key in identity} != identity:
            raise RecoveryError("restore_conflict", "A different capture or target owns this restore location; it was not modified")
        if journal is None:
            _empty_target(self.repository, "repository")
            _empty_target(self.runtime, "runtime")
            # Only a journal of this capture and these targets owns its recorded staging; with none, any entry is foreign.
            present = [str(path) for path in self.staging.values() if os.path.lexists(path)]
            if present:
                raise RecoveryError("restore_conflict", "A restore staging location already exists and this restore does not own it; it was left untouched",
                                    details={"paths": present})
            journal = {**identity, "phase": "started", "evidence": {}}
            self._save(journal, "started")
        self._reconcile(journal)
        if journal["phase"] == "complete":
            return self._finish(journal, resumed=True)
        if journal["phase"] in ("started", "staged", "bound"):
            # Nothing is installed yet and staging is ours alone (named by this journal): rebuild it deterministically.
            for target in (self.repository, self.runtime):
                _empty_target(target, "restore")
            for staging in self.staging.values():
                self._discard(staging)
            self._stage(journal)
            self._install_runtime(journal)
        if journal["phase"] == "runtime_installed":
            self._install_repository(journal)
        return self._finish(journal, resumed=False)

    def _reconcile(self, journal):
        """Account for a stop between an atomic rename and its journal entry; anything else unexpected is refused."""
        evidence = journal["evidence"]
        if journal["phase"] == "bound" and not self.staging["runtime"].exists() and self.runtime.exists():
            if _runtime_digest(self.runtime) != evidence["bound_runtime_sha256"] or not self.staging["repository"].is_dir():
                raise RecoveryError("restore_incomplete", "The installed runtime does not match its journal; the partial restore was left in place")
            self._save(journal, "runtime_installed")
        if journal["phase"] == "runtime_installed" and not self.staging["repository"].exists() and self.repository.exists():
            self._save(journal, "repository_installed")

    @staticmethod
    def _discard(staging):
        if staging.is_symlink() or (os.path.lexists(staging) and not staging.is_dir()):
            raise RecoveryError("restore_conflict", "A restore staging location is not a directory; it was left untouched")
        if staging.exists():
            shutil.rmtree(staging)

    def _stage(self, journal):
        self.checkpoint("before_staging")
        repository, runtime = self.staging["repository"], self.staging["runtime"]
        _materialize_git(self.directory, self.manifest, repository, checkout=True)
        runtime.mkdir(mode=0o700)
        for item in self.manifest["runtime"]["directories"]:
            (runtime / item["path"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        for item in self.manifest["runtime"]["files"]:
            target = runtime / item["path"]
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            size, sha = _copy_stable(self.directory / RUNTIME_ROOT / item["path"], target, item["mode"] or 0o600)
            if (size, sha) != (item["bytes"], item["sha256"]):
                raise RecoveryError("invalid_capture", f"A captured runtime file changed during restore: {item['path']}")
        for item in self.manifest["runtime"]["directories"]:
            os.chmod(runtime / item["path"], item["mode"])
        os.chmod(runtime, 0o700)
        # Content is validated before any ownership marker exists, so nothing here can be opened as a Context.
        _check_closure(repository, self.manifest)
        _git_content(repository, lambda event: _read_optional(runtime / "events" / event / "input.bin"),
                     runtime / "pending.json", self.manifest)
        self._save(journal, "staged", staged_closure_sha256=self.manifest["git"]["closure"]["sha256"])
        self.checkpoint("staged")
        self._bind(journal)

    def _bind(self, journal):
        """The dedicated, validated ownership step: only the two location markers are written, from the explicit targets."""
        source = self.manifest["source"]
        ownership = source["ownership"]
        if (ownership["repository"]["value"] != {"runtime_root": source["runtime"]}
                or ownership["runtime"]["value"] != {"context_root": source["repository"]}):
            raise RecoveryError("invalid_ownership", "The captured ownership pair is not self-consistent")
        if self.repository == self.runtime or _inside(self.repository, self.runtime) or _inside(self.runtime, self.repository):
            raise RecoveryError("invalid_target", "The targets are not a separate repository and runtime")
        repository, runtime = self.staging["repository"], self.staging["runtime"]
        atomic_write(repository / MARKERS["repository"], encode({"runtime_root": str(self.runtime)}))
        atomic_write(runtime / MARKERS["runtime"], encode({"context_root": str(self.repository)}))
        bound = _runtime_digest(runtime)
        if journal["evidence"].setdefault("bound_runtime_sha256", bound) != bound:
            raise RecoveryError("restore_conflict", "Rebuilding the staged runtime did not reproduce its journaled checksum")
        self._save(journal, "bound")
        self.checkpoint("bound")

    def _install_runtime(self, journal):
        runtime = self.staging["runtime"]
        if (not runtime.is_dir() or _runtime_digest(runtime) != journal["evidence"]["bound_runtime_sha256"]
                or not self.staging["repository"].is_dir()):
            raise RecoveryError("restore_incomplete", "The staged restore no longer matches its journal; nothing was installed")
        os.rename(runtime, self.runtime)
        _fsync_directory(self.runtime.parent)
        self.checkpoint("runtime_renamed")
        self._save(journal, "runtime_installed")
        self.checkpoint("runtime_installed")

    def _install_repository(self, journal):
        repository = self.staging["repository"]
        if not self.runtime.is_dir() or _runtime_digest(self.runtime) != journal["evidence"]["bound_runtime_sha256"]:
            raise RecoveryError("restore_incomplete", "The installed runtime no longer matches its journal; the partial restore was left in place")
        if not repository.is_dir():
            raise RecoveryError("restore_incomplete", "The staged repository is missing; the partial restore was left in place")
        os.rename(repository, self.repository)
        _fsync_directory(self.repository.parent)
        self.checkpoint("repository_renamed")
        self._save(journal, "repository_installed")
        self.checkpoint("repository_installed")

    def _finish(self, journal, *, resumed):
        report = check_pair(self.repository, self.runtime, self.manifest)
        if journal["phase"] != "complete":
            self._save(journal, "complete", published_commit=report["published_commit"])
        return {**report, "status": "restored", "resumed": resumed, "manifest_sha256": self.manifest_sha256,
                "pending_event": self.manifest["cutoffs"]["pending_event"], "source_repository": self.manifest["source"]["repository"],
                "source_runtime": self.manifest["source"]["runtime"], "journal": str(self.journal_path)}


def restore(directory, *, target_repository, target_runtime, expected_manifest_sha256, _restore=_Restore):
    """Restore a verified capture into new, explicit repository and runtime locations.

    Offline only: no writer, driver, model, queue, publication or index rebuild is started, and the pair is
    created in staging and installed (runtime first, repository last) only after validation. Both targets
    must be new or empty; they come only from the caller, never from the capture. Both targets are fenced
    by nonblocking locks, held in a stable order until the restored pair has been verified, and the capture,
    targets and every derived journal, lock and staging location must be disjoint. A journal beside the
    repository records phase, checksums and its exact staging so an interrupted restore is either completed
    deterministically or refused; only that journal owns its staging, a staging location it does not own is
    refused untouched, and a different capture or target never overwrites it. There is no force option.
    """
    directory = _location(directory, "capture directory")
    repository, runtime = _pair(target_repository, target_runtime, ("target repository", "target runtime"))
    for target, label in ((repository, "repository"), (runtime, "runtime")):
        if _inside(target, directory) or _inside(directory, target):
            raise RecoveryError("invalid_target", f"The {label} target must be separate from the capture")
    verify(directory, expected_manifest_sha256=expected_manifest_sha256)
    manifest = _manifest_shape(json.loads((directory / MANIFEST).read_bytes()))
    return _restore(directory, manifest, expected_manifest_sha256, repository, runtime).run()
