"""Connectors the owner sets up for their agents: what each one is, whether it is ready, and turning local ones on.

Qorqbot itself never contacts these services and keeps no copy of their data. There are four kinds:

* ``install`` - the agent browser: Google Chrome plus the ``agent-browser`` command, installed by the owner. Qorqbot only
  checks that both are present.
* ``local`` - a program that runs on this computer, turned on by the owner here: Playwright (an MCP server started with
  ``npx``) and Claude in Chrome (Claude Code's ``--chrome`` flag). With the owner's choice recorded in
  ``connectors.json``, Qorqbot writes ``connectors/launch.bash``; the prepared agent shell (``preparation.py``) reads it
  when it starts Claude Code or Codex, so these reach Qorqbot's agents only and never the owner's other projects. This is
  the owner-consent exception to "external MCP connections come only from installed extensions" (INTEGRATION.md).
* ``claude`` - an online service (Gmail, Notion, Linear...). Claude and Codex reach these differently, so each has its own
  status. Claude: a connector of the owner's claude.ai account, which Claude Code offers to every Claude session; the
  owner connects it on claude.ai and Qorqbot reads ``claude mcp list``. Codex: the service's public MCP address
  (``codex_url``) in the owner's Codex configuration (``codex_connectors``); services without one are Claude only.
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

from bazarlyk import codex_connectors

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
    # Codex addresses are listed only where the service's sign-in works for Codex (dynamic client registration);
    # Asana, HubSpot and Figma refuse it or need a pasted token, so they stay Claude only.
    {"id": "notion", "kind": "claude", "tier": "suggested", "clients": ["claude-code", "codex"], "claude_name": "Notion",
     "codex_url": "https://mcp.notion.com/mcp"},
    {"id": "slack", "kind": "claude", "tier": "suggested", "clients": ["claude-code"], "claude_name": "Slack"},
    {"id": "linear", "kind": "claude", "tier": "more", "clients": ["claude-code", "codex"], "claude_name": "Linear",
     "codex_url": "https://mcp.linear.app/mcp"},
    {"id": "atlassian", "kind": "claude", "tier": "more", "clients": ["claude-code", "codex"], "claude_name": "Atlassian",
     "codex_url": "https://mcp.atlassian.com/v1/mcp"},
    {"id": "sentry", "kind": "claude", "tier": "more", "clients": ["claude-code", "codex"], "claude_name": "Sentry",
     "codex_url": "https://mcp.sentry.dev/mcp"},
    {"id": "asana", "kind": "claude", "tier": "more", "clients": ["claude-code"], "claude_name": "Asana"},
    {"id": "hubspot", "kind": "claude", "tier": "more", "clients": ["claude-code"], "claude_name": "HubSpot"},
    {"id": "figma", "kind": "claude", "tier": "more", "clients": ["claude-code"], "claude_name": "Figma"},
    {"id": "canva", "kind": "claude", "tier": "more", "clients": ["claude-code"], "claude_name": "Canva"},
    {"id": "stripe", "kind": "claude", "tier": "more", "clients": ["claude-code"], "claude_name": "Stripe"},
)
LOCAL = frozenset(entry["id"] for entry in CATALOG if entry["kind"] == "local")
CODEX_URLS = {entry["id"]: entry["codex_url"] for entry in CATALOG if entry.get("codex_url")}
CATALOG_NAMES = frozenset(entry["claude_name"] for entry in CATALOG if entry.get("claude_name"))

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
    """The owner's recorded choices: ``{"enabled": {id: {"enabled_at": ..., ...}}, "codex_added": {id: codex name}}``."""
    path = state_path(runtime_directory)
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 1, "enabled": {}, "codex_added": {}}
    except (OSError, ValueError) as error:
        raise DomainError("configuration_unavailable", "Connector settings could not be read") from error
    enabled = document.get("enabled") if isinstance(document, dict) else None
    if not isinstance(enabled, dict) or not all(key in LOCAL and isinstance(value, dict) for key, value in enabled.items()):
        raise DomainError("configuration_unavailable", "Connector settings are not valid")
    added = document.get("codex_added", {})
    if not isinstance(added, dict) or not all(key in CODEX_URLS and isinstance(value, str) for key, value in added.items()):
        raise DomainError("configuration_unavailable", "Connector settings are not valid")
    return {"version": 1, "enabled": enabled, "codex_added": added}


