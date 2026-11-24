"""What follows a change of account (``native_chat/account_switch.py``, ``SessionOpener.restart``), over ``test_managed_chat_send``'s
fakes: the real probe, registry, arbiter and opener, and a fake managed runtime. Nothing starts a real session or CLI."""
import isolated_cli_environment  # noqa: F401 - before any Qorqbot import

from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut import account_state
from qorqut.managed_runtime import options as stored
from qorqut.native_chat import account_switch, store
from qorqut.native_chat.account_switch import AccountSwitch

from test_managed_chat_send import AGENT, CONVERSATION, OPEN_VIEW, OWNER, ChatCase

OLD = "22222222-2222-4222-8222-222222222222"


class RestartCase(ChatCase):
    def setUp(self):
        super().setUp()
        account_state.reset_for_tests()
        self.opener = self.chat.opener
        self.order = []
        self.managed.host = SimpleNamespace(record={"session_id": OLD, "client": "claude"})
        self.managed.close = self.close
        self.managed.on_open = self.fresh
        self.db.adopt(self.client.pid, self.client.started)           # the agent has bound a session before: it is on
        self.managed.running = False
        self.marker = None

    def close(self, agent_id, mode="graceful"):
        self.order.append(("close", mode))
        self.managed.host = None
        return True

    def fresh(self):
        self.order.append("open")
        self.bring_up()
        self.managed.host = SimpleNamespace(record={"session_id": CONVERSATION, "client": "claude"})


class OpenerRestartTests(RestartCase):
    def test_the_session_is_closed_changed_and_opened_again_resuming_with_the_notice_under_the_agents_flight(self):
        during = []

        def between():
            during.append((self.opener._lock(AGENT).locked(), self.managed.host))
            self.order.append("between")

        binding = self.opener.restart(self.profile(), "NOTICE", "account:claude:1", between=between)
        self.assertTrue(binding)
        self.assertEqual(self.order, [("close", "graceful"), "between", "open"])
        self.assertEqual(during, [(True, None)])                      # nothing runs while it changes; the flight is held
        self.assertEqual(self.managed.open_options[-1]["new_conversation"], False)         # the conversation is resumed, not replaced
        expected = self.managed.first_message_uuid(AGENT, "account:claude:1", "NOTICE")
        self.assertEqual(self.managed.first, [(AGENT, "NOTICE", expected)])
        self.assertFalse(self.opener._lock(AGENT).locked())

    def test_without_a_notice_nothing_is_sent(self):
        self.opener.restart(self.profile(), None, "account:claude:1")
        self.assertEqual((self.order, self.managed.first), ([("close", "graceful"), "open"], []))

    def test_a_session_that_is_not_open_is_only_opened(self):
        self.managed.host = None
        self.opener.restart(self.profile(), None, "account:claude:1")
        self.assertEqual(self.order, ["open"])

    def test_an_agent_that_is_off_is_refused_and_neither_closed_nor_turned_on(self):
        self.db.connection.execute("UPDATE native_session_owners SET desired_state='stopped' WHERE agent_id=?", (AGENT,))
        with self.assertRaises(DomainError) as refused:
            self.opener.restart(self.profile(), "NOTICE", "k-1")
        self.assertEqual((refused.exception.code, self.order), ("agent_off", []))

    def test_a_session_that_does_not_close_is_refused_and_not_opened(self):
        self.managed.close = lambda agent_id, mode="graceful": self.order.append(("close", mode))
        with self.assertRaises(DomainError) as refused:
            self.opener.restart(self.profile(), "NOTICE", "k-1", close_seconds=0.05)
        self.assertEqual((refused.exception.code, self.managed.opened), ("close_timeout", []))

    def test_a_host_with_no_conversation_yet_is_not_closed(self):
        self.managed.host = SimpleNamespace(record={})
        with self.assertRaises(DomainError) as refused:
            self.opener.restart(self.profile(), "NOTICE", "k-1")
        self.assertEqual((refused.exception.code, self.order), ("session_opening", []))

    def test_without_a_managed_runtime_nothing_is_restarted(self):
        self.chat.managed = None
        with self.assertRaises(DomainError) as refused:
            self.opener.restart(self.profile(), "NOTICE", "k-1")
        self.assertEqual(refused.exception.code, "managed_unavailable")


