"""The headless Codex driver, against a stand-in ``codex app-server``.

The stand-in is a small Python program (written below) that speaks the JSON-RPC lines recorded from the real Codex 0.154.0
app-server (no ``jsonrpc`` field): ``initialize`` / ``initialized``, ``thread/start`` and ``thread/resume`` (thread id and rollout
path), ``turn/start`` / ``turn/steer`` / ``turn/interrupt``, the ``turn/started``, ``item/started`` (a ``userMessage`` item carrying
our ``clientId``), ``item/completed`` and ``turn/completed`` notifications, ``hooks/list`` for the hook hashes, and the requests the
server may send (approvals, ``item/tool/requestUserInput``, MCP elicitations). What the message text asks for picks the behaviour
(SLOW, APPROVE, ASK, BLOCK, FAIL, CRASH, HANG, ...). Nothing here runs the real ``codex``.
"""
import isolated_cli_environment

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from qorqut.managed_runtime import codex_driver
from qorqut.managed_runtime.codex_driver import CodexSession, CodexSessionError
from test_managed_claude_driver import Recorder, pid_alive

FAKE_SERVER = r'''#!__PYTHON__
import json, os, queue, signal, subprocess, sys, threading, time, uuid

LOG = os.environ["FAKE_LOG"]
ARGV = sys.argv[1:]
q = queue.Queue()
state = {"thread": None, "turn": None, "pending": [], "interrupted": False, "eof": False, "counter": 0, "ids": 100}
responses = {}


def log(**record):
    with open(LOG, "a") as handle:
        handle.write(json.dumps(record) + chr(10))


def out(record):
    sys.stdout.write(json.dumps(record) + chr(10))
    sys.stdout.flush()


def note(method, params):
    out({"method": method, "params": params})


def reader():
    for line in sys.stdin:
        q.put(json.loads(line))
    q.put(None)


threading.Thread(target=reader, daemon=True).start()
log(kind="argv", argv=ARGV, cwd=os.getcwd(), env={k: os.environ.get(k) for k in ("CODEX_THREAD_ID", "CODEX_TURN_ID", "FAKE_MARKER")})


def hook_entries():
    entries = []
    for index, value in enumerate(ARGV):
        if value == "-c" and ARGV[index + 1].startswith("hooks.") and not ARGV[index + 1].startswith("hooks.state"):
            event = ARGV[index + 1].split("=", 1)[0][len("hooks."):]
            entries.append({"key": "/<session-flags>/config.toml:%s:0:0" % event.lower(), "eventName": event, "source": "sessionFlags",
                            "currentHash": "sha256:" + event.lower()})
    return entries


def reply(message, result=None, error=None):
    out({"id": message["id"], **({"error": error} if error else {"result": result})})


def handle(message):
    """One message from the client. Returns nothing; may change ``state``."""
    method = message.get("method")
    params = message.get("params") or {}
    if method is None:
        responses[message["id"]] = message
        log(kind="in", record=message)
        return
    log(kind="in", record=message)
    if method == "initialize":
        reply(message, {"userAgent": "fake/0", "codexHome": "/x", "platformFamily": "unix", "platformOs": "linux"})
    elif method == "hooks/list":
        entries = [] if os.environ.get("FAKE_NOHOOKS") else hook_entries()
        if os.environ.get("FAKE_HOOKLIST_HANG"):
            while True:
                time.sleep(1)
        reply(message, {"data": [{"cwd": params["cwds"][0], "hooks": entries, "warnings": [], "errors": []}]})
    elif method in ("thread/start", "thread/resume"):
        if os.environ.get("FAKE_START_ERROR"):
            reply(message, error={"code": -32600, "message": os.environ["FAKE_START_ERROR"]})
            return
        if method == "thread/resume" and params.get("threadId") == os.environ.get("FAKE_MISSING"):
            reply(message, error={"code": -32600, "message": "no rollout found for thread id " + params["threadId"]})
            return
        thread = params.get("threadId") or os.environ.get("FAKE_THREAD") or str(uuid.uuid4())
        state["thread"] = thread
        profile = (params.get("config") or {}).get("default_permissions")
        reply(message, {"thread": {"id": thread, "path": "/rollouts/rollout-" + thread + ".jsonl"},
                        "approvalPolicy": os.environ.get("FAKE_POLICY", "never"),
                        "activePermissionProfile": {"id": os.environ.get("FAKE_PROFILE", profile)} if profile else None})
    elif method == "turn/start":
        if state["turn"] is None:
            state["counter"] += 1
            state["turn"] = "turn-%d" % state["counter"]
            state["fresh"] = True
        reply(message, {"turn": {"id": state["turn"], "items": [], "status": "inProgress"}})
        state["pending"].append(message)
    elif method == "turn/steer":
        if state["turn"] is None:
            reply(message, error={"code": -32600, "message": "no active turn to steer"})
        elif params.get("expectedTurnId") != state["turn"]:
            reply(message, error={"code": -32600, "message": "expected active turn id %s but found %s" % (params.get("expectedTurnId"), state["turn"])})
        else:
            reply(message, {"turnId": state["turn"]})
            if not os.environ.get("FAKE_DROP_STEER"):
                state["pending"].append(message)
    elif method == "turn/interrupt":
        if state["turn"] is not None and params.get("turnId") == state["turn"]:
            reply(message, {})
            state["interrupted"] = True
        # a finished turn: no answer at all, as the real server does
    elif method in ("thread/compact/start", "thread/settings/update"):
        reply(message, {})


def take(timeout):
    try:
        message = q.get(timeout=timeout)
    except queue.Empty:
        return
    if message is None:
        state["eof"] = True
        return
    handle(message)


def pause(seconds):
    end = time.time() + seconds
    while time.time() < end and not state["interrupted"] and not state["eof"]:
        take(0.02)


def ask(method, params):
    state["ids"] += 1
    rid = 0 if (method == "item/tool/requestUserInput" and not state.get("asked")) else state["ids"]
    state["asked"] = True
    log(kind="asked", method=method, id=rid)
    out({"id": rid, "method": method, "params": params})
    while rid not in responses and not state["interrupted"] and not state["eof"]:
        take(0.02)
    return rid, responses.get(rid)


def item(kind, **fields):
    state["ids"] += 1
    return {"type": kind, "id": "item-%d" % state["ids"], **fields}


def agent(text):
    value = item("agentMessage", text=text, phase=None)
    note("item/started", {"threadId": state["thread"], "turnId": state["turn"], "item": dict(value, text="")})
    note("item/completed", {"threadId": state["thread"], "turnId": state["turn"], "item": value})


def serve(message):
    params = message["params"]
    text = "".join(part.get("text", "") for part in params["input"])
    thread, turn = state["thread"], state["turn"]
    if "BLOCK" in text:
        note("hook/completed", {"threadId": thread, "turnId": turn, "run": {"eventName": "userPromptSubmit", "status": "blocked", "entries": [
            {"kind": "feedback", "text": "BLOCKED_BY_HOOK"}]}})
        return "blocked"
    value = item("userMessage", clientId=params.get("clientUserMessageId"), content=params["input"])
    note("item/started", {"threadId": thread, "turnId": turn, "item": value})
    note("item/completed", {"threadId": thread, "turnId": turn, "item": value})
    if "OTHERTHREAD" in text:
        note("turn/completed", {"threadId": "other-thread", "turn": {"id": "x", "status": "failed", "error": {"message": "NOT_MINE"}}})
        note("item/started", {"threadId": "other-thread", "turnId": "x", "item": item("commandExecution", command="SUBAGENT_SECRET")})
    if "GRANDCHILD" in text:
        child = subprocess.Popen(["sleep", "300"])
        with open(os.environ["FAKE_PIDFILE"], "w") as handle:
            handle.write(str(child.pid))
    if "CRASH" in text:
        sys.stderr.write("OPENAI_API_KEY=sk-proj-SECRETSECRET123 failed to reach the host\n")
        sys.stderr.flush()
        note("item/started", {"threadId": thread, "turnId": turn, "item": item("commandExecution", command="true")})
        os._exit(3)
    if "HANG" in text:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(["sleep", "300"])
        with open(os.environ["FAKE_PIDFILE"], "w") as handle:
            handle.write(str(child.pid))
        while True:
            time.sleep(1)
    if "JUNK" in text:
        sys.stdout.write("this is not json\n[1, 2]\n\n")
        sys.stdout.flush()
    if "BIGLINE" in text:
        sys.stdout.write(json.dumps({"method": "item/updated", "params": {"blob": "z" * 300000}}) + chr(10))
        sys.stdout.flush()
    if "TOOLS" in text:
        note("item/started", {"threadId": thread, "turnId": turn, "item": item("commandExecution", command="curl -H 'Authorization: Bearer SECRETVALUE99' https://x.example")})
        note("item/started", {"threadId": thread, "turnId": turn, "item": item("mcpToolCall", server="qorqut", tool="ping", arguments={"token": "SECRETVALUE99"})})
        note("item/started", {"threadId": thread, "turnId": turn, "item": item("fileChange", changes=[{"path": "/home/owner/private/notes.md"}])})
        note("error", {"threadId": thread, "turnId": turn, "willRetry": True, "error": {"message": "stream dropped, key sk-proj-ABCDEF123456", "codexErrorInfo": "other"}})
    if "SLOW" in text:
        seconds = float(text.split("SLOW:")[1].split()[0]) if "SLOW:" in text else 2.0
        note("item/started", {"threadId": thread, "turnId": turn, "item": item("commandExecution", command="sleep")})
        pause(seconds)
        if state["interrupted"]:
            return "interrupted"
    if "APPROVE" in text:
        decisions = []
        for method, body in (
                ("item/commandExecution/requestApproval", {"command": "rm -rf /", "cwd": "/x", "availableDecisions": ["accept", "cancel"]}),
                ("item/fileChange/requestApproval", {"reason": "write"}),
                ("item/permissions/requestApproval", {"permissions": {"network": {"enabled": True}}, "reason": "net"}),
                ("execCommandApproval", {"command": ["ls"], "cwd": "/x", "conversationId": thread}),
                ("mcpServer/elicitation/request", {"serverName": "qorqut", "mode": "form", "message": "ok?", "requestedSchema": {}}),
                ("item/something/new", {"x": 1})):
            rid, response = ask(method, {"threadId": thread, "turnId": turn, "itemId": "i1", **body})
            decisions.append([method, response])
        agent("APPROVED")
        return "done"
    if "ASK" in text:
        questions = [{"id": "q1", "header": "Colour", "question": "Which colour?", "isOther": False, "isSecret": False,
                      "options": [{"label": "Red", "description": "r"}, {"label": "Blue", "description": "b"}]},
                     {"id": "q2", "header": "Extras", "question": "Which extras?", "isOther": True, "isSecret": False,
                      "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}]}]
        if "ASKCANCEL" in text:
            state["ids"] += 1
            out({"id": 77, "method": "item/tool/requestUserInput", "params": {"threadId": thread, "turnId": turn, "itemId": "i2", "questions": questions}})
            pause(0.5)
            note("serverRequest/resolved", {"threadId": thread, "requestId": 77})
            pause(0.3)
        elif "ASKLEFT" in text:
            out({"id": 78, "method": "item/tool/requestUserInput", "params": {"threadId": thread, "turnId": turn, "itemId": "i3", "questions": questions}})
            pause(30)
            return "interrupted" if state["interrupted"] else "done"
        else:
            rid, response = ask("item/tool/requestUserInput", {"threadId": thread, "turnId": turn, "itemId": "i1", "questions": questions,
                                                                "isBlocking": True, "autoResolutionMs": None})
            if response is None:
                return "interrupted"
            note("serverRequest/resolved", {"threadId": thread, "requestId": rid})
            agent("ANSWERS " + json.dumps(response.get("result")))
            return "done"
    if "BIGRESULT" in text:
        sys.stdout.write(json.dumps({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed",
                         "items": [{"type": "agentMessage", "text": "r" * 400000}]}}}) + chr(10))
        sys.stdout.flush()
        return "bigresult"
    if "FAIL" in text:
        note("error", {"threadId": thread, "turnId": turn, "willRetry": False, "error": {"message": "You have hit your usage limit. Key sk-proj-LIMITSECRET99",
                                                                                          "codexErrorInfo": "usageLimitExceeded"}})
        return "failed"
    agent("OK " + text[:40])
    return "done"


def run():
    thread, turn = state["thread"], state["turn"]
    note("turn/started", {"threadId": thread, "turn": {"id": turn, "items": [], "status": "inProgress"}})
    outcome, outcomes = "done", []
    while state["pending"] and outcome not in ("interrupted", "failed", "bigresult"):
        outcomes.append(outcome := serve(state["pending"].pop(0)))
        take(0.0)
    if outcome == "blocked" and any(item != "blocked" for item in outcomes):
        outcome = "done"
    if outcome == "interrupted":
        status = "interrupted"
    elif outcome == "failed":
        status = "failed"
    else:
        status = "completed"
    if outcome != "bigresult":
        body = {"id": turn, "status": status, "items": [], "error": None}
        if status == "failed":
            body["error"] = {"message": "You have hit your usage limit.", "codexErrorInfo": "usageLimitExceeded", "additionalDetails": None}
        note("turn/completed", {"threadId": thread, "turn": body})
    state["turn"], state["pending"], state["interrupted"] = None, [], False
    if os.environ.get("FAKE_DROP_STEER"):
        pass


while True:
    if state["pending"] and state["turn"] is not None:
        run()
    elif state["eof"] and not os.environ.get("FAKE_IGNORE_EOF"):
        break
    else:
        take(0.05)
log(kind="exit")
'''

