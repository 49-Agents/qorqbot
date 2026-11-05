"""Read-only facts for the owner's Settings page: the installation (About) and the agent CLIs (Agents).

Both are owner-only (``transport/http.py``). The CLI facts are read with the environment an agent terminal gets
(``connectors._probe_environment``), so they describe the Claude Code and Codex the agents actually run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

CLI_TIMEOUT = 15


def _payload_version(root):
    try:
        return json.loads((Path(root) / "runtime" / "PAYLOAD.json").read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def about_view(environ=None) -> dict:
    """The Qorqbot version and where it keeps its program, data and logs. Folder installs set QORQUT_FOLDER_ROOT."""
    from bazarlyk.connectors import host_platform
    env = os.environ if environ is None else environ
    root = env.get("QORQUT_FOLDER_ROOT")
    folder = Path(root) if root and Path(root).is_absolute() else None
    instance = folder / "instance" if folder else None
    return {"version": _payload_version(folder) if folder else None,
            "install_folder": str(folder) if folder else None,
            "data_folder": str(instance) if instance and instance.is_dir() else None,
            "logs_folder": str(instance / "logs") if instance and (instance / "logs").is_dir() else None,
            "platform": host_platform()}


def _run(run, command, env):
    try:
        completed = run(command, capture_output=True, text=True, timeout=CLI_TIMEOUT, stdin=subprocess.DEVNULL,
                        env=env, cwd=env.get("HOME") or "/")
    except (OSError, subprocess.SubprocessError):
        return None
    return completed


def _first_line(completed):
    if completed is None or completed.returncode != 0:
        return None
    lines = (completed.stdout or "").strip().splitlines()
    return lines[0].strip() if lines else None


def _claude(env, run):
    path = shutil.which("claude", path=env.get("PATH"))
    if not path:
        return {"installed": False}
    version = _first_line(_run(run, [path, "--version"], env))
    status = _run(run, [path, "auth", "status"], env)
    account = None
    try:
        data = json.loads(status.stdout) if status is not None else None
    except ValueError:
        data = None
    if isinstance(data, dict):
        account = {"signed_in": data.get("loggedIn") is True, "method": data.get("authMethod"),
                   "email": data.get("email") if isinstance(data.get("email"), str) else None,
                   "plan": data.get("subscriptionType") if isinstance(data.get("subscriptionType"), str) else None}
    return {"installed": True, "path": path, "version": version.split(" ")[0] if version else None,
            "signed_in": bool(account and account["signed_in"]), "account": account}


def _codex(env, run):
    path = shutil.which("codex", path=env.get("PATH"))
    if not path:
        return {"installed": False}
    version = _first_line(_run(run, [path, "--version"], env))
    status = _run(run, [path, "login", "status"], env)
    text = ((status.stdout or "") + (status.stderr or "")).strip() if status is not None else ""
    signed_in = status is not None and status.returncode == 0 and "logged in" in text.lower()
    return {"installed": True, "path": path, "version": version.split(" ")[-1] if version else None,
            "signed_in": signed_in, "detail": text.splitlines()[0] if text else None}


def agent_clients_view(environ=None, run=subprocess.run) -> dict:
    """Whether Claude Code and Codex are installed for the agents, their versions, and whether each is signed in."""
    from bazarlyk.connectors import _probe_environment
    env = _probe_environment(environ)
    return {"claude": _claude(env, run), "codex": _codex(env, run)}
