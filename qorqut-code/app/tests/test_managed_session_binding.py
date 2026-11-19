"""Managed (headless) agents: identity, binding, liveness, delivery routing and the hook-side ``-p`` allowance.

Everything runs against fakes of the managed runtime (a duck-typed object with ``verify``/``snapshot``/``refresh``/
``drain_events``) and a real in-memory database holding the native Chat tables. The hook-side checks that decide whether a
``claude -p`` client may own inbox delivery also run against real processes (a fake host, a ``claude``-named shell), because a
filter that only passes on mocks proves nothing about the strictness it exists for. Nothing starts a real CLI.
"""
import isolated_cli_environment  # before any Qorqbot import: tests never touch the real user's CLI files

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.bootstrap import native_binding
from qorqut.bootstrap.bridge_binding import BridgeBinding
from qorqut.bootstrap.native_binding import MANAGED_PROBE, NativeBindingAdapter, ProcessTable, managed_host, runs_host_module
from qorqut.bootstrap.terminal_chat import process_fields
from qorqut.managed_runtime import mode as modes
from qorqut.native_chat import arbiter as arbiter_module, liveness, schema, selection, store
from qorqut.native_chat.arbiter import AgentLocks, InputArbiter, MANAGED_COMMAND, SESSION_CLOSED
from qorqut.native_chat.contracts import (InputRequest, RESULT_DEFERRED, RESULT_REJECTED, STATE_SUBMITTED, STATE_WAITING,
                                          TRANSPORTS, TYPE_BROADCAST, TYPE_CHAT, TYPE_TELEGRAM, managed_menu_command)
from qorqut.native_chat.probe import (Observation, ProbeResult, REASON_LIVE, REASON_NO_MARKER, REASON_PROCESS_GONE, SessionProbe)
from qorqut.native_chat.service import NativeBackend
from qorqut.native_chat.sessions import SessionRegistry
from qorqut.transport.agent_terminal_control import OwnerInputOutcome

AGENT, OWNER = "agent-1", "owner-1"
CONVERSATION = "11111111-1111-4111-8111-111111111111"
# The process checks read /proc on Linux and qorqut.process_info (psutil) on macOS; both are exercised.
LINUX = sys.platform.startswith(("linux", "darwin"))


def ticks(pid):
    return process_fields(pid)[19]


