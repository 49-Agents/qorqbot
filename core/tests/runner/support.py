import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from qorqut_core.runner import RunBinding, TerminalRunner, Tmux, TmuxBinding, fingerprint


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("fixture condition did not become true")


class Receiver:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="terminal-r1-")
        self.root = Path(self.temporary.name)
        self.socket = self.root / "tmux.sock"
        self.command = [shutil.which("tmux"), "-S", str(self.socket), "-f", "/dev/null"]
        self.env = {"PATH": "/usr/bin:/bin", "TERM": "xterm-256color", "HOME": str(self.root)}
        subprocess.run([*self.command, "new-session", "-d", "-s", "receiver", "-x", "100", "-y", "30",
                        "--", sys.executable, str(Path(__file__).with_name("receiver.py")), str(self.root)],
                       check=True, capture_output=True, env=self.env)
        try:
            wait_for(lambda: (self.root / "state.json").exists())
            raw = self.command_run("display-message", "-p", "-t", "%0",
                                   "#{pid}|#{session_id}|#{window_id}|#{pane_id}|#{pane_pid}")
            server, session, window, pane, pid = raw.decode().strip().split("|")
            process = fingerprint(int(pid))
            sock = self.socket.stat()
            self.binding = RunBinding(
                agent_id=uuid4(), recovery_epoch=uuid4(), environment_generation=1,
                boot_id=process.boot_id, run_id=uuid4(), run_generation=1,
                tmux=TmuxBinding(socket_path=str(self.socket), socket_device=sock.st_dev,
                                 socket_inode=sock.st_ino, server=fingerprint(int(server)),
                                 session_id=session, window_id=window, pane_id=pane, pane=process),
                recorder=process, native=process,
            )
            self.command_run("set-option", "-w", "-t", window, "window-size", "manual")
        except BaseException:
            # This constructor alone created the new private socket/session.
            self.command_run("kill-session", "-t", "$0")
            self.temporary.cleanup()
            raise
        self.database = self.root / "spool.sqlite3"

    def command_run(self, *args):
        return subprocess.run([*self.command, *args], check=True, capture_output=True,
                              env=self.env, timeout=5).stdout

    def state(self):
        return json.loads((self.root / "state.json").read_text())

    def bytes(self):
        return (self.root / "input.bin").read_bytes()

    def classify(self, binding, snapshot):
        return "empty" if self.state()["current_bytes"] == 0 else "blocked"

    def verify(self, binding, snapshot, digest):
        return bool(wait_for(lambda: self.state()["last_paste"] == digest
                            and self.state()["current_sha256"] == digest))

    def runner(self, **overrides):
        options = dict(capture_ready=lambda _: True, machine_hold=lambda _: False,
                       classify_composer=self.classify, verify_paste=self.verify)
        options.update(overrides)
        runner = TerminalRunner(self.database, **options)
        runner.register_binding(self.binding)
        return runner

    def close(self):
        # Kill only the positively owned session, never a shared tmux server.
        Tmux().inspect(self.binding)
        self.command_run("kill-session", "-t", self.binding.tmux.session_id)

        def exited(expected):
            try:
                raw = Path(f"/proc/{expected.pid}/stat").read_text()
            except FileNotFoundError:
                return True
            fields = raw[raw.rindex(")") + 2:].split()
            return fields[0] in ("Z", "X", "x") or int(fields[19]) != expected.start_ticks

        wait_for(lambda: exited(self.binding.tmux.server) and exited(self.binding.native))
        self.temporary.cleanup()


class FakeTmux:
    """Faults at tmux input boundaries; real tmux bytes have separate assertions."""
    def __init__(self, binding):
        from qorqut_core.runner import TerminalSnapshot
        self.binding = binding
        self.snapshot = TerminalSnapshot(columns=100, rows=30, cursor_x=0, cursor_y=0,
            cursor_visible=True, in_mode=False, input_off=False, synchronized=False, screen="fixture")
        self.effects = []
        self.buffers = {}
        self.fail = None
        self.after_load = None

    def inspect(self, binding):
        from qorqut_core.runner import RunnerError
        if binding != self.binding:
            raise RunnerError("target_changed", "synthetic replaced binding")
        return self.snapshot

    def load_buffer(self, binding, buffer, body):
        self.inspect(binding)
        if self.fail == "load":
            raise OSError("synthetic load failure")
        self.buffers[buffer.name] = body
        if self.after_load:
            self.after_load()

    def paste(self, binding, buffer, *, bracketed=True):
        self.inspect(binding)
        self.effects.append(("paste", self.buffers.pop(buffer.name)))
        if self.fail == "paste":
            raise OSError("paste response lost")

    def enter(self, binding):
        self.inspect(binding)
        self.effects.append(("enter",))
        if self.fail == "enter":
            raise OSError("Enter response lost")

    def resize(self, binding, columns, rows):
        self.effects.append(("resize", columns, rows))

    def delete_buffer(self, binding, buffer):
        self.buffers.pop(buffer.name, None)


def synthetic_binding():
    # Explicit synthetic launch record. No discovery/adoption or real effects.
    from qorqut_core.runner import ProcessFingerprint
    boot = uuid4()
    process = ProcessFingerprint(boot_id=boot, pid=123, start_ticks=100, uid=1000, pid_namespace=10)
    return RunBinding(agent_id=uuid4(), recovery_epoch=uuid4(), environment_generation=1,
                      boot_id=boot, run_id=uuid4(), run_generation=1,
                      tmux=TmuxBinding(socket_path="/fixture/owned.sock", socket_device=1,
                                       socket_inode=1, server=process, pane=process,
                                       session_id="$0", window_id="@0", pane_id="%0"),
                      recorder=process, native=process)
