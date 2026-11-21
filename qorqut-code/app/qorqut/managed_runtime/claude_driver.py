"""Drive one long-lived headless Claude Code session in a prepared agent folder.

This replaces typing into the terminal UI for a managed agent. It starts the CLI directly::

    claude <caller args> -p --input-format stream-json --output-format stream-json --verbose \\
        --permission-prompt-tool stdio (--session-id=<new uuid> | --resume=<id>)

and speaks its stream-json protocol over stdin and stdout, so it needs no extra package and keeps the CLI's own default
system prompt. Everything about the process (reading, writing, killing) lives in ``_ProcessTransport``; the protocol lives in
``ClaudeSession``. A different transport (for example the Python SDK) can replace the first by setting
``ClaudeSession.transport_class`` to anything with the same four-method shape (``start``, ``write``, ``close_input``,
``close``) that calls ``on_message(dict)``, ``on_problem(kind, detail)`` and ``on_exit(code)``.

What the protocol was observed to do (Claude Code 2.1.269 and 2.1.327, see docs/MANAGED-RUNTIME.md):

* A user record ``{"type": "user", "uuid", "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}`` is
  queued by the CLI (``command_lifecycle`` ``queued`` then ``started`` then ``completed``, keyed by ``command_uuid``). A
  message sent while a turn runs waits until the running tool call ends, then is answered inside the same run, which ends
  with ONE ``result`` record whose ``user_message_uuids`` lists every message it covered.
* ``{"type": "control_request", "request": {"subtype": "interrupt"}}`` is answered at once (``still_queued``) and ends the
  run with ``result`` / ``error_during_execution``; the next message starts a normal new run. On an idle session it is
  answered at once and does nothing.
* A prompt blocked by a UserPromptSubmit hook still produces ``result`` / ``success`` (``num_turns`` 0). The only marker is
  a ``system`` / ``informational`` record carrying ``prompt_submit_outcome.kind == "blocked_by_hook"``. When such a prompt was
  steered into a run that was holding a tool call, the CLI reports its lifecycle ``started`` then ``completed`` (then the
  ``informational`` record) while the run's ``result`` does NOT list it in ``user_message_uuids``; a ``completed`` seen before
  the run's ``result`` therefore finishes that message together with the run's turn (Claude Code 2.1.327, captured).
* ``system`` / ``init`` arrives only after the first message was taken up and its UserPromptSubmit hooks finished, so a new
  session is announced (``session_started``) late; ``session_id`` is known from the start (``--session-id`` is ours).
* With ``--permission-prompt-tool stdio`` the CLI sends ``control_request`` / ``can_use_tool`` (``tool_name``, ``input``,
  ``tool_use_id``, ...) and waits for a ``control_response`` carrying ``{"behavior": "allow", "updatedInput": ...}`` or
  ``{"behavior": "deny", "message": ...}``. A question to the user (the AskUserQuestion tool) arrives the same way.
* Every ``assistant`` record carries the ``usage`` of the model request it came from (``input_tokens``,
  ``cache_creation_input_tokens``, ``cache_read_input_tokens``, ``output_tokens``); the three input numbers together are the whole
  context that request sent. A subagent's records have a ``parent_tool_use_id`` and count its own context, not the session's.
  ``result`` carries ``modelUsage`` ``{<model>: {..., "contextWindow"}}``, the model's limit (Claude Code 2.1.328, captured).

Threads: a reader (stdout), a stderr drain, a writer (stdin), a waiter (process exit) and an event dispatcher. The reader
never calls user code, so a slow ``on_event`` cannot fill the CLI's output pipe; the writer is the only thread that can block
on stdin, so ``send``, ``answer`` and ``interrupt`` never block the caller.
"""

from __future__ import annotations

import collections
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

__all__ = ["ClaudeSession", "ClaudeSessionError"]

# Flags the driver owns. A caller cannot change the protocol, the session identity or the permission route through ``args``.
_PROTECTED_FLAGS = frozenset({
    "-p", "--print", "--input-format", "--output-format", "--permission-prompt-tool", "--resume", "-r", "--session-id",
    "--continue", "-c", "--fork-session", "--replay-user-messages",
})
_SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NAME_CHARS = r"[A-Za-z0-9_.\-]*"
_SECRET_LIKE = (
    (re.compile(r"(?i)\bbearer\s+\S+"), "[redacted]"),
    # NAME=value / NAME: value where the name merely contains a credential word (ANTHROPIC_API_KEY, GITHUB_TOKEN, DB_PASSWORD ...)
    (re.compile(r"(?i)" + _NAME_CHARS + r"(?:api[_-]?key|access[_-]?key|token|secret|passw(?:or)?d|pwd|authorization|credential|"
                r"private[_-]?key)s?" + _NAME_CHARS + r"[\"']?\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|\S+)"), "[redacted]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@]+@"), r"\1[redacted]@"),      # scheme://user:password@host, scheme://token@host
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-.]{16,}"), "[redacted]"),
    (re.compile(r"[A-Za-z0-9+/_\-]{32,}"), "[redacted]"),
)
_ID_SAFE = re.compile(r"[^A-Za-z0-9_.:\-]")
_RESULT_MARK = re.compile(rb'"type"\s*:\s*"result"')
_RESULT_HEAD = re.compile(rb'^\s*\{\s*"type"\s*:\s*"result"')
_DROPPED_STATES = frozenset({"cancelled", "canceled", "dropped", "discarded", "removed"})


# Run as ``python -I -S -c <this> <parent pid> <argv...>``: ask the kernel to SIGKILL this process when its parent (thread) dies, then
# replace itself with the CLI (same pid, so the pid the host records is the CLI's). Isolated mode so nothing in the agent folder
# (the working directory) can shadow a module. If the parent is already gone by now, die instead of running unsupervised.
_PDEATHSIG_SHIM = (
    "import os, signal, sys\n"
    "try:\n"
    "    import ctypes\n"
    "    ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGKILL), 0, 0, 0)\n"
    "except Exception:\n"
    "    pass\n"
    "if os.getppid() != int(sys.argv[1]):\n"
    "    os._exit(1)\n"
    "try:\n"
    "    os.execvp(sys.argv[2], sys.argv[2:])\n"
    "except OSError as error:\n"
    "    sys.stderr.write('could not start the CLI: %s\\n' % type(error).__name__)\n"
    "    os._exit(127)\n"
)