class Database:
    """A real in-memory database with the native Chat tables and the owner row table the selection reads."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        for statement in schema.SCHEMA:
            self.connection.execute(statement)
        self.connection.execute("CREATE TABLE native_session_owners(agent_id TEXT PRIMARY KEY, consumer_id TEXT NOT NULL, "
                                "binding_json TEXT NOT NULL, generation INTEGER NOT NULL, desired_state TEXT NOT NULL DEFAULT 'running')")

    @contextmanager
    def read(self):
        yield SimpleNamespace(connection=self.connection)

    @contextmanager
    def transaction(self, operation=None):
        with self.connection:
            yield SimpleNamespace(connection=self.connection)

    def count(self, table):
        return self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def adopt(self, pid, started, *, managed=True, conversation=CONVERSATION, owner="pid"):
        """The owner row the hook-side bootstrap writes for a verified native process."""
        binding = {"runtime": "claude-code", "pid": pid, "started": started,
                   "owner": f"{pid}:{started}" if owner == "pid" else owner}
        if managed:
            binding["managed_host"] = {"host_id": "h", "host_pid": 1, "host_ticks": "1"}
        self.connection.execute("INSERT OR REPLACE INTO native_session_owners VALUES(?,?,?,?, 'running')",
                                (AGENT, conversation, json.dumps(binding), 1))
        self.connection.commit()


class FakeManaged:
    """The managed runtime, as the probe and the backend use it."""

    def __init__(self, owner=None):
        self.owner, self.state = owner, {"phase": "idle", "activity": None, "pending": []}
        self.verified, self.refreshed, self.events, self.drained = [], [], [], 0
        self.changed = False
        self.client = None  # (pid, started) of the claude the host reports; by default the one in the marker it verified
        self.marked = None

    def identity(self, agent_id):
        client = self.client or self.marked
        if not (self.owner and client):
            return None
        return {"agent_id": agent_id, "pid": client[0], "started": client[1], "owner": self.owner, "conversation_id": CONVERSATION}

    def verify(self, profile, marker):
        self.verified.append(marker["pid"])
        self.marked = (marker["pid"], marker["started"])
        return {"transport": "managed", "owner": self.owner} if self.owner else None

    def snapshot(self, agent_id):
        return dict(self.state)

    def refresh(self, agent_id):
        self.refreshed.append(agent_id)
        changed, self.changed = self.changed, False
        return changed

    def drain_events(self, agent_id):
        self.drained += 1
        events, self.events = self.events, []
        return events


class Untouchable:
    """A managed runtime a terminal agent must never reach."""

    def __getattr__(self, name):
        raise AssertionError(f"a terminal agent touched the managed runtime ({name})")


def profile(execution="managed", workspace="/nonexistent/agent"):
    return {"agent_id": AGENT, "workspace": workspace, "profile_json": json.dumps({"runtime": "claude-code"}), "execution": execution}


def marker(pid, started):
    return {"agent_id": AGENT, "session_id": CONVERSATION, "runtime": "claude-code", "pid": pid, "started": started,
            "transcript_path": f"/nonexistent/{CONVERSATION}.jsonl", "capture": True, "capture_enabled": True}


class Sleeper:
    """A throwaway live process, for identities that have to be real."""

    def __init__(self, case):
        self.process = subprocess.Popen(["sleep", "60"])
        case.addCleanup(self.kill)
        self.pid = self.process.pid
        self.started = ticks(self.pid)

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


# ----- vocabulary and schema ------------------------------------------------------------------------------------------------

class VocabularyTests(unittest.TestCase):
    def test_managed_is_a_transport_and_the_schema_accepts_exactly_the_three(self):
        self.assertEqual(TRANSPORTS, ("terminal", "codex_queue", "managed"))
        db = Database()
        for number, transport in enumerate(("terminal", "codex_queue", "managed")):
            self.insert(db, f"s{number}", number + 1, transport)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(db, "s9", 9, "bogus")

    @staticmethod
    def insert(db, identity, generation, transport):
        db.connection.execute(
            "INSERT INTO native_sessions(id,agent_id,provider,conversation_id,generation,transport,process_pid,process_started,"
            "owner,observed_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (identity, AGENT, "claude", "c", generation, transport, 4242, "1", None, "now", "now"))

    def test_only_a_menu_only_first_word_is_a_managed_menu_command(self):
        for text in ("/clear", "/resume", "/cost now", "  /STATUS", "/model", "/model\n", "/help me", "/context"):
            with self.subTest(text=text):
                self.assertIsNotNone(managed_menu_command(text))
        for text in ("/compact", "/model sonnet", "/tmp/foo", "/clearing", "hello /clear", "/init", "/exit", "", "   ", "/custom"):
            with self.subTest(text=text):
                self.assertIsNone(managed_menu_command(text))


# ----- the execution switch is carried by the profile -------------------------------------------------------------------------

class ProfileExecutionTests(unittest.TestCase):
    ROW = {"agent_id": AGENT, "name": "n", "workspace": "/stored", "profile_json": "{}", "revision": 1, "home_business_id": None,
           "business_slug": None, "profile_status": "active", "principal_kind": "agent", "principal_active": 1}

    def profile(self, paths=object()):
        with mock.patch("qorqut.composition.locations.agent_workspace_of", return_value=Path("/ws")):
            return store._profile(self.ROW, paths)

    @staticmethod
    def mode_module(mode_of):
        return mock.patch.dict(sys.modules, {"qorqut.managed_runtime.mode": SimpleNamespace(mode_of=mode_of)})

    def test_the_mode_of_the_host_setting_becomes_execution(self):
        seen = []
        with self.mode_module(lambda paths, agent: seen.append((paths, agent)) or "managed"):
            self.assertEqual(self.profile("paths")["execution"], "managed")
        self.assertEqual(seen, [("paths", AGENT)])
        with self.mode_module(lambda paths, agent: "terminal"):
            self.assertEqual(self.profile()["execution"], "terminal")

    def test_a_missing_module_a_failing_read_or_an_odd_value_is_terminal(self):
        with mock.patch.dict(sys.modules, {"qorqut.managed_runtime.mode": None}):  # an import of it raises ImportError
            self.assertEqual(self.profile()["execution"], "terminal")

        def unreadable(paths, agent):
            raise OSError("unreadable")
        for mode_of in (unreadable, lambda paths, agent: "bogus", lambda paths, agent: None):
            with self.mode_module(mode_of):
                self.assertEqual(self.profile()["execution"], "terminal")

    def test_a_caller_without_paths_gets_no_execution_and_nothing_is_read(self):
        with self.mode_module(lambda paths, agent: self.fail("the mode must not be read without paths")):
            result = store._profile(self.ROW, None)
        self.assertNotIn("execution", result)
        self.assertEqual(result["workspace"], "/stored")


# ----- annotate_input ------------------------------------------------------------------------------------------------------

class AnnotateInputTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.session = self.make_session()

    def make_session(self):
        VocabularyTests.insert(self.db, "s1", 1, "managed")
        return store.get_session(self.db.connection, "s1")

    def submitted(self, key, message_uuid="uuid-1"):
        request = InputRequest(AGENT, TYPE_CHAT, store.chat_source_key(OWNER, key), "hello", wait=True, principal_id=OWNER)
        with self.db.transaction() as uow:
            row = store.insert_input(uow.connection, self.session, request, store.request_hash(request), state=STATE_WAITING)
            store.claim_input(uow.connection, row)
            store.settle_input(uow.connection, row["id"], STATE_SUBMITTED, "processing_confirmed",
                               {"adapter": "managed", "message_uuid": message_uuid}, ("submitting",))
        return row["id"]

    def counters(self):
        return dict(store.counters(self.db.connection, AGENT))

    def test_the_input_stays_submitted_and_gains_one_reason_one_event_and_one_revision(self):
        identifier = self.submitted("key-0001")
        before, events = self.counters(), len(store.input_events(self.db.connection, identifier))
        with self.db.transaction() as uow:
            self.assertTrue(store.annotate_input(uow.connection, identifier, "prompt_blocked", {"prompt_blocked": {"reason": "denied"}}))
        row = store.get_input(self.db.connection, identifier)
        self.assertEqual((row["state"], row["reason"]), (STATE_SUBMITTED, "prompt_blocked"))
        self.assertEqual(json.loads(row["evidence_json"]),
                         {"adapter": "managed", "message_uuid": "uuid-1", "prompt_blocked": {"reason": "denied"}})
        self.assertEqual(len(store.input_events(self.db.connection, identifier)), events + 1)
        last = store.input_events(self.db.connection, identifier)[-1]
        self.assertEqual((last["state"], last["reason"], json.loads(last["evidence_json"])),
                         (STATE_SUBMITTED, "prompt_blocked", {"prompt_blocked": {"reason": "denied"}}))
        after = self.counters()
        self.assertEqual(after["revision"], before["revision"] + 1)
        # Unlike a second settle, no user turn is counted again.
        self.assertEqual(after["user_sequence"], before["user_sequence"])
        self.assertEqual(store.input_view(row, None)["reason"], "prompt_blocked")

    def test_it_is_a_compare_and_set_and_never_repeats(self):
        identifier = self.submitted("key-0001")
        waiting = InputRequest(AGENT, TYPE_CHAT, store.chat_source_key(OWNER, "key-0002"), "x", wait=True, principal_id=OWNER)
        with self.db.transaction() as uow:
            other = store.insert_input(uow.connection, self.session, waiting, store.request_hash(waiting), state=STATE_WAITING)
        with self.db.transaction() as uow:
            self.assertFalse(store.annotate_input(uow.connection, other["id"], "prompt_blocked", {}))  # not submitted
            self.assertFalse(store.annotate_input(uow.connection, "missing", "prompt_blocked", {}))
            self.assertTrue(store.annotate_input(uow.connection, identifier, "prompt_blocked", {"a": 1}))
            revision = self.counters()["revision"]
            self.assertFalse(store.annotate_input(uow.connection, identifier, "prompt_blocked", {"a": 2}))
        self.assertEqual((self.counters()["revision"], len(store.input_events(self.db.connection, identifier))), (revision, 4))
        self.assertEqual(json.loads(store.get_input(self.db.connection, identifier)["evidence_json"])["a"], 1)

    def test_the_input_is_found_by_the_message_uuid_of_its_receipt(self):
        first, second = self.submitted("key-0001", "uuid-1"), self.submitted("key-0002", "uuid-2")
        self.assertEqual(store.input_by_message_uuid(self.db.connection, AGENT, "uuid-2")["id"], second)
        self.assertEqual(store.input_by_message_uuid(self.db.connection, AGENT, "uuid-1")["id"], first)
        self.assertIsNone(store.input_by_message_uuid(self.db.connection, AGENT, "uuid-3"))
        self.assertIsNone(store.input_by_message_uuid(self.db.connection, "another-agent", "uuid-1"))


# ----- probe ---------------------------------------------------------------------------------------------------------------

class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.sleeper = Sleeper(self)
        self.host = "host:%d:%s" % (self.sleeper.pid + 1, "77")

    def probe(self, managed, mark=None):
        mark = mark or marker(self.sleeper.pid, self.sleeper.started)
        return SessionProbe(marker_reader=lambda profile: dict(mark), managed=managed)

    def test_a_verified_host_makes_the_marker_a_managed_observation(self):
        managed = FakeManaged(self.host)
        managed.state = {"phase": "working", "activity": "Reading", "pending": [{"request_id": "r1", "kind": "permission"}]}
        result = self.probe(managed).observe(profile())
        self.assertEqual(result.reason, REASON_LIVE)
        seen = result.observation
        self.assertEqual((seen.transport, seen.owner, seen.pid, seen.started, seen.conversation_id),
                         ("managed", self.host, self.sleeper.pid, self.sleeper.started, CONVERSATION))
        self.assertEqual((seen.working, seen.phase, seen.activity, seen.pending),
                         (True, "working", "Reading", ({"request_id": "r1", "kind": "permission"},)))
        self.assertEqual(managed.verified, [self.sleeper.pid])

    def test_the_phase_decides_working_and_an_idle_host_is_not_working(self):
        for phase, working in (("idle", False), ("working", True), ("blocked", True), ("starting", False), ("closing", False), (None, False)):
            managed = FakeManaged(self.host)
            managed.state = {"phase": phase}
            with self.subTest(phase=phase):
                self.assertEqual(self.probe(managed).observe(profile()).observation.working, working)

    def test_an_unverified_host_means_no_session(self):
        # The marker names a live process, but no host the runtime can verify drives it, and there is no pane either.
        managed = FakeManaged(None)
        result = self.probe(managed).observe(profile())
        self.assertEqual(result.reason, REASON_PROCESS_GONE)
        self.assertEqual(managed.verified, [self.sleeper.pid])

    def test_a_host_that_cannot_be_asked_is_not_a_crash(self):
        managed = FakeManaged(self.host)
        managed.verify = mock.Mock(side_effect=RuntimeError("socket"))
        self.assertEqual(self.probe(managed).observe(profile()).reason, REASON_PROCESS_GONE)
        managed = FakeManaged(self.host)
        managed.snapshot = mock.Mock(side_effect=RuntimeError("socket"))
        seen = self.probe(managed).observe(profile()).observation
        self.assertEqual((seen.transport, seen.phase, seen.pending, seen.working), ("managed", None, (), False))

    def test_a_dead_or_recycled_client_is_never_verified_by_a_host(self):
        managed = FakeManaged(self.host)
        result = self.probe(managed, marker(self.sleeper.pid, str(int(self.sleeper.started) + 1))).observe(profile())
        self.assertEqual(result.reason, REASON_PROCESS_GONE)
        self.assertEqual(managed.verified, [])

    def test_the_hook_activity_file_of_the_client_process_still_says_working(self):
        # The hook names its client process as the owner of the activity file; a managed observation's own owner is the host.
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve() / "agent"
            (workspace / ".qorqut/activity").mkdir(parents=True)
            activity = {"agent_id": AGENT, "session_id": CONVERSATION, "runtime": "claude-code", "state": "running",
                        "owner": f"{self.sleeper.pid}:{self.sleeper.started}"}
            (workspace / ".qorqut/activity" / (CONVERSATION + ".json")).write_text(json.dumps(activity))
            managed = FakeManaged(self.host)
            self.assertTrue(self.probe(managed).observe(profile(workspace=str(workspace))).observation.working)
            activity["owner"] = self.host  # a file that names the host is not the client's own
            (workspace / ".qorqut/activity" / (CONVERSATION + ".json")).write_text(json.dumps(activity))
            self.assertFalse(self.probe(managed).observe(profile(workspace=str(workspace))).observation.working)

    def test_a_descendant_of_the_host_that_is_not_the_client_the_host_reports_is_refused(self):
        # ``verify`` only proves descent from the host; the marker is a file the agent writes and any process it starts descends.
        other = Sleeper(self)
        for name, change in {"another process": lambda m: setattr(m, "client", (other.pid, other.started)),
                             "recycled ticks": lambda m: setattr(m, "client", (self.sleeper.pid, str(int(self.sleeper.started) + 1))),
                             "no identity": lambda m: setattr(m, "identity", lambda agent_id: None),
                             "no identity method": lambda m: setattr(m, "identity", None),
                             "another host": lambda m: setattr(m, "identity", lambda agent_id: {
                                 "pid": self.sleeper.pid, "started": self.sleeper.started, "owner": "host:9:9"}),
                             "another conversation": lambda m: setattr(m, "identity", lambda agent_id: {
                                 "pid": self.sleeper.pid, "started": self.sleeper.started, "owner": self.host, "conversation_id": "x"}),
                             "raises": lambda m: setattr(m, "identity", mock.Mock(side_effect=RuntimeError("socket")))}.items():
            managed = FakeManaged(self.host)
            managed.client = (self.sleeper.pid, self.sleeper.started)
            change(managed)
            with self.subTest(name):
                self.assertEqual(self.probe(managed).observe(profile()).reason, REASON_PROCESS_GONE)
        managed = FakeManaged(self.host)
        managed.client = (self.sleeper.pid, self.sleeper.started)
        self.assertEqual(self.probe(managed).observe(profile()).reason, REASON_LIVE)

    def test_a_terminal_agent_never_reaches_the_managed_runtime(self):
        for execution in ("terminal", None):
            with self.subTest(execution=execution):
                value = profile()
                value.pop("execution")
                if execution:
                    value["execution"] = execution
                result = self.probe(Untouchable()).observe(value)
                # No pane backend either: exactly what a terminal agent without tmux reported before.
                self.assertEqual(result.reason, REASON_PROCESS_GONE)
        self.assertEqual(SessionProbe(marker_reader=lambda p: marker(self.sleeper.pid, self.sleeper.started)).observe(profile()).reason,
                         REASON_PROCESS_GONE)

    def test_a_terminal_observation_carries_no_managed_fields(self):
        seen = Observation(AGENT, "claude-code", "claude", CONVERSATION, None, 1, "1", "terminal", None)
        self.assertEqual((seen.phase, seen.activity, seen.pending), (None, None, ()))


# ----- liveness ------------------------------------------------------------------------------------------------------------

class LivenessTests(unittest.TestCase):
    def row(self, client, host, owner=None):
        return {"transport": "managed", "process_pid": client.pid, "process_started": client.started,
                "owner": owner if owner is not None else f"host:{host.pid}:{host.started}"}

    def test_alive_only_while_the_client_and_its_host_both_are(self):
        client, host = Sleeper(self), Sleeper(self)
        self.assertEqual(liveness.of_session(self.row(client, host)), liveness.ALIVE)
        host.kill()
        # The client alone is not enough: a client whose host is gone cannot be driven.
        self.assertEqual(liveness.of_session(self.row(client, host)), liveness.DEAD)
        self.assertTrue(liveness.state(client.pid, client.started) == liveness.ALIVE)

    def test_the_death_of_the_client_alone_also_ends_it(self):
        client, host = Sleeper(self), Sleeper(self)
        client.kill()
        self.assertEqual(liveness.of_session(self.row(client, host)), liveness.DEAD)

    def test_a_recycled_host_pid_is_a_dead_host(self):
        client, host = Sleeper(self), Sleeper(self)
        row = self.row(client, host, f"host:{host.pid}:{int(host.started) + 1}")
        self.assertEqual(liveness.of_session(row), liveness.DEAD)

    def test_an_unreadable_host_is_unknown_never_dead(self):
        client, host = Sleeper(self), Sleeper(self)
        for owner in (None, "", "host:12", "pid:1:2", f"host:{host.pid}", "host:x:1", f"{host.pid}:{host.started}"):
            row = self.row(client, host, owner)
            row["owner"] = owner
            with self.subTest(owner=owner):
                self.assertEqual(liveness.of_session(row), liveness.UNKNOWN)
        # Verifiable death of the client outranks an unreadable owner.
        client.kill()
        self.assertEqual(liveness.of_session({**self.row(client, host), "owner": None}), liveness.DEAD)

    def test_the_other_transports_are_judged_as_before(self):
        client = Sleeper(self)
        for transport in ("terminal",):
            row = {"transport": transport, "process_pid": client.pid, "process_started": client.started, "owner": None}
            self.assertEqual(liveness.of_session(row), liveness.ALIVE)
        self.assertEqual(liveness.of_session({"transport": "codex_queue", "process_pid": 1, "process_started": "1", "owner": None}),
                         liveness.UNKNOWN)
        client.kill()
        self.assertEqual(liveness.of_session({"transport": "terminal", "process_pid": client.pid, "process_started": client.started,
                                              "owner": None}), liveness.DEAD)
        self.assertEqual(liveness.worst(liveness.ALIVE, liveness.UNKNOWN), liveness.UNKNOWN)
        self.assertEqual(liveness.worst(liveness.UNKNOWN, liveness.DEAD), liveness.DEAD)


# ----- selection -----------------------------------------------------------------------------------------------------------

class SelectionTests(unittest.TestCase):
    def chosen(self, **binding):
        base = {"runtime": "claude-code", "pid": 42, "started": "99", "owner": "42:99"}
        return selection.Selection(AGENT, CONVERSATION, "claude", 1, "running", {**base, **binding})

    @staticmethod
    def observation(transport, owner, pid=42, started="99"):
        return SimpleNamespace(provider="claude", conversation_id=CONVERSATION, pid=pid, started=started, transport=transport, owner=owner)

    def test_a_managed_observation_is_held_by_the_process_the_owner_row_was_adopted_for(self):
        host = "host:7:5"
        for owner in ("42:99", None, host):
            with self.subTest(owner=owner):
                chosen = self.chosen(owner=owner) if owner else self.chosen(owner=None)
                self.assertTrue(chosen.holds(self.observation("managed", host)))
                self.assertEqual(chosen.relation(self.observation("managed", host)), selection.HELD)

    def test_a_managed_observation_of_another_process_or_owner_is_not_held(self):
        host = "host:7:5"
        self.assertFalse(self.chosen().holds(self.observation("managed", host, pid=43)))
        self.assertFalse(self.chosen().holds(self.observation("managed", host, started="100")))
        self.assertFalse(self.chosen(owner="9:9").holds(self.observation("managed", host)))
        self.assertEqual(self.chosen().relation(self.observation("managed", host, pid=43)), selection.PENDING)

    def test_terminal_rules_are_unchanged(self):
        chosen = self.chosen()
        self.assertTrue(chosen.holds(self.observation("terminal", None)))
        self.assertFalse(chosen.holds(self.observation("terminal", "host:7:5")))  # a terminal never has a host owner
        self.assertFalse(chosen.holds(self.observation("terminal", None, pid=43)))
        self.assertTrue(self.chosen(owner=None).holds(self.observation("terminal", None)))

    def test_public_names_a_managed_selection_only_when_the_binding_says_so(self):
        self.assertEqual(self.chosen().public()["transport"], "terminal")
        self.assertEqual(self.chosen(managed_host={"host_pid": 7}).public()["transport"], "managed")
        self.assertTrue(self.chosen(managed_host={"host_pid": 7}).managed)
        self.assertFalse(self.chosen(managed_host="x").managed)


# ----- binding through the registry, delivery through the arbiter --------------------------------------------------------------

class Adapter:
    def __init__(self, name):
        self.name, self.typed, self.steer = name, [], []

    def submit(self, profile, session, text, *, steer, echo, checkpoint, replace_composer=False):
        checkpoint()
        self.typed.append(text)
        self.steer.append(steer)
        return OwnerInputOutcome("submitted", "processing_confirmed", {"message_uuid": "u-%d" % len(self.typed)})


class ManagedBindingTests(unittest.TestCase):
    """A fake host plus a fake marker yield a live managed binding; the arbiter then hands it to the ``managed`` adapter."""

    def setUp(self):
        isolated_cli_environment.isolate(self)
        self.client = Sleeper(self)
        self.host = f"host:{os.getpid()}:{ticks(os.getpid())}"
        self.db = Database()
        self.runtime = SimpleNamespace(database=self.db, paths=None, application=SimpleNamespace(ensure_writes_open=lambda uow: None))
        self.managed = FakeManaged(self.host)
        self.probe = SessionProbe(marker_reader=lambda p: marker(self.client.pid, self.client.started), managed=self.managed)
        self.registry = SessionRegistry(self.runtime)
        self.adapters = {"managed": Adapter("managed"), "terminal": Adapter("terminal"), "codex_queue": Adapter("codex_queue")}
        self.arbiter = InputArbiter(self.runtime, self.probe, self.registry, self.adapters)
        self.profile = profile()
        for patcher in (mock.patch.object(InputArbiter, "_profile", lambda arbiter, agent_id: self.profile),
                        mock.patch.object(arbiter_module, "forward_owner_message", mock.Mock()),
                        mock.patch("qorqut.management.identity.configured_ordubashy", return_value=None),
                        mock.patch("qorqut.agents.attention.enrolled_board", return_value=False),
                        mock.patch("qorqut.management.identity.installation_owner", return_value=OWNER)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def reconcile(self):
        return self.registry.reconcile(self.profile, self.probe.observe(self.profile))

    def test_a_managed_observation_binds_when_the_owner_row_was_adopted_for_that_process(self):
        self.db.adopt(self.client.pid, self.client.started)
        result = self.reconcile()
        self.assertTrue(result.live)
        self.assertTrue(result.created)
        self.assertEqual((result.session["transport"], result.session["owner"], result.session["process_pid"],
                          result.session["conversation_id"]), ("managed", self.host, self.client.pid, CONVERSATION))
        self.assertEqual(result.binding.public()["transport"], "managed")
        # The same host and client again is the same binding, not a new generation.
        again = self.reconcile()
        self.assertEqual((again.live, again.created, again.session["id"]), (True, False, result.session["id"]))

    def test_nothing_binds_before_the_owner_row_adopts_the_client(self):
        self.assertFalse(self.reconcile().live)  # no owner row at all: the observation is only a candidate
        self.db.adopt(self.client.pid + 1, "1")  # adopted for another process
        self.assertFalse(self.reconcile().live)
        self.assertIsNone(store.live_session(self.db.connection, AGENT))

    def test_a_new_host_for_the_same_conversation_is_a_new_generation(self):
        self.db.adopt(self.client.pid, self.client.started)
        first = self.reconcile().session
        self.managed.owner = f"host:{os.getpid()}:{int(ticks(os.getpid())) + 1}"
        # The first host is verifiably gone only when liveness says so; here the registry is told it is.
        self.registry._gone = lambda row: True
        second = self.reconcile()
        self.assertTrue(second.created)
        self.assertEqual((second.session["generation"], second.ended[0]["id"]), (first["generation"] + 1, first["id"]))

    def test_the_binding_ends_when_the_host_is_gone_even_though_the_client_lives(self):
        host = Sleeper(self)
        self.managed.owner = f"host:{host.pid}:{host.started}"
        self.db.adopt(self.client.pid, self.client.started)
        bound = self.reconcile()
        self.assertTrue(bound.live)
        host.kill()
        self.managed.owner = None  # the runtime can no longer verify a host
        ended = self.registry.reconcile(self.profile, self.probe.observe(self.profile))
        self.assertEqual((ended.live, ended.session, [row["end_reason"] for row in ended.ended]), (False, None, ["process_gone"]))

    def test_the_binding_survives_a_host_that_is_merely_unverifiable_for_a_moment(self):
        host = Sleeper(self)
        self.managed.owner = f"host:{host.pid}:{host.started}"
        self.db.adopt(self.client.pid, self.client.started)
        self.assertTrue(self.reconcile().live)
        self.managed.owner = None  # the runtime cannot verify it now, but the host process is still there
        held = self.reconcile()
        self.assertEqual((held.live, held.session is not None, held.ended), (False, True, []))

    def test_the_arbiter_dispatches_a_managed_session_to_the_managed_adapter(self):
        self.db.adopt(self.client.pid, self.client.started)
        self.reconcile()
        result = self.arbiter.submit(self.request("hello", TYPE_TELEGRAM, key="tg-1", wait=False))
        self.assertEqual((result.state, result.reason), (STATE_SUBMITTED, "processing_confirmed"))
        self.assertEqual((self.adapters["managed"].typed, self.adapters["terminal"].typed), (["hello"], []))
        self.assertEqual(result.evidence, {"adapter": "managed", "message_uuid": "u-1"})
        self.assertEqual(result.binding.transport, "managed")
        # A Chat input waits and is dispatched to the same adapter, steering as Chat does.
        waiting = self.arbiter.submit(self.request("later", TYPE_CHAT, key="chat-1"))
        self.assertEqual(waiting.state, STATE_WAITING)
        self.assertTrue(self.arbiter.dispatch(AGENT))
        self.assertEqual((self.adapters["managed"].typed, self.adapters["managed"].steer), (["hello", "later"], [False, True]))
        self.assertEqual(self.adapters["terminal"].typed, [])

    @staticmethod
    def request(text, input_type, *, key, wait=True):
        source = store.chat_source_key(OWNER, key) if input_type == TYPE_CHAT else key
        return InputRequest(AGENT, input_type, source, text, wait=wait, principal_id=OWNER if input_type == TYPE_CHAT else None,
                            steer=input_type == TYPE_CHAT)

    # ----- what the arbiter says when a managed agent has no session -----------------------------------------------------

    def test_a_managed_agent_without_a_session_is_closed_not_offline(self):
        self.managed.owner = None
        for reason in (REASON_NO_MARKER, REASON_PROCESS_GONE):
            observed = ProbeResult(reason)
            with self.subTest(reason=reason):
                self.arbiter.probe = SimpleNamespace(observe=lambda p, observed=observed: observed)
                attempt = self.arbiter.submit(self.request("hi", TYPE_TELEGRAM, key="tg-%s" % reason, wait=False))
                self.assertEqual((attempt.state, attempt.reason), (RESULT_DEFERRED, SESSION_CLOSED))
                chat = self.arbiter.submit(self.request("hi", TYPE_CHAT, key="chat-%s" % reason))
                self.assertEqual((chat.state, chat.reason), (RESULT_REJECTED, SESSION_CLOSED))
        self.assertEqual(self.db.count("native_inputs"), 0)

    def test_a_terminal_agent_without_a_session_is_still_offline(self):
        for build in (lambda: profile("terminal"), lambda: {k: v for k, v in profile().items() if k != "execution"}):
            self.profile = build()
            self.arbiter.probe = SimpleNamespace(observe=lambda p: ProbeResult(REASON_NO_MARKER))
            attempt = self.arbiter.submit(self.request("hi", TYPE_TELEGRAM, key="tg-%s" % len(self.profile), wait=False))
            self.assertEqual((attempt.state, attempt.reason), (RESULT_DEFERRED, "terminal_offline"))
            chat = self.arbiter.submit(self.request("hi", TYPE_CHAT, key="chat-%s" % len(self.profile)))
            self.assertEqual((chat.state, chat.reason), (RESULT_REJECTED, "terminal_offline"))

    def test_the_other_unavailable_reasons_are_not_remapped(self):
        for reason, code in (("capture_disabled", "capture_disabled"), ("native_only", "unsupported_target"),
                             ("unsupported_runtime", "unsupported_runtime")):
            result = InputArbiter._unavailable(self.request("x", TYPE_TELEGRAM, key="k", wait=False), ProbeResult(reason), managed=True)
            self.assertEqual((result.state, result.reason), (RESULT_REJECTED, code))

    # ----- menu-only commands --------------------------------------------------------------------------------------------

    def test_a_menu_only_command_is_refused_for_a_managed_session_before_anything_is_recorded(self):
        self.db.adopt(self.client.pid, self.client.started)
        self.reconcile()
        for index, text in enumerate(("/clear", "/model", "/resume", "/cost", " /STATUS now", "/help")):
            with self.subTest(text=text):
                result = self.arbiter.submit(self.request(text, TYPE_CHAT, key=f"cmd-{index:04d}"))
                self.assertEqual((result.state, result.reason, result.input_id), (RESULT_REJECTED, MANAGED_COMMAND, None))
        self.assertEqual((self.db.count("native_inputs"), self.db.count("native_input_events")), (0, 0))
        self.assertEqual(self.adapters["managed"].typed, [])
        self.assertEqual(result.binding.transport, "managed")

    def test_everything_else_is_admitted_and_only_owner_chat_is_refused(self):
        self.db.adopt(self.client.pid, self.client.started)
        self.reconcile()
        for index, text in enumerate(("/compact", "/model sonnet", "/tmp/foo", "/init", "hello")):
            result = self.arbiter.submit(self.request(text, TYPE_CHAT, key=f"ok-{index:04d}"))
            self.assertEqual((result.state, result.created), (STATE_WAITING, True), text)
        for input_type in (TYPE_TELEGRAM, TYPE_BROADCAST):
            result = self.arbiter.submit(self.request("/clear", input_type, key=f"other-{input_type}", wait=False))
            self.assertEqual((result.state, result.reason), (STATE_SUBMITTED, "processing_confirmed"))

    def test_a_terminal_session_still_takes_every_command(self):
        VocabularyTests.insert(self.db, "terminal-session", 1, "terminal")
        self.db.connection.execute("UPDATE native_sessions SET transcript_path='/x'")
        row = store.get_session(self.db.connection, "terminal-session")
        self.registry.reconcile = lambda profile, observed: SimpleNamespace(session=row, live=True)
        self.arbiter.probe = SimpleNamespace(observe=lambda p: ProbeResult(REASON_LIVE, SimpleNamespace(capture_enabled=True)))
        for index, text in enumerate(("/clear", "/model", "/resume")):
            result = self.arbiter.submit(self.request(text, TYPE_CHAT, key=f"term-{index:04d}"))
            self.assertEqual((result.state, result.created), (STATE_WAITING, True), text)


# ----- the backend pass --------------------------------------------------------------------------------------------------

class BackendTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        VocabularyTests.insert(self.db, "s1", 1, "managed")
        self.session = {**store.get_session(self.db.connection, "s1"), "transcript_path": None}
        self.runtime = SimpleNamespace(database=self.db, paths=None, authority=None,
                                       application=SimpleNamespace(ensure_writes_open=lambda uow: None))
        self.managed = FakeManaged("host:1:1")
        self.current = Observation(AGENT, "claude-code", "claude", CONVERSATION, None, 42, "99", "managed", "host:1:1")
        self.probe = SimpleNamespace(observe=lambda p: SimpleNamespace(reason="live", observation=self.current))
        self.registry = SimpleNamespace(reconcile=lambda p, observed: SimpleNamespace(session=self.session, live=True))
        self.arbiter = SimpleNamespace(wake=None, dispatch=mock.Mock(return_value=False))
        self.backend = NativeBackend(self.runtime, self.probe, self.registry, SimpleNamespace(step=mock.Mock()), self.arbiter,
                                     AgentLocks(), managed=self.managed)

    def revision(self):
        return store.revision(self.db.connection, AGENT)

    def settle_submitted(self, message_uuid="uuid-1", key="key-0001"):
        request = InputRequest(AGENT, TYPE_CHAT, store.chat_source_key(OWNER, key), "hello", wait=True, principal_id=OWNER)
        with self.db.transaction() as uow:
            row = store.insert_input(uow.connection, self.session, request, store.request_hash(request), state=STATE_WAITING)
            store.claim_input(uow.connection, row)
            store.settle_input(uow.connection, row["id"], STATE_SUBMITTED, "processing_confirmed",
                               {"adapter": "managed", "message_uuid": message_uuid}, ("submitting",))
        return row["id"]

    def test_a_managed_pass_refreshes_the_host_and_publishes_a_change_of_phase(self):
        self.backend.observe(profile())
        self.assertEqual(self.managed.refreshed, [AGENT])
        first = self.revision()
        self.backend.observe(profile())
        self.assertEqual(self.revision(), first)  # nothing changed
        self.current = Observation(**{**self.current.__dict__, "phase": "working", "working": True})
        self.backend.observe(profile())
        self.assertEqual(self.revision(), first + 1)
        self.current = Observation(**{**self.current.__dict__, "pending": ({"request_id": "r1"},)})
        self.backend.observe(profile())
        self.assertEqual(self.revision(), first + 2)

    def test_activity_text_alone_is_published_at_most_once_a_second(self):
        with mock.patch("qorqut.native_chat.service.time.monotonic") as clock:
            clock.return_value = 900.0
            self.backend.observe(profile())
            base = self.revision()
            clock.return_value = 1000.0
            self.current = Observation(**{**self.current.__dict__, "activity": "Reading a"})
            self.backend.observe(profile())
            self.assertEqual(self.revision(), base + 1)
            for step, text in ((1000.2, "Reading b"), (1000.4, "Reading c")):
                clock.return_value = step
                self.current = Observation(**{**self.current.__dict__, "activity": text})
                self.backend.observe(profile())
            self.assertEqual(self.revision(), base + 1)  # held back
            clock.return_value = 1001.1
            self.backend.observe(profile())
            self.assertEqual(self.revision(), base + 2)  # the held-back change is published on a later pass
            clock.return_value = 1001.2
            self.backend.observe(profile())
            self.assertEqual(self.revision(), base + 2)

    def test_a_change_the_host_reports_without_a_visible_difference_is_still_published(self):
        with mock.patch("qorqut.native_chat.service.time.monotonic") as clock:
            clock.return_value = 5000.0
            self.backend.observe(profile())
            base = self.revision()
            self.managed.changed = True
            clock.return_value = 5001.5
            self.backend.observe(profile())
        self.assertEqual(self.revision(), base + 1)

    def test_a_blocked_prompt_is_recorded_on_the_input_that_carried_it(self):
        identifier = self.settle_submitted("uuid-1")
        self.managed.events = [{"type": "turn_started", "message_uuid": "uuid-1"},
                               {"type": "prompt_blocked", "message_uuid": "uuid-1", "reason": "hook said no"}]
        self.backend.observe(profile())
        row = store.get_input(self.db.connection, identifier)
        self.assertEqual((row["state"], row["reason"]), (STATE_SUBMITTED, "prompt_blocked"))
        self.assertEqual(json.loads(row["evidence_json"])["prompt_blocked"], {"reason": "hook said no"})
        self.backend.observe(profile())  # the report is not applied twice
        self.assertEqual(len(store.input_events(self.db.connection, identifier)), 4)

    def test_events_wrapped_by_a_log_sequence_are_understood(self):
        identifier = self.settle_submitted("uuid-1")
        self.managed.events = [{"seq": 5, "event": {"type": "prompt_blocked", "message_uuid": "uuid-1", "reason": "x"}}]
        self.backend.observe(profile())
        self.assertEqual(store.get_input(self.db.connection, identifier)["reason"], "prompt_blocked")

    def test_a_report_that_arrives_before_its_input_is_settled_waits_for_it(self):
        self.managed.events = [{"type": "prompt_blocked", "message_uuid": "uuid-1", "reason": "late"}]
        self.backend.observe(profile())
        identifier = self.settle_submitted("uuid-1")
        self.backend.observe(profile())
        self.assertEqual(store.get_input(self.db.connection, identifier)["reason"], "prompt_blocked")

    def test_a_report_for_nothing_is_dropped_after_a_bounded_wait(self):
        from qorqut.native_chat import service
        self.managed.events = [{"type": "prompt_blocked", "message_uuid": "nobody", "reason": "x"}]
        for _ in range(service.BLOCKED_PASSES + 1):
            self.backend.observe(profile())
        self.assertEqual(self.backend._blocked[AGENT], {})

    def test_a_host_that_fails_does_not_stop_the_pass(self):
        self.managed.refresh = mock.Mock(side_effect=RuntimeError("socket"))
        self.managed.drain_events = mock.Mock(side_effect=RuntimeError("socket"))
        self.backend.observe(profile())
        self.arbiter.dispatch.assert_not_called()

    def test_a_terminal_agent_is_observed_exactly_as_before(self):
        backend = NativeBackend(self.runtime, self.probe, self.registry, SimpleNamespace(step=mock.Mock()), self.arbiter, AgentLocks(),
                                managed=Untouchable())
        self.current = Observation(AGENT, "claude-code", "claude", CONVERSATION, None, 42, "99", "terminal", None, working=True)
        base = self.revision()
        for value in (profile("terminal"), {k: v for k, v in profile().items() if k != "execution"}):
            backend.observe(value)
        self.assertEqual(self.revision(), base + 1)  # the working flag, once, as always
        self.assertEqual(backend._working[AGENT], (self.session["id"], True))
        self.assertEqual(NativeBackend(self.runtime, self.probe, self.registry, None, self.arbiter, AgentLocks()).managed, None)


# ----- the hook-side ``-p`` allowance --------------------------------------------------------------------------------------

class FakeProcesses:
    """A process table for ``managed_host``: pid -> (state, parent, ticks, comm, argv)."""

    def __init__(self, table):
        self.table = table

    def fields(self, pid):
        state, parent, started, _, _ = self.table[pid]
        values = [state, str(parent)] + ["0"] * 17 + [started]
        return values

    def comm(self, pid):
        return self.table[pid][3]

    def argv(self, pid):
        return self.table[pid][4]


HOST_ARGV = [b"/usr/bin/python3", b"-B", b"-m", b"qorqut.managed_runtime.host", b"--state", b"/runtime/managed-agents/a"]


def process(parent, started="100", comm="bash", argv=(), state="S"):
    return (state, parent, started, comm, list(argv))


class ManagedHostTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name).resolve() / "agent"
        (self.workspace / ".qorqut").mkdir(parents=True)
        self.record = {"host_id": "host:500:555", "host_pid": 500, "host_ticks": "555", "agent_id": AGENT}
        self.write(self.record)

    def write(self, value):
        (self.workspace / ".qorqut/managed-host.json").write_text(json.dumps(value))

    def table(self, overrides=None):
        table = {500: process(1, "555", "python3", HOST_ARGV), 600: process(500, "601", "claude", [b"claude", b"-p"])}
        table.update(overrides or {})
        return FakeProcesses(table)

    def host(self, processes, pid=600, agent=AGENT):
        return managed_host(self.workspace, pid, agent, processes=processes)

    def test_a_client_that_is_the_child_of_the_recorded_host_is_accepted(self):
        self.assertEqual(self.host(self.table()), {"host_id": "host:500:555", "host_pid": 500, "host_ticks": "555"})

    def test_intermediate_processes_that_are_not_a_claude_are_allowed(self):
        table = {500: process(1, "555", "python3", HOST_ARGV), 550: process(500, "560", "env"), 600: process(550, "601", "claude", [b"-p"])}
        self.assertEqual(self.host(FakeProcesses(table))["host_pid"], 500)

    def test_a_claude_worker_started_from_inside_the_managed_session_is_refused(self):
        # host -> managed claude -> bash -> claude -p: the worker's nearest claude ancestor is not a child of the host.
        table = {500: process(1, "555", "python3", HOST_ARGV), 600: process(500, "601", "claude", [b"-p"]),
                 650: process(600, "651", "bash"), 700: process(650, "701", "claude", [b"claude", b"-p"])}
        processes = FakeProcesses(table)
        self.assertIsNotNone(self.host(processes, 600))
        self.assertIsNone(self.host(processes, 700))

    def test_a_plain_claude_with_no_host_ancestor_is_refused(self):
        processes = self.table()
        processes.table[800] = process(1, "801", "claude", [b"claude", b"-p"])
        self.assertIsNone(self.host(processes, 800))
        processes.table[900] = process(1500, "901", "claude", [b"claude", b"-p"])  # a parent that is not even in the table
        self.assertIsNone(self.host(processes, 900))

    def test_every_part_of_the_host_identity_is_checked(self):
        cases = {
            "recycled host pid": {500: process(1, "556", "python3", HOST_ARGV)},
            "zombie host": {500: process(1, "555", "python3", HOST_ARGV, state="Z")},
            "not the host module": {500: process(1, "555", "python3", [b"python3", b"-m", b"http.server"])},
            "module without -m": {500: process(1, "555", "python3", [b"python3", b"qorqut.managed_runtime.host"])},
            "client is not claude": {600: process(500, "601", "node", [b"-p"])},
            "zombie client": {600: process(500, "601", "claude", [b"-p"], state="Z")},
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.host(self.table(change)))
        self.assertIsNone(self.host(self.table(), pid=601))  # a pid the table does not know

    def test_the_record_is_only_a_claim(self):
        for name, value in {"no pid": {"host_ticks": "555"}, "string pid": {**self.record, "host_pid": "500"},
                            "pid one": {**self.record, "host_pid": 1}, "no ticks": {"host_pid": 500},
                            "int ticks": {**self.record, "host_ticks": 555}, "long ticks": {**self.record, "host_ticks": "1" * 41},
                            "another agent": {**self.record, "agent_id": "someone-else"}, "list": [1], "text": "x"}.items():
            with self.subTest(name):
                self.write(value)
                self.assertIsNone(self.host(self.table()))
        (self.workspace / ".qorqut/managed-host.json").write_text("{not json")
        self.assertIsNone(self.host(self.table()))
        (self.workspace / ".qorqut/managed-host.json").unlink()
        self.assertIsNone(self.host(self.table()))
        self.write({**self.record, "pad": "x" * 5000})
        self.assertIsNone(self.host(self.table()))

    def test_a_record_that_names_no_agent_is_accepted_only_for_the_workspace_it_is_in(self):
        self.write({"host_pid": 500, "host_ticks": "555"})
        self.assertEqual(self.host(self.table())["host_id"], "host:500:555")

    def test_a_symlinked_record_is_refused(self):
        target = self.workspace / "elsewhere.json"
        target.write_text(json.dumps(self.record))
        link = self.workspace / ".qorqut/managed-host.json"
        link.unlink()
        link.symlink_to(target)
        self.assertIsNone(self.host(self.table()))


class AttestTests(unittest.TestCase):
    """``attest`` lets ``-p`` through only with a verified host, and records which."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name).resolve() / "agent"
        (self.workspace / ".qorqut").mkdir(parents=True)
        self.registration = {"agent_id": AGENT, "registration_id": "reg-1", "workspace": str(self.workspace)}
        self.sleeper = Sleeper(self)
        self.hook = {"session_id": CONVERSATION, "cwd": str(self.workspace), "hook_event_name": "SessionStart"}
        self.args = [b"claude", b"-p", b"--input-format", b"stream-json"]
        patches = (mock.patch.object(native_binding, "load_registration", return_value=self.registration),
                   mock.patch.object(native_binding, "write_json_private"),
                   mock.patch("qorqut.bootstrap.terminal_chat.native_process",
                              return_value={"pid": self.sleeper.pid, "started": self.sleeper.started}),
                   mock.patch.object(native_binding, "local_instance", return_value={}),
                   mock.patch.object(Path, "read_bytes", lambda path: b"\0".join(self.args) + b"\0"))
        self.written = patches[1].start()
        for patcher in patches[:1] + patches[2:]:
            patcher.start()
        self.addCleanup(mock.patch.stopall)

    def attest(self):
        return native_binding.attest(self.hook, "registration.json", "claude-code")

    def test_headless_is_refused_without_a_verified_host_and_every_diagnostic_flag_still_is(self):
        with mock.patch.object(native_binding, "managed_host", return_value=None) as host:
            for args in ([b"claude", b"-p"], [b"claude", b"--print", b"x"], [b"claude", b"-p", b"--agent-id", b"sub"]):
                self.args = args
                with self.subTest(args=args), self.assertRaises(DomainError) as refused:
                    self.attest()
                self.assertEqual(refused.exception.code, "inbox_binding_required")
        self.assertEqual(host.call_args.args, (str(self.workspace), self.sleeper.pid, AGENT))
        self.written.assert_not_called()

    def test_a_subagent_is_refused_even_under_a_verified_host(self):
        self.args = [b"claude", b"-p", b"--agent-id", b"sub"]
        with mock.patch.object(native_binding, "managed_host", return_value={"host_id": "h"}) as host:
            with self.assertRaises(DomainError):
                self.attest()
        host.assert_not_called()

    def test_a_verified_host_lets_the_headless_client_through_and_is_recorded(self):
        record = {"host_id": "host:5:6", "host_pid": 5, "host_ticks": "6"}
        with mock.patch.object(native_binding, "managed_host", return_value=record):
            value = self.attest()
        self.assertEqual((value["owner"], value["pid"], value["managed_host"]), (f"{self.sleeper.pid}:{self.sleeper.started}",
                                                                                   self.sleeper.pid, record))
        self.assertEqual(self.written.call_args.args[1]["managed_host"], record)

    def test_an_interactive_client_never_consults_the_host_and_records_none(self):
        self.args = [b"claude", b"--settings", b"x"]
        with mock.patch.object(native_binding, "managed_host") as host:
            value = self.attest()
        host.assert_not_called()
        self.assertNotIn("managed_host", value)


