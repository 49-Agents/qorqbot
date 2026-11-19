"""The one session opener and the off state (stage 1 of the managed cutover): every input source wakes an agent that is asleep and on,
none opens one that is off, and a failed open leaves the input waiting exactly as it was.

Runs over ``test_managed_chat_send``'s fakes: the real probe, registry, arbiter and opener, a fake managed runtime that "starts" when
``ensure_open`` is called. Wakes run inline (the opener's ``spawn`` is replaced) so nothing here is timing dependent. The real host
process is ``test_managed_end_to_end``.
"""
import isolated_cli_environment  # noqa: F401 - before any Qorqbot import

from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.agents import activity as activity_module
from qorqut.native_chat import liveness, nudges, opener as opener_module, store
from qorqut.native_chat.arbiter import SESSION_STARTING
from qorqut.native_chat.contracts import (InputRequest, RESULT_DEFERRED, TYPE_BROADCAST, TYPE_DIRECT, TYPE_NUDGE, TYPE_TELEGRAM)
from qorqut.native_chat.opener import FAILURES, OpenFailures, SessionOpener, WAKE_TEXT

from test_managed_chat_send import AGENT, OPEN_VIEW, OWNER, ChatCase, FakeManaged


class WakeCase(ChatCase):
    def setUp(self):
        super().setUp()
        self.opener = self.chat.opener
        self.opener._spawn = lambda target, *args: target(*args)  # inline: the wake is finished when the call returns
        self.addCleanup(FAILURES._streaks.clear) if hasattr(FAILURES, "_streaks") else None
        self.host_starts()
        self.delivered = []
        self.adapter.submit = self.submit
        self.adapter.name = "managed"

    def submit(self, profile, session, text, **options):
        from qorqut.native_chat.contracts import OwnerInputOutcome
        options['checkpoint']()
        self.delivered.append(text)
        return OwnerInputOutcome("submitted", "accepted_by_host", {"adapter": "managed", "client": "claude", "transport": "managed",
                                                                    "message_uuid": "u", "queued": True})

    def owner_row(self, state="running"):
        """An agent that has bound a session before has an owner row (it is never deleted); ``state`` is its recorded intent."""
        self.db.adopt(self.client.pid, self.client.started)
        self.db.connection.execute("UPDATE native_session_owners SET desired_state=? WHERE agent_id=?", (state, AGENT))
        self.db.connection.commit()
        self.managed.running = False  # adopting is not a running host: the session is asleep
        self.marker = None
        self.managed.view = dict(__import__("qorqut.managed_runtime.manager", fromlist=["CLOSED_VIEW"]).CLOSED_VIEW)

    def desired(self):
        return self.opener.desired(AGENT)

    def request(self, kind, key="k-0000000000001", text="hello"):
        return InputRequest(AGENT, kind, key, text, wait=False)


class OnOffTests(WakeCase):
    def test_an_agent_that_never_bound_is_on_and_has_nothing_to_record(self):
        self.assertIsNone(self.desired())
        self.assertFalse(self.opener.is_off(AGENT))
        self.assertTrue(self.opener.wakeable(AGENT))
        self.assertFalse(self.opener.turn_off(AGENT))

    def test_end_session_turns_the_agent_off_and_closes_it(self):
        pin = self.open()
        self.assertEqual(self.chat.close(OWNER, AGENT), {"closing": True} if self.managed.reply.get("closing", True) else {})
        self.assertEqual(self.desired(), "stopped")
        self.assertIn(("close", AGENT, "graceful"), self.managed.calls)
        self.assertTrue(self.chat.session_view(self.profile(), store.live_session(self.db.connection, AGENT))["managed"]["off"])

    def test_a_close_that_fails_leaves_the_agent_on(self):
        self.open()
        self.managed.error = DomainError("operation_failed", "no")
        with self.assertRaises(Exception):
            self.chat.close(OWNER, AGENT)
        self.assertEqual(self.desired(), "running")

    def test_the_owners_chat_message_turns_an_off_agent_on_and_opens_it(self):
        self.owner_row("stopped")
        result = self.chat.send(OWNER, AGENT, "come back", "key-on-000000001")
        self.assertEqual(self.managed.opened, [AGENT])
        self.assertEqual(self.desired(), "running")
        self.assertEqual(result["binding"]["transport"], "managed")

    def test_idle_close_service_restart_and_a_crashed_host_change_nothing(self):
        self.owner_row("running")
        for view in ({"open": False, "state": "closed", "phase": "closed"}, DomainError("host_unreachable", "gone")):
            self.managed.view = view
            self.chat.session_view(self.profile(), None)
            self.assertEqual(self.desired(), "running")


