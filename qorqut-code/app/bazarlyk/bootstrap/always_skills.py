"""Always-loaded skills for the configured Ordubashy (stdlib only).

Preparation installs this as `.bazarlyk/always-skills.py` and a separate
SessionStart/UserPromptSubmit hook entry only for the configured Ordubashy.
It injects the full text of the agent's own materialized SKILL.md files; the
files remain the single source. A separate hook keeps its own context budget,
so neither these skills nor the shared bootstrap configuration is truncated.
Lifecycle events always inject; prompt events inject only when the rendered
digest differs from this session's receipt. Failures never block the session.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

SKILLS = (
    "bazarlyk-role-owner-focus",
    "bazarlyk-role-chat-references",
    "bazarlyk-role-owner-input",
    "bazarlyk-role-action-recording",
    "bazarlyk-role-owner-reminders",
    "bazarlyk-role-calendar",
    "bazarlyk-plugin-email-management",
    "bazarlyk-role-business-tables",
    "bazarlyk-role-browser-control",
    # Owners ask Ordubashy how Qorqbot works; a skill matched only by its description was missed on the first question.
    "bazarlyk-qorqbot-guide",
)
SKILL_ROOTS = {"codex": ".agents/skills", "claude-code": ".claude/skills"}
# Matches the generated Codex additionalContextLimit for this hook entry.
LIMIT = 120000
HEADER = "# Always-loaded skills (already loaded — follow them; do not re-read)"


def _no_symlink(path: Path) -> bool:
    return not (path.is_symlink() or any(parent.is_symlink() for parent in path.parents))


def render(workspace: Path, runtime: str, limit: int = LIMIT) -> str:
    root = workspace / SKILL_ROOTS[runtime]
    parts, notes = [HEADER], []
    size = len(HEADER)
    for name in SKILLS:
        path = root / name / "SKILL.md"
        try:
            if not _no_symlink(path):
                raise OSError("symlink")
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            notes.append(f"- {name}: not available in this workspace; skipped.")
            continue
        block = f'<skill name="{name}" path="{path}">\n{text}\n</skill>'
        # Reserve room for a short note per skill so the total stays within limit.
        if size + len(block) + 2 > limit - 250 * len(SKILLS):
            # Never truncate a skill: name it so the agent reads it on demand.
            notes.append(f"- {name}: omitted (context budget); read {path} in full before using it.")
            continue
        parts.append(block)
        size += len(block) + 2
    if notes:
        parts.append("Skill notes:\n" + "\n".join(notes))
    return "\n\n".join(parts)


def _receipt_path(workspace: Path, session_id: str) -> Path:
    return workspace / ".bazarlyk/delivery" / ("skills-" + hashlib.sha256(session_id.encode()).hexdigest() + ".json")


def _write_private(path: Path, value: dict) -> None:
    if not _no_symlink(path):
        raise OSError("Receipts cannot follow symlinks")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".skills-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def handle(hook: dict, registration_path: Path, runtime: str) -> dict:
    event = hook.get("hook_event_name")
    if event not in {"SessionStart", "UserPromptSubmit"} or runtime not in SKILL_ROOTS:
        return {}
    session_id = hook.get("session_id")
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 200:
        return {}
    if not _no_symlink(registration_path):
        return {}
    registration = json.loads(registration_path.read_text())
    workspace = Path(registration["workspace"])
    context = render(workspace, runtime)
    digest = hashlib.sha256(context.encode()).hexdigest()
    receipt_path = _receipt_path(workspace, session_id)
    if event == "UserPromptSubmit":
        try:
            previous = json.loads(receipt_path.read_text()) if _no_symlink(receipt_path) else {}
        except (OSError, ValueError):
            previous = {}
        if (previous.get("registration_id") == registration.get("registration_id")
                and previous.get("digest") == digest):
            return {}
    try:
        _write_private(receipt_path, {"registration_id": registration.get("registration_id"),
            "session_id": session_id, "runtime": runtime, "digest": digest,
            "event": hook.get("source", "startup") if event == "SessionStart" else "prompt"})
    except OSError:
        pass  # Delivery still proceeds; the next prompt re-injects.
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Always-loaded skills hook")
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--runtime", choices=tuple(SKILL_ROOTS), required=True)
    args = parser.parse_args()
    try:
        hook = json.loads(sys.stdin.read(4 * 1024 * 1024))
        output = handle(hook, args.registration, args.runtime) if isinstance(hook, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        output = {}
    sys.stdout.write(json.dumps(output, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
