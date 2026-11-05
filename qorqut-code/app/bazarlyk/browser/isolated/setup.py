"""Repeatable operator setup for the isolated desktop: secrets, projection and host preflight.

Nothing here writes canonical configuration; the owner API does that. These
helpers create the private files a saved assignment needs, project the saved
assignment into the credential-free ``hq/browser/setup.json``, and report
whether the host is ready to start the two units.
"""

import json
import os
from pathlib import Path
import socket
import stat
import subprocess

from bazarlyk.bootstrap.client import path_has_symlink, write_json_private
from qorqut_operations.contracts import DomainError
from ..config import ISOLATED, check_identity, isolated_paths
from ..control import owns_listener
from ..provision import publish_private
from ..service import credentials_path, read_credentials
from .deployment import Binaries, Deployment, collisions
from .gateway import read_gateway
from .processes import identity_alive

PROJECTION_SCHEMA = 1


def private_directory(path):
    if path_has_symlink(path):
        raise DomainError("invalid_configuration", "Secret directories cannot follow symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        raise DomainError("invalid_configuration", "Secret directories must be private")


def store_credentials(paths, business_id, config, credentials):
    """The isolated credential file, published with no-clobber semantics.

    Identical input is a no-op and different input is never written over an existing file, even by a racing
    direct caller: the file appears complete or not at all, and only one of any competing writers creates it.
    """
    if not credentials["username"] or not credentials["password"]:
        raise DomainError("validation_error", "The isolated browser needs authenticated HTTP proxy credentials")
    path = credentials_path(paths, business_id, config)
    private_directory(path.parent)
    encoded = json.dumps(credentials, sort_keys=True, separators=(",", ":")).encode()
    if publish_private(path, encoded):
        return True
    if read_credentials(path) != (credentials["username"], credentials["password"]):
        raise DomainError("configuration_conflict", "Existing isolated credentials differ; refusing to overwrite them")
    return False


def ensure_vnc_password(deployment, *, vncpasswd="/usr/bin/vncpasswd", generate=lambda: __import__("secrets").token_urlsafe(9)):
    """Random VNC password: plaintext for the Guacamole connection, derived file for Xtigervnc. Never rotated here.

    Each file is published with no-clobber semantics and the derived file always comes from the plaintext that
    is actually on disk, so competing callers converge on one matching pair.
    """
    directory = deployment.secrets_dir
    private_directory(directory.parent)
    private_directory(directory)
    plain, derived = directory / "vnc.password", deployment.vnc_password_file
    if derived.exists() and not plain.exists():
        raise DomainError("configuration_conflict", "A VNC password file exists without its recorded password; explicit repair is required")
    created = False
    if not plain.exists():
        created = publish_private(plain, (generate() + "\n").encode())
    password = plain.read_text().strip()
    if not derived.exists():
        # vncpasswd -f reads the password on stdin and writes the obfuscated file to stdout.
        result = subprocess.run([vncpasswd, "-f"], input=(password + "\n").encode(), capture_output=True, timeout=15, check=True)
        created = publish_private(derived, result.stdout) or created
    for path in (plain, derived):
        if path_has_symlink(path) or path.stat().st_mode & 0o077:
            raise DomainError("invalid_configuration", "VNC password files must be private regular files")
    return created


def projection(row, paths, deployment=None):
    """Credential-free public projection of the saved assignment (never a configuration writer)."""
    browser = row["config_json"].get("browser")
    check_identity(row, paths, browser)
    if browser.get("mode") != ISOLATED:
        raise DomainError("invalid_configuration", "Only an isolated desktop assignment has a setup projection")
    locations = isolated_paths(paths, row)
    value = {"schema": PROJECTION_SCHEMA, "business_id": row["id"], "mode": ISOLATED,
             "assignment": {"enabled": browser["enabled"], "generation": browser["generation"], "cdp_port": browser["cdp_port"]},
             "proxy": {"server": browser["proxy_server"], "expected_exit_ip": browser["expected_exit_ip"],
                       "credentials_reference": browser["credentials_ref"]},
             "paths": {"profile": locations["user_data_dir"], "downloads": locations["downloads_dir"], "setup": locations["setup_path"]},
             "cdp": {"host": "127.0.0.1", "port": browser["cdp_port"]}}
    if deployment is not None:
        value["desktop"] = {"display": deployment.display, "geometry": {"width": deployment.width, "height": deployment.height},
                            "vnc": {"interface": deployment.vnc_interface, "port": deployment.vnc_port}}
    return value


def write_projection(row, paths, deployment=None):
    """Atomic; returns True when the file changed. The profile and downloads folders stay private and unversioned."""
    value = projection(row, paths, deployment)
    locations = isolated_paths(paths, row)
    base = Path(locations["browser_dir"])
    if path_has_symlink(base):
        raise DomainError("invalid_configuration", "Browser folders cannot follow symlinks")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    ignore = base / ".gitignore"
    if not path_has_symlink(ignore) and not ignore.exists():
        ignore.write_text("*\n")
    target = Path(locations["setup_path"])
    if path_has_symlink(target):
        raise DomainError("invalid_configuration", "Browser folders cannot follow symlinks")
    if target.exists() and json.loads(target.read_text()) == value:
        return False
    write_json_private(target, value)
    return True


def _listener_free(address, port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((address, port))
        except OSError:
            return False
    return True


def _finding(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": detail}


def preflight(deployment: Deployment, row, *, binaries=Binaries(), x_directory="/tmp", siblings=(), desktops=()):
    """Non-destructive host readiness for one isolated business. Never starts, kills or deletes anything."""
    paths, business_id = deployment.paths, deployment.business_id
    findings = []
    browser = row["config_json"].get("browser") or {}
    try:
        check_identity(row, paths, browser)
        assigned = browser.get("mode") == ISOLATED
    except DomainError:
        assigned = False
    findings.append(_finding("assignment", assigned, "" if assigned else "saved browser block is not a valid isolated assignment"))
    problems = collisions([deployment, *siblings], desktops)
    findings.append(_finding("deployment collisions", not problems, "; ".join(problems)))
    running = False
    try:
        gateway = read_gateway(deployment)
        running = identity_alive(gateway["pid"], gateway["identity"])
    except (OSError, ValueError, KeyError):
        pass
    try:
        credentials = read_credentials(credentials_path(paths, business_id, browser if assigned else {"mode": ISOLATED}))
        findings.append(_finding("isolated proxy credentials", bool(credentials[0]),
                                 "" if credentials[0] else "the isolated mode requires authenticated credentials"))
    except DomainError:
        findings.append(_finding("isolated proxy credentials", False, "missing, unreadable or not private"))
    for name in ("vnc.password", "vnc.passwd"):
        path = deployment.secrets_dir / name
        try:
            ok = path.is_file() and not path_has_symlink(path) and not path.stat().st_mode & 0o077
        except OSError:
            ok = False
        findings.append(_finding("VNC " + name, ok, "" if ok else "missing or not private (run provisioning)"))
    if not running:
        lock = Path(x_directory) / f".X{deployment.display}-lock"
        busy = False
        try:
            os.kill(int(lock.read_text().strip()), 0)
            busy = True
        except (FileNotFoundError, ValueError, ProcessLookupError):
            pass
        except PermissionError:
            busy = True
        busy = busy or (Path(x_directory) / ".X11-unix" / f"X{deployment.display}").exists()
        findings.append(_finding("display free", not busy, f"{deployment.display_name} is in use" if busy else ""))
        findings.append(_finding("VNC address free", _listener_free(deployment.vnc_interface, deployment.vnc_port),
                                 f"{deployment.vnc_interface}:{deployment.vnc_port}"))
        if browser.get("cdp_port"):
            findings.append(_finding("CDP loopback port free", _listener_free("127.0.0.1", browser["cdp_port"]),
                                     f"127.0.0.1:{browser['cdp_port']}"))
    else:
        findings.append(_finding("gateway", True, "running; listeners are held by the recorded gateway"))
    state = deployment.state_dir
    stopped = not (state / "ready.json").exists()
    findings.append(_finding("browser state", stopped or running, "ready receipt present" if not stopped else ""))
    for label, path in (("relay", deployment.tools_dir / "relay.cjs"), ("proxy-chain", deployment.tools_dir / "node_modules/proxy-chain")):
        findings.append(_finding("tools " + label, path.exists(), str(path)))
    if (deployment.tools_dir / "run-agent-browser").exists():
        # A bundled-Node runtime: agent control and the relay must both run on its own Node.
        bundled = deployment.tools_dir / "node/bin/node"
        findings.append(_finding("tools bundled node", os.access(bundled, os.X_OK) and bundled.is_file(), str(bundled)))
        findings.append(_finding("relay uses the bundled node", os.path.realpath(deployment.node) == os.path.realpath(bundled),
                                 deployment.node))
    for label, path in (("Xtigervnc", binaries.xvnc), ("openbox", binaries.window_manager), ("Chrome", binaries.chrome),
                        ("xauth", binaries.xauth), ("xdpyinfo", binaries.xdpyinfo), ("xset", binaries.xset)):
        findings.append(_finding(label, os.access(path, os.X_OK), path))
    node = deployment.node
    findings.append(_finding("node", os.access(node, os.X_OK) if node.startswith("/") else any(
        os.access(Path(part) / node, os.X_OK) for part in os.environ.get("PATH", "").split(":")), node))
    return findings