class OpenerUnitTests(WakeCase):
    def test_an_off_agent_is_not_woken_and_nothing_starts(self):
        self.owner_row("stopped")
        with self.assertRaises(DomainError) as refused:
            self.opener.wake(self.profile(), "inbox")
        self.assertEqual(refused.exception.code, "session_closed")
        self.assertEqual(self.managed.opened, [])

    def test_an_asleep_agent_is_opened_with_the_neutral_notice_never_with_the_inputs_text(self):
        self.owner_row()
        self.assertEqual(self.opener.wake(self.profile(), "inbox"), "waking")
        self.assertEqual(self.managed.opened, [AGENT])
        self.assertTrue(WAKE_TEXT.startswith("<qorqut-wake>"))
        for agent, text, uuid in self.managed.first:
            self.assertEqual(text, WAKE_TEXT)

    def test_a_wake_of_an_open_agent_opens_nothing(self):
        self.open()
        self.assertEqual(self.opener.wake(self.profile(), "inbox", wait=True), "live")
        self.assertEqual(self.managed.opened, [])

    def test_a_failed_open_is_refused_with_its_code_recorded_and_paced(self):
        self.owner_row()
        self.managed.open_error = SimpleNamespace  # replaced below
        from qorqut.managed_runtime.manager import ManagedError
        self.managed.open_error = ManagedError("cli_unavailable", "Claude Code is not installed")
        with self.assertRaises(DomainError) as first:
            self.opener.wake(self.profile(), "inbox", wait=True)
        self.assertEqual(first.exception.code, "cli_unavailable")
        # the next ask is refused at once with the same code and does not reach the host again (nothing spins)
        before = len(self.managed.opened)
        with self.assertRaises(DomainError) as second:
            self.opener.wake(self.profile(), "inbox", wait=True)
        self.assertEqual(second.exception.code, "cli_unavailable")
        self.assertEqual(len(self.managed.opened), before)
        self.assertEqual(FAILURES.streak(AGENT)[0], "cli_unavailable")

    def test_a_successful_wake_is_not_repeated_within_the_spacing(self):
        self.owner_row()
        self.assertEqual(self.opener.wake(self.profile(), "inbox", wait=True), "live")
        self.managed.running = False
        self.marker = None
        with self.assertRaises(DomainError):
            self.opener.wake(self.profile(), "inbox", wait=True)
        self.assertEqual(self.managed.opened, [AGENT])

    def test_a_wake_while_another_open_runs_does_not_start_a_second_one(self):
        self.owner_row()
        lock = self.opener._lock(AGENT)
        lock.acquire()
        self.addCleanup(lock.release)
        self.assertEqual(self.opener.wake(self.profile(), "inbox"), "waking")
        self.assertEqual(self.managed.opened, [])

    def test_without_a_managed_runtime_nothing_is_woken(self):
        self.chat.managed = None
        with self.assertRaises(DomainError):
            self.opener.wake(self.profile(), "inbox")
        self.assertFalse(self.opener.wakeable(AGENT))