PREPARED_CONFIG = """
default_permissions = ":danger-full-access"
approval_policy = "never"
[mcp_servers.qorqut]
command = "/bin/true"
[tui]
notifications = true
"""
SCOPED_CONFIG = """
default_permissions = "qorqut-agent"
[permissions.qorqut-agent.filesystem]
":project_roots" = { "." = "write" }
"""
PREPARED_HOOKS = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/bin/sh /x/.qorqut/codex-hook activity"}]}],
                            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/bin/sh /x/.qorqut/codex-hook activity"}]}]}}


class DriverCase(unittest.TestCase):
    def setUp(self):
        self.isolated = isolated_cli_environment.isolate(self)
        self.root = Path(tempfile.mkdtemp(prefix="codex driver "))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.folder = self.root / "agent folder"
        (self.folder / ".codex").mkdir(parents=True)
        (self.folder / ".codex" / "config.toml").write_text(PREPARED_CONFIG)
        (self.folder / ".codex" / "hooks.json").write_text(json.dumps(PREPARED_HOOKS))
        self.cli = self.root / "fake-codex"
        self.cli.write_text(FAKE_SERVER.replace("__PYTHON__", sys.executable))
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "fake.log"
        self.pidfile = self.root / "child.pid"
        self.sessions = []
        self.addCleanup(self.close_all)
        self.before_threads = self.driver_threads()

    @staticmethod
    def driver_threads():
        return {thread.name for thread in threading.enumerate() if thread.name.startswith(("claude-driver", "codex-driver"))}

    def close_all(self):
        for session in self.sessions:
            session.CLOSE_GRACE_SECONDS, session.TERMINATE_GRACE_SECONDS = 0.5, 0.5
            session.close()
        if self.pidfile.exists():
            try:
                os.kill(int(self.pidfile.read_text()), 9)
            except (OSError, ValueError):
                pass

    def make(self, recorder=None, env=None, args=(), resume=None, transport_limit=None, **extra):
        recorder = recorder if recorder is not None else Recorder()
        environment = {"FAKE_LOG": str(self.log), "FAKE_PIDFILE": str(self.pidfile), "FAKE_MARKER": "marker-1", **(env or {})}
        session = CodexSession(self.folder, self.cli, env=environment, args=args, resume_session_id=resume, on_event=recorder,
                               **{key: value for key, value in extra.items() if key in ("model", "effort", "bypass")})
        for name, value in extra.items():
            if name not in ("model", "effort", "bypass"):
                setattr(session, name, value)
        if transport_limit:
            session.transport_class = type("SmallTransport", (codex_driver._CodexTransport,), {"MAX_LINE_BYTES": transport_limit})
        self.sessions.append(session)
        return session, recorder

    def records(self, kind=None):
        if not self.log.exists():
            return []
        found = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        return [item for item in found if kind is None or item["kind"] == kind]

    def inputs(self, method=None):
        found = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()] if self.log.exists() else []
        last = max((index for index, item in enumerate(found) if item["kind"] == "argv"), default=0)      # the real server, not the hook probe
        records = [item["record"] for item in found[last:] if item["kind"] == "in"]
        return [record for record in records if method is None or record.get("method") == method]

    def send(self, session, text):
        message_uuid = str(uuid.uuid4())
        session.send(text, message_uuid)
        return message_uuid

    def assertNoDriverThreads(self, session):
        end = time.time() + 3
        while time.time() < end and session.alive_threads():
            time.sleep(0.02)
        self.assertEqual(session.alive_threads(), [])
        leaked = self.driver_threads() - self.before_threads
        self.assertEqual(leaked, set())


