"""The paid captcha solver's client: what it sends, how it waits, and what it says when the service does not answer well.

The service is a stand-in here (a function in place of the HTTPS call); the real one is never contacted."""

import traceback
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.browser import captcha_solver

KEY = "0123456789abcdef0123456789abcdef"
TASK = {"type": "RecaptchaV2TaskProxyless", "websiteURL": "https://shop.example/checkout", "websiteKey": "SITEKEY"}
READY = {"errorId": 0, "status": "ready", "cost": "0.00095", "solution": {"gRecaptchaResponse": "ANSWER"}}


class Time:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Service:
    """The service's replies in order; a reply that is an exception is raised, as a broken connection would."""

    def __init__(self, *replies, time=None, takes=0):
        self.replies, self.calls, self.time, self.takes = list(replies), [], time, takes

    def __call__(self, url, body, timeout):
        self.calls.append((url, body, timeout))
        if self.time is not None:
            self.time.now += self.takes
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def error(code):
    return {"errorId": 1, "errorCode": code, "errorDescription": f"something about {KEY}"}


class SolveTest(unittest.TestCase):
    def solve(self, *replies, **options):
        self.time = Time()
        self.service = Service(*replies, time=self.time, takes=options.pop("takes", 0))
        return captcha_solver.solve(TASK, **{"key": KEY, "post": self.service, "clock": self.time.clock, "sleep": self.time.sleep, **options})

    def failure(self, *replies, **options):
        with self.assertRaises(DomainError) as raised:
            self.solve(*replies, **options)
        shown = "".join(traceback.format_exception(raised.exception)) + repr(raised.exception.to_dict())
        self.assertNotIn(KEY, shown)
        return raised.exception

    def test_a_task_is_created_then_asked_about_until_it_is_ready(self):
        token, cost = self.solve({"errorId": 0, "taskId": 77}, {"errorId": 0, "status": "processing"}, READY)
        self.assertEqual((token, cost), ("ANSWER", 0.00095))
        self.assertEqual([(url, body) for url, body, _ in self.service.calls], [
            ("https://api.anti-captcha.com/createTask", {"clientKey": KEY, "task": TASK}),
            ("https://api.anti-captcha.com/getTaskResult", {"clientKey": KEY, "taskId": 77}),
            ("https://api.anti-captcha.com/getTaskResult", {"clientKey": KEY, "taskId": 77})])
        self.assertEqual(self.time.now, 2 * captcha_solver.POLL)

    def test_a_turnstile_answer_is_read_from_its_own_field(self):
        ready = {"errorId": 0, "status": "ready", "solution": {"token": "TURNSTILE-ANSWER"}}
        self.assertEqual(self.solve({"errorId": 0, "taskId": 1}, ready), ("TURNSTILE-ANSWER", None))

    def test_a_cost_that_is_not_a_plain_number_is_not_recorded(self):
        self.assertEqual(self.solve({"errorId": 0, "taskId": 1}, {**READY, "cost": f"1e9 {KEY}"})[1], None)

    def test_every_address_is_https_and_the_named_service_is_the_default(self):
        self.assertTrue(all(base.startswith("https://") for base in captcha_solver.SERVICES.values()))
        self.solve({"errorId": 0, "taskId": 1}, READY, provider=" AntiCaptcha ")
        self.assertTrue(all(url.startswith("https://api.anti-captcha.com/") for url, _, _ in self.service.calls))

    def test_without_a_key_the_service_is_not_called(self):
        for key in (None, ""):
            failure = self.failure(READY, key=key)
            self.assertEqual((failure.code, self.service.calls), ("captcha_solver_no_key", []))
            self.assertIn("CAPTCHA_SOLVER_API_KEY", failure.message)

    def test_a_service_qorqbot_cannot_use_is_refused_before_any_call(self):
        failure = self.failure(READY, provider="2captcha")
        self.assertEqual((failure.code, self.service.calls), ("captcha_solver_unknown", []))
        self.assertIn("CAPTCHA_SOLVER_PROVIDER", failure.message)

    def test_the_services_errors_are_said_in_plain_words(self):
        for code, expected, words in (("ERROR_KEY_DOES_NOT_EXIST", "captcha_solver_key_refused", "refused the key"),
                                      ("ERROR_ZERO_BALANCE", "captcha_solver_no_balance", "no money left"),
                                      ("ERROR_CAPTCHA_UNSOLVABLE", "captcha_solver_unsolved", "could not solve"),
                                      ("ERROR_NO_SLOT_AVAILABLE", "captcha_solver_unsolved", "could not solve"),
                                      ("ERROR_TASK_NOT_SUPPORTED", "captcha_solver_unsupported", "does not solve this kind")):
            with self.subTest(code):
                failure = self.failure(error(code))
                self.assertEqual(failure.code, expected)
                self.assertIn(words, failure.message)
                self.assertEqual(len(self.service.calls), 1)

    def test_an_error_while_waiting_ends_the_wait(self):
        failure = self.failure({"errorId": 0, "taskId": 1}, {"errorId": 0, "status": "processing"}, error("ERROR_CAPTCHA_UNSOLVABLE"))
        self.assertEqual((failure.code, len(self.service.calls)), ("captcha_solver_unsolved", 3))

    def test_an_error_it_does_not_know_is_reported_by_its_code_only(self):
        failure = self.failure(error("ERROR_SOMETHING_NEW"))
        self.assertEqual((failure.code, failure.message), ("captcha_solver_error", "The captcha solver answered with an error (ERROR_SOMETHING_NEW)."))

    def test_an_error_code_that_could_carry_the_key_is_not_repeated(self):
        for code in (KEY, f"ERROR_{KEY}", None, 7, "error_lower"):
            with self.subTest(code=code):
                self.assertEqual(self.failure({"errorId": 1, "errorCode": code}).message, "The captcha solver answered with an error (ERROR_UNKNOWN).")

    def test_a_broken_connection_or_a_strange_reply_is_unreachable_and_names_nothing(self):
        for reply in (OSError(f"https://api.anti-captcha.com/createTask {KEY}"), ValueError(KEY), [KEY], {"errorId": 0}, {"errorId": 0, "taskId": True}):
            with self.subTest(reply=type(reply).__name__):
                failure = self.failure(reply)
                self.assertEqual(failure.message, "The captcha solver could not be reached.")

    def test_a_ready_task_without_an_answer_is_unsolved(self):
        self.assertEqual(self.failure({"errorId": 0, "taskId": 1}, {"errorId": 0, "status": "ready", "solution": {}}).code, "captcha_solver_unsolved")

    def test_a_task_that_never_gets_ready_is_given_up_at_the_deadline(self):
        failure = self.failure({"errorId": 0, "taskId": 1}, {"errorId": 0, "status": "processing"}, wait=30)
        self.assertEqual(failure.code, "captcha_solver_timeout")
        self.assertLessEqual(len(self.service.calls), 30 / captcha_solver.POLL + 1)
        self.assertLessEqual(self.time.now, 30 + captcha_solver.POLL)

    def test_a_slow_service_cannot_stretch_the_deadline(self):
        failure = self.failure({"errorId": 0, "taskId": 1}, {"errorId": 0, "status": "processing"}, wait=40, takes=14)
        self.assertEqual(failure.code, "captcha_solver_timeout")
        self.assertLessEqual(self.time.now, 40 + captcha_solver.POLL + 14)
        self.assertTrue(all(0 < timeout <= captcha_solver.REQUEST_LIMIT for _, _, timeout in self.service.calls))

    def test_no_time_left_means_no_call(self):
        self.assertEqual((self.failure(READY, wait=0).code, self.service.calls), ("captcha_solver_timeout", []))


