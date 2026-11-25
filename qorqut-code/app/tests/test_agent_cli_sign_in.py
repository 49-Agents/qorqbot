"""Sign-in and status of the agent CLIs, with a fake subprocess, Keychain and browser opener. Nothing here starts
a real ``claude``/``codex`` or touches the network."""
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut import account_state, agent_cli_sign_in as sign_in

REAL_DEFAULT_METHOD = sign_in.default_method        # the tests below replace ``default_method`` (Linux would pick the device code)


def done(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class Recorder:
    """A fake ``subprocess.run`` that answers by command and counts calls."""

    def __init__(self, answers=None):
        self.answers, self.calls = answers or {}, []

    def __call__(self, command, **options):
        self.calls.append(list(command))
        answer = self.answers.get(tuple(command[1:3]) if command[0] != "/usr/bin/security" else "security", done(1))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def ran(self, *words):
        return any(list(words) == call[1:1 + len(words)] for call in self.calls)


class Installed:
    """Stands in for ``qorqut.agent_cli_install`` with the agreed contract."""

    def __init__(self, found=None, env=None):
        self.found, self.env = found or {}, env or {}

    def resolve(self, provider, environ=None):
        return self.found.get(provider)

    def status(self, provider):
        return {"phase": "ready" if provider in self.found else "not_downloaded", "progress": None, "message": None, "version": None}

    def launch_environment(self, provider):
        return dict(self.env)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.env = {"HOME": str(self.home), "PATH": "/nonexistent", "USER": "owner"}
        sign_in._pending.clear(); sign_in._failed.clear(); sign_in._cli_status_cache.clear()
        self.install = Installed({"claude": {"path": "/fake/claude", "source": "managed", "version": "2.1.327"},
                                  "codex": {"path": "/fake/codex", "source": "managed", "version": "0.170.0"}})
        patch = mock.patch.object(sign_in, "_install", return_value=self.install)
        patch.start(); self.addCleanup(patch.stop)
        probe = mock.patch("qorqut.connectors._probe_environment", side_effect=lambda environ=None: dict(environ or {}))
        probe.start(); self.addCleanup(probe.stop)

    def credential(self, **oauth):
        (self.home / ".claude").mkdir(exist_ok=True)
        body = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "subscriptionType": "max", **oauth}}
        (self.home / ".claude/.credentials.json").write_text(json.dumps(body))

    def account(self, email="o@x.y"):
        (self.home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))


