"""Connectors the owner sets up for their agents: what each one is, whether it is ready, and turning local ones on.

Qorqbot itself never contacts these services and keeps no copy of their data. There are four kinds:

* ``install`` - the agent browser: Google Chrome plus the ``agent-browser`` command, installed by the owner. Qorqbot only
  checks that both are present.
* ``local`` - a program that runs on this computer, turned on by the owner here: Playwright (an MCP server started with
  ``npx``) and Claude in Chrome (Claude Code's ``--chrome`` flag). With the owner's choice recorded in
  ``connectors.json``, Qorqbot writes ``connectors/launch.bash``; the prepared agent shell (``preparation.py``) reads it
  when it starts Claude Code or Codex, so these reach Qorqbot's agents only and never the owner's other projects. This is
  the owner-consent exception to "external MCP connections come only from installed extensions" (INTEGRATION.md).
* ``claude`` - a connector of the owner's Claude account (Gmail, Google Calendar, Google Drive, Notion, Slack). The owner
  signs in on claude.ai; Claude Code then offers it to every Claude session, including Qorqbot's agents. Qorqbot only
  reads ``claude mcp list`` to show whether it is connected.
* ``extension`` - a Qorqbot extension (WhatsApp), set up through that extension.

Turning a local connector on or off takes effect the next time an agent starts.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import threading
import time

from qorqut_operations.contracts import DomainError, utc_now

STATE_FILE = "connectors.json"
LAUNCH_DIRECTORY = "connectors"
LAUNCH_FILE = "launch.bash"
CLAUDE_MCP_FILE = "claude-mcp.json"
PLAYWRIGHT_PACKAGE = "@playwright/mcp@latest"
# Standard locations a GUI-launched service may be missing from PATH; Node itself must be findable by npx's shebang.
SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
PROBE_TIMEOUT = 90
PROBE_FRESH_FOR = 60
# Serializes the owner's changes; each one reads, changes and rewrites connectors.json.
_CHANGES = threading.Lock()

CATALOG = (
    # Ordubashy browses in the owner's Chrome (signed-in sites) and with Playwright; the managed agent browser serves
    # helper workers, so it is suggested rather than recommended.
    {"id": "chrome", "kind": "local", "tier": "recommended", "clients": ["claude-code"]},
    {"id": "playwright", "kind": "local", "tier": "recommended", "clients": ["claude-code", "codex"]},
    {"id": "gmail", "kind": "claude", "tier": "recommended", "clients": ["claude-code"], "claude_name": "Gmail"},
    {"id": "browser", "kind": "install", "tier": "suggested", "clients": ["claude-code", "codex"]},
    {"id": "google_calendar", "kind": "claude", "tier": "suggested", "clients": ["claude-code"],
     "claude_name": "Google Calendar"},
    {"id": "google_drive", "kind": "claude", "tier": "suggested", "clients": ["claude-code"], "claude_name": "Google Drive"},
    {"id": "whatsapp", "kind": "extension", "tier": "suggested", "clients": ["claude-code", "codex"]},
    {"id": "notion", "kind": "claude", "tier": "suggested", "clients": ["claude-code"], "claude_name": "Notion"},
    {"id": "slack", "kind": "claude", "tier": "suggested", "clients": ["claude-code"], "claude_name": "Slack"},
)
LOCAL = frozenset(entry["id"] for entry in CATALOG if entry["kind"] == "local")

# `claude mcp list` prints one line per server, e.g. "claude.ai Gmail: https://... - ✔ Connected".
CLAUDE_LINE = re.compile(r"^claude\.ai (?P<name>[^:]+): .* - (?P<state>.+?)\s*$")


def state_path(runtime_directory) -> Path:
    return Path(runtime_directory) / STATE_FILE


def launch_path(runtime_directory) -> Path:
    return Path(runtime_directory) / LAUNCH_DIRECTORY / LAUNCH_FILE


def _atomic_write(path: Path, content: bytes, mode=0o600):
    from bazarlyk.agent_trust import _atomic_write as write
    write(path, content, mode=mode)


def load(runtime_directory) -> dict:
    """The owner's recorded choices: ``{"enabled": {id: {"enabled_at": ..., ...}}}``."""
    path = state_path(runtime_directory)
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1, "enabled": {}}
    except (OSError, ValueError) as error:
        raise DomainError("configuration_unavailable", "Connector settings could not be read") from error
    enabled = document.get("enabled") if isinstance(document, dict) else None
    if not isinstance(enabled, dict) or not all(key in LOCAL and isinstance(value, dict) for key, value in enabled.items()):
        raise DomainError("configuration_unavailable", "Connector settings are not valid")
    return {"version": 1, "enabled": enabled}


