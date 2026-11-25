"""A blocked Context input tells the coordinator whose it is and what follows, within one inbox summary."""
import unittest
from unittest import mock

from qorqut_operations.discussion.events import SUMMARY_LIMIT
from qorqut_context.integration.queue import transitions

INPUT = "0f8fad5b-d9cb-469f-a165-70867728950e"
EVENT = "evt-7c9e6679-7425-40de-944b-e07fc1f90ae7"


class BlockedNoticeTest(unittest.TestCase):
    def notice(self, event_id, error):
        with mock.patch.object(transitions, "_notify", side_effect=lambda *arguments: arguments) as sent:
            transitions._notify_blocked("uow", "driver", INPUT, event_id, 3, error, "ordubashy")
        (uow, driver, recipient, input_id, attempt, event_type, text), = [call.args for call in sent.call_args_list]
        self.assertEqual((uow, driver, recipient, input_id, attempt, event_type),
                         ("uow", "driver", "ordubashy", INPUT, 3, "context.input_blocked"))
        return text

    def test_it_names_the_input_what_waits_behind_it_and_the_coordinators_next_steps(self):
        text = self.notice(EVENT, {"code": "model_failed", "message": "The model command is not signed in"})
        self.assertIn(f"Context input {INPUT} is blocked on attempt 3 (event {EVENT})", text)
        self.assertIn("later inputs for the same source wait", text)
        for step in ("context_queue_get", "one context_queue_retry", "a blocking Signoff for the owner"):
            self.assertIn(step, text)
        self.assertTrue(text.endswith("Error: model_failed: The model command is not signed in"))

    def test_the_next_steps_come_before_the_error_and_a_long_error_still_fits_one_summary(self):
        text = self.notice(EVENT, {"code": "context_validation_failed", "message": "m" * 2000})
        self.assertLess(text.index("Yours to resolve"), text.index("Error:"))
        self.assertLessEqual(len(text), SUMMARY_LIMIT)
        self.assertEqual(text.count("m" * transitions.BLOCKED_ERROR_LIMIT), 1)

    def test_an_input_that_never_got_an_event_names_none(self):
        text = self.notice(None, {"code": "preflight_failed", "message": "Context could not be checked"})
        self.assertIn(f"Context input {INPUT} is blocked on attempt 3; later inputs", text)
        self.assertNotIn("event", text.split(";")[0])


if __name__ == "__main__":
    unittest.main()
