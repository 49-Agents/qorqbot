"""The headless Claude Code driver, against a stand-in CLI.

The stand-in is a small Python program (written below) that speaks the stream-json records captured from real Claude Code
2.1.269 and 2.1.327: ``command_lifecycle`` queued/started/completed keyed by ``command_uuid``, one ``system`` / ``init`` per
run, ``assistant`` records with ``tool_use`` blocks, ``control_request`` / ``can_use_tool`` for permissions and questions,
``control_response`` for the interrupt (``still_queued``), the ``system`` / ``informational`` record with
``prompt_submit_outcome.kind == "blocked_by_hook"`` for a hook-blocked prompt, and ONE ``result`` record per run listing every
message it answered in ``user_message_uuids``. What the message text asks for picks the behaviour (SLOW, PERM, ASK, BLOCK,
CRASH, HANG, ...). Nothing here runs the real ``claude``.
"""
import isolated_cli_environment

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from qorqut.managed_runtime import ClaudeSession, ClaudeSessionError
from qorqut.managed_runtime import claude_driver

FAKE_CLI = r'''#!__PYTHON__
import json, os, queue, signal, subprocess, sys, threading, time, uuid

LOG = os.environ["FAKE_LOG"]
NO_LIFECYCLE = bool(os.environ.get("FAKE_NO_LIFECYCLE"))
args = sys.argv[1:]
session = next((a.split("=", 1)[1] for a in args if a.startswith(("--session-id=", "--resume="))), str(uuid.uuid4()))


def log(**record):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(record) + "\n")


def out(record):
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


log(kind="start", argv=args, cwd=os.getcwd(), marker=os.environ.get("FAKE_MARKER"), has_removed=("FAKE_REMOVED" in os.environ))
inbox = queue.Queue()


def read_stdin():
    for line in sys.stdin:
        try:
            inbox.put(json.loads(line))
        except ValueError:
            log(kind="bad_input", line=line[:80])
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()
pending, responses = [], {}
state = {"eof": False, "interrupted": False, "running": False}


def lifecycle(uuid_, name):
    if not NO_LIFECYCLE:
        out({"type": "command_lifecycle", "command_uuid": uuid_, "state": name, "uuid": str(uuid.uuid4()), "session_id": session})


def text_of(message):
    content = message["message"]["content"]
    return content if isinstance(content, str) else " ".join(block.get("text", "") for block in content)


def take(timeout):
    """Process one incoming record (or time out). Returns False once stdin is closed and drained."""
    try:
        record = inbox.get(timeout=timeout)
    except queue.Empty:
        return True
    if record is None:
        state["eof"] = True
        return False
    log(kind="in", record=record)
    kind = record.get("type")
    if kind == "user":
        lifecycle(record["uuid"], "queued")
        pending.append(record)
    elif kind == "control_request":
        sub = record["request"]["subtype"]
        if sub == "interrupt":
            out({"type": "control_response", "response": {"subtype": "success", "request_id": record["request_id"],
                                                          "response": {"still_queued": [m["uuid"] for m in pending]}}})
            if state["running"]:
                state["interrupted"] = True
        else:
            out({"type": "control_response", "response": {"subtype": "error", "request_id": record["request_id"], "error": "unsupported"}})
    elif kind == "control_response":
        responses[record["response"]["request_id"]] = record["response"]["response"]
    return True


def pause(seconds):
    """Wait ``seconds`` while still reading input; stop early on interrupt."""
    end = time.time() + seconds
    while time.time() < end and not state["interrupted"]:
        take(0.02)


def init():
    out({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": session, "tools": ["Bash", "Write"], "mcp_servers": [],
         "model": "claude-haiku-5-5", "permissionMode": "default", "claude_code_version": "2.1.327", "uuid": str(uuid.uuid4())})


def assistant(*blocks):
    out({"type": "assistant", "message": {"model": "claude-haiku-5-5", "id": "msg_" + uuid.uuid4().hex[:10], "type": "message", "role": "assistant",
                                          "content": list(blocks), "stop_reason": None}, "parent_tool_use_id": None, "session_id": session, "uuid": str(uuid.uuid4())})


def tool_result(text):
    out({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": text}]},
         "parent_tool_use_id": None, "session_id": session, "uuid": str(uuid.uuid4())})


def ask_permission(name, tool_input, request_id=None, suggestions=(), extra=None):
    request_id = request_id or "perm-" + uuid.uuid4().hex[:6]
    out({"type": "control_request", "request_id": request_id, "request": {
        "subtype": "can_use_tool", "tool_name": name, "input": tool_input, "tool_use_id": "toolu_1",
        "permission_suggestions": list(suggestions), "title": "Claude wants to use " + name, "display_name": name, **(extra or {})}})
    while request_id not in responses and not state["interrupted"] and not state["eof"]:
        take(0.02)
    if state["interrupted"] and request_id not in responses:
        out({"type": "control_cancel_request", "request_id": request_id})
        return None
    return responses[request_id]


def serve(message, started):
    """Answer one message according to what it asks for. Returns "interrupted" when the run was interrupted."""
    text = text_of(message)
    lifecycle(message["uuid"], "started")
    started.append(message["uuid"])
    init()
    if "BLOCK" in text:
        out({"type": "system", "subtype": "informational", "content": "UserPromptSubmit operation blocked by hook:\nUPS_BLOCKED_BY_HOOK\n\nOriginal prompt: " + text,
             "level": "warning", "prevent_continuation": True,
             "prompt_submit_outcome": {"kind": "blocked_by_hook", "prompt_kept": False, "reason": "UPS_BLOCKED_BY_HOOK"}, "uuid": str(uuid.uuid4())})
        return "blocked"
    assistant({"type": "thinking", "thinking": "", "signature": "x"})
    if "GRANDCHILD" in text:
        child = subprocess.Popen(["sleep", "300"])
        with open(os.environ["FAKE_PIDFILE"], "w") as handle:
            handle.write(str(child.pid))
    if "CRASH" in text:
        sys.stderr.write("ANTHROPIC_API_KEY=sk-ant-api03-SECRETSECRET123 failed to reach the host\n")
        sys.stderr.flush()
        assistant({"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "true"}})
        os._exit(3)
    if "HANG" in text or "STOPPED" in text:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(["sleep", "300"])
        with open(os.environ["FAKE_PIDFILE"], "w") as handle:
            handle.write(str(child.pid))
        log(kind="hung")
        if "STOPPED" in text:
            os.kill(os.getpid(), signal.SIGSTOP)  # neither reads input nor answers a signal until killed
        while True:
            time.sleep(1)
    if "STDERRFLOOD" in text:
        for _ in range(300):
            sys.stderr.write("x" * 4000 + "\n")
            sys.stderr.flush()
    if "FLOOD" in text:
        for index in range(3000):
            assistant({"type": "text", "text": "padding %d " % index + "y" * 400})
        log(kind="flood_done")
    if "JUNK" in text:
        sys.stdout.write("this is not json\n[1, 2]\n\n")
        sys.stdout.flush()
    if "BIGLINE" in text:
        sys.stdout.write("z" * 300000 + "\n")
        sys.stdout.flush()
    if "TOOLS" in text:
        assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "curl -H 'Authorization: Bearer SECRETVALUE99' https://x.example"}},
                  {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/home/owner/private/notes-for-bob.md"}},
                  {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "ls", "description": "Listing the project folder"}},
                  {"type": "tool_use", "id": "t4", "name": "mcp__echo__ping", "input": {"token": "SECRETVALUE99"}},
                  {"type": "tool_use", "id": "t5", "name": "WebFetch", "input": {"url": "https://docs.example.com/a?key=SECRETVALUE99"}})
    if "SLOW" in text:
        seconds = float(text.split("SLOW:")[1].split()[0]) if "SLOW:" in text else 2.0
        assistant({"type": "tool_use", "id": "toolu_1", "name": "mcp__echo__slow", "input": {}})
        pause(seconds)
        if state["interrupted"]:
            tool_result("The user doesn't want to proceed with this tool use. The tool use was rejected.")
            out({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]},
                 "parent_tool_use_id": None, "session_id": session, "uuid": str(uuid.uuid4())})
            return "interrupted"
        tool_result("done-slow")
    if "QUIETKID" in text:
        child = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.environ["FAKE_PIDFILE"], "w") as handle:
            handle.write(str(child.pid))
        time.sleep(0.2)
        os._exit(5)   # a helper that holds none of the pipes stays behind in the CLI's process group
    if "PERM" in text:
        tool_input = {"file_path": "/tmp/hello.txt", "content": "hi" if "BIGINPUT" not in text else "q" * (30000 if "HUGEINPUT" in text else 5000)}
        assistant({"type": "tool_use", "id": "toolu_1", "name": "Write", "input": tool_input})
        suggestions = [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "s" * 100000}], "behavior": "allow",
                        "destination": "localSettings"}] * 40 if "BIGSUGGEST" in text else []
        decision = ask_permission("Write", tool_input, suggestions=suggestions, extra={
            "decision_reason": "Path is outside the working directory", "blocked_path": "/tmp/hello.txt"})
        if decision is None:
            return "interrupted"
        tool_result("created" if decision["behavior"] == "allow" else decision.get("message", "denied"))
        assistant({"type": "text", "text": ("ALLOWED " + json.dumps(decision.get("updatedInput", {}))[:60]) if decision["behavior"] == "allow" else "DENIED"})
    if "ASK" in text:
        questions = {"questions": [{"question": "Which colour?", "header": "Colour", "options": [{"label": "Red", "description": "r"}, {"label": "Blue", "description": "b"}], "multiSelect": False}]}
        assistant({"type": "tool_use", "id": "toolu_1", "name": "AskUserQuestion", "input": questions})
        decision = ask_permission("AskUserQuestion", questions)
        if decision is None:
            return "interrupted"
        assistant({"type": "text", "text": "ANSWERS " + json.dumps(decision.get("updatedInput", {}).get("answers"))})
    if "BIGRESULT" in text:
        out({"subtype": "success", "is_error": False, "result": "r" * 400000, "session_id": session, "user_message_uuids": [message["uuid"]],
             "num_turns": 1, "stop_reason": "end_turn", "type": "result", "duration_ms": 5, "uuid": str(uuid.uuid4())})
        return "bigresult"
    assistant({"type": "text", "text": "OK " + text[:40]})
    return "done"


def run():
    started, outcome = [], "done"
    state["running"], state["interrupted"] = True, False
    t0 = time.time()
    outcomes = []
    while pending and outcome != "interrupted":
        outcome = serve(pending.pop(0), started)
        outcomes.append(outcome)
        take(0.0)
    if outcome == "blocked" and any(item != "blocked" for item in outcomes):
        outcome = "done"
    state["running"] = False
    base = {"type": "result", "duration_ms": int((time.time() - t0) * 1000), "session_id": session, "total_cost_usd": 0.001,
            "uuid": str(uuid.uuid4()), "user_message_uuids": started, "num_turns": 0 if outcome == "blocked" else len(started) + 1,
            "stop_reason": None if outcome == "blocked" else ("tool_use" if outcome == "interrupted" else "end_turn"), "terminal_reason": "completed"}
    if outcome == "interrupted":
        out({**base, "subtype": "error_during_execution", "is_error": False, "result": "", "terminal_reason": "aborted_tools"})
    elif outcome == "blocked":
        out({**base, "subtype": "success", "is_error": False, "result": "UserPromptSubmit operation blocked by hook:\nUPS_BLOCKED_BY_HOOK\n\nOriginal prompt: x"})
    elif outcome == "bigresult":
        pass
    else:
        out({**base, "subtype": "success", "is_error": False, "result": "OK"})
    for item in started:
        lifecycle(item, "completed")
    state["interrupted"] = False


while True:
    if pending:
        run()
    elif state["eof"] and not os.environ.get("FAKE_IGNORE_EOF"):
        break
    else:
        take(0.05)
log(kind="exit")
'''



