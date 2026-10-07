"""Persistent local Bash recording and disposable, bounded terminal viewers.

No HTTP route, native-session ownership or automated-input readiness is provided.
Closing a manager leaves execution intact; only explicit shutdown targets it.
"""

from __future__ import annotations

from contextlib import ExitStack
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import termios
import threading
import time
import tty
from uuid import UUID, uuid4, uuid5

from .models import MAX_BODY_BYTES, ProcessFingerprint, RunnerError, TmuxBinding, validate_dimensions
from .process import fingerprint, verify_process
from .shell_models import ShellCapture, ShellControl, ShellFrame, ShellSpec, ShellView, absolute_path
from . import shell_storage as store
from .storage import connect, input_lock
from ..transcripts.probe import ProbeResult, probe_recorder
from ..transcripts.spool import CaptureSpool, RunBinding as CaptureBinding, Source

_TMUX_CONFIG = (b"set-option -g default-terminal xterm-256color\n"
                b"set-option -g exit-unattached off\n"
                b"set-option -g status off\n"
                b"set-window-option -g remain-on-exit on\n")


def _directory(path: Path, *, private: bool = False, create: bool = False) -> None:
    if path.resolve() != path:
        raise RunnerError("invalid_argument", "managed paths cannot cross symlinks")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or (private and stat.S_IMODE(info.st_mode) & 0o077)):
        raise RunnerError("invalid_argument", "managed directory must be owned and private where required")


def _tmux_arg(value: str) -> str:
    # tmux parses even argv: a trailing ';' otherwise separates commands. There
    # is no shell interpolation and non-trailing semicolons stay literal.
    return value[:-1] + "\\;" if value.endswith(";") else value


def _counter(value: int, *, positive=False) -> None:
    if type(value) is not int or not int(positive) <= value <= 2**63 - 1:
        raise RunnerError("invalid_argument", "epoch/sequence is outside its supported range")


