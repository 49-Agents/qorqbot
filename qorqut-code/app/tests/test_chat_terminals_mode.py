"""Native Chat when the installation runs its agents in terminals (``managed_runtime.mode``): the payload, the send rules, the opener,
the attachment read, the launch options and the slash catalog, and that the managed answers stay exactly what they were.

Runs over ``test_managed_chat_send``'s fakes with the real probe, registry and arbiter. The mode is the real setting file under the
test's own temporary runtime folder; the terminal backend is never reached (the probe's process check is a stand-in that says the
marker's process sits in the agent's pane). Nothing starts tmux or a CLI.
"""
import isolated_cli_environment  # before any Qorqbot import

from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.managed_runtime import mode
from qorqut.native_chat import chat as chat_module, commands, launch_options, store
from qorqut.native_chat.arbiter import TERMINAL_OFFLINE
from qorqut.native_chat.contracts import OwnerInputOutcome, STATE_WAITING
from qorqut.native_chat.probe import ProbeResult

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:  # the service extras are not installed
    SERVICE = False

import test_managed_chat_send as base
from test_managed_chat_send import AGENT, OPEN_VIEW, OWNER, Untouchable

MANAGED_KEYS = {"state", "reason", "can_send", "working", "provider", "conversation_id", "transport", "binding", "detail",
                "selection", "candidate", "managed"}
TERMINAL_KEYS = (MANAGED_KEYS - {"managed"}) | {"launch_commands", "execution"}


class TerminalCase(base.ChatCase):
    """A Chat whose installation setting is ``terminal`` (``self.terminals(False)`` puts it back to managed)."""

    def setUp(self):
        super().setUp()
        self.probe._terminal_mode = lambda: mode.is_terminal(self.paths)
        self.probe._injected_probe = lambda profile, marker: {"transport": "terminal", "owner": None}
        self.delivered = []
        self.arbiter.adapters["terminal"] = SimpleNamespace(name="terminal", submit=self.submit)
        self.terminals(True)

    def submit(self, profile, session, text, **options):
        options["checkpoint"]()
        self.delivered.append(text)
        return OwnerInputOutcome("submitted", "typed", {"adapter": "terminal"})

    def terminals(self, on):
        mode.commit(self.paths, mode.TERMINAL if on else mode.MANAGED)

    def terminal_session(self):
        """The agent's terminal runs a Claude session that is bound and captured; returns its pin."""
        self.db.adopt(self.client.pid, self.client.started)
        self.db.connection.execute("UPDATE native_session_owners SET binding_json=?", (json.dumps(
            {"runtime": "claude-code", "pid": self.client.pid, "started": self.client.started,
             "owner": f"{self.client.pid}:{self.client.started}"}),))
        self.marker = {"agent_id": AGENT, "session_id": base.CONVERSATION, "runtime": "claude-code", "pid": self.client.pid,
                       "started": self.client.started, "transcript_path": f"/nonexistent/{base.CONVERSATION}.jsonl",
                       "capture": True, "capture_enabled": True}
        live = self.registry.reconcile(self.profile(), self.probe.observe(self.profile()))
        return live.session["id"]

    def view(self):
        with self.db.read() as uow:
            persisted = store.live_session(uow.connection, AGENT)
        return self.chat.session_view(self.profile(), persisted)


