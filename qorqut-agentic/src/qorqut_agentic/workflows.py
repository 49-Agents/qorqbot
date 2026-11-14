"""Explicit, persistent workflow progress; no scheduler, inference or provider calls.

Each run freezes its definition and referenced instruction bodies. SQLite write
transactions serialize progress; verifier execution takes place outside them.
The host remains responsible for authorization and human confirmation.

A plan run has no catalog entry: the host hands over an ordered list of step
instructions (``start_plan``). Its steps run one after another, a step may be
added while it runs (``add_step``) and a step may be skipped with a recorded
reason (``skip_step``). A skipped step counts as finished for the order of the
steps and for the end of the run.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .catalog import Selection, load_catalog
from .changes import normalize_actor
from .core import LayerContext
from .errors import (AgenticError, InvalidRequestError, NotFoundError,
                     OperationConflictError, StateConflictError, UnavailableError,
                     ValidationFailedError, VerificationBlockedError)
from .files import DiskSource
from .schemas import validate_instance
from .tools import Tools
from .util import check_operation_id, dumps, is_ref, json_digest, loads, new_id, sha256_hex

RUN_STATES = {"waiting_arguments", "running", "paused", "completed", "cancelled"}
# A step that needs no more work: done, or skipped with a recorded reason.
FINISHED_STEP_STATES = ("completed", "skipped")
# The ``workflow_ref`` of a plan run. It has no slash, so it can never name a catalog entry.
PLAN_REF = "task-plan"
PLAN_STEP_LIMIT = 50
PLAN_TEXT_LIMIT = 4000


class Workflows:
    def __init__(self, ctx: LayerContext, tools: Tools):
        self.ctx = ctx
        self.tools = tools

    def _snapshot(self, ref, selection):
        if not is_ref(ref):
            raise InvalidRequestError("ref must be package-id/entry-id")
        with self.ctx.store.lock(exclusive=False):
            self.ctx.require_no_recovery()
            catalog = load_catalog(DiskSource(self.ctx.tree))
            entry = catalog.get(ref)
            if entry is None:
                raise NotFoundError(f"no valid workflow entry {ref!r}", details={"ref": ref})
            if entry.kind != "workflow":
                raise InvalidRequestError(f"{ref} is not a workflow")
            available, diagnostics = catalog.availability(entry, selection)
            if not available:
                raise UnavailableError(f"workflow {ref} is unavailable", details={"diagnostics": diagnostics})
            definition = loads(dumps(entry.definition))
            for step in definition["steps"]:
                if "instruction_ref" in step:
                    instruction = catalog.get(step["instruction_ref"])
                    data = catalog.source.read(instruction.root_path)
                    if data is None or sha256_hex(data) != instruction.revision:
                        raise StateConflictError("instruction changed while taking workflow snapshot")
                    step["instructions"] = data.decode("utf-8")
                    step["instruction_revision"] = instruction.revision
                if "verifier" in step:
                    tool = self.tools.locate(catalog, step["verifier"], selection)
                    step["verifier_revision"] = tool.revision
                    step["verifier_code_revision"] = tool.code_revision
            return entry.revision, definition

    @staticmethod
    def _run(conn, run_id):
        check_operation_id(run_id, "run_id")
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"no workflow run {run_id!r}")
        return row

    @staticmethod
    def _step(snapshot, step_id):
        for step in snapshot["steps"]:
            if step["id"] == step_id:
                return step
        raise NotFoundError(f"no step {step_id!r}")

    def _event(self, conn, run_id, kind, actor, payload):
        ordinal = conn.execute("SELECT COALESCE(MAX(ordinal), 0) + 1 FROM run_events WHERE run_id = ?",
                               (run_id,)).fetchone()[0]
        conn.execute("INSERT INTO run_events VALUES (?, ?, ?, ?, ?, ?)",
                     (run_id, ordinal, self.ctx.now(), kind, dumps(actor), dumps(payload)))
        conn.execute("UPDATE runs SET updated_at = ? WHERE run_id = ?", (self.ctx.now(), run_id))

    @staticmethod
    def _fingerprint(kind, actor, **kwargs):
        try:
            return json_digest({"operation": kind, "actor": actor, **kwargs})
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(f"request must contain JSON values: {exc}") from exc

    @staticmethod
    def _op(conn, scope, operation_id, fingerprint):
        check_operation_id(operation_id)
        row = conn.execute("SELECT * FROM workflow_ops WHERE scope = ? AND operation_id = ?",
                           (scope, operation_id)).fetchone()
        if row is not None and row["fingerprint"] != fingerprint:
            raise OperationConflictError("operation ID was already used for a different request",
                                         details={"operation_id": operation_id})
        return row

    def _reserve(self, conn, scope, operation_id, fingerprint):
        conn.execute("INSERT INTO workflow_ops(scope, operation_id, fingerprint, created_at) VALUES (?, ?, ?, ?)",
                     (scope, operation_id, fingerprint, self.ctx.now()))

    def _result(self, conn, run_id, scope, operation_id):
        result = {"run": self._view(conn, run_id), "operation_id": operation_id}
        conn.execute("UPDATE workflow_ops SET result = ? WHERE scope = ? AND operation_id = ?",
                     (dumps(result), scope, operation_id))
        return result

    def _view(self, conn, run_id):
        row = self._run(conn, run_id)
        snapshot = loads(row["snapshot"])
        arguments = {}
        for argument in conn.execute("SELECT * FROM run_arguments WHERE run_id = ?", (run_id,)):
            arguments[argument["name"]] = {
                "value": loads(argument["value"]), "source": argument["source"], "note": argument["note"],
                "confirmed": bool(argument["confirmed"]), "locked": bool(argument["locked"]),
                "supplied_by": loads(argument["supplied_by"]), "at": argument["at"]}
        values = {name: item["value"] for name, item in arguments.items()}
        states = {step["step_id"]: dict(step) for step in
                  conn.execute("SELECT * FROM run_steps WHERE run_id = ?", (run_id,))}
        # ask_before_start always gates the whole run, even if its field names a step.
        missing_global = [arg for arg in snapshot.get("arguments", []) if arg["name"] not in arguments
                          and ("step" not in arg or arg["acquisition"] == "ask_before_start")]
        requests = []
        for arg in snapshot.get("arguments", []):
            if arg["name"] in arguments:
                continue
            if "step" in arg and arg["acquisition"] != "ask_before_start":
                step = self._step(snapshot, arg["step"])
                if not all(states[dep]["state"] in FINISHED_STEP_STATES for dep in step.get("depends_on", [])):
                    continue
                if missing_global:
                    continue
            request = {"name": arg["name"], "schema": arg["schema"], "prompt": arg["prompt"],
                       "acquisition": arg["acquisition"], "step": arg.get("step")}
            if "default" in arg:
                request["proposed_default"] = arg["default"]
            requests.append(request)
        steps, ready = [], []
        for definition in snapshot["steps"]:
            step = states[definition["id"]]
            step["id"] = step.pop("step_id")
            step.pop("run_id")
            for field in ("started_by", "completed_by", "output"):
                step[field] = loads(step[field])
            step["instructions"] = definition["instructions"]
            for field in ("instruction_ref", "instruction_revision", "verifier", "verifier_revision",
                          "verifier_code_revision"):
                if field in definition:
                    step[field] = definition[field]
            step["depends_on"] = definition.get("depends_on", [])
            if definition.get("added"):
                step["added"] = True
            step["attempts"] = []
            for attempt in conn.execute("SELECT * FROM run_attempts WHERE run_id = ? AND step_id = ? ORDER BY attempt_id",
                                        (run_id, step["id"])):
                item = dict(attempt)
                for field in ("actor", "output", "details"):
                    item[field] = loads(item[field])
                step["attempts"].append(item)
            dependencies_ready = all(states[dep]["state"] in FINISHED_STEP_STATES
                                     for dep in definition.get("depends_on", []))
            missing_local = any(arg["name"] not in arguments and arg.get("step") == step["id"]
                                for arg in snapshot.get("arguments", []))
            step["ready"] = (row["control"] == "running" and step["state"] not in FINISHED_STEP_STATES
                             and not missing_global and not missing_local and dependencies_ready
                             and not any(a["status"] == "verifying" for a in step["attempts"]))
            if step["ready"]:
                ready.append({"step_id": step["id"], "instructions": step["instructions"], "arguments": values})
            steps.append(step)
        state = row["control"]
        if state == "running" and missing_global:
            state = "waiting_arguments"
        return {"run_id": row["run_id"], "workflow_ref": row["workflow_ref"],
                "workflow_revision": row["workflow_revision"], "state": state, "actor": loads(row["actor"]),
                "selection": loads(row["selection"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "completed_at": row["completed_at"], "cancelled_at": row["cancelled_at"],
                "arguments": arguments, "steps": steps, "ready_steps": ready,
                "argument_requests": requests if state not in {"completed", "cancelled"} else [],
                "events": [{"ordinal": event["ordinal"], "at": event["at"], "type": event["type"],
                            "actor": loads(event["actor"]), "payload": loads(event["payload"])}
                           for event in conn.execute("SELECT * FROM run_events WHERE run_id = ? ORDER BY ordinal", (run_id,))]}

    def get(self, run_id):
        with self.ctx.store.reader() as conn:
            conn.execute("BEGIN")
            return {"run": self._view(conn, run_id)}

    def list(self, state=None, limit=100, offset=0, *, actor=None):
        """List run histories, optionally restricted to their creator before pagination.

        With no actor this preserves the standalone API's unrestricted behavior.
        Hosts derive the optional actor from their authenticated identity.
        """
        if state is not None and (not isinstance(state, str) or state not in RUN_STATES):
            raise InvalidRequestError("unknown workflow state")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise InvalidRequestError("limit must be an integer between 1 and 1000")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise InvalidRequestError("offset must be a nonnegative integer")
        creator = normalize_actor(actor) if actor is not None else None
        with self.ctx.store.reader() as conn:
            conn.execute("BEGIN")
            query = "SELECT run_id FROM runs"
            parameters = ()
            if creator is not None:
                query += " WHERE actor = ?"
                parameters = (dumps(creator),)
            runs = [self._view(conn, row["run_id"]) for row in conn.execute(
                query + " ORDER BY created_at, run_id", parameters)]
            if state is not None:
                runs = [run for run in runs if run["state"] == state]
            return {"runs": runs[offset:offset + limit], "total": len(runs)}

    @staticmethod
    def _argument_records(snapshot, arguments, actor, defaults=False):
        if arguments is None and defaults:
            arguments = {}
        if not isinstance(arguments, dict):
            raise InvalidRequestError("arguments must map names to value/source records")
        definitions = {arg["name"]: arg for arg in snapshot.get("arguments", [])}
        unknown = set(arguments) - set(definitions)
        if unknown:
            raise InvalidRequestError(f"unknown argument names: {sorted(unknown)}")
        records = {}
        for name, arg in definitions.items():
            if name in arguments:
                record = arguments[name]
                if (not isinstance(record, dict) or not {"value", "source"} <= set(record)
                        or set(record) - {"value", "source", "note", "confirmed"}):
                    raise InvalidRequestError(f"argument {name!r} needs value/source and optional note/confirmed")
                record = dict(record)
                if not isinstance(record["source"], str) or not record["source"].strip():
                    raise InvalidRequestError(f"argument {name!r} source must be a nonempty string")
                if "note" in record and not isinstance(record["note"], str):
                    raise InvalidRequestError(f"argument {name!r} note must be a string")
                if "confirmed" in record and not isinstance(record["confirmed"], bool):
                    raise InvalidRequestError(f"argument {name!r} confirmed must be a boolean")
                if arg["acquisition"] == "confirm_default" and record.get("confirmed") is not True:
                    raise ValidationFailedError(f"argument {name!r} requires explicit confirmation")
                if arg["acquisition"] == "derive" and not record.get("note", "").strip():
                    raise ValidationFailedError(f"argument {name!r} requires a derivation note")
                record["supplied_by"] = actor
            elif defaults and arg["acquisition"] == "default":
                record = {"value": arg["default"], "source": "default", "supplied_by": None}
            else:
                continue
            try:
                dumps(record["value"])
            except (TypeError, ValueError) as exc:
                raise InvalidRequestError(f"argument {name!r} is not a JSON value") from exc
            issues = validate_instance(record["value"], arg["schema"])
            if issues:
                raise ValidationFailedError(f"argument {name!r} violates its schema", details={"issues": issues})
            record.setdefault("confirmed", False)
            record.setdefault("note", None)
            records[name] = record
        return records

    def start(self, ref, actor, arguments=None, selection=None, operation_id=None):
        actor = normalize_actor(actor)
        chosen = Selection.from_value(selection)
        operation_id = new_id("op") if operation_id is None else operation_id
        fingerprint = self._fingerprint("start", actor, ref=ref, arguments=arguments,
                                        selection=chosen.to_dict())
        with self.ctx.store.reader() as conn:
            existing = self._op(conn, "start", operation_id, fingerprint)
            if existing is not None:
                return loads(existing["result"])
        revision, snapshot = self._snapshot(ref, chosen)
        records = self._argument_records(snapshot, arguments, actor, defaults=True)
        run_id, now = new_id("run"), self.ctx.now()
        self.ctx.require_no_check_mutation()
        with self.ctx.store.lock(exclusive=True), self.ctx.store.transaction() as conn:
            existing = self._op(conn, "start", operation_id, fingerprint)
            if existing is not None:
                return loads(existing["result"])
            self._reserve(conn, "start", operation_id, fingerprint)
            conn.execute("INSERT INTO runs(run_id, workflow_ref, workflow_revision, created_at, updated_at, actor, "
                         "control, selection, snapshot) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
                         (run_id, ref, revision, now, now, dumps(actor), dumps(chosen.to_dict()), dumps(snapshot)))
            for step in snapshot["steps"]:
                conn.execute("INSERT INTO run_steps(run_id, step_id, state, verification) VALUES (?, ?, 'pending', 'none')",
                             (run_id, step["id"]))
            self._write_arguments(conn, run_id, records)
            self._event(conn, run_id, "started", actor, {"operation_id": operation_id, "arguments": records})
            return self._result(conn, run_id, "start", operation_id)

    # -- plan runs ---------------------------------------------------------------

    @staticmethod
    def _plan_text(value, field):
        if not isinstance(value, str) or not value.strip() or len(value) > PLAN_TEXT_LIMIT:
            raise InvalidRequestError(f"{field} must be text of 1-{PLAN_TEXT_LIMIT} characters", details={"field": field})
        return value.strip()

    def start_plan(self, steps, actor, origin, operation_id):
        """Start a run from an ordered list of step instructions; each step waits for the one before it.

        ``origin`` names what the plan belongs to (the host's record) and is only stored. The operation ID is the
        host's key for the run: a repeated call with the same steps returns the same run.
        """
        actor = normalize_actor(actor)
        check_operation_id(operation_id)
        if not isinstance(origin, str) or not 1 <= len(origin) <= 500:
            raise InvalidRequestError("origin must be a string of 1-500 characters", details={"field": "origin"})
        if not isinstance(steps, list) or not 1 <= len(steps) <= PLAN_STEP_LIMIT:
            raise InvalidRequestError(f"a plan holds 1-{PLAN_STEP_LIMIT} steps", details={"field": "steps"})
        texts = [self._plan_text(text, "steps") for text in steps]
        definition = {"plan": True, "origin": origin, "steps": [
            {"id": f"s{number}", "instructions": text, **({"depends_on": [f"s{number - 1}"]} if number > 1 else {})}
            for number, text in enumerate(texts, 1)]}
        fingerprint = self._fingerprint("start_plan", actor, origin=origin, steps=texts)
        with self.ctx.store.reader() as conn:
            existing = self._op(conn, "start", operation_id, fingerprint)
            if existing is not None:
                return loads(existing["result"])
        run_id, now = new_id("run"), self.ctx.now()
        self.ctx.require_no_check_mutation()
        with self.ctx.store.lock(exclusive=True), self.ctx.store.transaction() as conn:
            existing = self._op(conn, "start", operation_id, fingerprint)
            if existing is not None:
                return loads(existing["result"])
            self._reserve(conn, "start", operation_id, fingerprint)
            conn.execute("INSERT INTO runs(run_id, workflow_ref, workflow_revision, created_at, updated_at, actor, "
                         "control, selection, snapshot) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)",
                         (run_id, PLAN_REF, json_digest(definition), now, now, dumps(actor),
                          dumps(Selection.from_value(None).to_dict()), dumps(definition)))
            for step in definition["steps"]:
                conn.execute("INSERT INTO run_steps(run_id, step_id, state, verification) VALUES (?, ?, 'pending', 'none')",
                             (run_id, step["id"]))
            self._event(conn, run_id, "started", actor, {"operation_id": operation_id, "origin": origin})
            return self._result(conn, run_id, "start", operation_id)

    def plan_run(self, operation_id):
        """The current state of the plan run started under this operation ID, or ``None`` before it was started."""
        check_operation_id(operation_id)
        with self.ctx.store.reader() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT result FROM workflow_ops WHERE scope = 'start' AND operation_id = ?",
                               (operation_id,)).fetchone()
            if row is None or row["result"] is None:
                return None
            return {"run": self._view(conn, loads(row["result"])["run"]["run_id"])}

    @staticmethod
    def _plan_snapshot(run):
        snapshot = loads(run["snapshot"])
        if snapshot.get("plan") is not True:
            raise StateConflictError("only a plan run takes added or skipped steps")
        return snapshot

    def add_step(self, run_id, instructions, actor, operation_id, after=None):
        """Add one step to a running plan, right after ``after`` (default: after the last step).

        The step that followed ``after`` now follows the new one, so it must not have started yet.
        """
        actor = normalize_actor(actor)
        text = self._plan_text(instructions, "instructions")
        if after is not None and not isinstance(after, str):
            raise InvalidRequestError("after must name a step", details={"field": "after"})
        fingerprint = self._fingerprint("add_step", actor, run_id=run_id, instructions=text, after=after)
        with self.ctx.store.lock(exclusive=True), self.ctx.store.transaction() as conn:
            self.ctx.guard_overseer_admission(conn, run_id=run_id)
            run = self._run(conn, run_id)
            op = self._op(conn, run_id, operation_id, fingerprint)
            if op is not None:
                return loads(op["result"])
            self._progress_allowed(run)
            snapshot = self._plan_snapshot(run)
            steps = snapshot["steps"]
            if len(steps) >= PLAN_STEP_LIMIT:
                raise StateConflictError(f"a plan holds at most {PLAN_STEP_LIMIT} steps")
            ids = [step["id"] for step in steps]
            anchor = ids[-1] if after is None else after
            if anchor not in ids:
                raise NotFoundError(f"no step {anchor!r}")
            index = ids.index(anchor)
            follower = steps[index + 1] if index + 1 < len(steps) else None
            if follower is not None:
                state = conn.execute("SELECT state FROM run_steps WHERE run_id = ? AND step_id = ?",
                                     (run_id, follower["id"])).fetchone()["state"]
                if state != "pending":
                    raise StateConflictError("a step can only be added before work that has not started",
                                             details={"step_id": follower["id"]})
            # Step IDs are never reused: the next number is one more than every step the run has ever held.
            new = {"id": f"s{len(steps) + 1}", "instructions": text, "depends_on": [anchor], "added": True}
            if follower is not None:
                follower["depends_on"] = [new["id"]]
            steps.insert(index + 1, new)
            self._reserve(conn, run_id, operation_id, fingerprint)
            conn.execute("UPDATE runs SET snapshot = ? WHERE run_id = ?", (dumps(snapshot), run_id))
            conn.execute("INSERT INTO run_steps(run_id, step_id, state, verification) VALUES (?, ?, 'pending', 'none')",
                         (run_id, new["id"]))
            self._event(conn, run_id, "step_added", actor,
                        {"operation_id": operation_id, "step_id": new["id"], "after": anchor})
            return self._result(conn, run_id, run_id, operation_id)

    def skip_step(self, run_id, step_id, actor, operation_id, reason):
        """Close one unfinished step of a running plan without doing it; the reason and who decided are recorded."""
        actor = normalize_actor(actor)
        text = self._plan_text(reason, "reason")
        fingerprint = self._fingerprint("skip_step", actor, run_id=run_id, step_id=step_id, reason=text)
        with self.ctx.store.lock(exclusive=True), self.ctx.store.transaction() as conn:
            self.ctx.guard_overseer_admission(conn, run_id=run_id)
            run = self._run(conn, run_id)
            op = self._op(conn, run_id, operation_id, fingerprint)
            if op is not None:
                return loads(op["result"])
            self._progress_allowed(run)
            self._step(self._plan_snapshot(run), step_id)
            state = conn.execute("SELECT state FROM run_steps WHERE run_id = ? AND step_id = ?",
                                 (run_id, step_id)).fetchone()["state"]
            if state in FINISHED_STEP_STATES:
                raise StateConflictError(f"step {step_id!r} is already {state}", details={"step_id": step_id})
            self._reserve(conn, run_id, operation_id, fingerprint)
            now = self.ctx.now()
            conn.execute("UPDATE run_steps SET state = 'skipped', completed_at = ?, completed_by = ?, completion_note = ? "
                         "WHERE run_id = ? AND step_id = ?", (now, dumps(actor), text, run_id, step_id))
            self._event(conn, run_id, "step_skipped", actor,
                        {"operation_id": operation_id, "step_id": step_id, "reason": text})
            self._finish_if_done(conn, run_id, actor, now)
            return self._result(conn, run_id, run_id, operation_id)

    def _write_arguments(self, conn, run_id, records):
        for name, record in records.items():
            conn.execute("INSERT INTO run_arguments(run_id, name, value, source, note, confirmed, supplied_by, at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, name) DO UPDATE SET "
                         "value=excluded.value, source=excluded.source, note=excluded.note, "
                         "confirmed=excluded.confirmed, supplied_by=excluded.supplied_by, at=excluded.at",
                         (run_id, name, dumps(record["value"]), record["source"], record["note"],
                          record["confirmed"], dumps(record["supplied_by"]), self.ctx.now()))

    @staticmethod
    def _progress_allowed(run):
        if run["control"] != "running":
            raise StateConflictError(f"run is {run['control']}; progress is not allowed")

    def _control(self, run_id, actor, operation_id, action):
        actor = normalize_actor(actor)
        fingerprint = self._fingerprint(action, actor, run_id=run_id)
        with self.ctx.store.lock(exclusive=True), self.ctx.store.transaction() as conn:
            self.ctx.guard_overseer_admission(conn, run_id=run_id, allow_uncertain_control=True)
            run = self._run(conn, run_id)
            op = self._op(conn, run_id, operation_id, fingerprint)
            if op is not None:
                return loads(op["result"])
            allowed = {"pause": {"running"}, "resume": {"paused"}, "cancel": {"running", "paused"}}
            if run["control"] not in allowed[action]:
                raise StateConflictError(f"cannot {action} a {run['control']} run")
            self._reserve(conn, run_id, operation_id, fingerprint)
            target = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[action]
            conn.execute("UPDATE runs SET control = ?, cancelled_at = ? WHERE run_id = ?",
                         (target, self.ctx.now() if action == "cancel" else None, run_id))
            self._event(conn, run_id, action, actor, {"operation_id": operation_id})
            return self._result(conn, run_id, run_id, operation_id)

    def pause(self, run_id, actor, operation_id):
        return self._control(run_id, actor, operation_id, "pause")

    def resume(self, run_id, actor, operation_id):
        return self._control(run_id, actor, operation_id, "resume")

    def cancel(self, run_id, actor, operation_id):
        return self._control(run_id, actor, operation_id, "cancel")

    def supply_arguments(self, run_id, arguments, actor, operation_id):
        actor = normalize_actor(actor)
        fingerprint = self._fingerprint("supply_arguments", actor, run_id=run_id, arguments=arguments)
        with self.ctx.store.lock(exclusive=True), self.ctx.store.transaction() as conn:
            self.ctx.guard_overseer_admission(conn, run_id=run_id)
            run = self._run(conn, run_id)
            op = self._op(conn, run_id, operation_id, fingerprint)
            if op is not None:
                return loads(op["result"])
            self._progress_allowed(run)
            records = self._argument_records(loads(run["snapshot"]), arguments, actor)
            for name, record in list(records.items()):
                existing = conn.execute("SELECT * FROM run_arguments WHERE run_id = ? AND name = ?",
                                        (run_id, name)).fetchone()
                if existing is not None and existing["locked"]:
                    if json_digest(loads(existing["value"])) != json_digest(record["value"]):
                        raise StateConflictError(f"argument {name!r} has already been consumed and is locked")
                    del records[name]  # identical values keep the original provenance
            self._reserve(conn, run_id, operation_id, fingerprint)
            self._write_arguments(conn, run_id, records)
            self._event(conn, run_id, "arguments_supplied", actor,
                        {"operation_id": operation_id, "arguments": records})
            return self._result(conn, run_id, run_id, operation_id)

    @staticmethod
    def _lock_arguments(conn, run_id, step_id, snapshot):
        for arg in snapshot.get("arguments", []):
            if "step" not in arg or arg["step"] == step_id:
                conn.execute("UPDATE run_arguments SET locked = 1 WHERE run_id = ? AND name = ?",
                             (run_id, arg["name"]))

    def _ready_step(self, conn, run_id, step_id):
        run = self._run(conn, run_id)
        self._progress_allowed(run)
        snapshot = loads(run["snapshot"])
        definition = self._step(snapshot, step_id)
        view = self._view(conn, run_id)
        if not any(s["step_id"] == step_id for s in view["ready_steps"]):
            raise StateConflictError(f"step {step_id!r} is not ready", details={"step_id": step_id})
        self._lock_arguments(conn, run_id, step_id, snapshot)
        return run, definition, view

    def start_step(self, run_id, step_id, actor, operation_id):
        actor = normalize_actor(actor)
        fingerprint = self._fingerprint("start_step", actor, run_id=run_id, step_id=step_id)
        with self.ctx.store.lock(exclusive=True), self.ctx.store.transaction() as conn:
            self.ctx.guard_overseer_admission(conn, run_id=run_id)
            self._run(conn, run_id)
            op = self._op(conn, run_id, operation_id, fingerprint)
            if op is not None:
                return loads(op["result"])
            self._ready_step(conn, run_id, step_id)
            step = conn.execute("SELECT state FROM run_steps WHERE run_id = ? AND step_id = ?",
                                (run_id, step_id)).fetchone()
            if step["state"] != "pending":
                raise StateConflictError("step has already started")
            self._reserve(conn, run_id, operation_id, fingerprint)
            conn.execute("UPDATE run_steps SET state = 'active', started_at = ?, started_by = ? "
                         "WHERE run_id = ? AND step_id = ?", (self.ctx.now(), dumps(actor), run_id, step_id))
            self._event(conn, run_id, "step_started", actor,
                        {"operation_id": operation_id, "step_id": step_id})
            return self._result(conn, run_id, run_id, operation_id)

    @staticmethod
    def _control_generation(conn, run_id):
        return conn.execute("SELECT COALESCE(MAX(ordinal), 0) FROM run_events "
                            "WHERE run_id = ? AND type IN ('pause', 'resume', 'cancel')", (run_id,)).fetchone()[0]

    def complete_step(self, run_id, step_id, actor, operation_id, completion_note="", output=None):
        # All transactions have committed before surfacing a blocked outcome.
        result = self._complete_step(run_id, step_id, actor, operation_id, completion_note, output)
        if "error" in result:
            error = result["error"]
            raise VerificationBlockedError(error["message"], details=error["details"])
        return result

    def _complete_step(self, run_id, step_id, actor, operation_id, completion_note, output):
        actor = normalize_actor(actor)
        if not isinstance(completion_note, str):
            raise InvalidRequestError("completion_note must be a string")
        fingerprint = self._fingerprint("complete_step", actor, run_id=run_id, step_id=step_id,
                                        completion_note=completion_note, output=output)
        with self.ctx.store.lock(exclusive=True), self.ctx.store.transaction() as conn:
            self.ctx.guard_overseer_admission(conn, run_id=run_id)
            run = self._run(conn, run_id)
            op = self._op(conn, run_id, operation_id, fingerprint)
            if op is not None:
                if op["result"] is not None:
                    return loads(op["result"])
                # A reserved verifier is resumed from its receipt, never redispatched
                # while the original owner may still be executing it.
                attempt = conn.execute("SELECT * FROM run_attempts WHERE run_id = ? AND operation_id = ?",
                                       (run_id, operation_id)).fetchone()
                definition = self._step(loads(run["snapshot"]), step_id)
                retry = True
            else:
                run, definition, view = self._ready_step(conn, run_id, step_id)
                self._reserve(conn, run_id, operation_id, fingerprint)
                invocation_id = ("wf-" + json_digest([run_id, operation_id])) if "verifier" in definition else None
                details = {"control_generation": self._control_generation(conn, run_id),
                           "arguments": {name: record["value"] for name, record in view["arguments"].items()}}
                now = self.ctx.now()
                conn.execute("INSERT INTO run_attempts(run_id, step_id, operation_id, at, updated_at, actor, note, "
                             "output, status, invocation_id, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (run_id, step_id, operation_id, now, now, dumps(actor), completion_note, dumps(output),
                              "verifying" if invocation_id else "declared", invocation_id, dumps(details)))
                conn.execute("UPDATE run_steps SET state = 'active', started_at = COALESCE(started_at, ?), "
                             "started_by = COALESCE(started_by, ?), verification = ? WHERE run_id = ? AND step_id = ?",
                             (now, dumps(actor), "pending" if invocation_id else "declared", run_id, step_id))
                self._event(conn, run_id, "completion_attempted", actor,
                            {"operation_id": operation_id, "step_id": step_id, "invocation_id": invocation_id})
                attempt = conn.execute("SELECT * FROM run_attempts WHERE run_id = ? AND operation_id = ?",
                                       (run_id, operation_id)).fetchone()
                retry = False
            if "verifier" not in definition:
                self._complete(conn, run_id, step_id, actor, completion_note, output, "declared", operation_id)
                return self._result(conn, run_id, run_id, operation_id)
            selection = loads(run["selection"])
            details = loads(attempt["details"])
        self.ctx.hook("after_workflow_attempt_recorded", run_id=run_id, operation_id=operation_id)
        verifier_input = {"run_id": run_id, "step_id": step_id, "arguments": details["arguments"],
                          "output": output, "completion_note": completion_note}
        receipt, error = None, None
        try:
            if retry:
                try:
                    receipt = self.tools.receipt(attempt["invocation_id"], actor=actor,
                                                 selection=selection)["receipt"]
                except NotFoundError:
                    # There is no receipt: no runtime effect has been dispatched.
                    receipt = None
            if receipt is None:
                with self.ctx.store.reader() as conn:
                    current = self._run(conn, run_id)
                    changed = (current["control"] != "running" or
                               self._control_generation(conn, run_id) != details["control_generation"])
                if changed:
                    error = {"code": "control_changed", "message": "control changed before verifier dispatch", "details": {}}
                else:
                    receipt = self.tools.invoke(definition["verifier"], verifier_input, attempt["invocation_id"],
                                                actor, selection,
                                                expected_definition_revision=definition["verifier_revision"],
                                                expected_code_revision=definition["verifier_code_revision"])["receipt"]
        except AgenticError as exc:
            error = exc.to_dict()
        return self._finish_verification(run_id, step_id, actor, operation_id, receipt, error)

    def _complete(self, conn, run_id, step_id, actor, note, output, verification, operation_id):
        now = self.ctx.now()
        conn.execute("UPDATE run_steps SET state = 'completed', verification = ?, completed_at = ?, completed_by = ?, "
                     "completion_note = ?, output = ? WHERE run_id = ? AND step_id = ?",
                     (verification, now, dumps(actor), note, dumps(output), run_id, step_id))
        self._event(conn, run_id, "step_completed", actor,
                    {"operation_id": operation_id, "step_id": step_id, "verification": verification})
        self._finish_if_done(conn, run_id, actor, now)

    def _finish_if_done(self, conn, run_id, actor, now):
        incomplete = conn.execute("SELECT 1 FROM run_steps WHERE run_id = ? AND state NOT IN ('completed', 'skipped')",
                                  (run_id,)).fetchone()
        if incomplete is None:
            conn.execute("UPDATE runs SET control = 'completed', completed_at = ? WHERE run_id = ?", (now, run_id))
            self._event(conn, run_id, "completed", actor, {})

    def _finish_verification(self, run_id, step_id, actor, operation_id, receipt, error):
        with self.ctx.store.lock(exclusive=True), self.ctx.store.transaction() as conn:
            self.ctx.guard_overseer_admission(conn, run_id=run_id)
            run = self._run(conn, run_id)
            op = conn.execute("SELECT * FROM workflow_ops WHERE scope = ? AND operation_id = ?",
                              (run_id, operation_id)).fetchone()
            if op["result"] is not None:
                return loads(op["result"])
            attempt = conn.execute("SELECT * FROM run_attempts WHERE run_id = ? AND operation_id = ?",
                                   (run_id, operation_id)).fetchone()
            details = loads(attempt["details"])
            if receipt is not None and receipt["status"] == "in_flight":
                # Another completion call still owns this invocation; keep its reservation.
                return {"run": self._view(conn, run_id), "operation_id": operation_id}
            verdict = None
            feedback, block = None, None
            if error:
                block = error["code"]
                feedback = error["message"]
                details["error"] = error
            elif receipt["status"] == "succeeded":
                output = receipt["output"]
                if (not isinstance(output, dict) or type(output.get("passed")) is not bool
                        or not isinstance(output.get("feedback"), str)):
                    block, feedback = "invalid_verdict", "verifier must return {passed: bool, feedback: string}"
                else:
                    verdict, feedback = output["passed"], output["feedback"]
            else:
                block = receipt["failure_code"] or receipt["status"]
                feedback = (receipt.get("diagnostics") or {}).get("message") or f"verifier receipt is {receipt['status']}"
            details["verdict"] = verdict
            details["receipt"] = receipt
            details["verifier_block_reason"] = block
            if (run["control"] != "running"
                    or self._control_generation(conn, run_id) != details["control_generation"]):
                block = "control_changed"
                details["control_state"] = run["control"]
            status = "blocked" if block else ("verified" if verdict else "failed")
            conn.execute("UPDATE run_attempts SET status = ?, feedback = ?, block_reason = ?, details = ?, updated_at = ? "
                         "WHERE run_id = ? AND operation_id = ?",
                         (status, feedback, block, dumps(details), self.ctx.now(), run_id, operation_id))
            if status == "verified":
                self._complete(conn, run_id, step_id, actor, attempt["note"], loads(attempt["output"]), "verified", operation_id)
            else:
                conn.execute("UPDATE run_steps SET verification = ? WHERE run_id = ? AND step_id = ?",
                             (status, run_id, step_id))
                self._event(conn, run_id, "verification_" + status, actor,
                            {"operation_id": operation_id, "step_id": step_id, "feedback": feedback, "block_reason": block})
            result = self._result(conn, run_id, run_id, operation_id)
            if status == "blocked":
                result = {"error": {
                    "code": "verification_blocked", "message": f"verification blocked: {block}",
                    "details": {**result, "reason": block, "attempt_recorded": True},
                }}
                conn.execute("UPDATE workflow_ops SET result = ? WHERE scope = ? AND operation_id = ?",
                             (dumps(result), run_id, operation_id))
            return result
