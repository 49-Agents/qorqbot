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

TIMEOUT = 1.0
RESPONSE_LIMIT = 16 * 1024


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
        deadline = time.monotonic() + 2 * TIMEOUT
        with httpx.Client(timeout=TIMEOUT, trust_env=False, follow_redirects=False) as client:
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
        if event in {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
                     "PermissionRequest", "Stop", "Interrupt", "SessionEnd"}:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": result.message}}))
    return 0  # Observation failure is not a native blocking/kill instruction.


if __name__ == "__main__":
    raise SystemExit(main())