def _gone(expected: ProcessFingerprint) -> bool:
    """Only positive evidence of death/reuse; inspection errors are not death."""
    try:
        if UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()) != expected.boot_id:
            return True
        raw = Path(f"/proc/{expected.pid}/stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()
        return int(fields[19]) != expected.start_ticks or fields[0] in {"Z", "X", "x"}
    except (FileNotFoundError, ProcessLookupError):
        return True
    except (OSError, ValueError, IndexError):
        return False


def _program(expected: ProcessFingerprint, executable: Path, argv: list[str], *, parent=None) -> None:
    verify_process(expected)
    root = Path(f"/proc/{expected.pid}")
    try:
        if (not os.path.samefile(root / "exe", executable)
                or (root / "cmdline").read_bytes().split(b"\0")[:-1]
                != [os.fsencode(arg) for arg in argv]):
            raise ValueError("program does not match recorded launch")
        if parent is not None:
            raw = (root / "stat").read_text()
            fields = raw[raw.rindex(")") + 2:].split()
            if int(fields[1]) != parent.pid or int(fields[3]) != expected.pid:
                raise ValueError("child is not the recorder's PTY session leader")
            verify_process(parent)
        verify_process(expected)
    except (OSError, ValueError, IndexError) as exc:
        raise RunnerError("target_changed", "recorded launch program is not corroborated") from exc


def _environment(spec: ShellSpec, terminal_id: UUID) -> dict[str, str]:
    return {
        "PATH": spec.path, "HOME": str(spec.home),
        "CODEX_HOME": str(spec.home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(spec.home / ".claude"),
        "XDG_CONFIG_HOME": str(spec.home / ".config"),
        "XDG_CACHE_HOME": str(spec.home / ".cache"),
        "XDG_DATA_HOME": str(spec.home / ".local/share"),
        "TERM": "xterm-256color", "LC_ALL": "C.UTF-8",
        "QORQUT_TERMINAL_ID": str(terminal_id),
    }


def _capture_binding(row) -> CaptureBinding:
    spec = ShellSpec.model_validate_json(row["spec_json"])
    return CaptureBinding(str(spec.installation_id), str(spec.agent_id), str(spec.recovery_epoch),
                          spec.environment_generation, row["boot_id"], row["terminal_id"], 1)


class ShellManager:
    """All paths are explicit. Construction starts no process or provider CLI."""

    def __init__(self, database, runtime_root, socket_path, python_executable,
                 tmux_executable, shell_executable):
        self.database = absolute_path(database)
        self.runtime_root = absolute_path(runtime_root)
        self.socket_path = absolute_path(socket_path)
        self.python_executable = absolute_path(python_executable)
        self.tmux_executable = absolute_path(tmux_executable)
        self.shell_executable = absolute_path(shell_executable)
        if len(bytes(self.socket_path)) > 100:
            raise ValueError("tmux socket path must fit the local Unix socket limit (100 bytes)")
        for path in (self.python_executable, self.tmux_executable, self.shell_executable):
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError("explicit executable is not an executable file")
        if self.shell_executable.resolve().name != "bash":
            raise ValueError("recorded shell executable must be Bash")
        _directory(self.database.parent)
        if self.database.resolve() != self.database:
            raise ValueError("database cannot cross symlinks")
        if self.database.exists():
            info = self.database.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise ValueError("database must be an owned regular file")
        _directory(self.runtime_root, private=True, create=True)
        _directory(self.socket_path.parent, private=True)
        self._launch_json = json.dumps({key: str(getattr(self, key)) for key in (
            "database", "runtime_root", "socket_path", "python_executable", "tmux_executable",
            "shell_executable",
        )}, sort_keys=True)
        store.ensure_schema(self.database)

    def close(self) -> None:
        """There is no manager-owned live connection and no implicit shutdown."""

    def _row(self, terminal_id: UUID):
        row = store.terminal(self.database, UUID(str(terminal_id)))
        if row["launch_json"] != self._launch_json:
            raise RunnerError("target_changed", "terminal belongs to another local launch configuration")
        return row

    def _argv(self, row) -> list[str]:
        binding = _capture_binding(row)
        return [str(self.python_executable), "-I", "-m", "qorqut_core.transcripts.recorder",
                "--spool", str(self.database), "--installation-id", binding.installation_id,
                "--agent-id", binding.agent_id, "--recovery-epoch", binding.recovery_epoch,
                "--boot-id", binding.boot_id, "--run-id", binding.run_id,
                "--environment-generation", str(binding.environment_generation),
                "--run-generation", "1", "--source-id", row["source_id"],
                "--source-epoch", row["source_epoch"], "--probe-address", "shell-" + binding.run_id,
                "--", str(self.shell_executable), "--noprofile", "--norc", "-i"]

    def _claims(self, row, *, create=False):
        expected = (row["terminal_id"] + "\n" + row["launch_marker"] + "\n"
                    + self._launch_json + "\n").encode()
        for path, content in ((self.runtime_root / "terminal.owner", expected),
                              (self.socket_path.with_name(self.socket_path.name + ".owner"), expected),
                              (self.runtime_root / "tmux.conf", _TMUX_CONFIG)):
            if create:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    with os.fdopen(fd, "wb", closefd=False) as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(fd)
                finally:
                    os.close(fd)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                        or os.read(fd, len(content) + 1) != content):
                    raise RunnerError("target_changed", "shell path ownership is not corroborated")
            finally:
                os.close(fd)

    def ensure(self, spec: ShellSpec) -> ShellView:
        if not isinstance(spec, ShellSpec):
            raise TypeError("spec must be a ShellSpec")
        digest = hashlib.sha256(spec.model_dump_json().encode()).hexdigest()
        with input_lock(self.database, uuid5(spec.installation_id, str(spec.agent_id))):
            with connect(self.database) as db:
                prior = db.execute("SELECT * FROM runner_shell_terminals WHERE installation_id=? AND agent_id=?",
                                   (str(spec.installation_id), str(spec.agent_id))).fetchone()
            if prior is not None:
                if prior["spec_hash"] != digest or prior["launch_json"] != self._launch_json:
                    raise RunnerError("idempotency_conflict", "the agent's terminal slot is already bound differently")
                # A recorded launch intention is never permission to launch again.
                return self.inspect(UUID(prior["terminal_id"]))
            _directory(self.runtime_root, private=True)
            _directory(self.socket_path.parent, private=True)
            with ExitStack() as locks:
                for path in sorted({self.runtime_root / "terminal.lock",
                                    self.socket_path.with_name(self.socket_path.name + ".shell-lock")}):
                    locks.enter_context(store.path_lock(path))
                for path in (self.socket_path, self.runtime_root / "terminal.owner",
                             self.socket_path.with_name(self.socket_path.name + ".owner")):
                    if os.path.lexists(path):
                        raise RunnerError("target_changed", "dedicated shell path is already occupied or ambiguous")
                _directory(spec.home)
                _directory(spec.workspace)
                if os.path.samefile(spec.home, spec.workspace):
                    raise RunnerError("invalid_argument", "HOME and workspace must be distinct directories")
                for suffix in (".codex", ".claude", ".config", ".cache", ".local", ".local/share"):
                    _directory(spec.home / suffix, private=True, create=True)
                terminal_id = uuid4()
                row = dict(terminal_id=str(terminal_id), installation_id=str(spec.installation_id),
                           agent_id=str(spec.agent_id), spec_hash=digest, spec_json=spec.model_dump_json(),
                           launch_json=self._launch_json, runtime_root=str(self.runtime_root),
                           socket_path=str(self.socket_path), boot_id=str(fingerprint(os.getpid()).boot_id),
                           source_id=str(uuid4()), source_epoch=str(uuid4()), launch_marker=uuid4().hex,
                           tmux_json=None, shell_json=None, stopped=0)
                with connect(self.database) as db, db:
                    db.execute("INSERT INTO runner_shell_terminals (" + ",".join(row) + ") VALUES ("
                               + ",".join("?" for _ in row) + ")", tuple(row.values()))
                    db.execute("INSERT INTO runner_shell_control VALUES (?,'unclaimed',0,NULL,0)",
                               (str(terminal_id),))
                self._claims(row, create=True)
                binding = _capture_binding(row)
                with CaptureSpool(self.database) as spool:
                    spool.register_run(binding)
                    spool.register_source(Source(row["source_id"], binding.installation_id, binding.agent_id,
                                                 "terminal"), row["source_epoch"], row["terminal_id"],
                                          capability_evidence="recorder-owned PTY output; no native semantic capture")
                try:
                    self._launch(row)
                except (OSError, subprocess.SubprocessError, RunnerError):
                    return self._view(row, "unknown", "launch_completion_unknown")
                deadline = time.monotonic() + 5
                while True:
                    view = self.inspect(terminal_id)
                    if view.state != "unknown" or time.monotonic() >= deadline:
                        return view
                    time.sleep(0.025)

    def _launch(self, row):
        spec = ShellSpec.model_validate_json(row["spec_json"])
        environment = _environment(spec, UUID(row["terminal_id"]))
        environment["QORQUT_SHELL_LAUNCH"] = row["launch_marker"]
        # Fixed private config sets options before creating the pane. All caller
        # values remain argv; -c is a tmux format, so quote '#' for tmux.
        command = ["new-session", "-d", "-s", "shell-" + row["terminal_id"], "-n", "terminal",
             "-e", "QORQUT_SHELL_LAUNCH=" + row["launch_marker"],
             "-c", str(spec.workspace).replace("#", "##"), "-x", "80", "-y", "24", "--", *self._argv(row)]
        argv = [str(self.tmux_executable), "-S", str(self.socket_path), "-f", str(self.runtime_root / "tmux.conf"),
                *(_tmux_arg(arg) for arg in command)]
        # tmux 3.4 cannot start its first window with global window-size=manual.
        # Set it on the just-created exact window in the same command queue.
        argv += [";", "set-window-option", "-t", "=shell-" + row["terminal_id"] + ":", "window-size", "manual"]
        result = subprocess.run(argv, capture_output=True, env=environment, cwd=spec.workspace, timeout=5)
        if result.returncode:
            raise RunnerError("unavailable", "recorded shell launch did not confirm completion")

    def _server(self, target: TmuxBinding):
        _directory(self.runtime_root, private=True)
        _directory(self.socket_path.parent, private=True)
        info = self.socket_path.lstat()
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid()
                or (info.st_dev, info.st_ino) != (target.socket_device, target.socket_inode)
                or target.server.uid != os.geteuid()):
            raise RunnerError("target_changed", "private tmux socket changed")
        verify_process(target.server)

    def _target(self, row, *, allow_dead=False) -> TmuxBinding:
        self._claims(row)
        info = self.socket_path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise RunnerError("target_changed", "private tmux socket is not owned")
        saved = TmuxBinding.model_validate_json(row["tmux_json"]) if row["tmux_json"] else None
        if saved:
            self._server(saved)
        fields = "#{pid}|#{session_id}|#{window_id}|#{pane_id}|#{pane_pid}|#{pane_dead}|#{session_windows}|#{window_panes}"
        lines = self._command("list-panes", "-s", "-t", "=shell-" + row["terminal_id"], "-F", fields).decode().splitlines()
        if len(lines) != 1:
            raise RunnerError("target_changed", "terminal must have exactly one window and pane")
        server, session, window, pane, pid, dead, windows, panes = lines[0].split("|")
        if windows != "1" or panes != "1" or dead not in ("0", "1"):
            raise RunnerError("target_changed", "recorded terminal topology changed")
        if dead == "1" and not allow_dead:
            raise RunnerError("not_ready", "recorded recorder pane has exited")
        if saved:
            if (str(saved.server.pid), saved.session_id, saved.window_id, saved.pane_id, str(saved.pane.pid)) != (
                    server, session, window, pane, pid):
                raise RunnerError("target_changed", "recorded tmux process/session/pane changed")
            target = saved
        else:
            target = TmuxBinding(socket_path=str(self.socket_path), socket_device=info.st_dev,
                                 socket_inode=info.st_ino, server=fingerprint(int(server)), session_id=session,
                                 window_id=window, pane_id=pane, pane=fingerprint(int(pid)))
        if not os.path.samefile(Path(f"/proc/{target.server.pid}/exe"), self.tmux_executable):
            raise RunnerError("target_changed", "server executable changed")
        marker = self._command("show-environment", "-g", "QORQUT_SHELL_LAUNCH")
        if marker != ("QORQUT_SHELL_LAUNCH=" + row["launch_marker"] + "\n").encode():
            raise RunnerError("target_changed", "private server launch marker changed")
        marker = self._command("show-environment", "-g", "QORQUT_TERMINAL_ID")
        if marker != ("QORQUT_TERMINAL_ID=" + row["terminal_id"] + "\n").encode():
            raise RunnerError("target_changed", "terminal launch marker changed")
        marker = self._command("show-environment", "-t", target.session_id, "QORQUT_SHELL_LAUNCH")
        if marker != ("QORQUT_SHELL_LAUNCH=" + row["launch_marker"] + "\n").encode():
            raise RunnerError("target_changed", "session launch marker changed")
        if self._command("show-options", "-w", "-v", "-t", target.window_id, "window-size").strip() != b"manual":
            raise RunnerError("target_changed", "recorded window size authority changed")
        if dead == "0" and not (allow_dead and saved and _gone(saved.pane)):
            _program(target.pane, self.python_executable, self._argv(row))
        elif not saved or not _gone(saved.pane):
            raise RunnerError("target_changed", "dead-pane process is not corroborated")
        self._server(target)
        return target

    def _view(self, row, state, reason, *, tmux=None, shell=None, capture=None):
        return ShellView(spec=ShellSpec.model_validate_json(row["spec_json"]), terminal_id=row["terminal_id"],
                         state=state, reason=reason, tmux=tmux, recorder=tmux.pane if tmux else None,
                         shell=shell, source_id=row["source_id"], source_epoch=row["source_epoch"],
                         probe_address="shell-" + row["terminal_id"],
                         capture=capture or ProbeResult(verified=False, terminal_active=False, reason="unavailable"),
                         saved_capture=self._saved_capture(row), control=store.control(self.database, row["terminal_id"]))

    def inspect(self, terminal_id: UUID) -> ShellView:
        terminal_id = UUID(str(terminal_id))
        with input_lock(self.database, terminal_id):
            return self._inspect(self._row(terminal_id))

    def _inspect(self, row) -> ShellView:
        saved_shell = ProcessFingerprint.model_validate_json(row["shell_json"]) if row["shell_json"] else None
        if row["stopped"] or (saved_shell is not None and _gone(saved_shell)):
            store.mark_stopped(self.database, row["terminal_id"])
            return self._view(row, "stopped", "shell_exited")
        try:
            target = self._target(row)
            if not row["tmux_json"]:
                store.save_binding(self.database, row["terminal_id"], target, None)
                row = self._row(row["terminal_id"])
            result = probe_recorder("shell-" + row["terminal_id"], _capture_binding(row),
                                    row["source_id"], row["source_epoch"], target.pane)
            if not result.verified or not result.terminal_active:
                return self._view(row, "unknown", "recorder_" + result.reason, tmux=target, capture=result)
            child = result.observation.child
            if saved_shell is not None and child != saved_shell:
                raise RunnerError("target_changed", "recorder child fingerprint changed")
            _program(child, self.shell_executable, [str(self.shell_executable), "--noprofile", "--norc", "-i"],
                     parent=target.pane)
            if self._target(row) != target:
                raise RunnerError("target_changed", "terminal changed during live observation")
            if saved_shell is None:
                store.save_binding(self.database, row["terminal_id"], target, child)
            return self._view(row, "running", "verified", tmux=target, shell=child, capture=result)
        except (OSError, ValueError, RunnerError):
            return self._view(row, "unknown", "binding_unconfirmed")

    def _running(self, row) -> ShellView:
        view = self._inspect(row)
        if view.state != "running":
            raise RunnerError("not_ready", "exact live shell/capture binding is unavailable", reason=view.reason)
        return view

    def _effect(self, target, argv, *, input_effect=True, shell=None):
        self._server(target)
        pairs = [("pid", target.server.pid), ("session_id", target.session_id),
                 ("window_id", target.window_id), ("pane_id", target.pane_id), ("pane_pid", target.pane.pid)]
        if input_effect:
            verify_process(target.pane)
            if shell is None:
                raise RunnerError("not_ready", "input requires the verified shell fingerprint")
            verify_process(shell)
            pairs += [(key, 0) for key in ("pane_dead", "pane_in_mode", "pane_input_off", "pane_synchronized")]
        predicates = ["#{==:#{" + key + "}," + str(value) + "}" for key, value in pairs]
        condition = predicates[0]
        for predicate in predicates[1:]:
            condition = "#{&&:" + condition + "," + predicate + "}"
        # Only generated IDs, fixed verbs and validated integers enter grammar.
        result = self._command("if-shell", "-F", "-t", target.pane_id, condition, " ".join(argv),
                               "display-message -p shell-target-changed")
        if result.strip() == b"shell-target-changed":
            raise RunnerError("target_changed", "terminal changed at the effect boundary")

    def shutdown(self, terminal_id: UUID) -> ShellView:
        """Explicitly terminate only this corroborated recorded tmux session."""
        terminal_id = UUID(str(terminal_id))
        with input_lock(self.database, terminal_id):
            row = self._row(terminal_id)
            try:
                target = self._target(row, allow_dead=True)
                self._effect(target, ["kill-session", "-t", target.session_id], input_effect=False)
            except (OSError, ValueError, RunnerError):
                # Even shutdown never treats an ambiguous socket as its own.
                return self._inspect(row)
            deadline = time.monotonic() + 3
            while True:
                view = self._inspect(row)
                if view.state == "stopped" or time.monotonic() >= deadline:
                    return view
                time.sleep(0.025)

    def attach(self, terminal_id: UUID, viewer_id: UUID, *, read_only=True) -> ShellAttachment:
        if type(read_only) is not bool:
            raise RunnerError("invalid_argument", "read_only must be a boolean")
        terminal_id, viewer_id = UUID(str(terminal_id)), UUID(str(viewer_id))
        with input_lock(self.database, terminal_id):
            view = self._running(self._row(terminal_id))
            return ShellAttachment(self, view, viewer_id, read_only)

    def _frame(self, attachment, epoch, sequence, *, data=None, dimensions=None) -> ShellFrame:
        _counter(epoch)
        _counter(sequence, positive=True)
        if attachment.read_only:
            raise RunnerError("not_ready", "read-only viewer cannot send input or resize")
        with input_lock(self.database, attachment.terminal_id):
            row = self._row(attachment.terminal_id)
            view = self._running(row)
            attachment._verify(view.tmux)
            with connect(self.database) as db, db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT * FROM runner_shell_control WHERE terminal_id=?",
                                     (str(attachment.terminal_id),)).fetchone()
                if (current["mode"] != "human" or current["epoch"] != epoch
                        or current["viewer_id"] != str(attachment.viewer_id)
                        or sequence <= current["last_sequence"]):
                    raise RunnerError("revision_conflict", "stale human input binding or sequence")
                db.execute("UPDATE runner_shell_control SET last_sequence=? WHERE terminal_id=?",
                           (sequence, str(attachment.terminal_id)))
            # Sequence intent is durable before any possible effect. Failure
            # after admission never returns that sequence to the caller to retry.
            buffer = "shell-frame-" + uuid4().hex if data is not None else None
            outcome, count = "uncertain", 0
            try:
                view = self._running(row)
                attachment._verify(view.tmux)
                if buffer:
                    self._command("load-buffer", "-b", buffer, "-", data=data)
                    view = self._running(row)
                    attachment._verify(view.tmux)
                    self._effect(view.tmux, ["paste-buffer", "-d", "-r", "-b", buffer, "-t", view.tmux.pane_id],
                                 shell=view.shell)
                    count = len(data)
                else:
                    columns, rows = dimensions
                    self._effect(view.tmux, ["resize-window", "-t", view.tmux.window_id,
                                            "-x", str(columns), "-y", str(rows)], shell=view.shell)
                    # Mirror geometry is only display state: -r/ignore-size and
                    # manual window sizing keep authority in the gated command.
                    termios.tcsetwinsize(attachment._fd, (rows, columns))
                outcome = "submitted"
            except (OSError, RunnerError, subprocess.SubprocessError):
                pass
            finally:
                if buffer:
                    try:
                        self._server(view.tmux)
                        self._command("delete-buffer", "-b", buffer)
                    except (OSError, RunnerError):
                        pass  # Raw paste -d normally already removed this buffer.
            return ShellFrame(viewer_id=attachment.viewer_id, epoch=epoch, sequence=sequence,
                              outcome=outcome, bytes_written=count)

    def _command(self, *argv: str, data: bytes | None = None) -> bytes:
        try:
            result = subprocess.run(
                [str(self.tmux_executable), "-N", "-S", str(self.socket_path), "-f", "/dev/null",
                 *(_tmux_arg(arg) for arg in argv)],
                input=data, capture_output=True, timeout=3,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": str(self.runtime_root)},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError("unavailable", "tmux command completion is unconfirmed") from exc
        if result.returncode:
            raise RunnerError("unavailable", "tmux did not confirm the exact terminal command")
        return result.stdout

    def _saved_capture(self, row):
        with connect(self.database) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='capture_epochs'").fetchone()
            saved = db.execute("SELECT * FROM capture_epochs WHERE source_id=? AND source_epoch=?",
                               (row["source_id"], row["source_epoch"])).fetchone() if exists else None
        if saved is None:
            return None
        return ShellCapture(**{field: saved[field] for field in ShellCapture.model_fields})

    def control(self, terminal_id: UUID) -> ShellControl:
        self._row(terminal_id)
        return store.control(self.database, terminal_id)

    def take_control(self, terminal_id: UUID, viewer_id: UUID, expected_epoch: int) -> ShellControl:
        return self._control(terminal_id, viewer_id, expected_epoch, take=True)

    def release_control(self, terminal_id: UUID, viewer_id: UUID, expected_epoch: int) -> ShellControl:
        return self._control(terminal_id, viewer_id, expected_epoch, take=False)

    def _control(self, terminal_id, viewer_id, expected_epoch, *, take):
        terminal_id, viewer_id = UUID(str(terminal_id)), UUID(str(viewer_id))
        _counter(expected_epoch)
        with input_lock(self.database, terminal_id):
            self._row(terminal_id)
            with connect(self.database) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM runner_shell_control WHERE terminal_id=?",
                                 (str(terminal_id),)).fetchone()
                if (row["epoch"] != expected_epoch or expected_epoch == 2**63 - 1
                        or (not take and (row["mode"] != "human" or row["viewer_id"] != str(viewer_id)))):
                    raise RunnerError("revision_conflict", "human control binding changed")
                result = ShellControl(mode="human" if take else "unclaimed", epoch=expected_epoch + 1,
                                      viewer_id=viewer_id if take else None, last_sequence=0)
                db.execute("UPDATE runner_shell_control SET mode=?,epoch=?,viewer_id=?,last_sequence=0 "
                           "WHERE terminal_id=?", (result.mode, result.epoch,
                           str(viewer_id) if take else None, str(terminal_id)))
            return result