class LaunchTests(DriverCase):
    def test_new_thread_command_line_handshake_and_thread_config(self):
        session, recorder = self.make(args=["-c", "check_for_update_on_startup=false"], model="gpt-5.6-luna", effort="low")
        self.assertIsNone(session.session_id)
        session.start()
        started = recorder.wait("session_started")
        self.assertEqual(started["session_id"], session.session_id)
        self.assertEqual(started["transcript_path"], "/rollouts/rollout-%s.jsonl" % session.session_id)
        self.assertEqual(session.transcript_path, started["transcript_path"])
        argv = self.records("argv")[-1]["argv"]
        self.assertEqual(argv[-3:], ["app-server", "--listen", "stdio://"])
        self.assertEqual(argv[:2], ["-c", "check_for_update_on_startup=false"])
        self.assertEqual(self.records("argv")[-1]["cwd"], os.path.realpath(self.folder))
        methods = [record["method"] for record in self.inputs() if "method" in record]
        self.assertEqual(methods[:3], ["initialize", "initialized", "thread/start"])
        initialize = self.inputs("initialize")[0]["params"]
        self.assertTrue(initialize["capabilities"]["experimentalApi"])
        params = self.inputs("thread/start")[0]["params"]
        self.assertEqual((params["approvalPolicy"], params["model"], params["cwd"]), ("never", "gpt-5.6-luna", str(self.folder)))
        self.assertEqual(params["config"]["model_reasoning_effort"], "low")
        self.assertEqual(params["config"]["default_permissions"], ":danger-full-access")
        self.assertIn("qorqut", params["config"]["mcp_servers"])
        self.assertNotIn("tui", params["config"])
        session.close()

    def test_hooks_are_passed_at_launch_with_their_trusted_hashes(self):
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        probe, real = self.records("argv")[0]["argv"], self.records("argv")[-1]["argv"]
        self.assertNotIn("hooks.state", " ".join(probe))
        joined = " ".join(real)
        self.assertIn("hooks.Stop=", joined)
        self.assertIn("hooks.UserPromptSubmit=", joined)
        state = next(value for value in real if value.startswith("hooks.state="))
        self.assertIn("trusted_hash", state)
        self.assertIn("sha256:stop", state)
        self.assertEqual(recorder.of("error"), [])
        session.close()

    def test_hooks_that_cannot_be_trusted_are_reported_and_the_session_still_runs(self):
        session, recorder = self.make(env={"FAKE_NOHOOKS": "1"})
        session.start()
        problem = recorder.wait("error")
        self.assertEqual(problem["kind"], "hooks_not_trusted")
        recorder.wait("session_started")
        self.assertFalse(any(value.startswith("hooks.state=") for value in self.records("argv")[-1]["argv"]))
        session.close()

    def test_no_hooks_file_means_no_probe(self):
        (self.folder / ".codex" / "hooks.json").unlink()
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        self.assertEqual(len(self.records("argv")), 1)
        self.assertEqual(recorder.of("error"), [])
        session.close()

    def test_resume_uses_the_thread_id_and_session_started_carries_it(self):
        thread = str(uuid.uuid4())
        session, recorder = self.make(resume=thread)
        self.assertEqual(session.session_id, thread)
        session.start()
        started = recorder.wait("session_started")
        self.assertEqual(started["session_id"], thread)
        params = self.inputs("thread/resume")[0]["params"]
        self.assertEqual(params["threadId"], thread)
        self.assertEqual(self.inputs("thread/start"), [])
        self.assertEqual(params["config"]["default_permissions"], ":danger-full-access")
        message_uuid = self.send(session, "after resume")
        recorder.wait("turn_finished")
        self.assertEqual(recorder.of("turn_finished")[0]["message_uuids"], [message_uuid])
        session.close()

    def test_resume_of_a_thread_the_server_does_not_have_fails_and_ends_the_process(self):
        thread = str(uuid.uuid4())
        session, recorder = self.make(resume=thread, env={"FAKE_MISSING": thread})
        session.start()
        problem = recorder.wait("error")
        self.assertEqual(problem["kind"], "thread_resume_failed")
        self.assertIn("no rollout found", problem["message"])
        exited = recorder.wait("exited")
        self.assertEqual(recorder.of("session_started"), [])
        self.assertTrue(exited["unexpected"])
        session.close()
        self.assertNoDriverThreads(session)

    def test_start_failure_is_reported(self):
        session, recorder = self.make(env={"FAKE_START_ERROR": "fs sandbox helper failed: bwrap: loopback: Failed RTM_NEWADDR"})
        session.start()
        problem = recorder.wait("error", where=lambda event: event["kind"] == "thread_start_failed")
        self.assertIn("bwrap", problem["message"])
        recorder.wait("exited")
        self.assertEqual(recorder.of("session_started"), [])
        session.close()

    def test_a_server_that_does_not_run_the_prepared_permissions_is_not_used(self):
        session, recorder = self.make(env={"FAKE_PROFILE": ":workspace"})
        session.start()
        problem = recorder.wait("error")
        self.assertEqual(problem["kind"], "start_failed")
        recorder.wait("exited")
        self.assertEqual(recorder.of("session_started"), [])
        session2, recorder2 = self.make(env={"FAKE_POLICY": "on-request"})
        session2.start()
        recorder2.wait("exited")
        self.assertEqual(recorder2.of("session_started"), [])

    def test_arguments_the_driver_owns_are_refused(self):
        for bad in (["app-server"], ["--listen", "ws://0.0.0.0:1"], ["--listen=stdio://"], ["--code-mode-host"], ["--ws-token-file", "x"]):
            with self.assertRaises(ValueError, msg=bad):
                CodexSession(self.folder, self.cli, args=bad)
        for bad in (dict(model="--x"), dict(model="a b"), dict(effort="turbo")):
            with self.assertRaises(ValueError, msg=bad):
                CodexSession(self.folder, self.cli, **bad)
        with self.assertRaises(ValueError):
            CodexSession(self.folder, self.cli, resume_session_id="not-a-uuid")

    def test_model_and_effort_in_args_are_read_and_not_passed_on(self):
        session, recorder = self.make(args=["--model", "gpt-5.6-terra", "--effort=high"])
        session.start()
        recorder.wait("session_started")
        self.assertNotIn("--model", self.records("argv")[-1]["argv"])
        params = self.inputs("thread/start")[0]["params"]
        self.assertEqual((params["model"], params["config"]["model_reasoning_effort"]), ("gpt-5.6-terra", "high"))
        self.send(session, "hello")
        recorder.wait("turn_finished")
        turn = self.inputs("turn/start")[0]["params"]
        self.assertEqual((turn["model"], turn["effort"]), ("gpt-5.6-terra", "high"))
        session.close()

    def test_environment_is_inherited_minus_the_codex_thread_variables(self):
        os.environ["CODEX_THREAD_ID"] = "leaked-thread"
        self.addCleanup(os.environ.pop, "CODEX_THREAD_ID", None)
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        env = self.records("argv")[-1]["env"]
        self.assertIsNone(env["CODEX_THREAD_ID"])
        self.assertEqual(env["FAKE_MARKER"], "marker-1")
        session.close()

    def test_start_twice_is_refused(self):
        session, recorder = self.make()
        session.start()
        with self.assertRaises(CodexSessionError):
            session.start()
        session.close()

    def test_bypass_is_read_from_the_prepared_configuration(self):
        self.assertTrue(self.make()[0].bypass)
        (self.folder / ".codex" / "config.toml").write_text(SCOPED_CONFIG)
        self.assertFalse(self.make()[0].bypass)
        self.assertTrue(self.make(bypass=True)[0].bypass)


