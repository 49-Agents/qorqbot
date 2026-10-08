"""Serialized local effects and receipts, with explicit recorder/adapter seams."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from uuid import UUID

from .models import (AttemptReceipt, ComposerVerdict, ControlReceipt, ControlState,
                     FrameReceipt, MAX_BODY_BYTES, RunBinding, RunnerError,
                     TerminalSnapshot, TmuxBinding, encode_body, validate_dimensions)
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
    Both are prompt observations, not settling or polling operations.
    classify_composer(binding, snapshot) returns empty/busy/blocked/unknown.
    Eligibility requires a supported native composer that interprets bracketed
    paste; explicit transport framing alone is NOT compatibility evidence.
    Unknown/unsupported native evidence must never yield empty/ready.
    verify_paste(binding, snapshot, sha256) must positively establish the exact
    owned paste is present and a normal Enter is safe. It is called after paste
    and again at the final tmux Enter boundary with a fresh snapshot AFTER the
    durable Enter intent. It must be a pure, prompt check of the supplied fresh
    snapshot; paste_settle_timeout bounds the runner's repaint polling before
    Enter intent, never the final proof. Capture/hold readiness is
    checked again after that final proof, with no intervening DB write or
    deliberate wait before Enter. No default readiness. These ordered checks
    cannot guarantee atomicity against asynchronous process death.

    Callables run under the recorded terminal's flock for mapped runs (the run
    flock for standalone bindings), outside SQLite transactions. They may read
    original state but must not reacquire this gate, including via ShellManager
    public methods, or write input. Machine-hold acquisition
    must set its higher-level hold first, then acquire this same gate before
    acknowledging quiescence. The runner never owns or clears that hold.
    """

    def __init__(
        self, database: str | Path, *, tmux: Tmux | None = None,
        capture_ready: Callable[[RunBinding], bool] | None = None,
        classify_composer: Callable[[RunBinding, TerminalSnapshot], ComposerVerdict] | None = None,
        verify_paste: Callable[[RunBinding, TerminalSnapshot, str], bool] | None = None,
        machine_hold: Callable[[RunBinding], bool] | None = None,
        paste_settle_timeout: float = 0.0,
        existing: bool = False,
    ):
        if (type(paste_settle_timeout) not in (int, float)
                or not 0 <= paste_settle_timeout <= 1 or not math.isfinite(paste_settle_timeout)):
            raise RunnerError("invalid_argument", "paste_settle_timeout must be finite and within 0..1 seconds")
        self.database = Path(database).resolve()
        self.tmux = tmux or Tmux()
        self.capture_ready = capture_ready
        self.classify_composer = classify_composer
        self.verify_paste = verify_paste
        self.machine_hold = machine_hold
        self.paste_settle_timeout = float(paste_settle_timeout)
        with connect(self.database, create=not existing) as db:
            if not existing:
                ensure_schema(db)

    def _connect(self):
        # After construction a removed receipt store is never recreated.
        return connect(self.database, create=False)

    @staticmethod
    def _saved_binding(db: sqlite3.Connection, run_id: UUID) -> sqlite3.Row | None:
        # One snapshot distinguishes an absent registration from a committed
        # standalone binding, which can never subsequently acquire a mapping.
        return db.execute("SELECT b.*,t.terminal_id,t.spec_hash FROM runner_bindings b "
                          "LEFT JOIN runner_binding_terminals t USING(run_id) WHERE b.run_id=?",
                          (str(run_id),)).fetchone()

    @staticmethod
    def _mapping(row: sqlite3.Row | None) -> tuple[str, str] | None:
        return (row["terminal_id"], row["spec_hash"]) if row and row["terminal_id"] else None

    @staticmethod
    def _require_terminal(db: sqlite3.Connection, binding: RunBinding, terminal_id: UUID,
                          *, spec_hash: str | None = None, running: bool = False) -> str:
        # Keep runner import independent of probe -> runner -> shell model
        # initialization. Standalone callers need no recorded-shell schema.
        from .shell_models import ShellSpec

        if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN "
                      "('runner_shell_terminals','runner_shell_control')").fetchone()[0] != 2:
            raise RunnerError("not_found", "recorded shell storage does not exist")
        row = db.execute("SELECT * FROM runner_shell_terminals WHERE terminal_id=?",
                         (str(terminal_id),)).fetchone()
        if row is None:
            raise RunnerError("not_found", "recorded terminal does not exist")
        try:
            spec = ShellSpec.model_validate_json(row["spec_json"])
            target = TmuxBinding.model_validate_json(row["tmux_json"])
        except ValueError as exc:
            raise RunnerError("target_changed", "recorded terminal binding is incomplete or invalid") from exc
        digest = hashlib.sha256(spec.model_dump_json().encode()).hexdigest()
        if (row["spec_hash"] != digest or (spec_hash is not None and spec_hash != digest)
                or row["agent_id"] != str(spec.agent_id)
                or row["installation_id"] != str(spec.installation_id)
                or (spec.agent_id, spec.recovery_epoch, spec.environment_generation) !=
                   (binding.agent_id, binding.recovery_epoch, binding.environment_generation)
                or row["boot_id"] != str(binding.boot_id)
                or row["socket_path"] != binding.tmux.socket_path
                or target != binding.tmux or target.pane != binding.recorder):
            raise RunnerError("target_changed", "run does not match the saved recorded terminal")
        if db.execute("SELECT 1 FROM runner_control WHERE run_id=?", (str(binding.run_id),)).fetchone():
            raise RunnerError("target_changed", "a standalone control record cannot become mapped")
        if running and row["stopped"]:
            raise RunnerError("not_ready", "recorded terminal is stopped")
        return digest

    @contextmanager
    def _gate(self, run_id: UUID, *, registering: bool = False, terminal_id: UUID | None = None,
              observation: bool = False):
        while True:
            with self._connect() as db:
                before = self._saved_binding(db, run_id)
                selected = self._mapping(before)
                if observation and selected is None:
                    # Preserve legacy nonblocking saved-state reads. Absence
                    # is returned from THIS snapshot, never looked up again.
                    yield db, before
                    return
            gate_id = UUID(selected[0]) if selected else run_id
            if registering and before is None and terminal_id is not None:
                gate_id = terminal_id
            with input_lock(self.database, gate_id), self._connect() as db:
                current = self._saved_binding(db, run_id)
                mapping = self._mapping(current)
                if mapping != selected:
                    if selected is not None:
                        raise RunnerError("target_changed", "saved terminal mapping changed")
                    # Initial mapped registration won before we acquired the
                    # old run gate. Release it and acquire the terminal gate.
                    continue
                if current is None and not registering:
                    # A registration on a different gate may still be in flight.
                    # Never yield an absent binding and then reread it as ready.
                    raise RunnerError("target_changed", "no matching trusted launch binding")
                if mapping:
                    try:
                        saved = RunBinding.model_validate_json(current["binding_json"])
                    except ValueError as exc:
                        raise RunnerError("target_changed", "saved run binding is invalid") from exc
                    if saved.digest != current["binding_digest"]:
                        raise RunnerError("target_changed", "saved run binding is inconsistent")
                    self._require_terminal(db, saved, UUID(mapping[0]), spec_hash=mapping[1])
                yield db, current
                return

    def register_binding(self, binding: RunBinding, *, terminal_id: UUID | None = None) -> RunBinding:
        """Persist an exact trusted launch record; never discover/adopt by name.

        Core supplies verified native ownership/ancestry. Binding to a saved
        shell is not capture/composer readiness. Replays cannot reset control
        or convert standalone registrations into mapped registrations.
        """
        terminal_id = UUID(str(terminal_id)) if terminal_id is not None else None
        with self._gate(binding.run_id, registering=True, terminal_id=terminal_id) as (db, row):
            def validate(current):
                if current:
                    self._require_binding(db, binding)
                    if current["terminal_id"] != (str(terminal_id) if terminal_id is not None else None):
                        raise RunnerError("target_changed", "registration terminal mapping differs")
                if terminal_id is not None:
                    digest = self._require_terminal(db, binding, terminal_id, running=True,
                                                   spec_hash=current["spec_hash"] if current else None)
                    self._control(db, binding, terminal_id=terminal_id)
                    return digest

            spec_hash = validate(row)
            if row:
                return binding
            self.tmux.inspect(binding)
            with db:
                # Competing first registrations may select different gates.
                # Recheck under the write transaction before inserting either
                # row, with all tmux/callback work outside the transaction.
                db.execute("BEGIN IMMEDIATE")
                row = self._saved_binding(db, binding.run_id)
                validate(row)
                if row:
                    return binding
                db.execute("INSERT INTO runner_bindings VALUES (?,?,?)",
                           (str(binding.run_id), binding.digest, binding.model_dump_json()))
                if terminal_id is None:
                    db.execute("INSERT INTO runner_control VALUES (?,'automation',0,NULL,0)",
                               (str(binding.run_id),))
                else:
                    db.execute("INSERT INTO runner_binding_terminals VALUES (?,?,?)",
                               (str(binding.run_id), str(terminal_id), spec_hash))
            return binding

    def inspect_binding(self, run_id: UUID) -> RunBinding | None:
        """Saved ownership only; live verification is explicit via inspect()."""
        with self._gate(UUID(str(run_id)), observation=True) as (_, row):
            return RunBinding.model_validate_json(row["binding_json"]) if row else None

    @staticmethod
    def _require_binding(db: sqlite3.Connection, binding: RunBinding) -> None:
        row = db.execute("SELECT binding_digest FROM runner_bindings WHERE run_id=?",
                         (str(binding.run_id),)).fetchone()
        if not row or row[0] != binding.digest:
            raise RunnerError("target_changed", "no matching trusted launch binding")

    def inspect(self, binding: RunBinding) -> TerminalSnapshot:
        with self._gate(binding.run_id) as (db, _):
            self._require_binding(db, binding)
            return self.tmux.inspect(binding)

    @staticmethod
    def _control(db: sqlite3.Connection, binding: RunBinding, *, terminal_id: UUID | None = None) -> ControlState:
        if terminal_id is None:
            mapping = db.execute("SELECT terminal_id FROM runner_binding_terminals WHERE run_id=?",
                                 (str(binding.run_id),)).fetchone()
            terminal_id = UUID(mapping[0]) if mapping else None
        if terminal_id is None:
            row = db.execute("SELECT mode,epoch,viewer_id,last_sequence FROM runner_control WHERE run_id=?",
                             (str(binding.run_id),)).fetchone()
        else:
            row = db.execute("SELECT mode,epoch,viewer_id,last_sequence FROM runner_shell_control WHERE terminal_id=?",
                             (str(terminal_id),)).fetchone()
        if row is None:
            raise RunnerError("target_changed", "saved terminal control is missing")
        values = dict(row)
        if terminal_id is not None and values["mode"] == "unclaimed":
            # Internal eligibility only; all positive readiness is still required.
            values["mode"] = "automation"
        try:
            return ControlState.model_validate(values)
        except ValueError as exc:
            raise RunnerError("target_changed", "saved terminal control is invalid") from exc

    def inspect_control(self, binding: RunBinding) -> ControlState:
        # Observation and viewer disconnect do not change ownership.
        with self._gate(binding.run_id, observation=True) as (db, row):
            if row is None:
                raise RunnerError("target_changed", "no matching trusted launch binding")
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

    def _settle_paste(self, binding: RunBinding, digest: str) -> None:
        deadline = time.monotonic() + self.paste_settle_timeout
        while True:
            self._external_ready(binding)
            snapshot = self.tmux.inspect(binding)
            if self.verify_paste is None:
                raise RunnerError("paste_unverified", "paste verification is absent")
            verified = self.verify_paste(binding, snapshot, digest) is True
            remaining = deadline - time.monotonic()
            if verified and (self.paste_settle_timeout == 0 or remaining >= 0):
                return
            if remaining <= 0:
                raise RunnerError("paste_unverified", "exact paste/normal Enter is unconfirmed")
            time.sleep(min(0.025, remaining))

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
        self._cleanup(binding, PasteBuffer(row["buffer_name"], binding.digest))
        return self._receipt(db, binding, UUID(row["attempt_id"]))

    def inspect_attempt(self, attempt_id: UUID) -> AttemptReceipt | None:
        attempt_id = UUID(str(attempt_id))
        with self._connect() as db:
            row = db.execute("SELECT run_id FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
            if not row:
                return None
            run_id = UUID(row[0])
        # Acquiring flock proves no compliant live dispatcher still owns this
        # unfinished receipt. No SQLite lease or wall-clock timeout infers death.
        with self._gate(run_id) as (db, _):
            binding = RunBinding.model_validate_json(db.execute(
                "SELECT binding_json FROM runner_bindings WHERE run_id=?", (str(run_id),)).fetchone()[0])
            row = db.execute("SELECT * FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
            return self._recover(db, binding, row)

    def attempt(self, binding: RunBinding, attempt_id: UUID, body: str, *, expected_epoch: int) -> AttemptReceipt:
        payload = encode_body(body)  # Limit/encoding validation precedes all terminal input.
        # D24: transport-owned delimiters are not user text, its hash or size.
        # Raw tmux paste must not add conditional framing based on pane mode.
        wire = b"\x1b[200~" + payload + b"\x1b[201~"
        attempt_id = UUID(str(attempt_id))
        _counter(expected_epoch)
        digest = hashlib.sha256(payload).hexdigest()
        with self._gate(binding.run_id) as (db, row):
            old = db.execute("SELECT * FROM runner_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
            if old:
                if (old["binding_digest"], old["payload_sha256"], old["control_epoch"]) != \
                        (binding.digest, digest, expected_epoch):
                    raise RunnerError("idempotency_conflict", "attempt ID was used for different input")
                return self._recover(db, binding, old)
            self._require_binding(db, binding)
            buffer = PasteBuffer.for_bytes(binding, wire)
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
                if row["terminal_id"] is not None:
                    self._require_terminal(db, binding, UUID(row["terminal_id"]),
                                           spec_hash=row["spec_hash"], running=True)
                control = self._control(db, binding)
                if control.epoch != expected_epoch:
                    raise RunnerError("target_changed", "terminal control epoch changed")
                if control.mode != "automation":
                    raise RunnerError("human_control", "human owns terminal input")
                if self.verify_paste is None:
                    raise RunnerError("not_ready", "paste verification is absent")
                self._empty(binding)
                self.tmux.load_buffer(binding, buffer, wire)
                self._empty(binding)  # A buffer load/DB wait can outlive readiness.
                self._update(db, attempt_id, "dispatching", "paste", "paste_may_follow")
                self._empty(binding)  # Recheck after the durable intent's DB wait.
                possible_input = True
                self.tmux.paste(binding, buffer)
                self._settle_paste(binding, digest)
                self._update(db, attempt_id, "dispatching", "enter", "enter_may_follow")
                self._external_ready(binding)
                # The earlier proof cannot survive the SQLite wait above.
                # Let the final tmux boundary supply and use its fresh screen.
                def verified_and_ready(current: TerminalSnapshot) -> bool:
                    if self.verify_paste is None or self.verify_paste(binding, current, digest) is not True:
                        return False
                    # Refresh external signals after the final proof, without
                    # reusing settlement's earlier snapshot or waiting again.
                    self._external_ready(binding)
                    return True

                self.tmux.enter(binding, verify_paste=verified_and_ready)
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
        with self._gate(binding.run_id) as (db, row):
            if row["terminal_id"] is not None:
                raise RunnerError("unsupported_capability", "mapped human control belongs to ShellManager")
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
        with self._gate(binding.run_id) as (db, row):
            if row["terminal_id"] is not None:
                raise RunnerError("unsupported_capability", "mapped human input and resize belong to ShellManager")
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
        buffer = PasteBuffer.for_bytes(binding, data)

        def effect():
            try:
                self.tmux.load_buffer(binding, buffer, data)
                self._external_ready(binding)
                self.tmux.paste(binding, buffer)
            finally:
                self._cleanup(binding, buffer)

        return self._frame(binding, epoch=epoch, viewer_id=viewer_id, sequence=sequence, effect=effect)

    def resize(self, binding: RunBinding, columns: int, rows: int, *, epoch: int,
               viewer_id: UUID, sequence: int) -> FrameReceipt:
        validate_dimensions(columns, rows)
        return self._frame(binding, epoch=epoch, viewer_id=viewer_id, sequence=sequence,
                           effect=lambda: self.tmux.resize(binding, columns, rows))
