"""Durable attachments, human requests and saved control jobs through core."""

import json
from pathlib import Path
from uuid import UUID

from ..agents.registry import get_row, reserved
from ..app import Change, Operation
from ..contracts import CoreError, Invocation, JobProgress, JobResolution, canonical
from ..messaging.models import MessagesSend
from ..storage.database import assert_external_io_allowed
from ..storage.events import EventInput, append_event
from ..storage.jobs import JobHandler, after
from .client import BrowserClient, payload_hash
from .models import (
    Binding, BrowserGet, BrowserSurface, BrowserView, BrowsersAttach, Control, ControlJob,
    Identity, InputRequestPage, InputRequestsComplete, InputRequestsCreate, InputRequestsList,
    InputRequestsResume, InputRequestView, Reconcile, Receipt, decode_cursor,
)

UNRESOLVED = ("requested", "ready", "interrupted")


def request_row(tx, request_id):
    row = tx.connection.execute("SELECT * FROM input_requests WHERE request_id=?", (str(request_id),)).fetchone()
    if row is None:
        raise CoreError("not_found", "Browser input request not found")
    return row


def intent_row(tx, command_id):
    return tx.connection.execute("SELECT * FROM browser_control_intents WHERE command_id=?", (str(command_id),)).fetchone()


def last_receipt(tx, command_id):
    row = tx.connection.execute("SELECT receipt_json FROM browser_control_receipts WHERE command_id=? ORDER BY rowid DESC LIMIT 1",
        (str(command_id),)).fetchone()
    return Receipt.model_validate_json(row[0]) if row else None


def request_view(tx, row, observation=None, reason=None):
    receipt = last_receipt(tx, row["latest_command_id"])
    pending = receipt is None or receipt.outcome != "completed"
    job = tx.connection.execute("SELECT j.state,j.progress_json FROM jobs j JOIN browser_control_intents i USING(job_id) WHERE i.command_id=?",
        (row["latest_command_id"],)).fetchone()
    if job and job["state"] == "cancelled" and json.loads(job["progress_json"])["phase"] == "retired_attachment":
        pending = False
    state, error = row["state"], json.loads(row["error_json"]) if row["error_json"] else None
    binding = Binding.model_validate_json(row["browser_binding_json"]) if row["browser_binding_json"] else None
    epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
    if state in UNRESOLVED:
        if epoch != row["recovery_epoch"]:
            state, reason = "interrupted", "recovery_review"
        elif state == "ready" and observation is not None and (
                observation.control.binding != binding or observation.identity != binding.identity
                or not observation.control.ready or observation.control.mode != "human"):
            reason = "control_changed"
        if reason is not None:
            state = "interrupted"
            error = {"code": reason, "message": "Browser control requires explicit resume or reattachment"}
    return InputRequestView(**{key: row[key] for key in ("request_id", "agent_id", "conversation_id", "surface_id",
        "requester_actor_id", "prompt", "revision", "created_at", "updated_at", "note")},
        state=state, browser_binding=binding, control_pending=pending, error=error)


