"""Generic helpers of the baseline-resumption runner: import-origin control, closed-checkpoint snapshots and read-only
readers of persisted state.

Nothing here imports the host, Operations or Context. The readers below use plain read-only SQLite and Git against the
stored contract (table and column names, Git refs), so the same evidence is gathered from a baseline checkpoint and from
the state a candidate leaves behind, independent of how either code base is organised internally.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile

# The first-party import roots whose origin is recorded and checked. A candidate run must name where these come from.
TRACKED_MODULES = ("bazarlyk", "qorqut_operations", "qorqut_context", "qorqut_business_docs_versioning")

# Stored layout of a fixture, relative to ``<fixture>/instance`` (the instance_paths contract; unchanged by the split).
DATABASE = "operations/.runtime/operations.sqlite3"
OPERATIONS_ROOT = "operations"
CONTEXT_REPOSITORY = "operations/context-graph"
TABLE_HISTORY_REF = "refs/heads/qorqut-tables"


# -- canonical data ------------------------------------------------------------------------------------------------------

def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def digest(value) -> str:
    return sha256_bytes(canonical(value))


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, default=str)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def read_json(path: Path):
    with open(path) as stream:
        return json.load(stream)


# -- the import environment -----------------------------------------------------------------------------------------------

def configure_imports(code_paths) -> None:
    """Put the explicit import roots first, in the given order. Without them the interpreter's own packages decide, which
    in the owned minimal environment holds third-party libraries only, so first-party code is never found by accident."""
    for path in reversed([str(Path(item).resolve()) for item in code_paths]):
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)


def import_origins() -> dict:
    """Where each tracked first-party package is imported from (its package directory), or ``None`` when absent."""
    result = {}
    for name in TRACKED_MODULES:
        module = sys.modules.get(name)
        if module is not None and getattr(module, "__file__", None):
            result[name] = str(Path(module.__file__).resolve().parent)
            continue
        spec = importlib.util.find_spec(name)
        result[name] = str(Path(spec.origin).resolve().parent) if spec and spec.origin else None
    return result


def origin_problems(origins: dict, expect_under=(), forbid_under=()) -> list:
    """Why the import origins are not the intended ones: a tracked package under a forbidden root (the baseline source for a
    candidate run), or not under any expected root."""
    expect = [Path(item).resolve() for item in expect_under]
    forbid = [Path(item).resolve() for item in forbid_under]
    problems = []
    for name, origin in origins.items():
        if origin is None:
            if name != "qorqut_business_docs_versioning":
                problems.append(f"{name} is not importable")
            continue
        location = Path(origin)
        for root in forbid:
            if location == root or root in location.parents:
                problems.append(f"{name} is imported from the forbidden root {root}: {origin}")
        if expect and not any(location == root or root in location.parents for root in expect):
            problems.append(f"{name} is imported from {origin}, which is under none of the expected roots")
    return problems


def installed_distributions() -> dict:
    """First-party distributions visible to this interpreter (name -> version), from installed metadata only."""
    from importlib import metadata

    wanted = ("qorqut", "bazarlyk")
    found = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"] or ""
        if name.lower().startswith(wanted):
            found[name] = distribution.version
    return found


def git_pin(directory) -> dict:
    """The recorded Git pin of a source directory: HEAD commit, tree and whether the working tree is clean."""
    directory = str(directory)
    try:
        head = _git(directory, "rev-parse", "HEAD")
        tree = _git(directory, "rev-parse", "HEAD^{tree}")
        dirty = bool(_git(directory, "status", "--porcelain", "--untracked-files=no"))
        branch = _git(directory, "branch", "--show-current")
    except (OSError, subprocess.CalledProcessError):
        return {"directory": directory, "pinned": False}
    return {"directory": directory, "pinned": True, "head": head, "tree": tree, "branch": branch, "clean": not dirty}


# -- Git (read-only, hook- and config-free) ------------------------------------------------------------------------------

def _environment() -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", LC_ALL="C")
    return env


def _git(directory, *args, data=None, env=None, check=True) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(directory), *args],
                            input=data, capture_output=True, env=env or _environment(), check=check)
    return result.stdout.decode().strip()


def git(directory, *args, **options) -> str:
    return _git(directory, *args, **options)


def git_bytes(directory, *args) -> bytes:
    """Raw standard output of a read-only Git command (blob bytes must not be decoded or stripped)."""
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(directory), *args],
                            capture_output=True, env=_environment(), check=True)
    return result.stdout


def git_log_parents(repository, ref) -> dict:
    """``{commit: [parent commits]}`` for everything reachable from ``ref`` (empty when the ref does not exist)."""
    output = _git(repository, "log", "--format=%H %P", ref, check=False)
    return {fields[0]: fields[1:] for fields in (line.split() for line in output.splitlines()) if fields}


def git_commit_content(repository, commit) -> dict:
    """The tree content of one commit as ``{path: {mode, bytes, sha256 of the blob bytes, json_digest}}``.

    ``json_digest`` is the canonical digest of the blob parsed as JSON (``None`` when it is not JSON), so published content can
    be compared with a stored JSON snapshot independent of how either side formatted its bytes."""
    files = {}
    for entry in git_bytes(repository, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, oid = meta.decode().split()
        if kind != "blob":
            files[path.decode()] = {"mode": mode, "kind": kind, "oid": oid}
            continue
        data = git_bytes(repository, "cat-file", "blob", oid)
        try:
            parsed = digest(json.loads(data))
        except ValueError:
            parsed = None
        files[path.decode()] = {"mode": mode, "bytes": len(data), "sha256": sha256_bytes(data), "json_digest": parsed}
    return files


def git_refs(repository) -> dict:
    """Every ref of a repository and the commit it names; empty for a directory that is not a repository."""
    if not (Path(repository) / ".git").exists() and not (Path(repository) / "HEAD").exists():
        return {}
    output = _git(repository, "for-each-ref", "--format=%(refname) %(objectname)", check=False)
    return dict(line.split(" ", 1) for line in output.splitlines() if line)


def git_commits(repository, ref) -> list:
    """The commits reachable from ``ref`` (newest first) as ``[id, subject]``; empty when the ref does not exist."""
    output = _git(repository, "log", "--format=%H %s", ref, check=False)
    return [line.split(" ", 1) for line in output.splitlines() if line]


# -- read-only SQLite --------------------------------------------------------------------------------------------------------

class ReadOnlyDatabase:
    """A read-only view of a fixture database; it never creates, migrates or checkpoints anything."""

    def __init__(self, path, *, closed=True):
        """``closed=True`` reads a checkpoint nobody writes: ``immutable=1`` so not even a ``-shm`` file is created beside it
        (a closed checkpoint must stay byte-identical), and a non-empty ``-wal`` file is refused as an unclean close. A live
        database (``closed=False``) is read through an ordinary read-only connection."""
        self.path = Path(path)
        if closed:
            wal = Path(str(self.path) + "-wal")
            if wal.exists() and wal.stat().st_size:
                raise RuntimeError(f"{self.path} has unmerged write-ahead log content; it was not closed cleanly")
            uri = f"file:{self.path}?mode=ro&immutable=1"
        else:
            uri = f"file:{self.path}?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True)
        self.connection.row_factory = sqlite3.Row

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def rows(self, sql, *parameters) -> list:
        result = []
        for row in self.connection.execute(sql, parameters):
            item = dict(row)
            for name, value in list(item.items()):
                if name.endswith("_json") and isinstance(value, str):
                    try:
                        item[name] = json.loads(value)
                    except ValueError:
                        pass
                elif isinstance(value, bytes):
                    item[name] = {"sha256": sha256_bytes(value), "bytes": len(value)}
            result.append(item)
        return result

    def one(self, sql, *parameters):
        found = self.rows(sql, *parameters)
        return found[0] if found else None

    def count(self, table, where="1", *parameters) -> int:
        return self.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", parameters).fetchone()[0]

    def tables(self) -> set:
        return {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def open_database(fixture: Path, *, closed=True) -> ReadOnlyDatabase:
    return ReadOnlyDatabase(Path(fixture) / "instance" / DATABASE, closed=closed)


def row_fingerprints(rows, columns) -> dict:
    """``{id: sha256(canonical selected columns)}`` for rows keyed by ``id``: the immutable part of saved work."""
    return {str(row["id"]): digest({name: row.get(name) for name in columns}) for row in rows}


# -- closed checkpoints ------------------------------------------------------------------------------------------------------

def tree_manifest(root: Path) -> list:
    """Every entry below ``root`` as ``[relative path, kind, mode, size, sha256-or-link-target]``, sorted. Modification times
    are deliberately not part of it: identity is the bytes, the modes and the layout."""
    root = Path(root)
    entries = []
    for current, directories, files in os.walk(root):
        directories.sort()
        for name in sorted(directories + files):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            mode = oct(info.st_mode & 0o7777)
            if path.is_symlink():
                entries.append([relative, "l", mode, 0, os.readlink(path)])
            elif path.is_dir():
                entries.append([relative, "d", mode, 0, ""])
            else:
                entries.append([relative, "f", mode, info.st_size, sha256_file(path)])
    return sorted(entries)


def snapshot_tree(root: Path, archive: Path) -> dict:
    """Write the exact content of ``root`` to ``archive`` (a tar file that is never rewritten) and describe it."""
    root, archive = Path(root), Path(archive)
    if archive.exists():
        raise FileExistsError(f"{archive} already exists; a closed checkpoint is never overwritten")
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest = tree_manifest(root)
    temporary = archive.with_suffix(".partial")
    with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as handle:
        handle.add(root, arcname=".")
    os.replace(temporary, archive)
    return {"archive": str(archive), "archive_sha256": sha256_file(archive), "entries": len(manifest),
            "manifest_sha256": digest(manifest), "manifest": manifest}


def restore_tree(archive: Path, root: Path, *, within: Path, expect: dict) -> dict:
    """Replace ``root`` by the content of the closed ``archive`` so every stored absolute path is valid again.

    ``root`` must lie below ``within`` (the fixtures directory). The archive must still have the recorded checksum, and the
    restored tree must equal the recorded manifest, or this raises and the caller must not continue."""
    archive, root, within = Path(archive), Path(root).absolute(), Path(within).absolute()
    if within not in root.parents:
        raise ValueError(f"refusing to restore {root}: it is not below {within}")
    if sha256_file(archive) != expect["archive_sha256"]:
        raise ValueError(f"{archive} no longer has its recorded checksum")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    with tarfile.open(archive) as handle:
        # Our own archive, whose checksum was verified above: "fully_trusted" keeps the exact modes (the "tar" filter would clear
        # group/other write bits and so change the closed checkpoint); the manifest comparison below proves the result.
        handle.extractall(root, filter="fully_trusted")
    manifest_sha256 = digest(tree_manifest(root))
    if manifest_sha256 != expect["manifest_sha256"]:
        raise ValueError(f"the restored tree {root} differs from the closed checkpoint manifest")
    return {"restored": str(root), "manifest_sha256": manifest_sha256, "archive_sha256": expect["archive_sha256"]}


def tracked_directory(root: Path) -> dict:
    """Digest summary of a directory's files (relative path -> sha256), for before/after comparison of authored side files."""
    root = Path(root)
    if not root.is_dir():
        return {}
    return {path.relative_to(root).as_posix(): sha256_file(path) for path in sorted(root.rglob("*")) if path.is_file()}


