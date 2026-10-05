import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut_core.runner import RunnerError, TerminalRunner, ensure_schema, fingerprint, verify_process
from support import FakeTmux, synthetic_binding


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="terminal-r1-receipts-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "spool.sqlite3"
        self.binding = synthetic_binding()
        self.tmux = FakeTmux(self.binding)
        self.runner = self.make_runner()
        self.runner.register_binding(self.binding)

    def make_runner(self, **changes):
        options = dict(tmux=self.tmux, capture_ready=lambda _: True, machine_hold=lambda _: False,
                       classify_composer=lambda *_: "empty", verify_paste=lambda *_: True)
        options.update(changes)
        return TerminalRunner(self.database, **options)

    def test_missing_or_negative_admission_evidence_never_types(self):
        cases = [dict(capture_ready=None), dict(capture_ready=lambda _: False),
                 dict(capture_ready=lambda _: "healthy"), dict(machine_hold=None),
                 dict(machine_hold=lambda _: True), dict(machine_hold=lambda _: None),
                 dict(classify_composer=None), dict(verify_paste=None)]
        cases += [dict(classify_composer=lambda *_, value=verdict: value)
                  for verdict in ("busy", "blocked", "unknown", "text", "modal", None)]
        for options in cases:
            with self.subTest(options=options):
                result = self.make_runner(**options).attempt(self.binding, uuid4(), "payload", expected_epoch=0)
                self.assertEqual("deferred", result.state)
                self.assertEqual([], self.tmux.effects)
                self.assertEqual({}, self.tmux.buffers)

    def test_unsafe_text_is_rejected_before_any_attempt_or_tmux_operation(self):
        bodies = ["left" + chr(code) + "right" for code in
                  [*range(0, 9), *range(11, 32), *range(0x7f, 0xa0)]]
        bodies += ["text\x1b[201~\r", "CRLF\r\n", "invalid\ud800", "invalid\udfff"]
        with patch.object(self.tmux, "inspect", wraps=self.tmux.inspect) as inspect, \
                patch.object(self.tmux, "load_buffer", wraps=self.tmux.load_buffer) as load:
            for body in bodies:
                attempt_id = uuid4()
                with self.subTest(body=repr(body)), self.assertRaises(RunnerError) as caught:
                    self.runner.attempt(self.binding, attempt_id, body, expected_epoch=0)
                self.assertEqual(("invalid_argument", "unsafe_terminal_text"),
                                 (caught.exception.code, caught.exception.reason))
                self.assertIsNone(self.runner.inspect_attempt(attempt_id))
            inspect.assert_not_called()
            load.assert_not_called()
        with sqlite3.connect(self.database) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM runner_attempts").fetchone()[0])
        self.assertEqual([], self.tmux.effects)
        self.assertEqual({}, self.tmux.buffers)

        # Interactive keys use the distinct epoch/viewer path, not automated text.
        viewer = uuid4()
        self.runner.control(self.binding, uuid4(), action="take", expected_epoch=0, viewer_id=viewer)
        result = self.runner.human_input(self.binding, b"\x1b[A\r", epoch=1, viewer_id=viewer, sequence=1)
        self.assertEqual("submitted", result.outcome)
        self.assertEqual([("paste", b"\x1b[A\r")], self.tmux.effects)

    def test_idempotent_receipt_is_exact_and_conflicts_do_not_replay(self):
        attempt_id = uuid4()
        first = self.runner.attempt(self.binding, attempt_id, "é\n", expected_epoch=0)
        self.assertEqual("submitted", first.state)
        self.assertEqual(hashlib.sha256("é\n".encode()).hexdigest(), first.payload_sha256)
        restarted = TerminalRunner(self.database)
        self.assertEqual(first, restarted.attempt(self.binding, attempt_id, "é\n", expected_epoch=0))
        self.assertEqual(first, restarted.inspect_attempt(attempt_id))
        for binding, body, epoch in ((self.binding, "changed", 0), (self.binding, "é\n", 1),
                                     (self.binding.model_copy(update={"run_generation": 2}), "é\n", 0)):
            with self.subTest(body=body, epoch=epoch), self.assertRaises(RunnerError) as error:
                restarted.attempt(binding, attempt_id, body, expected_epoch=epoch)
            self.assertEqual("idempotency_conflict", error.exception.code)
        self.assertEqual([("paste", "é\n".encode()), ("enter",)], self.tmux.effects)
        self.assertEqual(first.binding, restarted.inspect_binding(self.binding.run_id))

    def test_pre_effect_failure_defers_and_post_effect_failure_stays_uncertain(self):
        for failure, outcome, count in (("load", "deferred", 0), ("paste", "uncertain", 1),
                                       ("enter", "uncertain", 2)):
            self.tmux.effects.clear()
            self.tmux.fail = failure
            attempt_id = uuid4()
            result = self.runner.attempt(self.binding, attempt_id, "body", expected_epoch=0)
            self.assertEqual((outcome, count), (result.state, len(self.tmux.effects)))
            self.tmux.fail = None
            self.assertEqual(result, self.make_runner().attempt(self.binding, attempt_id, "body", expected_epoch=0))
            self.assertEqual(count, len(self.tmux.effects))
            self.assertEqual({}, self.tmux.buffers)

    def test_conflicting_attempt_ids_racing_on_different_runs_have_one_dispatcher(self):
        other = synthetic_binding()
        other_tmux = FakeTmux(other)
        other_runner = self.make_runner(tmux=other_tmux)
        other_runner.register_binding(other)
        attempt_id = uuid4()
        barrier = threading.Barrier(2)

        def synchronize(db, binding):
            TerminalRunner._require_binding(db, binding)
            barrier.wait(timeout=5)

        with patch.object(self.runner, "_require_binding", side_effect=synchronize), \
                patch.object(other_runner, "_require_binding", side_effect=synchronize), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(runner.attempt, binding, attempt_id, "body", expected_epoch=0)
                       for runner, binding in ((self.runner, self.binding), (other_runner, other))]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10).state)
                except RunnerError as error:
                    outcomes.append(error.code)
        self.assertCountEqual(["submitted", "idempotency_conflict"], outcomes)
        self.assertEqual(2, len(self.tmux.effects) + len(other_tmux.effects))

    def test_paste_evidence_binding_capture_and_composer_rechecked(self):
        result = self.make_runner(verify_paste=lambda *_: False).attempt(self.binding, uuid4(), "body", expected_epoch=0)
        self.assertEqual(("uncertain", "paste_unverified"), (result.state, result.reason))
        self.assertEqual([("paste", b"body")], self.tmux.effects)
        self.tmux.effects.clear()
        self.tmux.after_load = lambda: setattr(self.tmux, "binding", synthetic_binding())
        result = self.runner.attempt(self.binding, uuid4(), "body", expected_epoch=0)
        self.assertEqual(("deferred", "target_changed"), (result.state, result.reason))
        self.assertEqual([], self.tmux.effects)
        self.tmux.binding = self.binding
        self.tmux.after_load = None

        def replace_after_paste(*_):
            self.tmux.binding = synthetic_binding()
            return True

        result = self.make_runner(verify_paste=replace_after_paste).attempt(self.binding, uuid4(), "body", expected_epoch=0)
        self.assertEqual(("uncertain", "target_changed"), (result.state, result.reason))
        self.assertEqual([("paste", b"body")], self.tmux.effects)
        self.tmux.binding = self.binding
        self.tmux.effects.clear()
        checks = iter(["empty", "empty", "blocked"])
        result = self.make_runner(classify_composer=lambda *_: next(checks)).attempt(
            self.binding, uuid4(), "body", expected_epoch=0)
        self.assertEqual(("deferred", "composer_blocked"), (result.state, result.reason))
        self.assertEqual([], self.tmux.effects)

    def test_capture_loss_and_external_hold_after_paste_never_send_enter(self):
        for field in ("capture_ready", "machine_hold"):
            self.tmux.effects.clear()

            def verify(*_):
                setattr(runner, field, (lambda _: False) if field == "capture_ready" else (lambda _: True))
                return True

            runner = self.make_runner(verify_paste=verify)
            result = runner.attempt(self.binding, uuid4(), "body", expected_epoch=0)
            self.assertEqual("uncertain", result.state)
            self.assertEqual([("paste", b"body")], self.tmux.effects)

    def test_receipt_commit_loss_leaves_recoverable_uncertainty(self):
        attempt_id = uuid4()
        original = self.runner._update

        def fail_final(db, aid, state, phase, reason):
            if state in ("submitted", "uncertain"):
                raise sqlite3.OperationalError("synthetic full disk")
            original(db, aid, state, phase, reason)

        with patch.object(self.runner, "_update", side_effect=fail_final), self.assertRaises(RunnerError) as caught:
            self.runner.attempt(self.binding, attempt_id, "body", expected_epoch=0)
        self.assertEqual("uncertain_effect", caught.exception.code)
        receipt = self.make_runner().inspect_attempt(attempt_id)
        self.assertEqual(("uncertain", "enter"), (receipt.state, receipt.phase))
        self.assertEqual(receipt, self.runner.attempt(self.binding, attempt_id, "body", expected_epoch=0))
        self.assertEqual([("paste", b"body"), ("enter",)], self.tmux.effects)

    def test_control_receipts_epochs_and_higher_level_hold_are_independent(self):
        viewer, control_id = uuid4(), uuid4()
        first = self.runner.control(self.binding, control_id, action="take", expected_epoch=0, viewer_id=viewer)
        self.assertEqual(("human", 1), (first.state.mode, first.state.epoch))
        restarted = self.make_runner()
        restarted.register_binding(self.binding)
        self.assertEqual(first.state, restarted.inspect_control(self.binding))
        self.assertEqual(first, restarted.control(self.binding, control_id, action="take", expected_epoch=0, viewer_id=viewer))
        with self.assertRaises(RunnerError):
            restarted.control(self.binding, control_id, action="release", expected_epoch=1, viewer_id=viewer)
        with self.assertRaises(RunnerError):
            restarted.control(self.binding, uuid4(), action="release", expected_epoch=0, viewer_id=viewer)
        with self.assertRaises(RunnerError):
            restarted.control(self.binding, uuid4(), action="release", expected_epoch=1, viewer_id=uuid4())
        result = restarted.attempt(self.binding, uuid4(), "body", expected_epoch=1)
        self.assertEqual(("deferred", "human_control"), (result.state, result.reason))
        held = self.make_runner(machine_hold=lambda _: True)
        release_id = uuid4()
        result = held.control(self.binding, release_id, action="release", expected_epoch=1, viewer_id=viewer)
        self.assertEqual(("deferred", "machine_hold", "human"), (result.outcome, result.reason, result.state.mode))
        blocked = self.make_runner(classify_composer=lambda *_: "blocked")
        result = blocked.control(self.binding, uuid4(), action="release", expected_epoch=1, viewer_id=viewer)
        self.assertEqual(("deferred", 1), (result.outcome, result.state.epoch))
        released = restarted.control(self.binding, uuid4(), action="release", expected_epoch=1, viewer_id=viewer)
        self.assertEqual(("completed", "automation", 2), (released.outcome, released.state.mode, released.state.epoch))
        # Re-registration/restart cannot clear a distinct external hold, even
        # after the exact run-local control transition is complete.
        held.register_binding(self.binding)
        result = held.attempt(self.binding, uuid4(), "body", expected_epoch=2)
        self.assertEqual(("deferred", "machine_hold"), (result.state, result.reason))
        self.assertEqual([], self.tmux.effects)

    def test_uncertain_manual_frame_is_consumed_before_effect(self):
        viewer = uuid4()
        self.runner.control(self.binding, uuid4(), action="take", expected_epoch=0, viewer_id=viewer)
        self.tmux.fail = "paste"
        result = self.runner.human_input(self.binding, b"x", epoch=1, viewer_id=viewer, sequence=1)
        self.assertEqual("uncertain", result.outcome)
        with self.assertRaises(RunnerError):
            self.make_runner().human_input(self.binding, b"x", epoch=1, viewer_id=viewer, sequence=1)
        with self.assertRaises(RunnerError):
            self.runner.resize(self.binding, 120, 40, epoch=1, viewer_id=uuid4(), sequence=2)
        self.assertEqual([("paste", b"x")], self.tmux.effects)

    def test_invalid_resize_does_not_consume_a_frame_or_reach_tmux(self):
        viewer = uuid4()
        self.runner.control(self.binding, uuid4(), action="take", expected_epoch=0, viewer_id=viewer)
        for columns, rows in ((True, 30), (0, 30), (80, 300), ("80", 30)):
            with self.subTest(columns=columns, rows=rows), self.assertRaises(RunnerError) as caught:
                self.runner.resize(self.binding, columns, rows, epoch=1, viewer_id=viewer, sequence=1)
            self.assertEqual("invalid_argument", caught.exception.code)
        self.assertEqual(0, self.runner.inspect_control(self.binding).last_sequence)
        self.assertEqual([], self.tmux.effects)

    def test_schema_coexists_with_spool_and_preserves_global_user_version(self):
        with sqlite3.connect(self.database) as db:
            db.execute("PRAGMA user_version=37")
            db.execute("CREATE TABLE capture_fixture (value TEXT)")
            db.execute("INSERT INTO capture_fixture VALUES ('untouched')")
            db.commit()
            ensure_schema(db)
            ensure_schema(db)
            self.assertEqual(37, db.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual("untouched", db.execute("SELECT value FROM capture_fixture").fetchone()[0])
            self.assertEqual("wal", db.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, db.execute("PRAGMA foreign_keys").fetchone()[0])
            self.assertEqual(2, db.execute("PRAGMA synchronous").fetchone()[0])
            self.assertEqual(5000, db.execute("PRAGMA busy_timeout").fetchone()[0])
            db.execute("BEGIN")
            with self.assertRaises(ValueError):
                ensure_schema(db)
            self.assertTrue(db.in_transaction)


class ProcessTests(unittest.TestCase):
    def test_proc_fixture_handles_comm_and_refuses_pid_reuse_dead_or_wrong_boot(self):
        with tempfile.TemporaryDirectory(prefix="terminal-r1-proc-") as directory:
            root = Path(directory)
            boot_path = root / "sys/kernel/random/boot_id"
            boot_path.parent.mkdir(parents=True)
            boot_path.write_text(str(uuid4()))
            process = root / "123"
            (process / "ns").mkdir(parents=True)
            (process / "ns/pid").touch()
            fields = ["S"] + ["0"] * 19
            fields[19] = "100"
            stat_path = process / "stat"
            stat_path.write_text("123 (name with ) parentheses) " + " ".join(fields))
            first = fingerprint(123, proc_root=root)
            self.assertEqual(100, first.start_ticks)
            verify_process(first, proc_root=root)
            fields[19] = "101"
            stat_path.write_text("123 (same name) " + " ".join(fields))
            with self.assertRaises(RunnerError):
                verify_process(first, proc_root=root)
            second = fingerprint(123, proc_root=root)
            boot_path.write_text(str(uuid4()))
            with self.assertRaises(RunnerError):
                verify_process(second, proc_root=root)
            fields[0] = "Z"
            stat_path.write_text("123 (same name) " + " ".join(fields))
            with self.assertRaises(RunnerError):
                fingerprint(123, proc_root=root)


if __name__ == "__main__":
    unittest.main()
