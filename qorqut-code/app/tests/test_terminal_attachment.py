"""The selected-conversation attachment of Terminals mode: its view, the one resume command (with the agent's stored model and
effort), a selection that is not Claude Code, and the refusal on a managed installation. No tmux, no client, no real process."""

from contextlib import contextmanager, nullcontext
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.managed_runtime import mode, options
from qorqut.native_chat import attachment, selection
from qorqut.native_chat.probe import ProbeResult, REASON_NO_MARKER

CONVERSATION = "11111111-2222-3333-4444-555555555555"


def chosen(provider="claude", conversation=CONVERSATION):
    return selection.Selection("ordubashy", conversation, provider, 3, "running", {})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="attachment ")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.paths = SimpleNamespace(runtime=self.base)
        self.selected = chosen()
        self.probed, self.pane_reads = [], []

        @contextmanager
        def read():
            yield SimpleNamespace(connection=None)
        self.probe = SimpleNamespace(observe=lambda profile: (self.probed.append(profile), ProbeResult(REASON_NO_MARKER))[1])
        self.backend = SimpleNamespace(pane_observation=lambda profile, client: (self.pane_reads.append(client), {"state": "absent"})[1])
        self.runtime = SimpleNamespace(database=SimpleNamespace(read=read), paths=self.paths,
                                       terminal_control=SimpleNamespace(backend=self.backend))
        self.service = attachment.AttachmentService(self.runtime, self.probe, lambda agent: nullcontext())
        for name, value in (("active_profile", {"agent_id": "ordubashy", "workspace": str(self.base)}),):
            patched = mock.patch.object(attachment.store, name, lambda *args, _value=value: _value)
            patched.start()
            self.addCleanup(patched.stop)
        patched = mock.patch.object(attachment.selection, "selected", lambda connection, agent: self.selected)
        patched.start()
        self.addCleanup(patched.stop)

    def terminals(self):
        patched = mock.patch.object(mode, "current", return_value="terminal")
        patched.start()
        self.addCleanup(patched.stop)


class StateTest(Fixture):
    def test_a_selected_claude_conversation_is_the_attachment(self):
        self.terminals()
        view = self.service.state("ordubashy")
        self.assertEqual(view["mode"], "selected")
        self.assertEqual(view["selection"]["conversation_id"], CONVERSATION)
        self.assertEqual(self.service.revision("ordubashy"), "3:claude:" + CONVERSATION)
        self.assertEqual(self.pane_reads, ["claude"])

    def test_a_conversation_of_another_client_counts_as_nothing_selected(self):
        self.terminals()
        self.selected = chosen("codex")
        view = self.service.state("ordubashy")
        self.assertEqual((view["mode"], view["selection"], view["reason"]), ("first_launch", None, selection.REASON_NO_SELECTION))
        self.assertIsNone(self.service.revision("ordubashy"))
        self.assertEqual(self.pane_reads, [])

    def test_an_agent_whose_effective_client_is_not_claude_code_has_nothing_selected(self):
        self.terminals()
        self.backend.terminal_client = lambda profile: None
        view = self.service.state("ordubashy")
        self.assertEqual((view["mode"], view["selection"]), ("first_launch", None))
        self.backend.terminal_client = lambda profile: "claude"
        self.assertEqual(self.service.state("ordubashy")["mode"], "selected")

    def test_no_selection_is_a_first_launch(self):
        self.terminals()
        self.selected = None
        self.assertEqual(self.service.state("ordubashy")["mode"], "first_launch")

    def test_a_managed_installation_is_not_looked_at(self):
        view = self.service.state("ordubashy")
        self.assertEqual((view["state"], view["reason"]), ("unsupported", "managed"))
        self.assertEqual((self.probed, self.pane_reads), ([], []))


class ResumeTest(Fixture):
    def command(self):
        with mock.patch.object(attachment, "effective_executable", return_value="/bin/claude"), \
                mock.patch.object(attachment.features, "probe", return_value={"resume": True}):
            return self.service._command(self.selected, 4242)

    def test_the_resume_command_is_the_bare_resume_without_options(self):
        self.assertEqual(self.command(), "claude --resume " + CONVERSATION)

    def test_the_stored_model_and_effort_follow_the_resume(self):
        options.write(self.paths, "ordubashy", model="opus[1m]", effort="high")
        self.assertEqual(self.command(), "claude --resume " + CONVERSATION + " --model 'opus[1m]' --effort high")

    def test_a_model_of_the_other_client_is_not_passed_but_the_effort_is(self):
        options.write(self.paths, "ordubashy", model="gpt-6-luna", effort="low")
        self.assertEqual(self.command(), "claude --resume " + CONVERSATION + " --effort low")

    def test_minimal_effort_is_written_as_claude_takes_it(self):
        options.write(self.paths, "ordubashy", effort="minimal")
        self.assertEqual(self.command(), "claude --resume " + CONVERSATION + " --effort low")

    def test_a_conversation_id_that_is_not_a_uuid_is_refused(self):
        self.selected = chosen(conversation="--model x")
        with self.assertRaises(DomainError):
            self.command()

    def test_nothing_selected_resumes_nothing_and_asks_for_no_pane(self):
        self.terminals()
        self.selected = None
        self.assertIsNone(self.service.resume_selected("ordubashy", None))
        self.selected = chosen("codex")
        self.assertIsNone(self.service.resume_selected("ordubashy", None))
        self.assertEqual(self.pane_reads, [])


class ManagedRefusalTest(Fixture):
    def test_resume_and_reconnect_refuse_without_touching_anything(self):
        for call in (lambda: self.service.resume_selected("ordubashy", self.selected.revision),
                     lambda: self.service.reconnect("ordubashy", self.selected.revision)):
            with self.assertRaises(DomainError) as raised:
                call()
            self.assertEqual(raised.exception.code, "terminal_mode_off")
        self.assertEqual((self.probed, self.pane_reads), ([], []))

    def test_a_switch_in_progress_is_retryable(self):
        self.terminals()
        with mock.patch.object(mode, "switching", return_value=True):
            with self.assertRaises(DomainError) as raised:
                self.service.resume_selected("ordubashy", self.selected.revision)
        self.assertEqual((raised.exception.code, raised.exception.retryable), ("runtime_switching", True))


if __name__ == "__main__":
    unittest.main()