class RestartProductTests(RestartCase):
    """``AccountSwitch.restart_product``: which sessions, which of them are told to carry on, and the counts."""

    def setUp(self):
        super().setUp()
        self.switch = AccountSwitch(self.chat)
        self.restarted = []
        self.managed.running_hosts = lambda: [{"agent_id": AGENT, "state": "ready", "host_pid": 1, "session_id": OLD, "instance": "i"}]
        patcher = mock.patch.object(store, "active_profile", lambda connection, agent_id, paths: self.profile())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.chat.opener.restart = self.restart

    def restart(self, profile, text, key, **options):
        if self.fail:
            raise DomainError("open_timeout", "slow", retryable=True)
        self.restarted.append((profile["agent_id"], text, key))
        return "binding"

    fail = False

    def view(self, **fields):
        self.managed.view = {**OPEN_VIEW, **fields}

    def test_a_session_that_stopped_on_a_usage_limit_is_restarted_and_told_to_carry_on(self):
        self.view(turn_error_kind="usage_limit")
        counts = self.switch.restart_product("claude", "2026-11-24T10:00:00Z")
        self.assertEqual(counts, {"restarted": 1, "continued": 1, "failed": 0})
        agent, text, key = self.restarted[0]
        self.assertEqual((agent, key), (AGENT, "account:claude:2026-11-24T10:00:00Z"))
        self.assertTrue(text.startswith("<qorqut-account-notice>"))          # an opening tag: Chat shows it as an internal notice
        self.assertIn("usage limit was reached", text)

    def test_a_session_that_stopped_on_a_sign_in_problem_is_told_so(self):
        self.view(turn_error_kind="sign_in")
        self.switch.restart_product("claude", "k")
        self.assertIn("could not sign in", self.restarted[0][1])

    def test_a_session_that_was_fine_is_restarted_and_sent_nothing(self):
        self.view(turn_error_kind=None)
        self.assertEqual(self.switch.restart_product("claude", "k"), {"restarted": 1, "continued": 0, "failed": 0})
        self.assertIsNone(self.restarted[0][1])

    def test_only_the_sessions_of_the_product_are_touched(self):
        self.view(turn_error_kind="usage_limit")
        self.assertEqual(self.switch.restart_product("codex", "k"), {"restarted": 0, "continued": 0, "failed": 0})
        self.managed.host = SimpleNamespace(record={"session_id": OLD, "client": "codex"})
        self.assertEqual(self.switch.restart_product("codex", "k")["continued"], 1)
        self.assertEqual(self.switch.restart_product("claude", "k"), {"restarted": 0, "continued": 0, "failed": 0})

    def test_a_host_that_cannot_be_verified_or_has_no_agent_is_left_alone(self):
        self.managed.host_error = RuntimeError("unverified")
        self.assertEqual(self.switch.restart_product("claude", "k")["restarted"], 0)
        self.managed.host_error = None
        with mock.patch.object(store, "active_profile", lambda connection, agent_id, paths: None):
            self.assertEqual(self.switch.restart_product("claude", "k")["restarted"], 0)
        self.assertEqual(self.restarted, [])

    def test_a_session_that_cannot_be_restarted_is_counted_and_does_not_stop_the_rest(self):
        self.fail = True
        self.assertEqual(self.switch.restart_product("claude", "k"), {"restarted": 0, "continued": 0, "failed": 1})

    def test_agents_in_terminals_are_not_touched(self):
        with mock.patch("qorqut.managed_runtime.mode.is_terminal", return_value=True):
            self.assertEqual(self.switch.restart_product("claude", "k")["restarted"], 0)

    def test_the_handler_writes_the_counts_for_settings_and_ends_the_switch(self):
        self.view(turn_error_kind="usage_limit")
        entry = account_state.record_switch("claude", previous_account={"email": "a@x.y", "plan": "max"},
                                            account={"email": "b@x.y", "plan": "max"}, changed=True)
        self.switch.after_sign_in("claude", entry)
        done = account_state.switch("claude")
        self.assertEqual((done["state"], done["restarted"], done["continued"], done["failed"]), ("done", 1, 1, 0))

    def test_the_chat_registers_the_handler_when_it_starts(self):
        from qorqut import agent_cli_sign_in
        self.addCleanup(agent_cli_sign_in.set_completion_handler, None)
        self.managed.start = lambda: None
        self.backend.start = lambda: None
        self.chat.start()
        self.assertEqual(agent_cli_sign_in._completion_handler, self.chat.account_switch.after_sign_in)


