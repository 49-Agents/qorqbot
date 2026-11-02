"""The restart-safe, ordered driver of the one Context input queue (P4B).

Operations owns the queue rows, the explicit retry/discard controls and every state transition; Context owns draft and
publication. This driver sits between them for GENERAL source inputs and accepted Action revisions alike and never
reinterprets either:

* It reads exactly what the queue froze: the stored payload bytes and metadata, the recorded submitter, the namespace and,
  for an Action, the revision's frozen tags, pointers, text and revision reference. It never rereads today's Action or
  upload. An edit or removal is submitted as such; a missing prior add is a blocked input, never an inferred add.
* Controlled ``record_context`` evidence ``{scope, provenance}`` is derived from the stored input, never from a tag or from
  public text. For an Action it is the revision's own scope and provenance; for a GENERAL source it is the input's
  namespace with the queue's own facts (input id, submitter, queue time).
* The source is bound to the input's scope (``context_filing``) in the transaction that starts the attempt, before any
  Context work. A source of another scope, or one nobody owns, blocks the head with an ownership error; that refused
  attempt is recorded in the same transaction and never reaches Context or a model.
* The exact event ID is saved with the attempt before Git or a model is touched. Context, Git and model work happen outside
  any SQLite transaction.
* A published event is looked up by its exact ID before any runner call. It is verified against the frozen input (mode,
  source, operation, submitter, bytes or Action fields, record context, commit and an explicit handoff disposition) and only
  then recorded once. A published event is never discarded, rerun or blocked.
* Failures of SQLite recording leave the queue head ``processing`` with the publication intact: the next ``recover`` or
  ``process_next`` records it mechanically, with no second model call.
* The coordinator is the host's configured coordinator agent (any runtime). Without one no attempt starts and no model runs
  (``not_ready``); if it disappears after publication the exact publication stays pending for recovery and nothing is
  retried or fabricated. Only a committed, nonempty handoff note produces the queue's normal deduplicated notice.

``process_next`` advances the oldest unfinished input by one step and ``recover`` is mechanical startup reconciliation; both
return a ``status`` mapping and neither starts a process, a native agent or a runner of its own: all model work is the
supplied ``Pipeline``'s existing runner. ``ContextDriverLoop`` (``loop.py``) supplies bounded start/stop around them.

* A controlled deterministic graph edit (``context_graph_edits``, see ``graph_edit.py``) is a GENERAL input bound by its companion
  row, found by input ID and never by path or JSON shape. Before any fresh execution or explicit retry the driver rechecks, through
  the typed ``graph_authority`` port, that the plan, base and frozen targets still hold and that the *original caller's* current
  registration still allows it (the driver's system identity is never that permission); a refusal blocks the head with the reason
  and starts no attempt. If the exact event is already published, that real result is reconciled first whatever the caller's
  authority now is. The edit runs through ``Context.begin_edit``/``Pipeline.submit_edit`` with its frozen plan, original submitter,
  reserved event ID and controlled record context; it never reaches a model, a runner or a usage receipt, and a collision or
  mismatch of the reserved ID blocks instead of reallocating.

Terminal outcomes are required, not optional. Every transition that makes an input ``published`` or ``discarded`` receives the
supplied ``outcomes`` contribution (``context_filing.FilingOutcomes``), so a filing's stable completion receipt is recorded in
that same transaction. Before every step, under the driver lock and without any Context or model work, the driver also
closes the mechanical gaps that remain (``reconcile``): receipts of final bound inputs that have none, receipts whose bound
job or frozen definition can no longer apply them, and unfinished inputs whose registered job was cancelled (through the
queue's own discard mechanism, with the job's state and error as evidence, which a pending discard keeps through its
resolution; a retry still pending for such an input is rejected with that evidence instead of starting another attempt).
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
import re
import secrets
import string

from qorqut_context import Conflict, ContextError, Pipeline, PropagationError, graph_edits as library
from qorqut_context import input_modes as modes
from qorqut_context.integration import filing as context_filing
from qorqut_context.integration import graph_edits as context_graph_edits
from qorqut_context.integration.filing import completion
from qorqut_context.integration.graph_edits.graph_edit import GraphEditRefused
from qorqut_context.integration.queue import transitions
from qorqut_context.integration.queue.outcomes import require_outcomes
from qorqut_operations.contracts import DomainError

LOCK_NAME = "context-queue-driver.lock"
_EVENT_ID = re.compile(r"[a-z0-9]{5}")
_WRITER_BUSY = "Another writer/query is using this shared context"     # Context's transient lock conflict
_ALPHABET = string.ascii_lowercase + string.digits


class NotReady(Exception):
    """The registered coordinator cannot receive the notice a transition requires. Never a Context failure."""


def _random_event_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(5))


def writer_busy(error: BaseException) -> bool:
    """A transient Context writer/query lock conflict, which is not a failure of the queue head."""
    return isinstance(error, Conflict) and _WRITER_BUSY in str(error)


def describe(error: BaseException) -> dict:
    """A concise, storable description of a failure. The private diagnostics path of a propagation attempt stays in the
    Context runtime; the queue keeps only that diagnostics exist."""
    if isinstance(error, GraphEditRefused):
        return {"code": error.code, "type": "GraphEditRefused", "message": str(error), "retryable": True, "phase": error.phase}
    code = ("propagation_failed" if isinstance(error, PropagationError)
            else "context_conflict" if isinstance(error, Conflict)
            else "context_error" if isinstance(error, ContextError)
            else "storage_error" if isinstance(error, OSError) else "unexpected_error")
    result = {"code": code, "type": type(error).__name__, "message": str(error) or type(error).__name__, "retryable": True}
    if isinstance(error, PropagationError):
        result["phase"] = error.phase
        result["diagnostics_retained"] = bool(error.diagnostics)
    return result


def record_context_of(row: dict) -> dict:
    """The controlled ``{scope, provenance}`` of a stored queue input; a pure function of the frozen row.

    An Action input carries its revision's scope and provenance in its payload. A GENERAL input has only the facts the queue
    itself recorded, so those are its provenance; its caller-supplied metadata (including ``origin_ref``) stays in the
    source metadata as evidence and is not copied into controlled evidence."""
    if row["input_kind"] == "action":
        return {"scope": row["payload"]["scope"], "provenance": row["payload"]["provenance"]}
    provenance = {"input_id": row["id"], "submitted_by": row["submitted_by"], "queued_at": row["created_at"]}
    if row.get("edit") is not None:
        # A graph edit also carries its immutable original attribution: who asked, in which role and namespace.
        provenance["attribution"] = row["edit"]["attribution"]
    return {"scope": row["scope"], "provenance": provenance}


def submission_of(row: dict, event_id: str) -> tuple[tuple, dict]:
    """The exact positional and keyword arguments of the frozen input for ``Context.begin*`` / ``Pipeline.submit*``."""
    payload, evidence = row["payload"], record_context_of(row)
    if row.get("edit") is not None:
        return (edit_plan(row),), {"submitted_by": payload["submitted_by"], "event_id": event_id, "record_context": evidence}
    if row["input_kind"] == "action":
        return (payload["operation"], payload["action_ref"]), {
            "text": payload.get("text"), "revision_ref": payload["revision_ref"], "related_objects": payload["related_objects"],
            "tags": payload["tags"], "submitted_by": payload["submitted_by"], "event_id": event_id, "record_context": evidence}
    content = payload["content"]
    return (payload["operation"], payload["path"]), {
        "content": None if content is None else content.encode("utf-8"), "metadata": payload["metadata"],
        "submitted_by": payload["submitted_by"], "event_id": event_id, "record_context": evidence}


def edit_plan(row: dict) -> dict:
    """The frozen normalized plan of a bound graph-edit input, checked against the exact stored source bytes.

    The library normalizes the plan once more and its canonical encoding must be the stored payload content, so what is
    dispatched is exactly the immutable bytes the queue froze and the digest the binding names."""
    binding = row["edit"]
    plan = library.normalize(binding["plan"])
    content = library.canonical(plan)
    if content != binding["content"].encode("utf-8") or hashlib.sha256(content).hexdigest() != binding["plan_digest"]:
        raise ContextError("The stored graph-edit plan is not its frozen canonical source")
    return plan


class ContextQueueDriver:
    """Drive queued Context inputs through the Context ``Pipeline`` in queue order, restart-safely.

    ``driver_id`` is the active registered ``system`` principal the queue's transitions require. ``pipeline`` carries the
    only model runner this driver ever uses. ``outcomes`` is the required terminal-outcome contribution and ``programs`` the
    registered program catalog the mechanical completion check reads. ``coordinator`` is the required host callback
    ``coordinator(uow) -> principal ID | None`` that resolves the configured coordinator from the registration inside the
    read snapshot of every lookup (never cached). ``graph_authority`` is the typed port
    (``graph_edit.GraphEditAuthority``) that rechecks a controlled graph edit against the original caller's current authority;
    without it a graph-edit input is refused rather than run, and ordinary inputs are unaffected. ``id_source`` exists for
    deterministic collision checks."""

    def __init__(self, database, context, pipeline: Pipeline, *, driver_id: str, outcomes, programs, coordinator,
                 graph_authority=None, id_source=None):
        if pipeline.context is not context:
            raise ValueError("The pipeline must belong to the supplied Context")
        if not isinstance(driver_id, str) or not driver_id:
            raise ValueError("The Context queue driver needs the backend system principal ID")
        self.outcomes = require_outcomes(outcomes)
        if not callable(getattr(programs, "get", None)):
            raise ValueError("The Context queue driver needs the registered program catalog to check pending completions")
        if not callable(coordinator):
            raise ValueError("The Context queue driver needs the coordinator lookup coordinator(uow)")
        self.programs, self.graph_authority, self._coordinator = programs, graph_authority, coordinator
        self.database, self.context, self.pipeline, self.driver_id = database, context, pipeline, driver_id
        self._new_event_id = id_source or _random_event_id
        self.last: dict | None = None

    # ------------------------------------------------------------------ operational reads and writes

    def _read(self, function, **arguments):
        with self.database.read() as uow:
            return function(uow, driver_id=self.driver_id, **arguments)

    def _write(self, function, **arguments):
        with self.database.transaction(operation="context_queue_driver_write") as uow:
            return function(uow, driver_id=self.driver_id, **arguments)

    def coordinator(self) -> str | None:
        """The configured coordinator's identity if it can receive notices now, resolved from the registration each time."""
        with self.database.read() as uow:
            return self._coordinator(uow)

    def _attach(self, row: dict) -> dict:
        """Bind a queue input to its graph-edit companion by input ID (never by path or JSON shape), or mark it ordinary.

        ``row['edit']`` is the verified binding or None; ``row['edit_error']`` records a broken binding (a reserved path with no
        or a mismatched companion), which refuses the input instead of treating it as ordinary text. The graph-edit storage is a
        required part of the composed schema: a database without it raises the composition error here, before any Context or
        model work, and is never read as a queue of ordinary inputs."""
        row["edit"], row["edit_error"] = None, None
        try:
            with self.database.read() as uow:
                row["edit"] = context_graph_edits.load(uow, row["id"])
        except DomainError as error:
            if error.code not in context_graph_edits.BINDING_ERRORS:
                raise
            row["edit_error"] = {"code": error.code, "message": error.message}
        return row

    def _reserved_ids(self) -> set:
        """Context event IDs graph edits have reserved: ordinary allocation never uses them."""
        with self.database.read() as uow:
            return context_graph_edits.reserved_event_ids(uow)

    # ------------------------------------------------------------------ the public steps

    @contextmanager
    def _driver_lock(self):
        """One driver per Context across processes; yields False when another holds it."""
        runtime = self.context.runtime
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(runtime / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def process_next(self) -> dict:
        """Advance the oldest unfinished input by one step, never holding a database transaction over Context.

        ``status`` is one of ``idle``, ``busy`` (another process holds the driver, or Context reports a transient writer
        conflict: no attempt or failure is invented), ``blocked`` (the head is blocked and no retry is pending; later
        inputs wait), ``not_ready`` (no eligible coordinator: nothing started, or a publication awaits recording),
        ``published``, ``input_blocked``, ``unrecorded`` (published, SQLite recording failed; recovery records it),
        ``discarded`` or ``discard_failed``."""
        return self._remember(self._step(attempt=True))

    def recover(self) -> dict:
        """Mechanical startup reconciliation: record a published exact input, block an interrupted unpublished attempt for
        explicit retry or discard, finish a requested discard. It never starts a model run or a new attempt."""
        return self._remember(self._step(attempt=False))

    def _remember(self, result: dict) -> dict:
        self.last = result
        return result

    def _step(self, *, attempt: bool) -> dict:
        with self._driver_lock() as held:
            if not held:
                return {"status": "busy", "reason": "driver_locked"}
            self.reconcile()
            head = self._read(transitions.head)
            if head is None:
                return {"status": "idle"}
            self._attach(head)
            state = head["state"]
            if state == "processing":
                # The lock proves no live driver owns it: an earlier driver was interrupted.
                return self._reconcile_interrupted(head)
            if state == "discard_requested":
                return self._complete_discard(head)
            if state == "blocked" and not any(c["intent"] == "retry" and c["status"] == "pending" for c in head["controls"]):
                return {"status": "blocked", "input_id": head["id"], "attempt": head["attempt"]}
            if not attempt:
                return {"status": "unchanged", "input_id": head["id"], "state": state}
            return self._run_attempt(head)

    def reconcile(self) -> dict:
        """Mechanical completion reconciliation: one transaction, no Context, Git, model or provider work.

        It records the receipt of every final bound input that has none, marks a receipt whose bound job was cancelled or
        whose frozen definition is no longer registered as a visible conflict, and discards the unfinished input of a
        cancelled job through the queue's discard control (rejecting any retry still pending for it, so no new attempt starts).
        A published or processing input is never touched by the last."""
        with self.database.transaction(operation="context_queue_driver_reconcile") as uow:
            recorded = context_filing.outcomes.record_missing(uow, driver_id=self.driver_id)
            stopped = completion.reconcile_stopped(uow, driver_id=self.driver_id, outcomes=self.outcomes)
            conflicts = completion.verify_pending(uow, driver_id=self.driver_id, programs=self.programs)
        return {"recorded": len(recorded), "discarded_for_cancelled_job": len(stopped), "conflicts": len(conflicts)}

    # ------------------------------------------------------------------ one attempt

    def _run_attempt(self, head: dict) -> dict:
        coordinator = self.coordinator()
        if coordinator is None:
            return {"status": "not_ready", "reason": "no_coordinator", "input_id": head["id"]}
        persisted = self._read(transitions.used_event_ids) | self._reserved_ids()
        try:
            snapshot, pending = self.context.snapshot(), self.context.pending()
            self._refuse_broken_edit(head)
            event_id = self._event_id_for(head, persisted, snapshot, pending)
            exists = self._source_exists(head, snapshot)
            self._gate_edit(head, event_id, snapshot, pending)
        except Exception as error:
            if writer_busy(error):
                return {"status": "busy", "reason": "context_busy", "input_id": head["id"]}   # no attempt is invented
            return self._refuse(head, {**describe(error), "phase": "preflight"}, coordinator)
        started, refusal = self._start(head, event_id, exists, coordinator)
        if refusal is not None:
            return refusal
        started["edit"], started["edit_error"] = head["edit"], head["edit_error"]
        try:
            receipt = self._submit(started, event_id)
        except Exception as error:
            return self._after_failure(started, error)
        return self._finish(started, receipt)

    def _start(self, head: dict, event_id: str, exists: bool, coordinator: str) -> tuple[dict | None, dict | None]:
        """Bind the source to the input's scope and start the attempt with its reserved event ID: one transaction, so a
        refusal leaves nothing half-started. An ownership refusal binds nothing and starts nothing: it is recorded as the
        queue's preflight failure (no event ID is allocated for it) and returned instead of a started attempt."""
        with self.database.transaction(operation="context_queue_driver_start") as uow:
            try:
                context_filing.reserve_source(uow, driver_id=self.driver_id, input_id=head["id"], exists=exists)
            except DomainError as refused:
                if refused.code not in (context_filing.SCOPE_CONFLICT, context_filing.UNBOUND):
                    raise
                problem = {"code": refused.code, "message": refused.message, "phase": "ownership", "retryable": False}
                blocked = transitions.record_preflight_failure(uow, driver_id=self.driver_id, input_id=head["id"], error=problem,
                                                               ordubashy_id=coordinator)
                return None, {"status": "input_blocked", "input_id": head["id"], "attempt": blocked["attempt"],
                              "context_event_id": blocked["context_event_id"], "error": problem}
            return transitions.start_attempt(uow, driver_id=self.driver_id, input_id=head["id"], context_event_id=event_id), None

    def _source_exists(self, row: dict, snapshot) -> bool:
        payload = row["payload"]
        return payload["action_ref"] in snapshot.actions if row["input_kind"] == "action" else payload["path"] in snapshot.files

    def _refuse_broken_edit(self, head: dict) -> None:
        if head.get("edit_error") is not None:
            raise GraphEditRefused(head["edit_error"]["code"], head["edit_error"]["message"], "integrity")

    def _gate_edit(self, head: dict, event_id: str, snapshot, pending) -> None:
        """The gate before a fresh execution or explicit retry of a graph edit, once its event ID is known.

        If the exact event is already published, that real result is reconciled first (the library's exact-submission check has just
        verified it, and the result is verified again before it is recorded) whatever authority the caller holds now and whether or
        not the authority port is available: reporting an existing effect is not authorizing a new one. Otherwise missing wiring
        refuses the edit, and the typed port rechecks plan, base, frozen targets and the original caller's current registration;
        a refusal blocks the head."""
        if head.get("edit") is None:
            return
        if self._prior_event(head, event_id, snapshot, pending) == "published":
            return
        if self.graph_authority is None:
            raise GraphEditRefused("authority_unwired", "Graph-edit authority is not wired into the Context driver, so this "
                                   "edit cannot run", "authority")
        with self.database.read() as uow:
            self.graph_authority.recheck(uow, head["edit"], snapshot)

    def _event_id_for(self, head: dict, persisted: set[str], snapshot, pending) -> str:
        edit = head.get("edit")
        if edit is not None:
            # A graph edit has exactly its reserved ID, assigned at its first attempt. Nothing is reallocated: a collision
            # (the ID is used by something that is not this edit) or a mismatch blocks the input.
            reserved = edit["reserved_event_id"]
            if head["context_event_id"] not in (None, reserved):
                raise Conflict("The input's Context event ID is not the one reserved for this graph edit")
            if self._prior_event(head, reserved, snapshot, pending) == "collision":
                raise Conflict("The Context event ID reserved for this graph edit is used by something that is not this edit; "
                               "discard it and submit a newly reviewed plan")
            return reserved
        existing = head["context_event_id"]
        if existing is not None and self._prior_event(head, existing, snapshot, pending) != "collision":
            return existing
        return self._unused_event_id(persisted | set(snapshot.used_ids) | set(pending["reserved_ids"] if pending else ()))

    def _unused_event_id(self, reserved: set[str]) -> str:
        for _ in range(1024):
            candidate = self._new_event_id()
            if _EVENT_ID.fullmatch(candidate) and candidate not in reserved:
                return candidate
        raise Conflict("Could not allocate an unused five-character Context event ID")

    def _prior_event(self, head: dict, event_id: str, snapshot, pending) -> str:
        """Classify a persisted event ID without a model run: ``published``, ``pending`` (ours), ``absent`` or ``collision``
        (the ID is used by something that is not this exact input)."""
        if event_id in snapshot.events or (pending and pending["id"] == event_id):
            try:
                self._begin(head, event_id)         # Context verifies the exact digest of an existing event
            except Conflict as error:
                if writer_busy(error):
                    raise
                return "collision"
            return "published" if event_id in snapshot.events else "pending"
        return "collision" if event_id in snapshot.used_ids else "absent"

    def _begin(self, row: dict, event_id: str):
        positional, keywords = submission_of(row, event_id)
        begin = (self.context.begin_edit if row.get("edit") is not None else
                 self.context.begin_action if row["input_kind"] == "action" else self.context.begin)
        return begin(*positional, source_bytes=self.pipeline.config.source_bytes, **keywords)

    def _submit(self, row: dict, event_id: str) -> dict:
        """The exact stored input through the Pipeline: the only place a model can run."""
        positional, keywords = submission_of(row, event_id)
        submit = (self.pipeline.submit_edit if row.get("edit") is not None else
                  self.pipeline.submit_action if row["input_kind"] == "action" else self.pipeline.submit)
        return submit(*positional, **keywords)

    # ------------------------------------------------------------------ outcomes

    def _published(self, event_id: str):
        """The published ``(event, commit)`` of an exact event ID, or None. A pure lookup: no runner, no staging."""
        snapshot = self.context.snapshot()
        event = snapshot.events.get(event_id)
        return None if event is None else (event, snapshot.event_commits[event_id])

    def _verified(self, row: dict, event_id: str):
        """The published event and commit if, and only if, they are exactly this frozen input."""
        found = self._published(event_id)
        if found is None:
            raise Conflict("The Context event is not published")
        event, commit = found
        self._begin(row, event_id)                  # Context's own exact submission-digest check (text, tags, evidence)
        payload, limit = row["payload"], self.pipeline.config.source_bytes
        if row.get("edit") is not None:
            matches = self._edit_matches(row, event)
        elif row["input_kind"] == "action":
            action = event.get("action") or {}
            matches = (event.get("input_mode") == modes.ACTION and action.get("action_ref") == payload["action_ref"]
                       and action.get("revision_ref") == payload["revision_ref"]
                       and action.get("tags") == modes.tag_snapshots(payload["tags"])
                       and action.get("pointers") == modes.pointers(payload["related_objects"]))
        else:
            content = payload["content"]
            digest = None if content is None else hashlib.sha256(content.encode("utf-8")).hexdigest()
            source = event.get("source") or {}
            metadata = payload["metadata"] or {}
            matches = (event.get("input_mode") == modes.GENERAL and source.get("path") == payload["path"]
                       and event.get("input_digest") == digest
                       and all(source.get(name) == value for name, value in metadata.items()))
        if (not matches or event.get("operation") != payload["operation"] or event.get("submitted_by") != payload["submitted_by"]
                or event.get("record_context") != modes.record_context(record_context_of(row), limit)):
            raise Conflict("The published Context event is not this exact input")
        note = event.get("handoff_note")
        if (not isinstance(note, dict) or note.get("disposition") not in ("prepared", "empty")
                or not isinstance(note.get("text"), str) or (note["disposition"] == "prepared") != bool(note["text"])):
            raise Conflict("The published Context event has no explicit handoff disposition")
        if row.get("edit") is not None and note != library.HANDOFF:
            raise Conflict("A published graph edit carries the explicit empty handoff")
        return event, commit

    @staticmethod
    def _edit_matches(row: dict, event: dict) -> bool:
        """A published event is this graph edit only with its deterministic marker, plan digest, reserved path, fixed metadata,
        canonical source bytes and fixed base (the library has already checked the recorded changes against the plan)."""
        plan = edit_plan(row)
        source = event.get("source") or {}
        return (event.get("input_mode") == modes.GENERAL and event.get("operation") == "add"
                and event.get("execution") == library.marker(plan)
                and source.get("path") == library.source_path(event["id"]) == row["payload"]["path"]
                and {key: value for key, value in source.items() if key not in ("id", "path")} == library.METADATA
                and event.get("input_digest") == hashlib.sha256(library.canonical(plan)).hexdigest()
                and event.get("base_revision") == plan["expected_revision"])

    def _settle(self, event_id: str) -> None:
        """Mechanical Context completion of a published event whose pending record was left behind (checkout, index and
        cleanup). It never starts a model and never touches a pending record of any other event."""
        pending = self.context.pending()
        if pending is not None and pending["id"] == event_id:
            self.pipeline.recover()

    @staticmethod
    def _receipt(event_id: str, commit: str, receipt: dict | None) -> dict:
        """The bounded publication receipt kept with the input: no local runtime paths and no model transcript."""
        kept = ("source_id", "base_revision", "input_mode", "handoff_disposition", "action_ref", "association_count",
                "execution", "result_counts", "model", "recovered")
        return {"event_id": event_id, "published_commit": commit,
                **({name: receipt[name] for name in kept if name in receipt} if receipt else {"recovered": True})}

    def _finish(self, row: dict, receipt: dict | None) -> dict:
        """Verify the published event of a processing input and record it once."""
        event_id = row["context_event_id"]
        try:
            event, commit = self._verified(row, event_id)
            if receipt is not None and (receipt.get("event_id") != event_id or receipt.get("published_commit") != commit):
                raise Conflict("The Pipeline receipt does not match the published Context event")
        except Exception as error:
            if writer_busy(error):
                return {"status": "unrecorded", "reason": "context_busy", "input_id": row["id"], "context_event_id": event_id}
            if not isinstance(error, Conflict):
                return {"status": "unrecorded", "reason": "verification_unavailable", "input_id": row["id"],
                        "context_event_id": event_id, "error": describe(error)}
            return self._block(row, {"code": "event_conflict", "message": str(error), "retryable": True})
        note = {"disposition": event["handoff_note"]["disposition"], "text": event["handoff_note"]["text"]}
        coordinator = self.coordinator()
        pending = {"input_id": row["id"], "context_event_id": event_id, "published_commit": commit, "handoff_note": note}
        try:
            self._settle(event_id)
        except Exception as error:      # the publication is intact; the next step finishes Context's own cleanup first
            return {"status": "unrecorded", "reason": "busy" if writer_busy(error) else "context_settle_failed",
                    "error": describe(error), **pending}
        if coordinator is None and note["disposition"] == "prepared":
            return {"status": "not_ready", "reason": "no_coordinator", "pending_recovery": True, **pending}
        result = {"context_event_id": event_id, "published_commit": commit, "source_id": event["source"]["id"],
                  "base_revision": event["base_revision"], "input_mode": event["input_mode"],
                  **({"execution": event["execution"]} if "execution" in event else {}),
                  "receipt": self._receipt(event_id, commit, receipt)}
        try:
            self._write(transitions.record_published, input_id=row["id"], result=result, handoff_note=note,
                        ordubashy_id=coordinator or "", outcomes=self.outcomes)
        except DomainError as error:
            if error.code == "validation_error" and note["disposition"] == "prepared":
                return {"status": "not_ready", "reason": "coordinator_ineligible", "pending_recovery": True, **pending}
            return {"status": "unrecorded", "reason": error.code, "error": {"code": error.code, "message": error.message}, **pending}
        except Exception as error:      # SQLite could not complete: the publication stays intact for mechanical recovery
            return {"status": "unrecorded", "reason": "record_failed", "error": describe(error), **pending}
        return {"status": "published", **pending}

    def _after_failure(self, started: dict, error: Exception) -> dict:
        """A submit raised. Whether Context published is unknown until the exact event ID is looked up: a publication that
        exists is recorded, never blocked; a lookup that cannot read Context proves nothing, so the processing attempt and
        its reserved event stay as they are (no blocked notice, no new attempt) until a later step can read Context; only a
        successful lookup that finds nothing blocks the unpublished attempt for explicit retry or discard."""
        problem = describe(error)
        try:
            published = self._published(started["context_event_id"]) is not None
        except Exception as unreadable:
            return {"status": "unrecorded", "reason": "context_unavailable", "input_id": started["id"], "attempt": started["attempt"],
                    "context_event_id": started["context_event_id"], "error": problem, "lookup_error": describe(unreadable)}
        if published:
            return self._finish(started, None)
        return self._block(started, problem)

    def _block(self, row: dict, problem: dict) -> dict:
        problem = {**problem, "message": self._scrub(problem.get("message") or "Context attempt failed")}
        coordinator = self.coordinator()
        if coordinator is None:
            return {"status": "not_ready", "reason": "no_coordinator", "input_id": row["id"], "attempt": row["attempt"],
                    "context_event_id": row["context_event_id"], "error": problem}
        try:
            self._write(transitions.record_blocked, input_id=row["id"], error=problem, ordubashy_id=coordinator)
        except DomainError as error:
            if error.code == "validation_error":
                return {"status": "not_ready", "reason": "coordinator_ineligible", "input_id": row["id"], "error": problem}
            raise
        return {"status": "input_blocked", "input_id": row["id"], "attempt": row["attempt"],
                "context_event_id": row["context_event_id"], "error": problem}

    def _refuse(self, head: dict, problem: dict, coordinator: str) -> dict:
        """Block the head because Context could not be checked before an attempt could safely start: the queue's preflight
        failure. It allocates no event ID (a first failure has none; a retry of an input that had one keeps it) and starts
        no Context work."""
        problem = {**problem, "message": self._scrub(problem.get("message") or "preflight failed")}
        blocked = self._write(transitions.record_preflight_failure, input_id=head["id"], error=problem, ordubashy_id=coordinator)
        return {"status": "input_blocked", "input_id": head["id"], "attempt": blocked["attempt"],
                "context_event_id": blocked["context_event_id"], "error": problem}

    def _scrub(self, message: str) -> str:
        for path in (self.context.runtime, self.context.root):
            message = message.replace(str(path), "[context]")
        return message

    # ------------------------------------------------------------------ recovery and discard

    def _reconcile_interrupted(self, head: dict) -> dict:
        """A processing head with no live driver. Never starts a model: record the exact publication or block."""
        event_id = head["context_event_id"]
        try:
            published = self._published(event_id)
        except Exception as error:      # Context cannot be read now: nothing is concluded about the attempt
            return {"status": "busy", "reason": "context_unavailable", "input_id": head["id"], "error": describe(error)}
        if published is not None:
            return {**self._finish(head, None), "recovered": True}
        try:
            pending = self.context.pending()
            retained = bool(pending and pending["id"] == event_id)
        except Exception:
            retained = None
        return self._block(head, {
            "code": "interrupted",
            "message": f"Attempt {head['attempt']} for Context event {event_id} stopped before a verified publication and its "
                       "effect is uncertain; retry the same event or discard it",
            "draft_retained": retained})

    def _complete_discard(self, head: dict) -> dict:
        """Finish a requested discard mechanically. A published exact event is never discarded."""
        event_id = head["context_event_id"]
        if event_id is None:
            # Blocked before any Context event was allocated: there is no draft and Context is not touched.
            self._write(transitions.record_discarded, input_id=head["id"],
                        result={"context_event_id": None, "context": "no_event", "mechanical": True}, outcomes=self.outcomes)
            return {"status": "discarded", "input_id": head["id"], "context": "no_event"}
        try:
            snapshot, pending = self.context.snapshot(), self.context.pending()
            kind = self._prior_event(head, event_id, snapshot, pending)
            if kind == "published":
                # Published Context is never rewritten; the request closes as published (or stays pending recovery).
                return {**self._finish(head, None), "discard": "rejected"}
            if kind == "pending":
                self.pipeline.discard(event_id)
                outcome = "draft_discarded"
            elif kind == "collision":
                outcome = "foreign_event_id"        # the ID belongs to something that is not this input: none of ours exists
            else:
                outcome = "no_draft"
        except Exception as error:
            problem = describe(error)
            if writer_busy(error):
                return {"status": "busy", "reason": "context_busy", "input_id": head["id"]}
            self._write(transitions.record_discard_failure, input_id=head["id"], error=problem)
            return {"status": "discard_failed", "input_id": head["id"], "error": problem}
        self._write(transitions.record_discarded, input_id=head["id"],
                    result={"context_event_id": event_id, "context": outcome, "mechanical": True}, outcomes=self.outcomes)
        return {"status": "discarded", "input_id": head["id"], "context": outcome}

    # ------------------------------------------------------------------ diagnostics

    def diagnostics(self) -> dict:
        """A bounded, side-effect-free view for the host: head, coordinator readiness and the last step's outcome."""
        head = self._read(transitions.head)
        return {"driver_id": self.driver_id, "coordinator_ready": self.coordinator() is not None,
                "head": None if head is None else {key: head[key] for key in
                                                    ("id", "input_kind", "operation", "state", "attempt", "context_event_id", "error")},
                "last": self.last}
