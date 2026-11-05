"""Synthetic native sessions for the native Chat checks: a real assembled host over temporary data, scripted process facts and
a stateful fake tmux pane. No agent, terminal client, model, provider or network is started and no operational data is read.

``Native`` owns one synthetic registered Global agent whose workspace lives under the test's temporary instance. Process
verification is injected (the files are real: the registration, the hook marker and the transcript); the pane is a small state
machine that renders an idle composer, shows pasted text, and on Enter appends a user record to the synthetic transcript,
which is what a real client does."""

import json
from pathlib import Path
from types import SimpleNamespace
import uuid

from host_foundation_support import build_instance, create_registered_global, create_worker, register_sources

from bazarlyk.native_chat import store
from bazarlyk.native_chat.adapters import CodexQueueAdapter, TerminalAdapter
from bazarlyk.native_chat.arbiter import AgentLocks, InputArbiter
from bazarlyk.native_chat.captures import Indexer
from bazarlyk.native_chat.chat import NativeChat
from bazarlyk.native_chat.probe import SessionProbe
from bazarlyk.native_chat.reader import ConversationReader
from bazarlyk.native_chat.service import NativeBackend
from bazarlyk.native_chat.sessions import SessionRegistry
from bazarlyk.transport.agent_terminal_control import AgentTerminalBackend

E = "\x1b"
DIM, RESET, INVERSE = f"{E}[2m", f"{E}[0m", f"{E}[7m"
HISTORY = "  The assistant explained the change.\n  It touched three files.\n"


def codex_screen(composer_row, *, above=""):
    footer = f"  {DIM}? for shortcuts{' ' * 30}100% context left{RESET}"
    return "\n".join([HISTORY.rstrip("\n"), "", above, composer_row, footer]) + "\n"


def claude_screen(inner, *, footer=f"  {DIM}? for shortcuts{RESET}"):
    rule = f"{DIM}{'─' * 60}{RESET}"
    return "\n".join([HISTORY.rstrip("\n"), "", rule, inner, rule, footer]) + "\n"


IDLE = {"claude": claude_screen(f"> {INVERSE}T{RESET}{DIM}ry \"fix lint errors\"{RESET}"),
        "codex": codex_screen(f"› {DIM}Ask Codex to do anything{RESET}")}
BUSY = {"claude": claude_screen(f"> {DIM}Try something{RESET}", footer="  ✻ Thinking… (esc to interrupt)"),
        "codex": codex_screen(f"› {DIM}Ask Codex to do anything{RESET}", above="• Working (12s • esc to interrupt)")}


def pasted_screen(provider, text):
    """The composer holding exactly the pasted literal, as a real client draws it: the prompt on the first row and the
    remaining lines indented below it."""
    first, *rest = text.split("\n")
    prompt = "> " if provider == "claude" else "› "
    inner = "\n".join([prompt + first, *("  " + line for line in rest)])
    return claude_screen(inner) if provider == "claude" else codex_screen(inner)


CURSOR = (2, 4, True)


class Pane:
    """A stateful fake tmux for one pane running a native client. ``busy`` is whether a turn is running; a pasted text becomes
    a user record in the client's transcript when Enter is sent, exactly once."""

    def __init__(self, native):
        self.native, self.calls, self.pasted, self.enter_works = native, [], None, True
        self.busy, self.inputs = False, []

    def screen(self):
        provider = self.native.provider
        if self.pasted is not None:
            return pasted_screen(provider, self.pasted)
        return BUSY[provider] if self.busy else IDLE[provider]

    def tmux(self, *args):
        self.calls.append(args)
        name = args[0]
        if name == "display-message":
            return 0, f"0|0|1|{CURSOR[0]}|{CURSOR[1]}"
        if name == "capture-pane":
            return 0, self.screen()
        if name == "load-buffer":
            self.loaded = Path(args[-1]).read_text()
            return 0, ""
        if name == "paste-buffer":
            self.pasted = self.loaded
            return 0, ""
        if name == "send-keys":
            if self.enter_works and self.pasted is not None:
                self.inputs.append(self.pasted)
                self.native.client_user_turn(self.pasted)
                self.pasted = None
            return 0, ""
        return 0, ""

    def typed(self):
        return list(self.inputs)


