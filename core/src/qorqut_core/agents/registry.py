"""SQL-only directory operations. Configuration is captured once at registration."""

from __future__ import annotations

import hashlib
from uuid import UUID

from ..app import Change, Operation, register_actor
from ..contracts import Actor, CoreError, Mutation
from ..storage.events import EventInput, append_event
from .models import (
    DEFAULT_PERSONA, AgentCreated, AgentJob, AgentPage, AgentView, AgentsCreate,
    AgentsGet, AgentsList, AgentsStatusSet, AgentsUpdate, status_text,
)


def get_row(tx, agent_id):
    row = tx.connection.execute("SELECT * FROM agents WHERE id=?", (str(agent_id),)).fetchone()
    if row is None:
        raise CoreError("not_found", "Agent not found")
    return row


def view(row) -> AgentView:
    return AgentView(
        **{key: row[key] for key in ("id", "actor_id", "conversation_id", "display_name", "icon_key", "kind",
            "revision", "lifecycle_revision", "lifecycle", "created_by", "created_at", "updated_at",
            "workspace_initialized_at", "projection_revision")},
        workspace_path=f"agents/{row['id']}/workspace", start_requested=bool(row["start_requested"]),
        cli={"provider": row["cli_provider"], "model": row["model"]},
        resources={key: row[key] for key in ("cpu", "memory_mib", "disk_gib")},
        status={"text": row["status_text"], "revision": row["status_revision"],
                "updated_at": row["status_updated_at"], "reviewed_at": row["status_reviewed_at"],
                "actor_id": row["status_actor_id"]})


def reserved(tx, *, required=True):
    installation = tx.connection.execute("SELECT * FROM installation WHERE singleton=1").fetchone()
    host = tx.connection.execute("SELECT * FROM agents WHERE kind='host'").fetchone()
    pointer = installation["onbash_agent_id"]
    if host is None and pointer is None:
        if required:
            raise CoreError("not_ready", "Reserved agent is missing; run explicit installation setup")
        return None
    if host is None or pointer != host["id"]:
        raise CoreError("target_changed", "Reserved agent identity disagrees; explicit setup inspection is required")
    actor = tx.connection.execute("SELECT * FROM actors WHERE actor_id=?", (host["actor_id"],)).fetchone()
    if actor is None or actor["kind"] != "agent" or actor["agent_id"] != host["id"]:
        raise CoreError("target_changed", "Reserved agent actor disagrees; explicit setup inspection is required")
    return host


def advance(revision):
    if revision == 2**63 - 1:
        raise CoreError("storage_blocked", "Agent revision exhausted")
    return revision + 1