class SendTests(DriverCase):
    def test_first_message_reply_and_completion(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "hello there")
        self.assertTrue(session.busy)
        started = recorder.wait("turn_started")
        self.assertEqual(started["message_uuid"], message_uuid)
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["message_uuids"], finished["blocked_message_uuids"]), ("completed", [message_uuid], []))
        self.assertEqual(finished["result_text"], "OK hello there")
        self.assertEqual(finished["session_id"], session.session_id)
        types = recorder.types()
        self.assertLess(types.index("session_started"), types.index("turn_started"))
        self.assertLess(types.index("turn_started"), types.index("turn_finished"))
        deadline = time.time() + 3
        while session.busy and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(session.busy)
        turn = self.inputs("turn/start")[0]["params"]
        self.assertEqual(turn["clientUserMessageId"], message_uuid)
        self.assertEqual(turn["input"][0]["text"], "hello there")
        session.close()

    def test_a_message_sent_before_the_thread_exists_waits_for_it(self):
        session, recorder = self.make()
        session.start()
        first = self.send(session, "one")
        second = self.send(session, "two")
        recorder.wait("turn_finished", count=1)
        deadline = time.time() + 5
        covered = set()
        while covered != {first, second} and time.time() < deadline:
            covered = {u for event in recorder.of("turn_finished") for u in event["message_uuids"]}
            time.sleep(0.02)
        self.assertEqual(covered, {first, second})
        session.close()

    def test_sequential_messages_each_get_their_own_turn(self):
        session, recorder = self.make()
        session.start()
        first = self.send(session, "one")
        recorder.wait("turn_finished")
        second = self.send(session, "two")
        recorder.wait("turn_finished", count=2)
        self.assertEqual([event["message_uuids"] for event in recorder.of("turn_finished")], [[first], [second]])
        self.assertEqual(len(self.inputs("turn/start")), 2)
        session.close()

    def test_a_message_while_a_turn_runs_is_steered_into_it(self):
        session, recorder = self.make()
        session.start()
        first = self.send(session, "SLOW:1.0")
        recorder.wait("turn_started")
        deadline = time.time() + 5
        while session._turn_id is None and time.time() < deadline:
            time.sleep(0.02)
        second = self.send(session, "follow up")
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["message_uuids"], [first, second])
        self.assertEqual(finished["status"], "completed")
        steer = self.inputs("turn/steer")
        self.assertEqual(len(steer), 1)
        self.assertEqual((steer[0]["params"]["expectedTurnId"], steer[0]["params"]["clientUserMessageId"]), ("turn-1", second))
        self.assertEqual(len(recorder.of("turn_started")), 2)
        self.assertEqual(len(self.inputs("turn/start")), 1)
        self.assertEqual(len(recorder.of("turn_finished")), 1)
        session.close()

    def test_a_message_sent_before_the_turn_is_known_is_not_lost(self):
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        first = self.send(session, "SLOW:0.6")
        second = self.send(session, "right behind")             # turn/start while the first is still in flight: the server steers
        deadline = time.time() + 6
        covered = set()
        while covered != {first, second} and time.time() < deadline:
            covered = {u for event in recorder.of("turn_finished") for u in event["message_uuids"]}
            time.sleep(0.02)
        self.assertEqual(covered, {first, second})
        session.close()

    def test_steer_into_a_turn_that_just_ended_starts_a_new_turn(self):
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        first = self.send(session, "quick")
        recorder.wait("turn_finished")
        with session._lock:                      # the driver still believes the old turn runs
            session._turn_id = "turn-1"
        second = self.send(session, "late")
        finished = recorder.wait("turn_finished", count=2)
        self.assertEqual(finished["message_uuids"], [second])
        self.assertEqual(len(self.inputs("turn/start")), 2)
        session.close()

    def test_a_steer_the_server_accepted_but_never_showed_is_reported_dropped(self):
        session, recorder = self.make(env={"FAKE_DROP_STEER": "1"}, ORPHAN_GRACE_SECONDS=0.3)
        session.start()
        first = self.send(session, "SLOW:0.8")
        recorder.wait("turn_started")
        deadline = time.time() + 5
        while session._turn_id is None and time.time() < deadline:
            time.sleep(0.02)
        second = self.send(session, "vanishes")
        recorder.wait("turn_finished")
        dropped = recorder.wait("turn_finished", count=2)
        self.assertEqual(dropped["dropped_message_uuids"], [second])
        self.assertEqual(recorder.of("turn_finished")[0]["message_uuids"], [first])
        deadline = time.time() + 3
        while session.busy and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(session.busy)
        session.close()

    def test_validation(self):
        session, recorder = self.make()
        with self.assertRaises(CodexSessionError):
            session.send("too early", str(uuid.uuid4()))
        session.start()
        with self.assertRaises(ValueError):
            session.send(b"bytes", str(uuid.uuid4()))
        with self.assertRaises(ValueError):
            session.send("x", "not-a-uuid")
        with self.assertRaises(ValueError):
            session.send("x" * (session.MAX_MESSAGE_BYTES + 1), str(uuid.uuid4()))
        same = str(uuid.uuid4())
        session.send("SLOW:1.0", same)
        with self.assertRaises(ValueError):
            session.send("again", same)
        recorder.wait("turn_finished")
        session.close()

    def test_large_message_that_fits_is_delivered_whole(self):
        session, recorder = self.make()
        session.start()
        text = "é" * 500_000
        self.send(session, text)
        recorder.wait("turn_finished")
        self.assertEqual(self.inputs("turn/start")[0]["params"]["input"][0]["text"], text)
        session.close()

    def test_activity_uses_fixed_sentences_and_never_commands_or_arguments(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "TOOLS")
        recorder.wait("turn_finished")
        texts = [event["text"] for event in recorder.of("activity")]
        self.assertIn("Running a command", texts)
        self.assertIn("Using ping (qorqut)", texts)
        self.assertIn("Editing files", texts)
        dumped = json.dumps(recorder.events)
        for secret in ("SECRETVALUE99", "notes.md", "sk-proj-ABCDEF123456", "curl"):
            self.assertNotIn(secret, dumped)
        retry = recorder.wait("error")
        self.assertEqual(retry["kind"], "codex_retry")
        session.close()

    def test_notifications_of_other_threads_are_ignored(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "OTHERTHREAD")
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["message_uuids"]), ("completed", [message_uuid]))
        self.assertNotIn("NOT_MINE", json.dumps(recorder.events))
        self.assertNotIn("SUBAGENT_SECRET", json.dumps(recorder.events))
        session.close()

    def test_compact_and_settings_use_the_real_commands(self):
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        self.assertTrue(session.compact())
        self.assertTrue(session.update_settings(model="gpt-5.6-terra", effort="high"))
        deadline = time.time() + 3
        while len(self.inputs("thread/settings/update")) < 1 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.inputs("thread/compact/start")[0]["params"], {"threadId": session.session_id})
        self.assertEqual(self.inputs("thread/settings/update")[0]["params"], {"threadId": session.session_id, "model": "gpt-5.6-terra", "effort": "high"})
        self.send(session, "next")
        recorder.wait("turn_finished")
        turn = self.inputs("turn/start")[0]["params"]
        self.assertEqual((turn["model"], turn["effort"]), ("gpt-5.6-terra", "high"))
        with self.assertRaises(ValueError):
            session.update_settings()
        with self.assertRaises(ValueError):
            session.update_settings(effort="turbo")
        session.close()

    def test_slash_text_is_sent_as_plain_text(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "/compact")
        recorder.wait("turn_finished")
        self.assertEqual(self.inputs("turn/start")[0]["params"]["input"][0]["text"], "/compact")
        self.assertEqual(self.inputs("thread/compact/start"), [])
        session.close()