# The records Claude Code 2.1.327 wrote when a message was steered into a run that was holding a tool call (a Bash permission
# request) and a UserPromptSubmit hook blocked that message (captured with a real host; ids replaced by the ones the test sent).
# The order is the point: the blocked message gets lifecycle started AND completed, then the informational record, all while the
# first message's run is still going; the run's single ``result`` lists only the first message.
REPLAY_CLI = r"""#!__PYTHON__
import json, os, sys, time, uuid
SESSION = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith(("--session-id=", "--resume="))))
MODE = os.environ.get("REPLAY_MODE", "steer")
log = open(os.environ["FAKE_LOG"], "a")

def out(record):
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()

def life(command, state):
    out({"type": "command_lifecycle", "command_uuid": command, "state": state, "uuid": str(uuid.uuid4()), "session_id": SESSION})

first = None
for line in sys.stdin:
    record = json.loads(line)
    log.write(json.dumps({"kind": "in", "record": record}) + chr(10)); log.flush()
    kind = record.get("type")
    if kind == "user" and first is None:
        first = record["uuid"]
        life(first, "queued")
        life(first, "started")
        out({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": SESSION, "tools": ["Bash"], "mcp_servers": [],
             "model": "claude-haiku-5-5", "permissionMode": "default", "claude_code_version": "2.1.327", "uuid": str(uuid.uuid4())})
        if MODE == "alone":                 # a single prompt the hook blocked; the CLI reports it completed and no result follows
            out({"type": "system", "subtype": "informational", "content": "UserPromptSubmit operation blocked by hook", "level": "warning",
                 "prevent_continuation": True, "prompt_submit_outcome": {"kind": "blocked_by_hook", "prompt_kept": False, "reason": "blocked by the test hook"},
                 "uuid": str(uuid.uuid4()), "session_id": SESSION})
            life(first, "completed")
            continue
        out({"type": "assistant", "message": {"model": "claude-haiku-5-5", "id": "msg_1", "type": "message", "role": "assistant",
             "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "touch perm-test.txt",
                          "description": "Create perm-test.txt file in working directory"}, "caller": {"type": "direct"}}],
             "stop_reason": None}, "parent_tool_use_id": None, "session_id": SESSION, "uuid": str(uuid.uuid4())})
        out({"type": "control_request", "request_id": "56c67916-e8ea-4982-aac7-611297b97ef7", "request": {
            "subtype": "can_use_tool", "tool_name": "Bash", "display_name": "Bash",
            "input": {"command": "touch perm-test.txt", "description": "Create perm-test.txt file in working directory"},
            "description": "Create perm-test.txt file in working directory",
            "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "touch perm-test.txt"}],
                                        "behavior": "allow", "destination": "localSettings"},
                                       {"type": "setMode", "mode": "acceptEdits", "destination": "session"}],
            "blocked_path": "/work/perm-test.txt", "tool_use_id": "toolu_1"}})
    elif kind == "user":
        second = record["uuid"]
        life(second, "queued")                      # held until the tool call finishes
    elif kind == "control_response":
        out({"type": "user", "message": {"role": "user", "content": [{"tool_use_id": "toolu_1", "type": "tool_result",
             "content": "(Bash completed with no output)", "is_error": False}]}, "parent_tool_use_id": None, "session_id": SESSION,
             "uuid": str(uuid.uuid4())})
        life(second, "started")
        life(second, "completed")                   # completed BEFORE the informational record and the run's result
        out({"type": "system", "subtype": "informational", "content": "UserPromptSubmit operation blocked by hook:\nblocked by the test hook",
             "level": "warning", "prevent_continuation": True,
             "prompt_submit_outcome": {"kind": "blocked_by_hook", "prompt_kept": False, "reason": "blocked by the test hook"},
             "uuid": str(uuid.uuid4()), "session_id": SESSION})
        out({"type": "assistant", "message": {"model": "claude-haiku-5-5", "id": "msg_2", "type": "message", "role": "assistant",
             "content": [{"type": "text", "text": "done"}], "stop_reason": None}, "parent_tool_use_id": None, "session_id": SESSION,
             "uuid": str(uuid.uuid4())})
        out({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "done", "stop_reason": "end_turn",
             "terminal_reason": "completed", "session_id": SESSION, "user_message_uuid": first, "user_message_uuids": [first],
             "duration_ms": 3613, "uuid": str(uuid.uuid4())})
        life(first, "completed")                    # after the result, as always
    elif kind == "control_request":
        out({"type": "control_response", "response": {"subtype": "success", "request_id": record["request_id"], "response": {"still_queued": []}}})
log.write(json.dumps({"kind": "exit"}) + chr(10)); log.flush()
"""