class PayloadTests(TerminalCase):
    def test_a_terminal_agent_without_a_session_is_offline_with_the_terminal_wording(self):
        self.chat.managed = Untouchable()
        view = self.view()
        self.assertEqual(set(view), TERMINAL_KEYS)
        self.assertEqual((view["execution"], view["launch_commands"], view["state"], view["can_send"], view["reason"]),
                         ("terminal", ["claude"], "offline", False, "no_session"))
        self.assertEqual(view["detail"], "Start or resume the agent in its terminal")

    def test_a_bound_terminal_session_is_live_and_sendable(self):
        self.chat.managed = Untouchable()
        pin = self.terminal_session()
        view = self.view()
        self.assertEqual((view["state"], view["can_send"], view["transport"], view["binding"]["binding_id"], view["detail"]),
                         ("live", True, "terminal", pin, "Terminal connected"))
        self.assertNotIn("managed", view)

    def test_an_unbound_terminal_session_is_connecting_not_closed(self):
        self.terminal_session()
        view = self.chat.session_view(self.profile(), None)
        self.assertEqual((view["can_send"], view["reason"], view["detail"]),
                         (False, "binding_not_ready", "Terminal connected; connecting to this session"))

    def test_an_unsupported_client_says_only_claude_code_runs_in_terminals(self):
        view = self.chat.session_view(self.profile(), None, observed=ProbeResult("unsupported_runtime"))
        self.assertEqual((view["state"], view["reason"], view["can_send"], view["execution"], view["detail"]),
                         ("offline", "unsupported_runtime", False, "terminal", "Only Claude Code agents can run in terminals"))

    def test_an_agent_set_to_codex_reads_why_nothing_starts_it_and_one_set_to_claude_code_does_not(self):
        from qorqut.managed_runtime import options
        profile = self.profile()
        self.assertEqual(self.chat.session_view(profile, None)["detail"], "Start or resume the agent in its terminal")
        options.write(self.paths, AGENT, client="codex")
        view = self.chat.session_view(profile, None)
        self.assertEqual((view["state"], view["reason"], view["detail"]),
                         ("offline", "unsupported_runtime", "Only Claude Code agents can run in terminals"))
        # The owner's choice decides, not the profile: a Codex profile set to Claude Code is an ordinary terminal agent.
        options.write(self.paths, AGENT, client="claude")
        codex_profile = {**profile, "profile_json": json.dumps({"runtime": "codex"})}
        self.assertEqual(self.chat.session_view(codex_profile, None)["detail"], "Start or resume the agent in its terminal")

    def test_the_state_and_the_change_event_follow_the_mode_at_call_time(self):
        with self.db.transaction() as uow:
            store.bump(uow.connection, AGENT)
        self.chat.managed = Untouchable()
        self.assertEqual(set(self.chat.events(OWNER, AGENT, 0)[0][0]["data"]), {"revision", "newest_sequence", "waiting"})
        self.assertEqual(self.chat.state(OWNER, AGENT)["session"]["execution"], "terminal")
        self.chat.managed = self.managed
        self.terminals(False)
        self.assertEqual(set(self.chat.events(OWNER, AGENT, 0)[0][0]["data"]), {"revision", "newest_sequence", "waiting", "managed"})
        session = self.chat.state(OWNER, AGENT)["session"]
        self.assertEqual(set(session), MANAGED_KEYS)

    def test_the_managed_payload_has_neither_execution_nor_launch_commands(self):
        self.terminals(False)
        view = self.view()
        self.assertEqual(set(view), MANAGED_KEYS)
        self.assertEqual((view["reason"], view["can_send"], view["detail"]), ("session_closed", True, "Sending a message starts a session"))


class SendTests(TerminalCase):
    def test_a_message_without_a_pin_is_refused_and_nothing_is_opened(self):
        self.chat.managed = Untouchable()
        for pin in (None, "short"):
            with self.assertRaises(DomainError) as refused:
                self.chat.send(OWNER, AGENT, "hello", "key-0000001", pin)
            self.assertEqual(refused.exception.code, "validation_error")
        self.assertEqual((self.inputs(), self.managed.opened), (0, []))

    def test_a_new_conversation_is_refused(self):
        self.chat.managed = Untouchable()
        with self.assertRaises(DomainError) as refused:
            self.chat.send(OWNER, AGENT, "hello", "key-0000002", new_conversation=True)
        self.assertEqual(refused.exception.code, "validation_error")

    def test_a_pin_with_no_live_session_is_terminal_offline_and_wakes_and_turns_on_nothing(self):
        self.db.adopt(self.client.pid, self.client.started)
        self.db.connection.execute("UPDATE native_session_owners SET desired_state='stopped'")
        self.db.connection.commit()
        with self.assertRaises(DomainError) as refused:
            self.chat.send(OWNER, AGENT, "hello", "key-0000003", "a-pin-of-a-closed-session")
        self.assertEqual((refused.exception.code, refused.exception.message),
                         ("terminal_offline", "Start or resume the agent in its terminal first"))
        self.assertEqual((self.managed.opened, self.chat.opener.desired(AGENT), self.inputs()), ([], "stopped", 0))

    def test_a_message_for_the_live_session_is_admitted_for_its_pin(self):
        pin = self.terminal_session()
        result = self.chat.send(OWNER, AGENT, "hello", "key-0000004", pin)
        self.assertEqual((result["created"], result["binding"]["binding_id"], result["binding"]["transport"], result["input"]["state"]),
                         (True, pin, "terminal", STATE_WAITING))
        self.assertEqual((self.managed.opened, self.inputs()), ([], 1))

    def test_a_stale_pin_is_the_terminal_conversation_changed(self):
        self.terminal_session()
        with self.assertRaises(DomainError) as refused:
            self.chat.send(OWNER, AGENT, "hello", "key-0000005", "00000000-not-the-live-one")
        self.assertEqual((refused.exception.code, refused.exception.message),
                         ("terminal_changed", "The terminal conversation changed; send again"))

    def test_a_session_of_the_other_mode_is_not_served(self):
        """A managed row left behind by a switch is not a live terminal: the arbiter answers ``unsupported_transport``."""
        pin = self.terminal_session()
        self.probe._injected_probe = lambda profile, marker: {"transport": "managed", "owner": "host:1:1"}
        with self.assertRaises(DomainError):
            self.chat.send(OWNER, AGENT, "hello", "key-0000006", pin)

    def test_the_managed_send_is_unchanged_after_a_switch_back(self):
        self.terminals(False)
        self.host_starts()
        result = self.chat.send(OWNER, AGENT, "hello", "key-0000007")
        self.assertEqual((self.managed.opened, result["binding"]["transport"]), ([AGENT], "managed"))