class ClaudeSessionError(RuntimeError):
    """The session cannot do what was asked (not started, already closed, backlog full, could not start the CLI)."""


def _clean(text):
    text = "".join(ch if ch.isprintable() or ch == " " else " " for ch in str(text))
    return " ".join(text.split())


def _bounded(text, limit):
    text = _clean(text)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def scrub_diagnostic(raw, limit=300):
    """The last ``limit`` characters of CLI diagnostics (bytes or str) with anything credential-shaped replaced, on one line."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    text = _clean(text)[-4 * limit:]
    for pattern, replacement in _SECRET_LIKE:
        text = pattern.sub(replacement, text)
    return text if len(text) <= limit else "\u2026" + text[-(limit - 1):]


def _shorten_input(value, string_limit=2000, depth=0):
    """A copy of a tool input with long strings and deep or wide structures cut, for a permission card."""
    truncated = False
    if isinstance(value, str):
        if len(value) > string_limit:
            return value[:string_limit] + "…", True
        return value, False
    if depth >= 6:
        return "…", True
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                truncated = True
                break
            out[str(key)], cut = _shorten_input(item, string_limit, depth + 1)
            truncated = truncated or cut
        return out, truncated
    if isinstance(value, (list, tuple)):
        out = []
        for index, item in enumerate(value):
            if index >= 50:
                truncated = True
                break
            item, cut = _shorten_input(item, string_limit, depth + 1)
            out.append(item)
            truncated = truncated or cut
        return out, truncated
    return value, False


INPUT_LIMIT_BYTES = 16 * 1024
SUGGESTION_LIMIT = 10
SUGGESTION_ITEM_BYTES = 2 * 1024
SUGGESTIONS_TOTAL_BYTES = 8 * 1024


def _json_size(value):
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return INPUT_LIMIT_BYTES + 1


def _bounded_input(value, limit=INPUT_LIMIT_BYTES):
    """``(copy, truncated)``: the whole tool input when its JSON fits ``limit`` bytes (so a card can show the full command),
    otherwise a copy with strings, depth and width cut until it does. ``truncated`` tells the UI it is not the whole input."""
    shown, truncated = _shorten_input(value, string_limit=limit)
    for string_limit in (4000, 2000, 1000, 400, 150):
        if _json_size(shown) <= limit:
            return shown, truncated
        shown, _ = _shorten_input(value, string_limit=string_limit)
        truncated = True
    return (shown, True) if _json_size(shown) <= limit else ({"\u2026": "input too large to show"}, True)


def _bounded_suggestions(suggestions):
    """At most ``SUGGESTION_LIMIT`` permission suggestions, each at most ``SUGGESTION_ITEM_BYTES`` of JSON (a bigger one is
    replaced by ``{"type": ..., "truncated": true}``), all together at most ``SUGGESTIONS_TOTAL_BYTES``."""
    kept, total = [], 0
    for item in suggestions[:SUGGESTION_LIMIT]:
        size = _json_size(item)
        if size > SUGGESTION_ITEM_BYTES:
            item = {"type": _bounded(item.get("type") or "", 40) if isinstance(item, dict) else "", "truncated": True}
            size = _json_size(item)
        if total + size > SUGGESTIONS_TOTAL_BYTES:
            break
        kept.append(item)
        total += size
    return kept


def _tool_activity(name, tool_input):
    """One short plain sentence about a tool call. Never the command or the content, only a file name, host or the model's own
    one-line description, each cut short."""
    name = str(name or "")
    data = tool_input if isinstance(tool_input, dict) else {}

    def base(key="file_path"):
        value = data.get(key) or data.get("path") or data.get("notebook_path")
        return _bounded(os.path.basename(str(value)), 60) if isinstance(value, str) and value else ""

    if name == "Read":
        return "Reading " + base() if base() else "Reading a file"
    if name == "Write":
        return "Writing " + base() if base() else "Writing a file"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "Editing " + base() if base() else "Editing a file"
    if name == "Bash":
        described = data.get("description")
        return _bounded(described, 80) if isinstance(described, str) and described.strip() else "Running a command"
    if name in ("Glob", "Grep"):
        return "Searching files"
    if name == "WebSearch":
        return "Searching the web"
    if name == "WebFetch":
        host = urlsplit(str(data.get("url") or "")).hostname
        return "Fetching " + _bounded(host, 60) if host else "Fetching a web page"
    if name in ("Task", "Agent"):
        return "Running a subagent"
    if name == "ToolSearch":
        return "Looking up tools"
    if name == "TodoWrite":
        return "Updating the to-do list"
    if name == "AskUserQuestion":
        return "Asking a question"
    if name == "Skill":
        skill = data.get("skill") or data.get("name")
        return "Using skill " + _ID_SAFE.sub("", str(skill))[:40] if skill else "Using a skill"
    if name.startswith("mcp__"):
        _, _, rest = name.partition("mcp__")
        server, _, tool = rest.partition("__")
        return "Using %s (%s)" % (_ID_SAFE.sub("", tool)[:40] or "a tool", _ID_SAFE.sub("", server)[:40] or "connector")
    return "Using " + (_ID_SAFE.sub("", name)[:40] or "a tool")


def _transcript_path(folder, session_id, env):
    """Where Claude Code writes this session's transcript: ``<config dir>/projects/<folder, non-alphanumerics as "-">/<id>.jsonl``.
    When the computed file does not exist the other project folders are checked for the id (Claude Code shortens very long
    folder names with a hash this does not reproduce); failing that the computed path is returned."""
    config = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".claude")
    claude_projects = Path(config) / "projects"
    try:
        real = os.path.realpath(folder)
    except OSError:
        real = str(folder)
    expected = claude_projects / re.sub(r"[^A-Za-z0-9]", "-", real) / (session_id + ".jsonl")
    if expected.exists():
        return str(expected)
    try:
        for candidate in claude_projects.iterdir():
            found = candidate / (session_id + ".jsonl")
            if found.exists():
                return str(found)
    except OSError:
        pass
    return str(expected)


class _ProcessTransport:
    """The CLI as a child process: newline-delimited JSON in both directions, nothing about what the records mean.

    ``on_message(dict)``, ``on_problem(kind, detail)`` and ``on_exit(code)`` are called from this class's own threads; ``on_exit``
    is called once, after every message has been delivered."""

    MAX_LINE_BYTES = 8 * 1024 * 1024
    MAX_PENDING_WRITE_BYTES = 16 * 1024 * 1024
    STDERR_TAIL_BYTES = 8 * 1024
    DRAIN_SECONDS = 2.0

    def __init__(self, argv, cwd, env, on_message, on_problem, on_exit):
        self._argv, self._cwd, self._env = list(argv), cwd, env
        self._on_message, self._on_problem, self._on_exit = on_message, on_problem, on_exit
        self._process = None
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending = collections.deque()
        self._pending_bytes = 0
        self._input_closed = False
        self._stderr_tail = bytearray()
        self._exited = threading.Event()
        self._reap_lock = threading.Lock()   # held while the process group is signalled and while the leader is reaped
        self._reaped = False
        self._threads = []
        self.exit_code = None

    # -- lifecycle ---------------------------------------------------------------------------------------------------
    DIE_WITH_PARENT = True

    def _spawn_argv(self):
        """The argv to execute: the CLI's own, behind the PDEATHSIG shim on Linux when the CLI can be resolved here (a CLI that
        cannot be found is left to ``Popen`` to refuse with the usual ``FileNotFoundError``)."""
        argv = list(self._argv)
        if not (self.DIE_WITH_PARENT and sys.platform == "linux" and sys.executable and argv):
            return argv
        executable = argv[0]
        resolved = executable if os.sep in executable else shutil.which(executable, path=self._env.get("PATH") or os.defpath)
        if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
            return argv
        return [sys.executable, "-I", "-S", "-c", _PDEATHSIG_SHIM, str(os.getpid()), resolved, *argv[1:]]

    def start(self):
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        self._process = subprocess.Popen(
            self._spawn_argv(), cwd=self._cwd, env=self._env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, **kwargs)
        for name, target in (("read", self._read_stdout), ("stderr", self._read_stderr), ("write", self._write_stdin),
                             ("wait", self._wait_exit)):
            thread = threading.Thread(target=target, name="claude-driver-" + name, daemon=True)
            self._threads.append(thread)
            thread.start()

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    @property
    def exited(self):
        return self._exited.is_set()

    def stderr_tail(self):
        with self._lock:
            return bytes(self._stderr_tail)

    def write(self, record):
        """Queue one JSON record; never blocks on the pipe. Raises ClaudeSessionError when the child is gone or its input is
        backed up beyond ``MAX_PENDING_WRITE_BYTES``."""
        data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._wake:
            if self._input_closed or self._exited.is_set():
                raise ClaudeSessionError("the Claude session is not running")
            if self._pending_bytes + len(data) > self.MAX_PENDING_WRITE_BYTES:
                raise ClaudeSessionError("the Claude session is not accepting input (backlog full)")
            self._pending.append(data)
            self._pending_bytes += len(data)
            self._wake.notify()

    def close_input(self):
        """End of input after everything queued so far (the CLI finishes its run and exits)."""
        with self._wake:
            self._input_closed = True
            self._wake.notify()

    def close(self, grace, term_grace):
        """Stop the child and everything it started: end its input and wait ``grace`` seconds, then SIGTERM the process group
        and wait ``term_grace``, then SIGKILL. Always sweeps the group at the end so no helper outlives the session. Bounded
        by about ``grace + term_grace + 3`` seconds."""
        if self._process is None:
            return
        self.close_input()
        if not self._exited.wait(grace):
            self._signal(signal.SIGTERM)
            if not self._exited.wait(term_grace):
                self._signal(signal.SIGKILL)
                self._exited.wait(2.0)
        self._signal(signal.SIGKILL, group_only=True)
        deadline = time.monotonic() + 2.0
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(max(0.05, deadline - time.monotonic()))

    def join_all(self, timeout):
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))

    def alive_threads(self):
        return [thread for thread in self._threads if thread.is_alive()]

    def _signal(self, sig, group_only=False):
        """Signal the CLI's process group (it leads its own session), or just the CLI. Only while the leader has not been
        reaped: after that its pid may belong to an unrelated process, so nothing is signalled by number any more."""
        process = self._process
        if process is None:
            return
        with self._reap_lock:
            if self._reaped:
                return
            if os.name == "posix":
                try:
                    os.killpg(process.pid, sig)
                    return
                except (ProcessLookupError, PermissionError):
                    if group_only:
                        return
                except OSError:
                    pass
            if group_only:
                return
            try:
                os.kill(process.pid, sig)
            except OSError:
                pass

    # -- threads -----------------------------------------------------------------------------------------------------
    def _write_stdin(self):
        stream = self._process.stdin
        try:
            while True:
                with self._wake:
                    while not self._pending and not self._input_closed and not self._exited.is_set():
                        self._wake.wait()
                    if self._pending:
                        data = self._pending.popleft()
                        self._pending_bytes -= len(data)
                    elif self._input_closed or self._exited.is_set():
                        return
                try:
                    stream.write(data)
                    stream.flush()
                except (OSError, ValueError) as error:
                    if not self._exited.is_set():
                        self._on_problem("write_failed", type(error).__name__)
                    return
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _read_stdout(self):
        stream, limit = self._process.stdout, self.MAX_LINE_BYTES
        try:
            while True:
                line = stream.readline(limit + 1)
                if not line:
                    return
                if len(line) > limit and not line.endswith(b"\n"):
                    tail, head = bytes(line[-2048:]), bytes(line[:64])
                    length = len(line)
                    while True:  # discard the rest of the oversized record without holding it
                        chunk = stream.readline(limit)
                        length += len(chunk)
                        tail = (tail + chunk)[-2048:]
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    self._on_problem("oversized_line", {"bytes": length, "looks_like_result": bool(_RESULT_MARK.search(tail) or _RESULT_HEAD.match(head))})
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self._on_problem("malformed_line", {"bytes": len(line)})
                    continue
                if not isinstance(message, dict):
                    self._on_problem("malformed_line", {"bytes": len(line)})
                    continue
                try:
                    self._on_message(message)
                except Exception as error:  # a bug in a handler must not stop the pipe being drained
                    self._on_problem("handler_failed", type(error).__name__)
        except (OSError, ValueError):
            return

    def _read_stderr(self):
        stream = self._process.stderr
        try:
            while True:
                chunk = stream.read1(4096)
                if not chunk:
                    return
                with self._lock:
                    self._stderr_tail += chunk
                    if len(self._stderr_tail) > self.STDERR_TAIL_BYTES:
                        del self._stderr_tail[: len(self._stderr_tail) - self.STDERR_TAIL_BYTES]
        except (OSError, ValueError):
            return

    def _wait_for_exit_without_reaping(self):
        """Block until the CLI has exited but leave it unreaped (a zombie still owns its pid, so the pid and its process group
        number cannot be reused). ``False`` when this platform cannot do that here."""
        pid = self._process.pid
        if hasattr(os, "waitid"):
            try:
                os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
                return True
            except OSError:
                return False
        try:                                    # macOS: kqueue reports the exit without reaping (not run on a Mac here)
            import select
            queue = select.kqueue()
            try:
                event = select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT, select.KQ_NOTE_EXIT)
                queue.control([event], 1, None)
                return True
            finally:
                queue.close()
        except (AttributeError, OSError, ValueError):
            return False

    def _wait_exit(self):
        if os.name == "posix" and self._wait_for_exit_without_reaping():
            with self._reap_lock:
                # anything the CLI started and left behind is removed now, while the leader's pid is still ours
                try:
                    os.killpg(self._process.pid, signal.SIGKILL)
                except OSError:
                    pass
                code = self._process.wait()
                self._reaped = True
        else:
            code = self._process.wait()
            with self._reap_lock:
                self._reaped = True
        self.exit_code = code
        with self._wake:
            self._exited.set()
            self._wake.notify_all()
        readers = [thread for thread in self._threads if thread.name in ("claude-driver-read", "claude-driver-stderr")]
        deadline = time.monotonic() + self.DRAIN_SECONDS
        for thread in readers:
            thread.join(max(0.0, deadline - time.monotonic()))
        for thread in readers:                  # still open: something outside the group holds the pipes; stop reading them
            if thread.is_alive():
                thread.join(1.0)
        for stream in (self._process.stdout, self._process.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            self._on_exit(code)
        except Exception:
            pass


class ClaudeSession:
    """One headless Claude Code session in ``folder``. Thread-safe; events go to ``on_event(dict)`` on a private thread, in order.

    ``env`` is added to this process's environment (a value of None removes the variable); ``args`` go right after the
    executable (``--settings``, ``--setting-sources``, ``--model`` ...) and may not contain the flags the driver owns. A new
    session gets ``--session-id`` (so ``session_id`` is known before the first message); ``resume_session_id`` resumes one.

    Events (every dict has ``type``): ``session_started`` {session_id, transcript_path}; ``turn_started`` {message_uuid} (the CLI
    took the message up: its lifecycle ``started``; a UserPromptSubmit hook may still hold or block the prompt afterwards, so it
    does not mean a model turn is running, ``prompt_blocked`` follows for a blocked one);
    ``activity`` {text}; ``permission_request`` {request_id, tool, input, tool_use_id, ...}; ``question`` {request_id,
    questions, tool_use_id}; ``request_cancelled`` {request_id, reason} (the CLI withdrew a request, or its turn ended);
    ``prompt_blocked`` {message_uuid, reason}; ``usage`` {last {totalTokens, inputTokens, cachedInputTokens, outputTokens},
    contextWindow when known} (what the session's context holds after the latest model request, in the Codex driver's names;
    sent each time it has moved by ``USAGE_STEP_TOKENS``, so a long run reports it while it runs); ``turn_finished`` {status
    "completed"|"interrupted"|"error", message_uuids, blocked_message_uuids, ``usage`` (the same, when known), ...};
    ``error`` {message, kind}; ``exited`` {code, unexpected}, always last.

    One ``turn_finished`` covers every message the CLI answered in that run: a message sent while a run is busy is answered in
    the same run. A prompt a hook blocked is reported by ``prompt_blocked`` and, as the CLI says "success" for it, by a
    ``turn_finished`` with status "completed" whose ``message_uuids`` and ``blocked_message_uuids`` both name it (also when the
    run's ``result`` did not list it, see above).

    Process safety (Linux): the CLI is started through a tiny exec shim that sets ``PR_SET_PDEATHSIG`` (SIGKILL), so it dies with
    the process that started it. The kernel ties that signal to the *thread* that spawned the child: call ``start()`` from a
    thread that lives as long as the session (the host's main thread does). ``_ProcessTransport.DIE_WITH_PARENT = False`` turns it off. macOS has
    no equivalent; there the host's ``clean_stale`` (identity-checked) is the only protection against an orphaned CLI.
    """

    transport_class = _ProcessTransport
    CLOSE_GRACE_SECONDS = 5.0
    TERMINATE_GRACE_SECONDS = 3.0
    MAX_MESSAGE_BYTES = 2 * 1024 * 1024
    MAX_QUEUED_EVENTS = 10_000
    MAX_PROBLEM_EVENTS = 20
    RESULT_TEXT_LIMIT = 2000
    COMPLETED_GRACE_SECONDS = 2.0
    USAGE_STEP_TOKENS = 10_000

    def __init__(self, folder, executable, env=None, args=(), resume_session_id=None, on_event=None):
        self._folder = os.fspath(folder)
        self._executable = os.fspath(executable)
        for argument in args:
            if not isinstance(argument, (str, os.PathLike)):
                raise ValueError("args must be strings")
            flag = os.fspath(argument).split("=", 1)[0]
            if flag in _PROTECTED_FLAGS:
                raise ValueError("%s is set by the driver and cannot be passed in args" % flag)
        self._args = [os.fspath(argument) for argument in args]
        if resume_session_id is not None and not _SESSION_ID.match(str(resume_session_id)):
            raise ValueError("resume_session_id must be a session UUID")
        self._resume = str(resume_session_id).lower() if resume_session_id else None
        self._on_event = on_event
        environment = dict(os.environ)
        for key, value in (env or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[str(key)] = str(value)
        self._env = environment
        self._session_id = self._resume or str(uuid.uuid4())

        self._lock = threading.RLock()
        self._state = "new"  # new, running, closing, closed
        self._transport = None
        self._inflight = collections.OrderedDict()  # message uuid -> "queued" | "started" | "completed" (CLI says done, no result yet)
        self._current = None  # uuid of the message the CLI last reported starting
        self._saw_lifecycle = False
        self._blocked = []
        self._dropped = []
        self._pending_requests = {}
        self._interrupt_ids = {}
        self._interrupt_requested = False
        self._still_queued = None
        self._request_counter = 0
        self._announced_session = None
        self._last_activity = None
        self._usage = None            # what the context holds after the latest model request of the session itself
        self._usage_told = 0          # its size when a ``usage`` event last said so
        self._model = self._context_window = None
        self._problem_counts = collections.Counter()
        self._flush_timer = None

        self._events = collections.deque()
        self._events_wake = threading.Condition(threading.Lock())
        self._dispatch_done = False
        self._dispatcher = None
        self._closed = threading.Event()
        self._close_claimed = False
        self._expected_exit = False
        self._exit_code = None

    # -- public ------------------------------------------------------------------------------------------------------
    @property
    def session_id(self):
        """The session id: assigned at start for a new session, the resumed id otherwise; confirmed by ``session_started``."""
        return self._announced_session or self._session_id

    @property
    def transcript_path(self):
        return _transcript_path(self._folder, self.session_id, self._env)

    @property
    def busy(self):
        with self._lock:
            return bool(self._inflight)

    @property
    def pid(self):
        return self._transport.pid if self._transport is not None else None

    @property
    def command(self):
        """The argv this session starts (for diagnostics and tests)."""
        return self._argv()

    def start(self):
        with self._lock:
            if self._state != "new":
                raise ClaudeSessionError("the session was already started")
            if not os.path.isdir(self._folder):
                raise ClaudeSessionError("the agent folder does not exist")
            transport = self.transport_class(
                self._argv(), self._folder, self._env, self._on_message, self._on_problem, self._on_exit)
            self._dispatcher = threading.Thread(target=self._dispatch, name="claude-driver-events", daemon=True)
            self._dispatcher.start()
            try:
                transport.start()
            except OSError as error:
                self._state = "closed"
                self._stop_dispatcher()
                raise ClaudeSessionError("could not start Claude Code: %s" % type(error).__name__) from error
            self._transport = transport
            self._state = "running"

    def send(self, text, message_uuid):
        """Hand one human message to the session; usable at any time, including while a turn runs. ``message_uuid`` is the id
        every later event about it carries."""
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if len(text.encode("utf-8")) > self.MAX_MESSAGE_BYTES:
            raise ValueError("the message is too large")
        try:
            canonical = str(uuid.UUID(str(message_uuid)))
        except ValueError:
            raise ValueError("message_uuid must be a UUID") from None
        record = {"type": "user", "uuid": canonical, "origin": {"kind": "human"},
                  "message": {"role": "user", "content": text}}
        with self._lock:
            if self._state != "running":
                raise ClaudeSessionError("the Claude session is not running")
            if canonical in self._inflight:
                raise ValueError("message_uuid is already in use by an unfinished message")
            self._inflight[canonical] = "queued"
            try:
                self._transport.write(record)
            except ClaudeSessionError:
                del self._inflight[canonical]
                raise

    def interrupt(self):
        """Stop the running turn. Returns False without sending anything when nothing is running (an idle session needs no
        interrupt), True when the request was sent. The turn then ends with ``turn_finished`` status "interrupted"."""
        with self._lock:
            if self._state != "running" or not (self._pending_requests or any(
                    state != "completed" for state in self._inflight.values())):
                return False
            self._interrupt_requested = True
            return self._send_interrupt()

    def answer(self, request_id, decision):
        """Reply to a ``permission_request`` or ``question``. ``decision`` is True/"allow" or False/"deny", or a dict:
        ``{"behavior": "allow"|"deny", "message": ..., "interrupt": bool, "updated_input": {...}, "answers": {...},
        "updated_permissions": [...]}``. ``answers`` (question text to chosen label) answers a ``question`` and is the only thing
        that may stand without ``behavior``, and only for the AskUserQuestion tool: every other request needs an explicit
        allow or deny. Returns False when the request is unknown or no longer pending, True when the reply was sent; raises
        ``ValueError`` (and leaves the request pending) for a decision that is not valid for that request."""
        with self._lock:
            if self._state != "running":
                return False
            pending = self._pending_requests.get(request_id)
            if pending is None:
                return False
            body = self._decision_body(decision, pending)
            try:
                self._transport.write({"type": "control_response", "response": {
                    "subtype": "success", "request_id": request_id, "response": body}})
            except ClaudeSessionError:
                return False
            del self._pending_requests[request_id]
            return True

    def close(self, timeout=None):
        """End the session: interrupt a running turn, end the CLI's input and wait, then terminate and kill it, all within about
        ``CLOSE_GRACE_SECONDS + TERMINATE_GRACE_SECONDS + 3`` seconds (``timeout`` replaces the first). The process group is
        swept, so nothing the CLI started outlives this call. Safe to call twice and from ``on_event``."""
        with self._lock:
            if self._state == "new":
                self._state = "closed"
                self._close_claimed = True
                self._stop_dispatcher()
                self._closed.set()
                return
            if self._transport is None:       # start() failed: nothing was ever running
                self._close_claimed = True
                self._closed.set()
                return
            already = self._close_claimed
            if not already:
                self._close_claimed = True
                if self._state == "running":
                    self._state = "closing"
                    self._expected_exit = True
                    if self._inflight or self._pending_requests:
                        self._interrupt_requested = True
                        self._send_interrupt()
        if already:
            if threading.current_thread() is not self._dispatcher:
                self._closed.wait(self.CLOSE_GRACE_SECONDS + self.TERMINATE_GRACE_SECONDS + 6.0)
            return
        grace = self.CLOSE_GRACE_SECONDS if timeout is None else float(timeout)
        try:
            self._transport.close(grace, self.TERMINATE_GRACE_SECONDS)
            self._transport.join_all(2.0)
        finally:
            self._finish_exit(self._transport.exit_code if self._transport.exited else -9)
            self._closed.set()
        dispatcher = self._dispatcher
        if dispatcher is not None and dispatcher is not threading.current_thread():
            dispatcher.join(5.0)

    def alive_threads(self):
        """Names of this session's threads that are still running (empty once closed); for leak checks."""
        names = [thread.name for thread in (self._transport.alive_threads() if self._transport else [])]
        if self._dispatcher is not None and self._dispatcher.is_alive():
            names.append(self._dispatcher.name)
        return names

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- command line ------------------------------------------------------------------------------------------------
    def _argv(self):
        argv = [self._executable, *self._args, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--permission-prompt-tool", "stdio"]
        argv.append("--resume=" + self._resume if self._resume else "--session-id=" + self._session_id)
        return argv

    # -- events ------------------------------------------------------------------------------------------------------
    def _emit(self, event_type, /, **fields):
        event = {"type": event_type, **fields}
        with self._events_wake:
            if event_type == "activity" and len(self._events) >= self.MAX_QUEUED_EVENTS:
                return  # a stalled consumer loses progress lines, never outcomes
            self._events.append(event)
            self._events_wake.notify()

    def _dispatch(self):
        while True:
            with self._events_wake:
                while not self._events and not self._dispatch_done:
                    self._events_wake.wait()
                if not self._events:
                    return
                event = self._events.popleft()
            callback = self._on_event
            if callback is not None:
                try:
                    callback(event)
                except Exception:
                    pass  # a consumer's bug cannot stop events or the session

    def _stop_dispatcher(self):
        with self._events_wake:
            self._dispatch_done = True
            self._events_wake.notify_all()

    # -- protocol ----------------------------------------------------------------------------------------------------
    def _next_request_id(self, prefix):
        self._request_counter += 1
        return "%s-%d-%s" % (prefix, self._request_counter, uuid.uuid4().hex[:8])

    def _send_interrupt(self):
        request_id = self._next_request_id("interrupt")
        self._interrupt_ids[request_id] = True
        try:
            self._transport.write({"type": "control_request", "request_id": request_id,
                                   "request": {"subtype": "interrupt"}})
        except ClaudeSessionError:
            self._interrupt_ids.pop(request_id, None)
            return False
        return True

    _ALLOW_WORDS = frozenset({"allow", "approve", "yes"})

    def _decision_body(self, decision, pending):
        question = pending.get("tool") == "AskUserQuestion"
        if isinstance(decision, dict):
            fields = decision
        elif decision is True or (isinstance(decision, str) and decision.strip().lower() in self._ALLOW_WORDS):
            fields = {"behavior": "allow"}
        else:
            fields = {"behavior": "deny"}
        answers = fields.get("answers")
        if answers is not None and not isinstance(answers, dict):
            raise ValueError("answers must be an object")
        behavior = fields.get("behavior")
        if behavior is None:
            if answers is None or not question:
                raise ValueError("an explicit allow or deny is required")
            behavior = "allow"
        behavior = str(behavior).strip().lower()
        if behavior not in ("allow", "deny"):
            raise ValueError("behavior must be allow or deny")
        if behavior == "allow":
            updated = fields.get("updated_input")
            updated = dict(updated) if isinstance(updated, dict) else dict(pending["input"])
            if answers is not None and question:
                updated["answers"] = answers
            body = {"behavior": "allow", "updatedInput": updated}
            if isinstance(fields.get("updated_permissions"), list):
                body["updatedPermissions"] = fields["updated_permissions"]
            return body
        body = {"behavior": "deny", "message": str(fields.get("message") or "The user declined this request.")}
        if fields.get("interrupt"):
            body["interrupt"] = True
        return body

    def _on_problem(self, kind, detail):
        if kind == "oversized_line" and isinstance(detail, dict) and detail.get("looks_like_result"):
            # the line that ends a run was too big to read: do not leave its messages waiting forever
            with self._lock:
                self._finish_turn({"subtype": "error_result_too_large", "is_error": True}, forced_error="result record too large")
        with self._lock:
            self._problem_counts[kind] += 1
            count = self._problem_counts[kind]
        if count <= self.MAX_PROBLEM_EVENTS:
            messages = {
                "malformed_line": "Claude Code wrote a line that is not valid JSON; it was skipped.",
                "oversized_line": "Claude Code wrote a record larger than the limit; it was skipped.",
                "write_failed": "Could not write to Claude Code; it is probably exiting.",
                "handler_failed": "An output record could not be processed and was skipped.",
            }
            extra = {}
            if isinstance(detail, dict):
                extra["bytes"] = detail.get("bytes")
            self._emit("error", message=messages.get(kind, "Claude Code driver problem."), kind=kind, **extra)

    def _on_exit(self, code):
        self._finish_exit(code)

    def _finish_exit(self, code):
        with self._lock:
            if self._exit_code is not None:
                return
            self._exit_code = code if code is not None else -1
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            unexpected = not self._expected_exit
            pending_ids = list(self._pending_requests)
            self._pending_requests.clear()
            for request_id in pending_ids:
                self._emit("request_cancelled", request_id=request_id, reason="session_ended")
            inflight = list(self._inflight)
            self._inflight.clear()
            if unexpected:
                tail = scrub_diagnostic(self._transport.stderr_tail()) if self._transport is not None else ""
                self._emit("error", kind="process_exited", message="Claude Code exited unexpectedly (code %s)%s" % (
                    code, ": " + tail if tail else ""))
            if inflight:
                self._emit("turn_finished", status="error" if unexpected else "interrupted",
                           reason="process_exited" if unexpected else "session_closed", message_uuids=inflight,
                           blocked_message_uuids=list(self._blocked), session_id=self.session_id)
            self._blocked = []
            self._state = "closed"
            self._emit("exited", code=self._exit_code, unexpected=unexpected)
            self._stop_dispatcher()

    def _on_message(self, message):
        kind = message.get("type")
        with self._lock:
            if kind == "result":
                self._finish_turn(message)
            elif kind == "assistant":
                self._on_assistant(message)
            elif kind == "system":
                self._on_system(message)
            elif kind == "command_lifecycle":
                self._on_lifecycle(message)
            elif kind == "control_request":
                self._on_control_request(message)
            elif kind == "control_response":
                self._on_control_response(message)
            elif kind == "control_cancel_request":
                request_id = message.get("request_id")
                if self._pending_requests.pop(request_id, None) is not None:
                    self._emit("request_cancelled", request_id=request_id, reason="cancelled_by_claude")

    def _on_system(self, message):
        subtype = message.get("subtype")
        if subtype == "init":
            session_id = message.get("session_id")
            if isinstance(session_id, str) and session_id and session_id != self._announced_session:
                self._announced_session = session_id
                self._emit("session_started", session_id=session_id,
                           transcript_path=_transcript_path(self._folder, session_id, self._env))
            if not self._saw_lifecycle:  # a CLI that does not report message lifecycle: the first init starts the oldest
                for message_uuid, state in self._inflight.items():
                    if state == "queued":
                        self._mark_started(message_uuid)
                    break
        elif subtype == "informational":
            outcome = message.get("prompt_submit_outcome")
            if isinstance(outcome, dict) and outcome.get("kind") == "blocked_by_hook":
                target = self._current if self._current in self._inflight else next(iter(self._inflight), None)
                if target is not None:
                    self._blocked.append(target)
                self._emit("prompt_blocked", message_uuid=target, reason=_bounded(outcome.get("reason") or "", 500))
        elif subtype == "permission_denied":
            self._activity("Permission was denied for " + (_ID_SAFE.sub("", str(message.get("tool_name") or "a tool"))[:40]))
        elif subtype == "api_retry":
            self._activity("Waiting to retry a failed request")

    def _on_lifecycle(self, message):
        message_uuid, state = message.get("command_uuid"), message.get("state")
        self._saw_lifecycle = True
        if message_uuid not in self._inflight:
            return
        if state == "started":
            self._mark_started(message_uuid)
        elif state == "completed":
            # Normally ``result`` comes first and the message is already gone. A ``completed`` that arrives while the message is
            # still ours is a prompt the CLI finished on its own (a steered message a hook blocked while a tool call was held):
            # the run's ``result`` will not list it, so it is finished together with the next ``result`` (or by the timer below).
            if self._inflight.get(message_uuid) != "completed":
                self._inflight[message_uuid] = "completed"
                self._schedule_completed_flush()
        elif state in _DROPPED_STATES:
            del self._inflight[message_uuid]
            self._dropped.append(message_uuid)

    def _schedule_completed_flush(self):
        timer = self._flush_timer
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(self.COMPLETED_GRACE_SECONDS, self._flush_completed)
        timer.name, timer.daemon = "claude-driver-flush", True
        self._flush_timer = timer
        timer.start()

    def _flush_completed(self):
        """No run's ``result`` came for messages the CLI reported completed: finish them, unless a run is going on whose
        ``result`` will."""
        with self._lock:
            if self._state not in ("running", "closing"):
                return
            if not any(state == "completed" for state in self._inflight.values()):
                return
            if any(state == "started" for state in self._inflight.values()) or self._pending_requests:
                return
            self._finish_turn({"subtype": "success", "user_message_uuids": []})

    def _mark_started(self, message_uuid):
        if self._inflight.get(message_uuid) in ("started", "completed"):
            return
        self._inflight[message_uuid] = "started"
        self._current = message_uuid
        self._emit("turn_started", message_uuid=message_uuid)

    def _activity(self, text):
        text = _bounded(text, 100)
        if text and text != self._last_activity:
            self._last_activity = text
            self._emit("activity", text=text)

    def _on_assistant(self, message):
        body = message.get("message")
        blocks = body.get("content") if isinstance(body, dict) else None
        if isinstance(body, dict) and message.get("parent_tool_use_id") is None:
            self._on_usage(body)
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                self._activity(_tool_activity(block.get("name"), block.get("input")))
            elif block.get("type") == "thinking":
                self._activity("Thinking")
            elif block.get("type") == "text" and str(block.get("text") or "").strip():
                self._activity("Writing a reply")

    def _on_usage(self, body):
        """Keep what the latest model request of the session held: all it sent (cached or not) plus what it wrote is what the
        context holds now. Said in a ``usage`` event when it has moved by ``USAGE_STEP_TOKENS`` since the last one."""
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return

        def count(key):
            value = usage.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

        cached, written = count("cache_read_input_tokens"), count("output_tokens")
        sent = count("input_tokens") + count("cache_creation_input_tokens") + cached
        if not sent:
            return                      # a record the CLI wrote itself (an error text): no model request, nothing measured
        self._usage = {"totalTokens": sent + written, "inputTokens": sent, "cachedInputTokens": cached, "outputTokens": written}
        if isinstance(body.get("model"), str):
            self._model = body["model"]
        if abs(self._usage["totalTokens"] - self._usage_told) >= self.USAGE_STEP_TOKENS:
            self._usage_told = self._usage["totalTokens"]
            self._emit("usage", **self._usage_record())

    def _usage_record(self):
        record = {"last": dict(self._usage)}
        if self._context_window:
            record["contextWindow"] = self._context_window
        return record

    def _on_model_usage(self, models):
        """``result.modelUsage``: the limit of the model the session runs on (the largest one when that model is not named)."""
        if not isinstance(models, dict):
            return
        windows = {name: item["contextWindow"] for name, item in models.items()
                   if isinstance(item, dict) and isinstance(item.get("contextWindow"), int) and item["contextWindow"] > 0}
        if windows:
            self._context_window = windows.get(self._model) or max(windows.values())

    def _on_control_request(self, message):
        request_id = message.get("request_id")
        request = message.get("request")
        request = request if isinstance(request, dict) else {}
        subtype = request.get("subtype")
        if not isinstance(request_id, str) or not request_id:
            return
        if subtype != "can_use_tool":
            try:
                self._transport.write({"type": "control_response", "response": {
                    "subtype": "error", "request_id": request_id, "error": "unsupported request"}})
            except ClaudeSessionError:
                pass
            self._emit("error", kind="unsupported_request", message="Claude Code sent a request this driver does not support: "
                       + _ID_SAFE.sub("", str(subtype))[:40])
            return
        tool = str(request.get("tool_name") or "")
        tool_input = request.get("input") if isinstance(request.get("input"), dict) else {}
        self._pending_requests[request_id] = {"tool": tool, "input": tool_input}
        shown, truncated = _bounded_input(tool_input)
        common = {"request_id": request_id, "tool_use_id": _bounded(request.get("tool_use_id") or "", 200) or None}
        if tool == "AskUserQuestion":
            questions = shown.get("questions") if isinstance(shown, dict) else None
            self._emit("question", questions=questions if isinstance(questions, list) else [], input=shown,
                       input_truncated=truncated, **common)
            return
        extra = {key: _bounded(request[key], 500) for key in ("title", "display_name", "description", "decision_reason",
                                                               "agent_id") if isinstance(request.get(key), str)}
        if isinstance(request.get("blocked_path"), str):
            extra["blocked_path"] = "".join(ch if ch.isprintable() else " " for ch in request["blocked_path"])[:4096]
        if isinstance(request.get("permission_suggestions"), list):
            extra["suggestions"] = _bounded_suggestions(request["permission_suggestions"])
        self._emit("permission_request", tool=_bounded(tool, 200), input=shown, input_truncated=truncated, **common, **extra)

    def _on_control_response(self, message):
        response = message.get("response")
        response = response if isinstance(response, dict) else {}
        request_id = response.get("request_id")
        if request_id not in self._interrupt_ids:
            return
        del self._interrupt_ids[request_id]
        if response.get("subtype") == "error":
            self._emit("error", kind="interrupt_failed", message="Claude Code did not accept the interrupt: "
                       + _bounded(response.get("error") or "", 200))
            return
        body = response.get("response")
        queued = body.get("still_queued") if isinstance(body, dict) else None
        if isinstance(queued, list):
            self._still_queued = [item for item in queued if isinstance(item, str)]

    def _finish_turn(self, result, forced_error=None):
        subtype = str(result.get("subtype") or "")
        failed = bool(result.get("is_error")) or subtype != "success"
        covered = result.get("user_message_uuids")
        if not isinstance(covered, list) and isinstance(result.get("user_message_uuid"), str):
            covered = [result["user_message_uuid"]]
        if isinstance(covered, list):
            finished = [item for item in covered if item in self._inflight]
        else:
            finished = [key for key, state in self._inflight.items() if state != "queued"] or list(self._inflight)
        if forced_error:
            finished = [key for key, state in self._inflight.items() if state != "queued"] or list(self._inflight)
        # messages the CLI completed on its own that this ``result`` does not list (a hook-blocked steered message)
        finished += [key for key, state in self._inflight.items() if state == "completed" and key not in finished]
        for key in finished:
            self._inflight.pop(key, None)
        if forced_error or failed:
            status = "interrupted" if self._interrupt_requested and not forced_error else "error"
        else:
            status = "completed"
        text = result.get("result")
        event = {"status": status, "message_uuids": finished, "blocked_message_uuids": [item for item in self._blocked],
                 "session_id": result.get("session_id") or self.session_id, "subtype": subtype or None}
        for key in ("stop_reason", "terminal_reason", "num_turns", "duration_ms"):
            if result.get(key) is not None:
                event[key] = result[key]
        if status == "error":
            event["error"] = forced_error or _bounded(text if isinstance(text, str) and text else subtype or "error", 500)
        elif isinstance(text, str) and not (finished and set(finished) <= set(self._blocked)):
            event["result_text"] = text[: self.RESULT_TEXT_LIMIT]
        self._on_model_usage(result.get("modelUsage"))
        if self._usage:
            event["usage"] = self._usage_record()
        if self._dropped:
            event["dropped_message_uuids"] = list(self._dropped)
        if self._still_queued is not None and status == "interrupted":
            event["still_queued"] = list(self._still_queued)
        for request_id in list(self._pending_requests):
            del self._pending_requests[request_id]
            self._emit("request_cancelled", request_id=request_id, reason="turn_ended")
        self._blocked, self._dropped, self._still_queued = [], [], None
        self._interrupt_requested = False
        if self._flush_timer is not None and not any(state == "completed" for state in self._inflight.values()):
            self._flush_timer.cancel()
            self._flush_timer = None
        self._current = None
        self._last_activity = None
        self._emit("turn_finished", **event)