def pid_state(pid):
    """Process state letter (``Z`` zombie, ``S`` ...) or None when the process does not exist. Linux reads /proc; elsewhere (macOS)
    ``ps`` says the same."""
    if sys.platform == "linux":
        try:
            with open("/proc/%d/stat" % pid) as handle:
                return handle.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            return None
    try:
        done = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip()[:1] or None


def pid_alive(pid):
    if sys.platform == "linux":
        try:
            with open("/proc/%d/stat" % pid) as handle:
                return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return False
        except OSError:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
    state = pid_state(pid)
    return state is not None and state != "Z"


class Recorder:
    """Collects events from ``on_event`` and lets a test wait for one."""

    def __init__(self):
        self.events = []
        self.condition = threading.Condition()
        self.hold = None

    def __call__(self, event):
        if self.hold is not None:
            self.hold.wait(30)
        with self.condition:
            self.events.append(event)
            self.condition.notify_all()

    def types(self):
        with self.condition:
            return [event["type"] for event in self.events]

    def of(self, kind):
        with self.condition:
            return [event for event in self.events if event["type"] == kind]

    def wait(self, kind, timeout=10, count=1, where=None):
        end = time.time() + timeout
        with self.condition:
            while True:
                found = [event for event in self.events if event["type"] == kind and (where is None or where(event))]
                if len(found) >= count:
                    return found[count - 1]
                left = end - time.time()
                if left <= 0:
                    raise AssertionError("no %r event within %ss; saw %s" % (kind, timeout, [event["type"] for event in self.events]))
                self.condition.wait(left)


class DriverCase(unittest.TestCase):
    def setUp(self):
        self.isolated = isolated_cli_environment.isolate(self)
        self.root = Path(tempfile.mkdtemp(prefix="claude driver "))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.folder = self.root / "agent folder"
        self.folder.mkdir()
        self.cli = self.root / "fake-claude"
        self.cli.write_text(FAKE_CLI.replace("__PYTHON__", sys.executable))
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "fake.log"
        self.pidfile = self.root / "child.pid"
        self.sessions = []
        self.addCleanup(self.close_all)
        self.before_threads = {thread.name for thread in threading.enumerate() if thread.name.startswith("claude-driver")}

    def close_all(self):
        for session in self.sessions:
            session.CLOSE_GRACE_SECONDS, session.TERMINATE_GRACE_SECONDS = 0.5, 0.5
            session.close()
        if self.pidfile.exists():
            try:
                os.kill(int(self.pidfile.read_text()), 9)
            except (OSError, ValueError):
                pass

    def make(self, recorder=None, env=None, args=(), resume=None, **extra):
        recorder = recorder if recorder is not None else Recorder()
        environment = {"FAKE_LOG": str(self.log), "FAKE_PIDFILE": str(self.pidfile), "FAKE_MARKER": "marker-1", **(env or {})}
        session = ClaudeSession(self.folder, self.cli, env=environment, args=args, resume_session_id=resume, on_event=recorder)
        for name, value in extra.items():
            setattr(session, name, value)
        self.sessions.append(session)
        return session, recorder

    def make_replay(self, mode="steer", **extra):
        self.cli.write_text(REPLAY_CLI.replace("__PYTHON__", sys.executable))
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        return self.make(env={"REPLAY_MODE": mode}, **extra)

    def log_records(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def inputs(self, kind=None):
        records = [item["record"] for item in self.log_records() if item["kind"] == "in"]
        return [record for record in records if kind is None or record.get("type") == kind]

    def send(self, session, text):
        message_uuid = str(uuid.uuid4())
        session.send(text, message_uuid)
        return message_uuid

    def assertNoDriverThreads(self, session):
        end = time.time() + 3
        while time.time() < end and session.alive_threads():
            time.sleep(0.02)
        self.assertEqual(session.alive_threads(), [])
        leaked = {thread.name for thread in threading.enumerate() if thread.name.startswith("claude-driver")} - self.before_threads
        self.assertEqual(leaked, set())


class CommandLineTests(DriverCase):
    def test_new_session_flags_and_environment(self):
        session, recorder = self.make(args=["--model", "haiku", "--setting-sources", "project,local"], env={"FAKE_REMOVED": None})
        session.start()
        self.send(session, "hello")
        recorder.wait("turn_finished")
        start = self.log_records()[0]
        argv = start["argv"]
        self.assertEqual(argv[:4], ["--model", "haiku", "--setting-sources", "project,local"])
        for pair in (["-p"], ["--input-format", "stream-json"], ["--output-format", "stream-json"], ["--verbose"], ["--permission-prompt-tool", "stdio"]):
            at = argv.index(pair[0])
            self.assertEqual(argv[at:at + len(pair)], pair)
        sessions = [item for item in argv if item.startswith("--session-id=")]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].split("=", 1)[1], session.session_id)
        self.assertFalse([item for item in argv if item.startswith("--resume")])
        self.assertEqual(str(uuid.UUID(session.session_id)), session.session_id)
        self.assertEqual(start["cwd"], os.path.realpath(self.folder))
        self.assertEqual(start["marker"], "marker-1")
        self.assertFalse(start["has_removed"])
        self.assertEqual(session.command, [str(self.cli), *argv])
        session.close()

    def test_resume_uses_equals_form_and_reports_the_same_session(self):
        resumed = str(uuid.uuid4())
        session, recorder = self.make(resume=resumed.upper())
        session.start()
        self.assertEqual(session.session_id, resumed)
        self.send(session, "again")
        started = recorder.wait("session_started")
        self.assertEqual(started["session_id"], resumed)
        argv = self.log_records()[0]["argv"]
        self.assertIn("--resume=" + resumed, argv)
        self.assertFalse([item for item in argv if item.startswith("--session-id")])
        session.close()

    def test_flags_the_driver_owns_and_bad_ids_are_refused(self):
        for flag in (["-p"], ["--print"], ["--output-format", "json"], ["--input-format=text"], ["--resume", "x"], ["--session-id=1"],
                     ["--permission-prompt-tool", "mcp__x"], ["--continue"], ["-c"], ["--resume=abc"]):
            with self.assertRaises(ValueError, msg=flag):
                ClaudeSession(self.folder, self.cli, args=flag)
        for bad in ("not-a-uuid", "--evil", "1234", ""):
            if bad == "":
                continue
            with self.assertRaises(ValueError, msg=bad):
                ClaudeSession(self.folder, self.cli, resume_session_id=bad)
        ClaudeSession(self.folder, self.cli, args=["--model", "haiku", "--settings", ".qorqut/launch.json"])

    def test_start_failures_raise_and_leave_nothing_running(self):
        session = ClaudeSession(self.root / "missing", self.cli)
        with self.assertRaises(ClaudeSessionError):
            session.start()
        session = ClaudeSession(self.folder, self.root / "no-such-claude")
        with self.assertRaises(ClaudeSessionError):
            session.start()
        self.assertNoDriverThreads(session)
        with self.assertRaises(ClaudeSessionError):
            session.send("hi", str(uuid.uuid4()))
        started, _ = self.make()
        started.start()
        with self.assertRaises(ClaudeSessionError):
            started.start()


