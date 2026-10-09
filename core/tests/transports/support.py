"""Positively owned temporary installations and child servers for transport tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import qorqut_core
from qorqut_core.app import Application, register_actor
from qorqut_core.config import InstallationSettings, NewAgentDefaults
from qorqut_core.contracts import Actor, Invocation


FAULT_EXIT = 86  # An owned child's own exit status at its requested fault point.


def write_private(path, value):
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


class Installation:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qorqut-transport-")
        self.root = Path(self.temp.name)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.settings = InstallationSettings(installation_id=uuid4(), source_root=self.root / "source",
            release_root=self.root / "release", data_root=self.root / "data", listen_address="127.0.0.1",
            listen_port=port, busy_timeout_ms=100)
        self.defaults = NewAgentDefaults(cli={"provider": "codex", "model": "synthetic"},
                                        resources={"cpu": 1, "memory_mib": 512, "disk_gib": 4})
        self.owner = Actor(actor_id=uuid4(), kind="owner")
        self.agent = Actor(actor_id=uuid4(), kind="agent", agent_id=uuid4())
        self.installation_file = self.settings.data_root / "installation.json"
        application = Application.initialize(self.settings, self.defaults, self.owner)
        try:
            with application.db.transaction() as tx:
                register_actor(tx, self.agent)
            self.registry = application.registry
            self.initial = application.invoke("config_get", Invocation(actor_id=self.owner.actor_id)).model_dump(mode="json")
            self.initial_events = application.invoke("events_list", Invocation(actor_id=self.owner.actor_id)).data["items"]
        finally:
            application.close()
        self.env = {"PATH": os.defpath, "HOME": str(self.root / "home"), "LC_ALL": "C.UTF-8",
                    "PYTHONPATH": str(Path(qorqut_core.__file__).parent.parent), "PYTHONNOUSERSITE": "1"}
        (self.root / "home").mkdir()
        self.process = None
        self.log = None
        self.log_file = self.root / "server.log"
        self.fault_file = self.root / "fault.json"
        entrypoint = Path(sys.executable).parent / "qorqut-core"
        # Source runs exercise python -m; installed-wheel runs exercise the
        # generated console script for both real serve and operator calls.
        self.entrypoint = [str(entrypoint)] if entrypoint.is_file() else [sys.executable, "-m", "qorqut_core"]

    def headers(self, actor=None):
        return {"X-Qorqut-Actor": str((actor or self.owner).actor_id)}

    def update(self, key="shared-key", revision=1, model="updated"):
        defaults = self.defaults.model_dump(mode="json")
        defaults["cli"]["model"] = model
        return {"idempotency_key": key, "expected_revision": revision, "defaults": defaults}

    def start(self, *, gated=False, ui_root=None, crash_point=None, claim_lease_seconds=None):
        self.log = self.log_file.open("ab")
        command = [*self.entrypoint, "serve"]
        if gated or crash_point is not None or claim_lease_seconds is not None:
            command = [sys.executable, str(Path(__file__).with_name("owned_server.py"))]
        command += ["--installation", str(self.installation_file)]
        if gated:
            command += ["--gate", str(self.root)]
        if ui_root is not None:
            command += ["--ui-root", str(ui_root)]
        if crash_point is not None:
            command += ["--crash-point", crash_point, "--fault-record", str(self.fault_file),
                        "--fault-exit-code", str(FAULT_EXIT)]
        if claim_lease_seconds is not None:
            command += ["--claim-lease-seconds", str(claim_lease_seconds)]
        self.process = subprocess.Popen(command, env=self.env, cwd=self.root,
                                        stdout=self.log, stderr=self.log, stdin=subprocess.DEVNULL)
        if crash_point is not None:
            return  # The child exits itself; await that recorded exit with crashed().
        deadline = time.monotonic() + 15
        with httpx.Client(trust_env=False, timeout=0.3) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AssertionError("Owned server failed to start: " + self.log_file.read_text())
                try:
                    if client.get(self.url + "/health/ready").status_code == 200:
                        self.initial_events = self.post("events_list").json()["data"]["items"]
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.03)
        raise AssertionError("Owned server startup timed out")

    def crashed(self, point, timeout=30):
        """Await the child's own recorded fault exit; any other process failure is an error."""
        try:
            status = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError("Owned server did not reach its fault point") from None
        if status != FAULT_EXIT or not self.fault_file.is_file():
            raise AssertionError(f"Owned server exited with {status} before its fault: " + self.log_file.read_text())
        record = json.loads(self.fault_file.read_text(encoding="utf-8"))
        if record != {"point": point, "pid": self.process.pid}:
            raise AssertionError(f"Owned server recorded a different fault: {record}")

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
                    raise AssertionError("Owned server did not shut down")
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def close(self):
        try:
            self.stop()
        finally:
            self.temp.cleanup()

    def post(self, name, arguments=None, actor=None, **kwargs):
        with httpx.Client(trust_env=False, timeout=5) as client:
            return client.post(self.url + "/api/v1/operations/" + name,
                               json=arguments or {}, headers=self.headers(actor), **kwargs)

    def command(self, *arguments, env=None):
        return subprocess.run([*self.entrypoint, *map(str, arguments)],
                              env={**self.env, **(env or {})}, cwd=self.root, capture_output=True, text=True, timeout=15)
