"""One bounded JSON request on stdin, one JSON envelope on stdout.

Runtime configuration is trusted launch configuration, never request data.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from .errors import AgenticError, InvalidRequestError
from .layer import AgenticLayer
from .runtime import LocalProcessRuntime

MAX_INPUT_BYTES = 96 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
OPERATIONS = {
    **{f"catalog.{name}": ("catalog", name) for name in ("list", "read_entry", "read_resource", "validate")},
    **{f"authoring.{name}": ("authoring", name) for name in
       ("apply", "import_package", "update_entry", "remove_entry", "remove_package")},
    **{f"history.{name}": ("history", name) for name in ("status", "diff", "log", "commit", "restore")},
    **{f"changes.{name}": ("changes", name) for name in
       ("list", "get", "cursor", "advance_cursor", "scan", "cursor_head")},
    **{f"tools.{name}": ("tools", name) for name in ("invoke", "test", "receipt", "reconcile")},
    **{f"workflows.{name}": ("workflows", name) for name in
       ("start", "get", "list", "supply_arguments", "start_step", "complete_step", "pause", "resume", "cancel")},
    **{f"overseer.outside.{name}": ("overseer.outside", name) for name in ("record", "get", "list")},
    **{f"overseer.proposals.{name}": ("overseer.proposals", name) for name in ("record", "get", "list")},
    **{f"overseer.deltas.{name}": ("overseer.deltas", name) for name in ("create", "revise", "get", "list")},
    **{f"overseer.{group}.{name}": (f"overseer.{group}", name)
       for group in ("coverage", "materials") for name in ("record", "get")},
    **{f"overseer.inputs.{name}": ("overseer.inputs", name) for name in ("bind", "get")},
    **{f"overseer.review.{name}": ("overseer.review", name) for name in ("save_batch", "get_batch")},
    **{f"overseer.applications.{name}": ("overseer.applications", name) for name in ("get", "list", "save_plan")},
    "lifecycle.recovery_status": (None, "recovery_status"),
    "lifecycle.recover": (None, "recover"),
}
LIFECYCLE_OPERATIONS = {"lifecycle.initialize", "lifecycle.open", "lifecycle.migrate_state"}


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InvalidRequestError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _constant(value):
    raise InvalidRequestError(f"non-finite JSON number: {value}")


def parse_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InvalidRequestError(f"invalid JSON input: {exc}") from exc


def request_parts(request: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(request, dict) or set(request) != {"operation", "arguments"}:
        raise InvalidRequestError("request must contain exactly operation and arguments")
    operation, arguments = request["operation"], request["arguments"]
    if not isinstance(operation, str) or operation not in (OPERATIONS.keys() | LIFECYCLE_OPERATIONS):
        raise InvalidRequestError("unknown operation", details={"operation": operation})
    if not isinstance(arguments, dict):
        raise InvalidRequestError("arguments must be an object")
    return operation, arguments


def _matches(value, annotation) -> bool:
    if annotation in (Any, inspect.Parameter.empty):
        return True
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        return any(_matches(value, part) for part in args)
    if annotation is type(None):
        return value is None
    if annotation is bool:
        return type(value) is bool
    if annotation is int:
        return type(value) is int
    if annotation is float:
        return type(value) in (float, int)
    if origin in (dict, Mapping):
        return isinstance(value, dict) and (not args or all(
            _matches(k, args[0]) and _matches(v, args[1]) for k, v in value.items()))
    if origin in (list, tuple, set, frozenset):
        return isinstance(value, list) and (not args or all(_matches(v, args[0]) for v in value))
    return isinstance(value, annotation) if isinstance(annotation, type) else True


def _call(method, arguments):
    try:
        inspect.signature(method).bind(**arguments)
    except TypeError as exc:
        raise InvalidRequestError(str(exc)) from exc
    annotations = get_type_hints(method.__init__ if isinstance(method, type) else method)
    for name, value in arguments.items():
        if not _matches(value, annotations.get(name, Any)):
            raise InvalidRequestError(f"invalid type for argument {name}", details={"field": name})
    return method(**arguments)


def dispatch(layer: AgenticLayer, request: Any) -> Any:
    """Dispatch only the documented public operations against an injected layer."""
    operation, arguments = request_parts(request)
    if operation in LIFECYCLE_OPERATIONS:
        raise InvalidRequestError("lifecycle initialize/open require explicit CLI paths")
    component, name = OPERATIONS[operation]
    target = layer
    if component:
        for part in component.split("."):
            target = getattr(target, part)
    return _call(getattr(target, name), arguments)


def load_runtimes(path: str | None) -> dict[str, LocalProcessRuntime]:
    if path is None:
        return {}
    if not os.path.isabs(path):
        raise InvalidRequestError("--runtime-config must be an absolute path")
    with open(path, "rb") as handle:
        data = handle.read(65537)
    if len(data) > 65536:
        raise InvalidRequestError("runtime configuration exceeds 65536 bytes")
    config = parse_json(data)
    if not isinstance(config, dict) or set(config) != {"local_process"}:
        raise InvalidRequestError("runtime config must contain exactly local_process")
    options = config["local_process"]
    if not isinstance(options, dict):
        raise InvalidRequestError("local_process must be an object")
    runtime = _call(LocalProcessRuntime, options)
    return {alias: runtime for alias in runtime.interpreters}


def execute(request: Any, *, root: str, state: str, git_executable: str | None = None,
            runtimes: Mapping[str, Any] | None = None) -> Any:
    operation, arguments = request_parts(request)
    options = {"git_executable": git_executable, "runtimes": runtimes}
    if operation == "lifecycle.migrate_state":
        if arguments:
            raise InvalidRequestError("migration takes no arguments")
        return AgenticLayer.migrate_state(root, state)
    if operation in LIFECYCLE_OPERATIONS:
        allowed = {"git_user"} if operation == "lifecycle.initialize" else {"rebind_root"}
        if set(arguments) - allowed:
            raise InvalidRequestError("unknown lifecycle arguments", details={"fields": sorted(set(arguments) - allowed)})
        factory = AgenticLayer.initialize if operation == "lifecycle.initialize" else AgenticLayer.open
        layer = _call(factory, {"root": root, "state": state, **options, **arguments})
        return {"root": str(layer.root), "state": str(layer.state), "recovery_required": layer.recovery_required}
    layer = AgenticLayer.open(root, state, **options)
    return dispatch(layer, request)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InvalidRequestError(message)


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--root", required=True, help="explicit authoring Git root")
    parser.add_argument("--state", required=True, help="explicit runtime state directory outside root")
    parser.add_argument("--git-executable", help="absolute Git executable")
    parser.add_argument("--runtime-config", help="absolute trusted JSON configuration; opt-in local execution")
    try:
        options = parser.parse_args(argv)
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise AgenticError("JSON input exceeds the byte limit", code="limit_exceeded",
                               details={"limit": MAX_INPUT_BYTES})
        request = parse_json(data)
        request_parts(request)
        result = execute(request, root=options.root, state=options.state,
                         git_executable=options.git_executable, runtimes=load_runtimes(options.runtime_config))
        envelope, exit_code = {"ok": True, "result": result}, 0
    except AgenticError as exc:
        envelope, exit_code = {"ok": False, "error": exc.to_dict()}, 1
    except Exception as exc:
        envelope, exit_code = {"ok": False, "error": {"code": "internal_error", "message": str(exc)[:2000],
                                                     "details": {"exception": type(exc).__name__}}}, 1
    try:
        output = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        output, exit_code = b'{"ok":false,"error":{"code":"internal_error","message":"result is not JSON","details":{}}}', 1
    if len(output) > MAX_OUTPUT_BYTES:
        if envelope["ok"]:
            error = {"code": "limit_exceeded",
                     "message": "JSON result exceeds the byte limit; use narrower reads",
                     "details": {"limit": MAX_OUTPUT_BYTES, "operation_completed": True}}
        else:
            original = envelope["error"]
            details = {key: value for key, value in original["details"].items()
                       if key in ("operation_completed", "attempt_recorded") and isinstance(value, bool)}
            error = {"code": original["code"], "message": original["message"][:2000],
                     "details": {**details, "truncated": True, "limit": MAX_OUTPUT_BYTES}}
        output = json.dumps({"ok": False, "error": error}, ensure_ascii=False).encode("utf-8")
        exit_code = 1
    sys.stdout.buffer.write(output + b"\n")
    return exit_code