class UploadAndControlTests(TerminalCase):
    def test_a_file_without_a_live_session_is_terminal_offline(self):
        self.chat.managed = Untouchable()
        with self.assertRaises(DomainError) as refused:
            self.chat.upload(OWNER, AGENT, "upload-key-0001", "pin-not-used", "notes.txt", b"hello")
        self.assertEqual((refused.exception.code, refused.exception.message),
                         ("terminal_offline", "Start the agent in Terminal before sending files"))

    def test_a_file_for_the_live_session_is_saved_for_its_pin(self):
        pin = self.terminal_session()
        (self.root / "workspace").mkdir()
        with mock.patch("qorqut.native_chat.uploads.save", return_value={"saved": True}) as save:
            self.assertEqual(self.chat.upload(OWNER, AGENT, "upload-key-0002", pin, "notes.txt", b"hello"), {"saved": True})
        self.assertEqual((save.call_args.args[3], self.managed.opened), (pin, []))

    def test_interrupt_answer_and_close_are_refused_as_not_managed(self):
        self.chat.managed = Untouchable()
        for call in (lambda: self.chat.interrupt(OWNER, AGENT, "pin-0000001"),
                     lambda: self.chat.answer(OWNER, AGENT, "pin-0000001", "r1", "allow"),
                     lambda: self.chat.close(OWNER, AGENT)):
            with self.assertRaises(DomainError) as refused:
                call()
            self.assertEqual((refused.exception.code, refused.exception.message), ("not_managed", "This agent runs in its terminal"))

    def test_the_selection_says_terminal_when_it_cannot_prove_the_conversation(self):
        with self.assertRaises(DomainError) as refused:
            self.chat.select(OWNER, AGENT, "claude", base.CONVERSATION, 0)
        self.assertEqual((refused.exception.code, refused.exception.message), ("not_found", "The terminal conversation is still connecting"))
        self.terminals(False)
        with self.assertRaises(DomainError) as refused:
            self.chat.select(OWNER, AGENT, "claude", base.CONVERSATION, 0)
        self.assertEqual(refused.exception.message, "The session's conversation is still connecting")


class NoWakeTests(TerminalCase):
    def setUp(self):
        super().setUp()
        self.opener = self.chat.opener
        self.opener._spawn = lambda target, *args: target(*args)
        self.host_starts()

    def test_nothing_is_wakeable_and_a_wake_is_session_closed_before_anything_else(self):
        for source in ("inbox", "chat", "telegram", "reminder"):
            with self.assertRaises(DomainError) as refused:
                self.opener.wake(self.profile(), source, force=True)
            self.assertEqual(refused.exception.code, "session_closed", source)
        self.assertFalse(self.opener.wakeable(AGENT))
        self.assertFalse(self.opener.wakeable(AGENT, owner=True))
        self.assertEqual(self.managed.opened, [])
        self.opener.wake_quietly(self.profile(), "overseer")
        self.assertEqual(self.managed.opened, [])

    def test_the_arbiter_defers_an_input_for_an_agent_that_is_not_running_and_opens_nothing(self):
        from qorqut.native_chat.contracts import InputRequest, RESULT_DEFERRED, TYPE_TELEGRAM
        result = self.arbiter.submit(InputRequest(AGENT, TYPE_TELEGRAM, "telegram-0000001", "hello", wait=False, principal_id=OWNER))
        self.assertEqual((result.state, result.reason), (RESULT_DEFERRED, TERMINAL_OFFLINE))
        self.assertEqual((self.managed.opened, self.inputs()), ([], 0))

    def test_managed_mode_wakes_as_before(self):
        self.terminals(False)
        self.assertTrue(self.opener.wakeable(AGENT))
        self.assertEqual(self.opener.wake(self.profile(), "inbox", wait=True), "live")
        self.assertEqual(self.managed.opened, [AGENT])


