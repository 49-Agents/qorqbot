"""Ordubashy's agent tools (``agent_terminal_*``) in managed mode (the default) mean control of an agent's managed session.
Terminals mode is covered by ``test_agent_terminal_tools_terminal_mode``; here the terminal backend must never be reached.

status reads on/off, asleep/open and working; start turns the agent on and opens its session; restart closes it and opens it again
in the same conversation; kill closes it and turns it off; ping (and the owner-issued execute_command) deliver a direct message.
The tool names, schemas and the stored plan binding keep their shape. Stand-ins replace the opener, the arbiter and the database;
nothing here starts a host or a client.
"""
from contextlib import contextmanager
import inspect
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import DomainError

from qorqut.agents import terminal_control
from qorqut.agents.terminal_control import DESCRIPTIONS, PREFIX, TerminalControl, execution_outcome
from qorqut.native_chat import selection, store
from qorqut.native_chat.contracts import RESULT_DEFERRED, STATE_SUBMITTED, STATE_UNCERTAIN, InputResult

AGENT = "11111111-1111-4111-8111-111111111111"
IDENTITY = {"conversation_id": "conv-1", "pid": 4343, "started": "777", "owner": "host:4242:555"}


class FakeManaged:
    def __init__(self):
        self.calls, self.identity_value, self.view, self.handles = [], None, {}, []

    def identity(self, agent_id):
        return self.identity_value

    def snapshot(self, agent_id):
        return self.view

    def close(self, agent_id, mode):
        self.calls.append(("close", agent_id, mode))
        return True

    def handle(self, agent_id):
        return self.handles.pop(0) if self.handles else None


class FakeOpener:
    def __init__(self, managed):
        self.managed, self.off, self.log, self.open_error = managed, False, [], None

    def is_off(self, agent_id):
        return self.off

    def turn_on(self, agent_id):
        self.log.append(("on", agent_id))
        self.off = False

    def wake(self, profile, source, wait=False, force=False):
        self.log.append(("wake", profile["agent_id"], source, wait, force))
        if self.open_error:
            raise self.open_error


class FakeArbiter:
    def __init__(self, result):
        self.result, self.requests = result, []

    def submit(self, request):
        self.requests.append(request)
        return self.result


@contextmanager
def read():
    yield SimpleNamespace(connection=None)


class Case(unittest.TestCase):
    def setUp(self):
        self.managed = FakeManaged()
        self.opener = FakeOpener(self.managed)
        self.arbiter = FakeArbiter(InputResult(state=STATE_SUBMITTED, reason="accepted_by_host"))
        self.runtime = SimpleNamespace(
            native_chat=SimpleNamespace(opener=self.opener, arbiter=self.arbiter), managed_runtime=self.managed,
            database=SimpleNamespace(read=read), paths=SimpleNamespace(runtime="/r"), application=SimpleNamespace())
        self.control = TerminalControl.__new__(TerminalControl)
        self.control.runtime = self.runtime
        self.target = {"agent_id": AGENT, "revision": 3}
        patches = (mock.patch.object(store, "active_profile", lambda connection, agent_id, paths: {"agent_id": agent_id}),
                   mock.patch.object(selection, "selected", lambda connection, agent_id: SimpleNamespace(revision="rev-1")))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


class ContractTests(Case):
    def test_the_managed_path_never_reaches_the_terminal_backend(self):
        class Untouchable:
            def __getattr__(self, name):
                raise AssertionError(f"the terminal backend was touched: {name}")
        self.control.backend = Untouchable()
        self.managed.handles = [object(), None]
        with mock.patch("time.sleep"):
            self.control.snapshot(self.target)
            self.control.binding(self.target)
            self.control.session_state(self.target)
            for action in ("start", "restart", "kill", "ping"):
                self.control._execute(self.target, action, "hello" if action == "ping" else None, "k")

    def test_the_source_runs_no_tmux_and_no_process(self):
        source = inspect.getsource(terminal_control)
        for word in ("tmux", "subprocess", "agent_terminal_control", "terminal_composer"):
            self.assertNotIn(word, source)
        self.assertFalse(hasattr(self.control, "_validate"))

    def test_every_tool_keeps_its_name_and_gets_its_description(self):
        control = TerminalControl.__new__(TerminalControl)
        control.runtime = control.app = SimpleNamespace(registry=OperationRegistry(), application=None)
        control.app.registry = OperationRegistry()
        control.register()
        names = {PREFIX + name for name in DESCRIPTIONS} | {"terminal_command_issue"}
        registered = {name: control.app.registry.get(name) for name in names}
        for name, descriptor in registered.items():
            self.assertEqual(descriptor.name, name)
        self.assertEqual(DESCRIPTIONS, {
            "status": "Whether the agent is on or off, whether its session is open, and whether it is working.",
            "start": "Turn the agent on and open its session.",
            "restart": "Close the agent's session and open it again in the same conversation.",
            "kill": "Close the agent's session and turn it off, ending active work.",
            "ping": "Send the owner-issued direct message.",
            "execute_command": "Run the owner-issued session command, such as compacting."})
        for key, text in DESCRIPTIONS.items():
            self.assertEqual(registered[PREFIX + key].description, text)
        self.assertTrue(registered["terminal_command_issue"].owner_only)
        self.assertEqual(set(registered["terminal_command_issue"].input_schema["properties"]["action"]["enum"]),
                         {"start", "restart", "kill", "ping"})


