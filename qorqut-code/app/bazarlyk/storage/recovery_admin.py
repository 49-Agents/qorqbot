"""Local, peer-authenticated administration for encrypted recovery generations.

This deliberately is not an HTTP or MCP surface.  Composition supplies a trusted
quiescence *context factory* and starts this controller only after the runtime is
ready.  The controller never exposes restore: original-path activation remains an
operator-run, offline all-installation procedure.
"""
from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import re
import struct
import threading
from typing import Any, Callable
from uuid import UUID

from qorqut_operations.contracts import DomainError
from bazarlyk.storage.full_recovery import FullRecoveryStore, ResticBinary
from bazarlyk.posix_files import require_descriptor_socket_paths
from qorqut_operations.storage import safe_io

_MAX_REQUEST = 4096
_MAX_RESPONSE = 8192
_SOCKET_MODE = 0o600
_CONFIG_LIMIT = 16 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _absolute(value: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def _outside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return True
    return False


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise DomainError("validation_error", "Generation requires a canonical UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise DomainError("validation_error", "Generation requires a canonical UUID") from exc
    return value


def _path(value: object, *, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DomainError("invalid_configuration", "Recovery administration configuration is invalid")
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise DomainError("invalid_configuration", "Recovery administration configuration is invalid")
    return _absolute(candidate)


def _integer(value: object, *, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise DomainError("invalid_configuration", "Recovery administration configuration is invalid")
    return value


@dataclass(frozen=True)
class RecoveryAdminConfig:
    """Typed host-private references for the local recovery controller."""

    paths: object  # the InstancePaths whose operations root this controller recovers
    socket_path: Path
    repository: Path
    password_file: Path
    authority_staging: Path
    restic_executable: Path
    restic_sha256: str
    daily: int
    weekly: int
    command_timeout_seconds: int
    owner_uid: int
    initialize_repository: bool = False

    @classmethod
    def load(cls, config_path: Path | str, *, paths) -> "RecoveryAdminConfig":
        """Read a small private JSON config without following a replacement path."""
        root = _absolute(paths.operations)
        source = _absolute(config_path)
        if not _outside(source, root):
            raise DomainError("invalid_configuration", "Recovery administration configuration must be outside business mounts")
        raw = safe_io.read_bytes(source, limit=_CONFIG_LIMIT, private=True)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise DomainError("invalid_configuration", "Recovery administration configuration is invalid") from exc
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "operations_root", "socket_path", "repository", "password_file",
            "authority_staging", "restic", "retention", "owner_uid", "initialize_repository",
        }:
            raise DomainError("invalid_configuration", "Recovery administration configuration is invalid")
        if value["schema_version"] != 1 or _path(value["operations_root"], name="operations_root") != root:
            raise DomainError("invalid_configuration", "Recovery administration configuration is invalid")
        restic, retention = value["restic"], value["retention"]
        if (not isinstance(restic, dict) or set(restic) != {"executable", "sha256", "command_timeout_seconds"}
                or not isinstance(restic.get("sha256"), str) or not _SHA256.fullmatch(restic["sha256"])):
            raise DomainError("invalid_configuration", "Recovery administration configuration is invalid")
        if not isinstance(retention, dict) or set(retention) != {"daily", "weekly"}:
            raise DomainError("invalid_configuration", "Recovery administration configuration is invalid")
        if not isinstance(value["initialize_repository"], bool):
            raise DomainError("invalid_configuration", "Recovery administration configuration is invalid")
        result = cls(
            paths=paths,
            socket_path=_path(value["socket_path"], name="socket_path"),
            repository=_path(value["repository"], name="repository"),
            password_file=_path(value["password_file"], name="password_file"),
            authority_staging=_path(value["authority_staging"], name="authority_staging"),
            restic_executable=_path(restic["executable"], name="executable"),
            restic_sha256=restic["sha256"],
            daily=_integer(retention["daily"], lower=1, upper=3650),
            weekly=_integer(retention["weekly"], lower=1, upper=3650),
            command_timeout_seconds=_integer(restic["command_timeout_seconds"], lower=1, upper=3600),
            owner_uid=_integer(value["owner_uid"], lower=0, upper=2 ** 31 - 1),
            initialize_repository=value["initialize_repository"],
        )
        result.validate()
        return result

    @property
    def operations_root(self) -> Path:
        return _absolute(self.paths.operations)

    def validate(self) -> None:
        paths = (self.socket_path, self.repository, self.password_file, self.authority_staging)
        if not all(_outside(path, self.operations_root) for path in (*paths, self.restic_executable)):
            raise DomainError("invalid_configuration", "Recovery administration paths must be outside business mounts")
        for index, path in enumerate(paths):
            if any(not _outside(path, other) or not _outside(other, path) for other in paths[index + 1:]):
                raise DomainError("invalid_configuration", "Recovery administration paths must not overlap")
        if self.socket_path.name in {"", ".", ".."}:
            raise DomainError("invalid_configuration", "Recovery administration configuration is invalid")


class RecoveryAdministration:
    """Single-process local UDS server for create, verify, and list only.

    Construction is inert.  ``start`` binds a new mode-0600 socket and creates a
    dedicated serving thread; it refuses an existing path rather than unlinking
    something it did not create.  Calls are serialized so retention cannot race a
    capture.  A stop with an in-flight bounded restic command raises rather than
    pretending the server stopped.
    """

    def __init__(self, database, config: RecoveryAdminConfig,
                 quiescence_factory: Callable[[], AbstractContextManager], *, background_worker=None):
        if not callable(quiescence_factory):
            raise TypeError("quiescence_factory must be callable")
        self.database, self.config = database, config
        self._quiescence_factory = quiescence_factory
        self._background_worker = background_worker
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._operations = threading.Lock()
        self._started_socket: tuple[int, int] | None = None
        self._store: FullRecoveryStore | None = None

    @contextmanager
    def _socket_parent(self):
        """Retain the checked parent descriptor through bind/connect/unlink."""
        with safe_io.directory(self.config.socket_path.parent) as descriptor:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise DomainError("invalid_configuration", "Recovery socket directory must be private")
            yield descriptor

    def _socket_address(self, descriptor: int) -> str:
        # Linux resolves this retained descriptor, not a path that a writer could
        # replace with a symlink after validation.
        return f"/proc/self/fd/{descriptor}/{self.config.socket_path.name}"

    def _build_store(self) -> FullRecoveryStore:
        return FullRecoveryStore(
            self.database, self.config.paths, repository=self.config.repository,
            password_file=self.config.password_file, authority_staging=self.config.authority_staging,
            restic=ResticBinary(self.config.restic_executable, self.config.restic_sha256),
            initialize_repository=self.config.initialize_repository,
            command_timeout_seconds=self.config.command_timeout_seconds,
        )

    def start(self) -> None:
        require_descriptor_socket_paths()
        if self._thread is not None:
            raise RuntimeError("Recovery administration is already started")
        self.config.validate()
        self._store = self._build_store()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            with self._socket_parent() as descriptor:
                try:
                    os.lstat(self.config.socket_path.name, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
                else:
                    # Never unlink a pre-existing FIFO, socket, symlink, or regular file.
                    raise DomainError("invalid_configuration", "Recovery socket path already exists")
                listener.bind(self._socket_address(descriptor))
                bound = os.stat(self.config.socket_path.name, dir_fd=descriptor, follow_symlinks=False)
                self._started_socket = (bound.st_dev, bound.st_ino)
                os.chmod(self.config.socket_path.name, _SOCKET_MODE, dir_fd=descriptor, follow_symlinks=False)
                info = os.stat(self.config.socket_path.name, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != _SOCKET_MODE:
                    raise DomainError("invalid_configuration", "Recovery socket could not be secured")
                if (info.st_dev, info.st_ino) != self._started_socket:
                    raise DomainError("invalid_configuration", "Recovery socket changed while being secured")
            listener.listen(8)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            self._unlink_owned_socket()
            raise
        self._listener = listener
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="recovery-administration", daemon=True)
        self._thread.start()

    def _unlink_owned_socket(self) -> None:
        if self._started_socket is None:
            return
        try:
            with self._socket_parent() as descriptor:
                try:
                    info = os.stat(self.config.socket_path.name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if (info.st_dev, info.st_ino) == self._started_socket and stat.S_ISSOCK(info.st_mode):
                    os.unlink(self.config.socket_path.name, dir_fd=descriptor)
        finally:
            self._started_socket = None

    def stop(self, *, timeout: float = 5.0) -> None:
        if type(timeout) not in {int, float} or timeout <= 0:
            raise ValueError("timeout must be positive")
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        if thread is threading.current_thread():
            raise RuntimeError("Recovery administration cannot join its serving thread")
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError("Recovery administration still has an in-flight operation")
        self._thread = self._listener = None
        self._unlink_owned_socket()

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            with connection:
                self._serve_connection(connection)

    def _peer_owner(self, connection: socket.socket) -> bool:
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
        except (AttributeError, OSError, struct.error):
            return False
        return uid == self.config.owner_uid

    def _receive(self, connection: socket.socket) -> dict[str, Any]:
        connection.settimeout(2.0)
        data = bytearray()
        while len(data) <= _MAX_REQUEST:
            chunk = connection.recv(min(1024, _MAX_REQUEST + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in chunk:
                break
        if len(data) > _MAX_REQUEST or not data.endswith(b"\n") or data.count(b"\n") != 1:
            raise DomainError("validation_error", "Invalid recovery administration request")
        try:
            request = json.loads(data[:-1].decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise DomainError("validation_error", "Invalid recovery administration request") from exc
        if not isinstance(request, dict):
            raise DomainError("validation_error", "Invalid recovery administration request")
        return request

    def _send(self, connection: socket.socket, body: dict[str, Any]) -> None:
        data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        if len(data) > _MAX_RESPONSE:
            data = b'{"code":"internal_error","ok":false}\n'
        try:
            connection.sendall(data)
        except OSError:
            pass

    def _assert_not_background_worker(self) -> None:
        # Runtime composition may supply its actual background service.  Its own
        # thread cannot run recovery because quiescence would join itself.
        worker = self._background_worker
        if worker is not None and (getattr(worker, "_thread", None) is threading.current_thread()
                or callable(getattr(worker, "is_worker_thread", None)) and worker.is_worker_thread()):
            raise DomainError("recovery_unavailable", "Recovery cannot run from its background worker")

    @staticmethod
    def _projection(result: dict[str, Any]) -> dict[str, str]:
        return {key: result[key] for key in ("generation_id", "snapshot_id", "created_at", "state") if isinstance(result.get(key), str)}

    def _list(self) -> list[dict[str, str]]:
        assert self._store is not None
        rows = []
        for snapshot in self._store.restic.snapshots():
            if not isinstance(snapshot, dict):
                continue
            generation = next((tag.removeprefix("qorqut-generation:") for tag in snapshot.get("tags", [])
                               if isinstance(tag, str) and tag.startswith("qorqut-generation:")), None)
            if generation is None or not isinstance(snapshot.get("short_id"), str):
                continue
            rows.append({"generation_id": generation, "snapshot_id": snapshot["short_id"],
                         "created_at": snapshot.get("time", "") if isinstance(snapshot.get("time", ""), str) else ""})
        return sorted(rows, key=lambda row: (row["created_at"], row["generation_id"]), reverse=True)

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "list" and set(request) == {"action"}:
            return {"snapshots": self._list()}
        if action not in {"create", "verify"} or set(request) != {"action", "generation_id"}:
            raise DomainError("validation_error", "Invalid recovery administration request")
        generation = _uuid(request["generation_id"])
        assert self._store is not None
        if action == "verify":
            return self._projection(self._store.verify(generation))
        quiescence = self._quiescence_factory()
        if not isinstance(quiescence, AbstractContextManager) and not (hasattr(quiescence, "__enter__") and hasattr(quiescence, "__exit__")):
            raise DomainError("invalid_configuration", "Recovery quiescence factory did not return a context manager")
        return self._projection(self._store.create(generation, quiescence=quiescence,
                                                   daily=self.config.daily, weekly=self.config.weekly))

    def _serve_connection(self, connection: socket.socket) -> None:
        if not self._peer_owner(connection):
            self._send(connection, {"ok": False, "code": "forbidden"})
            return
        try:
            request = self._receive(connection)
            self._assert_not_background_worker()
            with self._operations:
                result = self._dispatch(request)
        except DomainError as exc:
            self._send(connection, {"ok": False, "code": exc.code})
        except Exception:
            self._send(connection, {"ok": False, "code": "internal_error"})
        else:
            self._send(connection, {"ok": True, "result": result})


def client_request(config: RecoveryAdminConfig, request: dict[str, Any], *, timeout: float = 5.0) -> dict[str, Any]:
    """Send one bounded request to the socket fixed in the private config."""
    require_descriptor_socket_paths()
    if type(timeout) not in {int, float} or timeout <= 0:
        raise ValueError("timeout must be positive")
    data = json.dumps(request, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    if len(data) > _MAX_REQUEST:
        raise DomainError("validation_error", "Recovery administration request is too large")
    try:
        with safe_io.directory(config.socket_path.parent) as descriptor, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            parent_info = os.fstat(descriptor)
            if parent_info.st_mode & 0o077:
                raise DomainError("invalid_configuration", "Recovery socket directory must be private")
            try:
                socket_info = os.stat(config.socket_path.name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise DomainError("recovery_unavailable", "Recovery administration socket is unavailable") from exc
            if not stat.S_ISSOCK(socket_info.st_mode) or stat.S_IMODE(socket_info.st_mode) != _SOCKET_MODE:
                raise DomainError("recovery_unavailable", "Recovery administration socket is unavailable")
            client.settimeout(timeout)
            client.connect(f"/proc/self/fd/{descriptor}/{config.socket_path.name}")
            client.sendall(data)
            response = bytearray()
            while len(response) <= _MAX_RESPONSE:
                part = client.recv(min(1024, _MAX_RESPONSE + 1 - len(response)))
                if not part:
                    break
                response.extend(part)
                if b"\n" in part:
                    break
    except (OSError, TimeoutError) as exc:
        raise DomainError("recovery_unavailable", "Recovery administration socket is unavailable", retryable=True) from exc
    if len(response) > _MAX_RESPONSE or not response.endswith(b"\n") or response.count(b"\n") != 1:
        raise DomainError("recovery_unavailable", "Recovery administration returned an invalid response")
    try:
        result = json.loads(response[:-1].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DomainError("recovery_unavailable", "Recovery administration returned an invalid response") from exc
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise DomainError("recovery_unavailable", "Recovery administration returned an invalid response")
    return result


def main(argv: list[str] | None = None) -> int:
    from bazarlyk.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
    parser = argparse.ArgumentParser(description=instruction_text('recovery-admin-local-encrypted-recovery-administration-client'))
    add_instance_arguments(parser)
    parser.add_argument("--config", required=True, help="private host recovery-admin JSON")
    parser.add_argument("--timeout", type=float, default=30.0)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("create", "verify"):
        command = commands.add_parser(name)
        command.add_argument("generation_id")
    commands.add_parser("list")
    args = parser.parse_args(argv)
    config = RecoveryAdminConfig.load(args.config, paths=instance_paths_from_arguments(args, required=()))
    request: dict[str, Any] = {"action": args.action}
    if args.action in {"create", "verify"}:
        request["generation_id"] = args.generation_id
    print(json.dumps(client_request(config, request, timeout=args.timeout), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