class InterruptTests(DriverCase):
    def test_interrupt_idle_returns_false_and_sends_nothing(self):
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        self.assertFalse(session.interrupt())
        time.sleep(0.2)
        self.assertEqual(self.inputs("turn/interrupt"), [])
        session.close()

    def test_interrupt_a_running_turn(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "SLOW:20")
        recorder.wait("turn_started")
        deadline = time.time() + 5
        while session._turn_id is None and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(session.interrupt())
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["message_uuids"]), ("interrupted", [message_uuid]))
        self.assertEqual(self.inputs("turn/interrupt")[0]["params"], {"threadId": session.session_id, "turnId": "turn-1"})
        self.assertEqual(recorder.of("error"), [])
        follow = self.send(session, "after the interrupt")
        done = recorder.wait("turn_finished", count=2)
        self.assertEqual((done["status"], done["message_uuids"]), ("completed", [follow]))
        session.close()

    def test_interrupt_requested_before_the_turn_is_known_is_sent_when_it_is(self):
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        message_uuid = self.send(session, "SLOW:20")
        self.assertTrue(session.interrupt())
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["message_uuids"]), ("interrupted", [message_uuid]))
        session.close()

    def test_interrupt_for_a_turn_that_already_ended_gets_no_answer_and_nothing_hangs(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "quick")
        recorder.wait("turn_finished")
        with session._lock:
            session._turn_id = "turn-1"
        self.assertTrue(session.interrupt())                      # the server never answers this one
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(recorder.of("error"), [])

    def test_interrupt_cancels_an_open_question(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "ASKLEFT")
        question = recorder.wait("question")
        deadline = time.time() + 5
        while session._turn_id is None and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(session.interrupt())
        cancelled = recorder.wait("request_cancelled")
        self.assertEqual((cancelled["request_id"], cancelled["reason"]), (question["request_id"], "turn_ended"))
        self.assertFalse(session.answer(question["request_id"], {"answers": {"Which colour?": "Red"}}))
        session.close()

    def test_interrupt_after_close_is_false(self):
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        session.close()
        self.assertFalse(session.interrupt())


