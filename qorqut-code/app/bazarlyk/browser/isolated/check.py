"""Operator checks for the isolated desktop: preflight, verify and an explicit exit recheck.

    preflight  non-destructive host readiness (collisions, listeners, secrets, tools); writes nothing
    verify     end to end from the host: readiness receipt, CDP through the gateway, the VNC banner
    recheck    ask the running session to repeat its proxy exit check now, then print the result
"""

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sys
import time
from urllib.request import ProxyHandler, build_opener

from qorqut_operations.contracts import DomainError
from ..config import configured_target
from ..control import ready_target
from .deployment import load, open_deployment_database
from .processes import identity_alive
from .setup import preflight


def saved_row(deployment):
    database = open_deployment_database(deployment)
    with database.read() as uow:
        return uow.repository("businesses").get(deployment.business_id, required=True)


def verify(deployment, row):
    """Every claim is observed from the host side: receipt, CDP via the bridge, VNC banner, last exit check."""
    config = configured_target(row, deployment.paths)
    target = ready_target(deployment.state_dir, config)
    report = {"ready": True, "generation": config["generation"], "instance": target["instance"]}
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{config['cdp_port']}/json/version", timeout=5) as response:
        version = json.load(response)
    if not version.get("webSocketDebuggerUrl"):
        raise DomainError("operation_unavailable", "CDP through the gateway returned no browser endpoint")
    report["cdp"] = {"endpoint": f"127.0.0.1:{config['cdp_port']}", "browser": str(version.get("Browser", ""))[:64]}
    with socket.create_connection((deployment.vnc_interface, deployment.vnc_port), timeout=5) as stream:
        banner = stream.recv(12)
    if not banner.startswith(b"RFB 003."):
        raise DomainError("operation_unavailable", "The VNC listener did not answer with an RFB banner")
    report["vnc"] = {"endpoint": f"{deployment.vnc_interface}:{deployment.vnc_port}", "banner": banner.decode().strip()}
    try:
        last = json.loads((deployment.state_dir / "recheck.json").read_text())
        report["exit_check"] = {key: last.get(key) for key in ("result", "observed_exit_ip", "expected_exit_ip", "checked_at")}
    except (OSError, ValueError):
        report["exit_check"] = None
    return report


def recheck(deployment, *, timeout=90):
    """Signal the verified manager process and wait for a newer recheck receipt."""
    directory = deployment.state_dir
    try:
        session = json.loads((directory / "session.json").read_text())
        if not identity_alive(session["manager_pid"], session["manager_identity"]):
            raise ValueError("stale")
    except (OSError, ValueError, KeyError, TypeError):
        raise DomainError("operation_unavailable", "No running isolated browser session was found") from None

    def latest():
        try:
            value = json.loads((directory / "recheck.json").read_text())
            return value if value.get("instance") == session["instance"] else {}
        except (OSError, ValueError):
            return {}

    before = latest().get("sequence", 0)
    os.kill(session["manager_pid"], signal.SIGUSR1)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = latest()
        if current.get("sequence", 0) > before:
            return current
        time.sleep(0.5)
    raise DomainError("operation_unavailable", "The session did not report a new exit check in time")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "verify", "recheck"):
        sub = commands.add_parser(name)
        sub.add_argument("--deployment", required=True, type=Path)
    commands.choices["preflight"].add_argument("--sibling", action="append", default=[], type=Path,
                                               help="another isolated deployment to check for collisions")
    options = parser.parse_args(argv)
    try:
        deployment = load(options.deployment)
        if options.command == "preflight":
            findings = preflight(deployment, saved_row(deployment), siblings=[load(path) for path in options.sibling])
            print(json.dumps({"business_id": deployment.business_id, "ok": all(item["ok"] for item in findings),
                              "findings": findings}, indent=1))
            return 0 if all(item["ok"] for item in findings) else 1
        if options.command == "verify":
            print(json.dumps(verify(deployment, saved_row(deployment)), indent=1))
            return 0
        result = recheck(deployment)
        print(json.dumps({key: result.get(key) for key in ("result", "observed_exit_ip", "expected_exit_ip", "checked_at")}, indent=1))
        return 0 if result.get("result") == "ok" else 1
    except (DomainError, OSError, ValueError, KeyError) as error:
        print("Isolated browser check failed: " + (error.message if isinstance(error, DomainError) else "unreadable state"),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
