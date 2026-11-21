"""Ordubashy's agent tools (``agent_terminal_*``) in Terminals mode act on the agent's tmux terminal, as the product did before
managed sessions. The mode is set explicitly and the terminal backend is a fake with the real method shapes: nothing starts tmux."""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.agents import terminal_control
from qorqut.agents.terminal_control import TerminalControl, execution_outcome
from qorqut.managed_runtime import mode
from qorqut.native_chat import selection

AGENT = "11111111-1111-4111-8111-111111111111"
ABSENT = {"state": "absent", "client": "claude", "selection_revision": "rev-1"}
PRESENT = {"state": "present", "client": "claude", "command": "claude", "selection_revision": "rev-1", "session_id": "$3",
           "pane_id": "%4", "foreground_pid": 4343}


class FakeBackend:
    """The shapes of ``AgentTerminalBackend``: snapshot, validate, execute."""

    def __init__(self):
        self.observed, self.calls = dict(ABSENT), []

    def snapshot(self, row):
        self.calls.append(("snapshot", row["agent_id"]))
        return dict(self.observed)

    def validate(self, action, snapshot):
        self.calls.append(("validate", action, snapshot["state"]))
        if action == "start" and snapshot["state"] != "absent":
            raise DomainError("invalid_transition", "Start requires an absent terminal or idle shell")
        if action != "start" and snapshot["state"] != "present":
            raise DomainError("invalid_transition", "Action requires an existing terminal")

    def execute(self, row, action, message, expected, input_key=None):
        self.calls.append(("execute", row["agent_id"], action, message, expected, input_key))
        return f"{action} done"


class NoManagedSession:
    """Any use of the managed session in Terminals mode fails the test."""

    def __getattr__(self, name):
        raise AssertionError(f"the managed session was touched: {name}")


@contextmanager
def read():
    yield SimpleNamespace(connection=None)


