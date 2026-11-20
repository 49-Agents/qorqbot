"""Real processes in a private tmux server: what an ended client leaves behind is ended by identity, nothing else is touched."""
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from qorqut.migration import managed_cutover as cutover

CLIENT = """#!{python}
import signal, subprocess, sys, time
# Stands in for an interactive Claude Code: it starts an MCP bridge helper and, on SIGTERM, exits without ending it.
helper = subprocess.Popen([{python!r}, '-c', 'import time; time.sleep(300)', 'qorqut.bootstrap.mcp_reconnect'])
open({marker!r}, 'w').write(str(helper.pid))
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
while True:
    time.sleep(1)
"""


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class RealLeftovers(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="f3-"))
        self.socket = "f3-" + secrets.token_hex(4)
        self.env = {**os.environ, "TMUX_TMPDIR": str(self.dir)}
        self.env.pop("TMUX", None)
        self.tmux = lambda *a: subprocess.run(["tmux", "-L", self.socket, "-f", "/dev/null", *a], env=self.env, capture_output=True, text=True)
        self.addCleanup(self.cleanup)
        self.marker = self.dir / "helper.pid"
        script = self.dir / "claude"
        script.write_text(CLIENT.format(python=sys.executable, marker=str(self.marker)))
        script.chmod(0o755)
        self.script = script
        self.bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)", "qorqut.bootstrap.mcp_reconnect"],
                                          start_new_session=True)       # same program, another session: someone else's
        self.addCleanup(lambda: (self.bystander.kill(), self.bystander.wait()))

    def cleanup(self):
        self.tmux("kill-server")
        for name in ("helper.pid",):
            try:
                os.kill(int((self.dir / name).read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_helper_that_outlives_the_client_is_ended_and_a_look_alike_is_not(self):
        self.tmux("new-session", "-d", "-s", "agent", "-x", "100", "-y", "20", f"exec bash --norc -c '{self.script}; sleep 300' ")
        deadline = time.time() + 10
        while not self.marker.exists() and time.time() < deadline:
            time.sleep(0.1)
        helper = int(self.marker.read_text())
        machine = cutover.Machine()
        pane = [int(x) for x in self.tmux("list-panes", "-t", "agent", "-F", "#{pane_pid}").stdout.split()]
        children = cutover.children_map(machine)
        pane_state = cutover.Pane(machine, pane, children)
        self.assertEqual(len(pane_state.clients), 1, [c["argv"] for c in pane_state.others])
        client = pane_state.clients[0]
        started = machine.start(helper)
        view = {"_client": {**client, "start": machine.start(client["pid"]), "members": []},
                "_pane_pids": pane, "turn": "idle", "client": "claude"}
        done = []
        cutover.end_client(machine, view, done, force=False, wait_seconds=1, poll=0.2, term_wait=10, child_grace=1.0)
        self.assertFalse(machine.alive(helper, started), done)
        self.assertTrue(machine.alive(self.bystander.pid, machine.start(self.bystander.pid)))
        self.assertTrue(any("leftover process(es) of the client" in item and f"pid {helper}" in item for item in done), done)
        self.assertEqual(done[-1], "client gone (pid and start time verified)")


if __name__ == "__main__":
    unittest.main()
