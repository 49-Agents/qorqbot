"""Publish the exact prepared native session for the owner's terminal conversation view.

No transcript discovery, model calls or network requests. This runs inside existing
hooks and records only their explicit session/path and native process identity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from .client import load_registration, path_has_symlink, write_json_private
from .transcript_snapshot import EVENTS, SESSION_ID

CODEX_SUFFIX = r"(?:_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})?"


def process_fields(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()


def native_process(runtime):
    pid = os.getppid()
    for _ in range(20):
        proc = Path(f"/proc/{pid}")
        fields = process_fields(pid)
        name = (proc / "comm").read_text().strip()
        if name in ({"claude", "claude.exe"} if runtime == "claude-code" else {"codex"}):
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
    path = workspace / ".bazarlyk/terminal-chat.json"
    # A delayed Stop/SessionEnd from an older session must not replace a newer one.
    if event in {"Stop", "SessionEnd"} and path.exists():
        old = json.loads(path.read_text())
        if old.get("session_id") != session:
            return False
    write_json_private(path, {"agent_id": registration["agent_id"], "workspace": str(workspace),
        "session_id": session, "runtime": runtime, "transcript_path": str(source),
        "event": event, **process})
    return True