class SourceTests(WakeCase):
    """One test per input source: asleep-and-on is opened and delivered once; off is not opened; a failed open leaves it waiting."""

    def test_telegram_opens_an_asleep_agent_and_the_retry_delivers_once(self):
        self.owner_row()
        first = self.arbiter.submit(self.request(TYPE_TELEGRAM))
        self.assertEqual((first.state, first.reason), (RESULT_DEFERRED, SESSION_STARTING))
        self.assertEqual(self.managed.opened, [AGENT])
        self.assertEqual(self.delivered, [], "nothing was delivered by the attempt that opened the session")
        again = self.arbiter.submit(self.request(TYPE_TELEGRAM))
        self.assertEqual((again.state, again.reason), ("submitted", "accepted_by_host"))
        self.assertEqual(self.delivered, ["hello"])
        self.arbiter.submit(self.request(TYPE_TELEGRAM))
        self.assertEqual(self.delivered, ["hello"], "the same key never delivers twice")

    def test_telegram_from_the_owner_turns_an_off_agent_on_first(self):
        self.owner_row("stopped")
        self.arbiter.submit(self.request(TYPE_TELEGRAM))
        self.assertEqual(self.desired(), "running")
        self.assertEqual(self.managed.opened, [AGENT])

    def test_a_failed_open_leaves_telegram_deferred_with_the_reason_and_nothing_stored(self):
        self.owner_row()
        from qorqut.managed_runtime.manager import ManagedError
        self.managed.open_error = ManagedError("resume_failed", "cannot resume")
        result = self.arbiter.submit(self.request(TYPE_TELEGRAM))
        self.assertEqual((result.state, result.reason), (RESULT_DEFERRED, "session_starting"))  # started in the background
        result = self.arbiter.submit(self.request(TYPE_TELEGRAM))
        self.assertEqual((result.state, result.reason), (RESULT_DEFERRED, "resume_failed"))
        self.assertEqual((self.delivered, self.inputs()), ([], 0))

    def test_a_broadcast_and_a_direct_message_wait_for_the_open_and_are_delivered_once(self):
        for kind, key in ((TYPE_BROADCAST, "k-broadcast-000001"), (TYPE_DIRECT, "k-direct-00000001")):
            with self.subTest(kind=kind):
                self.owner_row()
                self.opener._cool.clear()
                self.delivered.clear()
                result = self.arbiter.submit(self.request(kind, key, f"text for {kind}"))
                self.assertEqual(result.state, "submitted")
                self.assertEqual(self.delivered, [f"text for {kind}"])

    def test_a_broadcast_for_an_off_agent_is_not_opened(self):
        self.owner_row("stopped")
        result = self.arbiter.submit(self.request(TYPE_BROADCAST, "k-broadcast-000002"))
        self.assertEqual((result.state, result.reason), (RESULT_DEFERRED, "session_closed"))
        self.assertEqual((self.managed.opened, self.delivered), ([], []))

    def test_a_failed_open_is_not_a_delivery_for_a_direct_message(self):
        self.owner_row()
        from qorqut.managed_runtime.manager import ManagedError
        self.managed.open_error = ManagedError("driver_unavailable", "no")
        result = self.arbiter.submit(self.request(TYPE_DIRECT, "k-direct-00000002"))
        self.assertEqual((result.state, result.reason), (RESULT_DEFERRED, "driver_unavailable"))
        self.assertEqual((self.delivered, self.inputs()), ([], 0))

    def test_the_nudge_type_is_never_woken_by_the_arbiter(self):
        self.owner_row()
        result = self.arbiter.submit(self.request(TYPE_NUDGE, "k-nudge-000000001"))
        self.assertEqual((result.state, result.reason), (RESULT_DEFERRED, "session_closed"))
        self.assertEqual(self.managed.opened, [])

    def test_a_pinned_chat_send_for_an_asleep_agent_opens_it_instead_of_refusing_the_stale_pin(self):
        self.owner_row()
        result = self.chat.send(OWNER, AGENT, "hi", "key-stale-0000001", "a-binding-that-ended")
        self.assertEqual(self.managed.opened, [AGENT])
        self.assertEqual(result["binding"]["transport"], "managed")

    def test_a_chat_send_that_cannot_open_is_refused_and_stores_nothing(self):
        self.owner_row()
        from qorqut.managed_runtime.manager import ManagedError
        self.managed.open_error = ManagedError("cli_unavailable", "no")
        with self.assertRaises(DomainError) as refused:
            self.chat.send(OWNER, AGENT, "hi", "key-fail-00000001")
        self.assertEqual((refused.exception.code, self.inputs()), ("cli_unavailable", 0))

    def test_the_inbox_nudge_wakes_an_asleep_agent_and_not_an_off_one(self):
        backend = nudges.NudgeBackend(self.runtime, SimpleNamespace(), self.probe, self.registry, self.arbiter, self.locks,
                                      opener=self.opener)
        self.owner_row()
        self.assertTrue(backend._wake_agent(self.profile()))
        self.assertEqual(self.managed.opened, [AGENT])
        self.owner_row("stopped")
        self.opener._cool.clear()
        self.managed.opened.clear()
        self.assertFalse(backend._wake_agent(self.profile()))
        self.assertEqual(self.managed.opened, [])

    def test_the_inbox_status_calls_an_asleep_agent_asleep_and_an_off_one_offline(self):
        engine = SimpleNamespace(status=lambda agent_id, binding=None: {"state": "pending", "reason": "offline"})
        backend = nudges.NudgeBackend(self.runtime, engine, self.probe, self.registry, self.arbiter, self.locks, opener=self.opener)
        self.owner_row()
        self.assertEqual(backend.status(AGENT)["reason"], "asleep")
        self.owner_row("stopped")
        self.assertEqual(backend.status(AGENT)["reason"], "offline")

    def test_a_reminder_that_is_due_wakes_an_asleep_agent_and_claims_nothing(self):
        from qorqut.agents.attention_delivery import AttentionRuntime as AttentionDelivery
        delivery = AttentionDelivery.__new__(AttentionDelivery)
        self.owner_row()
        delivery._wake(self.chat, self.profile())
        self.assertEqual(self.managed.opened, [AGENT])
        self.owner_row("stopped")
        self.opener._cool.clear()
        self.managed.opened.clear()
        delivery._wake(self.chat, self.profile())
        self.assertEqual(self.managed.opened, [])