class SendTests(DriverCase):
    def test_record_shape_events_and_transcript_path(self):
        session, recorder = self.make()
        session.start()
        message_uuid = str(uuid.uuid4())
        session.send("hello there", message_uuid)
        finished = recorder.wait("turn_finished")
        record = self.inputs("user")[0]
        self.assertEqual(record, {"type": "user", "uuid": message_uuid, "origin": {"kind": "human"},
                                  "message": {"role": "user", "content": "hello there"}})
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["message_uuids"], [message_uuid])
        self.assertEqual(finished["blocked_message_uuids"], [])
        self.assertEqual(finished["result_text"], "OK")
        self.assertEqual(finished["subtype"], "success")
        started = recorder.of("turn_started")
        self.assertEqual([event["message_uuid"] for event in started], [message_uuid])
        types = recorder.types()
        self.assertLess(types.index("turn_started"), types.index("turn_finished"))
        begun = recorder.of("session_started")
        self.assertEqual(len(begun), 1)
        self.assertEqual(begun[0]["session_id"], session.session_id)
        expected = Path(self.isolated["CLAUDE_CONFIG_DIR"]) / "projects" / (
            "".join(ch if ch.isalnum() else "-" for ch in os.path.realpath(self.folder))) / (session.session_id + ".jsonl")
        self.assertEqual(begun[0]["transcript_path"], str(expected))
        self.assertEqual(session.transcript_path, str(expected))
        self.assertFalse(session.busy)
        session.close()

    def test_transcript_path_finds_a_file_under_another_project_folder(self):
        session_id = str(uuid.uuid4())
        other = Path(self.isolated["CLAUDE_CONFIG_DIR"]) / "projects" / "some-shortened-name-abc123"
        other.mkdir(parents=True)
        (other / (session_id + ".jsonl")).write_text("{}\n")
        self.assertEqual(claude_driver._transcript_path(self.folder, session_id, self.isolated), str(other / (session_id + ".jsonl")))

    def test_activity_is_short_plain_and_never_the_raw_input(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "TOOLS please")
        recorder.wait("turn_finished")
        texts = [event["text"] for event in recorder.of("activity")]
        self.assertIn("Thinking", texts)
        self.assertIn("Running a command", texts)
        self.assertIn("Reading notes-for-bob.md", texts)
        self.assertIn("Listing the project folder", texts)
        self.assertIn("Using ping (echo)", texts)
        self.assertIn("Fetching docs.example.com", texts)
        joined = " ".join(texts)
        for forbidden in ("SECRETVALUE99", "curl", "/home/owner", "Bearer", "key="):
            self.assertNotIn(forbidden, joined)
        self.assertTrue(all(len(text) <= 100 for text in texts))
        session.close()

    def test_send_arguments_and_state_are_checked(self):
        session, recorder = self.make()
        with self.assertRaises(ClaudeSessionError):
            session.send("too early", str(uuid.uuid4()))
        session.start()
        for bad in ("nope", None, 5, "123"):
            with self.assertRaises(ValueError):
                session.send("x", bad)
        with self.assertRaises(ValueError):
            session.send(b"bytes", str(uuid.uuid4()))
        with self.assertRaises(ValueError):
            session.send("x" * (session.MAX_MESSAGE_BYTES + 1), str(uuid.uuid4()))
        message_uuid = str(uuid.uuid4())
        session.send("SLOW:1.0", message_uuid)
        with self.assertRaises(ValueError):
            session.send("again", message_uuid)
        recorder.wait("turn_finished")
        session.send("reuse is fine once finished", message_uuid)
        recorder.wait("turn_finished", count=2)
        session.close()
        with self.assertRaises(ClaudeSessionError):
            session.send("after close", str(uuid.uuid4()))

    def test_non_ascii_text_round_trips(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "merhaba şüğ \U0001f600   line\nbreak")
        recorder.wait("turn_finished")
        self.assertEqual(self.inputs("user")[0]["message"]["content"], "merhaba şüğ \U0001f600   line\nbreak")
        session.close()

    def test_mid_turn_send_is_answered_in_the_same_run(self):
        session, recorder = self.make()
        session.start()
        first = self.send(session, "SLOW:1.2 first")
        recorder.wait("turn_started")
        self.assertTrue(session.busy)
        second = self.send(session, "second one")
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["message_uuids"], [first, second])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual([event["message_uuid"] for event in recorder.of("turn_started")], [first, second])
        users = self.inputs("user")
        self.assertEqual([record["uuid"] for record in users], [first, second])
        self.assertTrue(all(record["origin"] == {"kind": "human"} for record in users))
        self.assertEqual(len(recorder.of("turn_finished")), 1)
        deadline = time.time() + 2
        while session.busy and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(session.busy)
        session.close()

    def test_without_command_lifecycle_the_first_init_starts_the_oldest_message(self):
        session, recorder = self.make(env={"FAKE_NO_LIFECYCLE": "1"})
        session.start()
        first = self.send(session, "hello")
        recorder.wait("turn_finished")
        self.assertEqual([event["message_uuid"] for event in recorder.of("turn_started")], [first])
        session.close()


class InterruptTests(DriverCase):
    def test_interrupt_on_an_idle_session_returns_at_once_and_sends_nothing(self):
        session, recorder = self.make()
        session.start()
        started = time.monotonic()
        self.assertFalse(session.interrupt())
        self.assertLess(time.monotonic() - started, 0.2)
        self.send(session, "hello")
        recorder.wait("turn_finished")
        deadline = time.time() + 2
        while session.busy and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(session.interrupt())
        self.assertEqual(self.inputs("control_request"), [])
        session.close()

    def test_interrupt_ends_the_turn_and_the_session_keeps_working(self):
        session, recorder = self.make()
        session.start()
        first = self.send(session, "SLOW:30")
        recorder.wait("turn_started")
        time.sleep(0.2)
        started = time.monotonic()
        self.assertTrue(session.interrupt())
        self.assertLess(time.monotonic() - started, 0.5)
        finished = recorder.wait("turn_finished", timeout=5)
        self.assertEqual(finished["status"], "interrupted")
        self.assertEqual(finished["message_uuids"], [first])
        self.assertEqual(finished["subtype"], "error_during_execution")
        self.assertEqual(finished["still_queued"], [])
        requests = self.inputs("control_request")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["request"], {"subtype": "interrupt"})
        self.send(session, "after the interrupt")
        again = recorder.wait("turn_finished", count=2)
        self.assertEqual(again["status"], "completed")
        session.close()

    def test_a_message_queued_behind_an_interrupted_run_still_gets_its_own_turn(self):
        session, recorder = self.make()
        session.start()
        first = self.send(session, "SLOW:30")
        recorder.wait("turn_started")
        second = self.send(session, "queued behind")
        time.sleep(0.2)
        session.interrupt()
        interrupted = recorder.wait("turn_finished")
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["message_uuids"], [first])
        self.assertEqual(interrupted["still_queued"], [second])
        after = recorder.wait("turn_finished", count=2)
        self.assertEqual((after["status"], after["message_uuids"]), ("completed", [second]))
        session.close()

    def test_a_failed_run_that_was_not_interrupted_is_an_error(self):
        session, recorder = self.make()
        session.start()
        message_uuid = str(uuid.uuid4())
        with session._lock:
            session._inflight[message_uuid] = "started"
        session._on_message({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "too many turns",
                             "user_message_uuids": [message_uuid], "session_id": session.session_id})
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["status"], "error")
        self.assertEqual(finished["error"], "too many turns")
        session.close()


