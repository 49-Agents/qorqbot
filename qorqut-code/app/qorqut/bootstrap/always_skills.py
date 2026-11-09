"""Always-loaded skills for the configured Ordubashy (stdlib only).

The startup composer calls this for the configured Ordubashy before adding
context. It returns current skill text on lifecycle events or a changed digest,
and one short operational intake reminder for each UserPromptSubmit. Existing
skill receipts do not suppress that reminder. No separate prompt hook is installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


def agentic_policy(registration_path):
    import importlib.util
    path = Path(registration_path).parent / 'agentic-instructions.py'
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Agentic hook helper cannot be a symlink')
    spec = importlib.util.spec_from_file_location('prepared_agentic_instructions', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _no_symlink(path: Path) -> bool:
    return not (path.is_symlink() or any(parent.is_symlink() for parent in path.parents))


def render(workspace: Path, runtime: str, limit=None) -> str:
    registration_path = workspace / '.qorqut/registration.json'
    policy = agentic_policy(registration_path)
    return policy.render_skills(policy.from_prepared(registration_path), workspace, runtime,
                                limit=policy.SKILL_BUDGET if limit is None else limit)


def _receipt_path(workspace: Path, session_id: str) -> Path:
    return workspace / ".qorqut/delivery" / ("skills-" + hashlib.sha256(session_id.encode()).hexdigest() + ".json")


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
    if event not in {"SessionStart", "UserPromptSubmit"} or runtime not in {"claude-code", "codex"}:
        return {}
    session_id = hook.get("session_id")
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 200:
        return {}
    if not _no_symlink(registration_path):
        return {}
    registration = json.loads(registration_path.read_text())
    workspace = Path(registration["workspace"])
    context = render(workspace, runtime)
    policy = agentic_policy(registration_path)
    reminder = policy.from_prepared(registration_path).render('operational-intake') if event == 'UserPromptSubmit' else ''
    digest = hashlib.sha256(context.encode()).hexdigest()
    receipt_path = _receipt_path(workspace, session_id)
    if event == "UserPromptSubmit":
        try:
            previous = json.loads(receipt_path.read_text()) if _no_symlink(receipt_path) else {}
        except (OSError, ValueError):
            previous = {}
        if (previous.get("registration_id") == registration.get("registration_id")
                and previous.get("digest") == digest):
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": reminder}}
    try:
        _write_private(receipt_path, {"registration_id": registration.get("registration_id"),
            "session_id": session_id, "runtime": runtime, "digest": digest,
            "event": hook.get("source", "startup") if event == "SessionStart" else "prompt"})
    except OSError:
        pass  # Delivery still proceeds; the next prompt re-injects.
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": '\n\n'.join(filter(None, [reminder, context]))}}


def main() -> int:
    parser = argparse.ArgumentParser(description="Always-loaded skills hook")
    parser.add_argument("--registration", type=Path, required=True)
    parser.add_argument("--runtime", choices=('codex', 'claude-code'), required=True)
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