class SnapshotTests(Case):
    """The observation a plan is prepared against keeps the shape stored plans and owner commands carry."""

    def test_absent_without_a_verified_session_and_present_with_its_identity(self):
        self.assertEqual(self.control.snapshot(self.target),
                         {"state": "absent", "managed": True, "client": "claude", "selection_revision": "rev-1"})
        self.managed.identity_value = IDENTITY
        found = self.control.snapshot(self.target)
        self.assertEqual((found["state"], found["command"], found["client"], found["managed"]), ("present", "claude", "claude", True))
        self.assertEqual((found["session_id"], found["owner"], found["foreground_pid"], found["foreground_started"]),
                         ("conv-1", "host:4242:555", 4343, "777"))
        self.assertEqual(self.control.binding(self.target), {"profile_revision": 3, "terminal": found})

    def test_the_turn_state_never_changes_it_so_a_prepared_command_stays_valid(self):
        self.managed.identity_value = IDENTITY
        before = self.control.snapshot(self.target)
        self.managed.view = {"open": True, "phase": "working"}
        self.assertEqual(self.control.snapshot(self.target), before)

    def test_without_a_managed_runtime_the_agent_is_absent(self):
        self.runtime.managed_runtime = None
        self.runtime.native_chat.managed = None
        self.assertEqual(self.control.snapshot(self.target)["state"], "absent")


class ExecuteTests(Case):
    def test_start_turns_on_and_opens_the_session_waiting_for_it(self):
        detail = self.control._execute(self.target, "start", None, "k")
        self.assertEqual(detail, "Managed session open")
        self.assertEqual(self.opener.log, [("on", AGENT), ("wake", AGENT, "ordubashy", True, True)])
        self.assertEqual(self.managed.calls, [])

    def test_restart_closes_gracefully_waits_for_the_host_to_go_and_opens_again(self):
        self.managed.handles = [object(), None]
        with mock.patch("time.sleep"):
            detail = self.control._execute(self.target, "restart", None, "k")
        self.assertEqual(detail, "Managed session restarted")
        self.assertEqual(self.managed.calls, [("close", AGENT, "graceful")])
        self.assertEqual(self.opener.log[-1], ("wake", AGENT, "ordubashy", True, True))

    def test_a_restart_whose_session_never_goes_is_not_opened_again(self):
        self.managed.handle = lambda agent_id: object()
        times = iter([0.0, 1.0, 1000.0])
        with mock.patch("time.sleep"), mock.patch("time.monotonic", lambda: next(times)):
            with self.assertRaises(DomainError) as refused:
                self.control._execute(self.target, "restart", None, "k")
        self.assertEqual(refused.exception.code, "not_delivered")
        self.assertNotIn("wake", [entry[0] for entry in self.opener.log])

    def test_kill_closes_the_session_now_and_opens_nothing(self):
        detail = self.control._execute(self.target, "kill", None, "k")
        self.assertEqual(self.managed.calls, [("close", AGENT, "now")])
        self.assertEqual(self.opener.log, [])
        self.assertIn("off", detail)

    def test_a_session_that_cannot_be_opened_is_a_failed_action_not_an_uncertain_one(self):
        self.opener.open_error = DomainError("cli_unavailable", "no client")
        with self.assertRaises(DomainError) as refused:
            self.control._execute(self.target, "start", None, "k")
        self.assertEqual(refused.exception.code, "not_delivered")
        self.assertEqual(execution_outcome(refused.exception)[0], "failed")

    def test_an_inactive_agent_is_not_started(self):
        with mock.patch.object(store, "active_profile", lambda connection, agent_id, paths: None):
            with self.assertRaises(DomainError) as refused:
                self.control._execute(self.target, "start", None, "k")
        self.assertEqual(refused.exception.code, "not_delivered")
        self.assertEqual(self.opener.log, [])

    def test_without_the_managed_runtime_nothing_is_attempted(self):
        self.runtime.native_chat = SimpleNamespace(opener=None)
        for action in ("start", "restart", "kill", "ping"):
            with self.assertRaises(DomainError) as refused:
                self.control._execute(self.target, action, "hi" if action == "ping" else None, "k")
            self.assertEqual(refused.exception.code, "operation_unavailable")


