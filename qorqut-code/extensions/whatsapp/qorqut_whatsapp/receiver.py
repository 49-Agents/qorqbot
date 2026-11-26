"""One optional receiver child, owned and stopped by this installation's lifetime."""
import os
from pathlib import Path
import subprocess

from qorqut.private_files import require_private_directory
from qorqut_operations.contracts import DomainError
from . import binding


def helper_environment(data: Path, temporary: Path) -> dict:
    return dict(os.environ, HOME=str(data), XDG_CONFIG_HOME=str(data / "config"), XDG_CACHE_HOME=str(data / "cache"),
                XDG_DATA_HOME=str(data), TMPDIR=str(temporary))


def open_log(path: Path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    return os.fdopen(descriptor, "ab", buffering=0)


class Receiver:
    def __init__(self, paths, *, popen=subprocess.Popen):
        self.paths, self.popen = paths, popen
        self.process = self.log = None
        self.managed = False   # whether the running child serves the managed-send socket

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self):
        if self.running():
            return
        self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None
        root = self.paths.instance_root
        settings = binding.load_binding(root)
        if settings is None or not binding.linked(root):
            return  # Dormant: not set up, or the phone is not linked yet. Nothing contacts WhatsApp.
        ready, client = binding.launch(self.paths, {"agent_id": settings["agent_id"], "role": {"id": "ordubashy"},
                                                  "scope": {"kind": "ecosystem"}})
        if not ready or not ready["prepared"]:
            raise DomainError("operation_unavailable", "The installed WhatsApp receiver binary is unavailable")
        sockets = [settings["socket"]]
        send = settings.get("managed_send") or binding.pending_send(settings, root)
        if send:
            sockets.append(send["socket"])
        if not all(binding.socket_fits(path) for path in sockets):
            raise DomainError("invalid_configuration", "This installation path is too long for WhatsApp's Unix sockets; use a shorter Qorqbot folder path")
        for path in sockets:
            require_private_directory(Path(path).parent, create=True)
        require_private_directory(root / "private", create=True)
        data = require_private_directory(binding.data_path(root), create=True)
        temporary = require_private_directory(data / "tmp", create=True)
        log_directory = require_private_directory(root / "logs" / "whatsapp", create=True)
        args = [client["command"], "serve", "--data-dir", str(data), "--socket", settings["socket"]]
        if send:
            args += ["--managed-send-socket", send["socket"], "--managed-send-secret-file", send["secret_file"]]
        self.log = open_log(log_directory / "receiver.log")
        try:
            self.process = self.popen(args, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                                      cwd=data, env=helper_environment(data, temporary), start_new_session=True)
        except BaseException:
            self.log.close(); self.log = None
            raise
        self.managed = bool(send)

    def stop(self, timeout=5):
        process = self.process
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("WhatsApp receiver did not exit; its writer is not quiescent") from exc
            self.process = None
        self.managed = False
        if self.log is not None:
            self.log.close(); self.log = None
