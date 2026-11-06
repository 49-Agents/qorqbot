"""Connectors the owner sets up for their agents: what each one is and whether it is ready.

Qorqbot itself never contacts these services and keeps no copy of their data. There are three kinds:

* ``install`` - the agent browser: Google Chrome plus the pinned ``agent-browser`` command, which Qorqbot's own browser control
  (``bazarlyk.browser``, the ``browser_turn`` MCP operation) drives for the agents' managed browsers. Qorqbot only checks that
  both are present.
* ``claude`` - a connector of the owner's Claude account (Gmail, Google Calendar, Google Drive, Notion, Slack). The owner
  signs in on claude.ai; Claude Code then offers it to every Claude session, including Qorqbot's agents. Qorqbot only
  reads ``claude mcp list`` to show whether it is connected.
* ``extension`` - a Qorqbot extension (WhatsApp), set up through that extension.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

from qorqut_operations.contracts import DomainError

# Standard locations a GUI-launched service may be missing from PATH; Node itself must be findable by npx's shebang.
SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
PROBE_TIMEOUT = 90
PROBE_FRESH_FOR = 60
CATALOG = (
    {"id": "browser", "kind": "install", "tier": "recommended", "clients": ["claude-code", "codex"]},
    {"id": "gmail", "kind": "claude", "tier": "recommended", "clients": ["claude-code"], "claude_name": "Gmail"},
    {"id": "google_calendar", "kind": "claude", "tier": "suggested", "clients": ["claude-code"],
     "claude_name": "Google Calendar"},
    {"id": "google_drive", "kind": "claude", "tier": "suggested", "clients": ["claude-code"], "claude_name": "Google Drive"},
    {"id": "whatsapp", "kind": "extension", "tier": "suggested", "clients": ["claude-code", "codex"]},
    {"id": "notion", "kind": "claude", "tier": "suggested", "clients": ["claude-code"], "claude_name": "Notion"},
    {"id": "slack", "kind": "claude", "tier": "suggested", "clients": ["claude-code"], "claude_name": "Slack"},
)

# `claude mcp list` prints one line per server, e.g. "claude.ai Gmail: https://... - ✔ Connected".
CLAUDE_LINE = re.compile(r"^claude\.ai (?P<name>[^:]+): .* - (?P<state>.+?)\s*$")


def _atomic_write(path: Path, content: bytes, mode=0o600):
    from bazarlyk.agent_trust import _atomic_write as write
    write(path, content, mode=mode)


def parse_claude_list(text: str) -> dict:
    """``{connector name: "connected" | "needs_sign_in" | "failed"}`` from ``claude mcp list`` output."""
    found = {}
    for line in text.splitlines():
        match = CLAUDE_LINE.match(line.strip())
        if not match:
            continue
        state = match["state"].lower()
        found[match["name"].strip()] = ("connected" if "connected" in state and "fail" not in state
                                        else "needs_sign_in" if "auth" in state else "failed")
    return found


class ClaudeProbe:
    """Runs ``claude mcp list`` in the background (it checks each server, which takes a while) and keeps the last result.

    With ``cache`` (a file in the installation's runtime folder), the last result is also kept across restarts: a new
    server shows it at once, marked as checking, instead of "Checking…" for every connector until Claude answers."""

    def __init__(self, run=subprocess.run, clock=time.monotonic):
        self._run, self._clock, self._lock = run, clock, threading.Lock()
        self._result, self._checked, self._thread, self._cache = None, None, None, None

    def view(self, environ, refresh=False, cache=None) -> dict:
        with self._lock:
            if cache is not None:
                self._cache = Path(cache)
                if self._result is None:
                    self._result = _read_cached(self._cache)
            fresh = self._checked is not None and self._clock() - self._checked < PROBE_FRESH_FOR
            running = self._thread is not None and self._thread.is_alive()
            if (refresh or not fresh) and not running:
                self._thread = threading.Thread(target=self._probe, args=(dict(environ),), daemon=True,
                                                name="qorqbot-claude-connectors")
                self._thread.start()
                running = True
            return {"checking": running, "result": self._result}

    def _account(self, environ):
        """The Claude sign-in agents use: claude.ai connectors belong to that account, so the owner connects on it."""
        try:
            completed = self._run(["claude", "auth", "status"], capture_output=True, text=True, timeout=30,
                                  stdin=subprocess.DEVNULL, env=environ, cwd=environ.get("HOME") or "/")
            status = json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if not isinstance(status, dict):
            return None
        return {"signed_in": status.get("loggedIn") is True, "method": status.get("authMethod"),
                "email": status.get("email") if isinstance(status.get("email"), str) else None,
                "plan": status.get("subscriptionType") if isinstance(status.get("subscriptionType"), str) else None}

    def _probe(self, environ):
        account = self._account(environ)
        try:
            completed = self._run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                                  stdin=subprocess.DEVNULL, env=environ, cwd=environ.get("HOME") or "/")
            result = ({"available": True, "connectors": parse_claude_list(completed.stdout)} if completed.returncode == 0
                      else {"available": False, "reason": "claude_failed"})
        except FileNotFoundError:
            result = {"available": False, "reason": "claude_missing"}
        except (OSError, subprocess.SubprocessError):
            result = {"available": False, "reason": "claude_failed"}
        result["account"] = account
        with self._lock:
            self._result, self._checked, cache = result, self._clock(), self._cache
        if cache is not None:
            try:
                _atomic_write(cache, json.dumps(result).encode(), mode=0o600)
            except OSError:
                pass


def _read_cached(path):
    """The last saved result, if it is a well-formed one."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("available"), bool):
        return None
    if data["available"] and not isinstance(data.get("connectors"), dict):
        return None
    return data


PROBE = ClaudeProbe()


def _probe_environment(environ=None):
    """The environment an agent terminal gets (pty_broker.ensure_session): the invoking user's own HOME and CLI
    configuration, with keys the user did not set removed. Claude keeps a separate sign-in per CLAUDE_CONFIG_DIR, so an
    explicit default would report a different account than the agents use."""
    base = dict(os.environ if environ is None else environ)
    try:
        native = json.loads(base.get("QORQUT_NATIVE_USER_ENV") or "{}")
    except ValueError:
        native = {}
    for key, value in (native.items() if isinstance(native, dict) else ()):
        if value is None:
            base.pop(key, None)
        elif isinstance(value, str):
            base[key] = value
    if not shutil.which("claude", path=base.get("PATH")):
        base["PATH"] = ":".join([base.get("PATH", ""), *SYSTEM_PATH, str(Path(base.get("HOME", "/")) / ".local/bin")])
    return base


def _whatsapp_status(runtime, platform="linux") -> dict:
    connections = getattr(getattr(runtime, "resolver", None), "native_connections", None) or {}
    if "whatsapp" not in connections:
        # The WhatsApp helper is built for Linux x86-64 only (Linux and Windows through WSL2); there is no Mac build yet.
        return {"status": "coming_soon" if platform == "darwin" else "not_installed"}
    instance = Path(runtime.paths.runtime).parent
    if not (instance / "config" / "whatsapp-personal.json").is_file():
        return {"status": "not_set_up"}
    if not (instance / "private" / "whatsapp" / "session.sqlite3").is_file():
        return {"status": "not_paired"}
    return {"status": "connected"}


def host_platform(proc_version="/proc/version") -> str:
    """"darwin", "wsl" (Windows through WSL2) or "linux"."""
    if os.uname().sysname == "Darwin":
        return "darwin"
    try:
        with open(proc_version) as source:
            return "wsl" if "microsoft" in source.read().lower() else "linux"
    except OSError:
        return "linux"


AGENT_BROWSER_PACKAGE = "agent-browser@0.38.1"


def _browser_status() -> dict:
    """The agent browser is Google Chrome plus the agent-browser command, both installed by the owner."""
    from bazarlyk.native_paths import installed_chrome, installed_executable
    missing = [name for name, found in (
        ("chrome", installed_chrome()),
        ("agent_browser", installed_executable("agent-browser", override="QORQUT_AGENT_BROWSER_EXECUTABLE"))) if not found]
    if missing:
        return {"status": "needs_install", "missing": missing, "install": "npm install -g " + AGENT_BROWSER_PACKAGE}
    return {"status": "connected"}


def view(runtime, *, refresh=False, environ=None, probe=None) -> dict:
    """Every connector with its status, for the owner's dashboard."""
    from bazarlyk.agent_trust import clients
    platform = host_platform()
    claude = (probe or PROBE).view(_probe_environment(environ), refresh=refresh,
                                   cache=Path(runtime.paths.runtime) / "connectors-status.json")
    result = claude["result"]
    items = []
    for entry in CATALOG:
        item = {key: value for key, value in entry.items() if key != "claude_name"}
        if entry["kind"] == "install":
            item.update(_browser_status())
        elif entry["kind"] == "extension":
            item.update(_whatsapp_status(runtime, platform))
        elif result is None:
            item["status"] = "checking"
        elif not result["available"]:
            item["status"] = "unavailable"
            item["reason"] = result["reason"]
        else:
            item["status"] = result["connectors"].get(entry["claude_name"], "not_added")
        items.append(item)
    return {"connectors": items, "checking": claude["checking"], "clients": clients(environ), "platform": platform,
            "claude_account": (result or {}).get("account")}


def owner_view(runtime, change=None, environ=None) -> dict:
    """GET (``change`` None) or ``{"refresh": true}``."""
    if change is not None:
        if set(change) != {"refresh"} or change["refresh"] is not True:
            raise DomainError("validation_error", "Only a refresh can be requested")
        return view(runtime, refresh=True, environ=environ)
    return view(runtime, environ=environ)
