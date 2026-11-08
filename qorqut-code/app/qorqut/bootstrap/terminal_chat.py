"""Publish the exact prepared native session for the owner's terminal conversation view.

No transcript discovery, model calls or network requests. This runs inside existing
hooks and records only their explicit session/path and native process identity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

from .client import load_registration, path_has_symlink, write_json_private
from .transcript_snapshot import EVENTS, SESSION_ID

CODEX_SUFFIX = r"(?:_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})?"
NATIVE_IDENTITY = ("agent_id", "workspace", "session_id", "runtime", "pid", "started")


def process_fields(pid):
    if sys.platform == 'darwin':
        from qorqut.process_info import fields
        return fields(pid)
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


def native_process(runtime):
    pid = os.getppid()
    for _ in range(20):
        proc = Path(f"/proc/{pid}")
        fields = process_fields(pid)
        if sys.platform == 'darwin':
            from qorqut import process_info
            name = process_info.name(pid)
        else:
            name = (proc / "comm").read_text().strip()
        if name in ({"claude", "claude.exe"} if runtime == "claude-code" else {"codex"}):
            if sys.platform == 'darwin' and process_info.start_token(pid) != fields[19]:
                raise ProcessLookupError(pid)
            return {"pid": pid, "started": fields[19]}
        pid = int(fields[1])
        if pid <= 1:
            break
    return None


def publish(hook, registration_path, *, runtime="claude-code"):
    registration = load_registration(registration_path)
    if registration.get("capture_transcripts") is not True:
        return False
    session, event = hook.get("session_id"), hook.get("hook_event_name")
    if event not in EVENTS or not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        return False
    workspace = Path(registration["workspace"])
    raw = hook.get("transcript_path")
    if not isinstance(raw, str) or runtime not in {"claude-code", "codex"}:
        return False
    source = Path(raw)
    # Codex names a rollout after its exact session and may append the native ``_<UUID>`` suffix; nothing else is accepted.
    matches = (source.name == session + ".jsonl" if runtime == "claude-code" else
               re.fullmatch(r"rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-" + re.escape(session) + CODEX_SUFFIX + r"\.jsonl", source.name))
    if not source.is_absolute() or not matches or ".." in source.parts or path_has_symlink(source):
        return False
    process = native_process(runtime)
    if not process:
        return False
    value = {"agent_id": registration["agent_id"], "workspace": str(workspace),
        "session_id": session, "runtime": runtime, "transcript_path": str(source),
        "event": event, **process}
    path = workspace / ".qorqut/terminal-chat.json"
    # Completion can be the first successful publication after pending bootstrap.
    # Only the exact current attested binding may replace another session/process;
    # delayed completion from an old generation must not overwrite a newer one.
    if event in {"Stop", "SessionEnd"} and path.exists():
        old = json.loads(path.read_text())
        if any(old.get(key) != value[key] for key in NATIVE_IDENTITY):
            binding_path = workspace / ".qorqut/native-binding.json"
            if path_has_symlink(binding_path) or not binding_path.is_file():
                return False
            binding = json.loads(binding_path.read_text())
            if (binding.get("registration_id") != registration["registration_id"]
                    or any(binding.get(key) != value[key] for key in NATIVE_IDENTITY)):
                return False
    write_json_private(path, value)
    return True
