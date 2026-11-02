"""Local JSON command. No store is opened except for an explicit apply."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ImportFailure, apply_import_plan, build_import_plan, inventory_sources


MAX_INPUT_BYTES = 96 * 1024 * 1024


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImportFailure("invalid_request", "Duplicate JSON key", key=key)
        result[key] = value
    return result


def dispatch(request, *, root=None, state=None, instance_root=None):
    if not isinstance(request, dict) or set(request) != {"operation", "arguments"} or not isinstance(request["arguments"], dict):
        raise ImportFailure("invalid_request", "Expected {operation, arguments}")
    op, args = request["operation"], request["arguments"]
    contracts = {
        "inventory_sources": ({"source_roots", "selected_extensions"}, {"custom_sources", "email_guidance_root", "export_root"}),
        "build_import_plan": ({"inventory"}, {"existing_files", "previous_import"}),
        "apply_import_plan": ({"plan", "attribution"}, set()),
        "upgrade_prepare": ({"export_root"}, {"selected_extensions", "acknowledge_grants"}),
        "upgrade_apply": ({"plan", "attribution"}, set()),
    }
    if not isinstance(op, str) or op not in contracts:
        raise ImportFailure("unknown_operation", "Unknown offline import operation")
    required, optional = contracts[op]
    if not required <= set(args) or set(args) - required - optional:
        raise ImportFailure("invalid_arguments", "Missing or unknown operation arguments", operation=op)
    if op == "inventory_sources":
        return inventory_sources(**args)
    if op == "build_import_plan":
        return build_import_plan(**args)
    if op.startswith("upgrade_"):
        if not instance_root:
            raise ImportFailure("invalid_arguments", "The offline upgrade requires the explicit --instance-root")
        from bazarlyk.instance_paths import InstancePaths
        from .upgrade import apply_upgrade, prepare_upgrade, public_plan
        paths = InstancePaths(Path(instance_root), {})
        if op == "upgrade_prepare":
            return public_plan(prepare_upgrade(paths, **args))
        return apply_upgrade(paths, **args)
    if not root or not state:
        raise ImportFailure("invalid_arguments", "apply_import_plan requires explicit --root and --state")
    from qorqut_agentic import AgenticLayer
    return apply_import_plan(AgenticLayer.open(root=root, state=state), **args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    parser.add_argument("--state")
    parser.add_argument("--instance-root")
    options = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ImportFailure("limit_exceeded", "JSON input exceeds 96 MiB")
        request = json.loads(raw, object_pairs_hook=unique_object,
                             parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        response = {"ok": True, "result": dispatch(request, root=options.root, state=options.state,
                                                   instance_root=options.instance_root)}
        status = 0
    except (ImportFailure, ValueError, TypeError, KeyError, OSError) as exc:
        response = {"ok": False, "error": {"code": getattr(exc, "code", "invalid_request"),
                    "message": str(exc), "details": getattr(exc, "details", {})}}
        status = 1
    except Exception as exc:
        # Preserve the standalone layer's structured errors without importing it on inventory reads.
        if not hasattr(exc, "code"):
            raise
        response = {"ok": False, "error": {"code": exc.code, "message": str(exc), "details": getattr(exc, "details", {})}}
        status = 1
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
