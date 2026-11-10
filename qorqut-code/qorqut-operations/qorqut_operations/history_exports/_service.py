"""The operational-history snapshot service: explicit initialization, read-only validation, snapshots and status.

Adapted from the pinned v2.2 donor (``qorqut_app.history``). Differences that matter:

* Database rows come from a trusted ``ExportCatalog`` and the authored areas from a trusted area provider, both read
  inside the SAME existing Operations read unit, which is closed before any filesystem or Git work.
* The runtime root is never resolved with ``realpath``: the operations root, ``.runtime`` and the repository are opened
  with ``O_NOFOLLOW`` descriptors from the explicit locations the host supplies and re-checked before every Git command,
  so a replaced runtime location is refused rather than silently followed.
* The host supplies where things are, never this module: ``locations()`` returns the operations root and the history
  repository, and ``resolve_areas(selection)`` places each validated area below that root. Both are called again on every
  use, so the host's own path validation (and its translation of a refused path into a ``DomainError``) runs each time.
* ``complete`` requires both complete file capture and complete table coverage, and ``status()`` distinguishes the
  latest attempt from the latest known complete commit and reports the live ref against what was last recorded.
* A table whose rows have not changed since the last completed snapshot of this object is read and fingerprinted but
  not checked and serialized again: its files are the ones the current history commit already holds. The memory of
  those files is used only for a table whose every remembered file is in that commit with the same content, and it is
  dropped by any pass that does not complete.

Nothing runs on construction. A history failure never rolls back, repeats or approves Operations work: history reads.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import logging
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4

from qorqut_operations.contracts import DomainError

from . import _areas, _state
from ._capture import Capture
from ._fs import DIRECTORY, names_directory, open_directory
from ._git import REF, REGULAR, Git, git_date
from ._tree import MANIFEST, blob_id, manifest_file, table_files
from .catalog import ExportCatalog
from .errors import HistoryCompositionError

__all__ = ["History"]

_LOGGER = logging.getLogger(__name__)
LOCK, STATUS, CACHE = "history.lock", "history-status.json", "history-files.json"
LISTED = 200
CACHE_VERSION = 1


@dataclass
class _Locations:
    """Open descriptors and the exact paths they were opened from. Closed by ``History._locations``."""

    root: int
    runtime: int
    operations: Path
    runtime_path: Path
    repository_name: str

    @property
    def repository(self) -> Path:
        return self.runtime_path / self.repository_name


class _Guard:
    """Called before every Git command: the opened directories must still be the ones the paths name."""

    def __init__(self, locations: _Locations):
        self._locations, self._identity = locations, None

    def __call__(self):
        locations = self._locations
        if not (names_directory(locations.root, locations.operations)
                and names_directory(locations.runtime, locations.runtime_path)):
            raise DomainError("history_incompatible", "The history runtime location was replaced while in use")
        try:
            info = os.stat(locations.repository_name, dir_fd=locations.runtime, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise DomainError("history_incompatible", "The history repository location is not a plain directory")
        identity = (info.st_dev, info.st_ino)
        if self._identity is None:
            self._identity = identity
        elif identity != self._identity:
            raise DomainError("history_incompatible", "The history repository was replaced while in use")


class History:
    """Explicit initialization, validation, snapshots and status for one explicit Operations instance.

    ``database`` is the single Operations ``Database``; ``locations`` a host callable ``() -> (operations_root,
    history_repository)`` of absolute paths that raises ``DomainError("history_unavailable")`` for a location it refuses;
    ``resolve_areas`` a host callable ``(validated areas) -> resolved areas`` returning one ``ResolvedArea`` per selected
    area, in order; ``safe_segment`` the host's validator for one registered ID or slug; ``catalog`` the trusted
    ``ExportCatalog``; ``areas`` the required trusted provider ``areas(uow) -> typed areas`` called inside the export
    read unit; ``clock`` returns the attempt's ISO-8601 UTC time. Nothing touches the filesystem, Git or the database until
    a method is called. Methods are serialized by a file lock in the runtime directory beside the repository.
    """

    def __init__(self, database, *, locations, resolve_areas, safe_segment, catalog, areas, clock, git_runner=None,
                 git_timeout=300):
        if not callable(locations) or not callable(resolve_areas) or not callable(safe_segment):
            raise TypeError("History needs the host's locations, area resolver and safe-segment validator")
        if not isinstance(catalog, ExportCatalog):
            raise TypeError("History needs the trusted ExportCatalog")
        if not callable(areas) or not callable(clock):
            raise TypeError("History needs a trusted area provider and a clock")
        self._database, self._catalog = database, catalog
        self._locate, self._resolve, self._safe_segment = locations, resolve_areas, safe_segment
        self._areas, self._clock = areas, clock
        self._runner, self._timeout = git_runner, git_timeout
        # {table: (fingerprint, {tree path: blob id})} of the last completed snapshot; read and replaced under the lock.
        self._tables: dict = {}

    # -- locations -------------------------------------------------------------------------------------------

    def _place(self):
        """The host's operations root and history repository, asked again on every use (never remembered)."""
        placed = self._locate()
        try:
            operations, repository = placed
            return Path(operations), Path(repository)
        except (TypeError, ValueError) as error:
            raise DomainError("history_unavailable", "The history location is not available",
                              details={"reason": "invalid_location"}) from error

    def _layout(self):
        operations, repository = self._place()
        try:
            relative = repository.relative_to(operations).parts
        except ValueError:
            relative = ()
        if len(relative) != 2:
            raise DomainError("history_unavailable", "The history location is not directly inside the runtime directory")
        return operations, operations / relative[0], relative[1]

    @contextmanager
    def _locations(self, *, create=False):
        """Descriptors for the operations root and its runtime directory, opened without following any link."""
        operations, runtime_path, name = self._layout()
        try:
            root = open_directory(operations)
        except (OSError, ValueError) as error:
            raise DomainError("history_unavailable", "The operations root is not an available directory") from error
        try:
            if create:
                try:
                    os.mkdir(runtime_path.name, 0o700, dir_fd=root)
                except FileExistsError:
                    pass
            try:
                runtime = os.open(runtime_path.name, DIRECTORY, dir_fd=root)
            except FileNotFoundError as error:
                raise DomainError("history_uninitialized", "History has not been initialized") from error
            except OSError as error:
                raise DomainError("history_incompatible", "The runtime location is not a plain directory") from error
            try:
                yield _Locations(root, runtime, operations, runtime_path, name)
            finally:
                os.close(runtime)
        finally:
            os.close(root)

    @contextmanager
    def _lock(self, runtime):
        try:
            descriptor = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=runtime)
        except OSError as error:
            raise DomainError("history_incompatible", "The history lock location is not a plain file") from error
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DomainError("history_busy", "Another history operation is running", retryable=True) from error
            yield
        finally:
            os.close(descriptor)

    def _git(self, locations, guard):
        return Git(locations.repository, runner=self._runner, timeout=self._timeout, guard=guard)

    @staticmethod
    def _present(locations):
        try:
            os.stat(locations.repository_name, dir_fd=locations.runtime, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    # -- initialization and validation -----------------------------------------------------------------------

    def initialize(self):
        """Create the private history repository when it is absent, or validate the existing one without repairing it.

        The repository is built and checked beside its final name and then moved into place, so an interrupted creation
        never leaves a half-made history. An incompatible location is refused untouched, and nothing is cleaned up. No
        snapshot is taken."""
        with self._locations(create=True) as locations, self._lock(locations.runtime):
            guard = _Guard(locations)
            created = not self._present(locations)
            if created:
                self._create(locations, guard)
            else:
                self._git(locations, guard).validate()
            if self._stored(locations.runtime)[0] is None:
                self._write_status(locations.runtime, {"state": "ready", "recorded_commit": None})
        report = self.status()
        report["created"] = created
        return report

    def _create(self, locations, guard):
        temporary = f".history-init-{uuid4().hex}"
        os.mkdir(temporary, 0o700, dir_fd=locations.runtime)
        try:
            git = Git(locations.runtime_path / temporary, runner=self._runner, timeout=self._timeout, guard=guard)
            git.create()
            git.validate()
            os.rename(temporary, locations.repository_name, src_dir_fd=locations.runtime, dst_dir_fd=locations.runtime)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True, dir_fd=locations.runtime)
            raise

    def validate(self):
        """Read-only check that the existing repository is exactly the compatible private history; returns ``status()``.

        Takes no lock, creates and repairs nothing, and raises ``history_uninitialized``/``history_incompatible``/
        ``history_unavailable`` rather than touching an unexpected location."""
        with self._locations() as locations:
            if not self._present(locations):
                raise DomainError("history_uninitialized", "History has not been initialized")
            self._git(locations, _Guard(locations)).validate()
        report = self.status()
        report["valid"] = True
        return report

    # -- status ----------------------------------------------------------------------------------------------

    @staticmethod
    def _stored(runtime):
        try:
            return _state.read_json(runtime, STATUS), None
        except OSError:
            return None, "unreadable"

    def status(self):
        """What the last operations recorded plus the live ref observation; never raises on Git or status trouble.

        ``last_attempt`` is the latest snapshot attempt; ``last_complete`` is the latest known complete commit and time.
        ``observation`` compares the live history ref with the commit the status last recorded, so a commit whose advisory
        status could not be saved, or a ref moved by another writer, is reported rather than hidden."""
        report = {"state": "uninitialized", "initialized": False, "repository": None, "ref": REF, "commit": None,
                  "last_attempt": None, "last_complete": None, "consecutive_failures": 0, "last_snapshot": None,
                  "error": None, "observation": {"ref_commit": None, "recorded_commit": None, "ref_error": None,
                                                 "status_error": None, "consistent": True}}
        try:
            report["repository"] = str(self._place()[1])
        except DomainError as error:
            report.update(state="unavailable", error={"code": error.code})
            return report
        try:
            with self._locations() as locations:
                stored, status_error = self._stored(locations.runtime)
                if not self._present(locations):
                    return report
                head, ref_error = None, None
                try:
                    head = self._git(locations, _Guard(locations)).head()
                except DomainError as error:
                    ref_error = error.code
        except DomainError as error:
            if error.code != "history_uninitialized":
                report.update(state="unavailable", error={"code": error.code})
            return report
        stored = stored or {}
        recorded = stored.get("recorded_commit")
        report.update({key: stored.get(key) for key in ("last_attempt", "last_complete", "last_snapshot", "error")})
        report.update(initialized=True, state=stored.get("state", "ready"), commit=head,
                      consecutive_failures=stored.get("consecutive_failures", 0))
        consistent = ref_error is None and status_error is None and head == recorded
        report["observation"] = {"ref_commit": head, "recorded_commit": recorded, "ref_error": ref_error,
                                 "status_error": status_error, "consistent": consistent}
        if not consistent:
            report["observation"]["note"] = (
                "The history ref differs from the last recorded status, or it could not be observed: a snapshot may have "
                "committed without its advisory status being saved, or another writer moved the ref.")
        return report

    def _write_status(self, runtime, values):
        """Status is advisory: failing to save it cannot undo a committed snapshot. Returns whether it was saved."""
        try:
            _state.write_json(runtime, STATUS, values)
        except OSError:
            _LOGGER.error("History status could not be saved")
            return False
        return True

    # -- snapshots -------------------------------------------------------------------------------------------

    def snapshot(self):
        """Version the current exported rows and the registered authored areas; returns the snapshot summary.

        An unchanged tree creates no commit. Uncapturable or concurrently changed paths keep their previously saved
        version and are listed as incomplete and retryable; an unclassified table is listed as missing coverage. Failure
        raises a ``DomainError`` after recording it in ``status()``; accepted Operations records are never touched."""
        with self._locations() as locations, self._lock(locations.runtime):
            if not self._present(locations):
                raise DomainError("history_uninitialized", "History has not been initialized")
            attempt = self._clock()
            guard = _Guard(locations)
            try:
                summary = self._snapshot(locations, guard, attempt)
            except DomainError as error:
                self._record_failure(locations, attempt, error)
                raise
            except HistoryCompositionError as error:
                failure = DomainError("history_composition_invalid", "The history export composition is invalid",
                                      details={"message": str(error)[:200]})
                self._record_failure(locations, attempt, failure)
                raise failure from error
            except Exception as error:
                failure = DomainError("history_failed", "History snapshot failed unexpectedly", retryable=True,
                                      details={"type": type(error).__name__, "message": str(error)[:200]})
                self._record_failure(locations, attempt, failure)
                raise failure from error
            prior = self._stored(locations.runtime)[0] or {}
            complete = summary["complete"]
            recorded = self._write_status(locations.runtime, {
                "state": "ok" if complete else "incomplete", "recorded_commit": summary["commit"],
                "last_attempt": {"at": attempt, "outcome": "complete" if complete else "incomplete",
                                 "commit": summary["commit"], "error": None},
                "last_complete": {"at": attempt, "commit": summary["commit"]} if complete else prior.get("last_complete"),
                "consecutive_failures": 0, "last_snapshot": summary, "error": None})
            return {**summary, "status_recorded": recorded}

    def _read_database(self, held=None):
        """One consistent read: exported rows and area selections together; the unit is closed on return."""
        with self._database.read() as uow:
            exported = self._catalog.export(uow, held)
            returned = self._areas(uow)
            try:
                iterator = iter(returned)
            except TypeError:
                raise _areas.invalid("The area provider must return an iterable of typed areas") from None
            selection = tuple(iterator)  # consumed here, inside the same read unit
        return exported, _areas.validate(selection, self._safe_segment)

    def _snapshot(self, locations, guard, started):
        git = self._git(locations, guard)
        git.validate()
        base = git.head()
        known = git.entries(base)
        # What the last completed snapshot remembers is forgotten here and kept again only if this pass completes. A
        # table is offered as held only while the commit this pass builds on has each of its files with that content.
        remembered, self._tables = self._tables, {}
        held = {name: fingerprint for name, (fingerprint, paths) in remembered.items()
                if all(known.get(path) == (REGULAR, oid) for path, oid in paths.items())}
        exported, selection = self._read_database(held)
        areas = _areas.check_resolved(selection, self._resolve(selection))
        saved = self._cache(locations.runtime)
        capture = Capture(git, known, saved.get("files", {}) if saved.get("version") == CACHE_VERSION else {},
                          excluded=frozenset(path for item in areas for path in item.excluded))
        for item in areas:
            if item.parts is None:
                capture.exclude_area(item.label, item.blocked)
            else:
                capture.scan_area(locations.root, item.parts)

        entries, tables = {}, {}

        def store(path, data):
            oid = blob_id(data)
            if known.get(path) != (REGULAR, oid):
                git.write_blob(data=data)
            entries[path] = (REGULAR, oid)
            return oid

        for name, table in exported.tables.items():
            paths = {path: store(path, data) for path, data in table_files(table).items()}
            if table.fingerprint is not None:
                tables[name] = (table.fingerprint, paths)
        for name, table in exported.unchanged.items():
            tables[name] = remembered[name]
            entries.update({path: (REGULAR, oid) for path, oid in remembered[name][1].items()})
        store(MANIFEST, manifest_file(exported))
        entries.update(capture.finish())

        guard()
        tree = git.build_tree(entries, locations.runtime_path)
        changed = base is None or tree != git.tree_of(base)
        commit = base
        rows = sum(exported.counts().values())
        if changed:
            omitted = set(entries) - set(git.entries(tree))
            if omitted:
                raise DomainError("history_git_failed", "Git omitted paths from the history tree", retryable=True,
                                  details={"omitted": sorted(omitted)[:5], "count": len(omitted)})
            lines = [f"Qorqut operations history snapshot {started}", "", f"Database rows: {rows}",
                     f"Files: {capture.files} ({capture.bytes} bytes)"]
            if exported.missing:
                lines.append(f"Incomplete table coverage: {len(exported.missing)} unclassified tables")
            if capture.incomplete:
                lines.append(f"Incomplete capture: {len(capture.incomplete)} paths not captured this time")
            commit = git.commit(tree, base, "\n".join(lines) + "\n", git_date(started))
            git.advance(commit, base)
        try:
            git.pack_if_needed()
        except DomainError:
            # The snapshot is committed already. Maintenance failure must not
            # misreport that durable snapshot as lost; retry on a later pass.
            _LOGGER.exception("History packing failed; the snapshot is retained")
        try:
            _state.write_json(locations.runtime, CACHE, {"version": CACHE_VERSION, "files": capture.fresh})
        except OSError:
            _LOGGER.error("History file cache could not be saved; the next snapshot rereads files")
        self._tables = tables
        files_complete = not capture.incomplete
        return {
            "changed": changed, "commit": commit, "previous_commit": base, "tree": tree,
            "complete": files_complete and exported.coverage_complete, "files_complete": files_complete,
            "coverage_complete": exported.coverage_complete, "retryable": bool(capture.incomplete),
            "captured_at": started,
            "format": {"name": exported.format, "version": exported.version},
            "schema": {"name": exported.schema.name, "version": exported.schema.version, "digest": exported.schema.digest},
            "rows": exported.counts(),
            "coverage": {"complete": exported.coverage_complete, "missing": list(exported.missing[:LISTED]),
                         "missing_total": len(exported.missing)},
            "areas": [item.label for item in areas],
            "files": {"included": capture.files, "bytes": capture.bytes},
            "exclusions": [{"path": path, "reason": reason} for path, reason in capture.exclusions[:LISTED]],
            "exclusions_total": len(capture.exclusions),
            "incomplete": [{"path": path, "reason": reason} for path, reason in capture.incomplete[:LISTED]],
            "incomplete_total": len(capture.incomplete),
        }

    @staticmethod
    def _cache(runtime):
        try:
            return _state.read_json(runtime, CACHE) or {}
        except OSError:
            return {}

    def _record_failure(self, locations, attempt, error):
        prior = self._stored(locations.runtime)[0] or {}
        failure = {**error.to_dict(), "at": attempt}
        self._write_status(locations.runtime, {
            "state": "failed", "recorded_commit": prior.get("recorded_commit"),
            "last_attempt": {"at": attempt, "outcome": "failed", "commit": prior.get("recorded_commit"), "error": failure},
            "last_complete": prior.get("last_complete"), "last_snapshot": prior.get("last_snapshot"),
            "consecutive_failures": prior.get("consecutive_failures", 0) + 1, "error": failure})