# ----- real processes: the strictness the filter exists for -----------------------------------------------------------------

class RuntimeWithIdentity:
    """A managed runtime as the binding adapter reaches it: the mode file's ``paths`` and ``identity``, the service-held, token-verified
    view of the agent's host. ``Untouchable`` is what a terminal agent must never reach."""

    def __init__(self, paths, identity=None, error=None):
        self.paths, self._identity, self._error, self.asked = paths, identity, error, []
        self.managed_runtime = self  # it also stands for the service runtime that holds it (``managed_of`` finds it there)

    def identity(self, agent_id):
        self.asked.append(agent_id)
        if self._error:
            raise self._error
        return dict(self._identity) if self._identity else None


def identity_of(host_pid, client_pid, **extra):
    """What ``ManagedRuntime.identity`` returns for a live host and its claude."""
    return {"agent_id": AGENT, "provider": "claude", "conversation_id": CONVERSATION, "pid": client_pid, "started": ticks(client_pid),
            "owner": f"host:{host_pid}:{ticks(host_pid)}", "host_pid": host_pid, "host_started": ticks(host_pid), "instance": "inst-1",
            "state": "ready", "transcript_path": None, **extra}


STUB_HOST = ("import os,subprocess,sys,time\n"
             "child=subprocess.Popen([os.environ['FAKE_CLAUDE'],'-p','-c',os.environ['CHILD']])\n"
             "print(child.pid,flush=True)\n"
             "time.sleep(60)\n")


