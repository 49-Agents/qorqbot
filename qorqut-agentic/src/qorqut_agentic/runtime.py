"""Tool runtime protocol and the opt-in ``LocalProcessRuntime``.

A runtime executes one staged tool invocation. Tool definitions only name a
runtime *alias*; the trusted caller decides which runtime object serves it.

``LocalProcessRuntime`` is a process adapter, **not an OS security sandbox**:
the tool code runs with the privileges of the calling process, can read what
that user can read and reach the network if the interpreter allows it. It
bounds time, output and environment; it does not confine behaviour. It is never
a default: it exists only when the caller constructs and registers it.
"""

from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .errors import InvalidRequestError

DEFAULT_MAX_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
DEFAULT_MAX_STDERR_BYTES = 64 * 1024
_READ_SIZE = 65536
_POST_EXIT_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class ExecutionRequest:
    """One staged invocation handed to a runtime."""

    invocation_id: str
    runtime_alias: str
    code_path: str  # absolute path of the staged code file
    package_dir: str  # staged copy of the code and declared resources
    work_dir: str  # fresh, empty scratch directory for this invocation
    stdin: bytes  # exactly one JSON value
    timeout_seconds: float
    max_output_bytes: int
    max_stderr_bytes: int
    # Optional, trusted-host-only additions for a long-lived host run: descriptors the child inherits (for example a
    # socketpair end) and extra environment names. Both default to nothing, so an ordinary tool invocation is unchanged.
    pass_fds: tuple = ()
    extra_env: Mapping[str, str] = field(default_factory=dict)
    # Optional host abort signal (an object with ``is_set()``): the run ends with outcome ``cancelled`` and the process group is killed.
    cancel: Any = None


@dataclass
class ExecutionResult:
    """What a runtime reports.

    ``outcome`` is ``exited`` (see ``exit_code``), ``timeout``, ``output_overflow``
    or ``spawn_failed`` (the code never started). ``limits`` are the limits the
    runtime actually enforced.
    """

    outcome: str
    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    stderr_truncated: bool = False
    message: str | None = None
    limits: dict[str, Any] = field(default_factory=dict)