class SwitchingTests(TerminalCase):
    def setUp(self):
        super().setUp()
        self.opener = self.chat.opener
        self.host_starts()

    def switching(self, to):
        mode.begin(self.paths, to)
        self.addCleanup(mode.abort, self.paths)

    def test_a_wake_while_agents_are_switched_is_the_retryable_runtime_switching_in_either_mode(self):
        for terminal in (False, True):
            self.terminals(terminal)
            self.switching(mode.MANAGED if terminal else mode.TERMINAL)
            with self.assertRaises(DomainError) as refused:
                self.opener.wake(self.profile(), "inbox")
            self.assertEqual((refused.exception.code, refused.exception.message, refused.exception.retryable),
                             ("runtime_switching", "Agents are being switched. Try again in a minute.", True))
            mode.abort(self.paths)
        self.assertEqual(self.managed.opened, [])

    def test_the_owner_message_is_refused_while_switching_and_opens_nothing(self):
        self.terminals(False)
        self.switching(mode.TERMINAL)
        with self.assertRaises(DomainError) as refused:
            self.chat.send(OWNER, AGENT, "hello", "key-0000008")
        self.assertEqual((refused.exception.code, refused.exception.retryable), ("runtime_switching", True))
        self.assertEqual((self.managed.opened, self.inputs()), ([], 0))

    def test_outside_a_switch_the_managed_opener_is_untouched(self):
        self.terminals(False)
        self.assertEqual(self.opener.wake(self.profile(), "inbox", wait=True), "live")


class AttachmentTests(TerminalCase):
    def test_the_state_is_served_in_terminals_mode_after_the_owner_checks(self):
        view = {"state": "attached", "mode": "selected"}
        with mock.patch.object(self.chat.attachment, "state", return_value=view) as state:
            self.assertEqual(self.chat.attachment_state(OWNER, AGENT), view)
        state.assert_called_once_with(AGENT)
        with self.assertRaises(DomainError) as refused:
            self.chat.attachment_state("someone-else", AGENT)
        self.assertEqual(refused.exception.code, "forbidden")

    def test_a_managed_installation_does_not_register_it(self):
        self.terminals(False)
        with mock.patch.object(self.chat.attachment, "state") as state:
            with self.assertRaises(DomainError) as refused:
                self.chat.attachment_state(OWNER, AGENT)
        self.assertEqual(refused.exception.code, "not_found")
        state.assert_not_called()


class LaunchOptionsTests(TerminalCase):
    def change(self, body):
        return launch_options.change(self.chat, self.runtime, OWNER, AGENT, body, ready=lambda client: True)

    def test_the_options_stay_available_and_the_session_is_never_open(self):
        self.managed.host = object()  # a host record that must not count in Terminals mode
        view = launch_options.view(self.chat, self.runtime, OWNER, AGENT, ready=lambda client: True)
        self.assertFalse(view["session_open"])
        self.assertEqual(self.change({"model": "sonnet", "effort": "high"})["model"], "sonnet")
        self.assertEqual(self.change({"client": "claude"})["client"], "claude")

    def test_a_change_of_client_to_codex_is_refused(self):
        with self.assertRaises(DomainError) as refused:
            self.change({"client": "codex"})
        self.assertEqual((refused.exception.code, refused.exception.message),
                         ("validation_error", "Only Claude Code agents can run in terminals."))
        self.assertIsNone(launch_options.view(self.chat, self.runtime, OWNER, AGENT, ready=lambda client: True)["client"])

    def test_managed_mode_still_takes_codex_and_reports_an_open_host(self):
        self.terminals(False)
        self.managed.host = object()
        self.assertTrue(launch_options.view(self.chat, self.runtime, OWNER, AGENT, ready=lambda client: True)["session_open"])
        self.managed.host = None
        self.assertEqual(self.change({"client": "codex"})["client"], "codex")