class ClaudeStatusTest(Base):
    def test_stored_credential_is_read_without_running_the_cli(self):
        self.credential(); self.account()
        run = Recorder()
        status = sign_in.claude_status(self.env, run=run, system="Linux")
        self.assertEqual(status, {"signed_in": True, "account": {"signed_in": True, "method": "claude.ai", "email": "o@x.y", "plan": "max"}})
        self.assertEqual(run.calls, [])

    def test_expired_refresh_token_is_not_signed_in_and_asks_the_cli(self):
        self.credential(refreshTokenExpiresAt=1000)
        run = Recorder({("auth", "status"): done(0, json.dumps({"loggedIn": False}))})
        self.assertFalse(sign_in.claude_status(self.env, run=run, system="Linux", now=5000)["signed_in"])
        self.assertTrue(run.ran("/fake/claude", "auth", "status", "--json") or run.calls[0][1:] == ["auth", "status", "--json"])

    def test_future_expiry_is_signed_in(self):
        self.credential(refreshTokenExpiresAt=9_000_000_000_000)
        self.assertTrue(sign_in.claude_status(self.env, run=Recorder(), system="Linux", now=1)["signed_in"])

    def test_api_key_and_token_variables_are_answered_without_the_cli_or_the_store(self):
        self.credential(); self.account()                     # a refresh token the CLI could spend must stay untouched
        for name, method in (("ANTHROPIC_API_KEY", "api_key"), ("ANTHROPIC_AUTH_TOKEN", "api_key"),
                             ("CLAUDE_CODE_OAUTH_TOKEN", "token"), ("ANTHROPIC_IDENTITY_TOKEN_FILE", "token")):
            run = Recorder()
            status = sign_in.claude_status({**self.env, name: "x"}, run=run, system="Linux")
            self.assertEqual(status, {"signed_in": True, "account": {"signed_in": True, "method": method, "email": None, "plan": None}}, name)
            self.assertEqual(run.calls, [], name)

    def test_another_backend_is_answered_without_the_cli(self):
        self.credential()
        run = Recorder()
        status = sign_in.claude_status({**self.env, "CLAUDE_CODE_USE_BEDROCK": "1"}, run=run, system="Linux")
        self.assertEqual((status["signed_in"], status["account"]["method"], run.calls), (True, "third_party", []))

    def test_a_moved_store_is_not_read_as_the_default_one_and_the_cli_is_not_run(self):
        self.credential()
        run = Recorder()
        status = sign_in.claude_status({**self.env, "CLAUDE_CODE_HOST_CREDS_FILE": "/x"}, run=run, system="Linux")
        self.assertEqual((status, run.calls), ({"signed_in": False, "account": None}, []))

    def test_an_empty_override_variable_is_not_an_override(self):
        self.credential()
        run = Recorder()
        self.assertEqual(sign_in.claude_status({**self.env, "ANTHROPIC_API_KEY": ""}, run=run, system="Linux")["account"]["method"], "claude.ai")

    def test_a_timeout_or_a_failure_to_start_is_remembered_not_retried_on_every_read(self):
        for error in (subprocess.TimeoutExpired("claude", 15), OSError("no exec"), None):
            sign_in._cli_status_cache.clear()
            run = Recorder({("auth", "status"): error if error is not None else done(1, "not json")})
            for _ in range(10):
                self.assertEqual(sign_in.claude_status(self.env, run=run, system="Linux"), {"signed_in": False, "account": None})
            self.assertEqual(len(run.calls), 1, error)
            self.assertGreaterEqual(sign_in.CLI_STATUS_FAILURE_TTL, 60)

    def test_a_remembered_failure_is_tried_again_after_its_window(self):
        run = Recorder({("auth", "status"): subprocess.TimeoutExpired("claude", 15)})
        sign_in.claude_status(self.env, run=run, system="Linux")
        (key, (expires, result)), = sign_in._cli_status_cache.items()
        self.assertGreaterEqual(expires - time.monotonic(), 60)
        sign_in._cli_status_cache[key] = (time.monotonic() - 1, result)
        sign_in.claude_status(self.env, run=run, system="Linux")
        self.assertEqual(len(run.calls), 2)

    def test_concurrent_readers_run_one_status_call_and_the_others_get_the_last_value_or_signed_out(self):
        started, release, calls = threading.Event(), threading.Event(), []

        def slow(command, **options):
            calls.append(command)
            started.set()
            release.wait(5)
            return done(0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
        results = []
        first = threading.Thread(target=lambda: results.append(sign_in.claude_status(self.env, run=slow, system="Linux")))
        first.start()
        self.assertTrue(started.wait(3))
        others = [sign_in.claude_status(self.env, run=slow, system="Linux") for _ in range(5)]
        self.assertEqual(others, [{"signed_in": False, "account": None}] * 5)      # nothing known yet: not signed in, no second run
        self.assertEqual(len(calls), 1)
        release.set(); first.join(3)
        self.assertTrue(results[0]["signed_in"])
        self.assertTrue(sign_in.claude_status(self.env, run=slow, system="Linux")["signed_in"])      # now remembered
        self.assertEqual(len(calls), 1)

    def test_a_reader_during_a_run_gets_the_last_known_answer_even_when_it_has_expired(self):
        key = ("/fake/claude", self.env["HOME"], None)
        known = {"signed_in": True, "account": {"signed_in": True, "method": "claude.ai", "email": None, "plan": None}}
        sign_in._cli_status_cache[key] = (time.monotonic() - 5, known)
        self.assertTrue(sign_in._cli_status_flight.acquire(blocking=False))
        self.addCleanup(lambda: sign_in._cli_status_flight.locked() and sign_in._cli_status_flight.release())
        run = Recorder()
        self.assertEqual(sign_in.claude_status(self.env, run=run, system="Linux"), known)
        self.assertEqual(run.calls, [])

    def test_unreadable_store_asks_the_cli_and_the_answer_is_not_polled(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude/.credentials.json").write_text("not json")
        run = Recorder({("auth", "status"): done(0, json.dumps({"loggedIn": True}))})
        for _ in range(5):
            self.assertTrue(sign_in.claude_status(self.env, run=run, system="Linux")["signed_in"])
        self.assertEqual(len(run.calls), 1)

    def test_a_signed_out_store_keeps_cli_calls_to_one_per_window(self):
        run = Recorder({("auth", "status"): done(0, json.dumps({"loggedIn": False}))})
        for _ in range(3):
            sign_in.claude_status(self.env, run=run, system="Linux")
        self.assertEqual(len(run.calls), 1)
        sign_in._cli_status_cache.clear()
        sign_in.claude_status(self.env, run=run, system="Linux")
        self.assertEqual(len(run.calls), 2)

    def test_the_cli_is_never_run_while_a_sign_in_is_pending(self):
        run = Recorder()
        with mock.patch.object(sign_in, "sign_in_state", return_value={"state": "connecting", "reason": None}):
            status = sign_in.claude_status(self.env, run=run, system="Linux")
        self.assertEqual((status, run.calls), ({"signed_in": False, "account": None}, []))

    def test_no_cli_and_no_store_is_signed_out(self):
        self.install.found.pop("claude")
        run = Recorder()
        self.assertEqual(sign_in.claude_status(self.env, run=run, system="Linux"), {"signed_in": False, "account": None})
        self.assertEqual(run.calls, [])

    def test_keychain_item_is_read_with_security_and_the_account_name(self):
        text = json.dumps({"claudeAiOauth": {"refreshToken": "r", "subscriptionType": "pro"}})
        run = Recorder({"security": done(0, text.encode().hex() + "\n")})      # a value that is not plain text comes back as hex
        status = sign_in.claude_status(self.env, run=run, system="Darwin")
        self.assertEqual((status["signed_in"], status["account"]["plan"]), (True, "pro"))
        self.assertEqual(run.calls, [["/usr/bin/security", "find-generic-password", "-a", "owner", "-w", "-s", "Claude Code-credentials"]])

    def test_custom_config_dir_uses_the_hashed_item_name(self):
        config = str(self.home / "custom")
        run = Recorder({"security": done(0, json.dumps({"claudeAiOauth": {"refreshToken": "r"}}))})
        sign_in.claude_status({**self.env, "CLAUDE_CONFIG_DIR": config}, run=run, system="Darwin")
        self.assertEqual(run.calls[0][-1], "Claude Code-credentials-" + hashlib.sha256(config.encode()).hexdigest()[:8])

    def test_missing_keychain_item_falls_back_to_the_file(self):
        self.credential()
        run = Recorder({"security": done(44)})
        self.assertTrue(sign_in.claude_status(self.env, run=run, system="Darwin")["signed_in"])
        self.assertEqual(len(run.calls), 1)

    def test_locked_keychain_is_not_absence_and_asks_the_cli_not_the_file(self):
        self.credential()                                  # a stale file must not hide an unreadable Keychain
        run = Recorder({"security": done(36), ("auth", "status"): done(0, json.dumps({"loggedIn": False}))})
        self.assertFalse(sign_in.claude_status(self.env, run=run, system="Darwin")["signed_in"])
        self.assertTrue(any(call[1:3] == ["auth", "status"] for call in run.calls))


class CodexStatusTest(Base):
    def test_logged_in_and_not_logged_in(self):
        run = Recorder({("login", "status"): done(0, "Logged in using ChatGPT\n")})
        status = sign_in.codex_status(self.env, run=run)
        self.assertEqual(status, {"signed_in": True, "account": {"signed_in": True, "method": "chatgpt", "email": None, "plan": None}})
        run = Recorder({("login", "status"): done(0, "Not logged in\n")})
        self.assertFalse(sign_in.codex_status(self.env, run=run)["signed_in"])
        run = Recorder({("login", "status"): done(1, "Not logged in\n")})
        self.assertEqual(sign_in.codex_status(self.env, run=run)["account"], None)

    def test_api_key_login_is_named_and_none_of_the_cli_text_is_passed_on(self):
        run = Recorder({("login", "status"): done(0, "Logged in using an API key - sk-proj-abc***xyz\n")})
        status = sign_in.codex_status(self.env, run=run)
        self.assertEqual(status["account"]["method"], "api_key")
        self.assertNotIn("detail", status)
        self.assertNotIn("sk-proj", json.dumps(status))


class FakeProcess:
    """A process that lives until it is told to stop or given an exit code."""
    _next = 40000

    def __init__(self):
        FakeProcess._next += 1
        self.pid, self.code, self.stopped = FakeProcess._next, None, threading.Event()
        self.signals, self.groups = [], []

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if not self.stopped.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.code

    def exit(self, code):
        self.code = code
        self.stopped.set()

    def terminate(self):
        self.signals.append("terminate"); self.exit(-15)

    def kill(self):
        self.signals.append("kill"); self.exit(-9)


class Pipe:
    """The stdout of a fake app-server."""
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self):
        return self.lines.get()

    def put(self, value):
        self.lines.put(json.dumps(value) + "\n")

    def close(self):
        self.lines.put("")


class FakeAppServer(FakeProcess):
    """Answers the Codex app-server calls a sign-in makes. ``script`` maps a method to its result."""

    def __init__(self, script):
        super().__init__()
        self.script, self.received = script, []
        self.stdout, self.stdin = Pipe(), SimpleNamespace(write=self.write, flush=lambda: None)

    def write(self, data):
        value = json.loads(data)
        self.received.append(value)
        if "method" in value and "id" in value:
            if self.script.get("_silent") or (self.script.get("_silent_after_initialize") and value["method"] != "initialize"):
                return
            if value["method"] == "initialize" and self.script.get("_late_initialize"):
                threading.Timer(self.script["_late_initialize"], self.stdout.put, ({"id": value["id"], "result": {}},)).start()
                return
            result = self.script.get(value["method"], {})
            self.stdout.put({"id": value["id"], "result": result})
            if value["method"] == "account/login/start" and self.script.get("_complete"):
                self.stdout.put({"method": "account/login/completed", "params": self.script["_complete"]})

    def exit(self, code):
        super().exit(code)
        self.stdout.close()

    def methods(self):
        return [value.get("method") for value in self.received]


def wait_for(test, condition, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.01)
    test.fail("condition not reached in time")


class SignInBase(Base):
    def setUp(self):
        super().setUp()
        self.processes = []
        patch = mock.patch.object(sign_in, "_popen", side_effect=self.popen)
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(sign_in, "_signal_tree", side_effect=self.signal_tree)
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(sign_in, "default_method", return_value="browser")      # Linux would pick the device code
        patch.start(); self.addCleanup(patch.stop)
        sign_in.set_completion_handler(None); account_state.reset_for_tests()
        self.addCleanup(sign_in.set_completion_handler, None)
        self.commands, self.factory = [], FakeProcess
        self.addCleanup(sign_in.stop_all)        # runs while the fakes are still in place

    def signal_tree(self, process, number, group=True):
        process.groups.append(group)
        process.terminate() if number == signal.SIGTERM else process.kill()

    def popen(self, command, **options):
        self.commands.append((command, options))
        process = self.factory()
        self.processes.append(process)
        return process


class ClaudeSignInTest(SignInBase):
    def test_starts_the_cli_without_a_terminal_and_is_idle_after_a_clean_exit(self):
        state = sign_in.start_sign_in("claude", environ=self.env)
        self.assertEqual(state, {"state": "connecting", "reason": None})
        command, options = self.commands[0]
        self.assertEqual(command, ["/fake/claude", "auth", "login", "--claudeai"])
        self.assertEqual((options["stdin"], options["stderr"]), (subprocess.PIPE, subprocess.STDOUT))   # stdin carries the pasted code
        self.assertTrue(hasattr(options["stdout"], "fileno"))        # an unlinked temporary file, not a pipe
        self.assertTrue(options["start_new_session"])
        sign_in._cli_status_cache["old"] = (0, {})
        self.processes[0].exit(0)
        wait_for(self, lambda: sign_in.sign_in_state("claude")["state"] == "idle")
        self.assertEqual(sign_in._cli_status_cache, {})        # the next status read sees the new sign-in

    def test_nonzero_exit_fails_with_a_plain_reason_and_cancel_forgets_it(self):
        sign_in.start_sign_in("claude", environ=self.env)
        self.processes[0].exit(1)
        wait_for(self, lambda: sign_in.sign_in_state("claude")["state"] == "failed")
        self.assertIn("did not finish", sign_in.sign_in_state("claude")["reason"])
        self.assertEqual(sign_in.cancel_sign_in("claude")["state"], "idle")

    def test_managed_environment_is_added_to_the_launch(self):
        self.install.env = {"DISABLE_AUTOUPDATER": "1"}
        sign_in.start_sign_in("claude", environ=self.env)
        self.assertEqual(self.commands[0][1]["env"]["DISABLE_AUTOUPDATER"], "1")

    def test_cancel_stops_the_process_and_is_not_reported_as_a_failure(self):
        sign_in.start_sign_in("claude", environ=self.env)
        self.assertEqual(sign_in.cancel_sign_in("claude")["state"], "idle")
        self.assertEqual(self.processes[0].signals, ["terminate"])
        time.sleep(0.05)
        self.assertEqual(sign_in.sign_in_state("claude")["state"], "idle")

    def test_a_second_start_replaces_the_first(self):
        sign_in.start_sign_in("claude", environ=self.env)
        sign_in.start_sign_in("claude", environ=self.env)
        self.assertEqual(self.processes[0].signals, ["terminate"])
        self.assertIsNone(self.processes[1].poll())
        time.sleep(0.05)
        self.assertEqual(sign_in.sign_in_state("claude")["state"], "connecting")      # the replaced one did not end the new one

    def test_timeout_stops_the_process_and_fails(self):
        with mock.patch.object(sign_in, "SIGN_IN_TIMEOUT", 0.05):
            sign_in.start_sign_in("claude", environ=self.env)
            wait_for(self, lambda: sign_in.sign_in_state("claude")["state"] == "failed")
        self.assertIn("10 minutes", sign_in.sign_in_state("claude")["reason"])
        self.assertEqual(self.processes[0].signals, ["terminate"])

    def test_providers_are_independent_and_shutdown_stops_all(self):
        sign_in.start_sign_in("claude", environ=self.env)
        self.factory = lambda: FakeAppServer({"account/login/start": {"authUrl": "https://x", "loginId": "l"}})
        sign_in.start_sign_in("codex", environ=self.env, opener=lambda url: True)
        self.assertEqual(sign_in.sign_in_state("claude")["state"], "connecting")
        sign_in.stop_all()
        self.assertTrue(all(process.poll() is not None for process in self.processes))
        self.assertEqual([sign_in.sign_in_state(p)["state"] for p in sign_in.PROVIDERS], ["idle", "idle"])

    def test_not_downloaded_fails_without_starting_anything(self):
        self.install.found.pop("claude")
        state = sign_in.start_sign_in("claude", environ=self.env)
        self.assertEqual(state["state"], "failed")
        self.assertIn("Download", state["reason"])
        self.assertEqual(self.commands, [])

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(ValueError):
            sign_in.start_sign_in("gemini")

    def test_cannot_start_fails_plainly(self):
        with mock.patch.object(sign_in, "_popen", side_effect=OSError("no exec")):
            state = sign_in.start_sign_in("claude", environ=self.env)
        self.assertEqual(state["state"], "failed")


class ClaudeOutputTest(SignInBase):
    """What the CLI prints while it waits: a page it could not open is opened here; nothing else is."""

    def start(self):
        self.opened = []
        sign_in.start_sign_in("claude", environ=self.env, opener=lambda url: self.opened.append(url) or True)
        self.output = self.commands[0][1]["stdout"]

    def say(self, text):
        os.write(self.output.fileno(), text.encode())

    def settle(self):
        time.sleep(sign_in.OUTPUT_POLL * 3)

    def test_a_page_the_cli_could_not_open_is_opened_once(self):
        self.start()
        self.say("Could not open the browser.\nVisit https://claude.com/cai/oauth/authorize?code=true&state=abc to sign in.\n")
        wait_for(self, lambda: self.opened)
        self.settle()
        self.assertEqual(self.opened, ["https://claude.com/cai/oauth/authorize?code=true&state=abc"])

    def test_lines_that_arrive_in_pieces_and_in_either_order_are_put_together(self):
        self.start()
        self.say("If you see https://claude.ai/oauth/auth")
        self.settle()
        self.say("orize?x=1.\nUnable to launch a browser\n")
        wait_for(self, lambda: self.opened)
        self.assertEqual(self.opened, ["https://claude.ai/oauth/authorize?x=1"])

    def test_the_usual_hint_means_the_cli_opened_the_browser_and_nothing_is_opened_here(self):
        self.start()
        self.say("Opening browser to sign in...\nIf the browser didn't open, visit: https://claude.ai/oauth/authorize?x=1\n")
        self.settle()
        self.assertEqual(self.opened, [])

    def test_only_https_pages_of_the_claude_domains_are_ever_opened(self):
        self.start()
        self.say("Failed to open the browser\nhttp://claude.ai/x https://evil.example/claude.ai https://claude.ai.evil.example/x "
                 "https://user@claude.ai/x https://claude.ai:8443/x file:///etc/passwd\n")
        self.settle()
        self.assertEqual(self.opened, [])

    def test_a_broken_opener_is_not_fatal_and_the_sign_in_goes_on(self):
        sign_in.start_sign_in("claude", environ=self.env, opener=lambda url: 1 / 0)
        os.write(self.commands[0][1]["stdout"].fileno(), b"Could not open the browser\nhttps://claude.ai/x\n")
        self.settle()
        self.assertEqual(sign_in.sign_in_state("claude")["state"], "connecting")

    def test_output_is_read_to_a_limit_and_the_file_is_closed_when_the_sign_in_ends(self):
        self.start()
        self.say("x" * (sign_in.OUTPUT_LIMIT + 5000) + "\nCould not open the browser\nhttps://claude.ai/x\n")
        self.settle()
        self.assertEqual(self.opened, [])                      # past the limit nothing is looked at
        self.processes[0].exit(0)
        wait_for(self, lambda: self.output.closed)

    def test_cancel_closes_the_output_and_opens_nothing_more(self):
        self.start()
        sign_in.cancel_sign_in("claude")
        wait_for(self, lambda: self.output.closed)

    def test_only_the_cli_is_signalled_for_claude_so_the_owners_browser_survives(self):
        self.start()
        sign_in.cancel_sign_in("claude")
        self.assertEqual(self.processes[0].groups, [False])

    def test_the_whole_group_is_signalled_for_codex(self):
        self.factory = lambda: FakeAppServer({"account/login/start": {"authUrl": "https://x", "loginId": "l"}})
        sign_in.start_sign_in("codex", environ=self.env, opener=lambda url: True)
        wait_for(self, lambda: "account/login/start" in self.processes[0].methods())
        sign_in.cancel_sign_in("codex")
        self.assertEqual(self.processes[0].groups, [True])


class SignalTreeTest(unittest.TestCase):
    def test_a_group_is_killed_only_when_asked_and_only_when_the_process_leads_it(self):
        process = SimpleNamespace(pid=123, terminate=mock.Mock(), kill=mock.Mock())
        with mock.patch("os.getpgid", return_value=123), mock.patch("os.killpg") as killpg:
            sign_in._signal_tree(process, signal.SIGTERM, group=False)
            killpg.assert_not_called()
            process.terminate.assert_called_once_with()
            sign_in._signal_tree(process, signal.SIGTERM, group=True)
            killpg.assert_called_once_with(123, signal.SIGTERM)
        with mock.patch("os.getpgid", return_value=1), mock.patch("os.killpg") as killpg:
            sign_in._signal_tree(process, signal.SIGKILL, group=True)      # not ours: never the group
            killpg.assert_not_called()
            process.kill.assert_called_once_with()


class StopAllTest(SignInBase):
    class Stubborn(FakeProcess):
        def terminate(self):
            self.signals.append("terminate")              # the polite stop is ignored: the process stays alive

    class StubbornServer(FakeAppServer):
        def terminate(self):
            self.signals.append("terminate")

    def test_a_process_that_ignores_the_polite_stop_is_killed_within_the_budget(self):
        self.factory = self.Stubborn
        sign_in.start_sign_in("claude", environ=self.env)
        started = time.monotonic()
        sign_in.stop_all(budget=0.2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.processes[0].signals, ["terminate", "kill"])
        self.assertEqual([sign_in.sign_in_state(p)["state"] for p in sign_in.PROVIDERS], ["idle", "idle"])

    def test_the_budget_is_shared_not_spent_per_process(self):
        self.factory = self.Stubborn
        sign_in.start_sign_in("claude", environ=self.env)
        self.factory = lambda: self.StubbornServer({"account/login/start": {"authUrl": "https://x", "loginId": "l"}})
        sign_in.start_sign_in("codex", environ=self.env, opener=lambda url: True)
        started = time.monotonic()
        with mock.patch.object(sign_in, "STOP_GRACE", 5):         # sequential per-process grace would take 10 s
            sign_in.stop_all(budget=0.3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(all(process.signals[-1] == "kill" for process in self.processes))


class CodexSignInTest(SignInBase):
    def start(self, script, opener=None):
        self.factory = lambda: FakeAppServer(script)
        self.opened = []
        return sign_in.start_sign_in("codex", environ=self.env, opener=opener or (lambda url: self.opened.append(url) or True))

    def test_full_sign_in_opens_the_page_waits_and_verifies_the_account(self):
        self.start({"account/login/start": {"type": "chatgpt", "loginId": "L1", "authUrl": "https://auth.example/login"},
                    "account/read": {"account": {"type": "chatgpt", "email": "o@x.y"}},
                    "_complete": {"loginId": "L1", "success": True, "error": None}})
        wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "idle")
        server = self.processes[0]
        self.assertEqual(self.commands[0][0], ["/fake/codex", "app-server", "--listen", "stdio://"])
        self.assertEqual(self.opened, ["https://auth.example/login"])
        self.assertEqual(server.methods(), ["initialize", "initialized", "account/login/start", "account/read"])
        self.assertEqual(server.received[2]["params"], {"type": "chatgpt"})
        self.assertNotIn("jsonrpc", server.received[0])
        wait_for(self, lambda: server.poll() is not None)            # the app-server is stopped afterwards
        self.assertNotIn("account/login/cancel", server.methods())

    def test_managed_launch_arguments_follow_the_app_server_command(self):
        self.install.launch_arguments = lambda provider: ["-c", "check_for_update_on_startup=false"]
        self.start({"account/login/start": {"loginId": "L1", "authUrl": "https://a/b"}})
        self.assertEqual(self.commands[0][0][-2:], ["-c", "check_for_update_on_startup=false"])

    def test_a_completion_for_another_login_is_ignored(self):
        script = {"account/login/start": {"loginId": "L1", "authUrl": "https://a/b"}, "account/read": {"account": {"type": "chatgpt"}}}
        self.start(script)
        server = self.processes[0]
        wait_for(self, lambda: "account/login/start" in server.methods())
        server.stdout.put({"method": "account/login/completed", "params": {"loginId": "other", "success": False, "error": "no"}})
        time.sleep(0.05)
        self.assertEqual(sign_in.sign_in_state("codex")["state"], "connecting")
        server.stdout.put({"method": "account/login/completed", "params": {"loginId": "L1", "success": True}})
        wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "idle")

    def test_a_failed_login_reports_the_servers_reason(self):
        self.start({"account/login/start": {"loginId": "L1", "authUrl": "https://a/b"},
                    "_complete": {"loginId": "L1", "success": False, "error": "Access denied"}})
        wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "failed")
        self.assertEqual(sign_in.sign_in_state("codex")["reason"], "Access denied")

    def test_an_account_of_another_type_is_not_a_sign_in(self):
        self.start({"account/login/start": {"loginId": "L1", "authUrl": "https://a/b"},
                    "account/read": {"account": None}, "_complete": {"loginId": "L1", "success": True}})
        wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "failed")

    def test_cancel_sends_login_cancel_and_stops_the_server(self):
        self.start({"account/login/start": {"loginId": "L1", "authUrl": "https://a/b"}})
        server = self.processes[0]
        wait_for(self, lambda: self.opened)
        self.assertEqual(sign_in.cancel_sign_in("codex")["state"], "idle")
        self.assertIn("account/login/cancel", server.methods())
        self.assertEqual(server.received[-1]["params"], {"loginId": "L1"})
        self.assertIsNotNone(server.poll())
        time.sleep(0.05)
        self.assertEqual(sign_in.sign_in_state("codex")["state"], "idle")

    def test_a_browser_that_cannot_open_fails_the_sign_in(self):
        self.start({"account/login/start": {"loginId": "L1", "authUrl": "https://a/b"}}, opener=lambda url: False)
        wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "failed")
        self.assertIn("browser", sign_in.sign_in_state("codex")["reason"])

    def test_only_https_pages_are_opened(self):
        self.start({"account/login/start": {"loginId": "L1", "authUrl": "file:///etc/passwd"}})
        wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "failed")
        self.assertEqual(self.opened, [])

    def test_the_server_dying_fails_the_sign_in(self):
        self.start({"account/login/start": {"loginId": "L1", "authUrl": "https://a/b"}})
        server = self.processes[0]
        wait_for(self, lambda: self.opened)
        server.exit(1)
        wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "failed")

    def test_timeout_fails_and_stops_the_server(self):
        with mock.patch.object(sign_in, "SIGN_IN_TIMEOUT", 0.2):
            self.start({"account/login/start": {"loginId": "L1", "authUrl": "https://a/b"}})
            wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "failed")
        self.assertIn("10 minutes", sign_in.sign_in_state("codex")["reason"])
        wait_for(self, lambda: self.processes[0].poll() is not None)

    def test_a_server_that_never_answers_initialize_is_reported_as_not_started(self):
        with mock.patch.object(sign_in, "CODEX_START_TIMEOUT", 0.1), mock.patch.object(sign_in, "CODEX_REQUEST_TIMEOUT", 30):
            self.factory = lambda: FakeAppServer({"_silent": True})
            sign_in.start_sign_in("codex", environ=self.env, opener=lambda url: True)
            wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "failed")
        self.assertEqual(sign_in.sign_in_state("codex")["reason"], "Codex did not start. Try again.")

    def test_initialize_gets_more_time_than_the_other_requests(self):
        with mock.patch.object(sign_in, "CODEX_REQUEST_TIMEOUT", 0.05):
            server = FakeAppServer({"_late_initialize": 0.4, "account/login/start": {"loginId": "L1", "authUrl": "https://a/b"}})
            self.factory, self.opened = (lambda: server), []
            sign_in.start_sign_in("codex", environ=self.env, opener=lambda url: self.opened.append(url) or True)
            wait_for(self, lambda: self.opened)
        self.assertEqual(sign_in.sign_in_state("codex")["state"], "connecting")

    def test_a_slow_answer_after_the_start_is_not_called_a_ten_minute_wait(self):
        with mock.patch.object(sign_in, "CODEX_REQUEST_TIMEOUT", 0.1):
            self.factory = lambda: FakeAppServer({"_silent_after_initialize": True})
            sign_in.start_sign_in("codex", environ=self.env, opener=lambda url: True)
            wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "failed")
        reason = sign_in.sign_in_state("codex")["reason"]
        self.assertEqual(reason, "Codex did not answer in time. Try again.")
        self.assertNotIn("10 minutes", reason)

    def test_server_requests_are_refused_not_left_hanging(self):
        self.start({"account/login/start": {"loginId": "L1", "authUrl": "https://a/b"}})
        server = self.processes[0]
        wait_for(self, lambda: self.opened)
        server.stdout.put({"id": 77, "method": "item/commandExecution/requestApproval", "params": {}})
        wait_for(self, lambda: any(value.get("id") == 77 and "error" in value for value in server.received))


