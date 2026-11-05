"""The host side of a run's socket: every script request becomes one bounded, durable, authority-checked step.

The bridge is the only place a script's call becomes an Operations invocation, and it constructs the context itself: the *current* configured
Ordubashy, in the automation's own scope (Global, or its Project), bound to this run through the trusted ``automation_run_id`` (never
taken from the script). The central policy gate (``guard.Guard.gate``) then decides every call; this module adds durable replay, retry
lineage, waits and publication. Every database write is a short transaction inside the existing writer-admission fence
(``Automation.writing``); no admission and no transaction is held across an invocation, a provider call or the script.

Replay model: a step key that already succeeded returns its recorded result after its read access is revalidated; a key with a different
request is refused. The outcome of a failed, interrupted or unconfirmed *mutation* is classified by ``_outcome_path`` from the operation's
declared effects and the engine's own durable records for its exact idempotency key, never from an error string: a committed receipt or a
durable file-application record means the *same* key is safe to re-invoke (the engine answers or resumes it); only a transaction-only
operation without a receipt, or a file operation whose application record never existed or was released unchanged, is positive no-effect
and may be retried as a *new* linked attempt, at most twice - the budget is counted from the ledger, so a restart neither resets it nor
loses a retry that was scheduled but not yet inserted; an external provider callback (which runs before the receipt transaction and
leaves no intent) is never replayed - the step waits visibly in recovery on its original identity. Reads retry normally.

A call's result envelope is read by ``waits.outcome`` - here for a live result and, for a stopped run, from the engine's committed
receipt - so only an actual completed result finishes a step; a queued provider job (also named by an ``ok`` result) and a proposal become
the exact job and Signoff waits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import struct
import threading
import time

from qorqut_operations.contracts import DomainError, InvocationOptions, TargetSet, canonical_hash, new_id, plain, utc_now

from . import steps, waits
from .events import MANAGERS, ordubashy, publish as publish_event
from .failures import notify_uncertain, record_step_failure
from .guard import forbidden
from .records import MAX_PUBLISHED_BYTES, PHASE_NAME, STEP_KEY, bounded, encoded

MAX_FRAME = 8 * 1024 * 1024
READ_SECONDS = 30
RETRY_DELAY = (1.0, 3.0)
MAX_SAVED_BYTES = 65536
MAX_PROGRESS_BYTES = 65536
FATAL = frozenset({"cancelled", "timeout", "service_stopping", "maintenance", "bridge_read_timeout"})
# An engine refusal that means "stop now", never "this step failed": the step stays running and the replay re-attempts it
# under the same idempotency key once the run is resumed (a recovery window, a service stop or a cancellation).
STOP_CODES = frozenset({"cancelled", "service_stopping", "maintenance"})
AMBIGUOUS = frozenset({"external_unconfirmed", "apply_incomplete", "timeout", "external_result_invalid", "background_failed"})
QUESTION_POLICIES = ("owner_only", "management_allowed")


class BridgeError(Exception):
    def __init__(self, code, message="", retryable=False, details=None, exhausted=False, step_id=None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.details, self.exhausted = code, message, retryable, details or {}, exhausted
        self.step_id = step_id

    def payload(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


class Suspended(Exception):
    """Internal: the current step waits; the child is told to stop and the host records the wait."""


def _idempotency(run_id, key, attempt):
    return f"auto:{run_id}:{canonical_hash(key)[:24]}:{attempt}"


idempotency = _idempotency      # the one derivation of a step attempt's engine idempotency key (also read by cancellation settlement)


def outcome_path(ext, operation, idempotency_key):
    """Where the outcome of one engine mutation under this exact idempotency key can be settled, from the operation's declared
    effects and the engine's own durable records - never from an error string:

    ``"replay"``      the engine can answer the same key itself: a committed idempotency receipt, or a durable file-application
                      record that its file executor resumes. Re-invoking the same key is safe and settles the outcome.
    ``"none"``        positive no-effect: a transaction-only operation (its receipt and records commit together and an error rolls
                      both back) without a receipt, or a file operation whose application record was never created or was
                      released unchanged. Only this permits a fresh linked attempt.
    ``"unresolved"``  an external provider callback (calendar, Cloudflare, media...) runs before the receipt transaction and
                      leaves no durable intent, so a missing receipt proves nothing and replaying the key could repeat the
                      provider effect. Nothing canonical settles it; it waits visibly for a person."""
    descriptor = ext.runtime.registry.get(operation)
    with ext.runtime.database.read() as uow:
        if uow.connection.execute("SELECT 1 FROM idempotency_receipts WHERE operation_name=? AND idempotency_key=? LIMIT 1",
                                  (operation, idempotency_key)).fetchone() is not None:
            return "replay"
        applied = uow.connection.execute(
            "SELECT a.state, EXISTS (SELECT 1 FROM resource_claims c WHERE c.apply_id=a.id) AS claimed FROM apply_receipts a "
            "WHERE a.operation_name=? AND a.signoff_id IS NULL AND json_extract(a.progress_json,'$.invocation.idempotency_key')=? "
            "LIMIT 1", (operation, idempotency_key)).fetchone()
    if applied is not None:
        return "none" if applied["state"] in ("failed", "conflicted") and not applied["claimed"] else "replay"
    return "unresolved" if descriptor.external is not None else "none"


class RunBridge:
    def __init__(self, extension, control, automation_id, state, *, limits):
        self.ext, self.control, self.automation_id = extension, control, automation_id
        self.run_id = control.job["id"]
        self.state = state                      # the reserved ``result_json.automation`` object (snapshot immutable)
        self.limits = limits                    # {"max_calls", "max_step_bytes", "invocation_seconds"}
        self.calls = int(state.get("calls", 0))
        self.finished = self.failure = self.last_error = None
        self.suspended = self.continued = False
        self.in_flight = False
        self.stop = threading.Event()

    # -- framing --------------------------------------------------------------------------------------------------
    def serve(self, sock):
        sock.settimeout(1.0)
        while (self.finished is None and self.failure is None and not self.suspended and not self.continued
               and not self.stop.is_set()):
            try:
                header = self._recv(sock, 4)
            except TimeoutError:
                self.control.check()
                continue
            if header is None or len(header) < 4:
                return
            (size,) = struct.unpack(">I", header)
            if size > MAX_FRAME:
                self._send(sock, {"ok": False, "error": {"code": "request_too_large", "message": "Frame too large"}})
                return
            body = self._recv(sock, size, require=True)
            request = {}
            self.in_flight = True
            try:
                request = json.loads(body) if body is not None else {}
                reply = {"id": request.get("id"), "ok": True, "result": self.dispatch(request["method"], request.get("params") or {})}
            except Suspended:
                self.suspended = True
                reply = {"id": request.get("id"), "ok": False, "suspend": True}
            except BridgeError as exc:
                self.last_error = exc
                reply = {"id": request.get("id") if isinstance(request, dict) else None, "ok": False, "error": exc.payload()}
            except DomainError as exc:
                if exc.code in FATAL:
                    raise       # cancellation, timeout or service stop: the runner kills the child and settles the run
                reply = {"id": request.get("id") if isinstance(request, dict) else None, "ok": False,
                         "error": {"code": exc.code, "message": str(exc), "retryable": bool(getattr(exc, "retryable", False))}}
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                reply = {"id": None, "ok": False, "error": {"code": "bad_request", "message": f"Invalid bridge request: {exc}"}}
            finally:
                self.in_flight = False
            self._send(sock, reply)

    def _recv(self, sock, count, require=False):
        """Read exactly ``count`` bytes. A partial frame is bounded: it must complete within ``READ_SECONDS`` and a stop or
        cancellation ends the read, so a stalled script cannot pin the bridge."""
        data = bytearray()
        deadline = time.monotonic() + READ_SECONDS if require else None
        while len(data) < count:
            try:
                chunk = sock.recv(count - len(data))
            except TimeoutError:
                if not data and not require:
                    raise
                if deadline is None:
                    deadline = time.monotonic() + READ_SECONDS
                self.control.check()
                if self.stop.is_set() or time.monotonic() > deadline:
                    raise DomainError("bridge_read_timeout", "The script stopped in the middle of a request")
                continue
            if not chunk:
                return None if not data else bytes(data)
            data += chunk
        return bytes(data)

    @staticmethod
    def _send(sock, value):
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        try:
            sock.sendall(struct.pack(">I", len(data)) + data)
        except OSError:
            pass

    # -- dispatch -------------------------------------------------------------------------------------------------
    def dispatch(self, method, params):
        self.control.check()
        handler = {"hello": self.hello, "call": self.call, "ask_owner": self.ask_owner, "ask_agent": self.ask_agent,
                   "ask": self.ask_owner, "publish": self.publish, "phase": self.phase, "save": self.save,
                   "continue": self.continue_run, "finish": self.finish, "fail": self.fail}.get(method)
        if handler is None:
            raise BridgeError("unknown_method", f"Unknown bridge method {method!r}")
        return handler(params)

    def _open(self):
        """The run must still be open and its automation active before any new work (read fresh on every request)."""
        with self.ext.runtime.database.read() as uow:
            try:
                return self.ext.guard.bound_run(self.run_id, uow)
            except DomainError as exc:
                raise BridgeError("automation_not_active", str(exc)) from exc

    def hello(self, params):
        job, automation = self._open()
        snapshot = self.state["snapshot"]
        return {"run_id": self.run_id,
                "automation": {key: automation[key] for key in ("id", "slug", "title", "description", "business_id")},
                "input": snapshot["input"], "saved": automation["saved_json"],
                "phases": [item["name"] for item in snapshot["phases"]], "phase": self.state.get("phase"),
                "progress": self.state.get("progress"), "invocation_seconds": self.limits.get("invocation_seconds")}

    def _checkpoint(self, **changes):
        self.state.update(changes)
        self.state["calls"] = self.calls
        self.control.checkpoint("automation", self.state)

    def _clear_wait(self):
        if self.state.get("wait") is not None:
            self._checkpoint(wait=None)

    # -- shared step machinery ------------------------------------------------------------------------------------
    @staticmethod
    def _key(value):
        if not isinstance(value, str) or not STEP_KEY.fullmatch(value):
            raise BridgeError("bad_step_key", "A step key is 1-200 characters of letters, digits and ._:/-")
        return value

    def _count(self):
        self.calls += 1
        if self.calls > self.limits["max_calls"]:
            raise BridgeError("call_budget_exceeded", "This run reached its call limit", exhausted=True)

    def _context(self, uow, automation):
        responder = ordubashy(uow)
        if responder is None:
            raise BridgeError("management_unavailable", "No active Ordubashy is configured")
        app = self.ext.runtime.application
        scope = automation["business_id"]
        ctx = app.policy.context_for(responder, business_id=scope, review_business_id=scope, uow=uow, automation_run_id=self.run_id)
        return ctx, responder

    def _authorize_replay(self, operation, arguments):
        """A saved result is returned only while its operation is still permitted to this run (restriction, revocation, scope),
        judged against the same resolved scope as the original call: the bound arguments of a supported single-record call, so a
        resource-limited grant that was revoked or no longer matches ends the replay too."""
        app = self.ext.runtime.application
        with self.ext.runtime.database.read() as uow:
            try:
                job, automation = self.ext.guard.bound_run(self.run_id, uow)
                ctx, _responder = self._context(uow, automation)
                descriptor = app.registry.get(operation)
                effective = descriptor.business_policy if automation["business_id"] and descriptor.global_capability else descriptor
                decision = app.policy.resolve(ctx, effective, TargetSet((), "call", arguments), uow)
            except (DomainError, BridgeError) as exc:
                raise BridgeError("automation_restricted", f"This saved result is no longer readable by the run: {exc}") from exc
            if decision.mode == "deny":
                raise BridgeError("automation_restricted", f"{operation} is no longer permitted to this run")

    def _step(self, kind, key, operation, request_hash, execute, *, mutation, debug=None, replay_operation=None, replay_arguments=None):
        with self.ext.runtime.database.read() as uow:
            row = steps.latest(uow, self.run_id, key)
        if row is not None:
            if row["request_hash"] != request_hash or row["kind"] != kind:
                raise BridgeError("step_key_conflict", "This step key already belongs to a different request")
            if row["state"] == "succeeded":
                if replay_operation:
                    self._authorize_replay(replay_operation, replay_arguments or {})
                return row["result_json"]
            if row["state"] == "cancelled":
                raise BridgeError("step_cancelled", "The step was cancelled", step_id=row["id"])
            if row["state"] == "failed":
                error = row["error_json"] or {"code": "step_failed", "message": "The step failed"}
                if not self._retry_due(row, error):
                    raise BridgeError(error.get("code", "step_failed"), error.get("message", ""), False, error.get("details"),
                                      exhausted=bool(error.get("exhausted")), step_id=row["id"])
            elif row["state"] == "waiting":
                outcome = waits.settle_waiting(self.ext, row["id"])
                if outcome == "pending":
                    self._suspend(row)
                self._clear_wait()
                return self._step(kind, key, operation, request_hash, execute, mutation=mutation, debug=debug,
                                  replay_operation=replay_operation, replay_arguments=replay_arguments)
        return self._attempts(kind, key, operation, request_hash, execute, row, mutation=mutation, debug=debug)

    @staticmethod
    def _retry_due(row, error):
        """A failed attempt is retried only when its ledger evidence says so: a retryable engine failure with recorded no-effect, within
        the durable attempt budget (the initial attempt and two linked retries)."""
        return (bool(error.get("retryable")) and not error.get("exhausted") and row["effect"] == "none"
                and row["kind"] == "call" and row["attempt"] < steps.MAX_ATTEMPTS)

    def _begin(self, automation, kind, key, operation, request_hash, attempt, parent, mutation, debug):
        with self.ext.writing() as uow:
            return steps.begin(uow, automation, self.run_id, key, kind, operation, request_hash, attempt=attempt,
                               parent=parent, effect="possible" if mutation else "none", debug=debug)

    def _outcome_path(self, operation, idempotency_key):
        return outcome_path(self.ext, operation, idempotency_key)

    def _wait_until(self, moment):
        """Sleep until an ISO moment (at most a few seconds), stopping at once on cancellation or a service stop."""
        try:
            remaining = (datetime.fromisoformat(moment.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
        except (AttributeError, ValueError):
            remaining = 0
        end = time.monotonic() + max(0.0, min(remaining, RETRY_DELAY[-1]))
        while time.monotonic() < end:
            self.control.check()
            time.sleep(0.25)

    def _attempts(self, kind, key, operation, request_hash, execute, row, *, mutation, debug):
        _job, automation = self._open()
        if row is not None and row["state"] in ("running", "unknown"):
            step, attempt = row, row["attempt"]                       # the same attempt: the same idempotency key
            if mutation and self._outcome_path(operation, _idempotency(self.run_id, key, attempt)) == "unresolved":
                # Interrupted before its outcome was recorded, and an external callback may already have run without a receipt:
                # the same key is not replayed; the run waits visibly on this very step.
                return self._uncertain(step, key, {"code": "external_unconfirmed", "operation": operation, "message":
                                                   "This step was interrupted and its external effect cannot be confirmed from a "
                                                   "receipt; it was not repeated"}, recovery=True)
        elif row is not None and row["state"] == "failed":
            error = row["error_json"] or {}
            self._wait_until(error.get("retry_at", ""))
            attempt = row["attempt"] + 1
            step = self._begin(automation, kind, key, operation, request_hash, attempt, row["id"], mutation, debug)
        else:
            attempt = 1
            step = self._begin(automation, kind, key, operation, request_hash, attempt, None, mutation, debug)
        self._count()
        self._checkpoint()
        status, value = execute(attempt)
        if status == "ok":
            with self.ext.writing() as uow:
                steps.settle(uow, step["id"], "succeeded", effect="confirmed" if mutation else "none",
                             result=bounded(plain(value), self.limits["max_step_bytes"], "Step result"))
            return value
        if status == "wait":
            with self.ext.writing() as uow:
                steps.settle(uow, step["id"], "waiting", wait=value)
            self._checkpoint(wait={"step_key": key, "step_id": step["id"], "kind": value["kind"]})
            raise Suspended()
        return self._failed(step, kind, key, operation, request_hash, execute, error=value, attempt=attempt, mutation=mutation,
                            debug=debug)

    def _failed(self, step, kind, key, operation, request_hash, execute, *, error, attempt, mutation, debug):
        code = error.get("code", "failed")
        idempotency = _idempotency(self.run_id, key, attempt)
        path = self._outcome_path(error.get("operation", operation), idempotency) if mutation else "none"
        if mutation and code in AMBIGUOUS and path == "none":
            # The engine itself says a mutation's outcome is unknown: only the same key may settle it, never a fresh one. A read has
            # no effect that could be unknown (a read timeout is only a failure), so it keeps its ordinary bounded retries.
            path = "replay"
        if path != "none":
            # Uncertain effect: keep the original identity pending, recheck it a bounded number of times (the engine answers the same
            # key from its receipt or resumes its own file recovery), then keep a visible recovery wait for a person. An external
            # provider callback without a receipt is never replayed: it goes straight to the visible recovery wait. The run is never
            # failed (which would release the guard) while the effect is unknown.
            return self._uncertain(step, key, error, recovery=path == "unresolved")
        retryable = bool(error.get("retryable")) and attempt < steps.MAX_ATTEMPTS
        failure = {**error, "exhausted": bool(error.get("retryable")) and not retryable}
        if retryable:
            delay = RETRY_DELAY[min(attempt - 1, len(RETRY_DELAY) - 1)]
            failure["retry_at"] = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.ext.writing() as uow:
            closed = steps.settle(uow, step["id"], "failed", effect="none", error=failure)
            automation = uow.connection.execute("SELECT * FROM automations WHERE id=?", (self.automation_id,)).fetchone()
            record_step_failure(self.ext.runtime, uow, automation, self.run_id, closed, failure)
        if retryable:
            return self._step(kind, key, operation, request_hash, execute, mutation=mutation, debug=debug)
        raise BridgeError(code, error.get("message", ""), bool(error.get("retryable")), error.get("details"),
                          exhausted=failure["exhausted"], step_id=step["id"])

    def _uncertain(self, step, key, error, *, recovery=False):
        """Record an unconfirmed effect on the original step and identity and suspend the run: a bounded recheck wait, or at once
        the visible recovery wait (``recovery``) when nothing canonical can recheck it. Required Ordubashy notice in the same unit."""
        reconciles = int((step["debug_json"] or {}).get("reconciles") or 0) + 1
        wait = waits.reconcile_wait(waits.MAX_RECONCILES + 1 if recovery else reconciles)
        with self.ext.writing() as uow:
            closed = steps.settle(uow, step["id"], "waiting", wait=wait, effect="possible", debug={"reconciles": reconciles},
                                  error={**error, "uncertain": True})
            automation = uow.connection.execute("SELECT * FROM automations WHERE id=?", (self.automation_id,)).fetchone()
            notify_uncertain(self.ext.runtime, uow, automation, self.run_id, closed, recovery=wait["kind"] == "recovery_required")
        self._checkpoint(wait={"step_key": key, "step_id": step["id"], "kind": wait["kind"]})
        raise Suspended()

    def _suspend(self, row):
        self._checkpoint(wait={"step_key": row["step_key"], "step_id": row["id"], "kind": (row["wait_json"] or {}).get("kind")})
        raise Suspended()

    def _invoke(self, operation, arguments, key, attempt, automation):
        """One engine invocation as the current Ordubashy, bound to this run. Returns ``(status, value)``."""
        database, app = self.ext.runtime.database, self.ext.runtime.application
        with database.read() as uow:
            try:
                ctx, responder = self._context(uow, automation)
            except BridgeError as exc:
                return "error", {"code": exc.code, "message": exc.message, "retryable": False}
        scope = automation["business_id"]
        result = app.invoke(ctx, operation, arguments, InvocationOptions(
            idempotency_key=_idempotency(self.run_id, key, attempt), business_id=scope, review_business_id=scope))
        if result.status != "error":
            # Only an actual synchronous result finishes the step; a queued provider job (also an ``ok`` that names one) and a proposal
            # become the exact wait the cancellation settlement reads from the same receipt.
            with database.read() as uow:
                return waits.outcome(uow, operation, result.to_dict(), responder, self.run_id, scope)
        error = plain(result.error or {})
        if error.get("code") in STOP_CODES:
            raise DomainError(error["code"], error.get("message") or "The run must stop")
        return "error", {"code": error.get("code", "failed"), "message": error.get("message", ""),
                         "retryable": bool(error.get("retryable")), "details": error.get("details") or {}, "operation": operation}

    # -- capabilities ---------------------------------------------------------------------------------------------
    def call(self, params):
        job, automation = self._open()
        key, operation, arguments = self._key(params["key"]), params["operation"], params.get("arguments") or {}
        approval = params.get("approval")
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            raise BridgeError("bad_request", "call needs an operation name and an arguments object")
        if approval not in (None, "required"):
            raise BridgeError("bad_request", "approval is omitted or 'required'")
        if forbidden(operation):
            raise BridgeError("automation_forbidden_operation", f"A script cannot call {operation}")
        try:
            descriptor = self.ext.runtime.registry.get(operation)
        except Exception as exc:
            raise BridgeError("unknown_operation", f"No operation {operation!r}") from exc
        if automation["business_id"] is not None and "business" not in descriptor.scope_kinds:
            raise BridgeError("automation_scope", f"{operation} is not available to a Project automation")
        if "scope" in descriptor.input_schema.get("properties", {}) and "scope" not in arguments:
            arguments = {**arguments, "scope": "business" if automation["business_id"] else "global"}
        bounded(arguments, self.limits["max_step_bytes"], "Call arguments")
        request_hash = canonical_hash({"operation": operation, "arguments": arguments, "approval": approval})
        return self._step("call", key, operation, request_hash,
                          lambda attempt: self._invoke(operation, arguments, key, attempt, automation),
                          mutation=descriptor.effect_kind != "read", debug={"approval": approval} if approval else {},
                          replay_operation=operation, replay_arguments=arguments)

    def ask_owner(self, params):
        return self._ask(params, "owner")

    def ask_agent(self, params):
        return self._ask(params, "agent")

    def _ask(self, params, audience):
        job, automation = self._open()
        key, form = self._key(params["key"]), params.get("form")
        if not isinstance(form, dict):
            raise BridgeError("bad_request", "a question needs a form object")
        attention = params.get("attention")
        if attention is not None and not isinstance(attention, dict):
            raise BridgeError("bad_request", "attention must be an object")
        if audience == "owner":
            policy = form.get("response_policy", "owner_only")
            if policy not in QUESTION_POLICIES or (policy == "management_allowed" and form.get("kind", "question") == "request"):
                raise BridgeError("bad_request", "response_policy is owner_only, or management_allowed for a question")
            form = {"kind": "question", **form, "response_policy": policy}
            attention = attention or {"notify": True}
        else:
            if form.get("kind", "question") != "question" or form.get("response_policy", "management_allowed") != "management_allowed":
                raise BridgeError("bad_request", "A question for the managing agent is a management_allowed question")
            form = {**form, "kind": "question", "response_policy": "management_allowed"}
            attention = attention or {"notify": False}
        request_hash = canonical_hash({"ask": audience, "form": form, "attention": attention})
        arguments = {"scope": "business" if automation["business_id"] else "global",
                     "request_key": f"auto:{self.run_id}:{canonical_hash(key)[:24]}", "form": form, "attention": attention}

        def execute(attempt):
            status, value = self._invoke("signoffs_input_create", arguments, key, attempt, automation)
            if status != "ok":
                return status, value
            signoff = (value or {}).get("signoff") or {}
            with self.ext.runtime.database.read() as uow:
                row = uow.connection.execute("SELECT * FROM signoffs WHERE id=?", (signoff.get("id"),)).fetchone()
            if row is None:
                return "error", {"code": "input_unavailable", "message": "The question was not recorded", "retryable": False}
            if audience == "agent":
                self._route_to_management(automation, row, form)
            return "wait", {"kind": "input", "signoff_id": row["id"], "proposer_principal_id": row["proposer_principal_id"],
                            "proposal_revision": row["proposal_revision"], "payload_hash": row["payload_hash"],
                            "status_revision": row["status_revision"], "audience": audience, "request_key": arguments["request_key"]}
        return self._step("ask", key, "signoffs_input_create", request_hash, execute, mutation=True)

    def _route_to_management(self, automation, signoff, form):
        """Address a question to the managing agent: an explicit management route (the automatic routing skips a question the
        coordinator itself asked) and a durable, required notice that does not depend on any subscription. Idempotent."""
        with self.ext.writing() as uow:
            coordinator = ordubashy(uow)
            now = utc_now()
            uow.connection.execute(
                "INSERT OR IGNORE INTO management_routing(id,source_type,source_id,business_id,coordinator_id,attention_destination,"
                "management_allowed,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (new_id(), "signoffs", signoff["id"], signoff["review_business_id"], coordinator, "management", 1, coordinator, now, now))
            current = uow.connection.execute("SELECT * FROM automations WHERE id=?", (automation["id"],)).fetchone()
            publish_event(self.ext.runtime, uow, current, key=f"question:{signoff['id']}", event_type="question",
                          summary="Automation asks you: " + str(form.get("title", ""))[:120],
                          data={"signoff_id": signoff["id"], "proposal_revision": signoff["proposal_revision"],
                                "payload_hash": signoff["payload_hash"], "status_revision": signoff["status_revision"],
                                "title": form.get("title")},
                          run_id=self.run_id, required_for=(coordinator,), audience=MANAGERS)

    def publish(self, params):
        job, automation = self._open()
        key, summary = self._key(params["key"]), params["summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise BridgeError("bad_request", "publish needs a summary")
        data, advance = params.get("data"), params.get("advance")
        bounded(data, MAX_PUBLISHED_BYTES, "Published data")
        if advance is not None and not isinstance(advance, dict):
            raise BridgeError("bad_request", "advance must be an object")
        request_hash = canonical_hash({"summary": summary, "data": data, "advance": advance})
        with self.ext.writing() as uow:
            previous = steps.latest(uow, self.run_id, key)
            if previous is not None:
                if previous["request_hash"] != request_hash or previous["kind"] != "publish":
                    raise BridgeError("step_key_conflict", "This step key already belongs to a different request")
                if previous["state"] == "succeeded":
                    return {k: v for k, v in previous["result_json"].items() if k != "data"}
            self._count()
            fresh = uow.connection.execute("SELECT * FROM automations WHERE id=?", (self.automation_id,)).fetchone()
            step = previous or steps.begin(uow, fresh, self.run_id, key, "publish", None, request_hash, effect="possible")
            # The durable event and the cursor advance commit together, so a saved cursor can never get ahead of published output.
            event_id, created = publish_event(self.ext.runtime, uow, fresh, key="out:" + key, event_type="output", summary=summary,
                                              data=data, run_id=self.run_id)
            saved = self._merge_saved(uow, advance) if advance else None
            # The step keeps the complete bounded output; the inbox event only the first 8 KiB.
            result = {"event_id": event_id, "created": created, "summary": summary[:2000], "data": data,
                      **({"saved": saved} if saved is not None else {})}
            steps.settle(uow, step["id"], "succeeded", effect="confirmed", result=result)
        self._checkpoint()
        return {k: v for k, v in result.items() if k != "data"}

    def _merge_saved(self, uow, patch):
        row = uow.connection.execute("SELECT saved_json FROM automations WHERE id=?", (self.automation_id,)).fetchone()
        saved = {**json.loads(row["saved_json"]), **patch}
        if len(encoded(saved)) > MAX_SAVED_BYTES:
            raise BridgeError("saved_state_too_large", f"Saved state is limited to {MAX_SAVED_BYTES} bytes")
        uow.connection.execute("UPDATE automations SET saved_json=? WHERE id=?", (encoded(saved), self.automation_id))
        return saved

    def save(self, params):
        self._open()
        patch = params.get("patch")
        if not isinstance(patch, dict):
            raise BridgeError("bad_request", "save needs a patch object")
        with self.ext.writing() as uow:
            return self._merge_saved(uow, patch)

    def phase(self, params):
        job, automation = self._open()
        name = params.get("name")
        declared = [item["name"] for item in self.state["snapshot"]["phases"]]
        if not isinstance(name, str) or not PHASE_NAME.fullmatch(name) or name not in declared:
            raise BridgeError("unknown_phase", "A phase must be one the automation declares", details={"declared": declared})
        request_hash = canonical_hash({"phase": name})
        key = "phase:" + name
        with self.ext.writing() as uow:
            previous = steps.latest(uow, self.run_id, key)
            if previous is None:
                step = steps.begin(uow, automation, self.run_id, key, "phase", None, request_hash)
                steps.settle(uow, step["id"], "succeeded", result={"phase": name})
        self._checkpoint(phase=name)
        return {"phase": name}

    def continue_run(self, params):
        """Explicit JSON continuation: save the progress and end this invocation; the run resumes with the same snapshot."""
        self._open()
        progress = params.get("progress")
        bounded(progress, MAX_PROGRESS_BYTES, "Run progress")
        self.continued = True
        self._checkpoint(progress=plain(progress))
        return {"continue": True}

    def finish(self, params):
        result = params.get("result")
        bounded(result, self.limits["max_step_bytes"], "Run result")
        self.finished = {"result": plain(result)}
        return {"finished": True}

    def fail(self, params):
        self.failure = str(params.get("message") or "The script reported a failure")[:1000]
        return {"failed": True}
