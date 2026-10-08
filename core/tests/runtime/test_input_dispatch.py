"""D46 input proofs: real disposable ancestry, synthetic recorder/layout evidence."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import patch
from uuid import UUID, uuid4

from runtime.test_native_hooks import NativeHookCase
from runner.support import FakeTmux, synthetic_binding
from qorqut_core.config import LocalTerminalSettings
from qorqut_core.contracts import CoreError, Invocation
from qorqut_core.messaging.notifications import notification_text
from qorqut_core.runner.models import ProcessFingerprint, RunnerError, TerminalSnapshot
from qorqut_core.runner.shell import ShellManager
from qorqut_core.runner.shell_models import ShellCapture
from qorqut_core.runner.storage import input_lock
from qorqut_core.runner.terminal import TerminalRunner
from qorqut_core.runtime.input_dispatch import InputUnavailable, RuntimeInput
from qorqut_core.runtime.native_collector import NativeCollector
from qorqut_core.transcripts.archive_models import ArchiveMetadata
from qorqut_core.transcripts.probe import ProbeResult
from qorqut_core.transcripts.spool import CaptureSpool, RunBinding as CaptureBinding, Source


@dataclass(frozen=True)
class SyntheticRawObservation:
    """Fixed D48 return contract, without its separately owned implementation."""

    binding: CaptureBinding
    observed_at: str
    source_count: int
    healthy: bool


class InputFixture(NativeHookCase):
    def setUp(self):
        super().setUp()
        local = LocalTerminalSettings(runtime_root=self.root / "r", python_executable=Path(sys.executable),
            tmux_executable=Path("/usr/bin/tmux"), bash_executable=Path("/usr/bin/bash"), path=os.defpath,
            native_programs=self.programs)
        self.settings = self.settings.model_copy(update={"local_terminal": local})
        self.app.settings = self.settings
        self.app.terminals.settings = local
        self.app.native_collector = NativeCollector(self.app)
        self.process, shell, self.request = self.chain()
        result = self.handle(shell, self.request)
        self.assertEqual(result.outcome, "connected")
        self.owner_row = self.rows("session_owners")[0]
        self.native_process = ProcessFingerprint.model_validate_json(self.owner_row["process_json"])
        with self.app.db.read() as tx:
            selected, _, _ = self.app.terminals._target(tx, self.host["id"])
        self.configuration = self.app.terminals._configuration(selected)
        self.database = self.configuration["database"]
        local.runtime_root.mkdir(mode=0o700)
        self.manager = ShellManager(**self.configuration)
        tmux = synthetic_binding().tmux.model_copy(update={
            "socket_path": str(self.configuration["socket_path"]), "server": shell.shell, "pane": shell.shell})
        self.shell = shell.model_copy(update={"tmux": tmux, "recorder": shell.shell,
            "capture": ProbeResult.model_construct(verified=True, terminal_active=True, reason="active", observation=None),
            "saved_capture": ShellCapture(health="healthy", local_captured_offset=0, host_archived_offset=0,
                recorder_finished_at=None, child_exit_code=None, drain_complete=False)})
        shell = self.shell
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO runner_shell_terminals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (str(shell.terminal_id), str(shell.spec.installation_id), str(shell.spec.agent_id),
                 hashlib.sha256(shell.spec.model_dump_json().encode()).hexdigest(), shell.spec.model_dump_json(),
                 json.dumps({k: str(v) for k, v in self.configuration.items()}, sort_keys=True),
                 str(self.configuration["runtime_root"]), str(self.configuration["socket_path"]),
                 str(shell.shell.boot_id), str(shell.source_id), str(shell.source_epoch), str(uuid4()),
                 shell.tmux.model_dump_json(), shell.shell.model_dump_json()))
            db.execute("INSERT INTO runner_shell_control VALUES (?,'unclaimed',0,NULL,0)", (str(shell.terminal_id),))
        self.capture_binding = CaptureBinding(str(shell.spec.installation_id), str(shell.spec.agent_id),
            str(shell.spec.recovery_epoch), 1, str(shell.shell.boot_id), str(shell.terminal_id), 1)
        with CaptureSpool(self.database) as spool:
            spool.register_run(self.capture_binding)
            source = Source(str(shell.source_id), str(shell.spec.installation_id), str(shell.spec.agent_id), "terminal")
            spool.register_source(source, str(shell.source_epoch), str(shell.terminal_id), capability_evidence="synthetic recorder")
            spool.claim_recording(str(shell.source_id), str(shell.source_epoch), str(shell.terminal_id))
            spool.activate_recording(str(shell.source_id), str(shell.source_epoch))
        self.configured(shell)
        self.archive_binding = CaptureBinding(str(shell.spec.installation_id), str(shell.spec.agent_id),
            str(shell.spec.recovery_epoch), 1, str(shell.shell.boot_id), self.owner_row["owner_id"], 1)
        self.native_source = Source(str(uuid4()), str(shell.spec.installation_id), str(shell.spec.agent_id),
            "native", self.owner_row["native_session_id"])
        self.native_epoch = str(uuid4())
        self.admit_source()
        self.raw_patch = patch.object(self.app.native_collector, "input_observation",
            side_effect=self.raw_observation_for, create=True)
        self.raw_patch.start()
        self.addCleanup(self.raw_patch.stop)
        self.publish_capture()
        self.observations = 0

        def observe(manager, row):
            assert_external(self, self.database, self.shell.terminal_id)
            self.observations += 1
            return self.shell.model_copy(update={"control": manager.control(self.shell.terminal_id)})

        self.observe_patch = patch.object(ShellManager, "_inspect", observe)
        self.observe_patch.start()
        self.addCleanup(self.observe_patch.stop)
        self.input = RuntimeInput(self.app)
        self.binding = synthetic_binding().model_copy(update={"agent_id": shell.spec.agent_id,
            "recovery_epoch": shell.spec.recovery_epoch, "boot_id": shell.shell.boot_id,
            "run_id": UUID(self.owner_row["owner_id"]), "tmux": shell.tmux,
            "recorder": shell.recorder, "native": self.native_process})
        self.tmux = FakeTmux(self.binding)
        self.empty = self.snapshot()
        self.tmux.snapshot = self.empty
        self.tmux_patch = patch("qorqut_core.runtime.input_dispatch.Tmux", return_value=self.tmux)
        self.tmux_patch.start()
        self.addCleanup(self.tmux_patch.stop)
        paste = self.tmux.paste

        def painted(binding, buffer):
            payload = self.tmux.buffers[buffer.name][6:-6].decode("ascii")
            paste(binding, buffer)
            self.tmux.snapshot = self.snapshot(payload)

        self.tmux.paste = painted

    def snapshot(self, payload=None):
        fixture = json.loads((Path(__file__).parents[1] / "adapters/fixtures/native-composers.json").read_bytes())["captures"][0]
        snapshot = TerminalSnapshot(columns=fixture["columns"], rows=fixture["rows"],
            cursor_x=fixture["cursor"][0], cursor_y=fixture["cursor"][1], cursor_visible=fixture["cursor"][2],
            in_mode=False, input_off=False, synchronized=False, screen=fixture["screen"])
        if payload:
            snapshot = snapshot.model_copy(update={"cursor_x": 2 + len(payload),
                "screen": snapshot.screen.replace("❯\xa0\x1b[7m", "❯\xa0" + payload + "\x1b[7m")})
        return snapshot

    def publish_capture(self):
        self.app.native_collector._publish(self.host["id"], state="collecting", reason="active", source_count=1)
        self.raw_observation = SyntheticRawObservation(self.archive_binding, self.clock(), 1, True)

    def raw_observation_for(self, run_id):
        self.assertEqual(run_id, self.owner_row["owner_id"])
        return self.raw_observation

    def admit_source(self):
        self.app.archive.register(Invocation(actor_id=self.host["actor_id"]), self.archive_binding,
            self.native_source, self.native_epoch, ArchiveMetadata(capability_evidence="synthetic primary native source"))

    def hook(self, event):
        request = self.request.model_copy(update={"payload": {**self.request.payload, "hook_event_name": event}})
        return self.handle(self.shell, request)

    def refuse(self, reason):
        with self.assertRaises(InputUnavailable) as error:
            self.input.prepare(UUID(self.host["id"]))
        self.assertEqual(error.exception.reason, reason)
        self.assertEqual(self.tmux.effects, [])


def assert_external(case, database, terminal_id):
    from qorqut_core.storage.database import assert_external_io_allowed
    assert_external_io_allowed()
    with case.assertRaises(RunnerError) as error:
        with input_lock(database, terminal_id, timeout=0):
            case.fail("observation escaped terminal gate")
    case.assertEqual(error.exception.code, "unavailable")
    # These writes roll back; they prove callbacks have no host/runner SQL writer.
    for path in (database, case.app.db.path):
        with sqlite3.connect(path, timeout=0) as db:
            db.execute("BEGIN IMMEDIATE")
            db.rollback()


class InputProofs(InputFixture):
    def test_exited_process_cannot_use_a_saved_live_owner_observation(self):
        self.exit_child(self.process)
        self.assertEqual(self.rows("session_owners")[0]["observation"], "live")
        self.refuse("session_unavailable")

    def test_normal_application_job_composes_runtime_and_saves_typed_attempt(self):
        sent = self.app.invoke("messages_send", self.context, {"idempotency_key": "composed",
            "recipient_agent_id": self.host["id"], "body": "body stays exact\x00\x1b\n"})
        self.assertTrue(sent.ok, sent)
        while self.app.jobs.run_next(worker_id=uuid4()) is not None:
            pass
        result = self.app.invoke("deliveries_get", self.context,
            {"message_id": sent.data["message"]["message_id"]})
        self.assertTrue(result.ok, result)
        self.assertEqual(result.data["delivery"]["state"], "submitted")
        self.assertEqual(result.data["attempts"][0]["state"], "submitted")
        self.assertEqual(result.data["attempts"][0]["run_id"], self.owner_row["owner_id"])
        self.assertEqual([item[0] for item in self.tmux.effects], ["paste", "enter"])
        self.assertEqual(self.rows("messages")[0]["body"], "body stays exact\x00\x1b\n")

    def test_ready_target_exact_notification_gate_callbacks_and_receipt_after_native_exit(self):
        target = self.input.prepare(UUID(self.host["id"]))
        self.assertEqual(target.binding, self.binding)
        with sqlite3.connect(self.database) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='runner_attempts'").fetchone())
        mid, aid = uuid4(), uuid4()
        checks = []

        def still_current():
            assert_external(self, self.database, self.shell.terminal_id)
            checks.append(True)
            return True

        result = self.input.submit(target, aid, mid, still_current=still_current)
        self.assertEqual(result.state, "submitted")
        self.assertGreaterEqual(len(checks), 6)
        self.assertEqual(self.tmux.effects, [("paste", b"\x1b[200~" + notification_text(mid).encode() + b"\x1b[201~"), ("enter",)])
        self.exit_child(self.process)
        self.app.session_owners.reconcile()
        self.assertEqual(self.input.inspect(target, aid), result)
        self.assertEqual(len(self.tmux.effects), 2)
        self.recovery()
        self.assertEqual(self.input.inspect(target, aid), result)
        self.refuse_after_exit()

    def refuse_after_exit(self):
        with self.assertRaises(InputUnavailable):
            self.input.prepare(UUID(self.host["id"]))

    def test_long_idle_hook_is_valid_but_stale_collector_observation_is_not(self):
        before = self.input.prepare(UUID(self.host["id"]))
        self.clock.advance(3600)
        # D48 returns None when its observation/source monotonic freshness
        # expires. A fresh aggregate view must not serve as a fallback.
        self.app.native_collector._publish(self.host["id"], state="collecting", reason="active", source_count=1)
        self.raw_observation = None
        self.refuse("capture_unavailable")
        self.publish_capture()
        self.assertEqual(self.input.prepare(UUID(self.host["id"])), before)

    def test_healthy_raw_observation_allows_visible_parser_and_coverage_gaps(self):
        for reason in ("parser_gap", "capture_gap"):
            self.app.native_collector._publish(self.host["id"], state="gap", reason=reason,
                source_count=1, pending_archive_bytes=31, pending_parse_bytes=17)
            before = self.runtime()["native_capture"]
            target = self.input.prepare(UUID(self.host["id"]))
            self.assertEqual(target.binding, self.binding)
            self.assertEqual(self.runtime()["native_capture"], before)
        receipt = self.input.submit(target, uuid4(), uuid4(), still_current=lambda: True)
        self.assertEqual(receipt.state, "submitted")
        self.assertEqual(self.runtime()["native_capture"], before)
        self.assertEqual([effect[0] for effect in self.tmux.effects], ["paste", "enter"])

    def test_absent_unhealthy_old_or_mismatched_raw_observation_refuses(self):
        original = self.raw_observation
        # No method on this branch is a known-no-input capture refusal.
        with patch.object(self.app.native_collector, "input_observation", None):
            self.refuse("capture_unavailable")
        for value in (None, replace(original, healthy=False), replace(original, healthy=1),
                      replace(original, observed_at=(datetime.fromisoformat(self.owner_row["connected_at"])
                          - timedelta(seconds=1)).isoformat())):
            self.raw_observation = value
            self.refuse("capture_unavailable")
        for key, value in (("installation_id", str(uuid4())), ("agent_id", str(uuid4())),
                           ("recovery_epoch", str(uuid4())), ("environment_generation", 2),
                           ("boot_id", str(uuid4())), ("run_id", str(uuid4())), ("run_generation", 2)):
            with self.subTest(binding_field=key):
                self.raw_observation = replace(original, binding=replace(original.binding, **{key: value}))
                self.refuse("capture_unavailable")
        self.raw_observation = original
        self.assertEqual(self.input.prepare(UUID(self.host["id"])).binding, self.binding)

    def test_busy_waiting_end_and_rejected_rival_hooks(self):
        self.hook("UserPromptSubmit")
        self.refuse("native_busy")
        self.hook("PermissionRequest")
        self.refuse("native_waiting_user")
        self.hook("SessionEnd")
        self.refuse("hook_unavailable")
        self.hook("Stop")
        before = self.input.prepare(UUID(self.host["id"]))
        _, rival_shell, rival = self.chain(terminal_id=self.shell.terminal_id)
        self.assertEqual(self.handle(rival_shell, rival).outcome, "conflict")
        self.assertEqual(self.input.prepare(UUID(self.host["id"])), before)

    def test_latest_accepted_event_requires_exact_provenance_and_actor(self):
        from qorqut_core.storage.events import EventInput, append_event
        original = json.loads(self.events(self.host["id"], "native.hook")[-1]["payload_json"])
        for key, value in (("terminal_id", str(uuid4())), ("native_session_id", str(uuid4())),
                           ("run_generation", 9), ("provider", "codex"), ("cli_version", "unknown"),
                           ("environment_generation", 2), ("recovery_epoch", str(uuid4())),
                           ("native_process", self.native_process.model_copy(update={"start_ticks": 1}).model_dump(mode="json"))):
            with self.app.db.transaction() as tx:
                append_event(tx, EventInput(kind="native.hook", actor_id=self.host["actor_id"], agent_id=self.host["id"],
                    payload={**original, key: value}))
            self.refuse("hook_unavailable")
            self.hook("Stop")
        with self.app.db.transaction() as tx:
            append_event(tx, EventInput(kind="native.hook", actor_id=self.context.actor_id,
                agent_id=self.host["id"], payload=original))
        self.refuse("hook_unavailable")

    def test_capture_gate_requires_probe_saved_health_and_admitted_exact_primary_source(self):
        original = self.shell
        for change in ({"capture": ProbeResult(verified=False, terminal_active=False, reason="unavailable")},
                       {"saved_capture": None},
                       {"saved_capture": original.saved_capture.model_copy(update={"health": "incomplete"})}):
            self.shell = original.model_copy(update=change)
            self.refuse("capture_unavailable")
        self.shell = original
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE capture_epochs SET last_error='synthetic gap'")
        self.refuse("capture_unavailable")
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE capture_epochs SET last_error=NULL")
        self.app.native_collector._publish(self.host["id"], state="waiting", reason="waiting_source")
        self.raw_observation = replace(self.raw_observation, healthy=False)
        self.refuse("capture_unavailable")
        self.publish_capture()
        with self.app.db.transaction() as tx:
            tx.connection.execute("UPDATE archive_epochs SET conflicted=1")
        self.refuse("capture_unavailable")

    def test_human_control_and_layout_flags_geometry_and_draft_refuse(self):
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE runner_shell_control SET mode='human',viewer_id=?", (str(uuid4()),))
        self.refuse("human_control")
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE runner_shell_control SET mode='unclaimed',viewer_id=NULL,epoch=1")
        for changes in ({"input_off": True}, {"synchronized": True}, {"in_mode": True}, {"columns": 60},
                        {"input_off": None}):
            self.tmux.snapshot = self.empty.model_copy(update=changes)
            self.refuse("composer_unknown")
        self.tmux.snapshot = self.snapshot("human draft")
        self.refuse("composer_blocked")
        self.tmux.snapshot = self.empty
        self.assertEqual(self.input.prepare(UUID(self.host["id"])).control_epoch, 1)

    def test_unknown_owner_and_live_executable_or_managed_hook_change_refuse(self):
        with self.app.db.transaction() as tx:
            tx.connection.execute("UPDATE session_owners SET observation='unknown'")
        self.refuse("session_unavailable")
        with self.app.db.transaction() as tx:
            tx.connection.execute("UPDATE session_owners SET observation='live'")
        with patch("qorqut_core.runtime.input_dispatch._native_process", side_effect=ValueError("private process error")):
            self.refuse("session_unavailable")
        path = self.roots.workspace / ".claude/settings.local.json"
        contents = path.read_bytes()
        path.write_text('{"hooks": {}}')
        self.refuse("hook_unavailable")
        path.write_bytes(contents)
        self.app.settings = self.settings.model_copy(update={"listen_port": self.settings.listen_port + 1})
        self.refuse("hook_unavailable")
        self.app.settings = self.settings
        self.assertEqual(self.input.prepare(UUID(self.host["id"])).binding, self.binding)

    def test_submit_rechecks_idle_target_epoch_and_requires_exact_true(self):
        target = self.input.prepare(UUID(self.host["id"]))
        for false in (False, None, 1, "true"):
            with self.subTest(false=false), self.assertRaises(RunnerError):
                self.input.submit(target, uuid4(), uuid4(), still_current=lambda: false)
        self.assertEqual(self.tmux.effects, [])
        self.hook("Stop")
        with self.assertRaises(RunnerError):
            self.input.submit(target, uuid4(), uuid4(), still_current=lambda: True)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runner_attempts").fetchone()[0], 0)

    def test_readiness_loss_after_paste_and_after_enter_intent_is_uncertain(self):
        for stage in ("after_paste", "after_enter_intent"):
            target = self.input.prepare(UUID(self.host["id"]))
            old = TerminalRunner._update

            def change(db, aid, state, phase, reason):
                old(db, aid, state, phase, reason)
                if stage == "after_enter_intent" and phase == "enter":
                    self.app.native_collector._publish(self.host["id"], state="blocked", reason="collection_blocked")
                    self.raw_observation = replace(self.raw_observation, healthy=False)

            paste = self.tmux.paste
            if stage == "after_paste":
                def lost(binding, buffer):
                    paste(binding, buffer)
                    self.hook("UserPromptSubmit")
                self.tmux.paste = lost
            aid = uuid4()
            with patch.object(TerminalRunner, "_update", side_effect=change):
                receipt = self.input.submit(target, aid, uuid4(), still_current=lambda: True)
            self.assertEqual(receipt.state, "uncertain")
            self.assertEqual([item[0] for item in self.tmux.effects], ["paste"])
            self.assertEqual(self.input.inspect(target, aid), receipt)
            self.tmux.paste = paste
            self.tmux.effects.clear()
            self.tmux.snapshot = self.empty
            self.hook("Stop")
            self.publish_capture()

    def test_receipt_inspection_missing_or_replaced_store_never_creates_evidence(self):
        target = self.input.prepare(UUID(self.host["id"]))
        receipt = self.input.submit(target, uuid4(), uuid4(), still_current=lambda: True)
        self.assertEqual(self.input.inspect(target, receipt.attempt_id), receipt)
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE runner_bindings SET binding_digest=?", ("0" * 64,))
        db.close()
        with self.assertRaises(Exception):
            self.input.inspect(target, receipt.attempt_id)
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE runner_bindings SET binding_digest=?", (target.binding.digest,))
        db.close()
        backup = self.database.with_suffix(".original")
        self.database.rename(backup)
        with self.assertRaises(Exception):
            self.input.inspect(target, receipt.attempt_id)
        self.assertFalse(self.database.exists())
        with sqlite3.connect(self.database) as db:
            pass
        db.close()
        with self.assertRaises(Exception):
            self.input.inspect(target, receipt.attempt_id)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        db.close()


class MissingPrimary(InputFixture):
    def admit_source(self):
        pass

    def test_raw_health_alone_wrong_session_and_child_links_cannot_authorize_input(self):
        self.refuse("capture_unavailable")
        wrong = replace(self.native_source, source_id=str(uuid4()), native_session_id=str(uuid4()))
        self.app.archive.register(self.context, self.archive_binding, wrong, str(uuid4()),
                                  ArchiveMetadata(capability_evidence="synthetic wrong session"))
        child = replace(self.native_source, source_id=str(uuid4()), parent_source_id=wrong.source_id)
        self.app.archive.register(self.context, self.archive_binding, child, str(uuid4()),
                                  ArchiveMetadata(capability_evidence="synthetic child only"))
        self.refuse("capture_unavailable")
        InputFixture.admit_source(self)
        self.assertEqual(self.input.prepare(UUID(self.host["id"])).binding, self.binding)