class PermissionTests(DriverCase):
    def run_approvals(self, **extra):
        session, recorder = self.make(**extra)
        session.start()
        self.send(session, "APPROVE")
        finished = recorder.wait("turn_finished")
        session.close()
        replies = {record["id"]: record for record in self.inputs() if "method" not in record}
        decisions = {item["method"]: replies.get(item["id"]) for item in self.records("asked")}
        return finished, recorder, decisions

    def test_approvals_are_answered_by_the_driver_and_never_forwarded_when_bypass(self):
        finished, recorder, decisions = self.run_approvals()
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["result_text"][:8], "APPROVED")
        self.assertEqual(recorder.of("question"), [])
        self.assertEqual(recorder.of("permission_request"), [])
        by_method = {method: response["result"] if response and "result" in response else response for method, response in decisions.items()}
        self.assertEqual(by_method["item/commandExecution/requestApproval"], {"decision": "accept"})
        self.assertEqual(by_method["item/fileChange/requestApproval"], {"decision": "accept"})
        self.assertEqual(by_method["item/permissions/requestApproval"], {"permissions": {"network": {"enabled": True}}, "scope": "turn"})
        self.assertEqual(by_method["execCommandApproval"], {"decision": "approved"})
        self.assertEqual(by_method["mcpServer/elicitation/request"]["action"], "decline")
        self.assertEqual(recorder.of("error"), [recorder.of("error")[0]])
        self.assertEqual(recorder.of("error")[0]["kind"], "unsupported_request")      # the unknown method, once
        self.assertNotIn("rm -rf", json.dumps(recorder.events))

    def test_approvals_are_declined_when_not_bypass_and_unknown_requests_get_an_error(self):
        (self.folder / ".codex" / "config.toml").write_text(SCOPED_CONFIG)
        finished, recorder, decisions = self.run_approvals(env={"FAKE_PROFILE": "qorqut-agent"})
        self.assertEqual(decisions["item/commandExecution/requestApproval"]["result"], {"decision": "decline"})
        self.assertEqual(decisions["item/fileChange/requestApproval"]["result"], {"decision": "decline"})
        self.assertEqual(decisions["item/permissions/requestApproval"]["result"], {"permissions": {}, "scope": "turn"})
        self.assertIn("denied", json.dumps(decisions["execCommandApproval"]["result"]))
        self.assertEqual(decisions["mcpServer/elicitation/request"]["result"]["action"], "decline")
        self.assertEqual(decisions["item/something/new"]["error"]["code"], -32601)
        self.assertEqual(recorder.of("permission_request"), [])
        self.assertEqual(recorder.of("question"), [])

    def test_bypass_false_overrides_the_configuration(self):
        finished, recorder, decisions = self.run_approvals(bypass=False)
        self.assertEqual(decisions["item/commandExecution/requestApproval"]["result"], {"decision": "decline"})