class ReaderTests(WakeCase):
    def test_availability_names_open_asleep_off_and_unavailable(self):
        a = liveness.availability
        self.assertEqual(a(open_session=True, desired_state="running", can_open=True), "open")
        self.assertEqual(a(open_session=False, desired_state=None, can_open=True), "asleep")
        self.assertEqual(a(open_session=False, desired_state="running", can_open=True), "asleep")
        self.assertEqual(a(open_session=False, desired_state="stopped", can_open=True), "off")
        self.assertEqual(a(open_session=False, desired_state="paused", can_open=True), "unavailable")
        self.assertEqual(a(open_session=False, desired_state="running", can_open=False), "unavailable")

    def test_the_chat_view_of_a_closed_agent_says_whether_it_is_off(self):
        self.owner_row()
        self.assertFalse(self.chat.session_view(self.profile(), None)["managed"]["off"])
        self.owner_row("stopped")
        self.assertTrue(self.chat.session_view(self.profile(), None)["managed"]["off"])

    def test_the_activity_list_reports_an_asleep_agent_as_asleep_and_an_off_one_as_off(self):
        tracker = activity_module.AgentActivity.__new__(activity_module.AgentActivity)
        tracker.runtime = SimpleNamespace(native_chat=self.chat)
        self.owner_row()
        owner = {"desired_state": "running"}
        self.assertEqual(tracker.availability("offline", owner), "asleep")
        self.assertEqual(tracker.availability("offline", {"desired_state": "stopped"}), "off")
        self.assertEqual(tracker.availability("running", owner), "open")
        tracker.runtime = SimpleNamespace()
        self.assertIsNone(tracker.availability("offline", owner))


class FailureStreakTests(unittest.TestCase):
    def test_a_streak_counts_failures_and_ends_at_the_first_success(self):
        now = [100.0]
        failures = OpenFailures(clock=lambda: __import__("datetime").datetime.fromtimestamp(now[0], __import__("datetime").timezone.utc))
        self.assertIsNone(failures.streak("a"))
        failures.failed("a", "cli_unavailable")
        now[0] = 200.0
        failures.failed("a", "cli_unavailable")
        code, since, count = failures.streak("a")
        self.assertEqual((code, count), ("cli_unavailable", 2))
        failures.opened("a")
        self.assertIsNone(failures.streak("a"))

    def test_only_the_repeating_reasons_make_a_streak(self):
        failures = OpenFailures()
        for code in ("open_busy", "open_timeout", "session_opening"):
            failures.failed("a", code)
            failures.failed("a", code)
        self.assertIsNone(failures.streak("a"))


class OrdubashyToolTests(WakeCase):
    """``agents/terminal_control`` for a managed worker: start = on and open, restart = close and open, kill = close and off."""

    def control(self):
        from qorqut.agents.terminal_control import TerminalControl as AgentTerminalControl
        control = AgentTerminalControl.__new__(AgentTerminalControl)
        control.runtime = SimpleNamespace(native_chat=self.chat, database=self.db, paths=self.paths, application=self.runtime.application)
        return control

    def target(self):
        return {"agent_id": AGENT}

    def profile_patch(self):
        return mock.patch.object(store, "active_profile", lambda connection, agent_id, paths: self.profile())

    def test_start_turns_a_worker_on_and_opens_it_even_from_off(self):
        self.owner_row("stopped")
        with self.profile_patch():
            detail = self.control()._execute(self.target(), "start", None, "k")
        self.assertEqual((self.desired(), self.managed.opened, detail), ("running", [AGENT], "Managed session open"))

    def test_restart_closes_and_opens_again(self):
        self.owner_row("running")
        self.managed.handle = lambda agent_id: None
        with self.profile_patch():
            detail = self.control()._execute(self.target(), "restart", None, "k")
        self.assertIn(("close", AGENT, "graceful"), self.managed.calls)
        self.assertEqual((self.managed.opened, detail), ([AGENT], "Managed session restarted"))

    def test_kill_closes_the_session(self):
        self.owner_row("running")
        detail = self.control()._execute(self.target(), "kill", None, "k")
        self.assertIn(("close", AGENT, "now"), self.managed.calls)
        self.assertIn("off", detail)

    def test_a_start_that_cannot_open_is_a_failed_action(self):
        self.owner_row("running")
        from qorqut.managed_runtime.manager import ManagedError
        self.managed.open_error = ManagedError("cli_unavailable", "no")
        with self.profile_patch(), self.assertRaises(DomainError) as failed:
            self.control()._execute(self.target(), "start", None, "k")
        self.assertEqual(failed.exception.code, "not_delivered")

    def test_status_says_on_off_asleep_open_and_working(self):
        control = self.control()
        self.owner_row("running")
        self.assertEqual({k: v for k, v in control.session_state(self.target()).items() if k in ("on", "off", "open", "asleep", "working")},
                         {"on": True, "off": False, "open": False, "asleep": True, "working": False})
        self.owner_row("stopped")
        self.assertTrue(control.session_state(self.target())["off"])
        self.managed.view = {**OPEN_VIEW, "phase": "working"}
        self.assertTrue(control.session_state(self.target())["working"])


if __name__ == "__main__":
    unittest.main()
