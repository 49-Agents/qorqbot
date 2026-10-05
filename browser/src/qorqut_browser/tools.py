"""Fixed-CDP helper primitives. No MCP tools or transport are advertised here.

P08 translation adapted from Qorqut's bazarlyk/bootstrap/agent_browser.py at
e71fd1e527368bfd41b9815126f4bf33452aeebb (mapping for agent-browser 0.38.1).
No external helper/runtime package is copied or installed by this package.
"""

import hashlib
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time

from .contracts import (
    Artifact, BrowserConfig, BrowserError, Click, Command, EffectResult, Fill,
    Navigate, Screenshot, Snapshot, digest,
)
from .control import Gate, NotStarted
from .state import Store, check_paths, checked_path, exclusive_file, fsync_directory
from .supervisor import check_ready, child_environment

SOURCE_HELPER_VERSION = "0.38.1"


def helper_argv(config: BrowserConfig, executable: Path, command: Command, directory: Path) -> list[str]:
    """Translate typed actions, never pass through user-supplied helper options."""
    if command.binding.identity != config.binding.identity:
        raise BrowserError("target_changed", "Helper target does not match this browser")
    action = command.action
    if isinstance(action, Snapshot):
        args = ["snapshot", "-c"]
        if action.interactive:
            args.append("-i")
        if action.depth is not None:
            args.extend(["-d", str(action.depth)])
    elif isinstance(action, Navigate):
        args = ["open", action.url]
    elif isinstance(action, (Click, Fill)):
        selector = "@" + action.selector if re.fullmatch(r"e\d+", action.selector) else action.selector
        args = [action.kind, selector]
        if isinstance(action, Fill):
            args.append(action.text)
    elif isinstance(action, Screenshot):
        args = ["screenshot", str(directory / "screenshot.png")]
        if action.full_page:
            args.append("--full")
    else:
        raise BrowserError("unsupported_capability", "Unsupported browser action")
    session = "qorqut-" + digest({"identity": config.binding.identity.model_dump(mode="json"),
                                 "resources": config.resource_hash})[:24]
    return [str(executable), "--config", str(directory / "agent-browser.json"),
            "--session", session, "--cdp", config.cdp_endpoint, *args]


def _run(argv, *, directory, environment, lock_fd, prefix, timeout, limit):
    """Stream bounded output to owned files; inherit the actual process lock."""
    paths = [directory / f"{prefix}.{name}" for name in ("stdout", "stderr")]
    streams = []
    process = None
    try:
        for path in paths:
            streams.append(path.open("xb"))
            os.chmod(path, 0o600)
        process = subprocess.Popen(argv, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True, pass_fds=(lock_fd,), umask=0o077)
        deadline = time.monotonic() + timeout
        total = 0
        with selectors.DefaultSelector() as selector:
            for pipe, output in zip((process.stdout, process.stderr), streams):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, output)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("helper deadline")
                for key, _ in selector.select(min(.05, remaining)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(data)
                    if total > limit:
                        raise BrowserError("output_limit", "Helper output exceeded artifact budget")
                    key.data.write(data)
                screenshot = directory / "screenshot.png"
                if screenshot.exists() and screenshot.lstat().st_size > limit:
                    raise BrowserError("output_limit", "Screenshot exceeded artifact budget")
        code = process.wait(timeout=max(.001, deadline - time.monotonic()))
        for stream in streams:
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(directory)
        return code, paths
    finally:
        if process is not None:
            if process.returncode is None:
                # Only this new unreaped child group, never Chrome's existing PID.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
        for stream in streams:
            stream.close()


def _artifact(path: Path, config: BrowserConfig, media_type: str, limit: int) -> Artifact:
    info = checked_path(path, config.uid)
    if info.st_size > limit:
        raise BrowserError("output_limit", "Artifact exceeds the configured limit")
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(65536):
            result.update(block)
        os.fsync(stream.fileno())
    fsync_directory(path.parent)
    return Artifact(path=path, size=info.st_size, sha256=result.hexdigest(), media_type=media_type)


class AgentBrowser:
    def __init__(self, config: BrowserConfig, executable: Path, *, timeout=45,
                 artifact_limit=64 * 1024 * 1024, readiness=check_ready):
        if not executable.is_absolute() or timeout <= 0 or artifact_limit < 8192:
            raise ValueError("Explicit executable and positive bounded helper limits required")
        self.config, self.executable = config, executable
        self.timeout, self.artifact_limit = timeout, artifact_limit
        self.readiness = readiness

    def __call__(self, command: Command, lock_fd: int) -> EffectResult:
        config = self.config
        try:
            check_paths(config)
            if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
                raise NotStarted("not_ready", "Configured agent-browser executable is unavailable")
            directory = config.artifacts / str(command.command_id)
            directory.mkdir(mode=0o700)  # never overwrite artifacts from an earlier effect
            fsync_directory(directory.parent)
            exclusive_file(directory / "agent-browser.json", b"{}\n")
            temporary = config.database.parent / "helper-tmp"
            temporary.mkdir(mode=0o700, exist_ok=True)
            checked_path(temporary, config.uid, directory=True)
            environment = child_environment(config)
            environment.update(TMPDIR=str(temporary), XDG_RUNTIME_DIR=str(temporary))
            version_code, version_paths = _run([str(self.executable), "--version"], directory=directory,
                environment=environment, lock_fd=lock_fd, prefix="version", timeout=min(5, self.timeout), limit=4096)
            version = version_paths[0].read_text().strip()
            if version_code or version not in {SOURCE_HELPER_VERSION, f"agent-browser {SOURCE_HELPER_VERSION}"}:
                raise NotStarted("unsupported_capability", "Helper version does not match the inspected translation")
            argv = helper_argv(config, self.executable, command, directory)
            # The version probe may have taken time; recheck the exact live target
            # immediately before dispatch, still under the same command lock.
            self.readiness(config)
        except NotStarted:
            raise
        except Exception as error:
            raise NotStarted("not_ready", "Helper preparation failed before browser dispatch") from error
        code, paths = _run(argv, directory=directory, environment=environment, lock_fd=lock_fd,
                           prefix="command", timeout=self.timeout, limit=self.artifact_limit)
        if code:
            raise BrowserError("uncertain_effect", "Helper failed after possible browser dispatch")
        artifacts = []
        text = ""
        for index, path in enumerate(paths):
            size = path.stat().st_size
            if index == 0 and size <= 8192:
                text = path.read_bytes().decode("utf-8", errors="replace")
            elif size:
                artifacts.append(_artifact(path, config, "text/plain", self.artifact_limit))
        if isinstance(command.action, Screenshot):
            artifacts.append(_artifact(directory / "screenshot.png", config, "image/png", self.artifact_limit))
        return EffectResult(text=text, artifacts=tuple(artifacts))


class BrowserTools:
    """Local composition; both future transports will call this same gate."""

    def __init__(self, config: BrowserConfig, executable: Path):
        self.gate = Gate(Store(config), AgentBrowser(config, executable))

    def execute(self, command: Command, *, actor_id):
        return self.gate.execute(command, actor_id=actor_id)