class PermissionTests(DriverCase):
    def test_allow_round_trip(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "PERM write a file")
        request = recorder.wait("permission_request")
        self.assertEqual(request["tool"], "Write")
        self.assertEqual(request["input"], {"file_path": "/tmp/hello.txt", "content": "hi"})
        self.assertFalse(request["input_truncated"])
        self.assertEqual(request["tool_use_id"], "toolu_1")
        self.assertTrue(request["request_id"])
        self.assertIn("Writing hello.txt", [event["text"] for event in recorder.of("activity")])
        self.assertTrue(session.busy)
        self.assertTrue(session.answer(request["request_id"], True))
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["message_uuids"]), ("completed", [message_uuid]))
        reply = [item for item in self.inputs("control_response") if item["response"]["request_id"] == request["request_id"]]
        self.assertEqual(len(reply), 1)
        self.assertEqual(reply[0], {"type": "control_response", "response": {
            "subtype": "success", "request_id": request["request_id"],
            "response": {"behavior": "allow", "updatedInput": {"file_path": "/tmp/hello.txt", "content": "hi"}}}})
        self.assertFalse(session.answer(request["request_id"], True))
        session.close()

    def test_deny_round_trip_with_a_message(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "PERM")
        request = recorder.wait("permission_request")
        self.assertTrue(session.answer(request["request_id"], {"behavior": "deny", "message": "Not now."}))
        recorder.wait("turn_finished")
        reply = self.inputs("control_response")[0]["response"]["response"]
        self.assertEqual(reply, {"behavior": "deny", "message": "Not now."})
        session.close()
        session, recorder = self.make()
        session.start()
        self.send(session, "PERM")
        request = recorder.wait("permission_request")
        session.answer(request["request_id"], "deny")
        recorder.wait("turn_finished")
        last = [item for item in self.inputs("control_response")][-1]["response"]["response"]
        self.assertEqual(last["behavior"], "deny")
        self.assertTrue(last["message"])
        session.close()

    def test_the_card_gets_the_whole_input_up_to_a_bound_and_a_flag_when_it_is_cut(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "PERM BIGINPUT")
        request = recorder.wait("permission_request")
        self.assertFalse(request["input_truncated"])                       # 5000 characters: shown in full
        self.assertEqual(request["input"]["content"], "q" * 5000)
        self.assertEqual((request["decision_reason"], request["blocked_path"]), ("Path is outside the working directory", "/tmp/hello.txt"))
        session.answer(request["request_id"], "allow")
        recorder.wait("turn_finished")
        reply = self.inputs("control_response")[0]["response"]["response"]
        self.assertEqual(reply["updatedInput"]["content"], "q" * 5000)
        self.send(session, "PERM BIGINPUT HUGEINPUT")
        huge = recorder.wait("permission_request", count=2)
        self.assertTrue(huge["input_truncated"])
        self.assertLessEqual(len(json.dumps(huge["input"])), claude_driver.INPUT_LIMIT_BYTES)
        session.answer(huge["request_id"], "allow")
        recorder.wait("turn_finished", count=2)
        self.assertEqual(self.inputs("control_response")[1]["response"]["response"]["updatedInput"]["content"], "q" * 30000)   # answered in full
        session.close()

    def test_oversized_permission_suggestions_are_capped_and_the_request_still_arrives(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "PERM BIGSUGGEST")
        request = recorder.wait("permission_request")
        self.assertLessEqual(len(request["suggestions"]), claude_driver.SUGGESTION_LIMIT)
        self.assertLessEqual(len(json.dumps(request["suggestions"])), claude_driver.SUGGESTIONS_TOTAL_BYTES)
        self.assertEqual(request["suggestions"][0], {"type": "addRules", "truncated": True})
        self.assertLess(len(json.dumps(request)), 40 * 1024)
        self.assertTrue(session.answer(request["request_id"], "allow"))
        recorder.wait("turn_finished")
        session.close()

    def test_a_question_is_answered_with_chosen_labels(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "ASK me")
        question = recorder.wait("question")
        self.assertEqual(question["questions"][0]["question"], "Which colour?")
        self.assertEqual([option["label"] for option in question["questions"][0]["options"]], ["Red", "Blue"])
        self.assertEqual(recorder.of("permission_request"), [])
        self.assertTrue(session.answer(question["request_id"], {"answers": {"Which colour?": "Blue"}}))
        recorder.wait("turn_finished")
        reply = self.inputs("control_response")[0]["response"]["response"]
        self.assertEqual(reply["behavior"], "allow")
        self.assertEqual(reply["updatedInput"]["answers"], {"Which colour?": "Blue"})
        self.assertEqual(reply["updatedInput"]["questions"][0]["header"], "Colour")
        session.close()

    def test_answers_without_a_behavior_are_only_for_a_pending_question(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "PERM write")
        request = recorder.wait("permission_request")
        for decision in ({"answers": {"Which?": "A"}}, {"message": "hm"}, {}, {"behavior": "maybe"}, {"behavior": "allow", "answers": 3}):
            with self.assertRaises(ValueError, msg=str(decision)):
                session.answer(request["request_id"], decision)
        self.assertEqual(self.inputs("control_response"), [])              # nothing was sent; the request is still pending
        self.assertTrue(session.answer(request["request_id"], {"behavior": "allow", "answers": {"Which?": "A"}}))
        recorder.wait("turn_finished")
        reply = self.inputs("control_response")[0]["response"]["response"]
        self.assertNotIn("answers", reply["updatedInput"])                  # answers mean something for a question only
        session.close()

    def test_a_question_still_takes_answers_alone_and_an_explicit_deny(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "ASK me")
        question = recorder.wait("question")
        self.assertIn("input_truncated", question)
        with self.assertRaises(ValueError):
            session.answer(question["request_id"], {"answers": "Blue"})
        self.assertTrue(session.answer(question["request_id"], {"behavior": "deny", "message": "no thanks"}))
        recorder.wait("turn_finished")
        self.assertEqual(self.inputs("control_response")[0]["response"]["response"], {"behavior": "deny", "message": "no thanks"})
        session.close()

    def test_an_interrupt_withdraws_a_pending_request(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "PERM")
        request = recorder.wait("permission_request")
        self.assertTrue(session.interrupt())
        cancelled = recorder.wait("request_cancelled")
        self.assertEqual(cancelled["request_id"], request["request_id"])
        self.assertEqual(recorder.wait("turn_finished")["status"], "interrupted")
        self.assertFalse(session.answer(request["request_id"], True))
        self.assertEqual(self.inputs("control_response"), [])
        session.close()

    def test_a_request_that_is_not_supported_gets_an_error_reply(self):
        session, recorder = self.make()
        session.start()
        session._on_message({"type": "control_request", "request_id": "x1", "request": {"subtype": "elicitation", "message": "?"}})
        recorder.wait("error")
        deadline = time.time() + 3
        while not self.inputs("control_response") and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.inputs("control_response")[0]["response"],
                         {"subtype": "error", "request_id": "x1", "error": "unsupported request"})
        session.close()

    def test_answer_before_start_or_after_close_is_false(self):
        session, _ = self.make()
        self.assertFalse(session.answer("nothing", True))
        session.start()
        session.close()
        self.assertFalse(session.answer("nothing", True))


class BlockedPromptTests(DriverCase):
    def test_a_hook_blocked_prompt_is_its_own_outcome(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "BLOCK this one")
        blocked = recorder.wait("prompt_blocked")
        self.assertEqual(blocked["message_uuid"], message_uuid)
        self.assertEqual(blocked["reason"], "UPS_BLOCKED_BY_HOOK")
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["message_uuids"], [message_uuid])
        self.assertEqual(finished["blocked_message_uuids"], [message_uuid])
        self.assertEqual(finished["status"], "completed")
        self.assertNotIn("result_text", finished)
        self.assertEqual(finished["num_turns"], 0)
        types = recorder.types()
        self.assertLess(types.index("prompt_blocked"), types.index("turn_finished"))
        session.close()

    def test_only_the_blocked_message_of_a_busy_run_is_marked(self):
        session, recorder = self.make()
        session.start()
        first = self.send(session, "SLOW:1.0 go")
        recorder.wait("turn_started")
        second = self.send(session, "BLOCK me")
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["message_uuids"], [first, second])
        self.assertEqual(finished["blocked_message_uuids"], [second])
        self.assertEqual(recorder.of("prompt_blocked")[0]["message_uuid"], second)
        self.assertEqual(finished["result_text"], "OK")
        session.close()