class PingTests(Case):
    def test_a_ping_is_one_direct_message_through_the_shared_arbiter(self):
        detail = self.control._execute(self.target, "ping", "hello", "terminal-action:1")
        self.assertIn("submitted", detail)
        (request,) = self.arbiter.requests
        self.assertEqual((request.agent_id, request.input_type, request.source_key, request.text),
                         (AGENT, "direct_message", "terminal-action:1", "hello"))

    def test_an_owner_issued_session_command_such_as_compact_is_passed_as_the_message(self):
        self.control._execute(self.target, "ping", "/compact keep decisions", "k")
        self.assertEqual(self.arbiter.requests[0].text, "/compact keep decisions")

    def test_a_command_that_only_opens_a_menu_is_refused_the_way_owner_chat_refuses_it(self):
        for text in ("/model", "/clear", "/resume  "):
            with self.subTest(text=text):
                with self.assertRaises(DomainError) as refused:
                    self.control._execute(self.target, "ping", text, "k")
                self.assertEqual(refused.exception.code, "not_delivered")
        self.assertEqual(self.arbiter.requests, [])
        self.control._execute(self.target, "ping", "/model sonnet", "k")  # a model name is not a menu
        self.assertEqual(len(self.arbiter.requests), 1)

    def test_a_deferred_message_is_not_delivered_and_an_uncertain_one_is_uncertain(self):
        self.arbiter.result = InputResult(state=RESULT_DEFERRED, reason="session_closed")
        with self.assertRaises(DomainError) as refused:
            self.control._execute(self.target, "ping", "hi", "k")
        self.assertEqual(execution_outcome(refused.exception)[0], "failed")
        self.arbiter.result = InputResult(state=STATE_UNCERTAIN, reason="host_timeout")
        with self.assertRaises(DomainError) as unsure:
            self.control._execute(self.target, "ping", "hi", "k")
        self.assertEqual(execution_outcome(unsure.exception)[0], "uncertain")

    def test_an_arbiter_refusal_is_a_failed_action(self):
        from qorqut.native_chat.contracts import NativeChatError

        def refuse(request):
            raise NativeChatError("validation_error", "too long")
        self.arbiter.submit = refuse
        with self.assertRaises(DomainError) as refused:
            self.control._execute(self.target, "ping", "hi", "k")
        self.assertEqual(refused.exception.code, "not_delivered")


class StatusWordingTests(Case):
    def test_the_status_an_agent_reads_speaks_of_sessions(self):
        self.managed.view = {"open": True, "phase": "working"}
        self.opener.off = False
        found = self.control.session_state({"agent_id": AGENT, "profile_json": {"runtime": "claude-code"}})
        self.assertEqual((found["state"], found["turn"], found["client"]), ("open", "working", "claude"))
        self.managed.view = {}
        self.assertEqual(self.control.session_state({"agent_id": AGENT})["state"], "asleep")
        self.opener.off = True
        self.assertEqual(self.control.session_state({"agent_id": AGENT})["state"], "off")


class OutcomeTests(unittest.TestCase):
    def test_only_a_delivery_that_never_happened_is_failed(self):
        self.assertEqual(execution_outcome(DomainError("not_delivered", "No"))[0], "failed")
        self.assertEqual(execution_outcome(DomainError("operation_failed", "No"))[0], "uncertain")
        self.assertEqual(execution_outcome(RuntimeError())[0], "uncertain")


class MessagesToRetiredDevsTests(Case):
    def test_a_direct_message_target_is_refused_for_every_caller(self):
        control = TerminalControl.__new__(TerminalControl)
        with self.assertRaises(DomainError) as refused:
            control.message_target(SimpleNamespace(principal_kind="agent"), AGENT, None)
        self.assertEqual(refused.exception.code, "forbidden")
        with self.assertRaises(DomainError) as refused:
            control.queue_message(SimpleNamespace(principal_kind="agent"), {"recipient_id": AGENT}, None)
        self.assertEqual(refused.exception.code, "forbidden")


if __name__ == "__main__":
    unittest.main()