class FakeInput:
    """The standard input of a fake Claude CLI: collects what is written, can fail like a closed pipe."""

    def __init__(self, broken=False):
        self.data, self.closed, self.broken = b"", False, broken

    def write(self, data):
        if self.broken:
            raise BrokenPipeError
        self.data += data

    def flush(self):
        pass

    def close(self):
        self.closed = True


class SwitchSignInTest(SignInBase):
    """Signing in again on a signed-in product (a switch), the page and code a machine without a browser needs, and what a
    completed sign-in tells the rest of Qorqbot."""

    def claude(self, **options):
        stdin = FakeInput(**options)
        self.factory = lambda: self.with_input(stdin)
        return stdin

    def with_input(self, stdin):
        process = FakeProcess()
        process.stdin = stdin
        return process

    def say(self, text):
        os.write(self.commands[0][1]["stdout"].fileno(), text.encode())

    def test_a_signed_in_product_can_sign_in_again_and_the_stored_sign_in_is_left_alone(self):
        self.credential(); self.account("old@x.y")
        before = (self.home / ".claude/.credentials.json").read_text()
        self.claude()
        sign_in.start_sign_in("claude", environ=self.env)
        details = sign_in.sign_in_details("claude")
        self.assertEqual((details["switching"], details["previous_account"]), (True, {"email": "old@x.y", "plan": "max"}))
        self.assertEqual(sign_in.claude_status(self.env, cli_path="/fake/claude", run=Recorder())["account"]["email"], "old@x.y")
        sign_in.cancel_sign_in("claude")                          # cancelling changes nothing
        self.assertEqual((self.home / ".claude/.credentials.json").read_text(), before)
        self.assertEqual(sign_in.sign_in_state("claude")["state"], "idle")
        self.assertEqual(sign_in.sign_in_details("claude")["previous_account"], None)

    def test_a_first_sign_in_is_not_a_switch(self):
        self.claude()
        sign_in.start_sign_in("claude", environ=self.env)
        details = sign_in.sign_in_details("claude")
        self.assertEqual((details["switching"], details["previous_account"], details["method"]), (False, None, "browser"))

    def test_the_page_the_cli_prints_is_in_the_details_and_only_when_it_is_a_claude_page(self):
        self.claude()
        sign_in.start_sign_in("claude", environ=self.env, opener=lambda url: True)
        self.say("Opening browser to sign in...\nIf the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=abc\n"
                 "Paste code here if prompted > ")
        wait_for(self, lambda: sign_in.sign_in_details("claude")["page_url"])
        details = sign_in.sign_in_details("claude")
        self.assertEqual(details["page_url"], "https://claude.com/cai/oauth/authorize?code=true&state=abc")
        self.assertTrue(details["accepts_code"])
        self.assertTrue(details["expires_at"] > details["started_at"])

    def test_a_page_of_another_site_is_never_exposed(self):
        self.claude()
        sign_in.start_sign_in("claude", environ=self.env)
        self.say("visit https://evil.example/claude.ai or http://claude.ai/x\n")
        time.sleep(sign_in.OUTPUT_POLL * 3)
        self.assertIsNone(sign_in.sign_in_details("claude")["page_url"])

    def test_the_code_is_written_to_the_cli_with_one_newline(self):
        stdin = self.claude()
        sign_in.start_sign_in("claude", environ=self.env)
        sign_in.submit_code("claude", "  abc123#state-xyz \n")
        self.assertEqual(stdin.data, b"abc123#state-xyz\n")
        self.assertTrue(sign_in.sign_in_details("claude")["code_submitted"])

    def test_bad_codes_are_refused_and_nothing_is_written(self):
        stdin = self.claude()
        sign_in.start_sign_in("claude", environ=self.env)
        for bad in ("", "   ", "two words", "line\nbreak", "tab\there", "x" * (sign_in.CODE_LIMIT + 1), "bell\x07", None, 12):
            with self.assertRaises(sign_in.SignInError, msg=repr(bad)[:20]):
                sign_in.submit_code("claude", bad)
        self.assertEqual(stdin.data, b"")

    def test_a_code_needs_a_waiting_sign_in_that_takes_one(self):
        with self.assertRaises(sign_in.SignInError):
            sign_in.submit_code("claude", "abc")
        self.factory = lambda: FakeAppServer({"account/login/start": {"loginId": "L", "authUrl": "https://a/b"}})
        sign_in.start_sign_in("codex", environ=self.env, opener=lambda url: True)
        with self.assertRaises(sign_in.SignInError):
            sign_in.submit_code("codex", "abc")

    def test_a_closed_pipe_is_a_plain_error(self):
        self.claude(broken=True)
        sign_in.start_sign_in("claude", environ=self.env)
        with self.assertRaises(sign_in.SignInError) as caught:
            sign_in.submit_code("claude", "abc")
        self.assertIn("Start the sign-in again", caught.exception.message)

    def test_the_input_is_closed_when_the_sign_in_ends(self):
        stdin = self.claude()
        sign_in.start_sign_in("claude", environ=self.env)
        self.processes[0].exit(1)
        wait_for(self, lambda: sign_in.sign_in_state("claude")["state"] == "failed")
        self.assertTrue(stdin.closed)

    def test_a_failed_switch_keeps_the_old_account_and_records_no_switch(self):
        self.credential(); self.account("old@x.y")
        self.claude()
        sign_in.start_sign_in("claude", environ=self.env)
        self.processes[0].exit(1)
        wait_for(self, lambda: sign_in.sign_in_state("claude")["state"] == "failed")
        self.assertIsNone(account_state.switch("claude"))
        self.assertEqual(sign_in.claude_status(self.env, cli_path="/fake/claude", run=Recorder())["account"]["email"], "old@x.y")

    def test_a_completed_switch_records_before_and_after_and_tells_the_handler(self):
        self.credential(); self.account("old@x.y")
        told = []
        sign_in.set_completion_handler(lambda provider, entry: told.append((provider, entry)))
        self.claude()
        sign_in.start_sign_in("claude", environ=self.env)
        self.account("new@x.y")                                   # what the CLI writes when the new sign-in completes
        self.processes[0].exit(0)
        wait_for(self, lambda: told)
        provider, entry = told[0]
        self.assertEqual((provider, entry["previous_account"]["email"], entry["account"]["email"], entry["changed"], entry["state"]),
                         ("claude", "old@x.y", "new@x.y", True, "restarting"))
        self.assertEqual(account_state.switch("claude")["account"]["email"], "new@x.y")

    def test_the_same_account_again_is_not_a_change(self):
        self.credential(); self.account("same@x.y")
        sign_in.start_sign_in("claude", environ=self.env)
        self.processes[0].exit(0)
        wait_for(self, lambda: account_state.switch("claude"))
        entry = account_state.switch("claude")
        self.assertEqual((entry["changed"], entry["state"]), (False, "done"))         # no handler set: nothing is restarting

    def test_a_handler_that_raises_ends_the_switch_as_done(self):
        def broken(provider, entry):
            raise RuntimeError("boom")
        sign_in.set_completion_handler(broken)
        sign_in.start_sign_in("claude", environ=self.env)
        self.processes[0].exit(0)
        wait_for(self, lambda: (account_state.switch("claude") or {}).get("state") == "done")

    def test_a_completed_sign_in_ends_the_limit_of_the_product(self):
        account_state.observe_turn("claude", "usage_limit", None, "later", "2026-11-24T10:00:00Z", "error")
        self.assertTrue(account_state.limit("claude")["reached"])
        sign_in.start_sign_in("claude", environ=self.env)
        self.processes[0].exit(0)
        wait_for(self, lambda: account_state.switch("claude"))
        self.assertFalse(account_state.limit("claude")["reached"])

    def test_the_method_must_suit_the_product(self):
        with self.assertRaises(sign_in.SignInError):
            sign_in.start_sign_in("claude", environ=self.env, method="device_code")
        with self.assertRaises(sign_in.SignInError):
            sign_in.start_sign_in("codex", environ=self.env, method="carrier_pigeon")

    def test_the_default_way_is_the_device_code_on_linux_only_for_codex(self):
        self.assertEqual(REAL_DEFAULT_METHOD("codex", "Linux"), "device_code")
        self.assertEqual(REAL_DEFAULT_METHOD("codex", "Darwin"), "browser")
        self.assertEqual(REAL_DEFAULT_METHOD("claude", "Linux"), "browser")