def _save(runtime_directory, state):
    _atomic_write(state_path(runtime_directory), (json.dumps(
        {"version": 1, "enabled": state["enabled"], "codex_added": state["codex_added"]}, indent=2) + "\n").encode())


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
    state = load(runtime_directory)
    choices = dict(state["enabled"])
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
    state = {**state, "enabled": choices}
    _save(runtime_directory, state)
    return state


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


def _claude_state(entry, result) -> dict:
    if result is None:
        return {"status": "checking"}
    if not result["available"]:
        return {"status": "unavailable", "reason": result["reason"]}
    return {"status": result["connectors"].get(entry["claude_name"], "not_added")}


def _codex_state(entry, listed, added, codex) -> dict:
    url = entry.get("codex_url")
    if not url:
        return {"status": "not_available"}
    if not listed["available"]:
        return {"status": "unavailable", "reason": listed["reason"]}
    state = codex_connectors.state_for(url, listed["servers"])
    state["managed"] = added.get(entry["id"]) is not None and added.get(entry["id"]) == state.get("name")
    sign_in = codex.sign_in(entry["id"])
    if sign_in and (sign_in["running"] or sign_in["failed"]) and state["status"] != "connected":
        state["sign_in"] = sign_in
    return state


# What one status means for "is it usable at all": the item's overall status is its best client status.
_RANK = ("connected", "needs_sign_in", "needs_extension", "failed", "checking", "off", "not_added", "unavailable")


def _overall(clients) -> str:
    statuses = [state["status"] for state in clients.values() if state["status"] != "not_available"]
    for status in _RANK:
        if status in statuses:
            return status
    return statuses[0] if statuses else "not_available"


def view(runtime, *, refresh=False, environ=None, probe=None, codex=None) -> dict:
    """Every connector with its status for Claude and for Codex, for the owner's dashboard."""
    from bazarlyk.agent_trust import clients
    codex = codex or codex_connectors.CODEX
    state = load(runtime.paths.runtime)
    platform = host_platform()
    environment = _probe_environment(environ)
    claude = (probe or PROBE).view(environment, refresh=refresh,
                                   cache=Path(runtime.paths.runtime) / "connectors-status.json")
    result = claude["result"]
    listed = codex.servers(environment, fresh=refresh)
    items = []
    for entry in CATALOG:
        item = {key: value for key, value in entry.items() if key != "claude_name"}
        if entry["kind"] == "install":
            shared = _browser_status()
        elif entry["id"] == "chrome" and platform == "wsl" and entry["id"] not in state["enabled"]:
            # Claude in Chrome reaches a Chrome on the same system; WSL's Linux side cannot reach Windows' Chrome.
            shared = {"status": "unsupported"}
        elif entry["id"] == "chrome":
            # On is not enough: Claude in Chrome also needs its extension in one of the owner's Chrome profiles.
            extension = chrome_extension_installed(environment.get("HOME"))
            shared = {"extension": extension, "status": ("off" if entry["id"] not in state["enabled"]
                                                         else "connected" if extension else "needs_extension")}
        elif entry["kind"] == "local":
            shared = {"status": "connected" if entry["id"] in state["enabled"] else "off"}
        elif entry["kind"] == "extension":
            shared = _whatsapp_status(runtime, platform)
        else:
            shared = None
        if shared is not None:
            item.update(shared)
            item["clients"] = {"claude": {"status": shared["status"]},
                               "codex": {"status": shared["status"]} if "codex" in entry["clients"]
                               else {"status": "not_available"}}
        else:
            item["clients"] = {"claude": _claude_state(entry, result),
                               "codex": _codex_state(entry, listed, state["codex_added"], codex)}
            item["status"] = _overall(item["clients"])
            if item["clients"]["claude"].get("reason"):
                item["reason"] = item["clients"]["claude"]["reason"]
        items.append(item)
    # Connectors on the owner's claude.ai account that the list above does not name: Claude agents have them too.
    extras = sorted(({"name": name, "status": status} for name, status in (result or {}).get("connectors", {}).items()
                     if name not in CATALOG_NAMES), key=lambda extra: extra["name"].lower()) if result and result.get(
        "available") else []
    return {"connectors": items, "account_connectors": extras, "checking": claude["checking"],
            "clients": clients(environ), "platform": platform, "claude_account": (result or {}).get("account"),
            "codex": {"available": listed["available"], "reason": listed.get("reason")}}


