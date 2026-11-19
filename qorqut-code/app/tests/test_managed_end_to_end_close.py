"""The second half of ``test_managed_end_to_end``: the close route, the idle close, the refusals before anything starts, and a start
that fails. Split off so each module runs well under a minute; the stack, the fake host and every helper are the first module's."""
import isolated_cli_environment  # before any Qorqbot import: tests never touch the real user's CLI files

import unittest
from unittest import mock
import uuid

from qorqut_operations.contracts import DomainError

from qorqut.managed_runtime import launcher, state as layout
from qorqut.native_chat import chat as chat_module
from qorqut.native_chat.contracts import InputRequest, TYPE_NUDGE
from qorqut.native_chat.contracts import RESULT_DEFERRED

import test_managed_end_to_end as base
from test_managed_end_to_end import DriverLauncher, LINUX, OWNER, WAIT, wait_for


class ManagedEndToEnd(base.EndToEndCase):
    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_an_open_that_gave_up_is_finished_by_the_retry_with_the_same_key_on_the_same_host(self):
        """The open ran out of time while the host was still coming up (the message may or may not have reached it yet). The host
        keeps starting; the retry (same key, no pin) finds that host, starts nothing, and the message is typed once."""
        stack = self.stack(resume=str(uuid.uuid4()), hook_delay=1.0)
        with mock.patch.object(chat_module, "OPEN_TOTAL_SECONDS", 0.3):
            with self.assertRaises(DomainError) as gave_up:
                stack.chat.send(OWNER, stack.agent, "slow to announce", "key-retry-00001")
        self.assertEqual((gave_up.exception.code, gave_up.exception.retryable), ("open_timeout", True))
        with stack.db.read() as uow:
            self.assertEqual(uow.connection.execute("SELECT COUNT(*) FROM native_inputs").fetchone()[0], 0)
        wait_for(stack.host_running, WAIT, message="the host that was still starting to come up")
        retried = stack.chat.send(OWNER, stack.agent, "slow to announce", "key-retry-00001")
        self.assertTrue(retried["created"])
        self.assertEqual(self.submitted(stack, retried)["reason"], "accepted_by_host")
        self.assertEqual((len(stack.logged("start")), [entry["text"] for entry in stack.logged("send")]), (1, ["slow to announce"]))
        again = stack.chat.send(OWNER, stack.agent, "slow to announce", "key-retry-00001")
        self.assertEqual((again["created"], again["input"]["id"]), (False, retried["input"]["id"]))
        self.assertEqual(stack.observer.errors, [])

    # ===== close, reopen, the old pin ===============================================================================

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_the_close_route_ends_the_session_and_the_next_message_resumes_the_same_conversation(self):
        stack = self.stack()
        chat, agent = stack.chat, stack.agent
        first = self.say(stack, "first session", None, key="key-session-one")
        old_pin = first["binding"]["binding_id"]
        self.submitted(stack, first)
        first_host = launcher.inspect_host(stack.state).host
        with self.assertRaises(DomainError) as stale:
            chat.close(OWNER, agent, "not-the-live-binding")
        self.assertEqual(stale.exception.code, "terminal_changed")
        self.assertEqual(chat.close(OWNER, agent, old_pin), {"closing": True})
        self.assertTrue(launcher.wait_until_gone(stack.state, WAIT))
        wait_for(lambda: stack.live() is None, WAIT, message="the binding to end")
        self.assertFalse(layout.process_alive(first_host.claude_pid, first_host.claude_identity))
        # the earlier input keeps its own binding after its session ended; the repeat of it is still the same input
        repeat = chat.send(OWNER, agent, "first session", "key-session-one")
        self.assertEqual((repeat["created"], repeat["binding"]["binding_id"]), (False, old_pin))
        self.assertFalse(stack.host_running())

        second = self.say(stack, "second session", None, key="key-session-two")
        new_pin = second["binding"]["binding_id"]
        self.assertNotEqual(new_pin, old_pin)
        self.submitted(stack, second)
        sessions = stack.sessions()
        self.assertEqual([(row["generation"], row["transport"]) for row in sessions], [(1, "managed"), (2, "managed")])
        # the conversation is the same one: its transcript exists, so the next message resumes it (and /role is not sent again)
        self.assertEqual(sessions[0]["conversation_id"], sessions[1]["conversation_id"])
        self.assertNotEqual(launcher.inspect_host(stack.state).host.host_pid, first_host.host_pid)
        self.assertEqual([entry["resume"] for entry in stack.logged("start")], [None, sessions[0]["conversation_id"]])
        self.assertEqual([entry["text"] for entry in stack.logged("send")], ["/role", "first session", "second session"])
        # a message pinned to the session that ended is refused: it is never typed into the new one
        with self.assertRaises(DomainError) as old:
            chat.send(OWNER, agent, "to the old one", "key-to-old-0001", old_pin)
        self.assertEqual(old.exception.code, "terminal_changed")
        self.assertNotIn("to the old one", [entry["text"] for entry in stack.logged("send")])
        self.assertEqual(stack.observer.errors, [])

    # ===== the host closes itself when idle =========================================================================

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_an_idle_host_closes_itself_and_the_binding_ends_with_it(self):
        stack = self.stack(idle_seconds=4)
        agent = stack.agent
        first = self.say(stack, "one short turn", None, key="key-idle-0001")
        self.submitted(stack, first)
        self.assertTrue(stack.host_running())
        self.assertTrue(launcher.wait_until_gone(stack.state, WAIT))
        self.assertEqual(layout.read_host(stack.state)["exit"]["reason"], "idle")
        wait_for(lambda: stack.live() is None, WAIT, message="the binding to end")
        self.assertEqual(stack.sessions()[-1]["end_reason"], "process_gone")
        # The host's summary is cached for up to 5 seconds (ManagedRuntime.snapshot), so for that long after the host left Chat
        # still says "starting the session" (binding_not_ready, cannot send). It settles to session_closed within the cache's age.
        wait_for(lambda: stack.view()["reason"] == "session_closed", 12, message="the view to show the closed session")
        view = stack.view()
        self.assertEqual((view["reason"], view["can_send"], view["managed"]["open"]), ("session_closed", True, False))
        result = stack.arbiter.submit(InputRequest(agent, TYPE_NUDGE, "nudge-after-idle-1", "inbox", wait=False))
        self.assertEqual((result.state, result.reason), (RESULT_DEFERRED, "session_closed"))
        self.assertEqual(stack.observer.errors, [])

    # ===== refusals before anything starts ==============================================================================

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_a_menu_command_a_bad_key_and_a_closing_exit_start_nothing(self):
        stack = self.stack()
        chat, agent = stack.chat, stack.agent
        for text, key, code in (("/clear", "key-menu-00001", "managed_command"), ("/model", "key-menu-00002", "managed_command"),
                                ("/exit", "key-exit-00001", "session_closed"), ("hello", "short", "validation_error"),
                                ("   ", "key-blank-0001", "validation_error")):
            with self.subTest(text=text):
                with self.assertRaises(DomainError) as refused:
                    chat.send(OWNER, agent, text, key)
                self.assertEqual(refused.exception.code, code)
        self.assertFalse(stack.host_running())
        self.assertEqual(stack.logged(), [])
        with stack.db.read() as uow:
            self.assertEqual(uow.connection.execute("SELECT COUNT(*) FROM native_inputs").fetchone()[0], 0)
        self.assertEqual(stack.observer.errors, [])

    @unittest.skipUnless(LINUX, "the host's process checks need Linux or macOS")
    def test_a_start_that_fails_is_refused_with_its_code_and_stores_nothing(self):
        stack = self.stack()
        with mock.patch.object(DriverLauncher, "start_host", side_effect=launcher.LaunchError("not_ready", "slow")):
            with self.assertRaises(DomainError) as refused:
                stack.chat.send(OWNER, stack.agent, "hello", "key-fail-000001")
        self.assertEqual((refused.exception.code, refused.exception.retryable), ("open_timeout", True))
        self.assertEqual(stack.live(), None)
        with stack.db.read() as uow:
            self.assertEqual(uow.connection.execute("SELECT COUNT(*) FROM native_inputs").fetchone()[0], 0)
        self.assertFalse(stack.host_running())


if __name__ == "__main__":
    unittest.main()
