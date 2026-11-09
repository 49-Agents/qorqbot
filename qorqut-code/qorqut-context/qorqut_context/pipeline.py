"""Durable source-to-graph orchestration; only a completed, sealed turn can publish."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import uuid

from . import bootstrap, controlled, usage
from .engine import atomic_write, file_lock
from .runner import AppServerRunner, RunnerConfig
from .schema import Conflict, ContextError, RECORD_KINDS, digest, encode, identifier
from .tools import EventTools
from .input_modes import GENERAL, ACTION


INSTRUCTIONS = (Path(__file__).parent / "prompts/propagator.md").read_text()
MODE_INSTRUCTIONS = {mode: (Path(__file__).parent / "prompts" / filename).read_text()
                     for mode, filename in ((GENERAL, "general_input.md"), (ACTION, "operational_action.md"))}


class PropagationError(ContextError):
    def __init__(self, event_id, phase, reason, diagnostics=None):
        super().__init__(reason)
        self.event_id, self.phase, self.diagnostics = event_id, phase, diagnostics

    def as_dict(self):
        return {"event_id": self.event_id, "phase": self.phase, "reason": str(self),
                "diagnostics": self.diagnostics}


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


class Pipeline:
    def __init__(self, context, *, runner=None, config=None, rng=None, on_event=None):
        self.context = context
        self.config = config or getattr(runner, "config", None) or RunnerConfig()
        self.runner = runner or AppServerRunner(self.config)
        if getattr(self.runner, "config", self.config) != self.config:
            raise ContextError("Runner and pipeline configuration must match")
        self.rng, self.on_event = rng, on_event

    @contextmanager
    def _lock(self):
        coordination = self.context.git.common_directory() / "qorqut-context"
        coordination.mkdir(parents=True, exist_ok=True)
        with file_lock(coordination / "propagation.lock"):
            # Preserve Context's repository -> runtime lock/ownership ordering.
            self.context.pending()
            private_directory(self.context.runtime)
            yield

    def _text(self, content):
        if content is None:
            return None
        if not isinstance(content, bytes):
            raise ContextError("Source input must be bytes")
        if len(content) > self.config.source_bytes:
            raise ContextError("Source input exceeds the configured byte limit")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextError("Source input must be model-readable UTF-8") from error

    def submit(self, operation, path, *, content=None, metadata=None, submitted_by, event_id=None,
               record_context=None, expected_revision=None):
        with self._lock():
            snapshot = self.context.snapshot()
            # Repeated published submissions retain their digest check without a new model limit.
            if event_id not in snapshot.events:
                self._text(content)
                if operation in ("edit", "remove"):
                    self._text(snapshot.files.get(path))
            submission = self.context.begin(operation, path, content=content, metadata=metadata,
                                             submitted_by=submitted_by, event_id=event_id,
                                             source_bytes=self.config.source_bytes,
                                             record_context=record_context,
                                             expected_revision=expected_revision)
            if self.on_event:
                self.on_event(submission.event_id)
            return self._run(submission.event_id)

    def submit_edit(self, plan, *, submitted_by, event_id, record_context=None):
        """Execute a fixed ``qorqut-graph-edit-v1`` plan as one controlled GENERAL event; trusted callers only.

        No model, bootstrap, attempt or usage receipt is involved in admission, retry or recovery; the
        published receipt carries the deterministic execution marker instead (see CORE_CONTRACTS.md).
        """
        with self._lock():
            submission = self.context.begin_edit(plan, submitted_by=submitted_by, event_id=event_id,
                source_bytes=self.config.source_bytes, record_context=record_context)
            if self.on_event:
                self.on_event(submission.event_id)
            return self._run(submission.event_id)

    def preview_supervised(self, plan, *, content, submitted_by, event_id, record_context=None):
        """Validate and describe a supervised proposal against the published head; nothing is written or run."""
        return self.context.preview_supervised(plan, content=content, submitted_by=submitted_by, event_id=event_id,
                                               source_bytes=self.config.source_bytes, record_context=record_context)

    def submit_supervised(self, plan, *, content, submitted_by, event_id, record_context=None):
        """Execute a ``qorqut-supervised-edit-v1`` batch with its supporting Markdown document; trusted callers only.

        ``content`` is preserved exactly as ``foundation/supervised-edits/{event_id}.md``. No model, bootstrap,
        attempt or usage receipt is involved in admission, retry or recovery; the published receipt carries the
        deterministic execution marker (normalized plan and document digest) and ``model: null``. A stale
        ``expected_revision`` is refused, never rebased.
        """
        with self._lock():
            submission = self.context.begin_supervised(plan, content=content, submitted_by=submitted_by,
                event_id=event_id, source_bytes=self.config.source_bytes, record_context=record_context)
            if self.on_event:
                self.on_event(submission.event_id)
            return self._run(submission.event_id)

    def question_output(self, event_id):
        """``{event_id, published_commit, questions}`` for a published event; refused for any other event."""
        return self.context.question_output(event_id)

    def acknowledge_question_output(self, event_id, published_commit):
        """Remove the delivered runtime payload of a published event; repeating it changes nothing."""
        return self.context.acknowledge_question_output(event_id, published_commit)

    def submit_action(self, operation, action_ref, *, text=None, revision_ref=None,
                      related_objects=None, tags=None, submitted_by, event_id=None,
                      expected_previous_commit=None, record_context=None):
        with self._lock():
            submission = self.context.begin_action(operation, action_ref, text=text,
                revision_ref=revision_ref, related_objects=related_objects,
                tags=tags, submitted_by=submitted_by,
                event_id=event_id, expected_previous_commit=expected_previous_commit,
                source_bytes=self.config.source_bytes, record_context=record_context)
            if self.on_event:
                self.on_event(submission.event_id)
            return self._run(submission.event_id)

    def retry(self, event_id):
        identifier(event_id)
        with self._lock():
            if self.on_event:
                self.on_event(event_id)
            return self._run(event_id)

    def recover(self):
        """Mechanical recovery only; never starts a model or publishes an unready draft."""
        with self._lock():
            return self.context.recover()

    def discard(self, event_id):
        with self._lock():
            self.context.discard(event_id)
            return {"status": "discarded", "event_id": event_id}

    def _attempts(self, event_id):
        root = self.context.runtime / "runs" / identifier(event_id)
        if not root.exists():
            return []
        result = []
        for path in sorted(root.glob("*/attempt.json")):
            try:
                value = json.loads(path.read_bytes())
            except (OSError, ValueError) as error:
                raise Conflict("Invalid durable propagation attempt; retain it for inspection") from error
            if value.get("event_id") != event_id:
                raise Conflict("Durable propagation attempt belongs to a different event")
            result.append((path.parent, value))
        return sorted(result, key=lambda item: item[1]["sequence"])

    def usage_receipts(self, event_id, *, limit=100, cursor=None):
        """A bounded page of the private per-call usage receipts of one event, in attempt/stage order.

        Read-only: takes no lock, creates nothing, starts nothing. Returns
        ``{status, event_id, items, next_cursor, error}``; see CORE_CONTRACTS.md for the item DTO.
        """
        identifier(event_id)
        return usage.page(self.context.runtime / "runs", event_id, limit=limit, cursor=cursor)

    def pending(self):
        job = self.context.pending()
        attempts = self._attempts(job["id"]) if job else []
        return {"pending": job, "attempt": attempts[-1][1] if attempts else None,
                "diagnostics": str(attempts[-1][0]) if attempts else None}

    def _receipt(self, event_id, *, directory=None, recovered=False):
        snapshot = self.context.snapshot()
        event = snapshot.event(event_id)
        # Deterministic work has no model attempt; stray attempt data must not label it as model work.
        attempts = [] if "execution" in event else self._attempts(event_id)
        if directory is None and attempts:
            directory = attempts[-1][0]
        counts = {kind: {op: 0 for op in ("add", "edit", "remove")} for kind in RECORD_KINDS}
        for change in event["changes"]:
            counts[change["kind"]][change["operation"]] += 1
        receipt = {"status": "published", "event_id": event_id, "source_id": event["source"]["id"],
                   "base_revision": event["base_revision"], "published_commit": event["commit"],
                   "input_mode": event["input_mode"],
                   **({"handoff_disposition": event["handoff_note"]["disposition"]} if "handoff_note" in event else {}),
                   **({"action_ref": event["action"]["action_ref"],
                       "association_count": len(event["action_associations"]["associations"])} if "action" in event else {}),
                   **({"execution": event["execution"]} if "execution" in event else {}),
                   "result_counts": counts, "model": attempts[-1][1]["model"] if attempts else None,
                   "diagnostics": str(directory) if directory else None, "recovered": recovered}
        if attempts:
            receipt["model_execution"] = attempts[-1][1].get("identities", {}).get("propagation")
        if directory:
            atomic_write(directory / "receipt.json", encode(receipt))
        return receipt

    def _source_change(self, draft):
        snapshot = self.context.snapshot(draft["base_revision"])
        before = snapshot.files.get(draft["source"]["path"])
        after = (None if draft["operation"] == "remove" else
                 (self.context.runtime / "events" / draft["event_id"] / "input.bin").read_bytes())
        if (hashlib.sha256(after).hexdigest() if after is not None else None) != draft["input_digest"]:
            raise Conflict("The durable source input changed after submission")
        change = {"event_id": draft["event_id"], "base_revision": snapshot.revision,
                  "input_mode": draft["input_mode"], "previous_source_event": draft["previous_source_event"],
                  "operation": draft["operation"], "source": draft["source"],
                  "before": self._text(before), "after": self._text(after)}
        if "record_context" in draft:
            change["record_context"] = draft["record_context"]
        if "action" in draft:
            action = draft["action"]
            change["action"] = action
            change["pointer_resolution"] = [{**item, "status":
                "current" if item["id"] in snapshot.objects(item["kind"]) else
                "historical" if (item["kind"], item["id"]) in snapshot.known_objects else "unresolved"}
                for item in action["pointers"]]
            change["previous_action"] = (snapshot.action(action["action_ref"])
                                         if action["action_ref"] in snapshot.actions else None)
        return change

    def _checkpoint(self, phase):
        """Fault-injection seam; never alters execution policy."""

    def _run_edit(self, event_id, plan, mark):
        """Stage, validate and publish a validated controlled plan with the ordinary primitives; never a model.

        Entered only from the pending job's validated execution marker. A partial draft left by an
        interruption is completed for the same plan; any draft content outside the plan is refused.
        """
        mark("edit_staging")
        module = controlled.module_for(plan)
        if self._attempts(event_id):
            raise Conflict(f"A deterministic {module.NOUN} has no model attempt; the unexpected evidence was retained")
        expected = module.draft(plan)
        staged = self.context.inspect_draft(event_id)["draft"]
        for key, entry in staged.items():
            if key not in expected or encode(entry) != encode(expected[key]):
                raise Conflict(f"The pending draft has content outside its bound {module.NOUN}; it was retained")
        for operation in plan["operations"]:
            if f"{module.kind(operation['op'])}:{module.object_id(operation)}" not in staged:
                module.stage(self.context, event_id, operation)
                self._checkpoint("edit_staged")
        self.context.stage_handoff_note(event_id, "")
        mark("edit_validation")
        current = self.context.inspect_draft(event_id, validate=True)
        mark("publication")
        self.context.publish(event_id, expected_draft_digest=current["digest"])
        self._checkpoint("after_publication")
        return self._receipt(event_id)

    def _run(self, event_id):
        phase, directory, attempt = "recovery", None, None
        try:
            snapshot, pending = self.context.snapshot(), self.context.pending()
            if event_id in snapshot.events:
                if pending is None or pending["id"] == event_id:
                    self.context.recover()
                return self._receipt(event_id, recovered=True)
            if pending is None or pending["id"] != event_id:
                raise Conflict("No matching pending event to retry")
            recovered = self.context.recover()
            if recovered["status"] == "discarded":
                raise Conflict("This event was explicitly discarded")
            draft = self.context.inspect_draft(event_id)
            plan = self.context.pending_edit(event_id)
            if plan is not None:
                def mark(value):
                    nonlocal phase
                    phase = value
                return self._run_edit(event_id, plan, mark)
            previous = self._attempts(event_id)
            # Any ready record is a publication gate, even if the caller changed model config.
            for saved_directory, saved in reversed(previous):
                ready_path = saved_directory / "ready.json"
                if not ready_path.exists():
                    continue
                ready = json.loads(ready_path.read_bytes())
                if (ready.get("event_id") != event_id or ready.get("base_revision") != draft["base_revision"] or
                        ready.get("submission_digest") != draft["submission_digest"] or
                        ready.get("input_digest") != draft["input_digest"] or
                        ready.get("turn_status") != "completed" or
                        ready.get("completion", {}).get("status") != "complete" or
                        ready.get("completion", {}).get("draft_digest") != ready.get("draft_digest") or
                        ready.get("draft_digest") != draft["digest"]):
                    raise Conflict("Ready propagation record does not match this exact draft/input/base")
                directory, attempt, phase = saved_directory, saved, "publication"
                self.context.publish(event_id, expected_draft_digest=ready["draft_digest"])
                attempt.update(stage="published", error=None)
                atomic_write(directory / "attempt.json", encode(attempt))
                return self._receipt(event_id, directory=directory, recovered=True)

            phase = "preparation"
            self.context.reset_questions(event_id)
            source_change = self._source_change(draft)
            runs = private_directory(self.context.runtime / "runs")
            event_runs = private_directory(runs / event_id)
            attempt_id = uuid.uuid4().hex
            directory = private_directory(event_runs / attempt_id)
            instructions = INSTRUCTIONS + "\n\n" + MODE_INSTRUCTIONS[draft["input_mode"]]
            prompts = {"bootstrap": hashlib.sha256(bootstrap.INSTRUCTIONS.encode()).hexdigest(),
                       "propagation": hashlib.sha256(instructions.encode()).hexdigest()}
            bootstrap_key = digest({"base": draft["base_revision"], "submission": draft["submission_digest"],
                                    "input": draft["input_digest"], "prompt": prompts["bootstrap"],
                                    "configuration": asdict(self.config)})
            attempt = {"attempt_id": attempt_id, "event_id": event_id, "base_revision": draft["base_revision"],
                       "submission_digest": draft["submission_digest"], "input_digest": draft["input_digest"],
                       "input_mode": draft["input_mode"],
                       "prompt_digests": prompts, "model": self.config.identity(), "configuration": asdict(self.config),
                       "sequence": len(previous) + 1, "stage": phase, "error": None,
                       "bootstrap_key": bootstrap_key, "identities": {}, "completion": None,
                       "bootstrap_call_id": None}

            def save():
                atomic_write(directory / "attempt.json", encode(attempt))

            def identity(value):
                attempt["identities"][value["stage"]] = value
                save()

            save()
            # Each actual runner call is reserved durably before it is launched and settled after.
            calls = usage.AttemptCalls(self.runner, directory, attempt, self.config.identity())
            atomic_write(directory / "source-change.json", encode(source_change))
            tools = EventTools(self.context, event_id, max_response_bytes=self.config.tool_response_bytes)
            phase = attempt["stage"] = "bootstrap"
            save()
            sample = None
            for old_directory, old in reversed(previous):
                if old.get("bootstrap_key") == bootstrap_key and (old_directory / "bootstrap.json").exists():
                    sample = json.loads((old_directory / "bootstrap.json").read_bytes())
                    attempt["bootstrap_reused_from"] = old["attempt_id"]
                    # A reused sample references its original call and is not charged again.
                    calls.bootstrap_call_id = old.get("bootstrap_call_id")
                    break
            if sample is None:
                sample = bootstrap.prepare(tools.base, source_change, calls,
                    directory=directory / "bootstrap", max_payload_bytes=self.config.payload_bytes,
                    rng=self.rng, on_identity=identity)
            attempt["bootstrap_call_id"] = calls.bootstrap_call_id
            atomic_write(directory / "bootstrap.json", encode(sample))
            phase = attempt["stage"] = "propagation"
            save()
            existing = self.context.inspect_draft(event_id)["draft"]
            payload = {"input_mode": draft["input_mode"], "source_change": source_change, "bootstrap": sample,
                       "personal_context_ref": ("node:" + tools.base.personal_context_node
                                                if tools.base.personal_context_node else None),
                       "existing_mode_output": {key: draft[key] for key in ("handoff_note", "action_associations") if key in draft},
                       "required_reassessment": tools.required_reassessment,
                       "existing_draft": [{"kind": value["kind"], "id": value["id"],
                                           "removed": value["record"] is None} for _, value in sorted(existing.items())],
                       "catalog_discovery": "Use read_catalog for all node or edge attribute names and chosen categorical values; it includes staged vocabulary.",
                       "draft_discovery": "Use read_draft and read_object to inspect existing staged records/support before creating more objects."}
            result = calls.run(stage="propagation", instructions=instructions, payload=payload,
                               directory=directory / "propagation", tools=tools, on_identity=identity)
            attempt["completion"] = tools.completion
            attempt["turn_status"] = result.status
            save()
            if result.status != "completed" or result.model != self.config.model:
                raise ContextError("The requested propagation model turn did not complete successfully")
            if tools.completion is None or tools.completion["status"] != "complete":
                reason = tools.completion["summary"] if tools.completion else "Missing finish_propagation"
                raise ContextError("Propagation did not finish: " + reason)
            current = self.context.inspect_draft(event_id, validate=True)
            if current["digest"] != tools.completion["draft_digest"]:
                raise Conflict("The draft changed after finish_propagation")
            phase = attempt["stage"] = "ready"
            ready = {"event_id": event_id, "base_revision": draft["base_revision"],
                     "submission_digest": draft["submission_digest"], "input_digest": draft["input_digest"],
                     "draft_digest": current["digest"], "turn_status": result.status,
                     "thread_id": result.thread_id, "turn_id": result.turn_id, "completion": tools.completion}
            atomic_write(directory / "ready.json", encode(ready))
            save()
            self._checkpoint("after_ready")
            phase = attempt["stage"] = "publication"
            save()
            self.context.publish(event_id, expected_draft_digest=current["digest"])
            self._checkpoint("after_publication")
            attempt.update(stage="published", error=None)
            save()
            return self._receipt(event_id, directory=directory)
        except Exception as error:
            if attempt is not None:
                attempt.update(stage=phase, error={"type": type(error).__name__, "reason": str(error)})
                atomic_write(directory / "attempt.json", encode(attempt))
            raise PropagationError(event_id, phase, str(error), str(directory) if directory else None) from error