class Native:
    def __init__(self, tmp_path, provider="claude", *, capture=True, role=None, agent_factory=None):
        self.tmp_path = tmp_path
        self.provider = provider
        self.instance, self.businesses = build_instance(tmp_path)
        self.runtime = self.instance.runtime
        self.owner = self.instance.owner
        register_sources(self.instance, self.instance.business, "first")
        # One registered global agent; businesses have no resident agents. By default it is a global worker, whose runtime the
        # cases switch (an Ordubashy registration is Codex only); ``role`` names a singleton role instead, and a case that needs
        # another kind of agent supplies ``agent_factory(instance)`` and receives its identity as ``self.agent``.
        if agent_factory:
            self.agent = agent_factory(self.instance)
        elif role:
            self.agent = create_registered_global(self.instance, role)
        else:
            self.agent = create_worker(self.instance, "native-worker")
        with self.runtime.database.read() as uow:
            self.profile = store.active_profile(uow.connection, self.agent, self.runtime.paths)
        self.workspace = Path(self.profile["workspace"])
        (self.workspace / ".bazarlyk").mkdir(parents=True, exist_ok=True)
        self.write_registration(capture)
        self.runtime_name = {"claude": "claude-code", "codex": "codex"}[provider]
        self.set_runtime(self.runtime_name)
        self.pid, self.started = 4242, "99999"
        self.alive = True
        self.transport = "terminal"
        self.thread_owner = None
        self.session_id = str(uuid.uuid4())
        self.pane = Pane(self)
        self.queued = []
        self.codex_alive = True
        self.build()

    # ----- the assembled Chat graph with injected process facts ----------------------------------------------------

    def set_runtime(self, runtime):
        self.profile["profile_json"] = json.dumps({**json.loads(self.profile["profile_json"]), "runtime": runtime})
        with self.runtime.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (self.profile["profile_json"], self.agent))

    def steer_codex(self, session, text, checkpoint, **_):
        from bazarlyk.transport.agent_terminal_control import OwnerInputOutcome
        checkpoint()
        self.queued.append((["codex", "turn/steer", session["conversation_id"], text], {}))
        return OwnerInputOutcome("submitted", "processing_not_confirmed", {"method": "turn/steer"})

    def build(self, *, queue_run=None):
        backend = AgentTerminalBackend(SimpleNamespace(runtime=self.runtime), sleep=lambda seconds: None)
        backend.snapshot = lambda row, client=None: {
            "state": "present", "command": {"claude-code": "claude", "codex": "codex"}[json.loads(row["profile_json"])["runtime"]],
            "client": {"claude-code": "claude", "codex": "codex"}[json.loads(row["profile_json"])["runtime"]], "pane_id": "%3",
            "session_id": "$1", "foreground_pid": 10, "foreground_started": "1", "server_pid": "9", "pane_pid": "8"}
        backend.selected = lambda row: (self.pane, "key", "session", "cwd", None)
        self.terminal_backend = backend
        locks = AgentLocks()
        self.probe = SessionProbe(lambda: backend, process_probe=self.verify)
        self.registry = SessionRegistry(self.runtime, gone=lambda row: not self.alive)
        self.indexer = Indexer(self.runtime)
        codex = CodexQueueAdapter(run=queue_run or self.run_queue, alive=lambda owner: self.codex_alive,
                                  steering=self.steer_codex)
        self.arbiter = InputArbiter(self.runtime, self.probe, self.registry,
                                    {"terminal": TerminalAdapter(lambda: backend), "codex_queue": codex}, locks=locks)
        self.backend = NativeBackend(self.runtime, self.probe, self.registry, self.indexer, self.arbiter, locks)
        self.chat = NativeChat(self.runtime, probe=self.probe, registry=self.registry, indexer=self.indexer, arbiter=self.arbiter,
                               backend=self.backend, reader=ConversationReader())
        self.runtime.native_chat = self.chat

    def verify(self, profile, marker):
        if not self.alive:
            return None
        return {"transport": self.transport, "owner": self.thread_owner}

    def run_queue(self, argv, **options):
        self.queued.append((argv, options))
        return SimpleNamespace(returncode=0)

    # ----- hook-published facts ------------------------------------------------------------------------------------

    def write_registration(self, capture=True):
        path = self.workspace / ".bazarlyk/registration.json"
        path.write_text(json.dumps({"agent_id": self.agent, "capture_transcripts": capture}))

    def transcript_path(self):
        if self.provider == "claude":
            return self.workspace / "client-home" / f"{self.session_id}.jsonl"
        return self.workspace / "client-home" / f"rollout-2026-10-11T10-00-00-{self.session_id}.jsonl"

    def publish(self, event="SessionStart"):
        """What the native client's own hook writes: the exact conversation, transcript and process."""
        path = self.transcript_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        marker = {"agent_id": self.agent, "workspace": str(self.workspace), "session_id": self.session_id,
                  "runtime": self.runtime_name, "transcript_path": str(path), "event": event, "pid": self.pid,
                  "started": self.started}
        (self.workspace / ".bazarlyk/terminal-chat.json").write_text(json.dumps(marker))
        return marker

    def new_session(self, *, same_conversation=False, pid=None):
        self.alive = True
        if not same_conversation:
            self.session_id = str(uuid.uuid4())
        self.pid = pid if pid is not None else self.pid + 1
        self.started = str(int(self.started) + 1)
        self.pane = Pane(self) if not same_conversation else self.pane
        return self.publish()

    def end(self, event="SessionEnd"):
        self.alive = False
        self.publish(event)

    # ----- transcript growth ---------------------------------------------------------------------------------------

    def write(self, *records):
        self.transcript_path().parent.mkdir(parents=True, exist_ok=True)
        with open(self.transcript_path(), "ab") as handle:
            for record in records:
                handle.write((json.dumps(record) + "\n").encode())

    def client_user_turn(self, text):
        if self.provider == "claude":
            self.write(self.claude_user(text))
        else:
            self.write({"type": "event_msg", "timestamp": now(), "payload": {"type": "user_message", "message": text}})

    def claude_user(self, text, uuid_=None):
        return {"type": "user", "uuid": uuid_ or str(uuid.uuid4()), "sessionId": self.session_id, "cwd": str(self.workspace),
                "timestamp": now(),
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    def claude_assistant(self, text, *, stop="end_turn", message_id=None, uuid_=None, tool=False):
        blocks = [{"type": "text", "text": text}] if text else []
        if tool:
            blocks.append({"type": "tool_use", "id": "t1", "name": "Read", "input": {}})
        record = {"type": "assistant", "uuid": uuid_ or str(uuid.uuid4()), "sessionId": self.session_id, "cwd": str(self.workspace),
                  "timestamp": now(),
                  "message": {"id": message_id or "msg_" + uuid.uuid4().hex[:12], "role": "assistant", "stop_reason": stop,
                              "content": blocks}}
        return record

    def codex_session_meta(self):
        return {"type": "session_meta", "timestamp": now(), "payload": {"id": self.session_id, "cwd": str(self.workspace)}}

    def codex_assistant(self, text, *, final=False, native_id=None):
        payload = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
        if final:
            payload["phase"] = "final_answer"
        if native_id:
            payload["id"] = native_id
        return {"type": "response_item", "timestamp": now(), "payload": payload}

    def codex_event(self, kind, **extra):
        return {"type": "event_msg", "timestamp": now(), "payload": {"type": kind, **extra}}

    def owner_context(self):
        from qorqut_operations.contracts import InvocationContext, Principal
        from bazarlyk.agents.models import GLOBAL_CAPABILITIES
        return InvocationContext(Principal(self.owner, "human"), global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")

    def pin(self):
        """The ``binding_id`` of the agent's open binding, recorded the way the backend observer records it (nothing is typed):
        what a dashboard reads from ``state()["session"]["binding"]`` and sends back as ``expected_binding``."""
        self.registry.reconcile(self.profile, self.probe.observe(self.profile))
        return self.registry.current(self.agent)["id"]

    # ----- passes --------------------------------------------------------------------------------------------------

    def pass_(self, times=1):
        for _ in range(times):
            self.backend.run_once()


def now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def count(runtime, table, where="1=1", args=()):
    with runtime.database.read() as uow:
        return uow.connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def rows(runtime, sql, args=()):
    with runtime.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, args).fetchall()]


__all__ = ["Native", "count", "rows", "now"]
