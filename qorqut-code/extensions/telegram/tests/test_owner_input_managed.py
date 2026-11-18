"""Telegram owner input for a managed agent: what the shared arbiter reports for a headless session, and what the owner is told.

A managed agent has no terminal, so a message that cannot be delivered now must not say "typed into the terminal when the
composer is free". The worker is exercised with stand-in bridge and store objects; nothing here reaches Telegram or an agent.
"""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from qorqut.native_chat.contracts import InputResult, RESULT_DEFERRED, RESULT_REJECTED, STATE_FAILED, STATE_SUBMITTED, STATE_UNCERTAIN
from qorqut_telegram import owner_input
from qorqut_telegram.owner_input import DEFERRALS, MANAGED_REASONS, NOTICE_REASONS, OwnerInputs


class Writer:
    """``bridge.write(...)`` as a context manager over one stored input row."""

    def __init__(self, row):
        self.row, self.updates = row, []
        repository = NS(get=lambda key: dict(self.row), update=lambda key, changes, expected_revision=None: self.updates.append(changes))
        self.uow = NS(repository=lambda name: repository,
                      connection=NS(execute=lambda *a: NS(fetchone=lambda: None)))

    def __enter__(self):
        return self.uow

    def __exit__(self, *exc):
        return False


class ManagedOwnerInputTests(unittest.TestCase):
    def inputs(self, row=None):
        row = {"id": "i1", "agent_id": "a1", "state": "queued", "attempts": 0, "revision": 1, "purpose": "owner_message",
               "notice_sent_at": None, "message_id": "m1", "sensitive": 0, **(row or {})}
        self.writer = Writer(row)
        bridge = NS(now=lambda: "2026-01-01T00:00:00Z", rng=NS(random=lambda: 1.0), write=lambda operation: self.writer, wake=lambda: None)
        return OwnerInputs(bridge), row

    def deliver(self, reason):
        """Defer an input for ``reason`` and return the notice text the owner would get (None when there is none)."""
        worker, row = self.inputs()
        sent = []
        with patch.object(owner_input.store, "message", return_value={"chat_name": "ordubashy", "id": "m1"}), \
             patch.object(owner_input, "notice", side_effect=lambda bridge, uow, message, text, key: sent.append(text)):
            worker._defer(row, reason, "2026-01-01T00:00:00Z")
        return sent[0] if sent else None

    # what the arbiter says
    def test_a_closed_managed_session_is_a_deferral_with_its_own_code(self):
        result = InputResult(state=RESULT_DEFERRED, reason="session_closed")
        outcome = OwnerInputs._outcome(result)
        self.assertEqual((outcome.status, outcome.reason), ("deferred", "session_closed"))
        self.assertEqual(DEFERRALS["session_closed"], "session_closed")

    def test_the_terminal_codes_map_as_before(self):
        for arbiter, worker in (("terminal_offline", "terminal_absent"), ("unsupported_target", "terminal_unsupported")):
            outcome = OwnerInputs._outcome(InputResult(state=RESULT_DEFERRED, reason=arbiter))
            self.assertEqual((outcome.status, outcome.reason), ("deferred", worker))
        self.assertEqual(OwnerInputs._outcome(InputResult(state=RESULT_REJECTED, reason="unsupported_runtime")).status, "failed")

    def test_every_deferral_a_managed_session_reports_stays_queued(self):
        for reason in ("session_closed", "session_starting", "session_closing", "turn_running", "host_busy", "host_unverified",
                       "host_never_received", "host_rejected", "checkpoint_failed", "stale_session"):
            with self.subTest(reason=reason):
                outcome = OwnerInputs._outcome(InputResult(state=RESULT_DEFERRED, reason=reason))
                self.assertEqual((outcome.status, outcome.reason), ("deferred", reason))

    def test_a_submitted_input_is_submitted_and_keeps_its_receipt(self):
        receipt = {"adapter": "managed", "client": "claude", "transport": "managed", "message_uuid": "u1", "queued": True}
        outcome = OwnerInputs._outcome(InputResult(state=STATE_SUBMITTED, reason="queued_mid_turn", evidence=receipt))
        self.assertEqual((outcome.status, outcome.reason, outcome.receipt), ("submitted", "queued_mid_turn", receipt))
        outcome = OwnerInputs._outcome(InputResult(state=STATE_UNCERTAIN, reason="host_timeout", evidence=receipt))
        self.assertEqual((outcome.status, outcome.reason), ("uncertain", "host_timeout"))

    # what the owner reads
    def test_a_closed_session_says_that_chat_opens_it_and_not_that_a_composer_is_involved(self):
        text = self.deliver("session_closed")
        self.assertIn("its session is not open; sending a message in Chat opens it", text)
        self.assertIn("will be delivered when its session can take it", text)
        self.assertNotIn("terminal", text.replace("nothing was started", ""))
        self.assertNotIn("composer", text)
        self.assertIn("Nothing was started or interrupted.", text)

    def test_a_running_turn_is_waited_out_in_a_sentence_about_the_agent(self):
        text = self.deliver("turn_running")
        self.assertIn("because it is working on something", text)
        self.assertNotIn("composer", text)

    def test_every_managed_reason_has_its_own_wording(self):
        for reason in MANAGED_REASONS:
            with self.subTest(reason=reason):
                text = self.deliver(reason)
                self.assertIn(NOTICE_REASONS[reason], text)
                self.assertNotIn("composer", text)
                self.assertNotIn("terminal is busy", text)

    def test_a_reason_nobody_worded_keeps_the_terminal_text(self):
        # A reason nobody worded keeps the terminal text it always had (a terminal agent's deferral).
        text = self.deliver("composer_busy")
        self.assertIn("its terminal is busy or not ready for input", text)
        self.assertIn("will be typed into the terminal when the composer is free", text)

    def test_terminal_notices_are_unchanged(self):
        for reason in ("terminal_absent", "terminal_unverified", "terminal_unsupported"):
            with self.subTest(reason=reason):
                text = self.deliver(reason)
                self.assertIn(NOTICE_REASONS[reason], text)
                self.assertIn("will be typed into the terminal when the composer is free", text)

    def test_the_notice_comes_once_per_queue_episode(self):
        worker, row = self.inputs({"notice_sent_at": "2026-01-01T00:00:00Z"})
        with patch.object(owner_input.store, "message", return_value={"chat_name": "ordubashy"}), \
             patch.object(owner_input, "notice") as notice:
            worker._defer(row, "session_closed", "2026-01-01T00:00:00Z")
        notice.assert_not_called()
        self.assertEqual(self.writer.updates[0]["deferral_reason"], "session_closed")

    # what the owner reads when it is not delivered
    def settled(self, state, code, receipt=None):
        worker, _ = self.inputs()
        sent = []
        row = {"id": "i1", "purpose": "owner_message", "sensitive": 0, "message_id": "m1", "state": state, "error_code": code,
               "receipt_json": receipt}
        with patch.object(owner_input.store, "message", return_value={"chat_name": "ordubashy", "id": "m1"}), \
             patch.object(owner_input, "notice", side_effect=lambda bridge, uow, message, text, key: sent.append(text)):
            worker._after(NS(), row, "now")
        return sent[0]

    def test_an_uncertain_managed_message_points_to_chat_not_the_terminal(self):
        for receipt in ({"transport": "managed"}, '{"transport": "managed"}', None):
            with self.subTest(receipt=receipt):
                text = self.settled("uncertain", "host_timeout", receipt)
                self.assertIn("reached its session", text)
                self.assertIn("check its Chat before resending", text)
                self.assertNotIn("terminal", text)

    def test_a_failed_managed_message_says_session_not_terminal(self):
        text = self.settled("failed", "driver_unavailable", {"transport": "managed"})
        self.assertIn("could not be delivered to its session (driver_unavailable)", text)
        self.assertNotIn("terminal", text)

    def test_a_terminal_agents_failures_read_as_before(self):
        self.assertIn("could not be typed into its terminal (terminal_unsupported)",
                      self.settled("failed", "terminal_unsupported", {"client": "claude"}))
        self.assertIn("may or may not have reached the terminal", self.settled("uncertain", "submit_unconfirmed", None))
        self.assertIn("may or may not have reached the terminal", self.settled("uncertain", "session_changed", '{"client": "claude"}'))

    def test_session_changed_on_a_managed_receipt_reads_as_a_session(self):
        self.assertIn("reached its session", self.settled("uncertain", "session_changed", {"transport": "managed"}))


if __name__ == "__main__":
    unittest.main()