class Browsers:
    def __init__(self, application):
        self.app, self.db = application, application.db
        self.client = BrowserClient()

    def operations(self):
        return (
            Operation("browsers_attach", BrowsersAttach, True, self.unprepared, BrowserView, prepare=self.prepare_attach),
            Operation("browser_get", BrowserGet, False, self.unprepared, BrowserView, prepare=self.prepare_get),
            Operation("input_requests_create", InputRequestsCreate, True, self.unprepared, InputRequestView, prepare=self.prepare_create),
            Operation("input_requests_list", InputRequestsList, False, self.unprepared, InputRequestPage, prepare=self.prepare_list),
            Operation("input_requests_complete", InputRequestsComplete, True, self.complete, InputRequestView),
            Operation("input_requests_cancel", InputRequestsComplete, True, self.cancel, InputRequestView),
            Operation("input_requests_resume", InputRequestsResume, True, self.unprepared, InputRequestView, prepare=self.prepare_resume),
        )

    @staticmethod
    def unprepared(*args):
        raise CoreError("invalid_transition", "Browser operation requires a bounded observation")

    def handler(self):
        return JobHandler("browser_control", ControlJob, True, self.execute, self.inspect)

    def target(self, tx, agent_id):
        agent = get_row(tx, agent_id)
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        if agent["kind"] != "host" or agent["id"] != reserved(tx)["id"]:
            raise CoreError("unsupported_capability", "Browser attachment requires the prepared onbash environment; VM setup is unavailable")
        if agent["lifecycle"] not in {"provisioning", "active"} or not agent["workspace_initialized_at"]:
            raise CoreError("not_ready", "Browser target workspace is unavailable")
        return dict(agent), epoch

    @staticmethod
    def attachment(tx, agent_id):
        row = tx.connection.execute("""SELECT a.*,s.agent_id,s.latest_command_id FROM browser_surfaces s
            JOIN browser_attachments a ON a.attachment_id=s.attachment_id WHERE s.agent_id=?""", (str(agent_id),)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def active(tx, surface_id):
        return tx.connection.execute("SELECT * FROM input_requests WHERE surface_id=? AND state IN ('requested','ready','interrupted')",
            (str(surface_id),)).fetchone()

    def observe(self, attachment, actor_id, epoch):
        if attachment["recovery_epoch"] != epoch:
            raise CoreError("target_changed", "Browser attachment is held for recovery")
        observation = self.client.observe(attachment["service_url"], actor_id)
        self.validate_identity(observation, attachment["agent_id"], epoch)
        if observation.identity != Identity.model_validate_json(attachment["identity_json"]):
            raise CoreError("target_changed", "Browser identity changed; explicit reattachment is required")
        return observation

    @staticmethod
    def validate_identity(observation, agent_id, epoch):
        assert_external_io_allowed()
        identity = observation.identity
        boot = UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        if (str(identity.agent_id) != str(agent_id) or str(identity.recovery_epoch) != epoch
                or identity.environment_generation != 1 or identity.boot_id != boot
                or observation.control.binding.identity != identity):
            raise CoreError("target_changed", "Browser identity does not match the current target environment")

    def live(self, agent_id, context):
        with self.db.read() as tx:
            get_row(tx, agent_id)
            epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
            attachment = self.attachment(tx, agent_id)
        observation, reason = None, None
        if attachment:
            try:
                observation = self.observe(attachment, context.actor_id, epoch)
            except (CoreError, OSError, ValueError) as exc:
                reason = str(exc.code) if isinstance(exc, CoreError) else "unavailable"
        return attachment, epoch, observation, reason

    def view(self, tx, agent_id, attachment, epoch, observation, reason):
        if self.attachment(tx, agent_id) != attachment or tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0] != epoch:
            raise CoreError("target_changed", "Browser attachment changed during observation")
        surface, active = None, None
        if attachment:
            control_error = None
            if attachment["latest_command_id"]:
                job = tx.connection.execute("SELECT j.state,j.last_error_json FROM jobs j JOIN browser_control_intents i USING(job_id) WHERE i.command_id=?",
                    (attachment["latest_command_id"],)).fetchone()
                if job["state"] != "completed":
                    control_error = json.loads(job["last_error_json"])["code"] if job["last_error_json"] else "control_pending"
            surface = BrowserSurface(surface_id=attachment["surface_id"], agent_id=agent_id,
                status="available" if observation else "unavailable", reason=reason or control_error,
                binding=observation.control.binding if observation else None,
                control=Control(mode=observation.control.mode, ready=observation.control.ready) if observation else None)
            row = self.active(tx, attachment["surface_id"])
            if row:
                active = request_view(tx, row, observation, reason)
        return BrowserView(agent_id=agent_id, recovery_epoch=epoch, surface=surface, active_request=active)

    def prepare_get(self, args, context):
        live = self.live(args.agent_id, context)
        return lambda tx, args, context, operation_id: Change(self.view(tx, args.agent_id, *live))

    def prepare_list(self, args, context):
        attachment, epoch, observation, reason = self.live(args.agent_id, context)
        def read(tx, args, context, operation_id):
            self.view(tx, args.agent_id, attachment, epoch, observation, reason)
            where, values = "agent_id=?", [str(args.agent_id)]
            if args.before_created_at:
                when, request_id = decode_cursor(args.before_created_at)
                where += " AND (created_at,request_id)<(?,?)"
                values += [when, request_id]
            rows = tx.connection.execute(f"SELECT * FROM input_requests WHERE {where} ORDER BY created_at DESC,request_id DESC LIMIT ?",
                (*values, args.limit + 1)).fetchall()
            return Change(InputRequestPage(items=[request_view(tx, row, observation, reason) for row in rows[:args.limit]],
                has_more=len(rows) > args.limit))
        return read

    def prepare_conversation(self, args, context):
        # One live observation per page, never one network call per historical card.
        with self.db.read() as tx:
            if tx.connection.execute("SELECT 1 FROM conversation_entries WHERE agent_id=? AND source_type='browser_request' LIMIT 1",
                    (str(args.agent_id),)).fetchone() is None:
                return self.app.conversations.list
        live = self.live(args.agent_id, context)
        def read(tx, args, context, operation_id):
            self.view(tx, args.agent_id, *live)
            return self.app.conversations.list(tx, args, context, operation_id, browser_observation=live[2:])
        return read

    def save_intent(self, tx, attachment, expected, mode, purpose, actor_id, operation_id,
                    request_id=None, previous=None):
        if expected.control_epoch == 2**63 - 1:
            raise CoreError("storage_blocked", "Browser control epoch exhausted")
        command = Reconcile(command_id=self.db.id_factory(), expected=expected,
            desired=expected.model_copy(update={"control_epoch": expected.control_epoch + 1}), mode=mode,
            evidence=f"Core browser {purpose}" + (f" for request {request_id}" if request_id else " for explicit attachment"))
        job = self.app.jobs.enqueue(tx, "browser_control", ControlJob(command_id=command.command_id),
            operation_id=operation_id, target_id=expected.identity.agent_id)
        tx.connection.execute("""INSERT INTO browser_control_intents VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (str(command.command_id), attachment["attachment_id"], str(request_id) if request_id else None,
             str(previous) if previous else None, str(actor_id), canonical(command), purpose, str(job),
             str(operation_id), attachment["recovery_epoch"], self.db.clock()))
        tx.connection.execute("UPDATE browser_surfaces SET latest_command_id=? WHERE surface_id=?",
            (str(command.command_id), attachment["surface_id"]))
        if request_id:
            tx.connection.execute("UPDATE input_requests SET latest_command_id=? WHERE request_id=?",
                (str(command.command_id), str(request_id)))
        return command

    def prepare_attach(self, args, context):
        with self.db.read() as tx:
            agent, epoch = self.target(tx, args.agent_id)
            previous = self.attachment(tx, args.agent_id)
        observation = self.client.observe(args.service_url, context.actor_id)
        self.validate_identity(observation, args.agent_id, epoch)
        replacing = previous is not None and (previous["service_url"] != args.service_url
            or Identity.model_validate_json(previous["identity_json"]) != observation.identity)
        def attach(tx, args, context, operation_id):
            current_agent, current_epoch = self.target(tx, args.agent_id)
            if (current_agent, current_epoch, self.attachment(tx, args.agent_id)) != (agent, epoch, previous):
                raise CoreError("target_changed", "Browser target changed during attachment")
            seq = None
            if replacing:
                if self.active(tx, previous["surface_id"]):
                    raise CoreError("invalid_transition", "Complete or cancel the unresolved request before replacing its browser")
                outstanding = tx.connection.execute("""SELECT j.* FROM jobs j JOIN browser_control_intents i USING(job_id)
                    WHERE i.attachment_id=? AND j.state NOT IN ('completed','cancelled')""",
                    (previous["attachment_id"],)).fetchall()
                if outstanding and Identity.model_validate_json(previous["identity_json"]) == observation.identity:
                    raise CoreError("uncertain_effect", "A same-identity browser move cannot abandon unresolved control")
                for job in outstanding:
                    error = CoreError("uncertain_effect", "Retired browser attachment; release or effect was not confirmed").error
                    self.app.jobs._resolve(tx, job, JobResolution(state="cancelled",
                        progress=JobProgress(phase="retired_attachment"), error=error))
                tx.connection.execute("""UPDATE input_requests SET error_json=?,revision=revision+1,updated_at=?
                    WHERE latest_command_id IN (SELECT i.command_id FROM browser_control_intents i JOIN jobs j USING(job_id)
                        WHERE i.attachment_id=? AND j.state='cancelled' AND json_extract(j.progress_json,'$.phase')='retired_attachment')""",
                    (canonical({"code": "retired_attachment", "message": "Previous browser release or effect was not confirmed"}),
                     self.db.clock(), previous["attachment_id"]))
            if previous is None or replacing:
                surface_id = previous["surface_id"] if previous else str(self.db.id_factory())
                attachment_id, now = str(self.db.id_factory()), self.db.clock()
                if previous:
                    tx.connection.execute("UPDATE browser_surfaces SET attachment_id=?,latest_command_id=NULL WHERE surface_id=?",
                        (attachment_id, surface_id))
                else:
                    tx.connection.execute("INSERT INTO browser_surfaces VALUES (?,?,?,NULL,?)",
                        (surface_id, str(args.agent_id), attachment_id, now))
                tx.connection.execute("INSERT INTO browser_attachments VALUES (?,?,?,?,?,?,?,?,?)",
                    (attachment_id, surface_id, args.service_url, canonical(observation.identity), canonical(observation.control),
                     epoch, str(context.actor_id), str(operation_id), now))
                attached = self.attachment(tx, args.agent_id)
                # Startup reconciliation never clears an existing human hold, and
                # an empty settlement list cannot resolve uncertain browser effects.
                if not observation.control.ready and observation.control.mode == "reconciling":
                    self.save_intent(tx, attached, observation.control.binding, "automation", "initialize", context.actor_id, operation_id)
                seq = append_event(tx, EventInput(kind="browser.attached", actor_id=context.actor_id,
                    agent_id=args.agent_id, operation_id=operation_id, payload={"surface_id": surface_id,
                        "attachment_id": attachment_id, "identity": observation.identity.model_dump(mode="json"),
                        "previous_attachment_id": previous["attachment_id"] if previous else None,
                        "previous_identity": json.loads(previous["identity_json"]) if previous else None}))
            elif not observation.control.ready and observation.control.mode == "reconciling":
                # A fresh attach key explicitly reconnects an idle service after
                # restart; immutable original-key receipts never reach this path.
                unfinished = tx.connection.execute("""SELECT 1 FROM browser_control_intents i JOIN jobs j USING(job_id)
                    WHERE i.attachment_id=? AND j.state<>'completed' LIMIT 1""", (previous["attachment_id"],)).fetchone()
                if self.active(tx, previous["surface_id"]) is None and unfinished is None:
                    command = self.save_intent(tx, previous, observation.control.binding, "automation", "initialize",
                        context.actor_id, operation_id)
                    seq = append_event(tx, EventInput(kind="browser.reconnect_requested", actor_id=context.actor_id,
                        agent_id=args.agent_id, operation_id=operation_id, payload={"surface_id": previous["surface_id"],
                            "attachment_id": previous["attachment_id"], "command_id": str(command.command_id),
                            "observed_control": observation.control.model_dump(mode="json")}))
            attached = self.attachment(tx, args.agent_id)
            return Change(self.view(tx, args.agent_id, attached, epoch, observation, None), seq)
        return attach

    def prepare_create(self, args, context):
        with self.db.read() as tx:
            agent, epoch = self.target(tx, args.target_agent_id)
            attachment = self.attachment(tx, args.target_agent_id)
            if attachment is None:
                raise CoreError("not_ready", "No browser is attached to this agent")
            if self.active(tx, attachment["surface_id"]):
                raise CoreError("invalid_transition", "An unresolved browser request already exists for this surface")
        observation = self.observe(attachment, context.actor_id, epoch)
        if args.observed_tab and args.observed_tab.binding != observation.control.binding:
            raise CoreError("target_changed", "Observed tab belongs to another browser binding")
        if observation.control.mode != "automation" or not observation.control.ready:
            raise CoreError("not_ready", "Browser is held or awaiting control reconciliation")
        def create(tx, args, context, operation_id):
            if self.target(tx, args.target_agent_id) != (agent, epoch) or self.attachment(tx, args.target_agent_id) != attachment:
                raise CoreError("target_changed", "Browser target changed during observation")
            if self.active(tx, attachment["surface_id"]):
                raise CoreError("invalid_transition", "An unresolved browser request already exists for this surface")
            previous = attachment["latest_command_id"]
            if previous and (last_receipt(tx, previous) is None or last_receipt(tx, previous).outcome != "completed"):
                raise CoreError("not_ready", "The preceding browser control intent remains unresolved")
            request_id, now = str(self.db.id_factory()), self.db.clock()
            tx.connection.execute("""INSERT INTO input_requests (request_id,agent_id,conversation_id,surface_id,
                requester_actor_id,prompt,idempotency_key,observed_tab_json,creation_json,recovery_epoch,
                created_at,updated_at,state,revision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'requested',1)""",
                (request_id, agent["id"], agent["conversation_id"], attachment["surface_id"], str(context.actor_id),
                 args.prompt, args.idempotency_key, canonical(args.observed_tab) if args.observed_tab else None,
                 canonical({"actor_id": str(context.actor_id), **args.model_dump(mode="json")}), epoch, now, now))
            self.save_intent(tx, attachment, observation.control.binding, "human", "take", context.actor_id, operation_id,
                request_id=request_id, previous=previous)
            self.app.conversations.append_browser_request(tx, request_row(tx, request_id), operation_id=operation_id)
            seq = append_event(tx, EventInput(kind="browser.request_created", actor_id=context.actor_id,
                agent_id=args.target_agent_id, operation_id=operation_id,
                payload={"request_id": request_id, "surface_id": attachment["surface_id"], "state": "requested"}))
            self.db.fault("browser_request_after_event")
            return Change(request_view(tx, request_row(tx, request_id)), seq, "accepted")
        return create

    def complete(self, tx, args, context, operation_id):
        return self.terminal(tx, args, context, operation_id, "completed")

    def cancel(self, tx, args, context, operation_id):
        return self.terminal(tx, args, context, operation_id, "cancelled")

    def terminal(self, tx, args, context, operation_id, state):
        row = request_row(tx, args.request_id)
        if row["state"] in ("completed", "cancelled"):
            if row["state"] != state:
                raise CoreError("invalid_transition", "Browser request already has the opposite terminal outcome")
            return Change(request_view(tx, row))
        if row["revision"] != args.expected_revision:
            raise CoreError("revision_conflict", "Browser request revision changed")
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        if row["recovery_epoch"] != epoch:
            raise CoreError("target_changed", "Browser request is held for recovery")
        attachment = self.attachment(tx, row["agent_id"])
        previous = intent_row(tx, row["latest_command_id"])
        command = Reconcile.model_validate_json(previous["payload_json"])
        self.save_intent(tx, attachment, command.desired, "automation", "release", context.actor_id, operation_id,
            request_id=row["request_id"], previous=command.command_id)
        requester = tx.connection.execute("SELECT agent_id FROM actors WHERE actor_id=?", (row["requester_actor_id"],)).fetchone()
        recipient = requester[0] or row["agent_id"]
        text = f"Browser input request {state}.\nRequest ID: {row['request_id']}\nPrompt: {row['prompt']}"
        if args.note is not None:
            text += "\nNote: " + args.note
        notification = self.app.messaging.send(tx, MessagesSend(idempotency_key=f"browser:{row['request_id']}",
            recipient_agent_id=recipient, body=text), context, operation_id)
        tx.connection.execute("""UPDATE input_requests SET state=?,note=?,revision=revision+1,updated_at=?,error_json=NULL,
            notification_message_id=? WHERE request_id=?""", (state, args.note, self.db.clock(),
            str(notification.data.message.message_id), row["request_id"]))
        seq = append_event(tx, EventInput(kind="browser.request_" + state, actor_id=context.actor_id,
            agent_id=row["agent_id"], operation_id=operation_id, payload={"request_id": row["request_id"], "state": state,
                "note": args.note, "notification_message_id": str(notification.data.message.message_id)}))
        self.db.fault("browser_terminal_after_event")
        return Change(request_view(tx, request_row(tx, args.request_id)), seq, "accepted")

    def validate_receipt(self, intent, receipt):
        command = Reconcile.model_validate_json(intent["payload_json"])
        if (receipt.command_id != command.command_id or receipt.kind != "control" or receipt.binding != command.expected
                or str(receipt.actor_id) != intent["actor_id"] or receipt.payload_hash != payload_hash(command)
                or (receipt.outcome == "completed" and receipt.result.text != command.mode)):
            raise CoreError("target_changed", "Browser control receipt binding disagrees")

    def save_receipt(self, tx, intent, receipt):
        self.validate_receipt(intent, receipt)
        encoded = canonical(receipt)
        if tx.connection.execute("SELECT 1 FROM browser_control_receipts WHERE command_id=? AND receipt_json=?",
                (intent["command_id"], encoded)).fetchone() is None:
            tx.connection.execute("INSERT INTO browser_control_receipts VALUES (?,?,?)",
                (intent["command_id"], encoded, self.db.clock()))

    def _changed(self, tx, row, *, state=None, binding=None, error=None, intent):
        if row["latest_command_id"] != intent["command_id"]:
            return
        state = state or row["state"]
        encoded_error = canonical(error) if error else None
        encoded_binding = canonical(binding) if binding else row["browser_binding_json"]
        if (state, encoded_binding, encoded_error) == (row["state"], row["browser_binding_json"], row["error_json"]):
            return
        tx.connection.execute("UPDATE input_requests SET state=?,browser_binding_json=?,error_json=?,revision=revision+1,updated_at=? WHERE request_id=?",
            (state, encoded_binding, encoded_error, self.db.clock(), row["request_id"]))
        append_event(tx, EventInput(kind="browser.request_control", actor_id=intent["actor_id"], agent_id=row["agent_id"],
            operation_id=intent["operation_id"], payload={"request_id": row["request_id"], "command_id": intent["command_id"],
                "state": state, "error": error}))

    def prepare_resume(self, args, context):
        with self.db.read() as tx:
            row = dict(request_row(tx, args.request_id))
            if row["state"] not in UNRESOLVED:
                raise CoreError("invalid_transition", "Only an unresolved browser request can resume")
            if row["revision"] != args.expected_revision:
                raise CoreError("revision_conflict", "Browser request revision changed")
            attachment = self.attachment(tx, row["agent_id"])
            epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
            if epoch != row["recovery_epoch"]:
                raise CoreError("target_changed", "Browser request is held for recovery")
            intent = dict(intent_row(tx, row["latest_command_id"]))
        observation = self.observe(attachment, context.actor_id, epoch)
        saved = self.client.receipt(attachment["service_url"], intent["actor_id"], intent["command_id"])
        if saved:
            self.validate_receipt(intent, saved)
        command = Reconcile.model_validate_json(intent["payload_json"])
        # A missing or pending receipt never authorizes a replacement effect.
        retry = saved is None or saved.outcome in ("intent", "pending")
        if retry and observation.control.binding not in (command.expected, command.desired):
            raise CoreError("uncertain_effect", "The original browser control intent requires reconciliation")
        def resume(tx, args, context, operation_id):
            if dict(request_row(tx, args.request_id)) != row or self.attachment(tx, row["agent_id"]) != attachment:
                raise CoreError("target_changed", "Browser request changed during resume observation")
            if saved:
                self.save_receipt(tx, intent, saved)
                if saved.outcome not in ("intent", "pending"):
                    job = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (intent["job_id"],)).fetchone()
                    if job["state"] not in ("completed", "failed", "cancelled"):
                        self.app.jobs._resolve(tx, job, JobResolution(
                            state="completed" if saved.outcome == "completed" else "failed",
                            progress=JobProgress(phase="receipt_observed")))
            if (saved and saved.outcome == "completed" and observation.control.binding == command.desired
                    and observation.control.ready and observation.control.mode == "human"):
                self._changed(tx, row, state="ready", binding=command.desired, intent=intent)
            elif retry:
                job = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (intent["job_id"],)).fetchone()
                if job["state"] != "running":
                    tx.connection.execute("UPDATE jobs SET state='queued',wait_reason=NULL,next_attempt_at=NULL,last_error_json=NULL WHERE job_id=?",
                        (intent["job_id"],))
                    self.app.jobs._event(tx, job, job["state"], "queued", "explicit_resume", actor_id=context.actor_id)
                self._changed(tx, row, state="requested", intent=intent)
            else:
                # Completed/failed/interrupted receipt plus fresh same-identity
                # status explains why retrying the old command cannot acquire control.
                self.save_intent(tx, attachment, observation.control.binding, "human", "take", context.actor_id,
                    operation_id, request_id=row["request_id"])
                tx.connection.execute("UPDATE input_requests SET state='requested',error_json=NULL,revision=revision+1,updated_at=? WHERE request_id=?",
                    (self.db.clock(), row["request_id"]))
            seq = append_event(tx, EventInput(kind="browser.request_resumed", actor_id=context.actor_id,
                agent_id=row["agent_id"], operation_id=operation_id, payload={"request_id": row["request_id"],
                    "previous_command_id": intent["command_id"], "previous_outcome": saved.outcome if saved else None,
                    "observed_control": observation.control.model_dump(mode="json")}))
            return Change(request_view(tx, request_row(tx, args.request_id)), seq, "accepted")
        return resume

    def execute(self, claim):
        return self.run_intent(claim.arguments.command_id)

    def inspect(self, job):
        return self.run_intent(ControlJob.model_validate_json(job["args_json"]).command_id)

    def run_intent(self, command_id):
        with self.db.read() as tx:
            intent = dict(intent_row(tx, command_id))
            attachment = dict(tx.connection.execute("SELECT a.*,s.agent_id FROM browser_attachments a JOIN browser_surfaces s USING(surface_id) WHERE a.attachment_id=?",
                (intent["attachment_id"],)).fetchone())
            epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
            current = self.attachment(tx, attachment["agent_id"])
            if current["attachment_id"] != attachment["attachment_id"]:
                return JobResolution(state="cancelled", progress=JobProgress(phase="retired_attachment"),
                    error=CoreError("uncertain_effect", "Retired attachment; control remains unconfirmed").error)
            if intent["recovery_epoch"] != epoch or attachment["recovery_epoch"] != epoch:
                return JobResolution(state="waiting", progress=JobProgress(phase="recovery_review"), reason="recovery_review",
                    next_attempt_at=after(self.db.clock(), 5))
            previous = last_receipt(tx, intent["previous_command_id"]) if intent["previous_command_id"] else None
            if intent["previous_command_id"] and (previous is None or previous.outcome != "completed"):
                return JobResolution(state="waiting", progress=JobProgress(phase="preceding_control"), reason="retry_delay",
                    next_attempt_at=after(self.db.clock(), 5))
        command = Reconcile.model_validate_json(intent["payload_json"])
        try:
            observation = self.observe(attachment, intent["actor_id"], epoch)
            receipt = self.client.receipt(attachment["service_url"], intent["actor_id"], command.command_id)
            if receipt is not None:
                self.validate_receipt(intent, receipt)
            if receipt is None or receipt.outcome == "pending":
                receipt = self.client.reconcile(attachment["service_url"], intent["actor_id"], command)
                self.validate_receipt(intent, receipt)
            self.db.fault("browser_control_after_response")
            with self.db.transaction() as tx:
                self.save_receipt(tx, intent, receipt)
                if intent["request_id"]:
                    row = request_row(tx, intent["request_id"])
                    if receipt.outcome != "completed":
                        self._changed(tx, row, state="interrupted" if receipt.outcome in ("failed", "uncertain") and row["state"] in UNRESOLVED else None,
                            error={"code": receipt.error or "control_pending", "message": "Browser control has not been acknowledged"}, intent=intent)
            if receipt.outcome == "completed":
                observation = self.observe(attachment, intent["actor_id"], epoch)
                live = (observation.control.binding == command.desired and observation.control.mode == command.mode and observation.control.ready)
                with self.db.transaction() as tx:
                    if intent["request_id"]:
                        row = request_row(tx, intent["request_id"])
                        if intent["purpose"] == "take" and row["state"] in UNRESOLVED:
                            self._changed(tx, row, state="ready" if live else "interrupted", binding=command.desired,
                                error=None if live else {"code": "control_changed", "message": "Browser control needs explicit resume"}, intent=intent)
                        elif intent["purpose"] == "release":
                            self._changed(tx, row, error=None if live else {"code": "control_changed", "message": "Browser release needs reconciliation"}, intent=intent)
                return JobResolution(state="completed", progress=JobProgress(phase="control_acknowledged"))
            if receipt.outcome == "pending":
                return JobResolution(state="waiting", progress=JobProgress(phase="control_pending"), reason="retry_delay",
                    next_attempt_at=after(self.db.clock(), 5))
            return JobResolution(state="failed", progress=JobProgress(phase="explicit_resume_required"),
                error=CoreError("uncertain_effect", "Browser control requires explicit resume").error)
        except CoreError as exc:
            with self.db.transaction() as tx:
                if intent["request_id"]:
                    row = request_row(tx, intent["request_id"])
                    self._changed(tx, row, state="interrupted" if row["state"] in UNRESOLVED else None,
                        error={"code": str(exc.code), "message": "Browser control is unconfirmed; the original intent is retained"}, intent=intent)
            return JobResolution(state="uncertain", progress=JobProgress(phase="inspection_required"),
                reason="reconciliation_required", next_attempt_at=after(self.db.clock(), 5), error=exc.error)

    def stream_target(self, request_id, context):
        with self.db.read() as tx:
            if tx.connection.execute("SELECT 1 FROM actors WHERE actor_id=?", (str(context.actor_id),)).fetchone() is None:
                raise CoreError("not_found", "Calling actor is not registered")
            row = request_row(tx, request_id)
            view = request_view(tx, row)
            if view.state != "ready" or view.control_pending or view.browser_binding is None:
                raise CoreError("not_ready", "Browser request is not ready for input")
            attachment = self.attachment(tx, row["agent_id"])
            if Identity.model_validate_json(attachment["identity_json"]) != view.browser_binding.identity:
                raise CoreError("target_changed", "Browser attachment changed")
            return attachment["service_url"], view.browser_binding