class CompletedLifecycleTests(DriverCase):
    """A hook-blocked message steered into a run that holds a tool call (captured with Claude Code 2.1.327)."""

    def run_steered(self):
        session, recorder = self.make_replay()
        session.start()
        first = self.send(session, "Use the Bash tool to run `touch perm-test.txt`")
        request = recorder.wait("permission_request")
        second = self.send(session, "BLOCKME during pending permission")
        self.assertTrue(session.busy)
        self.assertTrue(session.answer(request["request_id"], "allow"))
        return session, recorder, first, second, request

    def test_the_captured_run_finishes_both_messages_and_leaves_nothing_in_flight(self):
        session, recorder, first, second, request = self.run_steered()
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["message_uuids"], [first, second])         # the result listed only the first
        self.assertEqual(finished["blocked_message_uuids"], [second])
        self.assertEqual(recorder.of("prompt_blocked")[0]["message_uuid"], second)
        self.assertEqual(finished["result_text"], "done")
        self.assertEqual(request["blocked_path"], "/work/perm-test.txt")
        self.assertEqual([s["type"] for s in request["suggestions"]], ["addRules", "setMode"])
        self.assertEqual([e["message_uuid"] for e in recorder.of("turn_started")], [first, second])      # "accepted by the CLI"
        deadline = time.time() + 3
        while session.busy and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(session.busy)
        self.assertFalse(session._inflight)
        self.assertFalse(session.interrupt())                                   # idle: nothing is sent
        self.assertFalse(session._interrupt_requested)
        self.assertEqual([r["request"]["subtype"] for r in self.inputs("control_request")], [])
        session.close()
        recorder.wait("exited")
        self.assertEqual(len(recorder.of("turn_finished")), 1)                   # no spurious interrupted/session_closed turn
        self.assertEqual(recorder.of("exited")[0]["unexpected"], False)
        self.assertNoDriverThreads(session)

    def test_the_run_after_a_steered_blocked_message_is_a_normal_one(self):
        session, recorder, first, second, _ = self.run_steered()
        recorder.wait("turn_finished")
        third = self.send(session, "again")
        self.assertTrue(session.busy)
        self.assertEqual(session._inflight[third], "queued")
        session.close()

    def test_a_completed_message_no_result_follows_is_finished_by_the_timer(self):
        session, recorder = self.make_replay("alone", COMPLETED_GRACE_SECONDS=0.3)
        session.start()
        only = self.send(session, "BLOCKME")
        blocked = recorder.wait("prompt_blocked")
        self.assertEqual(blocked["message_uuid"], only)
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["message_uuids"], finished["blocked_message_uuids"]), ("completed", [only], [only]))
        self.assertNotIn("result_text", finished)
        self.assertFalse(session.busy)
        session.close()
        recorder.wait("exited")
        self.assertEqual(len(recorder.of("turn_finished")), 1)

    def test_a_completed_message_is_not_flushed_while_another_run_is_going(self):
        session, recorder = self.make(args=[])
        session.COMPLETED_GRACE_SECONDS = 0.1
        session.start()
        first = self.send(session, "SLOW:1.0 hold")
        recorder.wait("turn_started")
        # a completed lifecycle for a message of a run still going waits for that run's result
        with session._lock:
            session._inflight["11111111-1111-4111-8111-111111111111"] = "queued"
            session._on_lifecycle({"command_uuid": "11111111-1111-4111-8111-111111111111", "state": "completed"})
        time.sleep(0.4)
        self.assertEqual(recorder.of("turn_finished"), [])
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["message_uuids"], [first, "11111111-1111-4111-8111-111111111111"])
        session.close()


class BadOutputTests(DriverCase):
    def test_malformed_lines_are_skipped_and_not_echoed(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "JUNK now")
        self.assertEqual(recorder.wait("turn_finished")["status"], "completed")
        problems = recorder.of("error")
        self.assertEqual({event["kind"] for event in problems}, {"malformed_line"})
        self.assertGreaterEqual(len(problems), 2)
        self.assertNotIn("this is not json", json.dumps(recorder.events))
        session.close()

    def test_an_oversized_line_is_dropped_without_stopping_the_session(self):
        with mock.patch.object(claude_driver._ProcessTransport, "MAX_LINE_BYTES", 100_000):
            session, recorder = self.make()
            session.start()
            self.send(session, "BIGLINE now")
            self.assertEqual(recorder.wait("turn_finished")["status"], "completed")
            problems = [event for event in recorder.of("error") if event["kind"] == "oversized_line"]
            self.assertEqual(len(problems), 1)
            self.assertGreaterEqual(problems[0]["bytes"], 300000)
            self.send(session, "still alive")
            self.assertEqual(recorder.wait("turn_finished", count=2)["status"], "completed")
            session.close()

    def test_an_oversized_result_record_still_ends_the_turn(self):
        with mock.patch.object(claude_driver._ProcessTransport, "MAX_LINE_BYTES", 100_000):
            session, recorder = self.make()
            session.start()
            message_uuid = self.send(session, "BIGRESULT")
            finished = recorder.wait("turn_finished")
            self.assertEqual(finished["status"], "error")
            self.assertEqual(finished["message_uuids"], [message_uuid])
            self.assertIn("too large", finished["error"])
            session.close()

    def test_a_flood_of_malformed_lines_is_reported_a_bounded_number_of_times(self):
        session, recorder = self.make()
        session.start()
        for _ in range(100):
            session._on_problem("malformed_line", {"bytes": 3})
        recorder.wait("error")
        session.close()
        self.assertEqual(len(recorder.of("error")), session.MAX_PROBLEM_EVENTS)

    def test_stderr_is_bounded_and_does_not_block_the_child(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "STDERRFLOOD")
        self.assertEqual(recorder.wait("turn_finished", timeout=15)["status"], "completed")
        self.assertLessEqual(len(session._transport.stderr_tail()), claude_driver._ProcessTransport.STDERR_TAIL_BYTES)
        self.assertGreater(len(session._transport.stderr_tail()), 1000)
        session.close()

    def test_a_slow_consumer_never_makes_the_child_block_on_its_output_pipe(self):
        recorder = Recorder()
        recorder.hold = threading.Event()
        session, _ = self.make(recorder)
        session.start()
        self.send(session, "FLOOD")
        deadline = time.time() + 20
        while time.time() < deadline and "flood_done" not in [item["kind"] for item in self.log_records()]:
            time.sleep(0.05)
        self.assertIn("flood_done", [item["kind"] for item in self.log_records()])  # the child wrote 1.5 MB while on_event was stuck
        recorder.hold.set()
        finished = recorder.wait("turn_finished", timeout=15)
        self.assertEqual(finished["status"], "completed")
        activities = len(recorder.of("activity"))
        self.assertLess(activities, 10)  # identical consecutive lines are collapsed
        session.close()

    def test_a_consumer_that_raises_does_not_stop_events(self):
        seen = []

        def consumer(event):
            seen.append(event["type"])
            raise RuntimeError("consumer bug")

        session = ClaudeSession(self.folder, self.cli, env={"FAKE_LOG": str(self.log)}, on_event=consumer)
        self.sessions.append(session)
        session.start()
        self.send(session, "hello")
        deadline = time.time() + 5
        while "turn_finished" not in seen and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn("turn_finished", seen)
        session.close()
        self.assertIn("exited", seen)

    def test_events_are_dropped_only_for_activity_when_the_consumer_is_far_behind(self):
        recorder = Recorder()
        recorder.hold = threading.Event()
        session, _ = self.make(recorder, MAX_QUEUED_EVENTS=3)
        session.start()
        session._emit("activity", text="a")
        for index in range(10):
            session._emit("activity", text="lost %d" % index)
        session._emit("error", message="kept", kind="x")
        recorder.hold.set()
        session.close()
        self.assertIn("error", recorder.types())
        self.assertLess(len(recorder.of("activity")), 10)


