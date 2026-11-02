"""Offline readiness and opt-in staging for the Outlook bridge's pinned browser client.

The bridge loads ``playwright-core`` from beside ``outlook_bridge.mjs``. A release that
lacks it cannot reach Outlook at all, and that used to surface as a generic
``outlook_bridge_unavailable``. This module gives the operator two explicit commands:

    python -m engine_email.outlook_dependencies check [--bridge PATH] [--node NODE] [--require]
    python -m engine_email.outlook_dependencies stage --source DIR --stage NEW_DIR [...]

``check`` runs the installed bridge with ``--check-dependencies``, so resolution happens at
exactly the location the service would use, with the configured Node. It reads no
configuration file, opens no browser and contacts no provider or network endpoint.
When Outlook is not configured (``EMAIL_OUTLOOK_WEB_FILE`` unset) a Fastmail-only
installation is simply "not required"; when it is configured, a failed check exits
non-zero and no other sender or provider is ever substituted.

``stage`` is never run by the service or by tests that touch a release. It prepares the
pinned dependency tree in a NEW directory and refuses any existing artifact. It never
touches ``/opt``, never installs a browser and never starts a service.

Output is one JSON line of stable codes. Exception text, paths and process output are
never copied into it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

BRIDGE_FILES = ("outlook_bridge.mjs", "outlook_capture.mjs", "outlook_quota.mjs")
MANIFESTS = ("package.json", "package-lock.json")
CHECK_FLAG = "--check-dependencies"
MIN_NODE_MAJOR = 20
CHECK_TIMEOUT_SECONDS = 30
INSTALL_TIMEOUT_SECONDS = 600
NPM_ARGS = ("ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund")
PROTECTED_ROOTS = (Path("/opt"),)
_CODE = re.compile(r"outlook_[a-z_]{1,80}")
_VERSION = re.compile(r"\d+\.\d+\.\d+")
_NODE_VERSION = re.compile(r"v(\d+)\.\d+\.\d+")

EXIT_READY, EXIT_NOT_READY, EXIT_USAGE = 0, 2, 64


def packaged_bridge() -> Path:
    """The bridge the service launches: the one beside this module."""
    return Path(__file__).with_name("outlook_bridge.mjs")


def outlook_configured(environ: dict[str, str] | None = None) -> bool:
    """Outlook is configured exactly when the service would load its config file."""
    return bool((environ if environ is not None else os.environ).get("EMAIL_OUTLOOK_WEB_FILE"))


def _node_major(node: str, environ: dict[str, str]) -> tuple[int | None, str | None]:
    try:
        result = subprocess.run([node, "--version"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=CHECK_TIMEOUT_SECONDS,
            check=False, env=environ)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    match = _NODE_VERSION.fullmatch(result.stdout.decode("ascii", "replace").strip())
    return (int(match.group(1)), match.group(0)) if match else (None, None)


def check_dependencies(bridge: Path | None = None, node: str | None = None, *,
        environ: dict[str, str] | None = None) -> dict:
    """Is the pinned ``playwright-core`` loadable from the bridge's own location?

    Returns ``{"ready": bool, "code": str, ...}``; ``code`` is ``ready`` or a stable
    ``outlook_*`` / ``node_*`` identifier. Never raises for an unready install."""
    environ = dict(os.environ if environ is None else environ)
    bridge = Path(bridge) if bridge is not None else packaged_bridge()
    resolved = node or shutil.which("node", path=environ.get("PATH"))
    if resolved is None:
        return {"ready": False, "code": "outlook_node_missing"}
    major, version = _node_major(resolved, environ)
    if major is None:
        return {"ready": False, "code": "outlook_node_unusable"}
    base = {"node": version}
    if major < MIN_NODE_MAJOR:
        return {"ready": False, "code": "outlook_node_unsupported", **base}
    try:
        text = bridge.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {"ready": False, "code": "outlook_bridge_missing", **base}
    if CHECK_FLAG not in text:
        # A bridge from before this check would treat the flag as a normal request.
        return {"ready": False, "code": "outlook_readiness_unsupported", **base}
    try:
        process = subprocess.run([resolved, str(bridge), CHECK_FLAG], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=CHECK_TIMEOUT_SECONDS,
            check=False, env=environ)
        response = json.loads(process.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return {"ready": False, "code": "outlook_bridge_unavailable", **base}
    except (ValueError, UnicodeError):
        return {"ready": False, "code": "outlook_bridge_response_invalid", **base}
    if not isinstance(response, dict):
        return {"ready": False, "code": "outlook_bridge_response_invalid", **base}
    if response.get("ok") is True:
        result = response.get("result")
        found = result.get("version") if isinstance(result, dict) else None
        if (isinstance(result, dict) and result.get("dependency") == "playwright-core"
                and isinstance(found, str) and _VERSION.fullmatch(found)):
            return {"ready": True, "code": "ready", "playwright_core": found, **base}
        return {"ready": False, "code": "outlook_bridge_response_invalid", **base}
    code = response.get("error")
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        code = "outlook_bridge_failed"
    return {"ready": False, "code": code, **base}


def _report(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True))


def run_check(args: argparse.Namespace, environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    required = args.require or outlook_configured(environ)
    if not required:
        _report({"required": False, "ready": None, "code": "outlook_not_configured"})
        return EXIT_READY
    outcome = check_dependencies(args.bridge, args.node, environ=environ)
    _report({"required": True, **outcome})
    return EXIT_READY if outcome["ready"] else EXIT_NOT_READY


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def stage_problem(source: Path, stage: Path) -> str | None:
    """A reason this directory may not be staged into, or ``None`` if it is a safe NEW target.

    Pure path inspection: nothing is created, read from a service tree or executed."""
    if stage.is_symlink():
        return "stage_target_is_symlink"
    resolved = stage.resolve(strict=False)
    if any(_inside(resolved, root.resolve(strict=False)) for root in PROTECTED_ROOTS):
        return "stage_target_protected"
    if _inside(resolved, source.resolve(strict=False)) or _inside(source.resolve(strict=False), resolved):
        return "stage_target_overlaps_source"
    if stage.exists() and (not stage.is_dir() or any(stage.iterdir())):
        return "stage_target_exists"
    return None


def source_problem(source: Path) -> str | None:
    """Stage only from a source checkout: never read from (or copy out of) a protected tree."""
    resolved = source.resolve(strict=False)
    if any(_inside(resolved, root.resolve(strict=False)) for root in PROTECTED_ROOTS):
        return "stage_source_protected"
    missing = [name for name in (*BRIDGE_FILES, *MANIFESTS) if not (source / name).is_file()]
    return "stage_source_incomplete" if missing else None


def run_stage(args: argparse.Namespace, environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ if environ is None else environ)
    source, stage = Path(args.source), Path(args.stage)
    problem = source_problem(source) or stage_problem(source, stage)
    if problem:
        _report({"staged": False, "ready": False, "code": problem})
        return EXIT_USAGE
    npm = args.npm or shutil.which("npm", path=environ.get("PATH"))
    node = args.node or shutil.which("node", path=environ.get("PATH"))
    if npm is None or node is None:
        _report({"staged": False, "ready": False, "code": "outlook_node_missing"})
        return EXIT_USAGE
    if args.dry_run:
        _report({"staged": False, "ready": None, "code": "dry_run", "would_run": " ".join(("npm", *NPM_ARGS))})
        return EXIT_READY
    stage.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = stage / "engine_email"
    target.mkdir(mode=0o700)
    for name in (*BRIDGE_FILES, *MANIFESTS):
        shutil.copy2(source / name, target / name)
    # The chosen Node must front PATH so npm's own launcher uses it, never a system Node.
    install_env = {**environ, "PATH": os.pathsep.join(
        [str(Path(node).resolve().parent), environ.get("PATH", "")]),
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}
    command = [npm, *NPM_ARGS, *(("--offline",) if args.offline else ())]
    try:
        installed = subprocess.run(command, cwd=target, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=INSTALL_TIMEOUT_SECONDS,
            check=False, env=install_env).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        installed = False
    if not installed:
        _report({"staged": True, "installed": False, "ready": False, "code": "outlook_dependency_install_failed"})
        return EXIT_NOT_READY
    outcome = check_dependencies(target / "outlook_bridge.mjs", node, environ=environ)
    _report({"staged": True, "installed": True, **outcome})
    return EXIT_READY if outcome["ready"] else EXIT_NOT_READY


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m engine_email.outlook_dependencies",
        description=__doc__.split("\n\n")[0])
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="offline readiness of the installed bridge")
    check.add_argument("--bridge", type=Path, help="installed outlook_bridge.mjs (default: the packaged one)")
    check.add_argument("--node", help="Node executable (default: node on PATH, as the service launches it)")
    check.add_argument("--require", action="store_true",
        help="fail when not ready even if EMAIL_OUTLOOK_WEB_FILE is unset")
    stage = commands.add_parser("stage", help="opt-in: prepare pinned npm dependencies in a NEW directory")
    stage.add_argument("--source", type=Path, required=True, help="engine_email directory to stage from")
    stage.add_argument("--stage", type=Path, required=True, help="new empty directory; never an existing artifact")
    stage.add_argument("--node", help="Node executable to use for npm and the readiness check")
    stage.add_argument("--npm", help="npm executable (default: npm on PATH)")
    stage.add_argument("--offline", action="store_true", help="install from the local npm cache only")
    stage.add_argument("--dry-run", action="store_true", help="validate the target and print the plan only")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return run_check(args) if args.command == "check" else run_stage(args)


if __name__ == "__main__":
    sys.exit(main())
