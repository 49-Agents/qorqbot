"""Drive one long-lived Codex thread through ``codex app-server`` (stdio, newline-delimited JSON-RPC) in a prepared agent folder.

This is the Codex counterpart of ``claude_driver.ClaudeSession`` and offers the same interface to ``host.py``: the constructor,
``start``, ``send``, ``interrupt``, ``answer``, ``close``, the attributes ``pid``, ``session_id``, ``transcript_path`` and ``busy``,
and the same event vocabulary. Select it with ``launch.json["driver"] = "qorqut.managed_runtime.codex_driver:CodexSession"``.

What the protocol was observed to do (Codex 0.154.0 and again 0.170.0, real binaries, scripted model replies; the account's usage limit
blocked real model turns):

* ``initialize`` (``capabilities.experimentalApi`` true) then the notification ``initialized``; ``thread/start`` answers at once with
  the thread id (a UUIDv7) and the rollout path, the file itself appears with the first turn. ``thread/resume`` by id re-opens a
  thread created by this driver or by the interactive client and appends to the same rollout; a thread another app-server holds
  is refused ("already has an active writer").
* Project-local ``.codex/config.toml`` and ``.codex/hooks.json`` are ignored until the owner trusts the folder in the user's
  ``config.toml``. This driver therefore passes the prepared file's contents as the thread's ``config`` and the prepared hooks as
  launch ``-c hooks.<Event>=...`` overrides (source ``sessionFlags``). A hook only runs when ``hooks.state`` holds its current
  hash, so a short-lived probe server first lists the hashes (``hooks/list``) and the real server gets them as ``-c hooks.state``.
  Hook payloads carry ``session_id`` (the thread id) and ``transcript_path`` (the rollout); the hook process is a child of the
  app-server, which holds the thread-writer lock from ``thread/start`` on.
* ``turn/start`` while a turn runs steers the input into that turn (same turn id comes back); ``turn/steer`` with
  ``expectedTurnId`` does the same explicitly and fails with "no active turn to steer" / "expected active turn id" when the turn is
  gone. Either way the input is answered inside the running turn, which ends with ONE ``turn/completed``; the user message item
  carries the ``clientUserMessageId`` as ``clientId``.
* ``turn/interrupt`` answers ``{}`` and the turn ends with ``turn/completed`` status ``interrupted`` (the ``Interrupt`` hook
  fires, ``Stop`` does not). For a turn that already ended it NEVER answers, so the reply is not waited for.
* A prompt a ``UserPromptSubmit`` hook blocks produces ``hook/completed`` status ``blocked`` and a turn with no items; no
  user message item is written for it.
* With ``approvalPolicy`` ``never`` no command, file or MCP-elicitation request is sent; ``item/tool/requestUserInput`` (needs
  the ``default_mode_request_user_input`` feature or plan mode) is still sent. Other policies send approval requests, which
  this driver answers itself: allow when the prepared configuration is full access (``bypass``), decline otherwise. They are
  never forwarded.
* The app-server exits with code 0 within a fraction of a second when its stdin closes, also in the middle of a turn, and
  everything it started (shell, MCP server, code-mode host) dies with it, also after SIGKILL of the app-server.
* ``thread/resume`` is sent with ``excludeTurns: true``: without it the server answers with the whole history of the thread (and, from
  0.170.0, a ``deprecationNotice`` that says so), which for a long conversation is a very large line.
* ``thread/tokenUsage/updated`` ``{threadId, turnId, tokenUsage{total, last, modelContextWindow}}`` follows every model request of a
  turn (and a compaction, and a resume). ``total`` and ``last`` carry ``totalTokens, inputTokens, cachedInputTokens,
  cacheWriteInputTokens, outputTokens, reasoningOutputTokens``: ``total`` is cumulative over the thread, ``last`` is the most recent
  request (the size of the context it sent plus its output). The driver keeps the newest one and reports it in ``turn_finished.usage``.
* ``/compact`` or ``/model`` typed as text are NOT commands (plain text for the model); ``$role`` typed as text IS expanded to
  the skill. ``thread/compact/start`` and ``thread/settings/update`` are the real commands (``compact`` and ``update_settings``).

Threads: a reader (stdout), a stderr drain, a writer (stdin), a waiter (process exit) and an event dispatcher, exactly as in the
Claude driver (the process code is shared). The reader never calls user code, and ``send``, ``answer`` and ``interrupt`` only queue
a line for the writer, so none of them blocks the caller or waits for the server. ``start`` blocks for the hook-hash probe
(about half a second) when the folder has hooks.

Extra constructor options (keyword only, all optional): ``model``, ``effort`` (also read from ``--model``/``--effort`` in ``args``)
and ``bypass`` (``None`` reads it from the prepared ``.codex/config.toml``: full access means bypass).
"""

from __future__ import annotations

import collections
import json
import os
import re
import threading
import time
import tomllib
import uuid

from .claude_driver import (
    ClaudeSessionError as _TransportError, _ProcessTransport, _bounded, _bounded_input, _clean, said_activity, scrub_diagnostic,
)

__all__ = ["CodexSession", "CodexSessionError"]

_SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_OPTION_VALUE = re.compile(r"^[A-Za-z0-9._:/\[\]-]{1,100}$")
_ID_SAFE = re.compile(r"[^A-Za-z0-9_.:\-]")
_FULL_ACCESS = ":danger-full-access"
_DENIED = "The request was declined by the managed session."
_TURN_COMPLETED_HEAD = re.compile(rb'^\s*\{\s*"method"\s*:\s*"turn/completed"')
# Arguments the driver owns: a caller cannot change the transport or turn the process into something else.
_OWNED_FLAGS = frozenset({"app-server", "daemon", "proxy", "generate-ts", "generate-json-schema", "--listen", "--stdio", "--code-mode-host",
                          "--ws-auth", "--ws-token-file", "--ws-token-sha256", "--ws-shared-secret-file", "--ws-issuer",
                          "--ws-audience", "--ws-max-clock-skew-seconds"})
