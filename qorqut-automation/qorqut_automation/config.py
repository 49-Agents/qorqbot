"""Validated extension configuration (installation ``extensions[].config``; every key is optional).

``python``               absolute interpreter that runs staged scripts (default: the host's own interpreter)
``state_dir``            absolute directory for immutable run snapshots (default: ``<instance state>/automation``)
``invocation_seconds``   wall-clock bound of one script invocation, waits excluded (default 60, at most 3600); longer work
                         continues explicitly with saved JSON progress (``run.continue_run``)
``max_run_seconds``      total active bound of one run across its invocations, waits excluded (default 3600, at most 14400)
``max_calls``            SDK calls one run may make (default 2000)
``max_step_bytes``       encoded bytes of one step request or result kept durably (default 262144)
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys

from qorqut_operations.contracts import DomainError

KEYS = frozenset({"python", "state_dir", "invocation_seconds", "max_run_seconds", "max_calls", "max_step_bytes"})


@dataclass(frozen=True)
class Config:
    python: str = ""
    state_dir: str | None = None
    invocation_seconds: int = 60
    max_run_seconds: int = 3600
    max_calls: int = 2000
    max_step_bytes: int = 262144


def _bad(message):
    return DomainError("invalid_installation", message)


def from_config(config) -> Config:
    config = dict(config or {})
    if set(config) - KEYS:
        raise _bad("Unknown automation extension setting: " + ", ".join(sorted(set(config) - KEYS)))
    python = config.get("python", sys.executable)
    if not isinstance(python, str) or not os.path.isabs(python):
        raise _bad("The automation interpreter must be an absolute path")
    state_dir = config.get("state_dir")
    if state_dir is not None and (not isinstance(state_dir, str) or not os.path.isabs(state_dir)):
        raise _bad("The automation state directory must be an absolute path")
    values = {}
    for name, low, high, default in (("invocation_seconds", 5, 3600, 60), ("max_run_seconds", 60, 14400, 3600),
                                      ("max_calls", 1, 20000, 2000), ("max_step_bytes", 1024, 4 * 1024 * 1024, 262144)):
        value = config.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise _bad(f"{name} must be an integer from {low} to {high}")
        values[name] = value
    if values["invocation_seconds"] > values["max_run_seconds"]:
        raise _bad("invocation_seconds cannot exceed max_run_seconds")
    return Config(python, state_dir, **values)
