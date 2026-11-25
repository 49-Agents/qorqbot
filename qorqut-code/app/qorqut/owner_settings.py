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
_NAMES = {"claude": "Claude Code", "codex": "Codex"}


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
    from qorqut import account_state, agent_cli_sign_in as sign_in
    found = sign_in.resolve_cli(provider, env)
    install = sign_in.install_status(provider, found, env)
    # ``sign_in``: the state and reason, and while one is waiting the page, the code and what it replaces (``sign_in_details``).
    # ``limit``: the product's usage limit (one agent that hit it marks it for all). ``switched``: the last sign-in since the
    # service started, with how many agents it restarted (``account_state``).
    row = {"installed": found is not None, "source": found["source"] if found else None,
           "install": {"phase": install.get("phase"), "progress": install.get("progress"), "message": install.get("message")},
           "sign_in": {**sign_in.sign_in_state(provider), **sign_in.sign_in_details(provider)},
           "sign_in_methods": {"available": ["browser", "device_code"] if provider == "codex" else ["browser"],
                               "default": sign_in.default_method(provider)},
           "limit": account_state.limit(provider), "switched": account_state.switch(provider)}
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
    from qorqut import account_state
    from qorqut.connectors import _probe_environment
    env = _probe_environment(environ)
    view = {"claude": _client("claude", env, run), "codex": _client("codex", env, run)}
    account_state.remember_clients(view)         # Chat names the account in its notices from this, for a short while
    return view


def for_agents(view) -> dict:
    """The view with the waiting sign-in's page and code left out: those are for the owner alone, and what an agent reads ends up in
    its conversation record."""
    return {name: ({**row, "sign_in": {**row["sign_in"], "page_url": None, "user_code": None}}
                   if isinstance(row, dict) and isinstance(row.get("sign_in"), dict) else row) for name, row in view.items()}


ACTIONS = ("download", "cancel_download", "connect", "switch", "cancel_connect", "submit_code")


def agent_clients_action(provider, action, environ=None, run=subprocess.run, code=None, method=None) -> dict:
    """The owner pressed a button on one provider's row: download or stop downloading its CLI, start or stop its sign-in, switch
    to another account, or give the sign-in the code its page shows. Returns the same view as ``agent_clients_view``.

    ``connect`` signs in; on a product that is already signed in it is the same as ``switch`` (the old sign-in stays in use until
    the new one is complete). ``switch`` refuses a product that is not signed in. ``method`` (``browser`` or ``device_code``,
    Codex only) overrides how Codex signs in; ``code`` goes with ``submit_code`` only."""
    from qorqut import agent_cli_sign_in as sign_in
    from qorqut.errors import DomainError
    if provider not in sign_in.PROVIDERS or action not in ACTIONS:
        raise DomainError("validation_error", "Unknown agent client or action")
    if action in ("download", "cancel_download"):
        install = sign_in._install()
        if install is None:
            raise DomainError("operation_unavailable", "Downloading agent clients is not available in this build")
        (install.start_download if action == "download" else install.cancel_download)(provider)
    elif action in ("connect", "switch"):
        if action == "switch" and not agent_clients_view(environ, run=run)[provider]["signed_in"]:
            raise DomainError("validation_error", f"{_NAMES[provider]} is not signed in. Sign in first.")
        try:
            sign_in.start_sign_in(provider, environ=environ, **({"method": method} if method else {}))
        except sign_in.SignInError as error:
            raise DomainError(error.code, error.message) from None
    elif action == "submit_code":
        try:
            sign_in.submit_code(provider, code)
        except sign_in.SignInError as error:
            raise DomainError(error.code, error.message) from None
    else:
        sign_in.cancel_sign_in(provider)
    return agent_clients_view(environ, run=run)