_APPROVAL_REQUESTS = frozenset({"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                                "item/permissions/requestApproval", "applyPatchApproval", "execCommandApproval"})
_STEER_GONE = ("no active turn", "expected active turn id")
_NO_TURN_ACTIVITY = {"reasoning": "Thinking", "agentMessage": "Writing a reply", "fileChange": "Editing files",
                     "webSearch": "Searching the web", "contextCompaction": "Compacting the conversation", "plan": "Planning",
                     "commandExecution": "Running a command", "dynamicToolCall": "Using a tool", "collabAgentToolCall": "Running a subagent",
                     "imageView": "Looking at an image", "imageGeneration": "Generating an image"}


class CodexSessionError(RuntimeError):
    """The session cannot do what was asked (not started, already closed, backlog full, could not start Codex)."""


def _toml_value(value):
    """``value`` as TOML on one line (for ``-c key=<value>``). Strings are TOML basic strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join("%s = %s" % (_toml_value(str(key)), _toml_value(item)) for key, item in value.items()) + "}"
    raise ValueError("a value that TOML cannot hold")


class _CodexTransport(_ProcessTransport):
    """The shared process transport, with one change: an oversized line reports its first bytes, so the driver can tell that the
    record that ends a turn was lost and finish the turn instead of waiting for it."""

    def _read_stdout(self):
        stream, limit = self._process.stdout, self.MAX_LINE_BYTES
        try:
            while True:
                line = stream.readline(limit + 1)
                if not line:
                    return
                if len(line) > limit and not line.endswith(b"\n"):
                    head, length = bytes(line[:128]), len(line)
                    while True:  # discard the rest of the oversized record without holding it
                        chunk = stream.readline(limit)
                        length += len(chunk)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    self._on_problem("oversized_line", {"bytes": length, "looks_like_result": bool(_TURN_COMPLETED_HEAD.match(head))})
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


def _read_prepared(folder):
    """``(config, hooks)`` of the prepared agent folder: ``.codex/config.toml`` as a dict and the ``hooks`` of
    ``.codex/hooks.json``. A missing or unreadable file gives an empty one (no claim is made about what it would have said)."""
    config, hooks = {}, {}
    try:
        with open(os.path.join(folder, ".codex", "config.toml"), "rb") as handle:
            loaded = tomllib.load(handle)
        config = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(folder, ".codex", "hooks.json"), "rb") as handle:
            loaded = json.loads(handle.read(4 * 1024 * 1024))
        found = loaded.get("hooks") if isinstance(loaded, dict) else None
        hooks = found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        pass
    return config, hooks


def hook_trust_arguments(executable, base_arguments, hook_arguments, folder, env, timeout=25.0):
    """``-c hooks.state=...`` for a launch that carries ``hook_arguments``: a short-lived app-server with the same hooks is asked
    (``hooks/list``) for each session-flag hook's current hash, which is exactly what Codex compares a trusted hash with. Returns
    ``(arguments, problem)``; ``arguments`` is empty and ``problem`` a short text when the probe failed."""
    done, box = threading.Event(), {}

    def on_message(message):
        if message.get("id") == 2:
            box["response"] = message
            done.set()

    transport = _CodexTransport([executable, *base_arguments, *hook_arguments, "app-server", "--listen", "stdio://"], folder, env,
                                on_message, lambda kind, detail: None, lambda code: done.set())
    try:
        transport.start()
        transport.write({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "qorqbot-managed", "title": "Qorqbot", "version": "0"}, "capabilities": {"experimentalApi": True}}})
        transport.write({"method": "initialized"})
        transport.write({"id": 2, "method": "hooks/list", "params": {"cwds": [folder]}})
        done.wait(timeout)
    except (OSError, _TransportError) as error:
        return [], "could not run the hook probe (%s)" % type(error).__name__
    finally:
        transport.close(0.5, 1.0)
        transport.join_all(1.0)
    response = box.get("response")
    result = response.get("result") if isinstance(response, dict) else None
    try:
        entries = result["data"][0]["hooks"]
    except (KeyError, IndexError, TypeError):
        return [], "the hook probe returned no hook list"
    state = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("source") == "sessionFlags" and isinstance(entry.get("key"), str) \
                and isinstance(entry.get("currentHash"), str):
            state[entry["key"]] = {"trusted_hash": entry["currentHash"]}
    if not state:
        return [], "the hook probe found no session hooks"
    return ["-c", "hooks.state=" + _toml_value(state)], None


class CodexSession:
    """One Codex thread, driven through its own ``codex app-server`` child process, in ``folder``. Thread-safe; events go to
    ``on_event(dict)`` on a private thread, in order.

    ``env`` is added to this process's environment (a value of None removes the variable); ``args`` go right after the
    executable (for example ``-c check_for_update_on_startup=false``) and may not contain the flags the driver owns.
    ``--model``, ``--effort`` (and ``--bypass-permissions`` / ``--scoped-permissions``) in ``args`` are read by the driver
    itself and not passed on. ``resume_session_id`` resumes a thread by id; a new thread otherwise.

    Events (every dict has ``type``): ``session_started`` {session_id, transcript_path} (the thread exists); ``turn_started``
    {message_uuid} (the thread took the message up); ``activity`` {text}; ``question`` {request_id, questions, input,
    input_truncated, tool_use_id}; ``request_cancelled`` {request_id, reason}; ``prompt_blocked`` {message_uuid, reason};
    ``turn_finished`` {status "completed"|"interrupted"|"error", message_uuids, blocked_message_uuids, ``usage`` (when Codex reported any:
    ``totalTokens, inputTokens, cachedInputTokens, outputTokens, reasoningOutputTokens`` cumulative for the thread, ``last`` {the same
    five for the latest model request}, ``contextWindow``), ...}; ``error``
    {message, kind}; ``exited`` {code, unexpected}, always last. A ``permission_request`` is never emitted: approval requests are
    answered by the driver (allow when ``bypass``, otherwise decline) and MCP elicitations are declined.

    One ``turn_finished`` covers every message answered in that turn: a message sent while a turn runs is steered into it.
    """

    transport_class = _CodexTransport
    CLOSE_GRACE_SECONDS = 5.0
    TERMINATE_GRACE_SECONDS = 3.0
    MAX_MESSAGE_BYTES = 2 * 1024 * 1024
    MAX_QUEUED_EVENTS = 10_000
    MAX_PROBLEM_EVENTS = 20
    MAX_PENDING_REQUESTS = 16
    MAX_UNANSWERED_INTERRUPTS = 8
    RESULT_TEXT_LIMIT = 2000
    ORPHAN_GRACE_SECONDS = 3.0
    START_TIMEOUT_SECONDS = 60.0

    def __init__(self, folder, executable, env=None, args=(), resume_session_id=None, on_event=None, *, model=None, effort=None,
                 bypass=None):
        self._folder = os.fspath(folder)
        self._executable = os.fspath(executable)
        passed, option = [], {}
        arguments = [os.fspath(argument) if isinstance(argument, (str, os.PathLike)) else None for argument in args]
        if any(argument is None for argument in arguments):
            raise ValueError("args must be strings")
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            flag, equals, inline = argument.partition("=")
            if flag in ("--model", "--effort"):
                if equals:
                    option[flag] = inline
                elif index + 1 < len(arguments):
                    index += 1
                    option[flag] = arguments[index]
                else:
                    raise ValueError("%s needs a value" % flag)
            elif argument in ("--bypass-permissions", "--scoped-permissions"):
                option["bypass"] = argument == "--bypass-permissions"
            elif flag in _OWNED_FLAGS or argument in _OWNED_FLAGS:
                raise ValueError("%s is set by the driver and cannot be passed in args" % flag)
            else:
                passed.append(argument)
            index += 1
        self._args = passed
        model = model if model is not None else option.get("--model")
        effort = effort if effort is not None else option.get("--effort")
        if model is not None and (not isinstance(model, str) or not _OPTION_VALUE.match(model) or model.startswith("-")):
            raise ValueError("model is not a valid model name")
        if effort is not None and effort not in _EFFORTS:
            raise ValueError("effort is not a valid reasoning effort")
        self._model, self._effort = model, effort
        if resume_session_id is not None and not _SESSION_ID.match(str(resume_session_id)):
            raise ValueError("resume_session_id must be a thread UUID")
        self._resume = str(resume_session_id).lower() if resume_session_id else None
        self._on_event = on_event
        environment = dict(os.environ)
        for key, value in (env or {}).items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[str(key)] = str(value)
        for key in ("CODEX_THREAD_ID", "CODEX_TURN_ID"):        # a session started from inside a Codex turn must not inherit it
            environment.pop(key, None)
        self._env = environment
        self._prepared_config, self._prepared_hooks = _read_prepared(self._folder)
        if bypass is None:
            bypass = option.get("bypass")
        if bypass is None:
            bypass = self._prepared_config.get("default_permissions") == _FULL_ACCESS
        self._bypass = bool(bypass)

        self._lock = threading.RLock()
        self._state = "new"                  # new, running, closing, closed
        self._transport = None
        self._thread_id = None
        self._rollout_path = None
        self._ready = False                  # the thread exists: sends go out
        self._announced = False
        self._turn_id = None                 # the turn the server says runs
        self._inflight = collections.OrderedDict()   # message uuid -> queued | sent | accepted | started | blocked
        self._msg_turn = {}                  # message uuid -> turn id its user message item started in
        self._texts = {}                     # message uuid -> text, until the server answered the send
        self._requests = {}                  # request id -> (method, callback)
        self._request_counter = 0
        self._interrupt_ids = collections.OrderedDict()
        self._interrupt_wanted = False
        self._interrupt_requested = False
        self._pending_requests = {}          # request id (text) -> {"rpc_id", "questions": [...]}
        self._blocked = []
        self._dropped = []
        self._last_activity = None
        self._last_text = None
        self._usage = None
        self._problem_counts = collections.Counter()
        self._reported = collections.Counter()
        self._orphan_timer = None
        self._start_timer = None
        self._exit_code = None

        self._events = collections.deque()
        self._events_wake = threading.Condition(threading.Lock())
        self._dispatch_done = False
        self._dispatcher = None
        self._closed = threading.Event()
        self._close_claimed = False
        self._expected_exit = False

    # -- public ------------------------------------------------------------------------------------------------------
    @property
    def session_id(self):
        """The thread id: the resumed id, or the new thread's once ``thread/start`` answered (``None`` before)."""
        return self._thread_id or self._resume

    @property
    def transcript_path(self):
        """The rollout file Codex writes this thread to (the file appears with the first turn), ``None`` before the thread exists."""
        return self._rollout_path

    @property
    def busy(self):
        with self._lock:
            return bool(self._inflight) or self._turn_id is not None

    @property
    def pid(self):
        return self._transport.pid if self._transport is not None else None

    @property
    def bypass(self):
        return self._bypass

    @property
    def command(self):
        """The argv the app-server is started with, without hook trust (for diagnostics and tests)."""
        return self._argv([], [])

    def start(self):
        with self._lock:
            if self._state != "new":
                raise CodexSessionError("the session was already started")
            if not os.path.isdir(self._folder):
                raise CodexSessionError("the agent folder does not exist")
            self._state = "starting"
        hook_arguments, trust_arguments, problem = self._hook_arguments()
        with self._lock:
            if self._state != "starting":
                raise CodexSessionError("the session was closed while it started")
            transport = self.transport_class(self._argv(hook_arguments, trust_arguments), self._folder, self._env, self._on_message,
                                             self._on_problem, self._on_exit)
            self._dispatcher = threading.Thread(target=self._dispatch, name="codex-driver-events", daemon=True)
            self._dispatcher.start()
            try:
                transport.start()
            except OSError as error:
                self._state = "closed"
                self._stop_dispatcher()
                raise CodexSessionError("could not start Codex: %s" % type(error).__name__) from error
            self._transport = transport
            self._state = "running"
            if problem:
                self._emit("error", kind="hooks_not_trusted", message="Codex hooks are not active: " + problem)
            self._start_timer = threading.Timer(self.START_TIMEOUT_SECONDS, self._start_timed_out)
            self._start_timer.name, self._start_timer.daemon = "codex-driver-start", True
            self._start_timer.start()
            self._request("initialize", {"clientInfo": {"name": "qorqbot-managed", "title": "Qorqbot", "version": "0"},
                                         "capabilities": {"experimentalApi": True}}, self._on_initialized)

    def send(self, text, message_uuid):
        """Hand one human message to the thread; usable at any time, including while a turn runs (it is steered into it).
        ``message_uuid`` is the id every later event about it carries."""
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        if len(text.encode("utf-8")) > self.MAX_MESSAGE_BYTES:
            raise ValueError("the message is too large")
        try:
            canonical = str(uuid.UUID(str(message_uuid)))
        except ValueError:
            raise ValueError("message_uuid must be a UUID") from None
        with self._lock:
            if self._state != "running":
                raise CodexSessionError("the Codex session is not running")
            if canonical in self._inflight:
                raise ValueError("message_uuid is already in use by an unfinished message")
            self._inflight[canonical] = "queued"
            self._texts[canonical] = text
            if self._ready:
                try:
                    self._dispatch_send(canonical)
                except _TransportError as error:
                    self._forget(canonical)
                    raise CodexSessionError(str(error)) from error

    def interrupt(self):
        """Stop the running turn. Returns False without sending anything when nothing is running, True when the request was sent
        (or will be, as soon as the server names the turn). The turn then ends with ``turn_finished`` status "interrupted"."""
        with self._lock:
            if self._state != "running" or not (self._turn_id is not None or any(
                    state in ("sent", "accepted") for state in self._inflight.values())):
                return False
            self._interrupt_requested = True
            if self._turn_id is None:
                self._interrupt_wanted = True
                return True
            return self._send_interrupt()

    def answer(self, request_id, decision):
        """Reply to a ``question``. ``decision`` is ``{"answers": {<question text>: <answer>}}`` (``allow``/``deny`` words are
        accepted too: ``deny``/False declines to answer). Returns False when the request is unknown or no longer pending, True
        when the reply was sent; raises ``ValueError`` (and leaves the request pending) for a decision that is not valid for it."""
        with self._lock:
            if self._state != "running":
                return False
            pending = self._pending_requests.get(request_id)
            if pending is None:
                return False
            body = self._answer_body(decision, pending)
            try:
                self._transport.write({"id": pending["rpc_id"], "result": body})
            except _TransportError:
                return False
            del self._pending_requests[request_id]
            return True

    def compact(self):
        """The ``/compact`` command: ``thread/compact/start``. False when the thread is not ready or a turn is running."""
        with self._lock:
            if self._state != "running" or not self._ready or self.busy:
                return False
            self._request("thread/compact/start", {"threadId": self._thread_id}, self._on_command_reply("compact"))
            return True

    def update_settings(self, model=None, effort=None):
        """The ``/model`` command: from the next turn on use ``model`` and/or ``effort`` (``thread/settings/update``)."""
        if model is not None and (not isinstance(model, str) or not _OPTION_VALUE.match(model) or model.startswith("-")):
            raise ValueError("model is not a valid model name")
        if effort is not None and effort not in _EFFORTS:
            raise ValueError("effort is not a valid reasoning effort")
        if model is None and effort is None:
            raise ValueError("nothing to change")
        with self._lock:
            if self._state != "running" or not self._ready:
                return False
            params = {"threadId": self._thread_id}
            if model is not None:
                params["model"] = model
            if effort is not None:
                params["effort"] = effort
            self._request("thread/settings/update", params, self._on_command_reply("settings"))
            self._model = model if model is not None else self._model
            self._effort = effort if effort is not None else self._effort
            return True

    def close(self, timeout=None):
        """End the session: interrupt a running turn, end the app-server's input and wait, then terminate and kill it, all within
        about ``CLOSE_GRACE_SECONDS + TERMINATE_GRACE_SECONDS + 3`` seconds (``timeout`` replaces the first). The process group is
        swept, so nothing the app-server started outlives this call. Safe to call twice and from ``on_event``."""
        with self._lock:
            if self._state in ("new", "starting") and self._transport is None:
                self._state = "closed"
                self._close_claimed = True
                self._stop_dispatcher()
                self._closed.set()
                return
            if self._transport is None:
                self._close_claimed = True
                self._closed.set()
                return
            already = self._close_claimed
            if not already:
                self._close_claimed = True
                if self._state == "running":
                    self._state = "closing"
                    self._expected_exit = True
                    if self._turn_id is not None:
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
        for thread in (self._dispatcher, self._start_timer, self._orphan_timer):
            if thread is not None and thread.is_alive():
                names.append(thread.name)
        return names

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- launch ------------------------------------------------------------------------------------------------------
    def _hook_arguments(self):
        """``(hook -c arguments, trust -c arguments, problem)``. Runs the hash probe, so it is called outside the lock."""
        hooks = {event: groups for event, groups in self._prepared_hooks.items() if isinstance(event, str) and isinstance(groups, list)}
        if not hooks:
            return [], [], None
        arguments = []
        try:
            for event, groups in hooks.items():
                arguments += ["-c", "hooks.%s=%s" % (event, _toml_value(groups))]
        except ValueError:
            return [], [], "the prepared hooks could not be handed to Codex"
        trust, problem = hook_trust_arguments(self._executable, self._args, arguments, self._folder, self._env)
        return arguments, trust, problem

    def _argv(self, hook_arguments, trust_arguments):
        return [self._executable, *self._args, *hook_arguments, *trust_arguments, "app-server", "--listen", "stdio://"]

    def _thread_config(self):
        """The per-thread configuration: the prepared ``.codex/config.toml`` (MCP servers, permission profile, approval policy ...)
        because the folder itself is not trusted, with the model and effort of this session on top."""
        config = {key: value for key, value in self._prepared_config.items() if key != "tui"}
        if self._model is not None:
            config["model"] = self._model
        if self._effort is not None:
            config["model_reasoning_effort"] = self._effort
        return config

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

    # The agent's own words change with every message: at most one such line a second, so a burst of messages is not a burst of events.
    SAID_ACTIVITY_SECONDS = 1.0
    _said_at = float("-inf")

    def _activity(self, text, said=False):
        text = _bounded(text, 100)
        if said:
            if time.monotonic() - self._said_at < self.SAID_ACTIVITY_SECONDS:
                return
            self._said_at = time.monotonic()
        if text and text != self._last_activity:
            self._last_activity = text
            self._emit("activity", text=text)

    def _report(self, kind, message, limit=3):
        """An ``error`` event, at most ``limit`` per kind."""
        self._reported[kind] += 1
        if self._reported[kind] <= limit:
            self._emit("error", kind=kind, message=_bounded(message, 400))

    # -- requests ----------------------------------------------------------------------------------------------------
    def _request(self, method, params, callback=None):
        """Queue one request. Never blocks. Raises ``_TransportError`` when the process is gone or its input is backed up."""
        self._request_counter += 1
        request_id = self._request_counter
        if callback is not None:
            self._requests[request_id] = (method, callback)
        try:
            self._transport.write({"id": request_id, "method": method, "params": params})
        except _TransportError:
            self._requests.pop(request_id, None)
            raise
        return request_id

    def _on_initialized(self, message):
        if "error" in message:
            self._fail_start("initialize", message["error"])
            return
        self._transport.write({"method": "initialized"})
        params = {"cwd": self._folder, "approvalPolicy": "never", "config": self._thread_config()}
        if self._model is not None:
            params["model"] = self._model
        if self._resume:
            params["threadId"] = self._resume
            params["excludeTurns"] = True           # the history is read from the rollout; the reply need not carry it
            self._request("thread/resume", params, self._on_thread)
        else:
            self._request("thread/start", params, self._on_thread)

    def _on_thread(self, message):
        if "error" in message:
            self._fail_start("resume" if self._resume else "start", message["error"])
            return
        result = message.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not _SESSION_ID.match(thread_id) or (self._resume and thread_id.lower() != self._resume):
            self._fail_start("start", {"message": "the thread has no usable id"})
            return
        policy = result.get("approvalPolicy")
        profile = result.get("activePermissionProfile")
        wanted = self._prepared_config.get("default_permissions")
        if policy != "never" or (isinstance(wanted, str) and wanted and (not isinstance(profile, dict) or profile.get("id") != wanted)):
            # What Codex enforces is not what was prepared (approvals could be asked, or the sandbox is wider): do not run.
            self._fail_start("settings", {"message": "Codex is not running with the prepared permissions"})
            return
        self._thread_id = thread_id.lower()
        path = thread.get("path")
        self._rollout_path = path if isinstance(path, str) and path else None
        self._ready = True
        if self._start_timer is not None:
            self._start_timer.cancel()
        if not self._announced:
            self._announced = True
            self._emit("session_started", session_id=self._thread_id, transcript_path=self._rollout_path)
        for message_uuid in [key for key, state in self._inflight.items() if state == "queued"]:
            try:
                self._dispatch_send(message_uuid)
            except _TransportError:
                self._forget(message_uuid)
                break

    def _fail_start(self, stage, error):
        text = error.get("message") if isinstance(error, dict) else None
        self._emit("error", kind="thread_%s_failed" % stage if stage in ("start", "resume") else "start_failed",
                   message="Codex could not %s the thread: %s" % ("resume" if stage == "resume" else "start", scrub_diagnostic(text or "unknown", 300)))
        # The host reports a process that ends without a confirmed session as a failed start (or resume).
        threading.Thread(target=self._close_after_failure, name="codex-driver-fail", daemon=True).start()

    def _close_after_failure(self):
        transport = self._transport
        if transport is not None:
            transport.close(0.5, 1.0)

    def _start_timed_out(self):
        with self._lock:
            if self._ready or self._state != "running":
                return
            self._emit("error", kind="start_timeout", message="Codex did not open the thread in time.")
        self._close_after_failure()

    def _dispatch_send(self, message_uuid):
        """Under the lock, thread ready: hand one queued message to the server (steer into a running turn, otherwise start one)."""
        text = self._texts.get(message_uuid, "")
        items = [{"type": "text", "text": text, "text_elements": []}]
        if self._turn_id is not None:
            self._inflight[message_uuid] = "sent"
            self._request("turn/steer", {"threadId": self._thread_id, "clientUserMessageId": message_uuid, "input": items,
                                         "expectedTurnId": self._turn_id}, self._on_send_reply(message_uuid, steered=True))
        else:
            self._start_message(message_uuid, items)

    def _start_message(self, message_uuid, items):
        params = {"threadId": self._thread_id, "clientUserMessageId": message_uuid, "input": items}
        if self._model is not None:
            params["model"] = self._model
        if self._effort is not None:
            params["effort"] = self._effort
        self._inflight[message_uuid] = "sent"
        self._request("turn/start", params, self._on_send_reply(message_uuid, steered=False))

    def _on_send_reply(self, message_uuid, steered):
        def handler(message):
            if message_uuid not in self._inflight:
                return
            error = message.get("error")
            if error is None:
                result = message.get("result")
                turn = (result.get("turn") if isinstance(result, dict) else None) or {}
                turn_id = turn.get("id") if isinstance(turn, dict) else None
                turn_id = turn_id or (result.get("turnId") if isinstance(result, dict) else None)
                if self._inflight.get(message_uuid) == "sent":
                    self._inflight[message_uuid] = "accepted"
                self._texts.pop(message_uuid, None)
                if isinstance(turn_id, str) and turn_id and not steered and self._turn_id is None:
                    self._turn_id = turn_id
                self._after_accept()
                return
            text = str(error.get("message") if isinstance(error, dict) else error)
            if steered and any(mark in text for mark in _STEER_GONE) and self._turn_id is None:
                try:
                    self._start_message(message_uuid, [{"type": "text", "text": self._texts.get(message_uuid, ""), "text_elements": []}])
                    return
                except _TransportError:
                    pass
            if steered and any(mark in text for mark in _STEER_GONE):
                # the turn the steer aimed at ended meanwhile: the message starts the next one
                try:
                    self._start_message(message_uuid, [{"type": "text", "text": self._texts.get(message_uuid, ""), "text_elements": []}])
                    return
                except _TransportError:
                    pass
            self._forget(message_uuid)
            self._emit("turn_finished", status="error", reason="send_failed", message_uuids=[message_uuid], blocked_message_uuids=[],
                       error=scrub_diagnostic(text, 300), session_id=self.session_id)
        return handler

    def _after_accept(self):
        if self._interrupt_wanted and self._turn_id is not None:
            self._interrupt_wanted = False
            self._send_interrupt()

    def _send_interrupt(self):
        request_id = self._request_counter + 1
        try:
            self._request("turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id}, self._on_interrupt_reply)
        except _TransportError:
            return False
        # The server never answers for a turn that already ended: remember a few, forget the rest.
        self._interrupt_ids[request_id] = time.monotonic()
        while len(self._interrupt_ids) > self.MAX_UNANSWERED_INTERRUPTS:
            old, _ = self._interrupt_ids.popitem(last=False)
            self._requests.pop(old, None)
        return True

    def _on_interrupt_reply(self, message):
        error = message.get("error")
        if error is not None:
            self._emit("error", kind="interrupt_failed", message="Codex did not accept the interrupt: "
                       + _bounded(error.get("message") if isinstance(error, dict) else error, 200))

    def _on_command_reply(self, name):
        def handler(message):
            error = message.get("error")
            if error is not None:
                self._emit("error", kind="%s_failed" % name, message="Codex did not accept the %s command: " % name
                           + _bounded(error.get("message") if isinstance(error, dict) else error, 200))
        return handler

    def _forget(self, message_uuid):
        self._inflight.pop(message_uuid, None)
        self._texts.pop(message_uuid, None)
        self._msg_turn.pop(message_uuid, None)

    # -- protocol ----------------------------------------------------------------------------------------------------
    def _on_problem(self, kind, detail):
        if kind == "oversized_line" and isinstance(detail, dict) and detail.get("looks_like_result"):
            # the record that ends a turn was too big to read: do not leave its messages waiting forever
            with self._lock:
                self._finish_turn(self._turn_id, {"status": "failed"}, forced_error="turn result too large to read")
        with self._lock:
            self._problem_counts[kind] += 1
            count = self._problem_counts[kind]
        if count <= self.MAX_PROBLEM_EVENTS:
            messages = {
                "malformed_line": "Codex wrote a line that is not valid JSON; it was skipped.",
                "oversized_line": "Codex wrote a record larger than the limit; it was skipped.",
                "write_failed": "Could not write to Codex; it is probably exiting.",
                "handler_failed": "An output record could not be processed and was skipped.",
            }
            extra = {"bytes": detail.get("bytes")} if isinstance(detail, dict) else {}
            self._emit("error", message=messages.get(kind, "Codex driver problem."), kind=kind, **extra)

    def _on_exit(self, code):
        self._finish_exit(code)

    def _finish_exit(self, code):
        with self._lock:
            if self._exit_code is not None:
                return
            self._exit_code = code if code is not None else -1
            for timer in (self._start_timer, self._orphan_timer):
                if timer is not None:
                    timer.cancel()
            unexpected = not self._expected_exit
            for request_id in list(self._pending_requests):
                self._emit("request_cancelled", request_id=request_id, reason="session_ended")
            self._pending_requests.clear()
            inflight = list(self._inflight)
            self._inflight.clear()
            self._texts.clear()
            self._turn_id = None
            if unexpected:
                tail = scrub_diagnostic(self._transport.stderr_tail()) if self._transport is not None else ""
                self._emit("error", kind="process_exited", message="Codex exited unexpectedly (code %s)%s" % (
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
        method = message.get("method")
        with self._lock:
            if method is None:
                self._on_response(message)
            elif "id" in message:
                self._on_server_request(message)
            else:
                self._on_notification(method, message.get("params") if isinstance(message.get("params"), dict) else {})

    def _on_response(self, message):
        entry = self._requests.pop(message.get("id"), None) if not isinstance(message.get("id"), (dict, list)) else None
        self._interrupt_ids.pop(message.get("id"), None)
        if entry is not None:
            try:
                entry[1](message)
            except Exception:
                self._on_problem("handler_failed", "response")

    # -- notifications -----------------------------------------------------------------------------------------------
    def _mine(self, params):
        thread = params.get("threadId")
        return thread is None or self._thread_id is None or str(thread).lower() == self._thread_id

    def _on_notification(self, method, params):
        if not self._mine(params):
            return                                  # a subagent's thread or any other thread of this server
        if method == "turn/started":
            turn = params.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                self._turn_id = turn["id"]
                self._after_accept()
        elif method in ("item/started", "item/completed"):
            self._on_item(method, params.get("item"), params.get("turnId"))
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            self._finish_turn(turn.get("id"), turn)
        elif method == "error":
            self._on_error_notification(params)
        elif method == "hook/completed":
            self._on_hook(params.get("run"))
        elif method == "thread/tokenUsage/updated":
            self._on_usage(params.get("tokenUsage"))
        elif method == "serverRequest/resolved":
            rpc_id = params.get("requestId")
            for request_id, pending in list(self._pending_requests.items()):
                if pending["rpc_id"] == rpc_id:
                    del self._pending_requests[request_id]
                    self._emit("request_cancelled", request_id=request_id, reason="resolved_by_codex")
        elif method == "mcpServer/startupStatus/updated":
            if params.get("status") == "failed":
                self._report("mcp_server_failed", "The MCP server %s did not start." % (
                    _ID_SAFE.sub("", str(params.get("name") or "")) or "?"))
        elif method == "thread/status/changed":
            status = params.get("status")
            if isinstance(status, dict) and status.get("type") == "systemError":
                self._report("thread_error", "Codex reports a system error for this thread.")

    def _on_item(self, method, item, turn_id):
        if not isinstance(item, dict):
            return
        kind = item.get("type")
        if kind == "userMessage":
            client = item.get("clientId")
            if method == "item/started" and isinstance(client, str):
                client = client.lower()
                if client in self._inflight and self._inflight[client] != "started":
                    self._inflight[client] = "started"
                    self._msg_turn[client] = turn_id if isinstance(turn_id, str) else self._turn_id
                    self._emit("turn_started", message_uuid=client)
            return
        if kind == "agentMessage":
            if method == "item/completed" and isinstance(item.get("text"), str):
                self._last_text = item["text"][: self.RESULT_TEXT_LIMIT]
                if item["text"].strip():
                    self._activity(said_activity(item["text"]), said=True)     # the agent's latest words, on one line
            if method == "item/started":
                self._activity("Writing a reply")
            return
        if method != "item/started":
            return
        if kind == "mcpToolCall":
            self._activity("Using %s (%s)" % (_ID_SAFE.sub("", str(item.get("tool") or ""))[:40] or "a tool",
                                              _ID_SAFE.sub("", str(item.get("server") or ""))[:40] or "connector"))
        elif kind in _NO_TURN_ACTIVITY:
            self._activity(_NO_TURN_ACTIVITY[kind])

    _USAGE_KEYS = ("totalTokens", "inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")

    def _on_usage(self, usage):
        """``thread/tokenUsage/updated``: keep the newest ``tokenUsage``. ``total`` is cumulative for the thread, ``last`` is the latest
        model request, so ``last.totalTokens`` is what the context holds now; ``modelContextWindow`` is the model's limit."""
        total = usage.get("total") if isinstance(usage, dict) else None
        if isinstance(total, dict):
            record = {key: total[key] for key in self._USAGE_KEYS if isinstance(total.get(key), int)}
            last = usage.get("last")
            if isinstance(last, dict):
                record["last"] = {key: last[key] for key in self._USAGE_KEYS if isinstance(last.get(key), int)}
            window = usage.get("modelContextWindow")
            if isinstance(window, int):
                record["contextWindow"] = window
            self._usage = record

    def _on_error_notification(self, params):
        error = params.get("error") if isinstance(params.get("error"), dict) else {}
        info = error.get("codexErrorInfo")
        name = info if isinstance(info, str) else (next(iter(info)) if isinstance(info, dict) and info else "other")
        retrying = bool(params.get("willRetry"))
        self._reported["codex_error"] += 1
        if self._reported["codex_error"] <= 10:
            self._emit("error", kind="codex_retry" if retrying else "codex_error", error_info=_ID_SAFE.sub("", str(name))[:60],
                       message=scrub_diagnostic(error.get("message") or "Codex reported an error.", 300))

    def _on_hook(self, run):
        if not isinstance(run, dict):
            return
        event, status = run.get("eventName"), run.get("status")
        entries = run.get("entries") if isinstance(run.get("entries"), list) else []
        reason = " ".join(str(entry.get("text") or "") for entry in entries if isinstance(entry, dict))
        if event == "userPromptSubmit" and status == "blocked":
            target = next((key for key, state in self._inflight.items() if state in ("sent", "accepted")), None)
            if target is not None:
                self._inflight[target] = "blocked"
                self._blocked.append(target)
            self._emit("prompt_blocked", message_uuid=target, reason=_bounded(reason, 500))
        elif status == "failed":
            self._report("hook_failed", "A %s hook failed: %s" % (_ID_SAFE.sub("", str(event or "?"))[:30], _bounded(reason, 200)), limit=5)

    def _finish_turn(self, turn_id, turn, forced_error=None):
        """Under the lock: the turn ended (or its end was lost). One ``turn_finished`` for every message it covered."""
        started = [key for key, state in self._inflight.items() if state == "started" and self._msg_turn.get(key) in (turn_id, None)]
        blocked = [key for key in self._blocked if key in self._inflight]
        finished = list(dict.fromkeys([*started, *blocked]))
        for key in finished:
            self._forget(key)
        raw = str(turn.get("status") or "")
        if forced_error:
            status = "error"
        elif raw == "completed":
            status = "completed"
        elif raw == "interrupted":
            status = "interrupted"
        else:
            status = "error"
        event = {"status": status, "message_uuids": finished, "blocked_message_uuids": blocked, "session_id": self.session_id,
                 "turn_id": turn_id if isinstance(turn_id, str) else None, "codex_status": raw or None}
        if status == "error":
            error = turn.get("error") if isinstance(turn.get("error"), dict) else {}
            info = error.get("codexErrorInfo")
            name = info if isinstance(info, str) else (next(iter(info)) if isinstance(info, dict) and info else None)
            event["error"] = forced_error or scrub_diagnostic(error.get("message") or raw or "error", 300)
            if name:
                event["reason"] = _ID_SAFE.sub("", str(name))[:60]
        elif self._last_text is not None and not (finished and set(finished) <= set(blocked)):
            event["result_text"] = self._last_text
        if self._usage:
            event["usage"] = {key: (dict(value) if isinstance(value, dict) else value) for key, value in self._usage.items()}
        if self._dropped:
            event["dropped_message_uuids"] = list(self._dropped)
        for request_id in list(self._pending_requests):
            del self._pending_requests[request_id]
            self._emit("request_cancelled", request_id=request_id, reason="turn_ended")
        self._blocked, self._dropped = [], []
        self._turn_id = None
        self._interrupt_wanted = self._interrupt_requested = False
        self._last_activity = self._last_text = None
        self._emit("turn_finished", **event)
        if any(state in ("accepted", "sent") for state in self._inflight.values()):
            self._schedule_orphan_check()

    def _schedule_orphan_check(self):
        timer = self._orphan_timer
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(self.ORPHAN_GRACE_SECONDS, self._orphans)
        timer.name, timer.daemon = "codex-driver-orphans", True
        self._orphan_timer = timer
        timer.start()

    def _orphans(self):
        """A message the server accepted (a steer into a turn that ended right then) that no turn picked up within the grace
        period: report it as dropped so the sender can send it again, instead of keeping the session busy forever."""
        with self._lock:
            if self._state not in ("running", "closing") or self._turn_id is not None:
                return
            lost = [key for key, state in self._inflight.items() if state == "accepted"]
            if not lost:
                return
            for key in lost:
                self._forget(key)
            self._emit("turn_finished", status="completed", message_uuids=[], blocked_message_uuids=[], dropped_message_uuids=lost,
                       session_id=self.session_id)

    # -- requests from the server -----------------------------------------------------------------------------------------
    def _reply(self, rpc_id, result=None, error=None):
        try:
            self._transport.write({"id": rpc_id, **({"error": error} if error is not None else {"result": result})})
        except _TransportError:
            pass

    def _on_server_request(self, message):
        rpc_id, method = message.get("id"), str(message.get("method"))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if isinstance(rpc_id, (dict, list)) or not self._mine(params):
            self._reply(rpc_id if not isinstance(rpc_id, (dict, list)) else None, error={"code": -32601, "message": "not handled"})
            return
        if method in _APPROVAL_REQUESTS:
            self._reply(rpc_id, self._approval_result(method, params))      # never forwarded
        elif method == "mcpServer/elicitation/request":
            self._reply(rpc_id, {"action": "decline", "content": None, "_meta": None})
        elif method == "item/tool/requestUserInput":
            self._on_question(rpc_id, params)
        else:
            self._reply(rpc_id, error={"code": -32601, "message": "unsupported request"})
            self._report("unsupported_request", "Codex sent a request this driver does not support: " + (_ID_SAFE.sub("", method)[:60]))

    def _approval_result(self, method, params):
        allow = self._bypass
        if method == "item/permissions/requestApproval":
            granted = params.get("permissions") if allow and isinstance(params.get("permissions"), dict) else {}
            return {"permissions": granted, "scope": "turn"}
        if method in ("applyPatchApproval", "execCommandApproval"):
            return {"decision": "approved" if allow else {"denied": {"rejection": _DENIED}}}
        return {"decision": "accept" if allow else "decline"}

    def _on_question(self, rpc_id, params):
        questions = params.get("questions") if isinstance(params.get("questions"), list) else []
        shaped, remembered = [], []
        for item in questions[:20]:
            if not isinstance(item, dict) or not isinstance(item.get("question"), str):
                continue
            options = [{"label": _bounded(option.get("label") or "", 200), "description": _bounded(option.get("description") or "", 400)}
                       for option in (item.get("options") or [])[:50] if isinstance(option, dict)]
            shaped.append({"question": _bounded_text(item["question"], 2000), "header": _bounded(item.get("header") or "", 60),
                           "options": options, "multiSelect": False, "isOther": bool(item.get("isOther")),
                           "isSecret": bool(item.get("isSecret"))})
            remembered.append({"id": str(item.get("id") or ""), "question": shaped[-1]["question"], "header": shaped[-1]["header"],
                               "labels": [option["label"] for option in options]})
        if not shaped or len(self._pending_requests) >= self.MAX_PENDING_REQUESTS:
            self._reply(rpc_id, {"answers": {}})                          # nothing the owner could answer
            return
        request_id = "q-%s" % _ID_SAFE.sub("", str(rpc_id))[:40]
        self._pending_requests[request_id] = {"rpc_id": rpc_id, "questions": remembered}
        shown, truncated = _bounded_input({"questions": shaped})
        self._emit("question", request_id=request_id, questions=shown.get("questions") if isinstance(shown, dict) else [],
                   input=shown, input_truncated=truncated, tool_use_id=_bounded(params.get("itemId") or "", 200) or None)

    def _answer_body(self, decision, pending):
        if decision is False or (isinstance(decision, str) and decision.strip().lower() in ("deny", "no", "decline")):
            return {"answers": {item["id"]: {"answers": []} for item in pending["questions"] if item["id"]}}
        answers = decision.get("answers") if isinstance(decision, dict) else None
        if not isinstance(answers, dict) or not answers:
            raise ValueError("an answer to each question is required")
        body = {}
        for item in pending["questions"]:
            value = next((answers[key] for key in (item["question"], item["id"], item["header"]) if key and key in answers), None)
            if not isinstance(value, str) or not value.strip():
                raise ValueError("every question needs an answer")
            parts = [part.strip() for part in value.split(", ")]
            chosen = parts if len(parts) > 1 and all(part in item["labels"] for part in parts) else [value]
            if item["id"]:
                body[item["id"]] = {"answers": [text[:4000] for text in chosen]}
        return {"answers": body}


def _bounded_text(text, limit):
    text = " ".join(_clean(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
