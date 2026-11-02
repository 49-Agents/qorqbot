"""The existing fixed repository-status routine, without an argv interface."""

import os
import subprocess

from bazarlyk.composition.locations import business_hq
from qorqut_operations.contracts import DomainError
from qorqut_operations.background.programs import ProgramResult


def repository_status(control, arguments):
    with control.service.database.read() as uow:
        business = uow.repository("businesses").get(control.business_id, required=True)
    paths = control.service.paths
    root = paths.operations.resolve(strict=True)
    path = business_hq(paths, business).resolve(strict=True)
    if not path.is_relative_to(root):
        raise DomainError("invalid_configuration", "Registered repository is outside the operations root")
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0"}
    for key in tuple(environment):
        if key.startswith("GIT_CONFIG_COUNT") or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_") or key in {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_CONFIG_PARAMETERS"}:
            environment.pop(key)
    control.check()
    from bazarlyk.native_paths import executable
    process = subprocess.Popen([executable("git", "/usr/bin/git"), "--no-optional-locks", "-c", "core.fsmonitor=false",
        "status", "--short", "--untracked-files=normal"], cwd=path, env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                control.check()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if process.returncode:
        raise DomainError("repository_status_failed", stderr.decode("utf-8", errors="replace")[:2000])
    if len(stdout) > 1024 * 1024:
        raise DomainError("result_too_large", "Repository status exceeds the bounded output limit")
    return ProgramResult(data={"stdout": stdout.decode("utf-8", errors="replace"), "returncode": 0})
