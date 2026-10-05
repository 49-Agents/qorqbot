"""Narrow argv-only tmux operations on an explicitly supplied launch binding.

The caller holds the runner input lock for mutations. This module deliberately
has no new-session, attach-or-create, process adoption, or managed CLI launcher.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import uuid4

from .models import RunBinding, RunnerError, TerminalSnapshot, validate_dimensions
from .process import verify_process

_FIELDS = ("pid", "session_id", "window_id", "pane_id", "pane_pid", "pane_dead",
           "pane_in_mode", "pane_input_off", "pane_synchronized", "pane_width",
           "pane_height", "cursor_x", "cursor_y", "cursor_flag")


@dataclass(frozen=True)
class PasteBuffer:
    name: str
    binding_digest: str
    payload_sha256: str

    @classmethod
    def for_body(cls, binding: RunBinding, body: bytes) -> PasteBuffer:
        return cls("runner-" + uuid4().hex, binding.digest, hashlib.sha256(body).hexdigest())


class Tmux:
    def __init__(self, executable: str = "/usr/bin/tmux", *, timeout: float = 5):
        if not Path(executable).is_absolute():
            raise ValueError("tmux executable must be absolute")
        self.executable = executable
        self.timeout = timeout

    def _server(self, binding: RunBinding) -> None:
        target = binding.tmux
        try:
            sock = Path(target.socket_path).lstat()
        except OSError as exc:
            raise RunnerError("target_changed", "owned tmux socket is unavailable") from exc
        if (not stat.S_ISSOCK(sock.st_mode) or sock.st_dev != target.socket_device
                or sock.st_ino != target.socket_inode or sock.st_uid != os.geteuid()
                or target.server.uid != os.geteuid()):
            raise RunnerError("target_changed", "tmux socket ownership changed")
        verify_process(target.server)

    def _command(self, binding: RunBinding, *argv: str, data: bytes | None = None) -> bytes:
        self._server(binding)
        try:
            result = subprocess.run(
                [self.executable, "-S", binding.tmux.socket_path, "-f", "/dev/null", *argv],
                input=data, capture_output=True, timeout=self.timeout, check=False,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError("unavailable", "tmux command did not confirm completion") from exc
        if result.returncode:
            raise RunnerError("unavailable", "tmux rejected the exact target command")
        return result.stdout

    def inspect(self, binding: RunBinding) -> TerminalSnapshot:
        target = binding.tmux
        raw = self._command(binding, "display-message", "-p", "-t", target.pane_id,
                            "|".join("#{" + field + "}" for field in _FIELDS))
        values = raw.decode("utf-8", "strict").strip().split("|")
        expected = [str(target.server.pid), target.session_id, target.window_id,
                    target.pane_id, str(target.pane.pid), "0"]
        if len(values) != len(_FIELDS) or values[:6] != expected:
            raise RunnerError("target_changed", "tmux pane/run binding changed")
        for process in (target.pane, binding.recorder, binding.native):
            verify_process(process)
        try:
            screen = self._command(binding, "capture-pane", "-p", "-e", "-t", target.pane_id)
            # A pane may be respawned while capture is in flight. Do not return
            # a replacement's screen as evidence about the saved native run.
            again = self._command(binding, "display-message", "-p", "-t", target.pane_id,
                                  "|".join("#{" + field + "}" for field in _FIELDS))
            values = again.decode("utf-8", "strict").strip().split("|")
            if len(values) != len(_FIELDS) or values[:6] != expected:
                raise RunnerError("target_changed", "pane changed during observation")
            for process in (target.pane, binding.recorder, binding.native):
                verify_process(process)
            numbers = [int(value) for value in values[6:]]
            if any(value not in (0, 1) for value in numbers[:3] + numbers[7:]):
                raise ValueError("invalid tmux flag")
            self._server(binding)
            return TerminalSnapshot(
                in_mode=bool(numbers[0]), input_off=bool(numbers[1]),
                synchronized=bool(numbers[2]), columns=numbers[3], rows=numbers[4],
                cursor_x=numbers[5], cursor_y=numbers[6], cursor_visible=bool(numbers[7]),
                screen=screen.decode("utf-8", "replace"),
            )
        except (ValueError, IndexError) as exc:
            raise RunnerError("unavailable", "invalid terminal observation") from exc

    @staticmethod
    def _buffer(binding: RunBinding, buffer: PasteBuffer) -> None:
        if (not re.fullmatch(r"runner-[a-f0-9]{32}", buffer.name)
                or buffer.binding_digest != binding.digest):
            raise RunnerError("target_changed", "buffer belongs to a different binding")

    def load_buffer(self, binding: RunBinding, buffer: PasteBuffer, body: bytes) -> None:
        self._buffer(binding, buffer)
        if hashlib.sha256(body).hexdigest() != buffer.payload_sha256:
            raise RunnerError("idempotency_conflict", "buffer payload changed")
        self.inspect(binding)
        # Payload is stdin, never tmux grammar, argv or shell source.
        self._command(binding, "load-buffer", "-b", buffer.name, "-", data=body)

    def _effect(self, binding: RunBinding, argv: list[str]) -> None:
        snapshot = self.inspect(binding)
        if snapshot.in_mode or snapshot.input_off or snapshot.synchronized:
            raise RunnerError("not_ready", "pane input is not exclusive and editable")
        target = binding.tmux
        # Recheck in tmux's own command queue as well as Linux above. The only
        # grammar here consists of fixed commands and validated IDs/integers.
        pairs = ("pid", target.server.pid), ("session_id", target.session_id), \
            ("window_id", target.window_id), ("pane_id", target.pane_id), \
            ("pane_pid", target.pane.pid), ("pane_dead", 0), ("pane_in_mode", 0), \
            ("pane_input_off", 0), ("pane_synchronized", 0)
        predicates = ["#{==:#{" + key + "}," + str(value) + "}" for key, value in pairs]
        condition = predicates[0]
        for predicate in predicates[1:]:
            condition = "#{&&:" + condition + "," + predicate + "}"
        output = self._command(binding, "if-shell", "-F", "-t", target.pane_id,
                               condition, " ".join(argv), "display-message -p runner-target-changed")
        if output.strip() == b"runner-target-changed":
            raise RunnerError("target_changed", "target changed at tmux effect boundary")
        self.inspect(binding)

    def paste(self, binding: RunBinding, buffer: PasteBuffer, *, bracketed: bool = True) -> None:
        self._buffer(binding, buffer)
        argv = ["paste-buffer", "-d", "-r"]
        if bracketed:
            argv.append("-p")
        self._effect(binding, [*argv, "-b", buffer.name, "-t", binding.tmux.pane_id])

    def enter(self, binding: RunBinding) -> None:
        self._effect(binding, ["send-keys", "-t", binding.tmux.pane_id, "Enter"])

    def resize(self, binding: RunBinding, columns: int, rows: int) -> None:
        validate_dimensions(columns, rows)
        self._effect(binding, ["resize-window", "-t", binding.tmux.window_id,
                               "-x", str(columns), "-y", str(rows)])

    def delete_buffer(self, binding: RunBinding, buffer: PasteBuffer) -> None:
        self._buffer(binding, buffer)
        # Server fingerprint suffices for deleting this exact, positively owned
        # unique buffer even when its old pane has exited. Never kill a server.
        self._command(binding, "delete-buffer", "-b", buffer.name)
