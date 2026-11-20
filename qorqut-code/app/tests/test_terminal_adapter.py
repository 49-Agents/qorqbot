"""Terminals mode: ``TerminalAdapter`` and the arbiter's delivery rules for a terminal agent (native_chat/adapters.py, arbiter.py).

Nothing here starts tmux: the backend is a recording stand-in. The mode is set explicitly on a temporary runtime folder.
"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut.managed_runtime import mode
from qorqut.native_chat import arbiter as arbiter_module
from qorqut.native_chat.adapters import ManagedAdapter, TerminalAdapter
from qorqut.native_chat.arbiter import InputArbiter
from qorqut.native_chat.contracts import OwnerInputOutcome, TYPE_CHAT, TYPE_TELEGRAM, InputRequest
from qorqut.native_chat.probe import ProbeResult, REASON_NO_MARKER

AGENT = "11111111-1111-4111-8111-111111111111"
SESSION = "33333333-3333-4333-8333-333333333333"
TERMINAL_ROW = {"id": "s1", "transport": "terminal", "provider": "claude", "conversation_id": SESSION, "owner": None,
                "transcript_path": "/t"}


class Backend:
    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or OwnerInputOutcome("submitted", "processing_not_confirmed", {"client": "claude"})

    def owner_input(self, row, text, **options):
        self.calls.append((row, text, options))
        return self.outcome


class AdapterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.profile = {"agent_id": AGENT, "workspace": str(self.workspace), "profile_json": json.dumps({"runtime": "codex"})}

    def submit(self, backend, text="hello", **kwargs):
        options = {"steer": False, "echo": True, "checkpoint": lambda: None, **kwargs}
        return TerminalAdapter(lambda: backend).submit(self.profile, TERMINAL_ROW, text, **options)

    def test_no_backend_defers_as_terminal_unavailable(self):
        outcome = self.submit(None)
        self.assertEqual((outcome.status, outcome.reason), ("deferred", "terminal_unavailable"))

    def test_the_session_client_decides_the_composer_rules_not_the_profile(self):
        backend = Backend()
        outcome = self.submit(backend, steer=True, echo=False, replace_composer=True)
        self.assertEqual(outcome.status, "submitted")
        row, text, options = backend.calls[0]
        self.assertEqual(json.loads(row["profile_json"])["runtime"], "claude-code")
        self.assertEqual(text, "hello")
        self.assertTrue(options["require_paste_echo"] and options["allow_busy"] and options["replace_composer"])
        self.assertEqual(options["workdir"], self.workspace / ".qorqut/chat-input")
        self.assertEqual(options["workdir"].stat().st_mode & 0o777, 0o700)

    def test_a_bare_slash_command_gets_one_trailing_space(self):
        backend = Backend()
        self.submit(backend, text="/compact")
        self.assertEqual(backend.calls[0][1], "/compact ")

    def test_the_name_is_terminal(self):
        self.assertEqual(TerminalAdapter.name, "terminal")


class ArbiterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.paths = SimpleNamespace(runtime=Path(directory.name))
        self.backend = Backend()
        self.managed = SimpleNamespace(name="managed", submit=mock.Mock())
        self.runtime = SimpleNamespace(paths=self.paths)
        self.arbiter = InputArbiter(self.runtime, None, None, {"managed": self.managed, "terminal": TerminalAdapter(lambda: self.backend)})
        self.profile = {"agent_id": AGENT, "workspace": directory.name, "profile_json": json.dumps({"runtime": "claude-code"})}

    def terminals(self):
        mode.commit(self.paths, mode.TERMINAL)

    def run_input(self, row):
        return self.arbiter._run(self.profile, row, "hi", False, TYPE_CHAT, lambda: None, {})

    def test_a_single_adapter_is_the_managed_one(self):
        single = InputArbiter(self.runtime, None, None, self.managed)
        self.assertIs(single.adapter, self.managed)
        self.assertEqual(single.adapters, {"managed": self.managed})
        self.assertIs(self.arbiter.adapter, self.managed)

    def test_a_terminal_row_is_delivered_by_the_terminal_adapter_in_terminals_mode(self):
        self.terminals()
        outcome, name = self.run_input(TERMINAL_ROW)
        self.assertEqual((name, outcome.status), ("terminal", "submitted"))
        self.assertEqual(len(self.backend.calls), 1)
        self.managed.submit.assert_not_called()

    def test_a_managed_row_is_unsupported_in_terminals_mode(self):
        self.terminals()
        outcome, _ = self.run_input({**TERMINAL_ROW, "transport": "managed"})
        self.assertEqual((outcome.status, outcome.reason), ("deferred", "unsupported_transport"))
        self.managed.submit.assert_not_called()

    def test_a_terminal_row_is_unsupported_in_managed_mode(self):
        outcome, name = self.run_input(TERMINAL_ROW)
        self.assertEqual((outcome.status, outcome.reason, name), ("deferred", "unsupported_transport", "managed"))
        self.assertEqual(self.backend.calls, [])

    def test_a_codex_queue_row_is_unsupported_in_both_modes(self):
        for terminal in (False, True):
            if terminal:
                self.terminals()
            outcome, _ = self.run_input({**TERMINAL_ROW, "transport": "codex_queue"})
            self.assertEqual(outcome.reason, "unsupported_transport")

    def request(self, input_type=TYPE_TELEGRAM, wait=False):
        return InputRequest(AGENT, input_type, "key-1", "hello", wait=wait)

    def drive(self, request):
        """``submit`` for an agent with no live session; returns the result and the opener's calls."""
        opener = mock.Mock()
        self.arbiter.opener = opener
        self.arbiter.probe = SimpleNamespace(observe=lambda profile: ProbeResult(REASON_NO_MARKER))
        self.arbiter.registry = SimpleNamespace(reconcile=lambda profile, observed: SimpleNamespace(session=None, live=False))
        self.arbiter.runtime = SimpleNamespace(paths=self.paths, database=SimpleNamespace(read=mock.MagicMock()))
        with mock.patch.object(InputArbiter, "_profile", return_value=self.profile), \
                mock.patch.object(arbiter_module.store, "request_hash", return_value="h"), \
                mock.patch.object(arbiter_module.store, "input_by_key", return_value=None):
            return self.arbiter.submit(request), opener

    def test_terminals_mode_never_wakes_and_reports_the_terminal_offline(self):
        self.terminals()
        result, opener = self.drive(self.request())
        self.assertEqual((result.state, result.reason), ("deferred", "terminal_offline"))
        self.assertEqual(opener.mock_calls, [])

    def test_an_owner_chat_input_for_an_offline_terminal_is_rejected(self):
        self.terminals()
        result, opener = self.drive(self.request(TYPE_CHAT, wait=True))
        self.assertEqual((result.state, result.reason), ("rejected", "terminal_offline"))
        self.assertEqual(opener.mock_calls, [])

    def test_managed_mode_still_wakes_and_says_session_closed(self):
        opener = mock.Mock()
        opener.wakeable.return_value = False
        self.arbiter.opener = opener
        result, opener = self.drive(self.request())
        self.assertEqual(result.reason, "session_closed")
        self.assertTrue(opener.wakeable.called)


if __name__ == "__main__":
    unittest.main()
