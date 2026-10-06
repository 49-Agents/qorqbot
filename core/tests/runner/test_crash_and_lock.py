from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from uuid import uuid4

import qorqut_core
from qorqut_core.runner import AttemptReceipt, ControlReceipt
from effect_worker import BODY
from support import Receiver, wait_for


@unittest.skipUnless(shutil.which("tmux"), "real tmux unavailable")
class CrashAndLockTests(unittest.TestCase):
    def launch(self, receiver, mode, identity, result_name):
        (receiver.root / "binding.json").write_text(receiver.binding.model_dump_json())
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(qorqut_core.__file__).resolve().parent.parent),
               "HOME": str(receiver.root)}
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("effect_worker.py")),
             str(receiver.root), mode, str(identity), result_name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )

        def close():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)

        self.addCleanup(close)
        return process

    def check_exit(self, process, expected):
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(expected, process.returncode, (stdout + stderr).decode())

    def test_process_death_receipts_before_paste_after_paste_enter_and_commit(self):
        for stage, expected_state, phase, expected_bytes in (
            ("prepared", "deferred", "prepared", b""),
            ("load", "deferred", "prepared", b""),
            ("before_paste", "uncertain", "paste", b""),
            ("paste", "uncertain", "paste", b"\x1b[200~" + BODY.encode() + b"\x1b[201~"),
            ("enter", "uncertain", "enter", b"\x1b[200~" + BODY.encode() + b"\x1b[201~\r"),
            ("before_receipt", "uncertain", "enter", b"\x1b[200~" + BODY.encode() + b"\x1b[201~\r"),
            ("after_receipt", "submitted", "finished", b"\x1b[200~" + BODY.encode() + b"\x1b[201~\r"),
        ):
            with self.subTest(stage=stage):
                receiver = Receiver()
                try:
                    receiver.runner()
                    attempt_id = uuid4()
                    process = self.launch(receiver, "crash:" + stage, attempt_id, "result.json")
                    self.check_exit(process, 91)
                    wait_for(lambda: receiver.bytes() == expected_bytes)
                    restarted = receiver.runner()
                    receipt = restarted.inspect_attempt(attempt_id)
                    self.assertEqual((expected_state, phase), (receipt.state, receipt.phase))
                    self.assertEqual(receipt, restarted.attempt(receiver.binding, attempt_id, BODY, expected_epoch=0))
                    self.assertEqual(expected_bytes, receiver.bytes())
                    self.assertEqual(b"", receiver.command_run("list-buffers", "-F", "#{buffer_name}"))
                finally:
                    receiver.close()

    def test_concurrent_attempt_replay_inspection_and_control_wait_for_effect_and_receipt(self):
        receiver = Receiver()
        self.addCleanup(receiver.close)
        runner = receiver.runner()
        attempt_id = uuid4()
        first = self.launch(receiver, "pause", attempt_id, "first.json")
        wait_for(lambda: (receiver.root / "paused").exists())
        duplicate = self.launch(receiver, "attempt", attempt_id, "duplicate.json")
        inspecting = self.launch(receiver, "inspect", attempt_id, "inspect.json")
        control = self.launch(receiver, "control", uuid4(), "control.json")
        for name in ("duplicate.json", "inspect.json", "control.json"):
            wait_for(lambda name=name: (receiver.root / (name + ".entered")).exists())
        time.sleep(0.1)
        for name in ("duplicate.json", "inspect.json", "control.json"):
            self.assertFalse((receiver.root / name).exists(), name + " escaped the input lock")
        with sqlite3.connect(receiver.database) as db:
            self.assertEqual(("dispatching", "paste"), db.execute("SELECT state,phase FROM runner_attempts").fetchone())
        self.assertEqual("automation", runner.inspect_control(receiver.binding).mode)
        (receiver.root / "continue").touch()
        for process in (first, duplicate, inspecting, control):
            self.check_exit(process, 0)
        receipt = AttemptReceipt.model_validate_json((receiver.root / "first.json").read_text())
        self.assertEqual("submitted", receipt.state)
        self.assertEqual(receipt, AttemptReceipt.model_validate_json((receiver.root / "duplicate.json").read_text()))
        self.assertEqual(receipt, AttemptReceipt.model_validate_json((receiver.root / "inspect.json").read_text()))
        transition = ControlReceipt.model_validate_json((receiver.root / "control.json").read_text())
        self.assertEqual(("completed", "human", 1), (transition.outcome, transition.state.mode, transition.state.epoch))
        expected = b"\x1b[200~" + BODY.encode() + b"\x1b[201~\r"
        wait_for(lambda: receiver.bytes() == expected)
        self.assertEqual("deferred", runner.attempt(receiver.binding, uuid4(), "wait", expected_epoch=1).state)
        self.assertEqual(expected, receiver.bytes())

    def test_distinct_concurrent_attempts_keep_buffers_and_input_separate(self):
        receiver = Receiver()
        self.addCleanup(receiver.close)
        receiver.runner()
        first = self.launch(receiver, "pause", uuid4(), "first.json")
        wait_for(lambda: (receiver.root / "paused").exists())
        second = self.launch(receiver, "attempt", uuid4(), "second.json")
        wait_for(lambda: (receiver.root / "second.json.entered").exists())
        time.sleep(0.1)
        self.assertFalse((receiver.root / "second.json").exists())
        (receiver.root / "continue").touch()
        self.check_exit(first, 0)
        self.check_exit(second, 0)
        for name in ("first.json", "second.json"):
            self.assertEqual("submitted", AttemptReceipt.model_validate_json((receiver.root / name).read_text()).state)
        expected = (b"\x1b[200~" + BODY.encode() + b"\x1b[201~\r") * 2
        wait_for(lambda: receiver.bytes() == expected)
        self.assertEqual(b"", receiver.command_run("list-buffers", "-F", "#{buffer_name}"))


if __name__ == "__main__":
    unittest.main()