def find_npx(environ=None):
    """Absolute npx path, from PATH or the owner's login shell (Node managers such as nvm live only there)."""
    environ = os.environ if environ is None else environ
    found = shutil.which("npx", path=environ.get("PATH"))
    if found:
        return found
    shell = environ.get("SHELL") or "/bin/zsh"
    if not os.path.isabs(shell) or not os.access(shell, os.X_OK):
        return None
    try:
        result = subprocess.run([shell, "-lic", "command -v npx"], capture_output=True, text=True, timeout=15,
                                stdin=subprocess.DEVNULL, env=dict(environ))
    except (OSError, subprocess.SubprocessError):
        return None
    for line in reversed(result.stdout.strip().splitlines()):
        if os.path.isabs(line) and os.access(line, os.X_OK):
            return line
    return None


def _server_path(npx: str) -> str:
    directories = [str(Path(npx).parent)]
    directories += [entry for entry in SYSTEM_PATH if entry not in directories]
    return ":".join(directories)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def launch_script(runtime_directory, enabled: dict) -> str:
    """Bash read by the agent shell: the extra arguments each CLI starts with. Every value is shell-quoted."""
    claude, codex = [], []
    playwright = enabled.get("playwright")
    if playwright:
        npx, path = playwright["command"], playwright["path"]
        claude += ["--mcp-config", str(Path(runtime_directory) / LAUNCH_DIRECTORY / CLAUDE_MCP_FILE)]
        codex += ["-c", "mcp_servers.playwright.command=" + _toml_string(npx),
                  "-c", "mcp_servers.playwright.args=[" + _toml_string(PLAYWRIGHT_PACKAGE) + "]",
                  "-c", "mcp_servers.playwright.env={PATH=" + _toml_string(path) + "}"]
    if enabled.get("chrome"):
        claude.append("--chrome")
    return ("# Generated by Qorqbot from connectors.json; the owner turns these on in Settings > Connectors.\n"
            "QORQBOT_CLAUDE_ARGS=(" + " ".join(shlex.quote(value) for value in claude) + ")\n"
            "QORQBOT_CODEX_ARGS=(" + " ".join(shlex.quote(value) for value in codex) + ")\n")


def _write_launch(runtime_directory, enabled: dict):
    directory = Path(runtime_directory) / LAUNCH_DIRECTORY
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    playwright = enabled.get("playwright")
    mcp = directory / CLAUDE_MCP_FILE
    if playwright:
        _atomic_write(mcp, (json.dumps({"mcpServers": {"playwright": {
            "type": "stdio", "command": playwright["command"], "args": [PLAYWRIGHT_PACKAGE],
            "env": {"PATH": playwright["path"]}}}}, indent=2) + "\n").encode())
    else:
        mcp.unlink(missing_ok=True)
    _atomic_write(directory / LAUNCH_FILE, launch_script(runtime_directory, enabled).encode())