class FailureTests(DriverCase):
    def test_child_crash_mid_turn(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "CRASH now")
        exited = recorder.wait("exited")
        self.assertEqual(exited["code"], 3)
        self.assertTrue(exited["unexpected"])
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["reason"], finished["message_uuids"]), ("error", "process_exited", [message_uuid]))
        problem = recorder.wait("error", where=lambda event: event["kind"] == "process_exited")
        self.assertIn("code 3", problem["message"])
        self.assertNotIn("SECRETSECRET123", json.dumps(recorder.events))
        self.assertIn("[redacted]", problem["message"])
        types = recorder.types()
        self.assertEqual(types[-1], "exited")
        self.assertLess(types.index("turn_finished"), types.index("exited"))
        with self.assertRaises(ClaudeSessionError):
            session.send("anyone there", str(uuid.uuid4()))
        self.assertFalse(session.interrupt())
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(recorder.of("exited")), 1)
        self.assertNoDriverThreads(session)

    def test_unexpected_exit_while_idle_is_reported_once(self):
        session, recorder = self.make()
        session.start()
        os.kill(session.pid, 9)
        exited = recorder.wait("exited")
        self.assertTrue(exited["unexpected"])
        self.assertEqual(recorder.of("turn_finished"), [])
        session.close()
        self.assertEqual(len(recorder.of("exited")), 1)

    def test_close_with_a_hung_child_is_bounded_and_leaves_nothing_behind(self):
        session, recorder = self.make(CLOSE_GRACE_SECONDS=0.4, TERMINATE_GRACE_SECONDS=0.4)
        session.start()
        message_uuid = self.send(session, "HANG forever")
        deadline = time.time() + 5
        while not self.pidfile.exists() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.pidfile.exists())
        grandchild, child = int(self.pidfile.read_text()), session.pid
        self.assertTrue(pid_alive(child) and pid_alive(grandchild))
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 6)
        self.assertFalse(pid_alive(child))
        self.assertFalse(pid_alive(grandchild))
        exited = recorder.wait("exited")
        self.assertFalse(exited["unexpected"])
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["reason"], finished["message_uuids"]), ("interrupted", "session_closed", [message_uuid]))
        self.assertNoDriverThreads(session)
        session.close()

    def test_close_while_a_turn_runs_interrupts_it_and_exits_cleanly(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "SLOW:30")
        recorder.wait("turn_started")
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 4)
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["message_uuids"]), ("interrupted", [message_uuid]))
        exited = recorder.wait("exited")
        self.assertEqual((exited["code"], exited["unexpected"]), (0, False))
        self.assertNoDriverThreads(session)

    def test_close_removes_helpers_the_cli_started(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "GRANDCHILD hi")
        recorder.wait("turn_finished")
        helper = int(self.pidfile.read_text())
        self.assertTrue(pid_alive(helper))
        session.close()
        deadline = time.time() + 3
        while pid_alive(helper) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(helper))
        self.assertFalse(pid_alive(session.pid))
        self.assertNoDriverThreads(session)

    def test_a_helper_holding_the_pipes_after_the_cli_died_is_removed(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "GRANDCHILD then crash CRASH")
        recorder.wait("exited", timeout=15)
        helper = int(self.pidfile.read_text())
        deadline = time.time() + 5
        while pid_alive(helper) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(helper))
        session.close()
        self.assertNoDriverThreads(session)

    def test_a_helper_that_holds_no_pipe_is_removed_as_soon_as_the_cli_died_not_only_at_close(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "QUIETKID go")
        recorder.wait("exited", timeout=15)
        self.assertEqual(recorder.of("exited")[0]["unexpected"], True)       # nobody called close(): the host does not after a crash
        helper = int(self.pidfile.read_text())
        deadline = time.time() + 3
        while pid_alive(helper) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(helper))
        session.close()
        self.assertNoDriverThreads(session)

    def test_nothing_is_signalled_by_number_once_the_cli_has_been_reaped(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "hi")
        recorder.wait("turn_finished")
        pid = session.pid
        session.CLOSE_GRACE_SECONDS = 3.0
        session.close()
        recorder.wait("exited")
        transport = session._transport
        self.assertTrue(transport._reaped)
        with mock.patch.object(claude_driver.os, "killpg") as killpg, mock.patch.object(claude_driver.os, "kill") as kill:
            transport._signal(claude_driver.signal.SIGKILL, group_only=True)
            transport._signal(claude_driver.signal.SIGTERM)
            transport._signal(claude_driver.signal.SIGKILL)
            session.close()
        killpg.assert_not_called()
        kill.assert_not_called()
        self.assertFalse(pid_alive(pid))

    def test_the_group_is_swept_while_the_leader_is_still_unreaped(self):
        calls = []
        real = claude_driver.os.killpg

        def spy(pid, sig):
            calls.append((pid, sig, pid_state(pid)))
            return real(pid, sig)
        session, recorder = self.make()
        session.start()
        self.send(session, "QUIETKID go")
        with mock.patch.object(claude_driver.os, "killpg", spy):
            recorder.wait("exited", timeout=15)
        sweeps = [call for call in calls if call[1] == claude_driver.signal.SIGKILL]
        self.assertTrue(sweeps)
        self.assertEqual({state for _, _, state in sweeps}, {"Z"})          # a zombie still owns the pid: it cannot have been reused
        session.close()

    def test_the_cli_dies_with_the_process_that_started_it(self):
        if sys.platform != "linux":
            self.skipTest("PR_SET_PDEATHSIG is Linux only")
        pid = self.orphaned_cli(die_with_parent=True)
        deadline = time.time() + 5
        while pid_alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(pid), "the CLI outlived its killed parent")

    def test_the_death_signal_can_be_turned_off_and_then_the_cli_does_outlive_its_parent(self):
        if sys.platform != "linux":
            self.skipTest("PR_SET_PDEATHSIG is Linux only")
        pid = self.orphaned_cli(die_with_parent=False)
        self.addCleanup(lambda: os.killpg(pid, 9) if pid_alive(pid) else None)
        time.sleep(1.0)
        self.assertTrue(pid_alive(pid))          # proves the test above would notice a missing death signal

    def orphaned_cli(self, die_with_parent):
        program = (
            "import sys, time\n"
            "from qorqut.managed_runtime import ClaudeSession, claude_driver\n"
            "claude_driver._ProcessTransport.DIE_WITH_PARENT = sys.argv[5] == '1'\n"
            "session = ClaudeSession(sys.argv[2], sys.argv[1], env={'FAKE_LOG': sys.argv[3], 'FAKE_PIDFILE': sys.argv[4] + '.x', 'FAKE_IGNORE_EOF': '1'})\n"
            "session.start()\n"
            "open(sys.argv[4], 'w').write(str(session.pid))\n"
            "time.sleep(600)\n")
        pidfile = self.root / "cli.pid"
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join(os.path.abspath(item) for item in sys.path if item))
        parent = subprocess.Popen([sys.executable, "-c", program, str(self.cli), str(self.folder), str(self.log), str(pidfile),
                                   "1" if die_with_parent else "0"], env=environment)
        self.addCleanup(lambda: (parent.kill(), parent.wait()))
        deadline = time.time() + 10
        while not (pidfile.exists() and pidfile.read_text()) and time.time() < deadline:
            time.sleep(0.05)
        pid = int(pidfile.read_text())
        self.addCleanup(lambda: pid_alive(pid) and os.kill(pid, 9))
        self.assertTrue(pid_alive(pid))
        os.kill(parent.pid, signal.SIGKILL)                          # no close(), no atexit: the parent is simply gone
        parent.wait()
        return pid

    def test_the_cli_is_started_by_an_isolated_shim_that_a_planted_module_cannot_hijack(self):
        marker = self.root / "planted-ran"
        (self.folder / "ctypes.py").write_text("open(%r, 'w').write('x')\n" % str(marker))
        (self.folder / "os.py").write_text("open(%r, 'w').write('x')\n" % str(marker))
        session, recorder = self.make()
        session.start()
        self.send(session, "hi")
        self.assertEqual(recorder.wait("turn_finished")["status"], "completed")
        self.assertFalse(marker.exists())
        self.assertEqual(self.log_records()[0]["argv"][-1], "--session-id=" + session.session_id)    # the CLI got its own argv
        session.close()

    def test_a_missing_cli_is_still_refused_at_start(self):
        session = ClaudeSession(self.folder, self.root / "no-such-claude", env={"FAKE_LOG": str(self.log)})
        with self.assertRaises(ClaudeSessionError):
            session.start()
        session.close()

    def test_send_and_interrupt_never_block_when_the_child_stops_reading(self):
        with mock.patch.object(claude_driver._ProcessTransport, "MAX_PENDING_WRITE_BYTES", 3_000_000):
            session, recorder = self.make(CLOSE_GRACE_SECONDS=0.3, TERMINATE_GRACE_SECONDS=0.3)
            session.start()
            self.send(session, "STOPPED")
            deadline = time.time() + 5
            while "hung" not in [item["kind"] for item in self.log_records()] and time.time() < deadline:
                time.sleep(0.05)
            big = "x" * 1_500_000
            started = time.monotonic()
            refused = 0
            for _ in range(6):
                try:
                    self.send(session, big)
                except ClaudeSessionError:
                    refused += 1
            self.assertGreaterEqual(refused, 1)
            self.assertTrue(session.interrupt() in (True, False))
            self.assertLess(time.monotonic() - started, 2)
            started = time.monotonic()
            session.close()
            self.assertLess(time.monotonic() - started, 6)
            self.assertNoDriverThreads(session)

    def test_close_is_safe_from_on_event_and_twice(self):
        holder = {}
        done = threading.Event()

        def consumer(event):
            if event["type"] == "turn_finished":
                holder["session"].close()
                done.set()

        session = ClaudeSession(self.folder, self.cli, env={"FAKE_LOG": str(self.log)}, on_event=consumer)
        holder["session"] = session
        self.sessions.append(session)
        session.start()
        self.send(session, "hello")
        self.assertTrue(done.wait(10))
        session.close()
        session.close()
        self.assertNoDriverThreads(session)

    def test_close_before_start_and_after_close(self):
        session, recorder = self.make()
        session.close()
        session.close()
        with self.assertRaises(ClaudeSessionError):
            session.start()
        self.assertNoDriverThreads(session)

    def test_context_manager_closes(self):
        session, recorder = self.make()
        with session:
            self.send(session, "hello")
            recorder.wait("turn_finished")
        self.assertFalse(pid_alive(session.pid))
        self.assertEqual(recorder.types()[-1], "exited")


