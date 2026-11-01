"""Best-effort copies of one prepared CLI session; enabled during preparation by default."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

from .client import load_registration, path_has_symlink

MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")
EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"})


def _validate_records(content: bytes, session_id: str, workspace: Path) -> None:
    bound_message = False
    for line in content.decode("utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            raise ValueError("Not a Claude transcript record")
        if "sessionId" in record and record["sessionId"] != session_id:
            raise ValueError("Transcript contains a different session")
        if record["type"] not in {"user", "assistant"}:
            continue
        message = record.get("message")
        if record.get("sessionId") != session_id or record.get("isSidechain") is True or not isinstance(message, dict):
            raise ValueError("Not an own-session Claude message")
        body = message.get("content")
        if not isinstance(body, (str, list)) or isinstance(body, list) and any(not isinstance(block, dict) for block in body):
            raise ValueError("Invalid Claude message content")
        if "cwd" in record:
            if record["cwd"] != str(workspace):
                raise ValueError("Transcript belongs to a different workspace")
            bound_message = True
    if not bound_message:
        raise ValueError("Transcript has no prepared-workspace message")


def validate_codex_records(content: bytes, session_id: str, workspace: Path) -> None:
    """Bind a Codex rollout to its own session metadata before accepting messages."""
    metadata = False
    message = False
    for line in content.decode("utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            raise ValueError("Invalid Codex rollout record")
        payload = record.get("payload")
        if record["type"] == "session_meta":
            if metadata or not isinstance(payload, dict) or payload.get("id") != session_id or payload.get("cwd") != str(workspace):
                raise ValueError("Codex metadata does not match the registered session")
            metadata = True
        elif record["type"] == "turn_context":
            if not metadata or not isinstance(payload, dict) or payload.get("cwd") != str(workspace):
                raise ValueError("Codex turn belongs to another workspace")
        elif record["type"] == "response_item" and isinstance(payload, dict) and payload.get("type") == "message":
            if not metadata:
                raise ValueError("Codex message precedes session metadata")
            if payload.get("role") in {"user", "assistant"}:
                blocks = payload.get("content")
                if not isinstance(blocks, list) or not blocks or any(not isinstance(block, dict) for block in blocks):
                    raise ValueError("Invalid Codex message content")
                message = True
    if not metadata or not message:
        raise ValueError("Codex rollout lacks a bound conversation")


def snapshot(hook: dict, registration_path: Path, *, runtime="claude-code") -> bool:
    registration = load_registration(registration_path)
    # Consent is checked before inspecting or opening any caller-supplied path.
    if registration.get("capture_transcripts") is not True:
        return False
    if hook.get("hook_event_name") not in EVENTS:
        raise ValueError("Unsupported transcript hook")
    workspace = Path(registration["workspace"])
    session_id = hook.get("session_id")
    if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid transcript session identifier")
    if hook.get("cwd") != str(workspace):
        raise ValueError("Hook workspace does not match its registration")
    supplied = hook.get("transcript_path")
    if not isinstance(supplied, str):
        raise ValueError("Missing transcript path")
    source = Path(supplied)
    if runtime not in {"claude-code", "codex"}:
        raise ValueError("Unsupported transcript runtime")
    filename_matches = (source.name == session_id + ".jsonl" if runtime == "claude-code" else
        bool(re.fullmatch(r"rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-" + re.escape(session_id) + r"\.jsonl", source.name)))
    if not source.is_absolute() or ".." in source.parts or not filename_matches:
        raise ValueError("Transcript filename does not match its session")
    destination = workspace / "sessions" / (("codex-" if runtime == "codex" else "") + session_id + ".jsonl")
    if path_has_symlink(source) or path_has_symlink(destination):
        raise ValueError("Transcript snapshots cannot follow symlinks")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_TRANSCRIPT_BYTES:
            raise ValueError("Transcript must be a bounded regular file")
        content = stream.read(MAX_TRANSCRIPT_BYTES + 1)
    if len(content) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("Transcript exceeds its size limit")
    (validate_codex_records if runtime == "codex" else _validate_records)(content, session_id, workspace)
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    if path_has_symlink(destination):
        raise ValueError("Transcript destination changed")
    destination.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def run(hook: dict, registration_path: Path, *, diagnostics=sys.stderr, runtime="claude-code") -> int:
    try:
        from .terminal_chat import publish
        publish(hook, registration_path, runtime=runtime)
    except Exception:
        pass  # Conversation display must never block the native agent.
    try:
        snapshot(hook, registration_path, runtime=runtime)
    except Exception:
        # Capture cannot block a turn; diagnostics never include private paths or content.
        try:
            diagnostics.write("Bazarlyk transcript snapshot skipped.\n")
        except (OSError, ValueError):
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepared Claude session snapshot")
    parser.add_argument("--registration", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(262_145)
        if len(raw) > 262_144:
            raise ValueError("Hook input exceeds its limit")
        hook = json.loads(raw)
        if not isinstance(hook, dict):
            raise ValueError("Hook input must be an object")
    except (ValueError, RecursionError):
        sys.stderr.write("Bazarlyk transcript snapshot skipped: invalid hook input.\n")
        return 0
    return run(hook, args.registration)


if __name__ == "__main__":
    raise SystemExit(main())
