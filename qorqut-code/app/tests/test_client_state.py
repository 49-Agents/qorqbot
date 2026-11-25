"""What an agent is told when its session cannot open on its client (``native_chat/client_state.py``) and how the manager refuses
before anything starts. No client is started: the status of each client is supplied."""
import isolated_cli_environment  # before any Qorqbot import

import json
import unittest

from qorqut.managed_runtime import manager as hosts, options
from qorqut.native_chat import client_state as state
from qorqut.native_chat.chat import NativeChat, _OPEN_REFUSALS, _OWN_MESSAGE
from qorqut_operations.contracts import DomainError

import test_managed_manager as base

AGENT = base.AGENT


def row(installed=True, signed_in=True, phase="not_downloaded", message=None, sign_in="idle"):
    return {"installed": installed, "signed_in": signed_in, "install": {"phase": phase, "message": message},
            "sign_in": {"state": sign_in, "reason": None}}


class States(unittest.TestCase):
    def test_every_way_a_client_can_be_unusable(self):
        cases = {
            "ready": row(),
            "not_signed_in": row(signed_in=False),
            "signing_in": row(signed_in=False, sign_in="connecting"),
            "downloading": row(installed=False, signed_in=False, phase="downloading"),
            "too_old": row(installed=False, signed_in=False, message="The Codex on this computer is too old (0.1.0; version 0.170.0 or newer is needed)."),
            "no_version": row(installed=False, signed_in=False, message="The Codex on this computer did not report its version, so Qorqbot will not use it."),
            "not_installed": row(installed=False, signed_in=False),
        }
        for expected, item in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(state._state(item), expected)
        self.assertEqual(state._state(None), "not_installed")

    def test_the_sentence_names_the_agent_its_client_the_state_and_the_action(self):
        ready, bad = {"state": "ready"}, lambda name: {"state": name}
        expected = {
            ("codex", "not_signed_in", True): ("cli_not_signed_in", "Helper is set to use Codex, and Codex isn't signed in yet. Sign in under Settings > Model/Provider, or switch Helper to Claude Code there."),
            ("codex", "not_signed_in", False): ("cli_not_signed_in", "Helper is set to use Codex, and Codex isn't signed in yet. Sign in under Settings > Model/Provider, then send again."),
            ("codex", "not_installed", True): ("cli_unavailable", "Helper is set to use Codex, and Codex isn't installed yet. Download it under Settings > Model/Provider, or switch Helper to Claude Code there."),
            ("claude", "not_installed", False): ("cli_unavailable", "Helper is set to use Claude Code, and Claude Code isn't installed yet. Download it under Settings > Model/Provider, then send again."),
            ("codex", "too_old", True): ("cli_unavailable", "Helper is set to use Codex, and the Codex on this computer is too old. Download Qorqbot's own copy under Settings > Model/Provider, or switch Helper to Claude Code there."),
            ("codex", "no_version", False): ("cli_unavailable", "Helper is set to use Codex, and the Codex on this computer didn't report its version. Download Qorqbot's own copy under Settings > Model/Provider, then send again."),
            ("codex", "downloading", True): ("cli_unavailable", "Helper is set to use Codex, and Codex is still downloading. Wait for the download to finish under Settings > Model/Provider, or switch Helper to Claude Code there."),
            ("claude", "signing_in", True): ("cli_not_signed_in", "Helper is set to use Claude Code, and Claude Code isn't signed in yet. Finish signing in in your browser, or start again under Settings > Model/Provider, or switch Helper to Codex there."),
        }
        for (client, name, other_ready), answer in expected.items():
            with self.subTest(client=client, state=name, other_ready=other_ready):
                other = "claude" if client == "codex" else "codex"
                found = {client: bad(name), other: ready if other_ready else bad("not_installed")}
                self.assertEqual(state.refusal("Helper", client, found), answer)
                self.assertLessEqual(len(answer[1]), 240, "the dashboard shows what is at most 240 characters")

    def test_a_ready_or_unknown_client_is_not_refused_and_a_nameless_agent_is_this_agent(self):
        self.assertIsNone(state.refusal("Helper", "codex", {"codex": {"state": "ready"}}))
        self.assertIsNone(state.refusal("Helper", "codex", {"codex": {"state": "unknown"}}))
        self.assertIsNone(state.refusal("Helper", "codex", {}))
        self.assertTrue(state.refusal("", "codex", {"codex": {"state": "not_installed"}})[1].startswith("This agent is set to use Codex"))
        long = state.refusal("x" * 200, "codex", {"codex": {"state": "not_signed_in"}})[1]
        self.assertLessEqual(len(long), 240)

    def test_an_unreadable_status_refuses_nothing(self):
        from unittest import mock
        with mock.patch("qorqut.owner_settings.agent_clients_view", side_effect=RuntimeError("x")):
            self.assertEqual(state.states(), {"claude": {"state": "unknown"}, "codex": {"state": "unknown"}})

    def test_chat_passes_these_messages_on_as_they_are(self):
        for code in state.CODES:
            self.assertIn(code, _OWN_MESSAGE)
            self.assertEqual(_OPEN_REFUSALS[code][0], code)
        error = NativeChat._open_failure(DomainError("cli_not_signed_in", "Helper is set to use Codex, and Codex isn't signed in yet."))
        self.assertEqual((error.code, error.message), ("cli_not_signed_in", "Helper is set to use Codex, and Codex isn't signed in yet."))