class CatalogTests(unittest.TestCase):
    def test_the_terminal_catalog_marks_nothing_unavailable_and_keeps_the_menu_commands_for_the_terminal(self):
        found = {item["name"]: item for item in commands.catalog("claude", managed=False)["commands"]}
        self.assertNotIn("unavailable", {item["where"] for item in found.values()})
        for name in commands.BUILT_IN_TERMINAL["claude"]:
            self.assertIn(name[0], found)
        self.assertEqual((found["/model"]["where"], found["/resume"]["where"], found["/compact"]["where"], found["/clear"]["where"]),
                         ("terminal", "terminal", "chat", "chat"))
        self.assertEqual(found["/exit"]["description"], "End this session. Start the agent again to continue.")

    def test_only_claude_code_has_a_terminal_catalog(self):
        self.assertEqual(commands.catalog("codex", managed=False), {"client": None, "commands": []})

    def test_the_default_catalog_is_the_managed_one(self):
        self.assertEqual(commands.catalog("claude"), commands.catalog("claude", managed=True))
        where = {item["name"]: item["where"] for item in commands.catalog("claude")["commands"]}
        self.assertEqual((where["/model"], where["/clear"], where["/compact"]), ("unavailable", "unavailable", "chat"))

    def test_a_bare_slash_command_is_typed_with_one_trailing_space(self):
        self.assertEqual(commands.delivery_text("/compact"), "/compact ")
        self.assertEqual(commands.delivery_text("/compact keep this"), "/compact keep this")
        self.assertEqual(commands.delivery_text(" /compact"), " /compact")
        self.assertEqual(commands.delivery_text("hello"), "hello")


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class RouteTests(unittest.TestCase):
    """``GET .../attachment`` and ``GET .../commands`` through the real route, per the mode on the test's own runtime folder."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        import shutil
        from qorqut.transport.http import HTTPAPI
        self.paths = SimpleNamespace(runtime=Path(tempfile.mkdtemp(prefix="mcs-http-")).resolve())
        self.addCleanup(shutil.rmtree, self.paths.runtime, True)
        self.calls = []
        calls = self.calls

        class Chat:
            def check_events(self, principal_id, agent_id):
                calls.append(("check_events", agent_id))

            def attachment_state(self, principal_id, agent_id):
                calls.append(("attachment_state", agent_id))
                return {"state": "attached"}

        @contextmanager
        def read():
            repository = SimpleNamespace(get=lambda name: {"owner_principal_id": OWNER})
            yield SimpleNamespace(repository=lambda name, **options: repository)

        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = SimpleNamespace(database=SimpleNamespace(read=read), require_authority=lambda: None, native_chat=Chat(), paths=self.paths)
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id=OWNER, display_name="Owner"),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options: None)
        api._report_transport_success = lambda *args: None
        api._report_transport_failure = lambda *args: None
        api.context = lambda actor, slug=None, owner=False: (SimpleNamespace(principal_kind="human", principal_id=OWNER), None)

        async def handler(request):
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route("/api/{path:path}", handler, methods=["GET", "POST"])]))
        self.addCleanup(self.client.close)
        self.url = "/api/root/agents/2f1c8a9e-5b1d-4a3e-9c1e-7d1f5a6b8c90/chat"

    def test_the_attachment_read_is_served_in_terminals_mode_only(self):
        missing = self.client.get(self.url + "/attachment")
        self.assertEqual((missing.status_code, missing.json()["error"]["code"]), (404, "not_found"))
        mode.commit(self.paths, mode.TERMINAL)
        served = self.client.get(self.url + "/attachment")
        self.assertEqual((served.status_code, served.json()["data"], served.headers["cache-control"]),
                         (200, {"state": "attached"}, "private, no-store"))
        denied = self.client.get(self.url + "/attachment", headers={"Authorization": "Bearer x"})
        self.assertEqual(denied.status_code, 403)

    def catalog(self):
        with mock.patch("qorqut.composition.locations.agent_workspace", return_value=None), \
                mock.patch("qorqut.connectors._probe_environment", return_value={"HOME": None}):
            response = self.client.get(self.url + "/commands?client=claude")
        return {item["name"]: item["where"] for item in response.json()["data"]["commands"]}

    def test_the_catalog_follows_the_mode(self):
        self.assertEqual(self.catalog()["/model"], "unavailable")
        mode.commit(self.paths, mode.TERMINAL)
        terminal = self.catalog()
        self.assertEqual((terminal["/model"], terminal["/clear"]), ("terminal", "chat"))

    def test_the_runtime_route_stays_gone_in_both_modes(self):
        for terminal in (False, True):
            if terminal:
                mode.commit(self.paths, mode.TERMINAL)
            response = self.client.post(self.url + "/runtime", content=json.dumps({"mode": "managed"}),
                                        headers={"Content-Type": "application/json"})
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()
