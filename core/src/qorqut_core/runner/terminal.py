"""Serialized local effects and receipts, with explicit recorder/adapter seams."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import UUID

from .models import (AttemptReceipt, ComposerVerdict, ControlReceipt, ControlState,
                     FrameReceipt, MAX_BODY_BYTES, RunBinding, RunnerError,
                     TerminalSnapshot, encode_body, validate_dimensions)
from .storage import connect, ensure_schema, input_lock
from .tmux import PasteBuffer, Tmux


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _counter(value: int) -> None:
    if type(value) is not int or value < 0:
        raise RunnerError("invalid_argument", "epoch/sequence must be a nonnegative integer")


class TerminalRunner:
    """A trusted local callable seam, not a lifecycle manager or network API.

    capture_ready(binding) must positively confirm recorder readiness.
    machine_hold(binding) must explicitly return False to admit input.
    classify_composer(binding, snapshot) returns empty/busy/blocked/unknown.
    verify_paste(binding, snapshot, sha256) must positively establish the exact
    owned paste is present and a normal Enter is safe; it may wait a bounded
    adapter-specific settle interval. No guessed composer or default readiness.

    Callables run under the per-run flock, outside SQLite transactions. They
    must not call back into this runner or write input. Machine-hold acquisition
    must set its higher-level hold first, then acquire this same gate before
    acknowledging quiescence. The runner never owns or clears that hold.
    """

    def __init__(
        self, database: str | Path, *, tmux: Tmux | None = None,
        capture_ready: Callable[[RunBinding], bool] | None = None,
        classify_composer: Callable[[RunBinding, TerminalSnapshot], ComposerVerdict] | None = None,
        verify_paste: Callable[[RunBinding, TerminalSnapshot, str], bool] | None = None,
        machine_hold: Callable[[RunBinding], bool] | None = None,
    ):
        self.database = Path(database).resolve()
        self.tmux = tmux or Tmux()
        self.capture_ready = capture_ready
        self.classify_composer = classify_composer
        self.verify_paste = verify_paste
        self.machine_hold = machine_hold
        with connect(self.database) as db:
            ensure_schema(db)

    def register_binding(self, binding: RunBinding) -> RunBinding:
        """Persist an exact trusted launch record; never discover/adopt by name.

        Future lifecycle assembly supplies recorder/native fingerprints from
        its own launch receipt. Repeating registration cannot reset control.
        """
        with input_lock(self.database, binding.run_id), connect(self.database) as db:
            row = db.execute("SELECT binding_json FROM runner_bindings WHERE run_id=?",
                             (str(binding.run_id),)).fetchone()
            if row:
                self._require_binding(db, binding)
                return binding
            self.tmux.inspect(binding)
            with db:
                db.execute("INSERT INTO runner_bindings VALUES (?,?,?)",
                           (str(binding.run_id), binding.digest, binding.model_dump_json()))
                db.execute("INSERT INTO runner_control VALUES (?,'automation',0,NULL,0)",
                           (str(binding.run_id),))
            return binding

    def inspect_binding(self, run_id: UUID) -> RunBinding | None:
        """Saved ownership only; live verification is explicit via inspect()."""
        with connect(self.database) as db:
            row = db.execute("SELECT binding_json FROM runner_bindings WHERE run_id=?",
                             (str(UUID(str(run_id))),)).fetchone()
            return RunBinding.model_validate_json(row[0]) if row else None

    @staticmethod
    def _require_binding(db: sqlite3.Connection, binding: RunBinding) -> None:
        row = db.execute("SELECT binding_digest FROM runner_bindings WHERE run_id=?",
                         (str(binding.run_id),)).fetchone()
        if not row or row[0] != binding.digest:
            raise RunnerError("target_changed", "no matching trusted launch binding")

    def inspect(self, binding: RunBinding) -> TerminalSnapshot:
        with input_lock(self.database, binding.run_id), connect(self.database) as db:
            self._require_binding(db, binding)
            return self.tmux.inspect(binding)

    @staticmethod
    def _control(db: sqlite3.Connection, binding: RunBinding) -> ControlState:
        row = db.execute("SELECT mode,epoch,viewer_id,last_sequence FROM runner_control WHERE run_id=?",
                         (str(binding.run_id),)).fetchone()
        return ControlState.model_validate(dict(row))

    def inspect_control(self, binding: RunBinding) -> ControlState:
        # Observation and viewer disconnect do not change ownership.
        with connect(self.database) as db:
            self._require_binding(db, binding)
            return self._control(db, binding)

    def _external_ready(self, binding: RunBinding) -> None:
        if self.capture_ready is None or self.capture_ready(binding) is not True:
            raise RunnerError("not_ready", "capture readiness is unconfirmed")
        if self.machine_hold is None:
            raise RunnerError("not_ready", "external machine hold is unconfirmed")
        if self.machine_hold(binding) is not False:
            raise RunnerError("machine_hold", "external machine hold blocks input")

    def _empty(self, binding: RunBinding) -> None:
        self._external_ready(binding)
        snapshot = self.tmux.inspect(binding)
        if snapshot.in_mode or snapshot.input_off or snapshot.synchronized:
            raise RunnerError("not_ready", "terminal is not exclusively editable")
        if self.classify_composer is None:
            raise RunnerError("not_ready", "composer classifier is absent")
        verdict = self.classify_composer(binding, snapshot)
        if verdict != "empty":
            reason = {"busy": "composer_busy", "blocked": "composer_blocked"}.get(verdict, "composer_unknown")
            raise RunnerError(reason, "composer is not verified empty")

    @staticmethod
    def _receipt(db: sqlite3.Connection, binding: RunBinding, attempt_id: UUID) -> AttemptReceipt:
        row = db.execute("SELECT * FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
        return AttemptReceipt(binding=binding, **{key: row[key] for key in
            ("attempt_id", "payload_sha256", "payload_bytes", "control_epoch", "state",
             "phase", "reason", "created_at", "updated_at")})

    @staticmethod
    def _update(db: sqlite3.Connection, attempt_id: UUID, state: str, phase: str, reason: str) -> None:
        with db:
            db.execute("UPDATE runner_attempts SET state=?,phase=?,reason=?,updated_at=? WHERE attempt_id=?",
                       (state, phase, reason, _now(), str(attempt_id)))

    def _cleanup(self, binding: RunBinding, buffer: PasteBuffer) -> None:
        try:
            self.tmux.delete_buffer(binding, buffer)
        except Exception:
            # Never delete on a replacement socket/server. Cleanup failure does
            # not make any input replayable. This unique name remains inspectable.
            pass

    def _recover(self, db: sqlite3.Connection, binding: RunBinding, row: sqlite3.Row) -> AttemptReceipt:
        if row["state"] in ("pending", "dispatching"):
            possible = row["state"] == "dispatching"
            self._update(db, UUID(row["attempt_id"]), "uncertain" if possible else "deferred",
                         row["phase"], "interrupted_after_intent" if possible else "interrupted_before_input")
        # Also covers a crash after the final receipt committed but before its
        # finally cleanup ran. This never types or touches a replacement server.
        self._cleanup(binding, PasteBuffer(row["buffer_name"], binding.digest, row["payload_sha256"]))
        return self._receipt(db, binding, UUID(row["attempt_id"]))

    def inspect_attempt(self, attempt_id: UUID) -> AttemptReceipt | None:
        attempt_id = UUID(str(attempt_id))
        with connect(self.database) as db:
            row = db.execute("SELECT run_id FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
            if not row:
                return None
            run_id = UUID(row[0])
        # Acquiring flock proves no compliant live dispatcher still owns this
        # unfinished receipt. No SQLite lease or wall-clock timeout infers death.
        with input_lock(self.database, run_id), connect(self.database) as db:
            binding = RunBinding.model_validate_json(db.execute(
                "SELECT binding_json FROM runner_bindings WHERE run_id=?", (str(run_id),)).fetchone()[0])
            row = db.execute("SELECT * FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
            return self._recover(db, binding, row)

    def attempt(self, binding: RunBinding, attempt_id: UUID, body: str, *, expected_epoch: int) -> AttemptReceipt:
        payload = encode_body(body)  # Limit/encoding validation precedes all terminal input.
        attempt_id = UUID(str(attempt_id))
        _counter(expected_epoch)
        digest = hashlib.sha256(payload).hexdigest()
        with input_lock(self.database, binding.run_id), connect(self.database) as db:
            old = db.execute("SELECT * FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
            if old:
                if (old["binding_digest"], old["payload_sha256"], old["control_epoch"]) != \
                        (binding.digest, digest, expected_epoch):
                    raise RunnerError("idempotency_conflict", "attempt ID was used for different input")
                return self._recover(db, binding, old)
            self._require_binding(db, binding)
            buffer = PasteBuffer.for_body(binding, payload)
            stamp = _now()
            try:
                with db:
                    db.execute("INSERT INTO runner_attempts VALUES (?,?,?,?,?,?,'pending','prepared','',?,?,?)",
                               (str(attempt_id), str(binding.run_id), binding.digest, digest, len(payload),
                                expected_epoch, buffer.name, stamp, stamp))
            except sqlite3.IntegrityError:
                # Different run locks can race on the globally unique attempt
                # ID. Only the successfully persisted binding can dispatch.
                if db.execute("SELECT 1 FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone():
                    raise RunnerError("idempotency_conflict", "attempt ID belongs to a different run") from None
                raise
            possible_input = False
            try:
                control = self._control(db, binding)
                if control.epoch != expected_epoch:
                    raise RunnerError("target_changed", "terminal control epoch changed")
                if control.mode != "automation":
                    raise RunnerError("human_control", "human owns terminal input")
                if self.verify_paste is None:
                    raise RunnerError("not_ready", "paste verification is absent")
                self._empty(binding)
                self.tmux.load_buffer(binding, buffer, payload)
                self._empty(binding)  # A buffer load/DB wait can outlive readiness.
                self._update(db, attempt_id, "dispatching", "paste", "paste_may_follow")
                self._empty(binding)  # Recheck after the durable intent's DB wait.
                possible_input = True
                self.tmux.paste(binding, buffer)
                self._external_ready(binding)
                snapshot = self.tmux.inspect(binding)
                if self.verify_paste(binding, snapshot, digest) is not True:
                    raise RunnerError("paste_unverified", "exact paste/normal Enter is unconfirmed")
                self._update(db, attempt_id, "dispatching", "enter", "enter_may_follow")
                self._external_ready(binding)
                self.tmux.enter(binding)
                self._update(db, attempt_id, "submitted", "finished", "processing_not_confirmed")
            except Exception as exc:
                reason = exc.code if isinstance(exc, RunnerError) else "boundary_failure"
                try:
                    phase = self._receipt(db, binding, attempt_id).phase
                    self._update(db, attempt_id, "uncertain" if possible_input else "deferred", phase, reason)
                except sqlite3.Error as storage_exc:
                    raise RunnerError("uncertain_effect" if possible_input else "storage_blocked",
                                      "receipt unavailable; inspect this attempt, never repeat input") from storage_exc
            finally:
                self._cleanup(binding, buffer)
            return self._receipt(db, binding, attempt_id)

    def control(self, binding: RunBinding, control_id: UUID, *, action: str,
                expected_epoch: int, viewer_id: UUID) -> ControlReceipt:
        control_id, viewer_id = UUID(str(control_id)), UUID(str(viewer_id))
        _counter(expected_epoch)
        if action not in ("take", "release"):
            raise RunnerError("invalid_argument", "action must be take or release")
        request_hash = hashlib.sha256(json.dumps(
            [binding.digest, action, expected_epoch, str(viewer_id)], separators=(",", ":")).encode()).hexdigest()
        with input_lock(self.database, binding.run_id), connect(self.database) as db:
            old = db.execute("SELECT request_hash,receipt_json FROM runner_control_receipts WHERE control_id=?",
                             (str(control_id),)).fetchone()
            if old:
                if old[0] != request_hash:
                    raise RunnerError("idempotency_conflict", "control ID was used for different input")
                return ControlReceipt.model_validate_json(old[1])
            self._require_binding(db, binding)
            current = self._control(db, binding)
            if current.epoch != expected_epoch:
                raise RunnerError("target_changed", "terminal control epoch changed")
            if action == "release" and (current.mode != "human" or current.viewer_id != viewer_id):
                raise RunnerError("target_changed", "release does not match the input viewer")
            try:
                self._external_ready(binding)
                self.tmux.inspect(binding)
                if action == "release":
                    self._empty(binding)
                updated = ControlState(mode="human" if action == "take" else "automation",
                                       epoch=current.epoch + 1, viewer_id=viewer_id if action == "take" else None,
                                       last_sequence=0)
                outcome, reason = "completed", ""
            except Exception as exc:
                updated = current
                outcome, reason = "deferred", exc.code if isinstance(exc, RunnerError) else "not_ready"
            receipt = ControlReceipt(control_id=control_id, binding=binding, action=action,
                                     state=updated, outcome=outcome, reason=reason)
            with db:
                db.execute("UPDATE runner_control SET mode=?,epoch=?,viewer_id=?,last_sequence=? WHERE run_id=?",
                           (updated.mode, updated.epoch, str(updated.viewer_id) if updated.viewer_id else None,
                            updated.last_sequence, str(binding.run_id)))
                db.execute("INSERT INTO runner_control_receipts VALUES (?,?,?,?)",
                           (str(control_id), str(binding.run_id), request_hash, receipt.model_dump_json()))
            return receipt

    def _frame(self, binding: RunBinding, *, epoch: int, viewer_id: UUID, sequence: int,
               effect: Callable[[], None]) -> FrameReceipt:
        _counter(epoch)
        _counter(sequence)
        viewer_id = UUID(str(viewer_id))
        with input_lock(self.database, binding.run_id), connect(self.database) as db:
            self._require_binding(db, binding)
            current = self._control(db, binding)
            if (current.mode != "human" or current.epoch != epoch or current.viewer_id != viewer_id
                    or sequence <= current.last_sequence):
                raise RunnerError("target_changed", "stale/observer input frame")
            self._external_ready(binding)
            self.tmux.inspect(binding)
            # Consume sequence before any possible effect; reconnect/restart may
            # inspect control but must never replay this ephemeral frame.
            with db:
                db.execute("UPDATE runner_control SET last_sequence=? WHERE run_id=?",
                           (sequence, str(binding.run_id)))
            try:
                effect()
                outcome = "submitted"
            except Exception:
                outcome = "uncertain"
            return FrameReceipt(epoch=epoch, viewer_id=viewer_id, sequence=sequence, outcome=outcome)

    def human_input(self, binding: RunBinding, data: bytes, *, epoch: int,
                    viewer_id: UUID, sequence: int) -> FrameReceipt:
        if not isinstance(data, bytes) or len(data) > MAX_BODY_BYTES:
            raise RunnerError("invalid_argument", "human frame must be at most 64 KiB bytes")
        buffer = PasteBuffer.for_body(binding, data)

        def effect():
            try:
                self.tmux.load_buffer(binding, buffer, data)
                self._external_ready(binding)
                self.tmux.paste(binding, buffer, bracketed=False)
            finally:
                self._cleanup(binding, buffer)

        return self._frame(binding, epoch=epoch, viewer_id=viewer_id, sequence=sequence, effect=effect)

    def resize(self, binding: RunBinding, columns: int, rows: int, *, epoch: int,
               viewer_id: UUID, sequence: int) -> FrameReceipt:
        validate_dimensions(columns, rows)
        return self._frame(binding, epoch=epoch, viewer_id=viewer_id, sequence=sequence,
                           effect=lambda: self.tmux.resize(binding, columns, rows))
