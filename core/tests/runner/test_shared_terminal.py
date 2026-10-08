"""D44's mapped runner boundary; all identities and stores here are synthetic."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from qorqut_core.runner import RunnerError, TerminalRunner
from qorqut_core.runner import shell_storage
from qorqut_core.runner.shell_models import ShellSpec
from qorqut_core.runner.storage import input_lock
from support import FakeTmux, synthetic_binding


class SharedTerminalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="shared-terminal-r1-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "spool.sqlite3"
        self.binding = synthetic_binding()
        self.tmux = FakeTmux(self.binding)
        self.runner = self.make_runner()
        shell_storage.ensure_schema(self.database)
        self.terminal_id = uuid4()
        self.spec = ShellSpec(installation_id=uuid4(), agent_id=self.binding.agent_id,
                              recovery_epoch=self.binding.recovery_epoch, environment_generation=1,
                              home=self.root / "home", workspace=self.root / "workspace", path="/usr/bin:/bin")
        self.spec_hash = hashlib.sha256(self.spec.model_dump_json().encode()).hexdigest()
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO runner_shell_terminals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                       (str(self.terminal_id), str(self.spec.installation_id), str(self.spec.agent_id),
                        self.spec_hash, self.spec.model_dump_json(), "{}", str(self.root / "runtime"),
                        self.binding.tmux.socket_path, str(self.binding.boot_id), str(uuid4()), str(uuid4()),
                        str(uuid4()), self.binding.tmux.model_dump_json(), self.binding.native.model_dump_json()))
            db.execute("INSERT INTO runner_shell_control VALUES (?,'unclaimed',0,NULL,0)",
                       (str(self.terminal_id),))

    def make_runner(self, **changes):
        options = dict(tmux=self.tmux, capture_ready=lambda _: True, machine_hold=lambda _: False,
                       classify_composer=lambda *_: "empty", verify_paste=lambda *_: True)
        options.update(changes)
        return TerminalRunner(self.database, **options)

    def register(self, runner=None):
        return (runner or self.runner).register_binding(self.binding, terminal_id=self.terminal_id)

    def hold(self, *, mode="human", epoch=7, sequence=12):
        viewer = uuid4() if mode == "human" else None
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE runner_shell_control SET mode=?,epoch=?,viewer_id=?,last_sequence=?",
                       (mode, epoch, str(viewer) if viewer else None, sequence))
        return viewer

    def test_registration_replay_conflicts_and_multiple_historical_runs_preserve_control(self):
        viewer = self.hold()
        self.register()
        restarted = self.make_runner()
        self.assertEqual(self.binding, self.register(restarted))
        self.assertEqual(self.binding, restarted.inspect_binding(self.binding.run_id))
        state = restarted.inspect_control(self.binding)
        self.assertEqual(("human", 7, viewer, 12),
                         (state.mode, state.epoch, state.viewer_id, state.last_sequence))
        for binding, terminal_id in ((self.binding, None), (self.binding, uuid4()),
                (self.binding.model_copy(update={"run_generation": 2}), self.terminal_id)):
            with self.subTest(terminal_id=terminal_id), self.assertRaises(RunnerError):
                restarted.register_binding(binding, terminal_id=terminal_id)
        other = self.binding.model_copy(update={"run_id": uuid4(), "run_generation": 2})
        self.make_runner(tmux=FakeTmux(other)).register_binding(other, terminal_id=self.terminal_id)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM runner_control").fetchone()[0])
            self.assertEqual([(str(self.terminal_id), self.spec_hash)] * 2,
                             db.execute("SELECT terminal_id,spec_hash FROM runner_binding_terminals").fetchall())
            for sql in ("UPDATE runner_binding_terminals SET spec_hash='changed'",
                        "DELETE FROM runner_binding_terminals",
                        "INSERT OR REPLACE INTO runner_binding_terminals SELECT * FROM runner_binding_terminals"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
        self.assertEqual(state, restarted.inspect_control(self.binding))

    def test_registration_validates_saved_shell_and_never_changes_control_on_failure(self):
        invalid = [dict(agent_id=uuid4()), dict(recovery_epoch=uuid4()), dict(environment_generation=2),
                   dict(tmux=self.binding.tmux.model_copy(update={"pane_id": "%99"})),
                   dict(recorder=self.binding.recorder.model_copy(update={"start_ticks": 999}))]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(RunnerError) as error:
                self.runner.register_binding(self.binding.model_copy(update=changes), terminal_id=self.terminal_id)
            self.assertEqual("target_changed", error.exception.code)
        with self.assertRaises(RunnerError) as error:
            self.runner.register_binding(self.binding, terminal_id=uuid4())
        self.assertEqual("not_found", error.exception.code)
        for field, value, original in (("stopped", 1, 0), ("spec_hash", "0" * 64, self.spec_hash),
                                      ("tmux_json", None, self.binding.tmux.model_dump_json())):
            with sqlite3.connect(self.database) as db:
                db.execute(f"UPDATE runner_shell_terminals SET {field}=?", (value,))
            with self.subTest(field=field), self.assertRaises(RunnerError):
                self.register()
            with sqlite3.connect(self.database) as db:
                db.execute(f"UPDATE runner_shell_terminals SET {field}=?", (original,))
        with patch.object(self.tmux, "inspect", side_effect=RunnerError("target_changed", "fixture exited")), \
                self.assertRaises(RunnerError):
            self.register()
        with sqlite3.connect(self.database) as db:
            for table in ("runner_bindings", "runner_binding_terminals", "runner_control"):
                self.assertEqual(0, db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            self.assertEqual(("unclaimed", 0, None, 0), db.execute(
                "SELECT mode,epoch,viewer_id,last_sequence FROM runner_shell_control").fetchone())
            db.execute("DELETE FROM runner_shell_control")
        with self.assertRaises(RunnerError) as error:
            self.register()
        self.assertEqual("target_changed", error.exception.code)
        self.assertIsNone(self.runner.inspect_binding(self.binding.run_id))
        self.assertEqual([], self.tmux.effects)

    def test_standalone_cannot_convert_and_missing_shell_schema_is_typed(self):
        self.runner.register_binding(self.binding)
        viewer = uuid4()
        saved = self.runner.control(self.binding, uuid4(), action="take", expected_epoch=0, viewer_id=viewer)
        with self.assertRaises(RunnerError) as error:
            self.register()
        self.assertEqual("target_changed", error.exception.code)
        self.assertEqual(saved.state, self.runner.inspect_control(self.binding))
        other = TerminalRunner(self.root / "no-shell.sqlite3", tmux=self.tmux)
        with self.assertRaises(RunnerError) as error:
            self.register(other)
        self.assertEqual("not_found", error.exception.code)
        self.assertIsNone(other.inspect_binding(self.binding.run_id))

    def test_competing_initial_registrations_have_one_atomic_mapping(self):
        for mapped_wins in (True, False):
            with self.subTest(mapped_wins=mapped_wins):
                binding = self.binding.model_copy(update={"run_id": uuid4()})
                slow_tmux, fast_tmux = FakeTmux(binding), FakeTmux(binding)
                slow = self.make_runner(tmux=slow_tmux)
                fast = self.make_runner(tmux=fast_tmux)
                entered, release = threading.Event(), threading.Event()

                def paused_inspect(target):
                    entered.set()
                    self.assertTrue(release.wait(5))
                    return slow_tmux.snapshot

                with patch.object(slow_tmux, "inspect", side_effect=paused_inspect), ThreadPoolExecutor(1) as pool:
                    future = pool.submit(slow.register_binding, binding,
                                         terminal_id=None if mapped_wins else self.terminal_id)
                    try:
                        self.assertTrue(entered.wait(5))
                        fast.register_binding(binding, terminal_id=self.terminal_id if mapped_wins else None)
                    finally:
                        release.set()
                    with self.assertRaises(RunnerError) as error:
                        future.result(timeout=5)
                    self.assertEqual("target_changed", error.exception.code)
                with sqlite3.connect(self.database) as db:
                    args = (str(binding.run_id),)
                    self.assertEqual(1, db.execute("SELECT count(*) FROM runner_bindings WHERE run_id=?", args).fetchone()[0])
                    self.assertEqual(int(mapped_wins), db.execute(
                        "SELECT count(*) FROM runner_binding_terminals WHERE run_id=?", args).fetchone()[0])
                    self.assertEqual(int(not mapped_wins), db.execute(
                        "SELECT count(*) FROM runner_control WHERE run_id=?", args).fetchone()[0])

    def test_initial_registration_race_releases_old_run_gate_before_any_input(self):
        selected_run, proceed = threading.Event(), threading.Event()
        gates = []

        @contextmanager
        def paused_lock(database, identity):
            gates.append(identity)
            if identity == self.binding.run_id:
                selected_run.set()
                self.assertTrue(proceed.wait(5))
            with input_lock(database, identity):
                yield

        with patch("qorqut_core.runner.terminal.input_lock", paused_lock), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.runner.attempt, self.binding, uuid4(), "body", expected_epoch=0)
            try:
                self.assertTrue(selected_run.wait(5))
                self.register()
                with input_lock(self.database, self.terminal_id):
                    proceed.set()
                    with self.assertRaises(TimeoutError):
                        future.result(timeout=0.075)
                    self.assertEqual([], self.tmux.effects)
            finally:
                proceed.set()
            self.assertEqual("submitted", future.result(timeout=5).state)
        self.assertEqual([self.binding.run_id, self.terminal_id, self.terminal_id], gates)

    def test_mapped_methods_and_restarted_receipts_use_terminal_gate(self):
        self.register()
        self.hold()
        restarted = self.make_runner()
        calls = [lambda: restarted.register_binding(self.binding, terminal_id=self.terminal_id),
                 lambda: restarted.inspect_binding(self.binding.run_id),
                 lambda: restarted.inspect_control(self.binding), lambda: restarted.inspect(self.binding)]
        attempt_id = uuid4()
        calls += [lambda: restarted.attempt(self.binding, attempt_id, "body", expected_epoch=7),
                  lambda: restarted.inspect_attempt(attempt_id)]
        for call in calls:
            with patch("qorqut_core.runner.terminal.input_lock", wraps=input_lock) as lock:
                call()
                lock.assert_called_once_with(self.database, self.terminal_id)
        self.assertEqual([], self.tmux.effects)
        self.assertEqual("human_control", restarted.inspect_attempt(attempt_id).reason)
        self.assertEqual("target_changed", restarted.attempt(self.binding, uuid4(), "body", expected_epoch=0).reason)
        for call in (lambda: restarted.control(self.binding, uuid4(), action="release", expected_epoch=7, viewer_id=uuid4()),
                     lambda: restarted.human_input(self.binding, b"x", epoch=7, viewer_id=uuid4(), sequence=13),
                     lambda: restarted.resize(self.binding, 80, 24, epoch=7, viewer_id=uuid4(), sequence=13)):
            with patch("qorqut_core.runner.terminal.input_lock", wraps=input_lock) as lock, \
                    self.assertRaises(RunnerError) as error:
                call()
            self.assertEqual("unsupported_capability", error.exception.code)
            lock.assert_called_once_with(self.database, self.terminal_id)
        self.assertEqual(12, restarted.inspect_control(self.binding).last_sequence)
        self.hold(mode="unclaimed", epoch=8, sequence=0)
        unconfigured = TerminalRunner(self.database, tmux=self.tmux)
        self.assertEqual("automation", unconfigured.inspect_control(self.binding).mode)
        self.assertEqual("not_ready", unconfigured.attempt(self.binding, uuid4(), "body", expected_epoch=8).reason)
        self.assertEqual([], self.tmux.effects)

    def test_callbacks_are_under_terminal_gate_and_outside_sqlite_transactions(self):
        self.register()
        observed = []

        def probe(*_):
            with self.assertRaises(RunnerError) as error:
                with input_lock(self.database, self.terminal_id, timeout=0):
                    self.fail("callback escaped the terminal gate")
            self.assertEqual("unavailable", error.exception.code)
            with sqlite3.connect(self.database, timeout=0) as db:
                db.execute("BEGIN IMMEDIATE")
                observed.append(db.execute("SELECT mode FROM runner_shell_control").fetchone()[0])
                db.rollback()
            return True

        runner = self.make_runner(capture_ready=probe, machine_hold=lambda b: not probe(b),
                                  classify_composer=lambda *args: "empty" if probe(*args) else "unknown",
                                  verify_paste=probe)
        self.assertEqual("submitted", runner.attempt(self.binding, uuid4(), "body", expected_epoch=0).state)
        self.assertTrue(observed)
        self.assertEqual({"unclaimed"}, set(observed))

    def test_saved_attempt_replay_and_interrupted_intent_survive_restart_and_human_hold(self):
        self.register()
        for interrupted in (False, True):
            attempt_id = uuid4()
            self.tmux.effects.clear()
            self.hold(mode="unclaimed", epoch=0, sequence=0)
            original = self.runner._update

            def crash(db, aid, state, phase, reason):
                original(db, aid, state, phase, reason)
                if state == "dispatching" and phase == "enter":
                    raise SystemExit("synthetic process interruption")

            if interrupted:
                with patch.object(self.runner, "_update", side_effect=crash), self.assertRaises(SystemExit):
                    self.runner.attempt(self.binding, attempt_id, "body", expected_epoch=0)
            else:
                self.runner.attempt(self.binding, attempt_id, "body", expected_epoch=0)
            expected_effects = list(self.tmux.effects)
            self.hold()
            with sqlite3.connect(self.database) as db:
                db.execute("UPDATE runner_shell_terminals SET stopped=1")
            restarted = self.make_runner(capture_ready=None, verify_paste=None)
            saved = restarted.inspect_attempt(attempt_id)
            self.assertEqual("uncertain" if interrupted else "submitted", saved.state)
            if interrupted:
                self.assertEqual(("enter", "interrupted_after_intent"), (saved.phase, saved.reason))
            self.assertEqual(saved, restarted.attempt(self.binding, attempt_id, "body", expected_epoch=0))
            self.assertEqual(saved, restarted.inspect_attempt(attempt_id))
            with self.assertRaises(RunnerError) as error:
                restarted.attempt(self.binding, attempt_id, "changed", expected_epoch=0)
            self.assertEqual("idempotency_conflict", error.exception.code)
            self.assertEqual(expected_effects, self.tmux.effects)
            with sqlite3.connect(self.database) as db:
                db.execute("UPDATE runner_shell_terminals SET stopped=0")

    def test_saved_spec_hash_is_revalidated_without_mutating_control_or_input(self):
        self.register()
        viewer = self.hold()
        changed = self.spec.model_copy(update={"path": "/changed/bin"})
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE runner_shell_terminals SET spec_json=?,spec_hash=?",
                       (changed.model_dump_json(), hashlib.sha256(changed.model_dump_json().encode()).hexdigest()))
        with self.assertRaises(RunnerError) as error:
            self.make_runner().attempt(self.binding, uuid4(), "body", expected_epoch=7)
        self.assertEqual("target_changed", error.exception.code)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM runner_attempts").fetchone()[0])
            self.assertEqual(("human", 7, str(viewer), 12), db.execute(
                "SELECT mode,epoch,viewer_id,last_sequence FROM runner_shell_control").fetchone())
        self.assertEqual([], self.tmux.effects)


    def test_settlement_validates_timeout_and_preserves_zero_wait_default(self):
        for timeout in (-1, 10**1000, 1.001, float("nan"), float("inf"), "0.5", None, True):
            with self.subTest(timeout=timeout), self.assertRaises(RunnerError) as error:
                self.make_runner(paste_settle_timeout=timeout)
            self.assertEqual("invalid_argument", error.exception.code)
        self.register()
        runner = self.make_runner(verify_paste=lambda *_: False)
        with patch("qorqut_core.runner.terminal.time.sleep") as sleep:
            result = runner.attempt(self.binding, uuid4(), "body", expected_epoch=0)
            sleep.assert_not_called()
        self.assertEqual(("uncertain", "paste", "paste_unverified"), (result.state, result.phase, result.reason))
        self.assertEqual([("paste", b"\x1b[200~body\x1b[201~")], self.tmux.effects)

    def repaint_clock(self):
        test = self

        class Clock:
            now = 0.0

            def __init__(self):
                self.sleeps = []
                self.after_sleep = lambda: None

            def monotonic(self):
                return self.now

            def sleep(self, seconds):
                test.assertGreater(seconds, 0)
                test.assertLessEqual(seconds, 0.025)
                test.assertEqual([("paste", b"\x1b[200~body\x1b[201~")], test.tmux.effects)
                with sqlite3.connect(test.database) as db:
                    test.assertEqual(("dispatching", "paste"), db.execute(
                        "SELECT state,phase FROM runner_attempts ORDER BY rowid DESC LIMIT 1").fetchone())
                self.sleeps.append(seconds)
                self.now += seconds
                self.after_sleep()

        return Clock()

    def test_settlement_refreshes_snapshots_and_final_enter_proof_after_intent(self):
        self.register()
        clock = self.repaint_clock()
        proofs, captures = [], []

        def snapshot(binding):
            self.assertEqual(self.binding, binding)
            return self.tmux.snapshot.model_copy(update={"screen": f"paint {clock.now}"})

        def verify(binding, current, digest):
            self.assertEqual(hashlib.sha256(b"body").hexdigest(), digest)
            with sqlite3.connect(self.database) as db:
                phase = db.execute("SELECT phase FROM runner_attempts").fetchone()[0]
            proofs.append((current, phase))
            return clock.now >= 0.05

        runner = self.make_runner(paste_settle_timeout=0.5, verify_paste=verify,
                                  capture_ready=lambda _: captures.append(clock.now) or True)
        with patch("qorqut_core.runner.terminal.time", clock), patch.object(self.tmux, "inspect", side_effect=snapshot):
            result = runner.attempt(self.binding, uuid4(), "body", expected_epoch=0)
        self.assertEqual("submitted", result.state)
        self.assertEqual([0.025, 0.025], clock.sleeps)
        self.assertEqual(["paste", "paste", "paste", "enter"], [phase for _, phase in proofs])
        self.assertEqual(4, len({id(current) for current, _ in proofs}))
        self.assertEqual([0, 0, 0, 0, 0.025, 0.05, 0.05, 0.05], captures)
        self.assertEqual([("paste", b"\x1b[200~body\x1b[201~"), ("enter",)], self.tmux.effects)

    def test_settle_timeout_or_lost_external_proof_is_uncertain_without_more_input(self):
        self.register()
        for failure in ("timeout", "capture", "hold"):
            with self.subTest(failure=failure):
                self.tmux.effects.clear()
                clock = self.repaint_clock()
                runner = self.make_runner(paste_settle_timeout=0.06, verify_paste=lambda *_: False)
                if failure == "capture":
                    clock.after_sleep = lambda: setattr(runner, "capture_ready", lambda _: False)
                elif failure == "hold":
                    clock.after_sleep = lambda: setattr(runner, "machine_hold", lambda _: True)
                attempt_id = uuid4()
                with patch("qorqut_core.runner.terminal.time", clock):
                    result = runner.attempt(self.binding, attempt_id, "body", expected_epoch=0)
                self.assertEqual(("uncertain", "paste"), (result.state, result.phase))
                self.assertEqual({"timeout": "paste_unverified", "capture": "not_ready", "hold": "machine_hold"}[failure],
                                 result.reason)
                if failure == "timeout":
                    self.assertAlmostEqual(0.06, sum(clock.sleeps))
                    self.assertEqual(3, len(clock.sleeps))
                else:
                    self.assertEqual([0.025], clock.sleeps)
                self.assertEqual(result, self.make_runner().attempt(self.binding, attempt_id, "body", expected_epoch=0))
                self.assertEqual([("paste", b"\x1b[200~body\x1b[201~")], self.tmux.effects)

    def test_a_fresh_but_late_positive_snapshot_cannot_extend_settlement_deadline(self):
        self.register()
        clock = self.repaint_clock()

        def slow_snapshot(binding):
            if self.tmux.effects:
                clock.now += 0.51  # A synchronous boundary can overrun its allowance.
            return self.tmux.snapshot.model_copy()

        runner = self.make_runner(paste_settle_timeout=0.5)
        with patch("qorqut_core.runner.terminal.time", clock), patch.object(self.tmux, "inspect", side_effect=slow_snapshot):
            result = runner.attempt(self.binding, uuid4(), "body", expected_epoch=0)
        self.assertEqual(("uncertain", "paste_unverified"), (result.state, result.reason))
        self.assertEqual([], clock.sleeps)
        self.assertEqual([("paste", b"\x1b[200~body\x1b[201~")], self.tmux.effects)

    def test_final_proof_loss_after_settlement_does_not_poll_or_enter(self):
        self.register()
        for failure in ("snapshot", "capture", "hold"):
            with self.subTest(failure=failure):
                self.tmux.effects.clear()
                clock = self.repaint_clock()
                phases = []

                def verify(binding, snapshot, digest):
                    with sqlite3.connect(self.database) as db:
                        phase = db.execute("SELECT phase FROM runner_attempts ORDER BY rowid DESC LIMIT 1").fetchone()[0]
                    phases.append(phase)
                    if phase == "enter":
                        if failure == "snapshot":
                            return False
                        if failure == "capture":
                            runner.capture_ready = lambda _: False
                        else:
                            runner.machine_hold = lambda _: True
                    return clock.now >= 0.025

                runner = self.make_runner(paste_settle_timeout=0.5, verify_paste=verify)
                with patch("qorqut_core.runner.terminal.time", clock):
                    result = runner.attempt(self.binding, uuid4(), "body", expected_epoch=0)
                self.assertEqual(("uncertain", "enter"), (result.state, result.phase))
                self.assertEqual(["paste", "paste", "enter"], phases)
                self.assertEqual([0.025], clock.sleeps)
                self.assertEqual([("paste", b"\x1b[200~body\x1b[201~")], self.tmux.effects)


if __name__ == "__main__":
    unittest.main()
