"""Durable cron slot claims with first-seen and repeated-wall-time suppression."""

from datetime import timedelta

from qorqut_operations.application.engine import Application
from qorqut_operations.contracts import DomainError, InvocationContext, Principal
from qorqut_operations.application.plans import apply_records
from qorqut_operations.storage.repositories import decode_row
from .schedule import RoutineError, next_slot, prev_slot, stamp, zone


class RoutineScheduler:
    def __init__(self, operations, database):
        self.operations = operations
        self.database = database
        self.errors = []

    @staticmethod
    def due(row, now):
        tz = zone(row["timezone"])
        slot = prev_slot(row["schedule"], now, tz)
        while slot.astimezone(tz).fold:
            slot = prev_slot(row["schedule"], slot - timedelta(minutes=1), tz)
        return slot

    def tick(self, now):
        now = now.replace(second=0, microsecond=0)
        self.errors = []

        def body(uow):
            # A tick refused the writer at its first write runs again from here, so its report starts afresh.
            queued = []
            self.errors = []
            Application.ensure_writes_open(uow)
            definitions = [decode_row(row) for row in uow.connection.execute(
                "SELECT * FROM routines WHERE status='active' ORDER BY created_at,id LIMIT 2001")]
            if len(definitions) > 2000:
                raise DomainError("result_too_large", "Routine scheduler exceeds its configured definition bound")
            projects = frozenset(row[0] for row in uow.connection.execute("SELECT id FROM projects WHERE archived_at IS NULL"))
            for row in definitions:
                if row["project_id"] is not None and row["project_id"] not in projects:
                    continue
                if row["metadata_json"].get("retired"):
                    continue
                try:
                    if row["metadata_json"].get("legacy_progress") in {"unavailable", "recovery_required"}:
                        raise DomainError("routine_progress_unavailable", "Imported scheduler progress requires an owner disposition")
                    program = self.operations.programs.require(row["job_kind"])
                    program.validate(row["arguments_json"])
                    worker_id = self.operations.authority.worker_id(row["job_kind"], uow)
                    self.operations.authority.require_worker(worker_id, None, uow)
                    context = InvocationContext(Principal(worker_id, "system", "Routine program"), source="worker",
                        project_id=row["project_id"], allowed_project_ids=projects)
                    targets = [target for target in self.operations.targets(context, row, uow)
                        if target is None or target in context.allowed_project_ids]
                    # The saved target itself must suit the program, even when no project is currently reachable.
                    kinds = {"global" if row["target"] == "root" else "project", *("project" if target else "global" for target in targets)}
                    if not kinds <= set(program.scope_kinds):
                        raise DomainError("forbidden", "Routine program does not support its registered target")
                    slot = self.due(row, now)
                except (DomainError, RoutineError) as exc:
                    self.errors.append({"project_id": row["project_id"], "namespace": row["namespace"],
                        "routine_id": row["id"], "error": str(exc)})
                    continue
                slot_text = stamp(slot)
                wall = slot.astimezone(zone(row["timezone"])).strftime("%Y-%m-%dT%H:%M")
                version = row["metadata_json"].get("schedule_revision", row["revision"])
                repo = uow.repository("routine_slots")
                pending, unfinished = False, False
                for target in targets:
                    key = {"namespace": row["namespace"], "routine_id": row["id"], "target_namespace": target or "global"}
                    prior = repo.get(key)
                    job_id = (prior or {}).get("state_json", {}).get("job_id")
                    if job_id:
                        job = uow.repository("jobs", project_id=target).get(job_id)
                        if job and job["state"] in {"queued", "running", "applying", "waiting_signoff", "cancel_requested"}:
                            pending = True
                    if prior and prior["schedule_revision"] == version and prior["state_json"].get("done") is False and not job_id:
                        unfinished = True
                if pending:
                    continue
                for target in targets:
                    key = {"namespace": row["namespace"], "routine_id": row["id"], "target_namespace": target or "global"}
                    prior = repo.get(key)
                    if prior is None or prior["schedule_revision"] != version:
                        values = {"schedule_revision": version, "slot": slot_text, "wall_slot": wall,
                                  "state_json": {"first_seen": True, "done": True}}
                        if prior:
                            repo.update(key, values, expected_revision=prior["revision"])
                        else:
                            repo.insert({**key, **values})
                        continue
                    waiting = prior["state_json"].get("done") is False and not prior["state_json"].get("job_id")
                    if unfinished and not waiting:
                        continue
                    if waiting:
                        fire, fire_wall = prior["slot"], prior["wall_slot"]
                        missed, capped = prior["state_json"].get("missed", 0), prior["state_json"].get("missed_capped", False)
                    else:
                        if slot_text <= prior["slot"] or wall <= prior["wall_slot"]:
                            continue
                        missed, cursor = 0, next_slot(row["schedule"], prior["slot"], row["timezone"])
                        while cursor < slot and missed < 10000:
                            if not cursor.astimezone(zone(row["timezone"])).fold:
                                missed += 1
                            cursor = next_slot(row["schedule"], cursor, row["timezone"])
                        fire, fire_wall, capped = slot_text, wall, cursor < slot
                        prior = repo.update(key, {"slot": fire, "wall_slot": fire_wall, "state_json": {
                            "done": False, "missed": missed, "missed_capped": capped}}, expected_revision=prior["revision"])
                    args = {"scope": row["scope"], "routine_id": row["id"]}
                    builder = self.operations._builder(context, "routines_run", args, "Run scheduled routine")
                    job_id = self.operations.add_run(builder, row, target, uow, trigger="schedule", slot=fire, missed=missed)
                    apply_records(context, self.operations._plan(builder, row), uow)
                    repo.update(key, {"slot": fire, "wall_slot": fire_wall,
                        "state_json": {"job_id": job_id, "done": False, "missed": missed, "missed_capped": capped}}, expected_revision=prior["revision"])
                    queued.append(job_id)
            return queued

        # No operation of its own otherwise; the host change feed's coarse
        # "jobs_routines" kind is the only signal a queued routine run exists
        # at all, since routine_runs/jobs have no capture trigger.
        # The tick runs every few seconds and usually finds nothing due, so it
        # takes SQLite's writer only from its first real change; until then it
        # holds no other writer up. Everything before that change is a read (and
        # this object's report), so a tick refused the writer there can simply
        # run again in an ordinary write unit.
        return self.database.write_if_needed(body, operation="jobs_routines_tick")