class HelperTests(unittest.TestCase):
    def test_scrub_diagnostic_removes_credentials_and_bounds_length(self):
        raw = b"boot ok\nANTHROPIC_API_KEY=sk-ant-abc123456789 Authorization: Bearer abcdef.ghi " + b"A" * 200 + b" end"
        cleaned = claude_driver.scrub_diagnostic(raw, 300)
        for secret in ("sk-ant-abc123456789", "abcdef.ghi", "A" * 40):
            self.assertNotIn(secret, cleaned)
        self.assertIn("[redacted]", cleaned)
        self.assertNotIn("\n", cleaned)
        self.assertLessEqual(len(claude_driver.scrub_diagnostic("word " * 1000, 120)), 120)

    def test_scrub_diagnostic_covers_environment_style_names_and_url_credentials(self):
        for line, secret in (("ANTHROPIC_API_KEY=sk-ant-short boom", "sk-ant-short"), ("GITHUB_TOKEN=ghp_abc123def x", "ghp_abc123def"),
                             ("DB_PASSWORD=hunter2 y", "hunter2"), ("dial postgres://user:pw0rd@db.internal:5432/app failed", "pw0rd"),
                             ("fetch https://ghp_tokenvalue@github.com/o/r", "ghp_tokenvalue"), ("MY_SECRET: abc", "abc"),
                             ("export AWS_SECRET_ACCESS_KEY='a b c' next", "a b c"), ("api-key = zzz", "zzz")):
            self.assertNotIn(secret, claude_driver.scrub_diagnostic(line), line)
        cleaned = claude_driver.scrub_diagnostic("dial postgres://user:pw0rd@db.internal:5432/app failed")
        self.assertIn("db.internal", cleaned)                                   # the useful part of the line stays
        self.assertEqual(claude_driver.scrub_diagnostic("exit code 3: connection refused"), "exit code 3: connection refused")

    def test_bounded_input_keeps_the_whole_input_when_it_fits_and_flags_a_cut(self):
        small = {"command": "echo " + "x" * 9000, "description": "d"}
        self.assertEqual(claude_driver._bounded_input(small), (small, False))
        big = {"command": "y" * 40000, "other": ["z" * 5000] * 20}
        shown, cut = claude_driver._bounded_input(big)
        self.assertTrue(cut)
        self.assertLessEqual(len(json.dumps(shown)), claude_driver.INPUT_LIMIT_BYTES)
        wide = {"k%d" % index: "v" * 600 for index in range(50)}
        shown, cut = claude_driver._bounded_input(wide)
        self.assertLessEqual(len(json.dumps(shown)), claude_driver.INPUT_LIMIT_BYTES)
        self.assertTrue(cut)

    def test_bounded_suggestions_cap_count_item_and_total(self):
        many = [{"type": "addDirectories", "directories": ["/d%d" % index]} for index in range(40)]
        self.assertEqual(len(claude_driver._bounded_suggestions(many)), claude_driver.SUGGESTION_LIMIT)
        huge = [{"type": "addRules", "rules": ["r" * 50000]}, {"type": "setMode", "mode": "acceptEdits"}]
        self.assertEqual(claude_driver._bounded_suggestions(huge), [{"type": "addRules", "truncated": True},
                                                                    {"type": "setMode", "mode": "acceptEdits"}])
        medium = [{"type": "addRules", "rules": ["m" * 1500]}] * 10
        self.assertLessEqual(len(json.dumps(claude_driver._bounded_suggestions(medium))), claude_driver.SUGGESTIONS_TOTAL_BYTES)
        self.assertEqual(claude_driver._bounded_suggestions([5, "x", None]), [5, "x", None])

    def test_tool_activity_texts(self):
        describe = claude_driver._tool_activity
        self.assertEqual(describe("Bash", {"command": "rm -rf /secret"}), "Running a command")
        self.assertEqual(describe("Edit", {"file_path": "/a/b/c.py", "old_string": "password"}), "Editing c.py")
        self.assertEqual(describe("Glob", {}), "Searching files")
        self.assertEqual(describe("WebSearch", {"query": "my secret"}), "Searching the web")
        self.assertEqual(describe("mcp__qorqut__send_note", {}), "Using send_note (qorqut)")
        self.assertEqual(describe("Weird Tool!", {}), "Using WeirdTool")
        self.assertLessEqual(len(describe("Bash", {"description": "d" * 500})), 80)

    def test_shorten_input_cuts_strings_depth_and_width(self):
        value = {"a": "x" * 5000, "b": list(range(100)), "c": {"d": {"e": {"f": {"g": {"h": {"i": 1}}}}}}}
        short, cut = claude_driver._shorten_input(value)
        self.assertTrue(cut)
        self.assertLess(len(short["a"]), 2100)
        self.assertEqual(len(short["b"]), 50)
        self.assertEqual(claude_driver._shorten_input({"a": "b"}), ({"a": "b"}, False))


if __name__ == "__main__":
    unittest.main()
