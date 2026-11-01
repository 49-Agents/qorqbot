"""Bounded integration observations. A lease or projection hash is not readiness."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shlex
import subprocess
import time

HELPER_MODULES = ("bazarlyk.bootstrap.claude_session", "bazarlyk.bootstrap.mcp_reconnect",
                  "bazarlyk.inbox.watcher", "bazarlyk.bootstrap.transcript_snapshot")
SENTINEL = "1970-01-01T00:00:00.000000Z"


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


def integration_facts(*, helper, watcher=None, process="unknown", backend="unknown",
                      delivery="unknown", scope="active", target="local", now=None):
    now = now or datetime.now(timezone.utc)
    watcher = dict(watcher or {})
    checked = watcher.get("last_check_at", SENTINEL)
    try:
        fresh = checked != SENTINEL and (now - datetime.fromisoformat(checked.replace("Z", "+00:00"))).total_seconds() < 360
    except (TypeError, ValueError):
        fresh = False
    return {"scope": scope, "execution_target": target, "helper": helper,
            "native_process": process, "backend_connection": backend,
            "watcher": "fresh" if fresh else "never_polled" if checked == SENTINEL else "stale",
            "delivery": delivery,
            "ready": scope == "active" and helper.get("ready") is True and process == "verified"
                and backend == "connected" and fresh and delivery == "ready"}


def prepared_helpers(projection,workspace=None):
    """Inspect prepared helper executables separately from projection hashes."""
    from functools import lru_cache
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
@lru_cache(maxsize=32)
def _prepared_imports(executable,inode,mtime,code=None):
    import json
    try:
        result=subprocess.run([executable,'-I','-B','-c',
            'import importlib,json,sys;'+('sys.path.insert(0,'+repr(code)+');' if code else '')+
            '[importlib.import_module(name) for name in '+repr(list(HELPER_MODULES))+'];'
            'print(json.dumps(sorted({m.__file__ for m in sys.modules.values() if getattr(m,"__file__",None) and not m.__file__.startswith("<")})))'],
            capture_output=True,text=True,timeout=1)
        if result.returncode==0:return {'ready':True,'reason':'helper_import_verified','imports':json.loads(result.stdout)}
    except (OSError,ValueError,subprocess.SubprocessError):pass
    return {'ready':False,'reason':'helper_import_failed'}
