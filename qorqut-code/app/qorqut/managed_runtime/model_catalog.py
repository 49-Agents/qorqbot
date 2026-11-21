"""The models each client offers, and the effort levels of each model, as the client itself reports them.

Nothing here is typed in by hand: Claude Code answers its ``initialize`` control request with ``models`` (each with ``value``,
``displayName``, ``description`` and ``supportedEffortLevels``); Codex's app-server answers ``model/list`` with ``data`` (each with
``id``, ``displayName``, ``description``, ``supportedReasoningEfforts``, ``defaultReasoningEffort``, ``isDefault``, ``hidden``).
Both were checked against the CLIs (Claude Code 2.1.329, Codex 0.167.1). Neither question sends a message or uses the model.

``catalog(client)`` returns ``{"client", "models": [{"id", "label", "description", "efforts", "default_effort", "default",
"older"}]}`` or None when the client could not be asked. ``older`` marks Claude Code's pinned versions (full ``claude-*`` ids),
which the picker folds away behind its aliases. Answers are kept for ``TTL_SECONDS``; a failure is retried after
``RETRY_SECONDS``. One question per client runs at a time.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import tempfile
import threading
import time

CLAUDE, CODEX = "claude", "codex"
TTL_SECONDS = 30 * 60
RETRY_SECONDS = 60
ASK_SECONDS = 25
LABEL_MAX, DESCRIPTION_MAX, ID_MAX = 60, 160, 100
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")

_lock = threading.Lock()
_client_locks = {CLAUDE: threading.Lock(), CODEX: threading.Lock()}
_cache = {}                                  # client -> (time, catalog or None)


def _text(value, limit):
    return " ".join(str(value).split())[:limit] if isinstance(value, str) else ""


def _efforts(values):
    return [value for value in values if value in EFFORTS] if isinstance(values, list) else []


def claude_models(models) -> list:
    """Claude Code's ``initialize`` ``models`` in the catalog shape."""
    shaped = []
    for item in models if isinstance(models, list) else []:
        if not isinstance(item, dict) or not _text(item.get("value"), ID_MAX):
            continue
        value = _text(item["value"], ID_MAX)
        shaped.append({"id": value, "label": _text(item.get("displayName"), LABEL_MAX) or value,
                       "description": _text(item.get("description"), DESCRIPTION_MAX),
                       "efforts": _efforts(item.get("supportedEffortLevels")) if item.get("supportsEffort") else [],
                       "default_effort": None, "default": value == "default", "older": value.startswith("claude-")})
    return shaped


def codex_models(data) -> list:
    """Codex's ``model/list`` ``data`` in the catalog shape (hidden models left out)."""
    shaped = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or item.get("hidden") or not _text(item.get("id"), ID_MAX):
            continue
        efforts = [entry.get("reasoningEffort") for entry in item.get("supportedReasoningEfforts") or [] if isinstance(entry, dict)]
        default_effort = item.get("defaultReasoningEffort")
        shaped.append({"id": _text(item["id"], ID_MAX), "label": _text(item.get("displayName"), LABEL_MAX) or _text(item["id"], ID_MAX),
                       "description": _text(item.get("description"), DESCRIPTION_MAX), "efforts": _efforts(efforts),
                       "default_effort": default_effort if default_effort in EFFORTS else None,
                       "default": item.get("isDefault") is True, "older": False})
    return shaped


class _Lines:
    """Reads JSON lines from a process until a deadline."""

    def __init__(self, process, deadline):
        self.process, self.deadline, self.buffer = process, deadline, b""

    def next(self):
        while True:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    return value
                continue
            left = self.deadline - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([self.process.stdout], [], [], min(left, 1.0))
            if ready:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    return None
                self.buffer += chunk


def _start(command, env, cwd):
    return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=cwd,
                            start_new_session=True)


def _stop(process):
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        os.killpg(process.pid, 15)
    except OSError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
        process.wait(timeout=3)


def _write(process, value):
    process.stdin.write((json.dumps(value) + "\n").encode())
    process.stdin.flush()


def _ask_claude(path, env, cwd):
    process = _start([path, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                      "--no-session-persistence"], env, cwd)
    try:
        _write(process, {"type": "control_request", "request_id": "catalog", "request": {"subtype": "initialize"}})
        lines = _Lines(process, time.monotonic() + ASK_SECONDS)
        while (message := lines.next()) is not None:
            response = message.get("response") if message.get("type") == "control_response" else None
            if isinstance(response, dict) and response.get("request_id") == "catalog":
                body = response.get("response")
                return claude_models(body.get("models")) if response.get("subtype") == "success" and isinstance(body, dict) else None
        return None
    finally:
        _stop(process)


def _ask_codex(path, env, cwd, arguments=()):
    process = _start([path, *arguments, "app-server"], env, cwd)
    try:
        _write(process, {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "qorqbot-models", "title": "Qorqbot",
                                                                                     "version": "0"}}})
        lines = _Lines(process, time.monotonic() + ASK_SECONDS)
        asked = False
        while (message := lines.next()) is not None:
            if message.get("id") == 1 and not asked:
                if "error" in message:
                    return None
                _write(process, {"method": "initialized"})
                _write(process, {"id": 2, "method": "model/list", "params": {}})
                asked = True
            elif message.get("id") == 2 and "method" not in message:
                result = message.get("result")
                return codex_models(result.get("data")) if isinstance(result, dict) else None
        return None
    finally:
        _stop(process)


def _ask(client):
    """The client's own list, asked with the CLI and environment the agents run with; None when it cannot be asked."""
    from qorqut import agent_cli_sign_in as cli
    env = cli.cli_environment(client)
    found = cli.resolve_cli(client, env)
    path = (found or {}).get("path")
    if not path:
        return None
    with tempfile.TemporaryDirectory(prefix="qorqbot-models-") as cwd:
        if client == CLAUDE:
            models = _ask_claude(path, env, cwd)
        else:
            models = _ask_codex(path, env, cwd, cli.launch_arguments(client))
    return {"client": client, "models": models} if models else None


def catalog(client, *, ask=None, now=time.monotonic):
    """The client's models (see the module text), from the cache while it is fresh. None when the client is unknown or could not
    be asked."""
    if client not in (CLAUDE, CODEX):
        return None
    with _client_locks[client]:
        with _lock:
            kept = _cache.get(client)
        if kept is not None and now() - kept[0] < (TTL_SECONDS if kept[1] is not None else RETRY_SECONDS):
            return kept[1]
        try:
            found = (ask or _ask)(client)
        except (OSError, ValueError, subprocess.SubprocessError):
            found = None
        with _lock:
            _cache[client] = (now(), found)
        return found


def forget():
    """Drop every kept answer (a CLI was installed, updated or signed in)."""
    with _lock:
        _cache.clear()