def file_state(path: Path) -> dict | None:
    path = Path(path)
    if not path.is_file():
        return None
    return {"sha256": sha256_file(path), "bytes": path.stat().st_size}


# -- evidence values ---------------------------------------------------------------------------------------------------------

def jsonable(value):
    """A JSON-safe copy of a result value: mappings, sequences, sets, results with ``to_dict``/``__dict__`` and paths."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"sha256": sha256_bytes(value), "bytes": len(value)}
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "items"):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((jsonable(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if hasattr(value, "to_dict"):
        return jsonable(value.to_dict())
    if hasattr(value, "__dict__"):
        return {key: jsonable(item) for key, item in vars(value).items() if not key.startswith("_")}
    return str(value)


UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
OID = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
# ``context_event_id`` is drawn at random by the Context engine for every published event.
VOLATILE_KEYS = frozenset({"claimed_by", "lease_until", "instance_id", "at", "created_at", "updated_at", "request_id", "elapsed",
                           "context_event_id"})


def scrub(value, known_ids=frozenset(), *, oids=True):
    """Remove what legitimately differs between two runs of the same continuation: timestamps, values generated during the run
    (identifiers that did not exist in the checkpoint, and by default Git object names, which carry the run's own times)."""
    if isinstance(value, dict):
        return {key: scrub(item, known_ids, oids=oids) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [scrub(item, known_ids, oids=oids) for item in value]
    if isinstance(value, str):
        if UUID.match(value):
            return value if value in known_ids else "<new-id>"
        text = TIMESTAMP.sub("<time>", value)
        return OID.sub("<git-oid>", text) if oids else text
    return value