class ToolRuntime(Protocol):
    """Anything that can run a staged invocation. Hosts supply their own."""

    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class LocalProcessRuntime:
    """Runs ``[interpreter, code_path]`` as a child process, without a shell.

    * ``interpreters`` maps each served runtime alias to an absolute interpreter path.
    * ``env`` is the complete child environment (default empty); only
      ``QORQUT_AGENTIC_PACKAGE_DIR`` and ``QORQUT_AGENTIC_SCRATCH_DIR`` are added.
      The caller's environment is never inherited.
    * Timeout, stdout and stderr are bounded while reading; the process group is
      killed on timeout or either stream overflowing. Requested limits above the maxima
      configured here are lowered to them.
    """

    def __init__(
        self,
        interpreters: Mapping[str, str],
        *,
        env: Mapping[str, str] | None = None,
        max_timeout_seconds: float = DEFAULT_MAX_TIMEOUT_SECONDS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_STDERR_BYTES,
    ) -> None:
        if not isinstance(interpreters, Mapping) or not interpreters:
            raise InvalidRequestError("LocalProcessRuntime needs at least one interpreter")
        checked: dict[str, str] = {}
        for alias, path in interpreters.items():
            if not isinstance(path, str) or not os.path.isabs(path):
                raise InvalidRequestError(f"interpreter for alias {alias!r} must be an absolute path",
                                          details={"alias": alias})
            if not os.path.isfile(path) or not os.access(path, os.X_OK):
                raise InvalidRequestError(f"interpreter for alias {alias!r} is not an executable file: {path}",
                                          details={"alias": alias, "path": path})
            checked[alias] = path
        if (isinstance(max_timeout_seconds, bool)
                or not isinstance(max_timeout_seconds, (int, float))
                or not math.isfinite(max_timeout_seconds) or max_timeout_seconds <= 0
                or isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int)
                or max_output_bytes < 1 or isinstance(max_stderr_bytes, bool)
                or not isinstance(max_stderr_bytes, int) or max_stderr_bytes < 0):
            raise InvalidRequestError("runtime limits must be positive")
        if env is not None and (not isinstance(env, Mapping) or not all(
            isinstance(k, str) and k and "=" not in k and "\0" not in k
            and isinstance(v, str) and "\0" not in v for k, v in env.items()
        )):
            raise InvalidRequestError("env must map environment names to strings without NULs")
        self.interpreters = checked
        self.env = dict(env or {})
        self.max_timeout_seconds = float(max_timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        self.max_stderr_bytes = int(max_stderr_bytes)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        timeout = min(float(request.timeout_seconds), self.max_timeout_seconds)
        out_cap = min(request.max_output_bytes, self.max_output_bytes)
        err_cap = min(request.max_stderr_bytes, self.max_stderr_bytes)
        limits = {"timeout_seconds": timeout, "max_output_bytes": out_cap, "max_stderr_bytes": err_cap}
        interpreter = self.interpreters.get(request.runtime_alias)
        if interpreter is None:
            return ExecutionResult("spawn_failed", message=f"this runtime does not serve alias {request.runtime_alias!r}",
                                   limits=limits)
        env = dict(self.env)
        env["QORQUT_AGENTIC_PACKAGE_DIR"] = request.package_dir
        env["QORQUT_AGENTIC_SCRATCH_DIR"] = request.work_dir
        env.update(request.extra_env)
        try:
            proc = subprocess.Popen(  # noqa: S603 - explicit interpreter, no shell
                [interpreter, request.code_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=request.work_dir, env=env, shell=False, close_fds=True,
                pass_fds=tuple(request.pass_fds), start_new_session=True,
            )
        except OSError as exc:
            return ExecutionResult("spawn_failed", message=f"could not start the interpreter: {exc}", limits=limits)

        def feed() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(request.stdin)
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        assert proc.stdout is not None and proc.stderr is not None
        selector = selectors.DefaultSelector()
        for stream, name in ((proc.stdout, "out"), (proc.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        out, err = bytearray(), bytearray()
        err_truncated = False
        outcome, message = "exited", None
        deadline = time.monotonic() + timeout
        exited_at: float | None = None
        open_streams = 2
        try:
            while True:
                now = time.monotonic()
                if request.cancel is not None and request.cancel.is_set():
                    outcome = "cancelled"
                    break
                if now >= deadline:
                    outcome = "timeout"
                    break
                if exited_at is None and proc.poll() is not None:
                    exited_at = now
                    _kill_group(proc.pid)  # reap stragglers that would hold the pipes open
                if open_streams == 0 and exited_at is not None:
                    break
                if exited_at is not None and now - exited_at > _POST_EXIT_GRACE_SECONDS:
                    message = "a descendant process kept the output pipes open after the tool exited"
                    break
                overflow = False
                for key, _ in selector.select(timeout=min(0.05, max(deadline - now, 0.001))):
                    try:
                        chunk = os.read(key.fd, _READ_SIZE)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        open_streams -= 1
                        continue
                    if key.data == "out":
                        room = out_cap - len(out)
                        out += chunk[:room]
                        if len(chunk) > room:
                            overflow = True
                    else:
                        room = err_cap - len(err)
                        if room > 0:
                            err += chunk[:room]
                        if len(chunk) > max(room, 0):
                            err_truncated = True
                            overflow = True
                if overflow:
                    outcome = "output_overflow"
                    break
        finally:
            if outcome != "exited" or proc.poll() is None:
                _kill_group(proc.pid)
            selector.close()
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not ignorable
                proc.kill()
            feeder.join(timeout=1)
        if outcome == "output_overflow":
            del out[out_cap:]
        return ExecutionResult(
            outcome=outcome,
            exit_code=proc.returncode if outcome == "exited" else None,
            stdout=bytes(out),
            stderr=bytes(err),
            stderr_truncated=err_truncated,
            message=message,
            limits=limits,
        )
