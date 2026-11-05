"""Disposable, offline native-image probe; never starts or authenticates a model.

Run inside the selected read-only workstation image with network disabled and
empty writable /business, /home/agent and /tmp mounts. Output contains only tool
versions and synthetic-workload measurements, never credential/configuration data.
"""

import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import time


def run(*args, cwd=None, input=None):
    result = subprocess.run(args, cwd=cwd, input=input, text=True, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError("Native image probe failed: " + args[0])
    return result.stdout.strip()


def main():
    started = time.monotonic()
    assert os.getuid() == 1000 and os.getgid() == 1000
    assert shutil.which("gh") == "/usr/local/bin/gh"
    assert run("bash", "--login", "-c", "command -v gh") == "/usr/local/bin/gh"
    versions = {name: run(*command).splitlines()[0] for name, command in {
        "claude": ["claude", "--version"], "codex": ["codex", "--version"],
        "python": ["/opt/qorqut/venv/bin/python", "--version"], "node": ["node", "--version"],
        "git": ["git", "--version"], "gh": ["gh", "--version"], "tmux": ["tmux", "-V"],
        "cc": ["cc", "--version"], "rg": ["rg", "--version"], "jq": ["jq", "--version"],
    }.items()}
    run("playwright-cli", "--help")
    project = Path("/business/code/smoke/main")
    project.mkdir(parents=True)
    (project.parent / "worktrees").mkdir()
    run("git", "init", "--quiet", cwd=project)
    assert run("git", "symbolic-ref", "--short", "HEAD", cwd=project) == "main"
    (project / "main.c").write_text('#include <stdio.h>\nint main(void) { puts("compile-ok"); return 0; }\n')
    run("cc", "-Wall", "-Werror", "main.c", "-o", "canary", cwd=project)
    assert run(str(project / "canary")) == "compile-ok"
    run("python3", "-m", "venv", str(project / ".venv"))
    assert run(str(project / ".venv/bin/python"), "-I", "-c", "import sqlite3; print(sqlite3.connect(':memory:').execute('SELECT 7').fetchone()[0])") == "7"
    assert run("node", "-e", "process.stdout.write(String(3 + 4))") == "7"
    run("tmux", "-L", "image-smoke", "new-session", "-d", "-s", "one", "sleep 45")
    try:
        run("tmux", "-L", "image-smoke", "new-session", "-d", "-s", "two", "sleep 45")
        assert set(run("tmux", "-L", "image-smoke", "list-sessions", "-F", "#{session_name}").splitlines()) == {"one", "two"}
    finally:
        run("tmux", "-L", "image-smoke", "kill-server")
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(json.dumps({"result": "native-workstation-ok", "versions": versions,
        "synthetic_seconds": round(time.monotonic() - started, 2),
        "child_cpu_seconds": round(usage.ru_utime + usage.ru_stime, 2),
        "largest_child_rss_kib": usage.ru_maxrss}))


if __name__ == "__main__":
    main()
