"""Fixture builder and transient-unit helpers for the real isolated-desktop smoke.

Everything is task-prefixed and temporary: a temp businesses root with its own
database, blank Chrome profile, unused display and ports, and transient *system*
units started with ``sudo systemd-run`` (a user-manager unit gets no network
namespace). Nothing here touches a production unit, profile or database.
"""

import getpass
import grp
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from bazarlyk.browser import config
from bazarlyk.browser.isolated import deployment as deploy_module, setup
from bazarlyk.storage import Database

WORKTREE = Path(__file__).resolve().parents[1]
PYTHON = os.environ.get("QORQUT_SMOKE_PYTHON", "/absolute/path/to/qorqut-code/.venv/bin/python")
BUSINESS = "7a49b000-0000-4000-8000-000000000049"
PREFIX = "t49-smoke"


def sudo(*argv, check=True, timeout=60, **options):
    result = subprocess.run(["sudo", "-n", *argv], timeout=timeout, capture_output=True, text=True, **options)
    if check and result.returncode:
        raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} failed ({result.returncode}): {result.stderr.strip()[:400]}")
    return result


class Smoke:
    def __init__(self, base, credentials, *, proxy, exit_ip, display=39, vnc_port=5939, cdp_range=range(19940, 19950),
                 runtime=None):
        """``runtime`` is a built tools/browser directory (bundled Node, locked packages, relay, wrapper); it is only read."""
        self.base, self.credentials = Path(base), Path(credentials)
        self.proxy, self.exit_ip, self.display, self.vnc_port = proxy, exit_ip, display, vnc_port
        self.cdp_range = cdp_range
        self.tools = Path(runtime)
        self.node = str(self.tools / "node/bin/node")
        self.root = self.base / "root"
        self.gateway_unit, self.session_unit = f"{PREFIX}-gateway", f"{PREFIX}-session"
        self.runtime_name = f"{PREFIX}/{BUSINESS}"
        self.runtime_dir = Path("/run") / self.runtime_name
        self.deployment_path = self.base / "deployment.json"

    # -- fixture ---------------------------------------------------------------------------

    def build(self, *, credentials=None):
        (self.root / ".bazarlyk").mkdir(parents=True, exist_ok=True, mode=0o700)
        database = Database(self.root / ".bazarlyk/state.sqlite3").initialize()
        hq = self.root / "smoke-business" / "hq"
        hq.mkdir(parents=True)
        with database.transaction() as uow:
            uow.repository("businesses").insert({"id": BUSINESS, "slug": "smoke", "name": "Smoke", "workspace": str(hq.parent),
                                                 "hq_path": str(hq)})
        self.assign(database)
        credentials = credentials or json.loads(self.credentials.read_text())
        block = self.saved(database)
        setup.store_credentials(self.root, BUSINESS, block, credentials)
        self.write_deployment()
        deployment = deploy_module.load(self.deployment_path)
        setup.ensure_vnc_password(deployment)
        return database

    def assign(self, database, **overrides):
        original = config.PORTS
        config.PORTS = self.cdp_range
        try:
            with database.transaction() as uow:
                repo = uow.repository("businesses")
                row = repo.get(BUSINESS, required=True)
                value = {**row["config_json"], "browser": {"mode": config.ISOLATED, "enabled": True, "proxy_server": self.proxy,
                                                           "expected_exit_ip": self.exit_ip, **overrides}}
                config.prepare_config(value, row["config_json"], root=self.root, business_id=BUSINESS,
                                      principal_kind="human", uow=uow)
                repo.update(BUSINESS, {"config_json": value}, expected_revision=row["revision"])
        finally:
            config.PORTS = original

    def saved(self, database):
        with database.read() as uow:
            return uow.repository("businesses").get(BUSINESS)["config_json"]["browser"]

    def write_deployment(self):
        self.deployment_path.write_text(json.dumps({
            "schema_version": 1, "business_id": BUSINESS, "root": str(self.root), "display": self.display,
            "geometry": {"width": 1280, "height": 800}, "vnc": {"interface": "127.0.0.1", "port": self.vnc_port},
            "runtime_dir": str(self.runtime_dir), "tools_dir": str(self.tools), "node": self.node, "recheck_seconds": 10}))
        self.deployment_path.chmod(0o600)

    @property
    def state_dir(self):
        return self.root / ".bazarlyk/browsers" / BUSINESS

    # -- units -----------------------------------------------------------------------------

    def _run(self, unit, module, properties):
        account, group = getpass.getuser(), grp.getgrgid(os.getgid()).gr_name
        args = ["systemd-run", "--unit", unit, f"--property=User={account}", f"--property=Group={group}",
                "--property=UMask=0077", "--property=Restart=no", "--property=KillMode=mixed",
                "--property=TimeoutStopSec=60", "--property=NoNewPrivileges=yes",
                f"--setenv=PYTHONPATH={WORKTREE}", "--setenv=PATH=/usr/local/bin:/usr/bin:/bin",
                f"--property=WorkingDirectory={self.base}", *properties,
                PYTHON, "-m", module, "--deployment", str(self.deployment_path)]
        return sudo(*args)

    def start_gateway(self, unit=None, runtime_name=None):
        return self._run(unit or self.gateway_unit, "bazarlyk.browser.isolated.gateway",
                         [f"--property=RuntimeDirectory={runtime_name or self.runtime_name}", "--property=RuntimeDirectoryMode=0700"])

    def start_session(self, *, private_network=True, unit=None, gateway=None, bind=True):
        gateway = gateway or self.gateway_unit
        properties = [f"--property=BindsTo={gateway}.service", f"--property=After={gateway}.service"] if bind else []
        if private_network:
            properties.append("--property=PrivateNetwork=yes")
        return self._run(unit or self.session_unit, "bazarlyk.browser.isolated.session", properties)

    def stop(self, unit):
        sudo("systemctl", "stop", unit + ".service", check=False, timeout=90)
        sudo("systemctl", "reset-failed", unit + ".service", check=False)

    def state(self, unit):
        result = sudo("systemctl", "show", unit + ".service", "-p", "ActiveState", "-p", "SubState", "-p", "Result",
                      "-p", "ExecMainStatus", "-p", "MainPID", check=False)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def journal(self, unit, lines=60):
        return sudo("journalctl", "-u", unit + ".service", "-n", str(lines), "--no-pager", "-o", "cat", check=False).stdout

    def wait(self, predicate, timeout, interval=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        return None

    def wait_ready(self, timeout=120):
        return self.wait(lambda: (self.state_dir / "ready.json").exists() or self.state(self.session_unit).get("ActiveState") == "failed"
                         or self.state(self.session_unit).get("ActiveState") == "inactive" and "exited" in self.journal(self.session_unit, 3), timeout)

    def cleanup(self):
        listed = sudo("systemctl", "list-units", "--all", "--plain", "--no-legend", f"{PREFIX}-*", check=False).stdout
        for unit in [line.split()[0].removesuffix(".service") for line in listed.splitlines() if line.strip()]:
            self.stop(unit)
        for unit in (self.session_unit, self.gateway_unit):
            self.stop(unit)
        sudo("rm", "-rf", str(self.runtime_dir.parent), check=False)
        shutil.rmtree(self.base, ignore_errors=True)
