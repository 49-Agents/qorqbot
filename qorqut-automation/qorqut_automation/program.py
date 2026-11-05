"""The one durable program, ``automation.run``: run the frozen admission's script against the host bridge, wait, continue or finish.

Lifecycle of one logical run (one ``jobs`` row):

* queue time: ``admit`` (the atomic one-unresolved-run guard shared by manual and scheduled paths) also *freezes* the script revisions,
  input, phases and limits this run will use on the automation row, in the job's own transaction;
* first execution: exactly those revisions are staged once into an immutable snapshot (a changed script is refused, never silently
  replaced), digest-verified on every later start and recorded under the job's reserved ``automation.snapshot``. Staging copies files
  and holds no database transaction;
* each execution (an *invocation*): a child process runs the staged code with one inherited socket; the bridge turns its requests into
  durable steps. An invocation is bounded by ``invocation_seconds`` (default 60) and the run by ``max_run_seconds`` of active time;
  longer work continues explicitly through ``run.continue_run(progress)`` with saved JSON;
* a step that needs a decision or outcome records a wait and the child exits; the run returns ``ProgramPending`` and holds **no** process
  slot. ``poll`` reconciles the wait (no child, no model turn) and the engine then re-runs the script, which replays finished steps from
  the ledger and continues;
* the bridge thread is always settled before the invocation ends. A thread still inside an engine call or provider dispatch is never
  abandoned and never reported as finished: the invocation - and with it the engine's per-job claim, compute slot and lane accounting
  - stays alive, holding no database transaction or writer admission, until that call has actually returned (after a normal child
  exit, a timeout or a cancellation alike). Nothing is returned early, so no successor, cancellation finalization or other run can
  start while an effect is unresolved; a stop that cannot quiesce is reported by the engine's own bounded stop/join, never claimed;
* a requested stop (``cancel_requested``) is not made terminal while an effect of the run is unresolved: the engine's lane asks ``cancel``,
  which reconciles the run's steps against their canonical Signoff, job and receipt records (``cancellation``) without a child, a compute
  slot or any new call; while one survives the run stays ``cancel_requested`` and is asked again, so its automation admits no successor;
* a failed run notifies Ordubashy once and pauses its automation (and schedule); the job then fails normally.

Snapshot and retention. The exact admitted script version of a run is protected by one *retained copy* per run (``state/retained/<run>``),
published whole and never modified, and by the run's own snapshot (``state/snapshots/<run>``), built from the retained copy (or, when none
was needed, from the catalog) in a unique temporary directory and renamed into place; the retained copy is *copied*, never consumed, so a
crash before the snapshot is recorded on the job rebuilds the very same version even if the authored script changed since. Retaining and
staging of one run are serialized by an installation-scoped application claim keyed on that run (not the engine's per-job claim, which is
already held while staging), taken in short non-blocking attempts and held only for file work. Retained copies and snapshots are recovery
artifacts and are never pruned automatically; only closed ordinary debug logs expire (``maintenance``).
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import threading
import time
from uuid import UUID, uuid4, uuid5

from qorqut_agentic.errors import AgenticError, RevisionConflictError
from qorqut_agentic.runtime import ExecutionRequest

from qorqut_operations.background.programs import Program, ProgramPending, ProgramResult
from qorqut_operations.contracts import DomainError, canonical_hash, plain
from qorqut_operations.storage.executor import application_claim

from . import cancellation, steps, waits
from .bridge import RunBridge
from .failures import record_run_failure
from .guard import automation_row
from .records import PROGRAM

POLL_SECONDS = 30
CONTINUE_SECONDS = 1
CANCEL_SECONDS = 30          # how soon a cancelled run that still has an unresolved effect is reconciled again
MAX_STDERR_TAIL = 2000
RETAINED = "retained"
DESCRIPTOR = "retained.json"
CLAIM_WAIT_SECONDS = 2.0     # a snapshot/retention claim is tried in short non-blocking attempts for this long, then reported busy
CLAIM_POLL_SECONDS = 0.05
SDK_NAME = "qorqut_automation_sdk.py"
STOPS = frozenset({"cancelled", "timeout", "service_stopping", "maintenance", "busy"})
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["automation_id"],
          "properties": {"automation_id": {"type": "string", "format": "uuid"}, "trigger": {"enum": ["manual", "schedule"]}}}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_durable(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    """Best effort: make a rename in ``path`` durable."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class AutomationProgram:
    def __init__(self, extension):
        self.ext = extension
        self._installation = None

    # -- engine interfaces ---------------------------------------------------------------------------------------
    def admit(self, context, arguments, uow):
        self.ext.guard.admit(context, arguments, uow)

    def poll(self, control, arguments):
        """Reconcile this run's saved waits without a child. ``ProgramPending`` still waiting; ``None`` ready to run."""
        ext, run_id = self.ext, control.job["id"]
        control.check()
        still = self._reconcile(control, run_id)
        pending = bool(still)
        with ext.runtime.database.read() as uow:
            automation = automation_row(uow, arguments["automation_id"])
            unresolved = bool(steps.unresolved_for_run(uow, run_id, 1))
        if automation is None or automation["state"] != "active":
            if pending or unresolved:
                # An effect whose outcome is not known keeps the run (and its guard) until it is reconciled or a person cancels it.
                return ProgramPending(retry_after=POLL_SECONDS)
            raise DomainError("cancelled", "The automation was paused or archived while this run waited")
        return ProgramPending(retry_after=POLL_SECONDS) if pending else None

    def cancel(self, control, arguments):
        """Settle a requested stop's unresolved effects (``Program.cancel``). It only reads and reconciles the canonical records of this
        run's steps - never a child, a compute slot, a new call or a send, and never the cancellation check that would end it at once.
        ``ProgramPending`` while any external step is still unresolved (the run stays ``cancel_requested`` and so blocks a successor);
        ``None`` once none is, when the engine completes the normal cancellation."""
        control.check(cancellation=False)
        remaining = cancellation.settle_run(self.ext, control.job["id"], check=lambda: control.check(cancellation=False))
        return ProgramPending(retry_after=CANCEL_SECONDS) if remaining else None

    def _reconcile(self, control, run_id):
        """Settle every waiting step of the run against its canonical record, then make the saved ``automation.wait`` checkpoint say
        exactly what the run still waits for (nothing, once every wait is settled). Returns the steps that are still waiting."""
        ext = self.ext
        with ext.runtime.database.read() as uow:
            waiting = [row["id"] for row in steps.waiting_for_run(uow, run_id)]
        for step_id in waiting:
            control.check()
            waits.settle_waiting(ext, step_id)
        with ext.runtime.database.read() as uow:
            still = steps.waiting_for_run(uow, run_id)
        saved = dict(control.saved("automation") or {})
        current = None
        if still:
            first = still[0]
            current = {"step_key": first["step_key"], "step_id": first["id"], "kind": (first["wait_json"] or {}).get("kind")}
        if saved and saved.get("wait") != current:
            control.checkpoint("automation", {**saved, "wait": current})
        return still

    def run(self, control, arguments):
        return self._execute(control, arguments)

    def recover(self, control, arguments):
        return self._execute(control, arguments)

    # -- execution -----------------------------------------------------------------------------------------------
    def _execute(self, control, arguments):
        ext, database = self.ext, self.ext.runtime.database
        run_id = control.job["id"]
        self._never_overlap(arguments["automation_id"], run_id)
        control.check()
        with database.read() as uow:
            automation = automation_row(uow, arguments["automation_id"])
        if automation is None or automation["state"] != "active":
            raise DomainError("cancelled", "The automation is paused or archived")
        state = dict(control.saved("automation") or {})
        try:
            if "snapshot" not in state:
                state["snapshot"] = self._stage(control, automation, state)
                control.checkpoint("automation", state)
            root = self._verify(state["snapshot"])
        except DomainError as exc:
            if exc.code in STOPS:
                raise
            return self._fail(control, automation, exc.code if exc.code in {"script_changed", "admission_missing"} else "snapshot_unavailable",
                              str(exc))
        if self._reconcile(control, run_id):
            return ProgramPending(retry_after=POLL_SECONDS)
        state = dict(control.saved("automation") or state)
        snapshot = state["snapshot"]
        limits = self._limits(snapshot)
        active = float(state.get("active_seconds", 0))
        if active >= limits["max_run_seconds"]:
            return self._fail(control, automation, "run_timeout", "The run used its active-time limit", {"active_seconds": active})
        state["wait"] = None        # nothing is pending: the lane must not poll a stale wait
        state["invocations"] = int(state.get("invocations", 0)) + 1
        bridge = RunBridge(ext, control, automation["id"], state, limits=limits)
        abort, errors = threading.Event(), []
        parent, child = socket.socketpair()
        work = root / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(mode=0o700)

        def serve():
            try:
                bridge.serve(parent)
            except BaseException as exc:  # noqa: BLE001 - recorded, then the child is aborted and the run settled below
                errors.append(exc)
                abort.set()
            finally:
                parent.close()

        thread = threading.Thread(target=serve, name="automation-bridge", daemon=True)
        timeout = min(control.remaining_seconds(), limits["invocation_seconds"], limits["max_run_seconds"] - active)
        started, result, spawn_error = time.monotonic(), None, None
        live_key = automation["id"]
        try:
            ext.live[live_key] = (run_id, thread, bridge)
            thread.start()
            result = ext.process_runtime(snapshot["runtime"]).execute(ExecutionRequest(
                invocation_id=f"{run_id}:{state['invocations']}", runtime_alias=snapshot["runtime"],
                code_path=str(root / "package" / snapshot["code"]), package_dir=str(root / "package"), work_dir=str(work),
                stdin=b"{}", timeout_seconds=timeout, max_output_bytes=limits["max_output_bytes"],
                max_stderr_bytes=limits["max_stderr_bytes"], pass_fds=(child.fileno(),),
                extra_env={"QORQUT_AUTOMATION_FD": str(child.fileno()), "PYTHONPATH": str(root / "sdk"),
                           "PYTHONDONTWRITEBYTECODE": "1"}, cancel=abort))
        except BaseException as exc:  # noqa: BLE001 - settle the bridge below, then report
            spawn_error = exc
        finally:
            bridge.stop.set()
            child.close()
            self._settle(live_key, thread)
            parent.close()
        elapsed = time.monotonic() - started
        if spawn_error is not None:
            if isinstance(spawn_error, DomainError) and spawn_error.code in STOPS:
                raise spawn_error
            if isinstance(spawn_error, (KeyboardInterrupt, SystemExit)):
                raise spawn_error
            return self._fail(control, automation, "script_unavailable", f"{type(spawn_error).__name__}: {spawn_error}",
                              step_id=self._step_of(bridge))
        state["active_seconds"] = round(active + elapsed, 3)
        if errors:
            error = errors[0]
            if isinstance(error, DomainError):
                if error.code in STOPS:
                    control.checkpoint("automation", state)
                    raise error
                return self._fail(control, automation, error.code, str(error), step_id=self._step_of(bridge))
            return self._fail(control, automation, "bridge_failed", f"{type(error).__name__}: {error}", step_id=self._step_of(bridge))
        if bridge.finished is not None:
            return ProgramResult(data={"result": bridge.finished["result"], "calls": bridge.calls,
                                       "code_revision": snapshot["code_revision"], "invocations": state["invocations"]})
        control.checkpoint("automation", {**state, "calls": bridge.calls})
        if bridge.suspended:
            return ProgramPending(retry_after=POLL_SECONDS)
        if bridge.continued:
            return ProgramPending(retry_after=CONTINUE_SECONDS)
        if result is not None and result.outcome == "cancelled":
            raise DomainError("cancelled", "The run was stopped")
        tail = result.stderr.decode("utf-8", "replace")[-MAX_STDERR_TAIL:] if result is not None else ""
        if result.outcome == "timeout":
            if state["active_seconds"] >= limits["max_run_seconds"] - 1:
                return self._fail(control, automation, "run_timeout", "The run used its active-time limit",
                                  {"stderr": tail, "active_seconds": state["active_seconds"]})
            return self._fail(control, automation, "invocation_timeout",
                              f"The script ran longer than its {int(timeout)}-second invocation limit without saving progress and "
                              "continuing (run.continue_run) or finishing", {"stderr": tail, "invocation_seconds": limits["invocation_seconds"]})
        if result.outcome != "exited":
            return self._fail(control, automation, "script_unavailable",
                              result.message or f"The script could not run ({result.outcome})", {"stderr": tail})
        failed = bridge.last_error
        code = "retries_exhausted" if failed is not None and failed.exhausted else "script_failed"
        message = bridge.failure or f"The script exited with status {result.exit_code} without a result"
        return self._fail(control, automation, code, message,
                          {"exit_code": result.exit_code, "stderr": tail, **({"step_error": failed.code} if failed else {})},
                          step_id=failed.step_id if failed is not None else None)

    @staticmethod
    def _step_of(bridge):
        return bridge.last_error.step_id if bridge.last_error is not None else None

    def _limits(self, snapshot):
        """The effective bounds of one run: configuration, the admitted restrictions and the tool's own declared limits."""
        config, run = self.ext.config, snapshot.get("run_limits") or {}
        tool = snapshot.get("limits") or {}
        invocation = min(config.invocation_seconds, float(tool.get("timeout_seconds") or config.invocation_seconds))
        return {"max_calls": min(run.get("max_calls", config.max_calls), config.max_calls), "max_step_bytes": config.max_step_bytes,
                "max_run_seconds": min(run.get("max_run_seconds", config.max_run_seconds), config.max_run_seconds),
                "invocation_seconds": invocation,
                "max_output_bytes": int(tool.get("max_output_bytes") or 1024 * 1024),
                "max_stderr_bytes": int(tool.get("max_stderr_bytes") if tool.get("max_stderr_bytes") is not None else 64 * 1024)}

    def _never_overlap(self, automation_id, run_id):
        """Defence in depth only: an invocation never returns while its bridge thread is alive, so a live entry for this automation
        (any run, found by the automation's id rather than the run's) means a second invocation was offered while an earlier step
        is still in flight. It is refused, never run beside the unresolved one."""
        entry = self.ext.live.get(automation_id)
        if entry is None:
            return
        _owner, thread, _bridge = entry
        thread.join(timeout=0)
        if thread.is_alive():
            raise DomainError("busy", "A step of this automation is still being dispatched", retryable=True)
        self.ext.live.pop(automation_id, None)

    def _settle(self, automation_id, thread):
        """Wait, without a cap and holding no transaction or writer admission, until the bridge thread has actually returned from
        its engine call or provider dispatch, then forget it. The engine's claims and lane accounting stay with this invocation
        meanwhile; the engine's own bounded stop/join is the only boundary and reports a failure to quiesce."""
        if thread.ident is not None:
            thread.join()
        self.ext.live.pop(automation_id, None)

    def _fail(self, control, automation, code, message, details=None, *, step_id=None):
        """Notify Ordubashy (once per failure identity) and pause this automation and its schedule in one short writer-admitted
        transaction, then fail the job. A responder outage does not roll back the saved failure: reconciliation delivers it later."""
        details = {**(details or {}), **({"step_id": step_id} if step_id else {})}
        with self.ext.writing() as uow:
            fresh = uow.connection.execute("SELECT * FROM automations WHERE id=?", (automation["id"],)).fetchone()
            record_run_failure(self.ext.runtime, uow, fresh, control.job["id"], code, message, details=details, step_id=step_id)
        raise DomainError(code, message, retryable=False, details=details)

    # -- the immutable snapshot ----------------------------------------------------------------------------------
    @contextmanager
    def _snapshot_claim(self, run_id):
        """The installation-scoped application claim on one run's snapshot and retention: a stable key of its own, never the engine's
        per-job execution claim (already held while a run is staged). Taken in short non-blocking attempts for a bounded time (``busy``
        after that) and held for file work only - never across a wait for the SQL writer."""
        ext = self.ext
        if self._installation is None:
            with ext.runtime.database.read() as uow:
                self._installation = UUID(ext.runtime.authority.installation(uow))
        identity = str(uuid5(self._installation, "automation-snapshot:" + run_id))
        deadline = time.monotonic() + CLAIM_WAIT_SECONDS
        with ExitStack() as stack:
            while True:
                try:
                    stack.enter_context(application_claim(ext.runtime.locks, identity))
                    break
                except DomainError as exc:
                    if exc.code != "busy" or time.monotonic() >= deadline:
                        raise
                    time.sleep(CLAIM_POLL_SECONDS)
            yield

    def _retained(self, run_id) -> Path:
        """Where the retained copy of a run that has not recorded its own snapshot is kept: one directory holding the staged package and
        its descriptor, published whole by a single rename."""
        return self.ext.state_root / RETAINED / run_id

    def _kept(self, run_id, admitted, *, verify):
        """``(directory, descriptor)`` of the run's retained copy when it is exactly the admitted revisions (with ``verify``, also when
        every file still has its recorded digest), else ``None`` - never a copy of any other version. A retained copy is immutable, so
        it is either absent or complete."""
        directory = self._retained(run_id)
        try:
            descriptor = json.loads((directory / DESCRIPTOR).read_text(encoding="utf-8"))
            if (not isinstance(descriptor, dict) or descriptor.get("definition_revision") != admitted.get("definition_revision")
                    or descriptor.get("code_revision") != admitted.get("code_revision")):
                return None
            if verify:
                package = directory / "package"
                if not (package / descriptor["code"]).is_file():
                    return None
                for item in descriptor["files"]:
                    path = package / item["path"]
                    if not path.is_file() or _sha(path) != item["sha256"]:
                        return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return directory, descriptor

    def retained_ready(self, run_id, admitted) -> bool:
        """Whether the run's retained copy exists and is exactly the version the run was admitted with (a descriptor check only: this is
        asked inside an edit's write transaction, so it never reads the copied files)."""
        return self._kept(run_id, plain(admitted) or {}, verify=False) is not None

    def retain(self, automation_id):
        """Keep the exact script version a queued run was admitted with, so a later authored edit changes future runs only. The admitted
        revisions are copied through the existing Agentic snapshot staging into a unique temporary directory under the state directory,
        outside every database transaction, and published whole by one rename under the run's snapshot claim. ``None`` when there is
        nothing to keep (no such run, it already recorded its own snapshot, the catalog no longer holds the admitted revisions - then the
        run is refused at start rather than started with other code - or another writer holds the claim: the caller asks again)."""
        ext = self.ext
        layer = ext.runtime.agentic_layer
        with ext.runtime.database.read() as uow:
            automation = automation_row(uow, automation_id)
            runs = ext.guard.unstaged_runs(uow, automation_id, 1)
        admitted = plain(automation["admitted_json"]) if automation is not None else {}
        if layer is None or not runs or not admitted or not admitted.get("script_ref") or not admitted.get("code_revision"):
            return None
        run_id = runs[0]["id"]
        try:
            with self._snapshot_claim(run_id):
                return self._publish(layer, run_id, admitted)
        except DomainError as exc:
            if exc.code == "busy":
                return None
            raise

    def _publish(self, layer, run_id, admitted):
        directory = self._retained(run_id)
        if self._kept(run_id, admitted, verify=False) is not None:
            return directory
        if directory.exists():
            return None             # present but not exactly the admitted version: never overwritten, never used
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = directory.parent / f".stage-{uuid4().hex}"     # unique to this attempt: no other writer or reader ever touches it
        try:
            staged = layer.tools.stage_snapshot(admitted["script_ref"], staging, expected={
                "definition_revision": admitted["definition_revision"], "code_revision": admitted["code_revision"]})
            package = Path(staged["package_dir"])
            _write_durable(staging / DESCRIPTOR, json.dumps({
                "runtime": staged["runtime"], "limits": plain(staged["limits"]), "files": staged["files"],
                "definition_revision": staged["definition_revision"], "code_revision": staged["code_revision"],
                "code": os.path.relpath(staged["code_path"], package)}))
            os.rename(staging, directory)                        # one atomic step publishes the copy together with its descriptor
            _sync_directory(directory.parent)
        except AgenticError:
            shutil.rmtree(staging, ignore_errors=True)
            return None
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return directory

    def _stage(self, control, automation, state):
        """Build the run's snapshot of exactly the revisions frozen at admission: from the run's retained copy when the script was kept
        (the copy is *copied*, never consumed), else from the catalog. It is assembled in a unique temporary directory and renamed into
        place under the run's snapshot claim, so nothing half built is ever visible. Until the job records the snapshot (the caller's
        checkpoint) an earlier unrecorded build of a crashed start is replaced - from the same retained copy, whatever the authored
        script says now. No database transaction is held while files are copied."""
        ext = self.ext
        layer = ext.runtime.agentic_layer
        if layer is None:
            raise DomainError("operation_unavailable", "The Agentic layer is not installed on this host")
        admitted = plain(automation["admitted_json"]) or {}
        if not admitted.get("script_ref") or not admitted.get("code_revision"):
            raise DomainError("admission_missing", "This run has no frozen admission; it must be queued through the automation tools")
        run_id = control.job["id"]
        relative = f"snapshots/{run_id}"
        destination = ext.state_root / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = destination.parent / f".stage-{uuid4().hex}"
        try:
            with self._snapshot_claim(run_id):
                staged = self._copy_admitted(layer, run_id, admitted, staging)
                if staged["runtime"] != admitted["runtime"]:
                    raise DomainError("script_changed", "The automation script's runtime changed after admission")
                sdk_dir = staging / "sdk"
                sdk_dir.mkdir(mode=0o700)
                shutil.copyfile(Path(__file__).with_name("sdk.py"), sdk_dir / SDK_NAME)
                shutil.rmtree(destination, ignore_errors=True)   # an unrecorded earlier build of a crashed start (no checkpoint exists)
                os.rename(staging, destination)
                _sync_directory(destination.parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return {"ref": admitted["script_ref"], "admission_id": admitted["admission_id"], "dir": relative,
                "automation_revision": admitted["automation_revision"], "trigger": admitted.get("trigger", "manual"),
                "runtime": staged["runtime"], "code": staged["code"],
                "definition_revision": staged["definition_revision"], "code_revision": staged["code_revision"],
                "files": staged["files"], "sdk_sha256": _sha(destination / "sdk" / SDK_NAME), "phases": admitted["phases"],
                "input": admitted["input"], "input_digest": admitted["input_digest"], "limits": plain(staged["limits"]),
                "run_limits": admitted.get("limits") or {}}

    def _copy_admitted(self, layer, run_id, admitted, staging):
        """Fill the not-yet-existing ``staging`` with the admitted package and describe it: ``runtime``, ``limits``, ``files``, the two
        revisions and the ``code`` path relative to the package."""
        kept = self._kept(run_id, admitted, verify=True)
        if kept is not None:
            directory, descriptor = kept
            staging.mkdir(mode=0o700, parents=True)
            shutil.copytree(directory / "package", staging / "package")
            return dict(descriptor)
        try:
            staged = layer.tools.stage_snapshot(admitted["script_ref"], staging, expected={
                "definition_revision": admitted["definition_revision"], "code_revision": admitted["code_revision"]})
        except RevisionConflictError as exc:
            raise DomainError("script_changed", "The automation script changed after this run was admitted and no copy of the "
                              "admitted version was kept; the run was not started with different code") from exc
        except AgenticError as exc:
            raise DomainError("script_unavailable", f"The automation script is unavailable: {exc}") from exc
        return {"runtime": staged["runtime"], "limits": plain(staged["limits"]), "files": staged["files"],
                "definition_revision": staged["definition_revision"], "code_revision": staged["code_revision"],
                "code": os.path.relpath(staged["code_path"], Path(staged["package_dir"]))}

    def _verify(self, snapshot) -> Path:
        root = self.ext.state_root / snapshot["dir"]
        for item in snapshot["files"]:
            path = root / "package" / item["path"]
            if not path.is_file() or _sha(path) != item["sha256"]:
                raise DomainError("snapshot_unavailable", f"The staged script file {item['path']} is missing or changed")
        sdk = root / "sdk" / SDK_NAME
        if not sdk.is_file() or _sha(sdk) != snapshot["sdk_sha256"]:
            raise DomainError("snapshot_unavailable", "The staged automation SDK is missing or changed")
        if canonical_hash(snapshot["input"]) != snapshot["input_digest"]:
            raise DomainError("snapshot_unavailable", "The frozen run input changed")
        return root


def register(programs, extension) -> AutomationProgram:
    """Register the one program: Global or Project, external and recoverable, with the engine's admission, poll and cancel interfaces."""
    program = AutomationProgram(extension)
    programs.register(Program(PROGRAM, 1, SCHEMA, program.run, {}, (), (), global_capabilities=(), scope_kinds=("global", "business"),
                              external=True, recover=program.recover, timeout_seconds=extension.config.max_run_seconds,
                              admit=program.admit, poll=program.poll, cancel=program.cancel))
    return program