class Agents:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def operations(self):
        return (
            Operation("agents_create", AgentsCreate, True, self.create, AgentCreated),
            Operation("agents_get", AgentsGet, False, self.get, AgentView),
            Operation("agents_list", AgentsList, False, self.list, AgentPage),
            Operation("agents_update", AgentsUpdate, True, self.update, AgentView),
            Operation("agents_status_set", AgentsStatusSet, True, self.status_set, AgentView),
        )

    def ensure_onbash(self, context):
        # Validate even before immutable receipt replay so corruption cannot be
        # concealed by a saved successful result. Defaults are not hash inputs.
        with self.db.read() as tx:
            reserved(tx, required=False)
        operation = Operation("installation_ensure_onbash", Mutation, True, self._ensure, AgentCreated)
        self.db.fault("onbash_before_registration")
        result = self.app.execute(operation, context, {"idempotency_key": "onbash:v1"})
        self.db.fault("onbash_after_registration")
        with self.db.read() as tx:
            reserved(tx)
        return result

    def _ensure(self, tx, args, context, operation_id):
        existing = reserved(tx, required=False)
        if existing is not None:
            return Change(AgentCreated(agent=view(existing), provisioning_job_id=existing["provisioning_job_id"]), outcome="accepted")
        defaults = self.app._view(tx).defaults
        return self._register(tx, context, operation_id, display_name="onbash", icon_key="terminal",
            cli=defaults.cli, resources=defaults.resources, persona=DEFAULT_PERSONA, start=False, kind="host")

    def _register(self, tx, context, operation_id, *, display_name, icon_key, cli, resources, persona, start, kind):
        tx.require_write()
        agent_id, actor_id, conversation_id = (self.db.id_factory() for _ in range(3))
        register_actor(tx, Actor(actor_id=actor_id, kind="agent", agent_id=agent_id))
        job_id = self.app.jobs.enqueue(tx, "agent_provision", AgentJob(agent_id=agent_id, expected_lifecycle_revision=1),
            operation_id=operation_id, target_id=agent_id, expected_generation=1)
        now = self.db.clock()
        tx.connection.execute("""INSERT INTO agents (id,actor_id,conversation_id,kind,display_name,icon_key,
            cli_provider,model,cpu,memory_mib,disk_gib,revision,lifecycle_revision,lifecycle,start_requested,
            created_by,created_at,updated_at,persona_seed,status_text,status_revision,status_updated_at,
            status_reviewed_at,status_actor_id,creation_operation_id,provisioning_job_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1,1,'provisioning',?,?,?,?,?, 'Initializing.',1,?,?,?,?,?)""",
            (str(agent_id), str(actor_id), str(conversation_id), kind, display_name, icon_key,
             cli.provider, cli.model, resources.cpu, resources.memory_mib, resources.disk_gib,
             int(start), str(context.actor_id), now, now, persona, now, now, str(context.actor_id),
             str(operation_id), str(job_id)))
        if kind == "host":
            tx.connection.execute("UPDATE installation SET onbash_agent_id=? WHERE singleton=1", (str(agent_id),))
        self.db.fault("agent_after_registration")
        payload = {"agent_id": str(agent_id), "actor_id": str(actor_id), "conversation_id": str(conversation_id),
            "kind": kind, "display_name": display_name, "icon_key": icon_key,
            "cli": cli.model_dump(mode="json"), "resources": resources.model_dump(mode="json"),
            "revision": 1, "lifecycle_revision": 1, "lifecycle": "provisioning", "start_requested": start,
            "created_by": str(context.actor_id), "created_at": now, "provisioning_job_id": str(job_id),
            "persona_sha256": hashlib.sha256(persona.encode("utf-8")).hexdigest(),
            "persona_length": len(persona.encode("utf-8"))}
        append_event(tx, EventInput(kind="agent.created", actor_id=context.actor_id, agent_id=agent_id,
            operation_id=operation_id, payload=payload))
        seq = append_event(tx, EventInput(kind="agent.status_changed", actor_id=context.actor_id,
            agent_id=agent_id, operation_id=operation_id,
            payload={"old_text": None, "new_text": "Initializing.", "status_revision": 1}))
        self.db.fault("agent_after_events")
        return Change(AgentCreated(agent=view(get_row(tx, agent_id)), provisioning_job_id=job_id), seq, "accepted")

    def create(self, tx, args, context, operation_id):
        resources = args.resources if args.resources is not None else self.app._view(tx).defaults.resources
        return self._register(tx, context, operation_id, display_name=args.display_name, icon_key=args.icon_key,
            cli=args.cli, resources=resources, persona=args.persona, start=args.start, kind="vm")

    def get(self, tx, args, context, operation_id):
        return Change(view(get_row(tx, args.agent_id)))

    def list(self, tx, args, context, operation_id):
        host = reserved(tx)
        epoch = tx.connection.execute("SELECT recovery_epoch FROM installation WHERE singleton=1").fetchone()[0]
        if args.recovery_epoch is not None and str(args.recovery_epoch) != epoch:
            raise CoreError("target_changed", "Agent cursor belongs to another recovery epoch")
        rows = tx.connection.execute("SELECT * FROM agents WHERE id>? ORDER BY id LIMIT ?",
            (str(args.after_id) if args.after_id is not None else "", args.limit + 1)).fetchall()
        items = [view(row) for row in rows[:args.limit]]
        return Change(AgentPage(items=items, next_after_id=items[-1].id if items else args.after_id,
            has_more=len(rows) > args.limit, recovery_epoch=epoch, onbash_agent_id=host["id"]))

    def update(self, tx, args, context, operation_id):
        before = get_row(tx, args.agent_id)
        if before["revision"] != args.expected_revision:
            raise CoreError("revision_conflict", "Agent presentation revision changed")
        old = {key: before[key] for key in ("display_name", "icon_key")}
        new = {key: getattr(args, key) if getattr(args, key) is not None else value for key, value in old.items()}
        if old == new:
            return Change(view(before))
        revision = advance(before["revision"])
        tx.connection.execute("UPDATE agents SET display_name=?,icon_key=?,revision=?,updated_at=? WHERE id=?",
            (new["display_name"], new["icon_key"], revision, self.db.clock(), before["id"]))
        seq = append_event(tx, EventInput(kind="agent.presentation_changed", actor_id=context.actor_id,
            agent_id=args.agent_id, operation_id=operation_id,
            payload={"old": old, "new": new, "revision": revision}))
        self.app.jobs.enqueue(tx, "agent_project", AgentJob(agent_id=args.agent_id,
            expected_lifecycle_revision=before["lifecycle_revision"]), operation_id=operation_id,
            target_id=args.agent_id, expected_generation=before["lifecycle_revision"])
        return Change(view(get_row(tx, args.agent_id)), seq, "accepted")

    def status_set(self, tx, args, context, operation_id):
        before = get_row(tx, args.agent_id)
        if before["status_revision"] != args.expected_revision:
            raise CoreError("revision_conflict", "Agent status revision changed")
        text, now = status_text(args.text), self.db.clock()
        seq = None
        if text == before["status_text"]:
            tx.connection.execute("UPDATE agents SET status_reviewed_at=? WHERE id=?", (now, before["id"]))
        else:
            revision = advance(before["status_revision"])
            tx.connection.execute("""UPDATE agents SET status_text=?,status_revision=?,status_updated_at=?,
                status_reviewed_at=?,status_actor_id=? WHERE id=?""",
                (text, revision, now, now, str(context.actor_id), before["id"]))
            seq = append_event(tx, EventInput(kind="agent.status_changed", actor_id=context.actor_id,
                agent_id=args.agent_id, operation_id=operation_id,
                payload={"old_text": before["status_text"], "new_text": text, "status_revision": revision}))
        return Change(view(get_row(tx, args.agent_id)), seq)
