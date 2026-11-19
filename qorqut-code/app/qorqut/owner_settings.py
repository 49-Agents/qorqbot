"""Read-only facts for the owner's Settings page: the installation (About) and the agent CLIs (Agents).

Both are owner-only (``transport/http.py``). The CLI facts are read with the environment an agent terminal gets
(``connectors._probe_environment``), so they describe the Claude Code and Codex the agents actually run. Where a CLI is,
how it is downloaded and how the owner signs in are in ``agent_cli_install`` and ``agent_cli_sign_in``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

CLI_TIMEOUT = 15


def _payload_version(root):
    try:
        return json.loads((Path(root) / "runtime" / "PAYLOAD.json").read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def about_view(environ=None, runtime_directory=None) -> dict:
    """The Qorqbot version, where it keeps its program, data and logs (folder installs set QORQUT_FOLDER_ROOT), and whether
    the owner has to install Google Chrome. The agents' browsers run on Chrome; Qorqbot downloads its own, so
    ``chrome_installed`` is false only when no Chrome is installed and Qorqbot's own cannot be used on this computer."""
    from qorqut.connectors import host_platform
    from qorqut.native_paths import installed_chrome
    from qorqut.browser import managed_chrome
    own = runtime_directory is not None and managed_chrome.view(runtime_directory)["state"] in {
        "ready", "waiting", "downloading", "failed"}
    env = os.environ if environ is None else environ
    root = env.get("QORQUT_FOLDER_ROOT")
    folder = Path(root) if root and Path(root).is_absolute() else None
    instance = folder / "instance" if folder else None
    return {"version": _payload_version(folder) if folder else None,
            "install_folder": str(folder) if folder else None,
            "data_folder": str(instance) if instance and instance.is_dir() else None,
            "logs_folder": str(instance / "logs") if instance and (instance / "logs").is_dir() else None,
            "platform": host_platform(), "chrome_installed": installed_chrome() is not None or own}


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


_VERSIONS: dict = {}     # (path, mtime) -> (monotonic time it expires | None, version); the CLIs are slow to start and the dashboard polls
VERSION_FAILURE_TTL = 120    # seconds a failed or timed-out `--version` is not tried again


def _version(path, env, run, last_word):
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        key = None
    cached = _VERSIONS.get(key) if key is not None else None
    if cached and (cached[0] is None or time.monotonic() < cached[0]):
        return cached[1]
    line = _first_line(_run(run, [path, "--version"], env))
    version = (line.split(" ")[-1 if last_word else 0] if line else None)
    if key is not None:
        _VERSIONS[key] = (None, version) if version else (time.monotonic() + VERSION_FAILURE_TTL, None)
    return version


def _client(provider, env, run):
    """One CLI's row: where it came from, what installing it is doing, whether it is signed in, and any sign-in in
    progress. The download is Qorqbot's own (``agent_cli_install``); the sign-in is the owner's, in the browser."""
    from qorqut import agent_cli_sign_in as sign_in
    found = sign_in.resolve_cli(provider, env)
    install = sign_in.install_status(provider, found)
    row = {"installed": found is not None, "source": found["source"] if found else None,
           "install": {"phase": install.get("phase"), "progress": install.get("progress"), "message": install.get("message")},
           "sign_in": sign_in.sign_in_state(provider)}
    if not found:
        return {**row, "version": install.get("version"), "signed_in": False, "account": None}
    run_env = {**env, **sign_in.launch_environment(provider)}
    version = found.get("version") or _version(found["path"], run_env, run, provider == "codex")
    if provider == "claude":
        status = sign_in.claude_status(run_env, cli_path=found["path"], run=run)
        return {**row, "path": found["path"], "version": version, "signed_in": status["signed_in"], "account": status["account"]}
    status = sign_in.codex_status(run_env, cli_path=found["path"], run=run)
    return {**row, "path": found["path"], "version": version, "signed_in": status["signed_in"], "account": status["account"]}


def agent_clients_view(environ=None, run=subprocess.run) -> dict:
    """Whether Claude Code and Codex are installed for the agents (and from where), their versions and download
    progress, whether each is signed in, and any sign-in in progress. Claude's sign-in is read from its stored
    credential, never by asking the CLI each time (``agent_cli_sign_in.claude_status``)."""
    from qorqut.connectors import _probe_environment
    env = _probe_environment(environ)
    return {"claude": _client("claude", env, run), "codex": _client("codex", env, run)}


def agent_clients_action(provider, action, environ=None, run=subprocess.run) -> dict:
    """The owner pressed a button on one provider's row: download or stop downloading its CLI, or start or stop its
    browser sign-in. Returns the same view as ``agent_clients_view``."""
    from qorqut import agent_cli_sign_in as sign_in
    from qorqut.errors import DomainError
    if provider not in sign_in.PROVIDERS or action not in ("download", "cancel_download", "connect", "cancel_connect"):
        raise DomainError("validation_error", "Unknown agent client or action")
    if action in ("download", "cancel_download"):
        install = sign_in._install()
        if install is None:
            raise DomainError("operation_unavailable", "Downloading agent clients is not available in this build")
        (install.start_download if action == "download" else install.cancel_download)(provider)
    elif action == "connect":
        sign_in.start_sign_in(provider, environ=environ)
    else:
        sign_in.cancel_sign_in(provider)
    return agent_clients_view(environ, run=run)
