"""Translate an already process-bound native hook into a runner persistence input."""

from datetime import datetime, timezone
import hashlib
import json

from .capabilities import require_profile
from .types import AdapterError, HookObservation, NativeBinding, NativeSource, Provider

ACTIVITY = {"SessionStart": "idle", "UserPromptSubmit": "working", "PreToolUse": "working",
    "PostToolUse": "working", "PostToolUseFailure": "working", "PermissionRequest": "waiting_user",
    "Stop": "idle", "Interrupt": "idle", "SessionEnd": "unknown"}


def translate_hook(provider: Provider, version: str, binding: NativeBinding, source: NativeSource,
                   payload: dict, *, sequence: int, observed_at: datetime) -> HookObservation:
    """Runner validates process ancestry/fingerprints before this function.

    The payload can corroborate the bound session/path, never select agent/run or
    infer ownership from cwd. This function neither writes nor forwards an event.
    """
    require_profile(provider, version)
    if source.provider != provider or source.binding != binding:
        raise AdapterError("Hook source does not belong to the supplied run/native binding")
    if payload.get("session_id") != str(binding.native_session_id) or payload.get("transcript_path") != str(source.path):
        raise AdapterError("Hook session/path differs from the discovered source")
    if payload.get("agent_id") or payload.get("parent_session_id"):
        raise AdapterError("Child hook cannot claim the primary run; child contract unsupported")
    # Platform identity is never accepted from hook text, even if it happens to match.
    if any(key in payload for key in ("run_id", "recovery_epoch", "environment_generation", "run_generation")):
        raise AdapterError("Native payload cannot supply platform binding fields")
    event = payload.get("hook_event_name")
    supported = set(ACTIVITY) - ({"Interrupt"} if provider == "claude" else {"PostToolUseFailure"})
    if event not in supported:
        raise AdapterError("Unsupported hook event")
    if type(sequence) is not int or sequence < 1 or observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise AdapterError("Supply the runner's sequence and timezone-aware observation time")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return HookObservation(provider=provider, binding=binding, source=source, event=event,
        activity=ACTIVITY[event], sequence=sequence,
        observed_at=observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        payload_sha256=hashlib.sha256(raw).hexdigest())
