import hashlib
from concurrent.futures import ThreadPoolExecutor
import shutil
import sqlite3
import threading
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

    def test_exactly_one_explicit_frame_with_mode_on_off_and_boundary_changes(self):
        receiver = self.receiver

        class ChangeModeAtPaste(Tmux):
            final_mode = True

            def load_buffer(self, binding, buffer, wire):
                self.loaded = buffer, wire
                super().load_buffer(binding, buffer, wire)

            def paste(self, *args, **kwargs):
                receiver.set_bracketed(self.final_mode)
                super().paste(*args, **kwargs)

        tmux = ChangeModeAtPaste()
        # For this transport assertion the deterministic receiver reports its
        # exact current bytes, independently of whether framing was received.
        runner = receiver.runner(tmux=tmux, verify_paste=lambda b, s, digest: bool(
            wait_for(lambda: receiver.state()["current_sha256"] == digest)))
        for initial, final in ((True, True), (False, False), (True, False), (False, True)):
            with self.subTest(initial=initial, final=final):
                receiver.set_bracketed(initial)
                tmux.final_mode = final
                body = f"mode {initial} → {final}\n世界\tλ"
                payload = body.encode()
                offset = len(receiver.bytes())
                submissions = len(receiver.state()["submitted"])
                result = runner.attempt(self.binding, uuid4(), body, expected_epoch=0)
                self.assertEqual("submitted", result.state)
                wait_for(lambda: len(receiver.state()["submitted"]) == submissions + 1)
                actual = receiver.bytes()[offset:]
                self.assertEqual(b"\x1b[200~" + payload + b"\x1b[201~\r", actual)
                self.assertEqual(1, actual.count(b"\x1b[200~"))
                self.assertEqual(1, actual.count(b"\x1b[201~"))
                self.assertEqual(hashlib.sha256(payload).hexdigest(), result.payload_sha256)
                self.assertEqual(len(payload), result.payload_bytes)
                buffer, wire = tmux.loaded
                self.assertEqual(b"\x1b[200~" + payload + b"\x1b[201~", wire)
                self.assertEqual(hashlib.sha256(wire).hexdigest(), buffer.wire_sha256)
                with self.assertRaises(RunnerError) as caught:
                    tmux.load_buffer(self.binding, buffer, wire + b"changed")
                self.assertEqual("idempotency_conflict", caught.exception.code)
                self.assertEqual(b"", receiver.command_run("list-buffers", "-F", "#{buffer_name}"))

    def test_modal_during_shared_sqlite_enter_intent_wait_is_reverified_without_enter(self):
        initial_proof, writer_ready, enter_write = (threading.Event() for _ in range(3))
        observed_modal = []

        def proof(binding, snapshot, digest):
            modal = "SYNTHETIC MODAL" in snapshot.screen
            observed_modal.append(modal)
            if len(observed_modal) == 1:
                self.assertTrue(self.receiver.verify(binding, snapshot, digest))
                initial_proof.set()
                self.assertTrue(writer_ready.wait(5))
            return not modal

        runner = self.receiver.runner(verify_paste=proof)
        update = runner._update

        def trace(statement):
            if statement.startswith("UPDATE runner_attempts") and "phase='enter'" in statement:
                enter_write.set()

        def observe_enter_write(db, attempt_id, state, phase, reason):
            if state == "dispatching" and phase == "enter":
                db.set_trace_callback(trace)
                try:
                    return update(db, attempt_id, state, phase, reason)
                finally:
                    db.set_trace_callback(None)
            return update(db, attempt_id, state, phase, reason)

        attempt_id, body = uuid4(), "paste before write wait λ\n"
        with patch.object(runner, "_update", side_effect=observe_enter_write), \
                ThreadPoolExecutor(1) as pool, sqlite3.connect(self.receiver.database, timeout=5) as writer:
            future = pool.submit(runner.attempt, self.binding, attempt_id, body, expected_epoch=0)
            try:
                self.assertTrue(initial_proof.wait(5))
                writer.execute("BEGIN IMMEDIATE")
                writer_ready.set()
                self.assertTrue(enter_write.wait(5))
                self.assertFalse(future.done())
                self.assertEqual(("dispatching", "paste"), writer.execute(
                    "SELECT state,phase FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone())
                # Synthetic native output changes while the runner's real
                # UPDATE waits on the shared spool's SQLite write lock.
                self.receiver.show_modal()
                self.assertFalse(future.done())
            finally:
                writer_ready.set()
                writer.commit()
            result = future.result(timeout=5)
        self.assertEqual([False, True], observed_modal)
        self.assertEqual(("uncertain", "enter", "paste_unverified"),
                         (result.state, result.phase, result.reason))
        expected = b"\x1b[200~" + body.encode() + b"\x1b[201~"
        self.assertEqual(expected, self.receiver.bytes())
        self.assertEqual(result, runner.attempt(self.binding, attempt_id, body, expected_epoch=0))
        self.assertEqual(expected, self.receiver.bytes())
        self.assertEqual([False, True], observed_modal)

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


@unittest.skipUnless(shutil.which("tmux"), "real tmux unavailable")
class FinalReadinessTests(unittest.TestCase):
    def test_readiness_loss_during_final_verifier_wait_prevents_enter(self):
        cases = (
            ("capture_ready", False, "not_ready"),
            ("capture_ready", None, "not_ready"),
            ("capture_ready", "missing", "not_ready"),
            ("capture_ready", "error", "boundary_failure"),
            ("machine_hold", True, "machine_hold"),
            ("machine_hold", None, "machine_hold"),
            ("machine_hold", "missing", "not_ready"),
            ("machine_hold", "error", "boundary_failure"),
        )
        for callback, outcome, reason in cases:
            with self.subTest(callback=callback, outcome=outcome):
                receiver = Receiver()
                try:
                    final_verifier, settle, changed = (threading.Event() for _ in range(3))
                    observations = []
                    verifications = 0

                    def readiness(name, ready):
                        observations.append(name)
                        if changed.is_set() and name == callback:
                            if outcome == "error":
                                raise OSError("synthetic readiness unavailable")
                            return outcome
                        return ready

                    def proof(binding, snapshot, digest):
                        nonlocal verifications
                        verifications += 1
                        if verifications == 2:
                            final_verifier.set()
                            self.assertTrue(settle.wait(5))
                            # A settling verifier refreshes its own evidence;
                            # this does not refresh capture/hold observations.
                            snapshot = Tmux().inspect(binding)
                        verified = (receiver.verify(binding, snapshot, digest)
                                    and not snapshot.in_mode and not snapshot.input_off
                                    and not snapshot.synchronized
                                    and "SYNTHETIC MODAL" not in snapshot.screen)
                        if verifications == 2:
                            observations.append("proof")
                        return verified

                    runner = receiver.runner(
                        capture_ready=lambda _: readiness("capture_ready", True),
                        machine_hold=lambda _: readiness("machine_hold", False),
                        verify_paste=proof)
                    attempt_id, body = uuid4(), "capture/hold changes while settling λ\n"
                    with ThreadPoolExecutor(1) as pool:
                        future = pool.submit(runner.attempt, receiver.binding, attempt_id,
                                             body, expected_epoch=0)
                        try:
                            self.assertTrue(final_verifier.wait(5))
                            self.assertEqual(2, verifications)
                            observations.clear()
                            changed.set()
                            if outcome == "missing":
                                setattr(runner, callback, None)
                        finally:
                            settle.set()
                        result = future.result(timeout=5)
                    self.assertEqual(("uncertain", "enter", reason),
                                     (result.state, result.phase, result.reason))
                    expected_observations = ["proof"]
                    if callback != "capture_ready" or outcome != "missing":
                        expected_observations.append("capture_ready")
                    if callback == "machine_hold" and outcome != "missing":
                        expected_observations.append("machine_hold")
                    self.assertEqual(expected_observations, observations)
                    expected = b"\x1b[200~" + body.encode() + b"\x1b[201~"
                    self.assertEqual(expected, receiver.bytes())
                    self.assertEqual([], receiver.state()["submitted"])
                    self.assertEqual(result, runner.attempt(receiver.binding, attempt_id,
                                                           body, expected_epoch=0))
                    self.assertEqual(expected, receiver.bytes())
                    self.assertEqual(expected_observations, observations)
                    self.assertEqual(2, verifications)
                finally:
                    receiver.close()


if __name__ == "__main__":
    unittest.main()
