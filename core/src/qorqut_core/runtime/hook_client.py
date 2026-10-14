"""Bounded best-effort native sink: failures report unavailability, never exit a CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..adapters.manual_hooks import binding_relative, read_hook_configuration
from ..runner.process import fingerprint
from ..transports.configuration import json_object
from .native_hooks import MAX_HOOK_BYTES, NativeHookRequest, NativeHookResult

# Finite HTTP budget in seconds: connect/write/pool, read inactivity, and elapsed time for
# the whole response. No retry. Claude's generated native hook timeout (10 s,
# manual_hooks.CLAUDE_HOOK_TIMEOUT) is the real bound: the read-inactivity and elapsed checks
# below run inside the sink and are not a hard real-time scheduler guarantee. Codex keeps its
# 3-second native hook and 1-second HTTP settings.
CONNECT_TIMEOUT = 1.0
CLAUDE_READ_TIMEOUT = 8.0
CLAUDE_RESPONSE_BUDGET = 8.0
CODEX_READ_TIMEOUT = 1.0
CODEX_RESPONSE_BUDGET = 2.0
RESPONSE_LIMIT = 16 * 1024

# Claude reads stdout JSON for these events; SessionEnd output is discarded or rejected.
CLAUDE_WARNING_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "PermissionRequest", "Stop"})
CODEX_CONTEXT_EVENTS = frozenset({"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "PermissionRequest", "Stop", "Interrupt", "SessionEnd"})


def _budget(provider: str):
    read, elapsed = ((CLAUDE_READ_TIMEOUT, CLAUDE_RESPONSE_BUDGET) if provider == "claude"
                     else (CODEX_READ_TIMEOUT, CODEX_RESPONSE_BUDGET))
    return httpx.Timeout(CONNECT_TIMEOUT, read=read), elapsed


def submit(binding_path: Path, provider: str, raw: bytes) -> NativeHookResult:
    try:
        if len(raw) > MAX_HOOK_BYTES:
            raise ValueError("Hook input exceeds limit")
        config = read_hook_configuration(binding_path)
        if (config.program.provider != provider
                or binding_path != config.roots.workspace / binding_relative(provider)):
            raise ValueError("Hook provider binding differs")
        request = NativeHookRequest(provider=provider, agent_id=config.binding.agent_id,
            terminal_id=config.binding.terminal_id, hook_process=fingerprint(os.getpid()), payload=json_object(raw))
        body = request.model_dump_json().encode()
        if len(body) > MAX_HOOK_BYTES:
            raise ValueError("Hook request exceeds limit")
        endpoint = urlsplit(config.core.url)
        url = urlunsplit((endpoint.scheme, endpoint.netloc, "/api/v1/native/hook", "", ""))
        headers = dict(config.core.headers)
        headers["Content-Type"] = "application/json"
        # No environment proxy/credentials, redirect, retry, or exception body.
        timeout, elapsed = _budget(provider)
        deadline = time.monotonic() + elapsed
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as client:
            with client.stream("POST", url, headers=headers, content=body) as response:
                output = bytearray()
                for chunk in response.iter_raw():
                    if time.monotonic() > deadline or len(output) + len(chunk) > RESPONSE_LIMIT:
                        raise ValueError("Hook response is unavailable")
                    output.extend(chunk)
                result = NativeHookResult.model_validate(json_object(bytes(output)))
                expected = 409 if result.outcome == "conflict" else 503 if result.outcome == "unavailable" else 200
                if response.status_code != expected:
                    raise ValueError("Hook response status differs")
                return result
    except Exception:
        return NativeHookResult.result("unavailable")


def _failure_output(provider: str, event, message: str):
    """Native stdout JSON for a failed/conflicting hook, or None for none.

    Only Claude SessionStart gets model-visible additionalContext. Any other Claude event
    gets just the user-facing systemMessage: additionalContext on Stop would start another
    turn, and SessionEnd output is not accepted. Codex output is unchanged.
    """
    if not isinstance(event, str):
        return None
    context = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}
    if provider == "codex":
        return context if event in CODEX_CONTEXT_EVENTS else None
    if event == "SessionStart":
        return {"systemMessage": message, **context}
    return {"systemMessage": message} if event in CLAUDE_WARNING_EVENTS else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Submit a configured native hook observation")
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    args = parser.parse_args(argv)
    event = None
    try:
        raw = sys.stdin.buffer.read(MAX_HOOK_BYTES + 1)
        if len(raw) <= MAX_HOOK_BYTES:
            event = json_object(raw).get("hook_event_name")
        result = submit(args.binding, args.provider, raw)
    except Exception:
        result = NativeHookResult.result("unavailable")
    if result.outcome in {"conflict", "unavailable"}:
        print(result.message, file=sys.stderr)
        output = _failure_output(args.provider, event, result.message)
        if output is not None:
            print(json.dumps(output))
    return 0  # Observation failure is not a native blocking/kill instruction.


if __name__ == "__main__":
    raise SystemExit(main())
