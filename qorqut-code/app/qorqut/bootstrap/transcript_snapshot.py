"""The prepared CLI session's hook: publish which native conversation, transcript and process this agent now runs.

The host's native backend captures the transcript into the append-only native archive from the exact source the hook names;
this hook copies nothing. Capture needs the registration's consent (``capture_transcripts``) and a session whose
identity the hook verified."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")
EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"})


def run(hook: dict, registration_path: Path, *, diagnostics=sys.stderr, runtime="claude-code") -> int:
    try:
        from .terminal_chat import publish
        publish(hook, registration_path, runtime=runtime)
    except Exception:
        # Publishing the session can never block a turn; diagnostics never include private paths or content.
        try:
            diagnostics.write("Qorqut session publication skipped.\n")
        except (OSError, ValueError):
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepared CLI session publication")
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
        sys.stderr.write("Qorqut session publication skipped: invalid hook input.\n")
        return 0
    return run(hook, args.registration)


if __name__ == "__main__":
    raise SystemExit(main())
