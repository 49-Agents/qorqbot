"""The owner's Qorqbot account against a fake account service: sign-in, token custody, heartbeats, the offline fallback
and the release notice."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest

from qorqut_operations.contracts import DomainError

from qorqut import account


class FakeService:
    """Records each request; ``replies`` maps a route to (status, body)."""
    def __init__(self):
        self.requests, self.replies = [], {}
        service = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                service.requests.append({"method": self.command, "path": self.path, "body": body,
                                         "auth": self.headers.get("Authorization")})
                status, reply = service.replies.get(self.path, (204, None))
                data = json.dumps(reply).encode() if reply is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            do_GET = do_POST = _reply

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


VERIFIED = (200, {"token": "secret-token", "account": {"id": "a1", "email": "sam@example.com", "use": "work",
                                                       "company": "Acme", "created": "2026-11-12T00:00:00Z"}})


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path(tempfile.mkdtemp())
        self.service = FakeService()
        self.addCleanup(self.service.close)
        self.env = {"QORQBOT_ACCOUNT_API": self.service.url}
        account._release_cache.clear()
        account._heartbeat_started.clear()
        account._license_started.clear()
        account._license_started[str(self.runtime)] = time.monotonic()   # the licence check is exercised directly
        # The background refresh is exercised directly; keep owner_view from starting threads against other tests.
        account._release_cache["checked"] = 1e18

    def sign_in(self):
        self.service.replies["/v1/auth/email/verify"] = VERIFIED
        return account.verify(self.runtime, {"email": "Sam@Example.com ", "code": "abcd-1234", "use": "work",
                                             "company": " Acme ", "marketing_ok": True}, self.env)

    def test_start_sends_the_email_and_a_stable_installation_id(self):
        account.start(self.runtime, {"email": " Sam@Example.com"}, self.env)
        account.start(self.runtime, {"email": "sam@example.com"}, self.env)
        first, second = self.service.requests
        self.assertEqual(first["path"], "/v1/auth/email/start")
        self.assertEqual(first["body"]["email"], "sam@example.com")
        self.assertEqual(first["body"]["install_id"], second["body"]["install_id"])
        self.assertEqual(stat.S_IMODE(os.stat(account.path(self.runtime)).st_mode), 0o600)

    def test_invalid_input_is_refused_before_any_request(self):
        for body in ({"email": "nope"}, {}, {"email": 3}):
            with self.subTest(body=body), self.assertRaises(DomainError):
                account.start(self.runtime, body, self.env)
        for body in ({"email": "a@b.co", "code": "short", "use": "personal"},
                     {"email": "a@b.co", "code": "ABCD1234", "use": "team"},
                     {"email": "a@b.co", "code": "ABCD1234", "use": "work", "company": " "}):
            with self.subTest(body=body), self.assertRaises(DomainError):
                account.verify(self.runtime, body, self.env)
        self.assertEqual(self.service.requests, [])

    def test_verify_keeps_the_token_server_side(self):
        view = self.sign_in()
        sent = self.service.requests[-1]["body"]
        self.assertEqual((sent["email"], sent["code"], sent["company"], sent["marketing_ok"]),
                         ("sam@example.com", "ABCD1234", "Acme", True))
        self.assertEqual(sent["app_version"], "dev")
        self.assertTrue(view["signed_in"])
        self.assertEqual(view["account"]["email"], "sam@example.com")
        self.assertTrue(view["license"]["commercial"])
        self.assertNotIn("secret-token", json.dumps(view))
        self.assertEqual(json.loads(account.path(self.runtime).read_text())["token"], "secret-token")

    def test_required_only_for_released_builds_unless_chosen(self):
        self.assertFalse(account.required({}))
        self.assertTrue(account.required({"QORQBOT_ACCOUNT": "on"}))
        released = Path(tempfile.mkdtemp())
        (released / "runtime").mkdir()
        (released / "runtime" / "PAYLOAD.json").write_text(json.dumps({"version": "1.3.0"}))
        self.assertTrue(account.required({"QORQUT_FOLDER_ROOT": str(released)}))
        self.assertFalse(account.required({"QORQUT_FOLDER_ROOT": str(released), "QORQBOT_ACCOUNT": "off"}))

    def test_unreachable_service_leaves_sign_in_required(self):
        env = {"QORQBOT_ACCOUNT_API": "http://127.0.0.1:9", "QORQBOT_ACCOUNT": "on"}
        with self.assertRaises(DomainError) as raised:
            account.start(self.runtime, {"email": "sam@example.com"}, env)
        self.assertEqual(raised.exception.code, "provider_unavailable")
        self.service.replies["/v1/auth/email/start"] = (503, {"error": "down"})
        with self.assertRaises(DomainError) as raised:
            account.start(self.runtime, {"email": "sam@example.com"}, self.env)
        self.assertEqual(raised.exception.code, "provider_unavailable")
        view = account.owner_view(self.runtime, env)
        self.assertTrue(view["required"])
        self.assertNotIn("deferred", view)
        self.assertFalse(hasattr(account, "defer"))

    def test_a_refused_code_keeps_the_service_message(self):
        self.service.replies["/v1/auth/email/verify"] = (400, {"error": "invalid_code", "message": "That code is wrong."})
        with self.assertRaises(DomainError) as raised:
            account.verify(self.runtime, {"email": "a@b.co", "code": "ABCD1234", "use": "personal"}, self.env)
        self.assertEqual((raised.exception.code, raised.exception.message), ("validation_error", "That code is wrong."))
        self.assertFalse(account.owner_view(self.runtime, self.env)["signed_in"])

    def test_heartbeat_sends_only_installation_facts(self):
        self.sign_in()
        self.assertTrue(account.heartbeat(self.runtime, self.env))
        beat = self.service.requests[-1]
        self.assertEqual(beat["path"], "/v1/heartbeat")
        self.assertEqual(beat["auth"], "Bearer secret-token")
        self.assertEqual(set(beat["body"]), {"install_id", "app_version", "platform"})

    def test_revoked_token_signs_out_locally(self):
        self.sign_in()
        self.service.replies["/v1/heartbeat"] = (401, {"error": "invalid_token"})
        self.assertFalse(account.heartbeat(self.runtime, self.env))
        self.assertFalse(account.owner_view(self.runtime, self.env)["signed_in"])

    def test_logout_forgets_the_token_even_offline(self):
        self.sign_in()
        view = account.logout(self.runtime, {"QORQBOT_ACCOUNT_API": "http://127.0.0.1:9"})
        self.assertFalse(view["signed_in"])
        self.assertNotIn("token", json.loads(account.path(self.runtime).read_text()))

    def test_release_notice_only_for_a_newer_version(self):
        self.service.replies["/v1/releases/latest"] = (200, {"version": "1.3.0", "notes_url": "https://x/notes"})
        account._refresh_release(self.env)
        self.assertEqual(account.update_view({}), None)   # a source checkout has no version to compare
        self.assertLess(account._version_key("1.3.0-preview.3"), account._version_key("1.3.0"))
        self.assertLess(account._version_key("1.3.0"), account._version_key("v1.3.1"))
        released = Path(tempfile.mkdtemp())
        (released / "runtime").mkdir()
        (released / "runtime" / "PAYLOAD.json").write_text(json.dumps({"version": "1.3.0-preview.3"}))
        old = os.environ.get("QORQUT_FOLDER_ROOT")
        os.environ["QORQUT_FOLDER_ROOT"] = str(released)
        try:
            self.assertEqual(account.update_view()["version"], "1.3.0")
            (released / "runtime" / "PAYLOAD.json").write_text(json.dumps({"version": "1.3.0"}))
            self.assertIsNone(account.update_view())
        finally:
            if old is None:
                os.environ.pop("QORQUT_FOLDER_ROOT")
            else:
                os.environ["QORQUT_FOLDER_ROOT"] = old

    def test_unchecked_licence_is_not_known_and_keeps_the_free_date(self):
        view = self.sign_in()
        self.assertEqual(view["license"], {"commercial_free_until": "2027-01-01", "commercial": True, "known": False,
                                           "paid": None, "paid_until": None, "price": None, "status": None,
                                           "checked": None})

    def test_licence_is_fetched_with_the_token_and_kept_beside_the_session(self):
        self.sign_in()
        self.service.replies["/v1/me"] = (200, {"account": {"email": "sam@example.com"},
                                                "license": {"commercial_free_until": "2027-01-01", "paid": True,
                                                            "paid_until": "2027-03-15T00:00:00Z", "price": "yearly",
                                                            "status": "active"}})
        self.assertTrue(account.refresh_license(self.runtime, self.env))
        asked = self.service.requests[-1]
        self.assertEqual((asked["method"], asked["path"], asked["auth"], asked["body"]),
                         ("GET", "/v1/me", "Bearer secret-token", None))
        view = account.owner_view(self.runtime, self.env)
        self.assertEqual({key: view["license"][key] for key in ("commercial_free_until", "known", "paid", "paid_until",
                                                                "price", "status")},
                         {"commercial_free_until": "2027-01-01", "known": True, "paid": True,
                          "paid_until": "2027-03-15T00:00:00Z", "price": "yearly", "status": "active"})
        self.assertIsInstance(view["license"]["checked"], float)
        self.assertNotIn("secret-token", json.dumps(view))
        self.assertNotIn("secret-token", json.dumps(json.loads(account.path(self.runtime).read_text())["server_license"]))

    def test_unpaid_licence_is_known_and_drops_stale_paid_details(self):
        self.sign_in()
        self.service.replies["/v1/me"] = (200, {"license": {"commercial_free_until": "2027-01-01", "paid": False,
                                                            "paid_until": "2027-03-15", "price": "monthly"}})
        account.refresh_license(self.runtime, self.env)
        license = account.owner_view(self.runtime, self.env)["license"]
        self.assertEqual((license["known"], license["paid"], license["paid_until"], license["price"]),
                         (True, False, None, None))

    def test_missing_or_malformed_fields_are_not_known_and_never_invent_paid(self):
        self.sign_in()
        self.service.replies["/v1/me"] = (200, {"license": {"commercial_free_until": "2027-01-01"}})
        self.assertTrue(account.refresh_license(self.runtime, self.env))
        license = account.owner_view(self.runtime, self.env)["license"]
        self.assertEqual((license["known"], license["paid"]), (False, None))
        self.service.replies["/v1/me"] = (200, {"license": {"commercial_free_until": "soon", "paid": "yes",
                                                            "paid_until": "next year", "price": "weekly",
                                                            "status": "<b>x</b>"}})
        self.assertTrue(account.refresh_license(self.runtime, self.env))
        license = account.owner_view(self.runtime, self.env)["license"]
        self.assertEqual((license["commercial_free_until"], license["known"], license["paid_until"], license["price"],
                          license["status"]), ("2027-01-01", False, None, None, None))

    def test_a_reply_without_a_licence_object_changes_nothing(self):
        self.sign_in()
        self.service.replies["/v1/me"] = (200, {"license": {"paid": True, "price": "monthly"}})
        account.refresh_license(self.runtime, self.env)
        for reply in ((200, {"account": {"email": "sam@example.com"}}), (200, [1]), (204, None)):
            self.service.replies["/v1/me"] = reply
            self.assertFalse(account.refresh_license(self.runtime, self.env))
        license = account.owner_view(self.runtime, self.env)["license"]
        self.assertEqual((license["known"], license["paid"], license["price"]), (True, True, "monthly"))

    def test_offline_or_failing_service_keeps_the_last_licence_and_the_session(self):
        self.sign_in()
        self.service.replies["/v1/me"] = (200, {"license": {"paid": True, "price": "monthly"}})
        account.refresh_license(self.runtime, self.env)
        self.assertFalse(account.refresh_license(self.runtime, {"QORQBOT_ACCOUNT_API": "http://127.0.0.1:9"}))
        self.service.replies["/v1/me"] = (503, {"error": "down"})
        self.assertFalse(account.refresh_license(self.runtime, self.env))
        view = account.owner_view(self.runtime, self.env)
        self.assertTrue(view["signed_in"])
        self.assertEqual((view["license"]["known"], view["license"]["paid"]), (True, True))

    def test_revoked_session_signs_out_and_forgets_the_licence(self):
        self.sign_in()
        self.service.replies["/v1/me"] = (200, {"license": {"paid": True}})
        account.refresh_license(self.runtime, self.env)
        self.service.replies["/v1/me"] = (401, {"error": "invalid_token"})
        self.assertFalse(account.refresh_license(self.runtime, self.env))
        saved = json.loads(account.path(self.runtime).read_text())
        self.assertNotIn("token", saved)
        self.assertNotIn("server_license", saved)
        self.assertFalse(account.owner_view(self.runtime, self.env)["signed_in"])

    def test_no_request_is_made_when_signed_out(self):
        self.assertFalse(account.refresh_license(self.runtime, self.env))
        self.assertEqual(self.service.requests, [])

    def test_paid_licence_follows_the_email_not_the_use(self):
        self.sign_in()
        self.service.replies["/v1/me"] = (200, {"license": {"paid": True, "paid_until": "2027-03-15", "price": "yearly"}})
        account.refresh_license(self.runtime, self.env)
        self.service.replies["/v1/auth/email/verify"] = (200, {"token": "t2", "account": {
            "id": "a1", "email": "sam@example.com", "use": "personal", "company": None, "created": "2026-11-12T00:00:00Z"}})
        view = account.verify(self.runtime, {"email": "sam@example.com", "code": "abcd-1234", "use": "personal"}, self.env)
        self.assertFalse(view["license"]["commercial"])
        self.assertEqual((view["license"]["known"], view["license"]["paid"], view["license"]["price"]),
                         (True, True, "yearly"))

    def test_a_different_email_does_not_inherit_the_licence(self):
        self.sign_in()
        self.service.replies["/v1/me"] = (200, {"license": {"paid": True, "price": "yearly"}})
        account.refresh_license(self.runtime, self.env)
        self.service.replies["/v1/auth/email/verify"] = (200, {"token": "t2", "account": {
            "id": "a2", "email": "lee@example.com", "use": "personal", "company": None, "created": "2026-11-13T00:00:00Z"}})
        view = account.verify(self.runtime, {"email": "lee@example.com", "code": "abcd-1234", "use": "personal"}, self.env)
        self.assertEqual((view["license"]["known"], view["license"]["paid"]), (False, None))
        self.assertNotIn("server_license", json.loads(account.path(self.runtime).read_text()))

    def test_logout_forgets_the_licence(self):
        self.sign_in()
        self.service.replies["/v1/me"] = (200, {"license": {"paid": True}})
        account.refresh_license(self.runtime, self.env)
        account.logout(self.runtime, {"QORQBOT_ACCOUNT_API": "http://127.0.0.1:9"})
        self.assertNotIn("server_license", json.loads(account.path(self.runtime).read_text()))

    def test_a_stale_or_missing_licence_is_due_for_a_background_refresh_with_a_retry_pause(self):
        self.sign_in()
        started = []
        original = account._license_quietly
        account._license_quietly = lambda runtime, environ: started.append(runtime)
        self.addCleanup(setattr, account, "_license_quietly", original)
        account._license_started.clear()
        account.owner_view(self.runtime, self.env)
        account.owner_view(self.runtime, self.env)     # inside the retry pause
        time.sleep(0.2)
        self.assertEqual(len(started), 1)
        self.service.replies["/v1/me"] = (200, {"license": {"paid": False}})
        account.refresh_license(self.runtime, self.env)
        account._license_started.clear()
        account.owner_view(self.runtime, self.env)     # a fresh answer is not asked for again
        time.sleep(0.2)
        self.assertEqual(len(started), 1)

    def test_corrupt_file_is_reported_not_reset(self):
        account.path(self.runtime).write_text("{")
        with self.assertRaises(DomainError):
            account.owner_view(self.runtime, self.env)
        self.assertEqual(account.path(self.runtime).read_text(), "{")


if __name__ == "__main__":
    unittest.main()