class QuestionTests(DriverCase):
    def test_question_event_shape_and_answer(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "ASK")
        question = recorder.wait("question")
        self.assertEqual(question["request_id"], "q-0")
        self.assertEqual(question["tool_use_id"], "i1")
        self.assertFalse(question["input_truncated"])
        first = question["questions"][0]
        self.assertEqual((first["question"], first["header"], first["multiSelect"]), ("Which colour?", "Colour", False))
        self.assertEqual([option["label"] for option in first["options"]], ["Red", "Blue"])
        self.assertTrue(question["questions"][1]["isOther"])
        self.assertEqual(question["input"]["questions"], question["questions"])
        self.assertTrue(session.busy)
        with self.assertRaises(ValueError):
            session.answer(question["request_id"], {"answers": {"Which colour?": "Red"}})        # the second question is missing
        with self.assertRaises(ValueError):
            session.answer(question["request_id"], {"nonsense": 1})
        self.assertTrue(session.answer(question["request_id"], {"answers": {"Which colour?": "Red", "Which extras?": "A, B"}}))
        finished = recorder.wait("turn_finished")
        sent = json.loads(finished["result_text"][len("ANSWERS "):])
        self.assertEqual(sent, {"answers": {"q1": {"answers": ["Red"]}, "q2": {"answers": ["A", "B"]}}})
        self.assertFalse(session.answer(question["request_id"], {"answers": {"Which colour?": "Red"}}))   # already answered
        self.assertEqual(recorder.of("permission_request"), [])
        session.close()

    def test_free_text_answer_is_kept_whole_and_deny_gives_no_answers(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "ASK")
        question = recorder.wait("question")
        self.assertTrue(session.answer(question["request_id"], {"answers": {"Which colour?": "Green, with yellow", "Which extras?": "something else"}}))
        finished = recorder.wait("turn_finished")
        sent = json.loads(finished["result_text"][len("ANSWERS "):])
        self.assertEqual(sent["answers"]["q1"], {"answers": ["Green, with yellow"]})
        self.assertEqual(sent["answers"]["q2"], {"answers": ["something else"]})
        self.send(session, "ASK again")
        question = recorder.wait("question", count=2)
        self.assertTrue(session.answer(question["request_id"], "deny"))
        finished = recorder.wait("turn_finished", count=2)
        self.assertEqual(json.loads(finished["result_text"][len("ANSWERS "):]), {"answers": {"q1": {"answers": []}, "q2": {"answers": []}}})
        session.close()

    def test_a_question_codex_withdraws_is_cancelled(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "ASKCANCEL")
        question = recorder.wait("question")
        self.assertEqual(question["request_id"], "q-77")
        cancelled = recorder.wait("request_cancelled")
        self.assertEqual((cancelled["request_id"], cancelled["reason"]), ("q-77", "resolved_by_codex"))
        self.assertFalse(session.answer("q-77", {"answers": {"Which colour?": "Red"}}))
        self.assertFalse(session.answer("never-seen", {"answers": {}}))
        recorder.wait("turn_finished")
        session.close()

    def test_a_question_open_at_close_is_cancelled(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "ASKLEFT")
        recorder.wait("question")
        session.close()
        cancelled = recorder.wait("request_cancelled")
        self.assertIn(cancelled["reason"], ("session_ended", "turn_ended"))


class BlockedPromptTests(DriverCase):
    def test_blocked_prompt_is_reported_and_finishes_with_the_message_listed(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "BLOCK this")
        blocked = recorder.wait("prompt_blocked")
        self.assertEqual(blocked["message_uuid"], message_uuid)
        self.assertIn("BLOCKED_BY_HOOK", blocked["reason"])
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["message_uuids"], finished["blocked_message_uuids"]), ("completed", [message_uuid], [message_uuid]))
        self.assertNotIn("result_text", finished)
        self.assertEqual(recorder.of("turn_started"), [])
        self.assertFalse(session.busy)
        session.close()


class BadOutputTests(DriverCase):
    def test_junk_lines_are_skipped_and_reported_boundedly(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "JUNK")
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["message_uuids"], [message_uuid])
        kinds = [event["kind"] for event in recorder.of("error")]
        self.assertIn("malformed_line", kinds)
        session.close()

    def test_oversized_record_is_skipped_and_the_session_continues(self):
        session, recorder = self.make(transport_limit=100_000)
        session.start()
        self.send(session, "BIGLINE")
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["status"], "completed")
        problem = recorder.wait("error")
        self.assertEqual(problem["kind"], "oversized_line")
        self.assertGreater(problem["bytes"], 100_000)
        self.send(session, "still alive")
        self.assertEqual(recorder.wait("turn_finished", count=2)["status"], "completed")
        session.close()

    def test_an_oversized_end_of_turn_finishes_the_turn_as_an_error(self):
        session, recorder = self.make(transport_limit=100_000)
        session.start()
        message_uuid = self.send(session, "BIGRESULT")
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["message_uuids"]), ("error", [message_uuid]))
        self.assertIn("too large", finished["error"])
        self.assertFalse(session.busy)
        session.close()

    def test_a_failed_turn_reports_the_reason_and_scrubs_secrets(self):
        session, recorder = self.make()
        session.start()
        message_uuid = self.send(session, "FAIL")
        finished = recorder.wait("turn_finished")
        self.assertEqual((finished["status"], finished["reason"], finished["message_uuids"]), ("error", "usageLimitExceeded", [message_uuid]))
        self.assertIn("usage limit", finished["error"])
        problem = recorder.wait("error")
        self.assertEqual(problem["kind"], "codex_error")
        self.assertNotIn("LIMITSECRET99", json.dumps(recorder.events))
        self.send(session, "next try")
        self.assertEqual(recorder.wait("turn_finished", count=2)["status"], "completed")
        session.close()


