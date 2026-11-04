"""Bounded local dispatcher for opted-in durable Ordubashy assignments.

The dispatcher runs beside, never inside, the parent conversation. Workers use
the CLI the parent session runs (Codex or Claude Code), each in a separate
registration with its own delegation credential; the normal parent registration
remains the sole inbox consumer and lease owner.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import safe_io
from .client import BootstrapClient, load_registration, path_has_symlink

MAX_WORKERS = 3
POLL_SECONDS = 10
HEARTBEAT_SECONDS = 30
MAX_LINE_BYTES = 1_000_000
MAX_TURN_BYTES = 8_000_000
RUNTIME_EXECUTABLES = {"codex": "codex", "claude-code": "claude"}
RUNTIME_NAMES = {"codex": "Codex", "claude-code": "Claude Code"}
CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CLAUDE_MODEL_PREFIXES = ("claude", "opus", "sonnet", "haiku")
# Process environment a Claude Code worker needs to find its own sign-in; nothing from the parent session.
CLAUDE_ENVIRONMENT = ("HOME", "USER", "LOGNAME", "TMPDIR", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY")


def startup_diagnostic(raw):
    """Persist only recognized failure categories and correlation IDs, never raw secrets."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    kinds = [label for needle, label in (
        ("mcp", "MCP startup"), ("unauthorized", "provider authentication"),
        ("401", "provider authentication"), ("api key", "provider credentials"),
        ("permission denied", "filesystem permission"), ("sandbox", "sandbox"),
        ("connection", "connection")) if needle in text.lower()]
    ids = re.findall(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}", text)[-4:]
    return "; ".join(dict.fromkeys(kinds + (["correlation IDs: " + ", ".join(ids)] if ids else []))) or "no recognized startup diagnostic"


class MissingRuntimeThread(Exception):
    """A resume process exited before reopening its saved worker thread (Codex thread or Claude Code session)."""


def supported_cli_version(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r"codex-cli \d+\.\d+\.\d+", value.strip()) is not None


def _quote(value):
    # JSON basic strings are valid TOML strings and quote path/URL characters.
    return json.dumps(str(value), ensure_ascii=True)


def _inline_table(values):
    return "{" + ",".join(f"{key}={_quote(value)}" for key, value in values.items()) + "}"