def _codex_change(runtime, connector_id, action, environ, codex):
    """Connect (add if needed, then sign in) or disconnect a service for Codex. Returns the sign-in to show, if any."""
    url = CODEX_URLS.get(connector_id)
    if url is None or action not in {"connect", "disconnect"}:
        raise DomainError("validation_error", "This connector is not available for Codex")
    environment = _probe_environment(environ)
    with _CHANGES:
        state = load(runtime.paths.runtime)
        listed = codex.servers(environment, fresh=True)
        if not listed["available"]:
            raise DomainError("prerequisite_missing", "Codex is not installed or not working. Run codex once in Terminal.")
        current = codex_connectors.state_for(url, listed["servers"])
        added = dict(state["codex_added"])
        if action == "disconnect":
            if current["status"] == "not_added":
                added.pop(connector_id, None)
            elif added.get(connector_id) != current["name"]:
                raise DomainError("validation_error", "This one was set up in Codex outside Qorqbot. Remove it in Codex.")
            else:
                codex.remove(environment, current["name"])
                added.pop(connector_id)
            _save(runtime.paths.runtime, {**state, "codex_added": added})
            return None
        if current["status"] == "off":
            raise DomainError("validation_error", "This connector is turned off in your Codex settings. Turn it on there.")
        if current["status"] == "not_added":
            taken = {server["name"] for server in listed["servers"]}
            name = next(candidate for candidate in (connector_id.replace("_", "-"), connector_id.replace("_", "-") + "-qorqbot",
                                                    "qorqbot-" + connector_id.replace("_", "-")) if candidate not in taken)
            codex.add(environment, name, url)
            added[connector_id] = name
            _save(runtime.paths.runtime, {**state, "codex_added": added})
            current = {"status": "needs_sign_in", "name": name}
        if current["status"] == "connected":
            return None
    return codex.start_sign_in(environment, connector_id, current["name"])


def owner_view(runtime, change=None, environ=None, codex=None) -> dict:
    """GET (``change`` None), ``{"refresh": true}``, ``{"id": "playwright", "enabled": true}`` or
    ``{"id": "notion", "client": "codex", "action": "connect" | "disconnect"}``."""
    if change is not None:
        if set(change) == {"refresh"} and change["refresh"] is True:
            return view(runtime, refresh=True, environ=environ, codex=codex)
        if set(change) == {"id", "client", "action"} and change["client"] == "codex":
            sign_in = _codex_change(runtime, change["id"], change["action"], environ, codex or codex_connectors.CODEX)
            result = view(runtime, environ=environ, codex=codex)
            result["sign_in"] = sign_in
            return result
        if set(change) != {"id", "enabled"}:
            raise DomainError("validation_error", "Send a connector id and whether it is enabled")
        set_enabled(runtime.paths.runtime, change["id"], change["enabled"], environ)
    return view(runtime, environ=environ, codex=codex)