class FailureTests(DriverCase):
    def test_server_exit_mid_turn(self):
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
        with self.assertRaises(CodexSessionError):
            session.send("anyone there", str(uuid.uuid4()))
        self.assertFalse(session.interrupt())
        self.assertFalse(session.busy)
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(recorder.of("exited")), 1)
        self.assertNoDriverThreads(session)

    def test_unexpected_exit_while_idle_is_reported_once(self):
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        os.kill(session.pid, 9)
        exited = recorder.wait("exited")
        self.assertTrue(exited["unexpected"])
        self.assertEqual(recorder.of("turn_finished"), [])
        session.close()
        self.assertEqual(len(recorder.of("exited")), 1)

    def test_hook_probe_that_never_answers_does_not_block_forever(self):
        started = time.monotonic()
        original = codex_driver.hook_trust_arguments
        try:
            codex_driver.hook_trust_arguments = lambda *a, **k: original(*a, timeout=0.5, **k)
            session, recorder = self.make(env={"FAKE_HOOKLIST_HANG": "1"})
            session.start()
        finally:
            codex_driver.hook_trust_arguments = original
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(recorder.wait("error")["kind"], "hooks_not_trusted")
        session.close()

    def test_a_server_that_never_opens_the_thread_is_given_up_on(self):
        session, recorder = self.make(env={"FAKE_IGNORE_EOF": "1"}, START_TIMEOUT_SECONDS=0.3)
        (self.folder / ".codex" / "hooks.json").unlink()
        with open(self.cli, "a"):
            pass
        text = self.cli.read_text().replace('    if method == "initialize":\n', '    if method == "initialize" and os.environ.get("FAKE_SILENT"):\n        return\n    if method == "initialize":\n')
        self.cli.write_text(text)
        session2, recorder2 = self.make(env={"FAKE_SILENT": "1"}, START_TIMEOUT_SECONDS=0.3)
        session2.start()
        self.assertEqual(recorder2.wait("error")["kind"], "start_timeout")
        recorder2.wait("exited", timeout=10)
        session2.close()

    def test_close_while_idle_ends_cleanly_with_exit_code_zero(self):
        session, recorder = self.make()
        session.start()
        recorder.wait("session_started")
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 3)
        exited = recorder.wait("exited")
        self.assertEqual((exited["code"], exited["unexpected"]), (0, False))
        self.assertEqual(recorder.types()[-1], "exited")
        self.assertNoDriverThreads(session)
        session.close()
        self.assertEqual(len(recorder.of("exited")), 1)
        self.assertEqual(recorder.of("error"), [])

    def test_close_never_started_and_close_twice(self):
        session, recorder = self.make()
        session.close()
        session.close()
        with self.assertRaises(CodexSessionError):
            session.start()

    def test_close_with_a_running_turn_interrupts_it_and_reports_it(self):
        session, recorder = self.make(CLOSE_GRACE_SECONDS=2.0)
        session.start()
        message_uuid = self.send(session, "SLOW:30")
        recorder.wait("turn_started")
        deadline = time.time() + 5
        while session._turn_id is None and time.time() < deadline:
            time.sleep(0.02)
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 6)
        finished = recorder.wait("turn_finished")
        self.assertEqual(finished["message_uuids"], [message_uuid])
        self.assertIn(finished["status"], ("interrupted", "error"))
        exited = recorder.wait("exited")
        self.assertFalse(exited["unexpected"])
        self.assertEqual(recorder.of("error"), [])
        self.assertNoDriverThreads(session)

    def test_close_with_a_hung_server_is_bounded_and_leaves_nothing_behind(self):
        session, recorder = self.make(CLOSE_GRACE_SECONDS=0.4, TERMINATE_GRACE_SECONDS=0.4)
        session.start()
        self.send(session, "HANG")
        deadline = time.time() + 5
        while not self.pidfile.exists() and time.time() < deadline:
            time.sleep(0.02)
        child = int(self.pidfile.read_text())
        self.assertTrue(pid_alive(child))
        server = session.pid
        started = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - started, 8)
        self.assertFalse(pid_alive(server))
        end = time.time() + 3
        while pid_alive(child) and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(pid_alive(child))
        self.assertEqual(recorder.wait("exited")["unexpected"], False)
        self.assertNoDriverThreads(session)

    def test_a_grandchild_of_a_server_that_exits_by_itself_is_swept_at_close(self):
        session, recorder = self.make()
        session.start()
        self.send(session, "GRANDCHILD")
        recorder.wait("turn_finished")
        child = int(self.pidfile.read_text())
        self.assertTrue(pid_alive(child))
        session.close()
        end = time.time() + 3
        while pid_alive(child) and time.time() < end:
            time.sleep(0.05)
        self.assertFalse(pid_alive(child))

    def test_a_slow_consumer_does_not_block_the_reader_or_close(self):
        recorder = Recorder()
        recorder.hold = threading.Event()
        session, recorder = self.make(recorder=recorder)
        session.start()
        for index in range(3):
            self.send(session, "message %d" % index)
        time.sleep(0.5)
        started = time.monotonic()
        recorder.hold.set()
        session.close()
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(recorder.types()[-1], "exited")

    def test_a_consumer_that_raises_does_not_stop_the_session(self):
        calls = []

        def broken(event):
            calls.append(event["type"])
            raise RuntimeError("consumer bug")

        session = CodexSession(self.folder, self.cli, env={"FAKE_LOG": str(self.log), "FAKE_PIDFILE": str(self.pidfile)}, on_event=broken)
        self.sessions.append(session)
        session.start()
        self.send(session, "hello")
        end = time.time() + 5
        while "turn_finished" not in calls and time.time() < end:
            time.sleep(0.02)
        self.assertIn("turn_finished", calls)
        session.close()
        self.assertEqual(calls[-1], "exited")

    def test_secrets_in_prompts_and_environment_never_reach_events(self):
        session, recorder = self.make(env={"OPENAI_API_KEY": "sk-proj-ENVSECRET777"})
        session.start()
        self.send(session, "hello")
        recorder.wait("turn_finished")
        session.close()
        self.assertNotIn("ENVSECRET777", json.dumps(recorder.events))


class HostIntegrationTests(DriverCase):
    def test_the_host_contract_names_exist(self):
        for name in ("start", "send", "interrupt", "answer", "close", "pid", "session_id", "transcript_path", "busy"):
            self.assertTrue(hasattr(CodexSession, name), name)

    def test_driver_loads_by_the_host_path(self):
        from qorqut.managed_runtime import host
        self.assertIs(host.load_driver("qorqut.managed_runtime.codex_driver:CodexSession"), CodexSession)


class HelperTests(unittest.TestCase):
    def test_toml_values(self):
        self.assertEqual(codex_driver._toml_value({"a": [1, "x\"y"], "b": True}), '{"a" = [1, "x\\"y"], "b" = true}')
        with self.assertRaises(ValueError):
            codex_driver._toml_value(object())


if __name__ == "__main__":
    unittest.main()
