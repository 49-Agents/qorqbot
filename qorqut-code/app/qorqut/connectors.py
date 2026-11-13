"""Extensions the owner sets up for their agents: what each one is and whether it is ready.

Each entry is a Qorqbot extension (treg, WhatsApp), set up through that extension. Its tools reach the agents through
Qorqbot, so they work for every agent client. The core never contacts these services and keeps no copy of their data; an
extension makes its own requests (treg's are described in ``qorqut_treg``).

Connectors of the owner's Claude account are not listed or set up here. An agent running in Claude Code still receives
whatever that account has connected, and ``qorqut.connector_gate`` keeps such a call from changing anything before the
owner approves it.
"""
import json
import os
from pathlib import Path
import shutil

# Standard locations a GUI-launched service may be missing from PATH; Node itself must be findable by npx's shebang.
SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
CATALOG = (
    {"id": "treg", "kind": "extension", "clients": ["claude-code", "codex"]},
    {"id": "whatsapp", "kind": "extension", "clients": ["claude-code", "codex"]},
)


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


def _treg_status(runtime) -> dict:
    """Whether the treg extension is part of this install and the owner has saved a key. No key is read here."""
    selected = getattr(getattr(runtime, "selection", None), "extensions", ())
    extension = next((item for item in selected if item.id == "treg"), None)
    return {"status": "not_installed" if extension is None else extension.status()}


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


def view(runtime, *, environ=None) -> dict:
    """Every extension with its status, for the owner's dashboard. Reads local files only."""
    from qorqut.agent_trust import clients
    platform = host_platform()
    items = []
    for entry in CATALOG:
        item = dict(entry)
        item.update(_treg_status(runtime) if entry["id"] == "treg" else _whatsapp_status(runtime, platform))
        items.append(item)
    return {"connectors": items, "clients": clients(environ), "platform": platform}
