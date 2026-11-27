"""Extensions the owner sets up for their agents: what each one is and whether it is ready.

Each entry is a Qorqbot extension (OpenRouter, treg, WhatsApp), set up through that extension. Extensions that follow
the contract follow them, one entry each of kind ``contract`` (``qorqut.extension_connections``). Its tools reach the agents through
Qorqbot, so they work for every agent client. The core never contacts these services and keeps no copy of their data; an
extension makes its own requests (treg's are described in ``qorqut_treg``, OpenRouter's in ``qorqut_openrouter``).

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
    {"id": "openrouter", "kind": "extension", "clients": ["claude-code", "codex"]},
    {"id": "treg", "kind": "extension", "clients": ["claude-code", "codex"]},
    {"id": "whatsapp", "kind": "extension", "clients": ["claude-code", "codex"]},
)
HAND_WRITTEN = frozenset(entry["id"] for entry in CATALOG)


def _probe_environment(environ=None):
    """The environment the owner's own command line gets: the invoking user's own HOME and CLI
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


# Extensions the owner turns on by saving a key; each reports its own ``status()``.
KEYED = frozenset({"openrouter", "treg"})


def _keyed_status(runtime, identity) -> dict:
    """Whether a keyed extension is part of this install and the owner has saved its key. No key is read here."""
    selected = getattr(getattr(runtime, "selection", None), "extensions", ())
    extension = next((item for item in selected if item.id == identity), None)
    return {"status": "not_installed" if extension is None else extension.status()}


def _whatsapp_status(runtime) -> dict:
    """The WhatsApp extension's own setup state (``qorqut_whatsapp.setup``): unsupported, not_set_up, linking, finishing,
    connected or failed, with its note; ``not_installed`` when the extension is not selected. Reads local files only."""
    selected = getattr(getattr(runtime, "selection", None), "extensions", ())
    extension = next((item for item in selected if item.id == "whatsapp"), None)
    setup = getattr(extension, "setup", None)
    if setup is None:
        return {"status": "not_installed"}
    try:
        view = setup.status()
    except Exception:  # noqa: BLE001 - one unreadable card never hides the others
        return {"status": "failed", "note": "WhatsApp's setup could not be read right now."}
    return {"status": view["state"], "note": view["note"], "send_ready": view["send_ready"]}


def host_platform(proc_version="/proc/version") -> str:
    """"darwin", "wsl" (Windows through WSL2) or "linux"."""
    if os.uname().sysname == "Darwin":
        return "darwin"
    try:
        with open(proc_version) as source:
            return "wsl" if "microsoft" in source.read().lower() else "linux"
    except OSError:
        return "linux"


def _contract_items(runtime) -> tuple[list, set]:
    """The card entries of the extensions that follow the contract (``qorqut.extension_connections``), from the status
    the registry holds, and the ids of those whose folder is fine; no set-up handler is asked. Empty while none is
    found."""
    registry = getattr(runtime, "extension_registry", None)
    if registry is None or not registry.found:
        return [], set()
    from qorqut import extension_connections
    with runtime.database.read() as uow:
        entries = registry.list(connection=uow.connection)
        existing = extension_connections.agents(uow.connection)
    fine = {entry.id for entry in entries if entry.manifest is not None and not entry.refused_findings}
    return [extension_connections.item(entry, existing) for entry in entries], fine


def view(runtime, *, environ=None) -> dict:
    """Every extension with its status, for the owner's dashboard. Reads local files only."""
    platform = host_platform()
    contract, fine = _contract_items(runtime)
    # Only a fine folder replaces a hand-written entry; a broken one of the same name leaves today's card as it is.
    converted = {item["id"] for item in contract if item["id"] in fine}
    contract = [item for item in contract if item["id"] in converted or item["id"] not in HAND_WRITTEN]
    items = []
    for entry in CATALOG:
        if entry["id"] in converted:     # a converted extension is shown once, by its contract entry
            continue
        item = dict(entry)
        item.update(_keyed_status(runtime, entry["id"]) if entry["id"] in KEYED else _whatsapp_status(runtime))
        items.append(item)
    return {"connectors": items + contract, "platform": platform}