def worker_registration(claim, parent_registration, *, state_root=None):
    """Materialize only the new generation's private credential and registration."""
    assignment = claim.get("assignment")
    if not isinstance(assignment, dict):
        raise DomainError("invalid_response", "Dispatcher did not return an assignment")
    try:
        assignment_id = str(uuid.UUID(assignment["id"]))
        parent_id = str(uuid.UUID(parent_registration["agent_id"]))
        registration_id = str(uuid.UUID(claim["worker_registration_id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise DomainError("invalid_response", "Dispatcher identity is invalid") from None
    workspace = Path(claim["worker_workspace"])
    if not workspace.is_absolute() or str(workspace) != str(workspace.resolve(strict=False)):
        raise DomainError("invalid_response", "Worker workspace is not an absolute normalized path")
    if workspace.is_relative_to(Path(parent_registration["workspace"])):
        raise DomainError("invalid_response", "Worker workspace must not inherit parent project configuration")
    if state_root is None:
        state_root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) \
            / "bazarlyk" / "delegated-workers"
    state_root = Path(state_root)
    if not state_root.is_absolute() or str(state_root) != str(state_root.resolve(strict=False)):
        raise DomainError("invalid_response", "Worker state root is not an absolute normalized path")
    expected_workspace = state_root / parent_id / assignment_id
    if workspace != expected_workspace:
        raise DomainError("invalid_response", "Worker workspace escaped the private runtime state root")
    if path_has_symlink(workspace):
        raise DomainError("projection_conflict", "Worker workspace cannot follow symlinks")
    token = claim.get("worker_token")
    if (not isinstance(token, str) or not 32 <= len(token) <= 512
            or any(char.isspace() or ord(char) < 33 for char in token)):
        raise DomainError("invalid_response", "Worker credential is invalid")
    service_url = claim.get("service_url")
    if not isinstance(service_url, str) or service_url != parent_registration["service_url"]:
        raise DomainError("invalid_response", "Worker service does not match its parent registration")

    metadata = workspace / ".bazarlyk"
    safe_io.mkdir(metadata)
    registration_path = metadata / "registration.json"
    credential_path = metadata / "credential"
    registration = {
        "schema_version": 1,
        "registration_id": registration_id,
        "agent_id": parent_id,
        "workspace": str(workspace),
        "service_url": service_url,
        "credential_file": ".bazarlyk/credential",
        "capture_transcripts": False,
        "durable_delegation_enabled": False,
    }
    safe_io.atomic_write(credential_path, (token + "\n").encode(), mode=0o600, enforce_mode=True)
    safe_io.atomic_write(registration_path,
        (json.dumps(registration, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        mode=0o600, enforce_mode=True)
    return registration, registration_path


def worker_command(assignment, registration, registration_path, *, executable="codex", thread_id=None):
    """Build a one-server Codex invocation with inherited user config disabled."""
    server_url = registration["service_url"] + "/mcp"
    header_helper = shlex.join([
        os.sys.executable, "-I", "-m", "bazarlyk.bootstrap.mcp_headers",
        "--registration", str(registration_path),
    ])
    # This child has no project config, Apps, plugins, code mode, browser, web
    # search, native subagents, parent MCP entry, or writable shell sandbox.
    feature_override = "features={apps=false,plugins=false,code_mode=false,multi_agent=false,browser_use=false,shell_tool=false}"
    command = [executable]
    if thread_id is None:
        command.extend(["exec", "--json", "--ignore-user-config", "--skip-git-repo-check",
            "--sandbox", "read-only", "--cd", registration["workspace"]])
    else:
        if not isinstance(thread_id, str) or str(uuid.UUID(thread_id)) != thread_id:
            raise DomainError("invalid_response", "Saved Codex thread ID is invalid")
        command.extend(["exec", "resume", "--json", thread_id, "--ignore-user-config",
            "--skip-git-repo-check"])
    # The saved plan has already authorized this exact MCP operation set.
    # Approval here does not bypass server-side binding, scope, or signoff checks.
    from bazarlyk.agents.operational_delegation import WORKER_OPERATIONS
    allowed_tools = sorted(set(assignment.get("allowed_operations_json", [])) | set(WORKER_OPERATIONS))
    command.extend([
        "--model", assignment["selected_model"],
        "-c", f'model_reasoning_effort={_quote(assignment["reasoning_effort"])}',
        "-c", 'sandbox_mode="read-only"',
        "-c", 'web_search="disabled"',
        "-c", feature_override,
        "-c", f'mcp_servers.bazarlyk={{url={_quote(server_url)},http_headers_helper={_quote(header_helper)},required=true,default_tools_approval_mode="approve",enabled_tools={json.dumps(allowed_tools)}}}',
        "-c", 'default_permissions="qorqut-delegated-worker"',
        "-c", "permissions.qorqut-delegated-worker={filesystem={\"/\"=\"deny\","
            f"{_quote(registration['workspace'])}=\"read\","
            f"{_quote(str(Path(registration['workspace']) / '.bazarlyk' / 'credential'))}=\"deny\","
            f"{_quote(str(registration_path))}=\"deny\"}},network={{enabled=false}}}}",
        "-",
    ])
    return command


def claude_worker_command(assignment, registration, registration_path, *, executable="claude", thread_id=None):
    """Build a headless Claude Code invocation with one MCP server, no built-in tools and no user configuration.

    ``--setting-sources project,local`` skips user settings, hooks and memory; the private worker folder has no
    project settings. ``--tools ""`` removes every built-in tool, ``--strict-mcp-config`` loads only the Bazarlyk
    server, and ``dontAsk`` denies any tool not named in ``--allowedTools`` instead of prompting."""
    header_helper = shlex.join([
        os.sys.executable, "-I", "-m", "bazarlyk.bootstrap.mcp_headers",
        "--registration", str(registration_path),
    ])
    servers = {"mcpServers": {"bazarlyk": {"type": "http", "url": registration["service_url"] + "/mcp",
                                           "headersHelper": header_helper}}}
    from bazarlyk.agents.operational_delegation import WORKER_OPERATIONS
    from .tool_permissions import PREFIX
    allowed_tools = sorted(set(assignment.get("allowed_operations_json", [])) | set(WORKER_OPERATIONS))
    command = [executable, "-p", "--output-format", "stream-json", "--verbose", "--input-format", "text",
               "--setting-sources", "project,local", "--strict-mcp-config",
               "--mcp-config", json.dumps(servers, separators=(",", ":")),
               "--tools", "", "--permission-mode", "dontAsk",
               "--allowedTools", *[PREFIX + name for name in allowed_tools]]
    if thread_id is not None:
        try:
            valid = isinstance(thread_id, str) and str(uuid.UUID(thread_id)) == thread_id
        except ValueError:
            valid = False
        if not valid:
            raise DomainError("invalid_response", "Saved Claude Code session ID is invalid")
        command.extend(["--resume", thread_id])
    # The worker model is a Codex model unless the owner chose a Claude one; otherwise Claude Code uses its own default.
    model = assignment.get("selected_model")
    if isinstance(model, str) and model.lower().startswith(CLAUDE_MODEL_PREFIXES):
        command.extend(["--model", model])
    if assignment.get("reasoning_effort") in CLAUDE_EFFORTS:
        command.extend(["--effort", assignment["reasoning_effort"]])
    return command


def interpret_event(runtime, event):
    """Normalize one JSONL event: ("thread", id), ("completed", usage), ("failed", None) or (None, None)."""
    kind = event.get("type")
    if runtime == "codex":
        if kind == "thread.started":
            return "thread", event.get("thread_id")
        if kind == "turn.completed":
            raw = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            return "completed", {key: value for key, value in raw.items() if key in {
                "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}}
        if kind in {"turn.failed", "error"}:
            return "failed", None
        return None, None
    if kind == "system" and event.get("subtype") == "init":
        return "thread", event.get("session_id")
    if kind == "result":
        if event.get("is_error") or event.get("subtype") != "success":
            return "failed", None
        return "completed", claude_usage(event.get("usage"))
    return None, None


def claude_usage(raw):
    """Claude reports uncached, cache-read and cache-write input separately; the ledger counts all input and its cached part."""
    raw = raw if isinstance(raw, dict) else {}
    count = lambda name: raw.get(name) if type(raw.get(name)) is int and raw.get(name) >= 0 else 0
    cached = count("cache_read_input_tokens")
    details = raw.get("output_tokens_details") if isinstance(raw.get("output_tokens_details"), dict) else {}
    thinking = details.get("thinking_tokens") if type(details.get("thinking_tokens")) is int and details.get("thinking_tokens") >= 0 else 0
    return {"input_tokens": count("input_tokens") + cached + count("cache_creation_input_tokens"),
            "cached_input_tokens": cached, "output_tokens": count("output_tokens"), "reasoning_output_tokens": thinking}


def _prompt(assignment, events, *, continuation=False):
    scope = {
        "assignment_id": assignment["id"], "business_id": assignment.get("business_id"),
        "scope": assignment.get("scope_json"), "objective": assignment.get("objective"),
        "approved_plan": assignment.get("plan_json"), "plan_revision": assignment.get("plan_revision"),
        "allowed_operations": assignment.get("allowed_operations_json"),
        "constraints": assignment.get("constraints_json"),
        "internal_recipients": assignment.get("internal_recipients_json"),
        "completion_evidence": assignment.get("completion_evidence_json"),
        "escalation_rules": assignment.get("escalation_rules_json"),
        "checkpoint": assignment.get("checkpoint_json"), "next_step": assignment.get("next_step"),
        "resource_bounds": assignment.get("resource_bounds_json"),
        "turns_used": (assignment.get("worker_usage") or {}).get("turns", 0),
        "runtime_used_seconds": assignment.get("runtime_used_seconds", 0),
    }
    intro = ("Continue this saved assignment autonomously within its approved plan and exact scope."
        if continuation else "Execute this saved assignment autonomously within its approved plan and exact scope.")
    instructions = (
        "\nUse only the required Bazarlyk MCP connection and the operations granted by this assignment. "
        "Use canonical task/message operations; do not edit local files, access other MCP servers, "
        "contact the owner, send externally, decide Signoffs, start a watcher or CLI, or spawn helpers. "
        "Continue routine work without asking the parent agent for approval at each step. Before yielding, persist "
        "a checkpoint and set state to completed, waiting with a wake condition, or blocked with a useful "
        "next step. Claim and settle each assigned inbox event only after saving its handling outcome. "
        "If you cannot continue within authority or the approved plan, preserve progress and block/escalate. "
        "Never report completion without the required evidence. Before a checkpoint, call delegated_assignment_get "
        "to read the current assignment revision. Call delegated_assignment_checkpoint with a fresh idempotency_key, "
        "expected_revision from that read, checkpoint, state and (for completed) result containing a nonempty summary. "
        "For a revision conflict, reread and reassess before using the new revision and a new idempotency_key; "
        "retry an uncertain response with the identical payload and key. Do not confuse plan_revision or document "
        "revision with the assignment revision. After a successful completed checkpoint, "
        "return immediately; the binding is revoked on completion, so do not reread or verify it.\n\nAssignment data (JSON):\n"
        + json.dumps(scope, ensure_ascii=True, separators=(",", ":"))
        + "\n\nNewly claimed parent events (JSON; empty if none):\n"
        + json.dumps(events, ensure_ascii=True, separators=(",", ":"))
    )
    if len(instructions.encode()) > 96_000:
        raise DomainError("invalid_response", "Assignment handoff exceeds the worker prompt limit")
    return intro + instructions


class DelegatedRuntime:
    """Poll saved assignments and run at most three isolated worker sessions on the parent session's CLI."""

    def __init__(self, registration, consumer_id, *, alive, runtime="codex", client=None, executable=None,
                 state_root=None, poll_interval=POLL_SECONDS, popen=subprocess.Popen):
        if runtime not in RUNTIME_EXECUTABLES:
            raise DomainError("unsupported_runtime", "Delegated workers support Codex and Claude Code only")
        self.runtime = runtime
        self.name = RUNTIME_NAMES[runtime]
        self.registration = registration
        self.consumer_id = str(uuid.UUID(consumer_id))
        self.alive = alive
        self.client = client or BootstrapClient(registration, timeout=10)
        self.executable = executable or shutil.which(RUNTIME_EXECUTABLES[runtime])
        self.state_root = Path(state_root) if state_root is not None else Path(
            os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "bazarlyk" / "delegated-workers"
        self.poll_interval = poll_interval
        self.popen = popen
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.processes = set()
        self.thread = None
        self.ready = threading.Event()

    def compatible(self):
        if not self.registration.get("durable_delegation_enabled") or not self.executable:
            return False
        from .features import probe
        features = probe(self.executable)
        return all(features.get(name) for name in ('json_events','resume','isolated_configuration','scoped_mcp'))

    def command(self, assignment, registration, registration_path, thread_id):
        build = worker_command if self.runtime == "codex" else claude_worker_command
        return build(assignment, registration, registration_path, executable=self.executable, thread_id=thread_id)

    def environment(self, cwd):
        env = {"PATH": os.environ.get("PATH", os.defpath), "TERM": "dumb", "LANG": os.environ.get("LANG", "C.UTF-8")}
        if self.runtime == "codex":
            env["HOME"] = cwd
            env["CODEX_HOME"] = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        else:
            # Claude Code keeps its sign-in and resumable sessions under the real home; settings and memory are skipped
            # by the command line, not by hiding the home folder.
            env.update({name: os.environ[name] for name in CLAUDE_ENVIRONMENT if name in os.environ})
            env.setdefault("HOME", str(Path.home()))
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
            if name in os.environ:
                env[name] = os.environ[name]
        return env

    def start(self):
        if not self.compatible():
            return False
        self.thread = threading.Thread(target=self.run, name="qorqut-delegated-dispatch", daemon=True)
        self.thread.start()
        self.ready.wait(timeout=12)
        return True

    def run(self):
        from concurrent.futures import ThreadPoolExecutor

        futures = set()
        body = {"registration_id": self.registration["registration_id"], "consumer_id": self.consumer_id}
        with ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="qorqut-worker") as pool:
            try:
                while not self.stop_event.is_set() and self.alive():
                    futures = {future for future in futures if not future.done()}
                    if len(futures) < MAX_WORKERS:
                        try:
                            claim = self.client.post("/api/agent/delegation/claim", body)
                        except (DomainError, OSError):
                            claim = None
                        if isinstance(claim, dict) and isinstance(claim.get("assignment"), dict):
                            futures.add(pool.submit(self._execute, claim))
                    self.ready.set()
                    self.stop_event.wait(self.poll_interval)
            finally:
                self.ready.set()
                self.stop_event.set()
                self._terminate_processes()
                for future in futures:
                    try:
                        future.result(timeout=3)
                    except Exception:
                        pass
                pool.shutdown(wait=False, cancel_futures=True)

    def stop(self):
        self.stop_event.set()
        self._terminate_processes()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)

    def _terminate_processes(self):
        with self.lock:
            processes = tuple(self.processes)
        for process in processes:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    pass
        deadline = time.monotonic() + 2
        for process in processes:
            remaining = max(0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    @staticmethod
    def _stop_process(process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def _post(self, route, assignment, generation, **optional):
        body = {"registration_id": self.registration["registration_id"],
            "consumer_id": self.consumer_id, "assignment_id": assignment["id"],
            "generation": generation}
        body.update({key: value for key, value in optional.items() if value is not None})
        return self.client.post("/api/agent/delegation/" + route, body)

    def _execute(self, claim):
        assignment = claim["assignment"]
        assignment_id = assignment.get("id")
        generation = claim.get("generation")
        worker_files = None
        try:
            if type(generation) is not int or generation < 1 or assignment.get("state") != "running":
                raise DomainError("invalid_response", "Dispatcher returned an invalid worker lease")
            registration, registration_path = worker_registration(claim, self.registration,
                state_root=self.state_root)
            worker_files = (registration_path, Path(registration["workspace"]) / registration["credential_file"])
            self._run_assignment(assignment, claim, registration, registration_path)
        except DomainError as error:
            self._failed(assignment_id, generation, "Worker runtime failed (" + error.code + "): " + (error.message if error.code == "worker_turn_failed" else startup_diagnostic(error.message)))
        except Exception:
            self._failed(assignment_id, generation, self.name + " worker exited before saving a durable checkpoint")
        finally:
            if worker_files is not None:
                for path in worker_files:
                    try:
                        safe_io.unlink(path, missing_ok=True)
                    except (OSError, DomainError):
                        pass

    def _failed(self, assignment_id, generation, reason):
        if not self.alive() or self.stop_event.is_set() or not isinstance(assignment_id, str):
            return
        try:
            self._post("fail", {"id": assignment_id}, generation, reason=reason)
        except (DomainError, OSError):
            pass

    def _run_assignment(self, assignment, claim, registration, registration_path):
        assignment_id, generation = assignment["id"], claim["generation"]
        turns = (assignment.get("worker_usage") or {}).get("turns", 0)
        maximum_turns = assignment["resource_bounds_json"]["max_turns"]
        maximum_runtime = assignment["resource_bounds_json"]["max_runtime_seconds"]
        runtime_used = assignment.get("runtime_used_seconds", 0)
        deadline = time.monotonic() + max(0, maximum_runtime - runtime_used)
        thread_id = assignment.get("runtime_thread_id")
        events = claim.get("events", [])
        seen_events = {event.get("id") for event in events if isinstance(event, dict)}
        continuation = thread_id is not None
        recreations = 0
        while turns < maximum_turns and not self.stop_event.is_set() and self.alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = self._post("poll", assignment, generation)
                if status.get("assignment", {}).get("state") == "running":
                    self._failed(assignment_id, generation, "Assignment runtime limit reached")
                return
            prompt = _prompt(assignment, events, continuation=continuation)
            command = self.command(assignment, registration, registration_path, thread_id)
            try:
                thread_id, _usage, status, new_events = self._run_turn(command, prompt, assignment,
                    generation, deadline, registration["workspace"], thread_id, turns + 1)
            except MissingRuntimeThread:
                if thread_id is None or recreations >= 1:
                    raise
                status = self._post("poll", assignment, generation, reset_runtime_thread=True)
                assignment = status.get("assignment") or assignment
                if assignment.get("state") != "running":
                    return
                thread_id = None
                continuation = False
                recreations += 1
                continue
            assignment = status.get("assignment") or assignment
            if assignment.get("state") != "running":
                return
            if thread_id is None:
                raise DomainError("invalid_response", self.name + " did not report a resumable worker thread")
            for event in new_events:
                if not isinstance(event, dict) or event.get("id") in seen_events:
                    continue
                seen_events.add(event.get("id"))
                events.append(event)
            turns = (assignment.get("worker_usage") or {}).get("turns", turns + 1)
            if assignment.get("state") != "running":
                return
            continuation = True
            events = list(events)
        if self.alive() and not self.stop_event.is_set():
            status = self._post("poll", assignment, generation)
            if status.get("assignment", {}).get("state") == "running":
                self._failed(assignment_id, generation, "Assignment worker-turn limit reached")

    def _run_turn(self, command, prompt, assignment, generation, deadline, cwd, prior_thread_id, turn_number):
        env = self.environment(cwd)
        process = self.popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        diagnostic_tail = bytearray()
        def drain_errors():
            while True:
                chunk = process.stderr.read(4096)
                if not chunk:
                    return
                diagnostic_tail.extend(chunk)
                del diagnostic_tail[:-16384]
        error_reader = threading.Thread(target=drain_errors, daemon=True)
        error_reader.start()
        selector = None
        with self.lock:
            self.processes.add(process)
        try:
            process.stdin.write(prompt.encode())
            process.stdin.close()
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            buffer = bytearray()
            total = 0
            thread_id = prior_thread_id
            status = {"assignment": assignment, "events": []}
            usage = None
            completed = False
            failed = None
            thread_started = False
            service_stopped = False
            heartbeat = time.monotonic() + HEARTBEAT_SECONDS
            eof = False
            while not eof:
                if self.stop_event.is_set() or not self.alive():
                    raise InterruptedError
                if time.monotonic() >= deadline:
                    raise TimeoutError
                if time.monotonic() >= heartbeat:
                    status = self._post("poll", assignment, generation)
                    assignment = status.get("assignment", assignment)
                    if assignment.get("state") != "running":
                        self._stop_process(process)
                        service_stopped = True
                        break
                    heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                readable = selector.select(timeout=min(1, max(0.05, deadline - time.monotonic())))
                if not readable:
                    if process.poll() is not None:
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            eof = True
                            break
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    eof = True
                    break
                total += len(chunk)
                if total > MAX_TURN_BYTES:
                    raise DomainError("invalid_response", self.name + " output exceeded its bounded size")
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, tail = buffer.partition(b"\n")
                    buffer[:] = tail
                    if len(line) > MAX_LINE_BYTES:
                        raise DomainError("invalid_response", self.name + " emitted an oversized JSONL event")
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        raise DomainError("invalid_response", self.name + " emitted invalid JSONL") from None
                    if not isinstance(event, dict):
                        continue
                    kind, value = interpret_event(self.runtime, event)
                    if kind == "thread":
                        reported = value
                        try:
                            valid = isinstance(reported, str) and str(uuid.UUID(reported)) == reported
                        except ValueError:
                            valid = False
                        if not valid:
                            raise DomainError("invalid_response", self.name + " emitted an invalid thread ID")
                        if thread_id is not None and thread_id != reported:
                            raise DomainError("runtime_conflict", self.name + " resumed a different worker thread")
                        thread_id = reported
                        thread_started = True
                        status = self._post("poll", assignment, generation, runtime_thread_id=thread_id)
                        assignment = status.get("assignment", assignment)
                        if assignment.get("state") != "running":
                            self._stop_process(process)
                            service_stopped = True
                            break
                    elif kind == "completed":
                        usage = value
                        status = self._post("poll", assignment, generation,
                            runtime_thread_id=thread_id, turn_number=turn_number, turn_usage=usage)
                        assignment = status.get("assignment", assignment)
                        completed = True
                        if assignment.get("state") != "running":
                            self._stop_process(process)
                            service_stopped = True
                            break
                    elif kind == "failed":
                        # Claude Code reports a resume of a missing session as a failed result before any init event:
                        # that is a missing thread, decided after the process exits. Codex keeps its immediate failure.
                        if self.runtime == "claude-code" and prior_thread_id is not None and not thread_started:
                            failed = event
                            continue
                        raise DomainError("worker_turn_failed", self.name + " worker turn failed: " + startup_diagnostic(json.dumps(event)))
                if service_stopped:
                    break
            selector.close()
            return_code = process.wait(timeout=2)
            error_reader.join(timeout=1)
            if service_stopped:
                return thread_id, usage, status, status.get("events", [])
            if (return_code != 0 or failed is not None) and prior_thread_id is not None and not thread_started:
                raise MissingRuntimeThread
            if return_code != 0 or not completed:
                raise DomainError("worker_turn_failed", self.name + " worker did not complete the turn: " + startup_diagnostic(diagnostic_tail))
            return thread_id, usage, status, status.get("events", [])
        finally:
            if selector is not None:
                selector.close()
            with self.lock:
                self.processes.discard(process)
            for stream in (process.stdin, process.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except OSError:
                        pass

            error_reader.join(timeout=1)
            if not error_reader.is_alive():
                process.stderr.close()
