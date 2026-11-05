"""Bounded integration observations. A projection hash is not readiness, and a submitted nudge is not proof the agent read anything."""
from __future__ import annotations

import os
import logging
from pathlib import Path
import shlex
import subprocess
import time

LOG = logging.getLogger(__name__)
PREPARED_IMPORT_TIMEOUT = 5

# Preparation always generates .bazarlyk/agentic through the stable helper, so
# bazarlyk.agentic.client is as required as the bootstrap modules; a stable
# manifest that omits it is rejected by the launcher before agentic execution.
HELPER_MODULES = ("bazarlyk.bootstrap.claude_session", "bazarlyk.bootstrap.mcp_reconnect",
                  "bazarlyk.bootstrap.transcript_snapshot", "bazarlyk.agentic.client")


def helper_probe(executable, *, run=subprocess.run, timeout=2):
    """Probe only the explicit registered interpreter, never substitute one."""
    start = time.monotonic()
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        return {"ready": False, "reason": "missing_executable", "elapsed_ms": 0}
    try:
        result = run([str(executable), "-I", "-B", "-c",
            "import importlib; " + "; ".join(f"importlib.import_module({module!r})" for module in HELPER_MODULES)],
            capture_output=True, timeout=timeout)
        reason = "available" if result.returncode == 0 else "helper_import_failed"
    except (OSError, subprocess.TimeoutExpired):
        reason = "helper_probe_unavailable"
    return {"ready": reason == "available", "reason": reason,
            "elapsed_ms": round((time.monotonic() - start) * 1000, 3)}


def command_executables(value):
    """Extract commands from generated projections without exposing tokens or arguments."""
    found = set()
    def visit(item):
        if isinstance(item, dict):
            command = item.get("command")
            if isinstance(command, str):
                try:
                    words = shlex.split(command)
                    if words and words[0].startswith("/"):
                        found.add(words[0])
                except ValueError:
                    pass
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return sorted(found)


def integration_facts(*, helper, nudge=None, process="unknown", backend="unknown",
                      delivery="unknown", scope="active", target="local"):
    """``nudge`` is the NudgeBackend status of the agent: idle, pending, deferred, submitted, uncertain, stale, unavailable or
    offline. It describes the advisory notice only; readiness depends on the helper, the verified native process, the backend
    connection and the composer, never on a notice having been sent."""
    state = dict(nudge or {}).get("state", "unknown")
    return {"scope": scope, "execution_target": target, "helper": helper,
            "native_process": process, "backend_connection": backend,
            "nudge": state, "delivery": delivery,
            "ready": scope == "active" and helper.get("ready") is True and process == "verified"
                and backend == "connected" and delivery == "ready"}


def prepared_helpers(projection,workspace=None):
    """Inspect prepared helper executables separately from projection hashes."""
    runtime=projection.get('helper_runtime')
    if not runtime:
        try:
            import json
            settings=json.loads(Path(workspace).joinpath('.claude/settings.json').read_text())
            executables=command_executables(settings)
            selected=next(path for path in executables if 'python' in Path(path).name or Path(path).name=='agent-helper')
            runtime={'executable':selected,'stable':Path(selected).name=='agent-helper'}
        except (OSError,ValueError,StopIteration,TypeError):return {'ready':False,'reason':'helper_runtime_unobserved'}
    executable=Path(runtime['executable'])
    if not executable.is_file() or not os.access(executable,os.X_OK):return {'ready':False,'reason':'missing_executable'}
    if runtime.get('stable'):
        try:
            import json
            selected=executable.parent.parent/'runtime/agent-tools/current'
            manifest=json.loads(selected.resolve(strict=True).joinpath('manifest.json').read_text())
            # The launcher enforces this allowlist before agentic execution, so readiness
            # must match it. Read it on every call; only the import probe is cached, and
            # correcting the allowlist has to recover without a restart or reprepare.
            modules=manifest.get('modules') if isinstance(manifest,dict) else None
            if not isinstance(modules,list) or not all(isinstance(name,str) for name in modules):
                return {'ready':False,'reason':'helper_manifest_invalid'}
            missing=sorted(set(HELPER_MODULES)-set(modules))
            if missing:return {'ready':False,'reason':'helper_module_unsupported','missing_modules':missing}
            python=Path(manifest['python'])
            if not python.is_file() or not os.access(python,os.X_OK):return {'ready':False,'reason':'missing_executable'}
            stat=python.stat()
            result=_prepared_imports(str(python),stat.st_ino,stat.st_mtime_ns,manifest['code'])
            if not result['ready']:return result
            if any(not Path(path).is_file() for path in result['imports']):return {'ready':False,'reason':'helper_import_missing'}
            return result
        except (OSError,ValueError,KeyError):return {'ready':False,'reason':'helper_import_failed'}
    stat=executable.stat()
    result=_prepared_imports(str(executable),stat.st_ino,stat.st_mtime_ns)
    if not result['ready']:return result
    if any(not Path(path).is_file() for path in result['imports']):return {'ready':False,'reason':'helper_import_missing'}
    return result


from functools import lru_cache


class _ImportProbeFailure(Exception):
    def __init__(self, reason):
        self.reason = reason


def _prepared_imports(executable, inode, mtime, code=None):
    # Cache successful imports only. Transient failures must recover on the next
    # check without re-preparing the agent or restarting the application.
    try:
        return _verified_imports(executable, inode, mtime, code)
    except _ImportProbeFailure as exc:
        return {"ready": False, "reason": exc.reason}


@lru_cache(maxsize=32)
def _verified_imports(executable, inode, mtime, code):
    import json
    started = time.monotonic()
    try:
        result = subprocess.run([executable, '-I', '-B', '-c',
            'import importlib,json,sys;' + ('sys.path.insert(0,' + repr(code) + ');' if code else '') +
            '[importlib.import_module(name) for name in ' + repr(list(HELPER_MODULES)) + '];'
            'print(json.dumps(sorted({m.__file__ for m in sys.modules.values() if getattr(m,"__file__",None) and not m.__file__.startswith("<")})))'],
            capture_output=True, text=True, timeout=PREPARED_IMPORT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("Native helper probe unavailable: interpreter=%s elapsed=%.3fs error=%s",
                    executable, time.monotonic() - started, type(exc).__name__)
        raise _ImportProbeFailure("helper_probe_unavailable") from exc
    try:
        imports = json.loads(result.stdout) if result.returncode == 0 else None
        if not isinstance(imports, list) or not imports or not all(isinstance(path, str) for path in imports):
            raise ValueError("Invalid helper import inventory")
    except ValueError as exc:
        LOG.warning("Native helper import failed: interpreter=%s elapsed=%.3fs exit=%s stderr=%s",
                    executable, time.monotonic() - started, result.returncode, result.stderr[-2000:])
        raise _ImportProbeFailure("helper_import_failed") from exc
    return {"ready": True, "reason": "helper_import_verified", "imports": imports}
