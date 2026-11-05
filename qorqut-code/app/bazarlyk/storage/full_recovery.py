"""Encrypted, installation-wide recovery generations.

This module deliberately has no service integration.  A trusted runtime owns
the quiescence context: entering it means that every business writer, browser,
agent terminal, and service writer has actually stopped.  The recovery store
cannot infer that state from a flag, a PID, or an SQLite transaction.
"""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Protocol
from uuid import UUID

from qorqut_operations.contracts import DomainError, utc_now
from bazarlyk.migration.snapshots import _fsync_directory, restore_snapshot, verify_snapshot
from qorqut_context.integration.recovery import CAPTURE_DIRECTORY, ContextRecovery
from bazarlyk.storage.recovery import BackupStore, require_target_businesses


RESTIC_VERSION = "0.19.1"
_FULL_TAG = "qorqut-full-recovery"
_GENERATION_TAG = "qorqut-generation:"
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class TrustedQuiescence(Protocol):
    """A caller-owned context whose body starts only after all writers drain."""

    def __enter__(self) -> object: ...
    def __exit__(self, exc_type, exc_value, traceback) -> bool | None: ...


def _uuid(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise DomainError("validation_error", "Recovery generation requires a canonical UUID") from exc
    return value


def _absolute(value: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _no_symlink_ancestors(path: Path) -> None:
    current = path
    while True:
        try:
            if current.is_symlink():
                raise DomainError("invalid_configuration", "Recovery paths cannot cross symbolic links")
        except OSError as exc:
            raise DomainError("invalid_configuration", "Recovery path cannot be inspected") from exc
        if current.parent == current:
            return
        current = current.parent


_GIT_METADATA_LIMIT = 4096


def _git_metadata_line(path: Path, *, description: str) -> str:
    """Read a tiny Git control file without following a replacement symlink."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, ValueError) as exc:
        raise DomainError("backup_incomplete", f"{description} cannot be read") from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            data = stream.read(_GIT_METADATA_LIMIT + 1)
    except OSError as exc:
        raise DomainError("backup_incomplete", f"{description} cannot be read") from exc
    if not stat.S_ISREG(mode) or len(data) > _GIT_METADATA_LIMIT:
        raise DomainError("backup_incomplete", f"{description} is invalid")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DomainError("backup_incomplete", f"{description} is invalid") from exc
    if not text.endswith("\n") or "\n" in text[:-1] or not text[:-1]:
        raise DomainError("backup_incomplete", f"{description} is invalid")
    return text[:-1]


def _contained_git_path(value: str, *, base: Path, root_git: Path, description: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = Path(os.path.normpath(os.path.abspath(os.fspath(candidate))))
    try:
        relative = candidate.relative_to(root_git)
    except ValueError as exc:
        raise DomainError("backup_incomplete", f"{description} points outside the installation") from exc
    current = root_git
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise DomainError("backup_incomplete", f"{description} is missing") from exc
        if stat.S_ISLNK(mode):
            raise DomainError("backup_incomplete", f"{description} crosses a symbolic link")
    try:
        mode = candidate.lstat().st_mode
    except OSError as exc:
        raise DomainError("backup_incomplete", f"{description} is missing") from exc
    if not stat.S_ISDIR(mode):
        raise DomainError("backup_incomplete", f"{description} is not a contained directory")
    return candidate


def _validate_linked_gitdir(git_file: Path, root_git: Path) -> None:
    line = _git_metadata_line(git_file, description=instruction_text('full-recovery-registered-hq-git-metadata'))
    if not line.startswith("gitdir: "):
        raise DomainError("backup_incomplete", "Registered HQ Git metadata is invalid")
    gitdir = _contained_git_path(line.removeprefix("gitdir: "), base=git_file.parent,
                                 root_git=root_git, description=instruction_text('full-recovery-registered-hq-git-directory'))
    common = gitdir / "commondir"
    if common.exists() or common.is_symlink():
        common_dir = _contained_git_path(
            _git_metadata_line(common, description=instruction_text('full-recovery-registered-hq-git-common-directory')), base=gitdir,
            root_git=root_git, description=instruction_text('full-recovery-registered-hq-git-common-directory'))
        if common_dir != root_git:
            raise DomainError("backup_incomplete", "Registered HQ Git common directory is not the installation Git directory")


def _regular_secret_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise DomainError("invalid_configuration", "Restic password file is unavailable") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise DomainError("invalid_configuration", "Restic password file must be an ordinary file")
    if mode & 0o077:
        raise DomainError("invalid_configuration", "Restic password file must not be group or world readable")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return sha256(stream.read()).hexdigest()


def _restic_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise DomainError("invalid_configuration", "Recovery clock must return an ISO-8601 timestamp with a timezone") from exc
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class ResticBinary:
    """A verified, pinned restic executable provisioned by trusted operations."""

    executable: Path | str
    sha256: str

    def __post_init__(self) -> None:
        executable = _absolute(self.executable)
        if not _HASH.fullmatch(self.sha256):
            raise DomainError("invalid_configuration", "Restic binary checksum must be a SHA-256 digest")
        object.__setattr__(self, "executable", executable)
        self.verify()

    def verify(self) -> None:
        """Hash immediately before each launch; do not trust a prior path check."""
        _no_symlink_ancestors(self.executable)
        try:
            mode = self.executable.lstat().st_mode
        except OSError as exc:
            raise DomainError("invalid_configuration", "Pinned restic executable is unavailable") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or not mode & stat.S_IXUSR:
            raise DomainError("invalid_configuration", "Pinned restic executable must be an executable regular file")
        if _digest(self.executable) != self.sha256:
            raise DomainError("invalid_configuration", "Pinned restic executable checksum does not match")


class _Restic:
    def __init__(self, binary: ResticBinary, repository: Path, password_file: Path, *, timeout_seconds: int, installation_tag: str):
        self.binary, self.repository, self.password_file = binary, repository, password_file
        self.timeout_seconds = timeout_seconds
        self.installation_tag = installation_tag
        self._checked_binary = False

    def _run(self, arguments: list[str], *, json_output: bool = False) -> str:
        self.binary.verify()
        _no_symlink_ancestors(self.password_file)
        _regular_secret_file(self.password_file)
        if not self._checked_binary:
            self._check_binary()
        command = [str(self.binary.executable), "--repo", str(self.repository), "--password-file", str(self.password_file)]
        if json_output:
            command.append("--json")
        command.extend(arguments)
        cache = Path(tempfile.mkdtemp(prefix=".restic-cache-"))
        try:
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8", errors="replace", check=False,
                                       env={"PATH": os.defpath, "RESTIC_CACHE_DIR": str(cache)}, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            raise DomainError("backup_incomplete", "Encrypted recovery repository command timed out", retryable=True) from None
        except OSError as exc:
            raise DomainError("backup_incomplete", "Pinned restic command could not run", retryable=True) from exc
        finally:
            shutil.rmtree(cache, ignore_errors=True)
        if completed.returncode:
            raise DomainError("invalid_backup", "Encrypted recovery repository command failed")
        return completed.stdout

    def _check_binary(self) -> None:
        self.binary.verify()
        try:
            completed = subprocess.run([str(self.binary.executable), "version"], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                       errors="replace", check=False, env={"PATH": os.defpath}, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise DomainError("invalid_configuration", "Pinned restic version check timed out") from exc
        except OSError as exc:
            raise DomainError("invalid_configuration", "Pinned restic command could not run") from exc
        if completed.returncode or not completed.stdout.startswith("restic " + RESTIC_VERSION + " "):
            raise DomainError("invalid_configuration", "Pinned restic version does not match the recovery contract")
        self._checked_binary = True

    def initialized(self) -> bool:
        return (self.repository / "config").is_file()

    def init(self) -> None:
        self._run(["init"])

    def snapshots(self, generation: str | None = None) -> list[dict]:
        tags = [_FULL_TAG, self.installation_tag]
        if generation:
            tags.append(_GENERATION_TAG + generation)
        output = self._run(["snapshots", "--tag", ",".join(tags)], json_output=True)
        try:
            result = json.loads(output)
        except (TypeError, ValueError) as exc:
            raise DomainError("invalid_backup", "Encrypted recovery repository returned an invalid snapshot list") from exc
        if not isinstance(result, list):
            raise DomainError("invalid_backup", "Encrypted recovery repository returned an invalid snapshot list")
        return result

    def backup(self, paths: list[Path], generation: str, timestamp: str, *, excludes: tuple[str, ...]) -> None:
        self._run(["backup", "--host", "qorqut-full-installation", "--tag", _FULL_TAG, "--tag", self.installation_tag,
                   "--tag", _GENERATION_TAG + generation, "--time", timestamp,
                   *[item for pattern in excludes for item in ("--exclude", pattern)], *[str(path) for path in paths]])

    def verify(self) -> None:
        # This reads all data so a successful generation is a real encrypted-repository verification.
        self._run(["check", "--read-data-subset=100%"])

    def restore(self, snapshot_id: str, target: Path) -> None:
        self._run(["restore", snapshot_id, "--target", str(target), "--overwrite", "never"])

    def forget(self, *, daily: int, weekly: int) -> None:
        self._run(["forget", "--tag", _FULL_TAG + "," + self.installation_tag, "--group-by", "host",
                   "--keep-daily", str(daily), "--keep-weekly", str(weekly), "--prune"])


@dataclass(frozen=True)
class FullRecoveryGeneration:
    generation_id: str
    snapshot_id: str
    created_at: str
    source_root: str
    sources: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"generation_id": self.generation_id, "snapshot_id": self.snapshot_id, "created_at": self.created_at,
                "source_root": self.source_root, "sources": list(self.sources), "state": "complete"}


class FullRecoveryStore:
    """Compose verified authority snapshots with a restic-encrypted full installation snapshot.

    It only supports an all-installation generation.  It never restores a business
    independently, because the SQLite authority covers every registered business.
    ``restore`` is deliberately a closed-gate rehearsal into a separate empty root;
    activating an original-path recovery is an external all-installation runbook.
    Optional installation Git metadata preserves linked-worktree control data; root
    working-tree bytes remain limited to BackupStore's registered authority inventory.
    """

    def __init__(self, database, paths, *, repository, password_file, authority_staging, restic: ResticBinary,
                 initialize_repository: bool = False, command_timeout_seconds: int = 1800, clock=utc_now):
        self.database = database
        self.paths = paths
        self.root = _absolute(paths.operations)
        if self.root.is_symlink() or self.root.exists() and not self.root.is_dir():
            raise DomainError("invalid_configuration", "Operations root must be an ordinary directory")
        self.repository = _absolute(repository)
        self.password_file = _absolute(password_file)
        self.authority_staging = _absolute(authority_staging)
        self.initialize_repository = initialize_repository
        if type(command_timeout_seconds) is not int or not 1 <= command_timeout_seconds <= 3600:
            raise DomainError("invalid_configuration", "Restic command timeout must be between 1 and 3600 seconds")
        self.command_timeout_seconds = command_timeout_seconds
        self._repository_preexisted = self.repository.exists()
        for path in (self.root, self.repository, self.password_file, self.authority_staging):
            _no_symlink_ancestors(path)
        if any(_within(path, self.root) or _within(self.root, path)
               for path in (self.repository, self.password_file, self.authority_staging)):
            raise DomainError("invalid_configuration", "Recovery repository, password file, and staging must be outside business mounts")
        if _within(self.authority_staging, self.repository) or _within(self.repository, self.authority_staging):
            raise DomainError("invalid_configuration", "Authority staging must not overlap the encrypted repository")
        if _within(self.password_file, self.repository) or _within(self.repository, self.password_file):
            raise DomainError("invalid_configuration", "Restic password file must not overlap the encrypted repository")
        if _within(self.password_file, self.authority_staging) or _within(self.authority_staging, self.password_file):
            raise DomainError("invalid_configuration", "Restic password file must not overlap authority staging")
        if self.repository.exists() and not self.repository.is_dir():
            raise DomainError("invalid_configuration", "Encrypted recovery repository must be a directory")
        if self.authority_staging.exists() and not self.authority_staging.is_dir():
            raise DomainError("invalid_configuration", "Authority staging must be a directory")
        _regular_secret_file(self.password_file)
        try:
            self.database_relative = _absolute(database.path).relative_to(self.root)
        except (AttributeError, ValueError) as exc:
            raise DomainError("invalid_configuration", "Database must be located under the operations root") from exc
        self.restic = _Restic(restic, self.repository, self.password_file, timeout_seconds=command_timeout_seconds,
                              installation_tag="qorqut-installation:" + sha256(str(self.root).encode()).hexdigest())
        self.clock = clock
        self.context = ContextRecovery(paths.context, paths.context_runtime, paths.operations)

    def _authority_source(self, generation: str) -> Path:
        return self.authority_staging / generation

    def _runtime_sources(self, authority: Path) -> list[Path]:
        try:
            manifest = json.loads((authority / "authored/manifest.json").read_text(encoding="utf-8"))
            businesses = manifest["businesses"]
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise DomainError("invalid_backup", "Authority snapshot has no valid registered-business manifest") from exc
        # The installation repository is the common Git directory for linked HQ
        # worktrees.  It is optional for legacy installations.  When present it
        # must be a directory: a root gitdir file can redirect to an arbitrary
        # host repository that this generation cannot cover.
        root_git = self.root / ".git"
        try:
            root_git_mode = root_git.lstat().st_mode
        except FileNotFoundError:
            root_git = None
        except OSError as exc:
            raise DomainError("backup_incomplete", "Installation Git metadata cannot be inspected") from exc
        if root_git is not None and not stat.S_ISDIR(root_git_mode):
            raise DomainError("backup_incomplete", "Installation Git metadata is not a contained directory")
        paths = [authority]
        if root_git is not None:
            paths.append(root_git)
        for business in businesses:
            try:
                hq = Path(business["hq"])
            except (TypeError, KeyError) as exc:
                raise DomainError("invalid_backup", "Authority snapshot has an invalid registered business path") from exc
            if hq.is_absolute() or ".." in hq.parts:
                raise DomainError("invalid_backup", "Authority snapshot has an invalid registered business path")
            code = self.root / hq.parent / "code"
            # An existing directory symlink is a valid Git checkout reference: restic records the link itself.
            try:
                mode = code.lstat().st_mode
            except OSError as exc:
                raise DomainError("backup_incomplete", "Registered business code root is missing") from exc
            if not (stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                raise DomainError("backup_incomplete", "Registered business code root is not a directory or link")
            paths.append(code)
            hq_git = self.root / hq / ".git"
            if hq_git.exists() or hq_git.is_symlink():
                try:
                    hq_git_mode = hq_git.lstat().st_mode
                except OSError as exc:
                    raise DomainError("backup_incomplete", "Registered HQ Git metadata cannot be inspected") from exc
                if stat.S_ISLNK(hq_git_mode) or not (stat.S_ISDIR(hq_git_mode) or stat.S_ISREG(hq_git_mode)):
                    raise DomainError("backup_incomplete", "Registered HQ Git metadata is not a regular file or directory")
                if stat.S_ISREG(hq_git_mode):
                    if root_git is None:
                        raise DomainError("backup_incomplete", "Linked HQ Git metadata requires an installation Git directory")
                    _validate_linked_gitdir(hq_git, root_git)
                # A linked-worktree gitdir file is already an authored HQ file.
                # A standalone HQ repository's `.git` directory is skipped by
                # BackupStore, so carry only that directory in the runtime layer.
                if stat.S_ISDIR(hq_git_mode):
                    paths.append(hq_git)
        return paths

    def _snapshot(self, generation: str) -> dict:
        snapshots = self.restic.snapshots(generation)
        if len(snapshots) != 1:
            raise DomainError("invalid_backup", "Recovery generation must resolve to exactly one encrypted snapshot")
        snapshot = snapshots[0]
        if not isinstance(snapshot.get("short_id"), str) or _GENERATION_TAG + generation not in snapshot.get("tags", []):
            raise DomainError("invalid_backup", "Encrypted snapshot does not bind its recovery generation")
        return snapshot

    def create(self, generation_id: str, *, quiescence: TrustedQuiescence, daily: int = 7, weekly: int = 4) -> dict:
        generation_id = _uuid(generation_id)
        if not self.root.is_dir():
            raise DomainError("invalid_configuration", "Full recovery capture requires the operations root to exist")
        if type(daily) is not int or type(weekly) is not int or daily < 1 or weekly < 1:
            raise DomainError("validation_error", "Retention must preserve positive daily and weekly generations")
        if not isinstance(quiescence, AbstractContextManager) and not (hasattr(quiescence, "__enter__") and hasattr(quiescence, "__exit__")):
            raise DomainError("invalid_configuration", "Full recovery requires a trusted quiescence context manager")
        existing = None
        if self.restic.initialized():
            snapshots = self.restic.snapshots(generation_id)
            if len(snapshots) > 1:
                raise DomainError("invalid_backup", "Recovery generation must resolve to exactly one encrypted snapshot")
            if snapshots:
                existing = self._snapshot(generation_id)
        if existing is not None:
            self.restic.verify()
            return FullRecoveryGeneration(generation_id, existing["short_id"], existing.get("time", ""), str(self.root),
                                          tuple(existing.get("paths", ()))).to_dict()
        self.repository.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.repository.chmod(0o700)
        self.authority_staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.authority_staging.chmod(0o700)
        authority = self._authority_source(generation_id)
        if authority.exists():
            raise DomainError("invalid_backup", "Unverified authority staging already exists for this generation")
        try:
            with quiescence as barrier:
                # The yielded value is the actual recovery barrier proof; the authority generation is captured under it.
                authority_store = BackupStore(self.database, self.paths, destination=self.authority_staging, clock=self.clock)
                authority_store.create(generation_id, barrier=barrier)
                paths = self._runtime_sources(authority)
                if not self.restic.initialized():
                    if not self.initialize_repository or self._repository_preexisted and any(self.repository.iterdir()):
                        raise DomainError("invalid_configuration", "Recovery repository must be explicitly initialized before capture")
                    self.restic.init()
                timestamp = self.clock()
                self.restic.backup(paths, generation_id, _restic_time(timestamp), excludes=())
                snapshot = self._snapshot(generation_id)
                self.restic.verify()
            result = FullRecoveryGeneration(generation_id, snapshot["short_id"], snapshot.get("time", timestamp), str(self.root),
                                            tuple(str(path) for path in paths))
            # A failed capture never reaches retention.  The new encrypted generation
            # has already passed a full data read before older snapshots are pruned.
            self.restic.forget(daily=daily, weekly=weekly)
            return result.to_dict()
        finally:
            # Plaintext authority generations exist only while restic is capturing them.
            shutil.rmtree(authority, ignore_errors=True)

    def verify(self, generation_id: str) -> dict:
        generation_id = _uuid(generation_id)
        snapshot = self._snapshot(generation_id)
        self.restic.verify()
        return FullRecoveryGeneration(generation_id, snapshot["short_id"], snapshot.get("time", ""), str(self.root),
                                      tuple(snapshot.get("paths", ()))).to_dict()

    def _restore_destination(self, destination: Path | str) -> Path:
        destination = _absolute(destination)
        _no_symlink_ancestors(destination)
        if (_within(destination, self.root) or _within(self.root, destination)
                or destination == self.root or destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
            raise DomainError("invalid_restore", "Full recovery restore requires an empty, separate directory")
        return destination

    def _raw_path(self, raw: Path, original: Path) -> Path:
        if original.is_absolute():
            return raw / original.relative_to("/")
        raise DomainError("invalid_backup", "Recovery source path is not absolute")

    def _merge_runtime(self, raw_root: Path, target: Path, authority: Path) -> None:
        try:
            manifest = json.loads((authority / "authored/manifest.json").read_text(encoding="utf-8"))
            businesses = manifest["businesses"]
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise DomainError("invalid_backup", "Restored authority manifest is invalid") from exc
        for item in businesses:
            try:
                hq = Path(item["hq"])
            except (TypeError, KeyError) as exc:
                raise DomainError("invalid_restore", "Restored authority manifest has an invalid business path") from exc
            if hq.is_absolute() or ".." in hq.parts or not hq.parts:
                raise DomainError("invalid_restore", "Restored authority manifest has an invalid business path")
            relative = hq.parent / "code"
            source, destination = raw_root / relative, target / relative
            if not (source.exists() or source.is_symlink()) or destination.exists() or destination.is_symlink():
                raise DomainError("invalid_restore", "Restored business code tree is missing or conflicts")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if destination.parent.is_symlink():
                raise DomainError("invalid_restore", "Restored business code parent is not safe")
            os.rename(source, destination)
            git_relative = hq / ".git"
            git_source, git_destination = raw_root / git_relative, target / git_relative
            if git_source.exists() or git_source.is_symlink():
                if git_destination.exists() or git_destination.is_symlink():
                    raise DomainError("invalid_restore", "Restored HQ Git metadata conflicts with authority data")
                git_destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if git_destination.parent.is_symlink():
                    raise DomainError("invalid_restore", "Restored HQ Git parent is not safe")
                os.rename(git_source, git_destination)
        for relative in (Path(".git"),):
            source, destination = raw_root / relative, target / relative
            if not (source.exists() or source.is_symlink()):
                continue
            if destination.exists() or destination.is_symlink():
                raise DomainError("invalid_restore", "Restored runtime tree conflicts with authority data")
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if destination.parent.is_symlink():
                raise DomainError("invalid_restore", "Restored runtime parent is not safe")
            os.rename(source, destination)

    def _restore_authority(self, authority: Path, generation_id: str, target: Path) -> tuple[dict, Path]:
        """Restore verified storage bytes without consulting the currently-live root.

        This parallels :meth:`BackupStore.restore` so original-path recovery can
        happen after that root has been safely retired.  It retains the same closed
        authority gates and row-count parity checks.  The generation's Context capture is verified here and
        returned with the staged incomplete marker; it is restored only at its final locations.
        """
        try:
            metadata = json.loads((authority / "generation.json").read_text(encoding="utf-8"))
            if (metadata["schema_version"] != 2 or metadata["generation_id"] != generation_id
                    or metadata["database_relative_path"] != self.database_relative.as_posix()
                    or metadata["state"] != "complete"):
                raise ValueError
            context = metadata["context"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DomainError("invalid_restore", "Restored authority bytes do not pass generation verification") from exc
        self.context.verify(authority / CAPTURE_DIRECTORY, context)
        try:
            manifest = verify_snapshot(authority / "authored")
            if manifest.root != self.root:
                raise ValueError
            require_target_businesses(manifest, authority / "state.sqlite3", self.paths)
            restore_snapshot(authority / "authored", target)
            restored = self.database.restore(authority / "state.sqlite3", target / self.database_relative,
                                        expected_checksum=metadata["database"]["checksum"])
            with restored.transaction() as uow:
                repo, row = uow.repository("maintenance_state"), uow.repository("maintenance_state").get("service")
                values = {"state": "closed", "message": "Full isolated restore; original root bindings retained. Do not activate here."}
                if row:
                    repo.update("service", values, expected_revision=row["revision"])
                else:
                    repo.insert({"id": "service", **values})
            parity = self.database.validate_snapshot(restored.path)
            expected = dict(metadata["database"]["record_counts"])
            expected["maintenance_state"] = max(1, expected["maintenance_state"])
            if parity["record_counts"] != expected:
                raise ValueError
            _fsync_directory(target)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise DomainError("invalid_restore", "Restored authority bytes do not pass generation verification") from exc
        return context, self.context.mark_incomplete(target, generation_id)

    def _restore(self, generation_id: str, destination: Path) -> dict:
        snapshot = self._snapshot(generation_id)
        self.restic.verify()
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        raw = Path(tempfile.mkdtemp(prefix=".full-recovery-", dir=destination.parent))
        try:
            self.restic.restore(snapshot["short_id"], raw)
            raw_root = self._raw_path(raw, self.root)
            raw_authority = self._raw_path(raw, self._authority_source(generation_id))
            if not raw_root.is_dir() or not raw_authority.is_dir():
                raise DomainError("invalid_restore", "Encrypted snapshot omitted the recovery root or authority generation")
            staging = raw / "rehearsal-root"
            context, marker = self._restore_authority(raw_authority, generation_id, staging)
            self._merge_runtime(raw_root, staging, raw_authority)
            if destination.exists():
                destination.rmdir()
            os.rename(staging, destination)
            # The Context pair is restored last, where it will live; until then the root carries its incomplete marker.
            restored = self.context.restore(raw_authority / CAPTURE_DIRECTORY, context, destination,
                                            marker=destination / marker.relative_to(staging))
        finally:
            shutil.rmtree(raw, ignore_errors=True)
        return {"generation_id": generation_id, "snapshot_id": snapshot["short_id"], "destination": str(destination),
                "activation_root": str(self.root), "state": "closed", "scope": "full_installation", "context": restored}

    def restore(self, generation_id: str, destination: Path | str) -> dict:
        """Rehearse one complete installation generation into an isolated empty root.

        This is intentionally not an in-place recovery API.  The restored database
        retains its original absolute bindings and both authority gates remain closed.
        """
        generation_id = _uuid(generation_id)
        destination = self._restore_destination(destination)
        return self._restore(generation_id, destination)

    def restore_original_root(self, generation_id: str, *, quiescence: TrustedQuiescence) -> dict:
        """Prepare a closed-gate original-path restore after the live root is retired.

        The trusted caller must keep the whole installation quiesced.  This refuses
        to merge with a live/nonempty root and never starts a service or opens either
        authority gate.
        """
        generation_id = _uuid(generation_id)
        if not isinstance(quiescence, AbstractContextManager) and not (hasattr(quiescence, "__enter__") and hasattr(quiescence, "__exit__")):
            raise DomainError("invalid_configuration", "Original-path recovery requires a trusted quiescence context manager")
        _no_symlink_ancestors(self.root)
        if self.root.exists() and (not self.root.is_dir() or any(self.root.iterdir())):
            raise DomainError("invalid_restore", "Original-path recovery refuses a live or nonempty operations root")
        with quiescence:
            return self._restore(generation_id, self.root)