class Refusal(base.ManagerFixture):
    """The manager checks the agent's own client before it prepares or starts anything."""

    def manager_with(self, found):
        return hosts.ManagedRuntime(self.runtime, launcher=self.launcher, prepare=self.manager._prepare_override, resume=lambda a: None,
                                    executable=str(self.cli), clock=lambda: self.now[0], sleep=self.advance, client_state=lambda: found)

    def profile(self, name="Helper"):
        return {"agent_id": AGENT, "profile_json": json.dumps({"runtime": "claude-code", "name": name, "display_name": name}), "name": name}

    def test_codex_not_signed_in_is_refused_by_name_and_nothing_starts(self):
        options.write(self.paths, AGENT, client="codex")
        manager = self.manager_with({"claude": {"state": "ready"}, "codex": {"state": "not_signed_in"}})
        with self.assertRaises(hosts.ManagedError) as refused:
            manager.ensure_open(self.profile(), 5)
        self.assertEqual(refused.exception.code, "cli_not_signed_in")
        self.assertEqual(refused.exception.message, "Helper is set to use Codex, and Codex isn't signed in yet. Sign in under "
                                                    "Settings > Model/Provider, or switch Helper to Claude Code there.")
        self.assertEqual((self.launcher.started, self.prepared), ([], []))

    def test_codex_not_installed_keeps_the_stable_code(self):
        options.write(self.paths, AGENT, client="codex")
        manager = self.manager_with({"claude": {"state": "not_installed"}, "codex": {"state": "not_installed"}})
        with self.assertRaises(hosts.ManagedError) as refused:
            manager.ensure_open(self.profile(), 5)
        self.assertEqual(refused.exception.code, "cli_unavailable")
        self.assertTrue(refused.exception.message.endswith("Download it under Settings > Model/Provider, then send again."))
        self.assertEqual(self.launcher.started, [])

    def test_a_ready_client_opens_and_an_agent_with_no_client_set_moves_to_the_ready_one(self):
        manager = self.manager_with({"claude": {"state": "not_signed_in"}, "codex": {"state": "ready"}})
        manager.ensure_open(self.profile(), 5)
        self.assertEqual(self.launcher.started[0][1]["client"], "codex")


class FirstTurnError(unittest.TestCase):
    def test_the_clients_own_words_about_a_failed_turn_reach_the_chat_view(self):
        status = {"phase": "idle", "last_turn_status": "error",
                  "last_turn": {"status": "error", "error": "You've hit your usage limit. Try again at 4:30 PM.", "reason": "UsageLimitExceeded"}}
        view = hosts._view({"state": "ready", "session_id": "s"}, status)
        self.assertEqual(view["turn_error"], "You've hit your usage limit. Try again at 4:30 PM.")
        fine = hosts._view({"state": "ready"}, {"phase": "idle", "last_turn": {"status": "completed"}})
        self.assertIsNone(fine["turn_error"])
        self.assertIsNone(hosts._view({"state": "ready"}, None)["turn_error"])
        from qorqut.native_chat.chat import MANAGED_FIELDS
        self.assertIn("turn_error", MANAGED_FIELDS)


if __name__ == "__main__":
    unittest.main()