@unittest.skipUnless(LINUX, "builds a real process tree (Linux or macOS)")
class RealProcessCase(unittest.TestCase):
    """A fake host started exactly as the launcher starts the real one (``python -B -m qorqut.managed_runtime.host --state <dir>``;
    the module is a stub found through PYTHONPATH, which is also all an agent needs to imitate it) with a ``claude``-named shell
    below it, against the real process table and the real probe scripts."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = self.root / "agent"
        (self.workspace / ".qorqut").mkdir(parents=True)
        self.claude = self.root / "claude"
        shutil.copy("/bin/bash", self.claude)  # a process whose name is "claude" and which accepts -p
        self.registration = {"agent_id": AGENT, "registration_id": "reg-1", "workspace": str(self.workspace)}
        stub = self.root / "stub/qorqut/managed_runtime"
        stub.mkdir(parents=True)
        for folder in (stub.parent, stub):
            (folder / "__init__.py").write_text("")
        (stub / "host.py").write_text(STUB_HOST)
        self.paths = SimpleNamespace(runtime=self.root / "runtime")

    def spawn(self, command, **environment):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, start_new_session=True, cwd=self.root,
                                   env={**os.environ, "FAKE_CLAUDE": str(self.claude), **environment})
        self.addCleanup(self.reap, process)
        return process

    @staticmethod
    def reap(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()

    def host_with_client(self, child="sleep 60; :"):
        """``(host, client_pid)``: a host of the launcher's argument shape, and the headless claude it spawned."""
        host = self.spawn([sys.executable, "-B", "-m", "qorqut.managed_runtime.host", "--state", str(self.root / "state")],
                          CHILD=child, PYTHONPATH=str(self.root / "stub"))
        return host, int(host.stdout.readline())

    def bash_imitation(self, child="sleep 60; :"):
        """The reviewer's forgery: a shell whose trailing positional arguments spell the host's command line."""
        host = self.spawn(["bash", "-c", f'{self.claude} -p -c "{child}" & wait', "-m", "qorqut.managed_runtime.host", "--state", "/x"])
        for _ in range(100):
            kids = self.children(host.pid)
            if kids:
                return host, kids[0]
            time.sleep(0.02)
        self.fail("the imitation did not start its child")

    def plain_claude(self, child="sleep 60; :"):
        process = self.spawn([str(self.claude), "-p", "-c", child])
        return process, process.pid

    def record(self, pid, **extra):
        value = {"host_id": "h", "host_pid": pid, "host_ticks": ticks(pid), "agent_id": AGENT, **extra}
        (self.workspace / ".qorqut/managed-host.json").write_text(json.dumps(value))

    def wait_for_name(self, pid, name="claude"):
        for _ in range(100):
            if (Path("/proc") / str(pid) / "comm").read_text().strip() == name:
                return
            time.sleep(0.02)
        self.fail("process did not take its name")

    @staticmethod
    def children(pid):
        found = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    if int(process_fields(int(entry.name))[1]) == pid:
                        found.append(int(entry.name))
                except (OSError, IndexError, ValueError):
                    continue
        return found

    def observation(self, pid, **extra):
        value = {"agent_id": AGENT, "registration_id": "reg-1", "session_id": CONVERSATION, "runtime": "claude-code",
                 "workspace": str(self.workspace), "pid": pid, "started": ticks(pid), "owner": f"{pid}:{ticks(pid)}", **extra}
        (self.workspace / ".qorqut/native-binding.json").write_text(json.dumps(value))
        return value

    def probe_script(self, client, host, *, client_ticks=None, host_ticks=None):
        return subprocess.check_output(
            [sys.executable, "-I", "-c", MANAGED_PROBE, str(self.workspace), CONVERSATION, AGENT, "reg-1", str(client),
             client_ticks or ticks(client), str(host), host_ticks or ticks(host)], text=True, timeout=20, stderr=subprocess.DEVNULL)

    def probe(self, client, host):
        return json.loads(self.probe_script(client, host))

    def refuses(self, client, host, **expected):
        with self.assertRaises(subprocess.CalledProcessError):
            self.probe_script(client, host, **expected)

    def adapter(self, runtime, target_identity=None):
        # The real ``run`` gives a probe one second; a loaded test machine should not turn that into a failure.
        run = staticmethod(lambda execution, script, arguments: subprocess.check_output(
            [sys.executable, "-I", "-c", script, *map(str, arguments)], text=True, timeout=20, stderr=subprocess.DEVNULL))
        patcher = mock.patch.object(NativeBindingAdapter, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return NativeBindingAdapter(runtime), SimpleNamespace(target_identity=target_identity), {"workspace": str(self.workspace)}

    def verify_managed(self, runtime, mode="managed"):
        if mode is not None:
            modes.set_mode(self.paths, AGENT, mode)
        adapter, execution, profile_ = self.adapter(runtime)
        return adapter.verify_managed(execution, profile_, AGENT, "reg-1", CONVERSATION)

    def verify(self, runtime, mode="managed"):
        """``NativeBindingAdapter.verify`` end to end with a terminal that has no pane: what a binding request does."""
        modes.set_mode(self.paths, AGENT, mode)
        adapter, execution, profile_ = self.adapter(runtime)
        execution.broker = SimpleNamespace(tmux=lambda *arguments: (1, ""))
        with mock.patch.object(NativeBindingAdapter, "selected", return_value=(profile_, execution, "terminal")):
            return adapter.verify(AGENT, "reg-1", CONVERSATION)


class RealProcessTests(RealProcessCase):
    def test_the_real_child_of_the_host_is_a_managed_host_client(self):
        host, client = self.host_with_client()
        self.wait_for_name(client)
        self.record(host.pid)
        found = managed_host(self.workspace, client, AGENT)
        self.assertEqual((found["host_pid"], found["host_ticks"]), (host.pid, ticks(host.pid)))
        self.assertEqual(ProcessTable.comm(client), "claude")
        self.assertEqual(ProcessTable.exe(host.pid).startswith("python"), True)
        self.assertIn(b"-p", ProcessTable.argv(client))

    def test_a_plain_claude_dash_p_is_refused_even_with_a_host_recorded_in_its_workspace(self):
        host, _ = self.host_with_client()
        self.record(host.pid)
        plain, pid = self.plain_claude()
        self.wait_for_name(pid)
        self.assertIsNone(managed_host(self.workspace, pid, AGENT))
        # No host record at all.
        (self.workspace / ".qorqut/managed-host.json").unlink()
        self.assertIsNone(managed_host(self.workspace, pid, AGENT))

    def test_a_worker_claude_started_inside_the_managed_claude_is_refused(self):
        nested = f'{self.claude} -p -c "sleep 60; :"; :'
        host, client = self.host_with_client(child=nested)
        self.wait_for_name(client)
        self.record(host.pid)
        worker = None
        for _ in range(100):
            children = self.children(client)
            if children:
                worker = children[0]
                break
            time.sleep(0.02)
        self.assertIsNotNone(worker)
        self.wait_for_name(worker)
        self.assertIsNotNone(managed_host(self.workspace, client, AGENT))
        self.assertIsNone(managed_host(self.workspace, worker, AGENT))
        # The probe script the service runs holds the same line, even if the service were told the worker is the client.
        self.observation(client)
        self.assertEqual(self.probe(client, host.pid)["pid"], client)
        self.observation(worker)
        self.refuses(worker, host.pid)

    def test_the_probe_script_accepts_the_host_child_the_service_names(self):
        host, client = self.host_with_client()
        self.wait_for_name(client)
        self.record(host.pid)
        self.observation(client)
        self.assertEqual(self.probe(client, host.pid)["pid"], client)

    def test_the_probe_script_refuses_whatever_disagrees_with_the_identity_the_service_holds(self):
        host, client = self.host_with_client()
        self.wait_for_name(client)
        plain, pid = self.plain_claude()
        self.wait_for_name(pid)
        other, other_client = self.host_with_client()
        self.wait_for_name(other_client)
        stale = str(int(ticks(host.pid)) + 1)
        refusals = {
            "plain claude -p that the service names": dict(target=pid, host=host.pid, record={}),
            "marker names another process": dict(target=pid, host=host.pid, record={}, service_client=client),
            "service names another claude": dict(target=client, host=host.pid, record={}, service_client=other_client),
            "service names another host": dict(target=client, host=other.pid, record={}),
            "recycled host in the record": dict(target=client, host=host.pid, record={"host_ticks": stale}),
            "record names another host": dict(target=client, host=host.pid, record={"host_pid": other.pid, "host_ticks": ticks(other.pid)}),
            "recycled host in the identity": dict(target=client, host=host.pid, record={}, host_ticks=stale),
            "recycled client in the identity": dict(target=client, host=host.pid, record={}, client_ticks=stale),
            "another agent": dict(target=client, host=host.pid, record={"agent_id": "someone-else"}),
            "host pid is the client": dict(target=client, host=client, record={"host_pid": client, "host_ticks": ticks(client)}),
        }
        for name, case in refusals.items():
            with self.subTest(name):
                self.record(host.pid, **case["record"])
                self.observation(case["target"])
                self.refuses(case.get("service_client", case["target"]), case["host"], client_ticks=case.get("client_ticks"),
                             host_ticks=case.get("host_ticks"))

    def test_the_probe_script_refuses_a_host_that_does_not_have_the_launchers_shape(self):
        host, client = self.bash_imitation()
        self.wait_for_name(client)
        self.record(host.pid)
        self.observation(client)
        self.assertEqual(ProcessTable.comm(client), "claude")
        self.assertFalse(runs_host_module(ProcessTable.argv(host.pid)))
        self.refuses(client, host.pid)
        self.assertIsNone(managed_host(self.workspace, client, AGENT))

    def test_the_terminal_probe_still_refuses_claude_dash_p_even_for_the_host_child(self):
        host, client = self.host_with_client()
        self.wait_for_name(client)
        self.record(host.pid)
        self.observation(client)
        from qorqut.bootstrap.native_binding import PROBE
        # The terminal probe was not loosened: its ``-p`` assertion is still in the script, and it names no managed host.
        self.assertIn("assert not any(a in argv for a in (b'exec',b'queue',b'--print',b'-p',b'--agent-id'))", PROBE)
        self.assertNotIn("managed", PROBE)
        with self.assertRaises(subprocess.CalledProcessError):
            subprocess.check_output([sys.executable, "-I", "-c", PROBE, str(self.workspace), CONVERSATION, str(host.pid), AGENT, "reg-1"],
                                    text=True, timeout=20, stderr=subprocess.DEVNULL)

    def test_the_bridge_binds_the_host_child_and_leaves_a_plain_worker_unbound(self):
        host, client = self.host_with_client()
        self.wait_for_name(client)
        self.record(host.pid)
        plain, pid = self.plain_claude()
        self.wait_for_name(pid)

        def bridge(target):
            return BridgeBinding(self.registration, ancestor=lambda runtime: ({"pid": target, "started": ticks(target)}
                                                                              if runtime == "claude-code" else None))
        self.assertEqual(bridge(client).parent, ("claude-code", f"{client}:{ticks(client)}"))
        self.assertIsNone(bridge(pid).parent)
        self.assertEqual(bridge(pid).headers(), {"X-Qorqut-Binding-Reason": "native_session_association_unavailable"})
        (self.workspace / ".qorqut/managed-host.json").unlink()
        self.assertIsNone(bridge(client).parent)


class ServiceAuthorityTests(RealProcessCase):
    """The binding of a managed session rests on what the service holds (the token-verified host identity), never on what the
    agent's own shell can write: a workspace record, a process table entry, a command line, a process name."""

    def setUp(self):
        super().setUp()
        self.host, self.client = self.host_with_client()
        self.wait_for_name(self.client)
        self.record(self.host.pid)
        self.observation(self.client)
        self.runtime = RuntimeWithIdentity(self.paths, identity_of(self.host.pid, self.client))

    def test_the_client_and_host_the_service_names_are_bound_and_taken_from_the_identity(self):
        # A workspace that lies about everything else still cannot put its text in the binding.
        self.record(self.host.pid, host_id="<script>" + "x" * 500, instance="evil", extra="x" * 200)
        self.observation(self.client, event="startup", managed_host={"host_id": "forged", "host_pid": 1, "host_ticks": "1"},
                         junk="y" * 300, host="box", target="local")
        value = self.verify_managed(self.runtime)
        self.assertEqual((value["pid"], value["started"], value["owner"]), (self.client, ticks(self.client), f"{self.client}:{ticks(self.client)}"))
        self.assertEqual(value["managed_host"], {"host_id": "inst-1", "host_pid": self.host.pid, "host_ticks": ticks(self.host.pid)})
        self.assertEqual((value["session_id"], value["event"], value["host"], value["target"]), (CONVERSATION, "startup", "box", "local"))
        self.assertNotIn("junk", value)
        self.assertEqual(self.runtime.asked, [AGENT])
        self.assertEqual(self.verify(self.runtime)[0]["managed_host"]["host_pid"], self.host.pid)

    def test_a_host_id_the_identity_gives_that_is_not_plain_short_text_is_replaced(self):
        for instance in ("<b>", "x" * 65, None, 7):
            with self.subTest(instance=instance):
                runtime = RuntimeWithIdentity(self.paths, identity_of(self.host.pid, self.client, instance=instance))
                self.assertEqual(self.verify_managed(runtime)["managed_host"]["host_id"], f"host:{self.host.pid}:{ticks(self.host.pid)}")

    def test_a_managed_runtime_that_is_not_there_or_cannot_answer_is_a_refusal(self):
        modes.set_mode(self.paths, AGENT, "managed")
        for name, runtime in {"none at all": SimpleNamespace(), "no paths": SimpleNamespace(managed_runtime=RuntimeWithIdentity(None)),
                              "raises": RuntimeWithIdentity(self.paths, error=RuntimeError("socket")),
                              "no host": RuntimeWithIdentity(self.paths, None)}.items():
            with self.subTest(name):
                self.assertIsNone(self.verify_managed(SimpleNamespace(managed_runtime=runtime) if isinstance(runtime, RuntimeWithIdentity)
                                                      else runtime, mode=None))
        # Through the native chat attribute as well: the lookup is the one every consumer uses.
        nested = SimpleNamespace(native_chat=SimpleNamespace(managed=self.runtime))
        self.assertEqual(self.verify_managed(nested, mode=None)["pid"], self.client)
        with self.assertRaises(DomainError) as refused:
            self.verify(SimpleNamespace())
        self.assertEqual(refused.exception.code, "inbox_binding_ambiguous")

    def test_an_identity_that_is_not_a_live_host_with_this_conversation_is_a_refusal(self):
        for name, change in {"closed": {"state": "closed"}, "no state": {"state": None}, "another conversation": {"conversation_id": "other"},
                             "no client": {"pid": None}, "no client ticks": {"started": None}, "text pid": {"pid": str(self.client)},
                             "no host": {"host_pid": None}, "no host ticks": {"host_started": ""},
                             "bad ticks": {"started": "12 34"}, "host is the client": {"host_pid": self.client},
                             "another agent": {"agent_id": "someone-else"}}.items():
            with self.subTest(name):
                runtime = RuntimeWithIdentity(self.paths, {**identity_of(self.host.pid, self.client), **change})
                self.assertIsNone(self.verify_managed(SimpleNamespace(managed_runtime=runtime)))
        # A host that has not learned its conversation yet does not refuse its own client.
        runtime = RuntimeWithIdentity(self.paths, identity_of(self.host.pid, self.client, conversation_id=None))
        self.assertEqual(self.verify_managed(SimpleNamespace(managed_runtime=runtime))["pid"], self.client)

    def forge(self):
        """From its own shell an agent starts a host that looks right to every hook-side filter (the launcher's argument vector, a
        python executable, a ``claude`` child, its own record and marker), and the real host and client keep running elsewhere."""
        fake_host, fake_client = self.host_with_client()
        self.wait_for_name(fake_client)
        self.record(fake_host.pid, host_id="forged")
        self.observation(fake_client)
        self.assertEqual(managed_host(self.workspace, fake_client, AGENT)["host_pid"], fake_host.pid)  # the filter is fooled
        return fake_host, fake_client

    def test_a_forged_host_and_client_are_refused_for_a_managed_agent(self):
        fake_host, fake_client = self.forge()
        # The service's host is another one (or none): the forged pair is not what it holds.
        self.assertIsNone(self.verify_managed(self.runtime))
        self.assertIsNone(self.verify_managed(SimpleNamespace(managed_runtime=RuntimeWithIdentity(self.paths, None))))
        # Even naming only the forged client or only the forged host fails.
        for client, host in ((fake_client, self.host.pid), (self.client, fake_host.pid)):
            runtime = RuntimeWithIdentity(self.paths, identity_of(host, client))
            self.assertIsNone(self.verify_managed(SimpleNamespace(managed_runtime=runtime)))
        with self.assertRaises(DomainError) as refused:
            self.verify(self.runtime)
        self.assertEqual(refused.exception.code, "inbox_binding_ambiguous")

    def test_the_reviewers_bash_imitation_binds_nothing_service_side(self):
        fake_host, fake_client = self.bash_imitation()
        self.wait_for_name(fake_client)
        self.record(fake_host.pid)
        self.observation(fake_client)
        self.assertIsNone(managed_host(self.workspace, fake_client, AGENT))
        self.assertIsNone(self.verify_managed(self.runtime))
        # Not even if the service were told to trust it: the machine disagrees.
        lying = RuntimeWithIdentity(self.paths, identity_of(fake_host.pid, fake_client))
        self.assertIsNone(self.verify_managed(SimpleNamespace(managed_runtime=lying)))

    def test_a_delegated_claude_worker_is_not_the_client_the_service_holds(self):
        worker, pid = self.plain_claude()
        self.wait_for_name(pid)
        self.observation(pid)
        self.assertIsNone(self.verify_managed(self.runtime))
        # And one nested inside the real managed claude, which does descend from the host.
        nested = f'{self.claude} -p -c "sleep 60; :"; :'
        host, client = self.host_with_client(child=nested)
        self.wait_for_name(client)
        for _ in range(100):
            kids = self.children(client)
            if kids:
                break
            time.sleep(0.02)
        self.wait_for_name(kids[0])
        self.record(host.pid)
        self.observation(kids[0])
        runtime = SimpleNamespace(managed_runtime=RuntimeWithIdentity(self.paths, identity_of(host.pid, client)))
        self.assertIsNone(self.verify_managed(runtime))
        self.observation(client)
        self.assertEqual(self.verify_managed(runtime)["pid"], client)

    def test_a_terminal_agent_ignores_a_managed_host_record_and_never_asks_the_managed_runtime(self):
        fake_host, fake_client = self.forge()
        for mode in ("terminal", None):
            with self.subTest(mode=mode):
                if mode is None:
                    (self.paths.runtime / "managed-agents" / AGENT / "mode.json").unlink()
                else:
                    modes.set_mode(self.paths, AGENT, mode)
                # Even a runtime that agrees with the forgery is never consulted.
                runtime = SimpleNamespace(managed_runtime=Untouchable())
                adapter, execution, profile_ = self.adapter(runtime)
                self.assertIsNone(adapter.verify_managed(execution, profile_, AGENT, "reg-1", CONVERSATION))
                adapter, execution, profile_ = self.adapter(SimpleNamespace(managed_runtime=RuntimeWithIdentity(
                    self.paths, identity_of(fake_host.pid, fake_client))))
                self.assertIsNone(adapter.verify_managed(execution, profile_, AGENT, "reg-1", CONVERSATION))
                with self.assertRaises(DomainError):
                    self.verify(SimpleNamespace(managed_runtime=RuntimeWithIdentity(self.paths, identity_of(fake_host.pid, fake_client))),
                                mode="terminal")

    def test_a_remote_target_never_takes_the_managed_path(self):
        modes.set_mode(self.paths, AGENT, "managed")
        adapter, execution, profile_ = self.adapter(self.runtime, target_identity=("ssh", "x"))
        self.assertIsNone(adapter.verify_managed(execution, profile_, AGENT, "reg-1", CONVERSATION))
        self.assertEqual(self.runtime.asked, [])

    def test_a_missing_record_or_marker_is_a_refusal_even_for_the_service_named_client(self):
        (self.workspace / ".qorqut/managed-host.json").unlink()
        self.assertIsNone(self.verify_managed(self.runtime))
        self.record(self.host.pid)
        (self.workspace / ".qorqut/native-binding.json").unlink()
        self.assertIsNone(self.verify_managed(self.runtime))

    def test_the_binding_request_for_the_real_client_succeeds_end_to_end(self):
        value, old_state = self.verify(self.runtime)
        self.assertEqual((value["pid"], value["managed_host"]["host_pid"], value["execution_target"], old_state),
                         (self.client, self.host.pid, "local", "unknown"))


class HostFilterShapeTests(unittest.TestCase):
    """``runs_host_module`` and the probe script's copy of it accept exactly the launcher's argument vector."""

    CASES = {
        (b"/usr/bin/python3", b"-B", b"-m", b"qorqut.managed_runtime.host", b"--state", b"/runtime/a", b""): True,
        (b"python3", b"-m", b"qorqut.managed_runtime.host", b"--state", b"/runtime/a"): True,
        (b"bash", b"-c", b"x", b"-m", b"qorqut.managed_runtime.host", b"--state", b"/x"): False,
        (b"bash", b"-c", b"x", b"-m", b"qorqut.managed_runtime.host"): False,
        (b"python3", b"-m", b"qorqut.managed_runtime.host"): False,
        (b"python3", b"-m", b"qorqut.managed_runtime.host", b"--state"): False,
        (b"python3", b"-m", b"qorqut.managed_runtime.host", b"--state", b"/a", b"extra"): False,
        (b"python3", b"-m", b"qorqut.managed_runtime.host", b"--other", b"/a"): False,
        (b"python3", b"-m", b"qorqut.managed_runtime.host", b"--state", b""): False,
        (b"python3", b"x", b"-m", b"qorqut.managed_runtime.host", b"--state", b"/a"): False,
        (b"python3", b"-m", b"qorqut.managed_runtime.hosts", b"--state", b"/a"): False,
        (b"python3", b"qorqut.managed_runtime.host", b"--state", b"/a"): False,
        (b"-m", b"qorqut.managed_runtime.host", b"--state", b"/a"): False,
        (b"python3", b"-m", b"qorqut.managed_runtime.host", b"--state", b"x" * 5000): False,
        (): False,
    }

    def script_shape(self):
        source = MANAGED_PROBE.split("def shape(a):\n", 1)[1].split("def safe", 1)[0]
        scope = {}
        exec("def shape(a):\n" + source.replace("\\0", "\0"), scope)
        return scope["shape"]

    def test_the_filter_and_the_probe_script_agree_with_the_table(self):
        shape = self.script_shape()
        for argv, expected in self.CASES.items():
            with self.subTest(argv=argv):
                self.assertIs(runs_host_module(argv), expected)
                self.assertIs(bool(shape(argv)), expected)

    def test_the_host_executable_must_be_a_python_where_it_can_be_read(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        workspace = Path(directory.name) / "agent"
        (workspace / ".qorqut").mkdir(parents=True)
        (workspace / ".qorqut/managed-host.json").write_text(json.dumps({"host_pid": 500, "host_ticks": "555", "agent_id": AGENT}))

        class Processes(FakeProcesses):
            def __init__(self, exe):
                super().__init__({500: process(1, "555", "python3", HOST_ARGV), 600: process(500, "601", "claude", [b"-p"])})
                self.name = exe

            def exe(self, pid):
                return self.name
        for exe, accepted in (("python3.12", True), ("python3", True), ("Python", True), (None, True), ("bash", False), ("claude", False),
                              ("pythonista", False), ("sleep", False)):
            with self.subTest(exe=exe):
                found = managed_host(workspace, 600, AGENT, processes=Processes(exe))
                self.assertEqual(found is not None, accepted)

    def test_a_record_host_id_is_short_plain_text_or_replaced(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        workspace = Path(directory.name) / "agent"
        (workspace / ".qorqut").mkdir(parents=True)
        table = FakeProcesses({500: process(1, "555", "python3", HOST_ARGV), 600: process(500, "601", "claude", [b"-p"])})
        for host_id, expected in (("host-1:a.b", "host-1:a.b"), ("<x>", "host:500:555"), ("x" * 65, "host:500:555"), (5, "host:500:555"),
                                  ("a\nb", "host:500:555"), (None, "host:500:555")):
            (workspace / ".qorqut/managed-host.json").write_text(json.dumps({"host_id": host_id, "host_pid": 500, "host_ticks": "555"}))
            with self.subTest(host_id=host_id):
                self.assertEqual(managed_host(workspace, 600, AGENT, processes=table)["host_id"], expected)
        (workspace / ".qorqut/managed-host.json").write_text(json.dumps({"host_pid": 500, "host_ticks": "55 5"}))
        self.assertIsNone(managed_host(workspace, 600, AGENT, processes=table))


class BridgeTerminalUnchangedTests(unittest.TestCase):
    """Without a managed host the bridge refuses exactly the argument vectors it always refused."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = self.root / "agent"
        (self.workspace / ".qorqut").mkdir(parents=True)
        self.registration = {"agent_id": AGENT, "registration_id": "reg-1", "workspace": str(self.workspace)}

    def bridge(self, args, runtime="claude-code"):
        proc = self.root / "proc" / "42"
        proc.mkdir(parents=True, exist_ok=True)
        (proc / "cmdline").write_bytes(b"\0".join(args) + b"\0")
        with mock.patch("qorqut.process_info.cmdline", return_value=[a.decode() for a in args]):
            return BridgeBinding(self.registration, ancestor=lambda r: {"pid": 42, "started": "9"} if r == runtime else None,
                                 proc=proc.parent)

    def test_interactive_clients_bind_and_every_other_vector_stays_refused(self):
        self.assertEqual(self.bridge([b"claude"]).parent, ("claude-code", "42:9"))
        for args in ([b"claude", b"-p"], [b"claude", b"--print"], [b"claude", b"--agent-id", b"x"], [b"codex", b"exec"],
                     [b"codex", b"queue"], [b"claude", b"-p", b"--agent-id", b"x"]):
            with self.subTest(args=args):
                self.assertIsNone(self.bridge(args).parent)

    def test_a_headless_codex_is_never_a_managed_client(self):
        # Even a host record cannot make a ``codex -p``-like vector bindable: managed is a Claude arrangement.
        (self.workspace / ".qorqut/managed-host.json").write_text(json.dumps({"host_pid": 500, "host_ticks": "5"}))
        with mock.patch.object(native_binding, "managed_host", return_value={"host_pid": 500}) as host:
            self.assertIsNone(self.bridge([b"codex", b"-p"], runtime="codex").parent)
        host.assert_not_called()


if __name__ == "__main__":
    unittest.main()