def set_enabled(runtime_directory, connector_id, enabled, environ=None) -> dict:
    """Turn a local connector on or off for Qorqbot's agents. Returns the new recorded choices."""
    if connector_id not in LOCAL or type(enabled) is not bool:
        raise DomainError("validation_error", "Only Playwright and Chrome are turned on here")
    if connector_id == "chrome" and enabled and host_platform() == "wsl":
        raise DomainError("validation_error", "Claude in Chrome does not work from WSL on Windows. Use Playwright instead.")
    with _CHANGES:
        return _set_enabled(runtime_directory, connector_id, enabled, environ)


def _set_enabled(runtime_directory, connector_id, enabled, environ):
    choices = dict(load(runtime_directory)["enabled"])
    if not enabled:
        choices.pop(connector_id, None)
    elif connector_id == "playwright":
        npx = find_npx(environ)
        if not npx:
            raise DomainError("prerequisite_missing",
                              "Playwright needs Node.js. Install it from nodejs.org (or `brew install node`), then try again.")
        choices["playwright"] = {"enabled_at": utc_now(), "command": npx, "path": _server_path(npx)}
    else:
        choices[connector_id] = {"enabled_at": utc_now()}
    _write_launch(runtime_directory, choices)
    _atomic_write(state_path(runtime_directory), (json.dumps({"version": 1, "enabled": choices}, indent=2) + "\n").encode())
    return {"version": 1, "enabled": choices}


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
    """Runs ``claude mcp list`` in the background (it checks each server, which takes a while) and keeps the last result."""

    def __init__(self, run=subprocess.run, clock=time.monotonic):
        self._run, self._clock, self._lock = run, clock, threading.Lock()
        self._result, self._checked, self._thread = None, None, None

    def view(self, environ, refresh=False) -> dict:
        with self._lock:
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
            self._result, self._checked = result, self._clock()


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
# The Claude in Chrome extension, as Claude Code's native messaging host allows it (com.anthropic.claude_code_browser_extension).
CLAUDE_IN_CHROME = "fcoeoabgfenejglbffodgkkbkcdhcgfn"


def chrome_extension_installed(home) -> bool:
    """Whether any Google Chrome profile of the owner has the Claude in Chrome extension installed."""
    if not home:
        return False
    roots = (Path(home) / "Library/Application Support/Google/Chrome", Path(home) / ".config/google-chrome")
    for root in roots:
        try:
            profiles = list(root.iterdir())
        except OSError:
            continue
        if any((profile / "Extensions" / CLAUDE_IN_CHROME).is_dir() for profile in profiles):
            return True
    return False


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
    state = load(runtime.paths.runtime)
    platform = host_platform()
    claude = (probe or PROBE).view(_probe_environment(environ), refresh=refresh)
    result = claude["result"]
    items = []
    for entry in CATALOG:
        item = {key: value for key, value in entry.items() if key != "claude_name"}
        if entry["kind"] == "install":
            item.update(_browser_status())
        elif entry["id"] == "chrome" and platform == "wsl" and entry["id"] not in state["enabled"]:
            # Claude in Chrome reaches a Chrome on the same system; WSL's Linux side cannot reach Windows' Chrome.
            item["status"] = "unsupported"
        elif entry["id"] == "chrome":
            # On is not enough: Claude in Chrome also needs its extension in one of the owner's Chrome profiles.
            item["extension"] = chrome_extension_installed(_probe_environment(environ).get("HOME"))
            item["status"] = ("off" if entry["id"] not in state["enabled"]
                              else "connected" if item["extension"] else "needs_extension")
        elif entry["kind"] == "local":
            item["status"] = "connected" if entry["id"] in state["enabled"] else "off"
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
    """GET (``change`` None), ``{"refresh": true}`` or ``{"id": "playwright", "enabled": true}``."""
    if change is not None:
        if set(change) == {"refresh"} and change["refresh"] is True:
            return view(runtime, refresh=True, environ=environ)
        if set(change) != {"id", "enabled"}:
            raise DomainError("validation_error", "Send a connector id and whether it is enabled")
        set_enabled(runtime.paths.runtime, change["id"], change["enabled"], environ)
    return view(runtime, environ=environ)