class UseOtherProductTests(RestartCase):
    def setUp(self):
        super().setUp()
        self.ready = {"claude": True, "codex": True}
        for target in (mock.patch.object(stored, "ready_function", lambda environ=None, run=None: (lambda client: self.ready[client])),
                       mock.patch("qorqut.native_chat.launch_options.stored.ready_function",
                                  lambda environ=None, run=None: (lambda client: self.ready[client]))):
            target.start()
            self.addCleanup(target.stop)
        self.chat.account_switch = AccountSwitch(self.chat)

    def use(self, **options):
        return self.chat.account_switch.use_other_product(OWNER, AGENT, **options)

    def test_an_open_session_moves_to_the_other_product_with_a_new_conversation_and_a_notice(self):
        self.managed.view = {**OPEN_VIEW, "turn_error_kind": "usage_limit", "provider": "claude"}
        result = self.use()
        self.assertEqual((result["client"], result["previous_client"], result["restarted"], result["continued"]), ("codex", "claude", True, True))
        self.assertEqual(stored.read(self.paths, AGENT).client, "codex")
        self.assertEqual(self.order[:2], [("close", "graceful"), "open"])
        text = self.managed.first[0][1]
        self.assertTrue(text.startswith("<qorqut-account-notice>"))
        self.assertIn("Codex cannot continue it", text)
        self.assertIn("because its usage limit was reached on Claude Code", text)

    def test_an_agent_with_no_session_only_has_its_product_changed(self):
        self.managed.host = None
        result = self.use()
        self.assertEqual((result["client"], result["restarted"], result["continued"], self.order, self.managed.first), ("codex", False, False, [], []))

    def test_the_client_can_be_named_and_must_differ(self):
        stored.write(self.paths, AGENT, client="codex")
        self.managed.host = None
        with self.assertRaises(DomainError) as same:
            self.use(client="codex")
        self.assertIn("already uses Codex", same.exception.message)
        self.assertEqual(self.use(client="claude")["client"], "claude")

    def test_a_product_that_is_not_ready_is_refused_and_nothing_changes(self):
        self.ready["codex"] = False
        with self.assertRaises(DomainError) as refused:
            self.use()
        self.assertEqual((refused.exception.code, self.order, stored.read(self.paths, AGENT).client), ("cli_not_signed_in", [], None))
        self.assertIn("Settings > Agents", refused.exception.message)

    def test_terminals_cannot_move_to_codex(self):
        with mock.patch("qorqut.managed_runtime.mode.is_terminal", return_value=True):
            with self.assertRaises(DomainError):
                self.use()

    def test_only_the_owner_may(self):
        with self.assertRaises(DomainError) as refused:
            self.chat.account_switch.use_other_product("someone", AGENT)
        self.assertEqual(refused.exception.code, "forbidden")


class AccountProblemTests(ChatCase):
    """What Chat's session summary says about an account problem."""

    VIEW = {"claude": {"installed": True, "signed_in": True, "account": {"email": "me@x.y", "plan": "max"}},
            "codex": {"installed": True, "signed_in": True, "account": {"email": None, "plan": None}}}

    def setUp(self):
        super().setUp()
        account_state.reset_for_tests()
        account_state.remember_clients(self.VIEW)

    def summary(self, **fields):
        self.managed.view = {**OPEN_VIEW, **fields}
        return self.chat._managed_summary(self.profile())

    def test_a_limited_turn_gets_the_notice_with_the_account_and_the_other_product(self):
        summary = self.summary(turn_error="You've hit your limit", turn_error_kind="usage_limit", provider="claude",
                               turn_error_resets_at="2026-11-29T00:00:00Z", turn_error_resets_text="Nov 28, 7pm (America/New_York)")
        self.assertEqual(summary["turn_error_kind"], "usage_limit")
        self.assertEqual(summary["account_problem"], {
            "kind": "usage_limit", "provider": "claude", "resets_at": "2026-11-29T00:00:00Z", "resets_text": "Nov 28, 7pm (America/New_York)",
            "account": {"email": "me@x.y", "plan": "max"}, "other_provider": {"id": "codex", "ready": True}})

    def test_a_sign_in_problem_names_no_reset(self):
        problem = self.summary(turn_error="Login expired", turn_error_kind="sign_in", provider="codex")["account_problem"]
        self.assertEqual((problem["kind"], problem["provider"], problem["resets_at"], problem["account"]), ("sign_in", "codex", None, {"email": None, "plan": None}))
        self.assertEqual(problem["other_provider"], {"id": "claude", "ready": True})

    def test_the_other_product_is_not_ready_when_it_is_not_signed_in(self):
        account_state.remember_clients({**self.VIEW, "codex": {"installed": True, "signed_in": False, "account": None}})
        problem = self.summary(turn_error_kind="usage_limit", provider="claude")["account_problem"]
        self.assertEqual(problem["other_provider"], {"id": "codex", "ready": False})

    def test_the_product_wide_reset_time_is_the_one_shown(self):
        account_state.observe_turn("claude", "usage_limit", "2026-11-30T00:00:00Z", "Nov 29", "2026-11-24T10:00:00Z", "error")
        problem = self.summary(turn_error_kind="usage_limit", provider="claude", turn_error_resets_at="2026-11-29T00:00:00Z")["account_problem"]
        self.assertEqual(problem["resets_at"], "2026-11-30T00:00:00Z")

    def test_no_problem_no_notice(self):
        summary = self.summary(turn_error=None, turn_error_kind=None, provider="claude")
        self.assertIsNone(summary["account_problem"])
        self.assertIsNone(self.summary()["account_problem"])
        self.assertIn("turn_error_kind", __import__("qorqut.native_chat.chat", fromlist=["MANAGED_FIELDS"]).MANAGED_FIELDS)


if __name__ == "__main__":
    unittest.main()
