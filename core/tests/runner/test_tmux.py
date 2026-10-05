import hashlib
import shutil
import unittest
from uuid import uuid4
from unittest.mock import patch

from pydantic import ValidationError

from qorqut_core.runner import RunnerError, TerminalRunner, Tmux
from support import Receiver, wait_for


@unittest.skipUnless(shutil.which("tmux"), "real tmux unavailable")
class RealTmuxTests(unittest.TestCase):
    def setUp(self):
        self.receiver = Receiver()
        self.addCleanup(self.receiver.close)
        self.binding = self.receiver.binding
        self.runner = self.receiver.runner()

    def test_unicode_multiline_shell_text_and_exact_utf8_limit(self):
        marker = self.receiver.root / "SHOULD_NOT_EXIST"
        bodies = [f"Türkmençe 👋\n第二行\t$(touch {marker}); `false`\n",
                  "🌍" * (65536 // 4)]
        expected = b""
        for body in bodies:
            payload = body.encode()
            result = self.runner.attempt(self.binding, uuid4(), body, expected_epoch=0)
            self.assertEqual("submitted", result.state)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), result.payload_sha256)
            self.assertEqual(len(payload), result.payload_bytes)
            expected += b"\x1b[200~" + payload + b"\x1b[201~\r"
            wait_for(lambda: self.receiver.bytes() == expected)
            wait_for(lambda: self.receiver.state()["submitted"][-1:] == [result.payload_sha256])
        self.assertEqual(2, self.receiver.state()["pastes"])
        before = self.receiver.bytes()
        with self.assertRaises(RunnerError) as caught:
            self.runner.attempt(self.binding, uuid4(), bodies[-1] + "é", expected_epoch=0)
        self.assertEqual("invalid_argument", caught.exception.code)
        self.assertEqual(before, self.receiver.bytes())
        self.assertFalse(marker.exists())
        self.assertEqual(b"", self.receiver.command_run("list-buffers", "-F", "#{buffer_name}"))

    def test_binding_process_start_and_socket_identity_cannot_be_reused(self):
        for altered in (
            self.binding.model_copy(update={"run_generation": 2}),
            self.binding.model_copy(update={"recovery_epoch": uuid4()}),
            self.binding.model_copy(update={"environment_generation": 2}),
        ):
            with self.subTest(binding=altered.digest), self.assertRaises(RunnerError):
                self.runner.inspect(altered)
        for key, value in (("socket_inode", self.binding.tmux.socket_inode + 1),
                           ("pane_id", "%999999"),
                           ("server", self.binding.tmux.server.model_copy(update={"start_ticks": 1}))):
            altered = self.binding.model_copy(update={"tmux": self.binding.tmux.model_copy(update={key: value})})
            with self.subTest(key=key), self.assertRaises(RunnerError):
                Tmux().inspect(altered)
        with self.assertRaises(ValidationError):
            self.binding.run_generation = 9
        self.assertEqual(b"", self.receiver.bytes())

    def test_human_control_raw_bytes_resize_and_stale_frames(self):
        viewer, observer = uuid4(), uuid4()
        take = self.runner.control(self.binding, uuid4(), action="take", expected_epoch=0, viewer_id=viewer)
        self.assertEqual("completed", take.outcome)
        self.assertEqual(1, take.state.epoch)
        with self.assertRaises(RunnerError):
            self.runner.human_input(self.binding, b"bad", epoch=1, viewer_id=observer, sequence=1)
        result = self.runner.human_input(self.binding, b"human text", epoch=1, viewer_id=viewer, sequence=1)
        self.assertEqual("submitted", result.outcome)
        wait_for(lambda: self.receiver.bytes() == b"human text")
        wait_for(lambda: self.receiver.state()["current_bytes"] == 10)
        release = self.runner.control(self.binding, uuid4(), action="release", expected_epoch=1, viewer_id=viewer)
        self.assertEqual(("deferred", "composer_blocked", 1), (release.outcome, release.reason, release.state.epoch))
        self.assertEqual(b"human text", self.receiver.bytes())
        resize = self.runner.resize(self.binding, 132, 44, epoch=1, viewer_id=viewer, sequence=2)
        self.assertEqual("submitted", resize.outcome)
        wait_for(lambda: self.receiver.state()["columns"] == 132 and self.receiver.state()["rows"] == 44)
        snapshot = self.runner.inspect(self.binding)
        self.assertEqual((132, 44), (snapshot.columns, snapshot.rows))
        restarted = self.receiver.runner()
        self.assertEqual("human", restarted.inspect_control(self.binding).mode)
        with self.assertRaises(RunnerError):
            restarted.human_input(self.binding, b"duplicate", epoch=1, viewer_id=viewer, sequence=1)
        takeover = restarted.control(self.binding, uuid4(), action="take", expected_epoch=1, viewer_id=observer)
        self.assertEqual(2, takeover.state.epoch)
        with self.assertRaises(RunnerError):
            restarted.human_input(self.binding, b"stale", epoch=1, viewer_id=viewer, sequence=3)
        restarted.human_input(self.binding, b"\r", epoch=2, viewer_id=observer, sequence=1)
        wait_for(lambda: self.receiver.state()["current_bytes"] == 0)
        final = restarted.control(self.binding, uuid4(), action="release", expected_epoch=2, viewer_id=observer)
        self.assertEqual(("completed", "automation", 3), (final.outcome, final.state.mode, final.state.epoch))

    def test_absent_injected_readiness_never_types_or_launches(self):
        default = TerminalRunner(self.receiver.database)
        result = default.attempt(self.binding, uuid4(), "must wait", expected_epoch=0)
        self.assertEqual(("deferred", "not_ready"), (result.state, result.reason))
        self.assertEqual(b"", self.receiver.bytes())
        self.assertEqual(self.binding, default.inspect_binding(self.binding.run_id))
        self.assertIsNone(default.inspect_binding(uuid4()))

    def test_inspect_rechecks_identity_after_capturing_screen(self):
        tmux = Tmux()
        original = tmux._command
        observations = 0

        def replace_metadata(binding, *argv, **kwargs):
            nonlocal observations
            result = original(binding, *argv, **kwargs)
            if argv[0] == "display-message":
                observations += 1
                if observations == 2:
                    fields = result.decode().strip().split("|")
                    fields[4] = str(binding.tmux.pane.pid + 1)
                    return "|".join(fields).encode()
            return result

        with patch.object(tmux, "_command", side_effect=replace_metadata), self.assertRaises(RunnerError) as caught:
            tmux.inspect(self.binding)
        self.assertEqual("target_changed", caught.exception.code)
        self.assertEqual(b"", self.receiver.bytes())


if __name__ == "__main__":
    unittest.main()