class CodexDeviceCodeTest(SignInBase):
    """Codex on a computer without a browser: ``account/login/start`` with ``chatgptDeviceCode`` gives a page and a short code."""

    def start(self, script, **options):
        self.factory = lambda: FakeAppServer(script)
        self.opened = []
        return sign_in.start_sign_in("codex", environ=self.env, method="device_code",
                                     opener=lambda url: self.opened.append(url) or True, **options)

    DEVICE = {"type": "chatgptDeviceCode", "loginId": "L9", "userCode": "AB1C-DEFGH", "verificationUrl": "https://auth.openai.com/codex/device"}

    def test_the_page_and_the_code_are_shown_and_no_browser_is_opened(self):
        self.start({"account/login/start": self.DEVICE})
        wait_for(self, lambda: sign_in.sign_in_details("codex")["user_code"])
        details = sign_in.sign_in_details("codex")
        self.assertEqual((details["method"], details["page_url"], details["user_code"], details["accepts_code"]),
                         ("device_code", "https://auth.openai.com/codex/device", "AB1C-DEFGH", False))
        self.assertEqual(self.processes[0].received[-1]["params"], {"type": "chatgptDeviceCode"})
        self.assertEqual(self.opened, [])
        with self.assertRaises(sign_in.SignInError):
            sign_in.submit_code("codex", "AB1C-DEFGH")

    def test_completion_confirms_the_account_and_records_it(self):
        self.start({"account/login/start": self.DEVICE, "account/read": {"account": {"type": "chatgpt", "email": "n@x.y", "planType": "plus"}},
                    "_complete": {"loginId": "L9", "success": True, "error": None}})
        wait_for(self, lambda: account_state.switch("codex"))
        entry = account_state.switch("codex")
        self.assertEqual((entry["account"], entry["previous_account"], entry["changed"]),
                         ({"email": "n@x.y", "plan": "plus"}, None, True))
        self.assertEqual(sign_in.sign_in_state("codex")["state"], "idle")

    def test_a_switch_reads_the_account_it_replaces_first(self):
        script = {"account/login/start": self.DEVICE, "account/read": {"account": {"type": "chatgpt", "email": "o@x.y", "planType": "pro"}},
                  "_complete": {"loginId": "L9", "success": True, "error": None}}
        with mock.patch.object(sign_in, "codex_status", return_value={"signed_in": True, "account": {}}):
            self.start(script)
            wait_for(self, lambda: account_state.switch("codex"))
        server = self.processes[0]
        self.assertEqual(server.methods()[:4], ["initialize", "initialized", "account/read", "account/login/start"])
        self.assertEqual(account_state.switch("codex")["previous_account"], {"email": "o@x.y", "plan": "pro"})
        self.assertEqual(server.received[2]["params"], {"refreshToken": False})

    def test_a_cancelled_device_login_is_cancelled_at_codex_and_changes_nothing(self):
        self.start({"account/login/start": self.DEVICE})
        wait_for(self, lambda: sign_in.sign_in_details("codex")["user_code"])
        sign_in.cancel_sign_in("codex")
        server = self.processes[0]
        wait_for(self, lambda: "account/login/cancel" in server.methods())
        self.assertIsNone(account_state.switch("codex"))

    def test_a_code_that_is_missing_or_odd_fails_the_sign_in(self):
        for bad in ({**self.DEVICE, "userCode": ""}, {**self.DEVICE, "userCode": "x" * 100}, {**self.DEVICE, "verificationUrl": "http://a/b"}):
            self.start({"account/login/start": bad})
            wait_for(self, lambda: sign_in.sign_in_state("codex")["state"] == "failed")
            self.assertIsNone(sign_in.sign_in_details("codex")["user_code"])
            sign_in.cancel_sign_in("codex")

    def test_the_browser_way_still_shows_its_page(self):
        self.factory = lambda: FakeAppServer({"account/login/start": {"type": "chatgpt", "loginId": "L1", "authUrl": "https://auth.example/login"}})
        sign_in.start_sign_in("codex", environ=self.env, method="browser", opener=lambda url: True)
        wait_for(self, lambda: sign_in.sign_in_details("codex")["page_url"])
        self.assertEqual(sign_in.sign_in_details("codex")["method"], "browser")
        self.assertIsNone(sign_in.sign_in_details("codex")["user_code"])


class WebbrowserTest(unittest.TestCase):
    def test_default_opener_uses_the_stdlib(self):
        with mock.patch("webbrowser.open", return_value=True) as opened:
            self.assertTrue(sign_in._open_browser("https://a/b"))
        opened.assert_called_once_with("https://a/b")


class InstallShimTest(unittest.TestCase):
    def test_without_the_install_module_the_cli_on_path_is_used(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        binary = home / "claude"
        binary.write_text("#!/bin/sh\n"); binary.chmod(0o755)
        with mock.patch.object(sign_in, "_install", return_value=None):
            self.assertEqual(sign_in.resolve_cli("claude", {"PATH": str(home)}), {"path": str(binary), "source": "system", "version": None})
            self.assertIsNone(sign_in.resolve_cli("codex", {"PATH": str(home)}))
            self.assertEqual(sign_in.install_status("codex")["phase"], "not_downloaded")
            self.assertEqual(sign_in.launch_environment("claude"), {})


if __name__ == "__main__":
    unittest.main()