class TaskTest(unittest.TestCase):
    def task(self, kind, **details):
        return captcha_solver.task(kind, "https://shop.example/checkout", "SITEKEY", **details)

    def test_each_captcha_is_described_the_way_the_service_names_it(self):
        base = {"websiteURL": "https://shop.example/checkout", "websiteKey": "SITEKEY"}
        for described, expected in (
                (self.task("recaptcha"), {"type": "RecaptchaV2TaskProxyless"}),
                (self.task("recaptcha", invisible=True), {"type": "RecaptchaV2TaskProxyless", "isInvisible": True}),
                (self.task("recaptcha", enterprise=True, invisible=True), {"type": "RecaptchaV2EnterpriseTaskProxyless"}),
                (self.task("recaptcha_v3", invisible=True, action="apply"),
                 {"type": "RecaptchaV3TaskProxyless", "minScore": 0.7, "isEnterprise": False, "pageAction": "apply"}),
                (self.task("recaptcha_v3", enterprise=True), {"type": "RecaptchaV3TaskProxyless", "minScore": 0.7, "isEnterprise": True}),
                (self.task("hcaptcha"), {"type": "HCaptchaTaskProxyless"}),
                (self.task("hcaptcha", invisible=True), {"type": "HCaptchaTaskProxyless", "isInvisible": True}),
                (self.task("turnstile"), {"type": "TurnstileTaskProxyless"}),
                (self.task("turnstile", action="login", cdata="abc"), {"type": "TurnstileTaskProxyless", "action": "login", "turnstileCData": "abc"})):
            with self.subTest(expected["type"]):
                self.assertEqual(described, {**base, **expected})

    def test_a_captcha_it_has_no_name_for_is_refused(self):
        with self.assertRaises(DomainError) as raised:
            self.task("cloudflare")
        self.assertEqual(raised.exception.code, "captcha_solver_unsupported")


class PostTest(unittest.TestCase):
    def test_the_call_ignores_proxy_settings_and_follows_no_redirect(self):
        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, limit):
                return b'{"errorId": 0, "taskId": 5}'

        opener = mock.Mock()
        opener.open.return_value = Reply()
        with mock.patch.object(captcha_solver, "build_opener", return_value=opener) as build, \
                mock.patch.dict("os.environ", {"https_proxy": "http://proxy.example:3128", "HTTPS_PROXY": "http://proxy.example:3128"}):
            self.assertEqual(captcha_solver._post("https://api.anti-captcha.com/createTask", {"clientKey": KEY}, 15), {"errorId": 0, "taskId": 5})
        proxies, redirects = build.call_args.args
        self.assertEqual(proxies.proxies, {})
        self.assertIsNone(redirects.redirect_request(None, None, 302, "Found", {}, "http://elsewhere.example/"))
        request = opener.open.call_args.args[0]
        self.assertEqual((request.get_method(), request.full_url, opener.open.call_args.kwargs), ("POST", "https://api.anti-captcha.com/createTask", {"timeout": 15}))


if __name__ == "__main__":
    unittest.main()
