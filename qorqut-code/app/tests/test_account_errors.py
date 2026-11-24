"""How failed turns are labelled (usage limit, sign-in, neither) and how the product-wide limit is remembered."""

import unittest
from datetime import datetime, timedelta, timezone

from qorqut import account_state
from qorqut.managed_runtime import account_errors as errors


class ClaudeLabelTest(unittest.TestCase):
    def test_codes_the_client_sends_decide_the_kind(self):
        self.assertEqual(errors.classify_claude(error="authentication_failed")["kind"], "sign_in")
        self.assertEqual(errors.classify_claude(error="oauth_org_not_allowed")["kind"], "sign_in")
        self.assertEqual(errors.classify_claude(api_error="stored_login_refused")["kind"], "sign_in")
        self.assertEqual(errors.classify_claude(error="rate_limit", api_error="usage_limit_reached")["kind"], "usage_limit")
        self.assertEqual(errors.classify_claude(error="rate_limit", rate_limit_info={"status": "rejected"})["kind"], "usage_limit")
        self.assertIsNone(errors.classify_claude(error="rate_limit", api_error="overloaded"))
        self.assertIsNone(errors.classify_claude(error="server_error", text="You've hit your limit"))

    def test_the_reset_time_comes_from_the_structured_epoch_first(self):
        found = errors.classify_claude(error="rate_limit", api_error="usage_limit_reached",
                                       rate_limit_info={"status": "rejected", "resetsAt": 1795910400},
                                       text="You've hit your limit · resets Nov 28, 7pm (America/New_York)")
        self.assertEqual(found, {"kind": "usage_limit", "resets_at": "2026-11-29T00:00:00Z", "resets_text": "Nov 28, 7pm (America/New_York)"})

    def test_text_is_only_a_fallback_anchored_on_the_known_sentence(self):
        self.assertEqual(errors.classify_text("claude", "You've hit your limit · resets Nov 28, 7pm (America/New_York)")["resets_at"],
                         "2026-11-29T00:00:00Z")
        self.assertEqual(errors.classify_text("claude", "Please run /login")["kind"], "sign_in")
        self.assertIsNone(errors.classify_text("claude", "Our docs say: You've hit your limit"))
        self.assertIsNone(errors.classify_text("claude", None))


class CodexLabelTest(unittest.TestCase):
    def test_error_info_names_decide_and_the_snapshot_gives_the_reset(self):
        found = errors.classify_codex("usageLimitExceeded", "You've hit your usage limit. Try again in 4 days 3 hours.",
                                      {"primary": {"usedPercent": 100, "resetsAt": 1795910400}})
        self.assertEqual((found["kind"], found["resets_at"], found["resets_text"]), ("usage_limit", "2026-11-29T00:00:00Z", "in 4 days 3 hours"))
        self.assertEqual(errors.classify_codex("unauthorized")["kind"], "sign_in")
        self.assertIsNone(errors.classify_codex("badRequest", "You've hit your usage limit"))
        self.assertEqual(errors.classify_codex(None, "You've hit your usage limit. Try again at Nov 28th, 2026 7:00 PM.")["kind"], "usage_limit")


class LastTurnTest(unittest.TestCase):
    def test_a_driver_label_is_used_and_an_old_host_falls_back_to_the_text(self):
        turn = {"status": "error", "error": "x", "error_kind": "usage_limit", "resets_at": "2026-11-29T00:00:00Z", "resets_text": "Nov 28"}
        self.assertEqual(errors.label_of(turn, "claude"), {"kind": "usage_limit", "resets_at": "2026-11-29T00:00:00Z", "resets_text": "Nov 28"})
        self.assertIsNone(errors.label_of({"status": "error", "error": "You've hit your limit", "error_kind": None}, "claude"))
        self.assertEqual(errors.label_of({"status": "error", "error": "Please run /login"}, "claude")["kind"], "sign_in")
        self.assertIsNone(errors.label_of({"status": "completed", "error": "Please run /login"}, "claude"))
        self.assertIsNone(errors.label_of(None, "claude"))


class ProductLimitTest(unittest.TestCase):
    def setUp(self):
        account_state.reset_for_tests(); self.addCleanup(account_state.reset_for_tests)
        self.t0 = datetime(2026, 11, 24, 10, tzinfo=timezone.utc)
        self.later = (self.t0 + timedelta(days=1)).isoformat().replace("+00:00", "Z")

    def test_one_limited_agent_marks_the_whole_product_until_its_reset_time(self):
        account_state.observe_turn("claude", "usage_limit", self.later, "tomorrow", self.t0, "error", now=self.t0)
        self.assertTrue(account_state.limit("claude", now=self.t0)["reached"])
        self.assertFalse(account_state.limit("codex", now=self.t0)["reached"])
        self.assertFalse(account_state.limit("claude", now=self.t0 + timedelta(days=2))["reached"])

    def test_a_later_success_ends_the_mark_an_older_one_does_not(self):
        account_state.observe_turn("claude", "usage_limit", None, None, self.t0, "error")
        account_state.observe_turn("claude", None, None, None, self.t0 - timedelta(hours=1), "completed")
        self.assertTrue(account_state.limit("claude")["reached"])
        account_state.observe_turn("claude", None, None, None, self.t0 + timedelta(hours=1), "completed")
        self.assertFalse(account_state.limit("claude")["reached"])

    def test_a_switch_clears_the_limit_and_ignores_turns_from_before_it(self):
        account_state.observe_turn("claude", "usage_limit", None, None, self.t0, "error")
        account_state.record_switch("claude", previous_account={"email": "a"}, account={"email": "b"}, changed=True, now=self.t0 + timedelta(hours=1))
        self.assertFalse(account_state.limit("claude")["reached"])
        account_state.observe_turn("claude", "usage_limit", None, None, self.t0 + timedelta(minutes=30), "error")
        self.assertFalse(account_state.limit("claude")["reached"])
        account_state.update_switch("claude", state="done", restarted=2, continued=1)
        entry = account_state.switch("claude")
        self.assertEqual((entry["state"], entry["restarted"], entry["continued"], entry["changed"]), ("done", 2, 1, True))
        self.assertIsNone(account_state.switch("codex"))

    def test_the_problem_notice_names_the_account_and_the_other_product(self):
        view = {"claude": {"signed_in": True, "account": {"email": "o@x.y", "plan": "max"}, "installed": True},
                "codex": {"signed_in": True, "installed": True}}
        found = account_state.problem("claude", "sign_in", None, None, loader=lambda: view)
        self.assertEqual((found["account"], found["other_provider"]), ({"email": "o@x.y", "plan": "max"}, {"id": "codex", "ready": True}))


if __name__ == "__main__":
    unittest.main()