class Case(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.runtime = SimpleNamespace(
            native_chat=SimpleNamespace(opener=NoManagedSession(), arbiter=NoManagedSession()), managed_runtime=NoManagedSession(),
            database=SimpleNamespace(read=read), paths=SimpleNamespace(runtime="/r"), application=SimpleNamespace())
        self.control = TerminalControl.__new__(TerminalControl)
        self.control.runtime, self.control.backend = self.runtime, self.backend
        self.target = {"agent_id": AGENT, "revision": 3, "home_project_id": None, "name": "scout"}
        self.mode = mock.patch.object(mode, "current", return_value="terminal")
        self.mode.start()
        self.addCleanup(self.mode.stop)
        patch = mock.patch.object(selection, "selected", lambda connection, agent_id: SimpleNamespace(revision="rev-1"))
        patch.start()
        self.addCleanup(patch.stop)


class SnapshotAndStatusTests(Case):
    def test_the_binding_is_the_terminal_observation_without_a_managed_key(self):
        self.backend.observed = dict(PRESENT)
        found = self.control.binding(self.target)
        self.assertEqual(found, {"profile_revision": 3, "terminal": PRESENT})
        self.assertNotIn("managed", found["terminal"])

    def test_status_reports_the_terminal_and_no_session_terms(self):
        control = self.control
        control.actor = lambda ctx, uow: None
        control.target = lambda ctx, identity, uow, control=True: self.target
        result = control.status(SimpleNamespace(), {"target_agent_id": AGENT}, None, SimpleNamespace(connection=None))
        self.assertEqual(result, {"target_agent_id": AGENT, "profile_revision": 3, "terminal": ABSENT})

    def test_without_an_attached_backend_nothing_is_attempted(self):
        self.control.backend = None
        for call in (lambda: self.control.binding(self.target), lambda: self.control._execute(self.target, "start", None, "k", ABSENT)):
            with self.assertRaises(DomainError) as refused:
                call()
            self.assertEqual(refused.exception.code, "operation_unavailable")

    def test_the_mode_is_asked_at_every_call(self):
        self.mode.stop()
        with mock.patch.object(mode, "current", return_value="managed"):
            self.assertFalse(self.control.in_terminal_mode())
        with mock.patch.object(mode, "current", return_value="terminal"):
            self.assertTrue(self.control.in_terminal_mode())
        self.mode.start()


class ExecuteTests(Case):
    def test_each_action_goes_to_the_backend_with_the_prepared_observation_and_never_to_the_managed_session(self):
        for action, message in (("start", None), ("restart", None), ("kill", None), ("ping", "/compact keep decisions")):
            with self.subTest(action=action):
                detail = self.control._execute(self.target, action, message, f"terminal-action:{action}", PRESENT)
                self.assertEqual(detail, f"{action} done")
                self.assertEqual(self.backend.calls[-1], ("execute", AGENT, action, message, PRESENT, f"terminal-action:{action}"))

    def test_a_menu_command_is_typed_into_the_terminal_not_refused(self):
        self.control._execute(self.target, "ping", "/model", "k", PRESENT)
        self.assertEqual(self.backend.calls[-1][3], "/model")

    def test_a_terminal_that_changed_fails_the_action_as_uncertain_by_the_backends_own_conflict(self):
        self.backend.execute = mock.Mock(side_effect=DomainError("revision_conflict", "Terminal changed before dispatch"))
        with self.assertRaises(DomainError) as refused:
            self.control._execute(self.target, "restart", None, "k", PRESENT)
        self.assertEqual(execution_outcome(refused.exception)[0], "uncertain")


class PrepareTests(Case):
    def prepare(self, name, **args):
        control = self.control
        control.actor = lambda ctx, uow: None
        control.select_targets = lambda ctx, args, uow: [self.target]
        with mock.patch.object(terminal_control, "PlanBuilder") as builder, mock.patch.object(terminal_control, "ref"):
            builder.return_value.effects = []
            control.prepare(name, SimpleNamespace(principal_kind="agent", principal_id="p"), args, {}, None)

    def test_start_needs_an_absent_terminal_and_the_others_an_existing_one(self):
        self.backend.observed = dict(PRESENT)
        with self.assertRaises(DomainError) as refused:
            self.prepare("agent_terminal_start")
        self.assertEqual(refused.exception.code, "invalid_transition")
        for name in ("agent_terminal_restart", "agent_terminal_kill"):
            self.prepare(name)
        self.backend.observed = dict(ABSENT)
        self.prepare("agent_terminal_start")
        for name in ("agent_terminal_restart", "agent_terminal_kill"):
            with self.assertRaises(DomainError) as refused:
                self.prepare(name)
            self.assertEqual(refused.exception.code, "invalid_transition")

    def test_start_and_restart_refuse_while_the_installation_is_being_switched(self):
        self.backend.observed = dict(PRESENT)
        with mock.patch.object(mode, "switching", return_value=True):
            for name in ("agent_terminal_start", "agent_terminal_restart"):
                with self.assertRaises(DomainError) as refused:
                    self.prepare(name)
                self.assertEqual((refused.exception.code, refused.exception.retryable), ("runtime_switching", True))
            self.prepare("agent_terminal_kill")

    def test_managed_mode_refuses_start_and_restart_while_switching_too_and_does_not_validate_in_the_terminal(self):
        self.mode.stop()
        self.addCleanup(self.mode.start)
        self.control.managed_snapshot = lambda row: {"state": "absent", "managed": True}
        with mock.patch.object(mode, "current", return_value="managed"):
            with mock.patch.object(mode, "switching", return_value=True):
                with self.assertRaises(DomainError) as refused:
                    self.prepare("agent_terminal_start")
                self.assertEqual(refused.exception.code, "runtime_switching")
            self.prepare("agent_terminal_kill")  # valid whatever the session state: no terminal precondition in managed mode
        self.assertEqual(self.backend.calls, [])


class FakeRepository:
    def __init__(self, rows):
        self.rows = rows

    def get(self, identity, required=False):
        return dict(self.rows[identity])

    def update(self, identity, values, expected_revision=None):
        self.rows[identity] = {**self.rows[identity], **values}


class TickTests(Case):
    """A plan prepared in one mode and run after a switch fails with ``revision_conflict`` by the stored binding comparison."""

    def setUp(self):
        super().setUp()
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE terminal_actions(id TEXT, batch_id TEXT, state TEXT, created_at TEXT)")
        self.db.execute("CREATE TABLE native_session_owners(agent_id TEXT, desired_state TEXT)")
        self.db.execute("INSERT INTO terminal_actions VALUES('a1','b1','queued','t')")
        self.db.execute("INSERT INTO native_session_owners VALUES(?, 'running')", (AGENT,))
        self.rows = {}
        uow = SimpleNamespace(connection=self.db, repository=lambda name: FakeRepository(self.rows))
        self.runtime.database.transaction = contextmanager(lambda: (yield uow))
        self.control.app = SimpleNamespace(ensure_writes_open=lambda uow: None)
        self.control._authorized = lambda action, uow: self.target
        self.executed = []
        self.control._execute = lambda *args: self.executed.append(args) or "done"

    def queue(self, action, binding):
        self.rows["a1"] = {"id": "a1", "batch_id": "b1", "action": action, "message": None, "state": "queued", "revision": 1,
                           "binding_json": {"profile_revision": 3, "terminal": binding}}

    def test_a_plan_prepared_in_managed_mode_fails_after_a_switch_to_terminals(self):
        self.queue("kill", dict(PRESENT, managed=True))
        self.assertTrue(self.control._tick())
        self.assertEqual((self.rows["a1"]["state"], self.rows["a1"]["detail"]), ("failed", "Target session changed before execution"))
        self.assertEqual(self.executed, [])

    def test_a_plan_prepared_in_terminals_mode_fails_after_a_switch_to_managed(self):
        self.mode.stop()
        self.addCleanup(self.mode.start)
        self.control.managed_snapshot = lambda row: dict(PRESENT, managed=True)
        self.queue("kill", dict(PRESENT))
        with mock.patch.object(mode, "current", return_value="managed"):
            self.assertTrue(self.control._tick())
        self.assertEqual(self.rows["a1"]["state"], "failed")
        self.assertEqual(self.executed, [])

    def test_a_plan_prepared_in_terminals_mode_runs_in_terminals_mode(self):
        self.backend.observed = dict(PRESENT)
        self.queue("kill", dict(PRESENT))
        self.assertTrue(self.control._tick())
        self.assertEqual(self.rows["a1"]["state"], "completed")
        self.assertEqual(self.executed, [(self.target, "kill", None, "terminal-action:a1", PRESENT)])
        self.assertEqual(self.db.execute("SELECT desired_state FROM native_session_owners").fetchone()[0], "stopped")

    def test_a_start_waits_in_the_queue_while_the_installation_is_being_switched(self):
        self.queue("start", dict(ABSENT))
        with mock.patch.object(mode, "switching", return_value=True):
            self.assertFalse(self.control._tick())
        self.assertEqual(self.rows["a1"]["state"], "queued")
        self.assertEqual(self.executed, [])
        self.assertEqual(self.db.execute("SELECT desired_state FROM native_session_owners").fetchone()[0], "running")


if __name__ == "__main__":
    unittest.main()