class ShellAttachment:
    """A disposable read-only tmux mirror, even when logically writable.

    read(max_bytes=65536) returns b'' when no bytes are currently available (or
    after EOF); ``closed`` distinguishes a closed client. write(bytes, epoch=,
    sequence=) sends one authorized raw frame, without framing or implicit Enter.
    resize(columns, rows, epoch=, sequence=) changes the exact tmux window.
    A submitted receipt proves transport submission, never native processing.
    Earlier admitted bytes cannot be retracted by a subsequent control change.
    There is no Python output queue and the viewer is not an archive/replay API.
    """

    def __init__(self, manager, view, viewer_id, read_only):
        self._manager = manager
        self.terminal_id, self.viewer_id, self.read_only = view.terminal_id, viewer_id, read_only
        self._closed, self._fd, self._process = False, -1, None
        self._io_lock = threading.RLock()
        master, slave = os.openpty()
        self._fd = master
        try:
            # In particular disable ECHO before tmux emits terminal discovery
            # escapes. No client output or automatic response becomes input.
            tty.setraw(slave, termios.TCSANOW)
            os.set_blocking(master, False)
            self._process = subprocess.Popen(
                [str(manager.tmux_executable), "-N", "-S", str(manager.socket_path), "-f", "/dev/null",
                 "attach-session", "-E", "-r", "-t", view.tmux.session_id],
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "TERM": "xterm-256color",
                     "HOME": str(manager.runtime_root)},
            )
            self._fingerprint = fingerprint(self._process.pid)
            deadline = time.monotonic() + 3
            while True:
                try:
                    self._verify(view.tmux)
                    break
                except RunnerError:
                    if self._process.poll() is not None or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(slave)

    @property
    def closed(self):
        return self._closed or self._process.poll() is not None

    def _verify(self, target):
        if self.closed:
            raise RunnerError("not_ready", "viewer is disconnected")
        verify_process(self._fingerprint)
        self._manager._server(target)
        lines = self._manager._command("list-clients", "-t", target.session_id, "-F",
                                       "#{client_pid}|#{session_id}|#{pane_id}|#{client_readonly}").decode().splitlines()
        expected = f"{self._fingerprint.pid}|{target.session_id}|{target.pane_id}|1"
        if lines.count(expected) != 1:
            raise RunnerError("target_changed", "viewer is not attached read-only to the exact terminal")

    def read(self, max_bytes=MAX_BODY_BYTES) -> bytes:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BODY_BYTES:
            raise RunnerError("invalid_argument", "read size must be 1-65536 bytes")
        with self._io_lock:
            if self._closed:
                return b""
            try:
                return os.read(self._fd, max_bytes)
            except BlockingIOError:
                return b""
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                return b""  # Linux PTY EOF; close() still owns reaping the client.

    def write(self, data: bytes, *, epoch: int, sequence: int) -> ShellFrame:
        if type(data) is not bytes or not 1 <= len(data) <= MAX_BODY_BYTES:
            raise RunnerError("invalid_argument", "input must be 1-65536 bytes")
        with self._io_lock:
            return self._manager._frame(self, epoch, sequence, data=data)

    def resize(self, columns: int, rows: int, *, epoch: int, sequence: int) -> ShellFrame:
        validate_dimensions(columns, rows)
        with self._io_lock:
            return self._manager._frame(self, epoch, sequence, dimensions=(columns, rows))

    def close(self):
        with self._io_lock:
            self._close()

    def _close(self):
        if self._closed:
            return
        self._closed = True
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        if self._process is not None:
            try:
                self._process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                # This unreaped Popen child cannot be a reused unrelated PID.
                self._process.terminate()
                try:
                    self._process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
