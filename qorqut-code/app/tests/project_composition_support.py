"""One inert synthetic composed host for the Project-record UI/backend qualification.

The real host composition (assembled schema and catalog, owned mutation admission, ``create_app`` with the dashboard
compatibility routes, owner origin checks and ``manage_lifecycle=False``) serves the combined dashboard's own static
files. Everything is synthetic and disposable: a pytest ``tmp_path`` instance, rows created through the real
operations (or small explicit stored shapes where an existing provider/Signoff/history fixture requires them), one
loopback ephemeral HTTP server and no model, native agent, provider delivery, routine/job execution or live data.

The one non-product route below is the fixture control channel (``/__fixture/...``): it only applies stored-state
changes that a concurrent writer or an authorization change would cause, so the browser can exercise the real app's
own denial and conflict behaviour. It never answers a product read and never stands in for an operation handler.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import uvicorn
from qorqut_operations.contracts import InvocationOptions, new_id, plain, utc_now
from starlette.responses import JSONResponse

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.background.integration import attach_background
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from bazarlyk.transport.compatibility import DashboardCompatibility
from host_foundation_support import (build_instance, create_registered_global, register_sources, select_coordinator)

CODE = Path(__file__).resolve().parents[2]
DASHBOARD = CODE / "biz-dashboard"
BROWSER_TEST = DASHBOARD / "tests" / "test_project_composition_browser.js"


def agentic_installed_identity():
    """The installed ``qorqut-agentic`` distribution this interpreter resolves, or an explicit skip.

    The composed fixture must run against the distribution the environment actually installed: it never publishes local
    stand-in metadata, so the version validation inside ``bazarlyk.agentic.installation`` reads the real wheel.
    """
    import importlib.metadata
    try:
        distribution = importlib.metadata.distribution("qorqut-agentic")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("qorqut-agentic is not installed in this environment; the composed fixture needs the real distribution")
    return {"version": distribution.version, "origin": str(distribution.locate_file(""))}


# -- invocation helpers --------------------------------------------------------------------------------------------------

def call(host, ctx, name, arguments=None, *, revision=None, key=None):
    return host.runtime.application.invoke(
        ctx, name, arguments or {}, InvocationOptions(idempotency_key=key or new_id(), expected_revision=revision))


def ok(result):
    assert result.status in {"ok", "accepted"}, result.to_dict()
    return plain(result.data)


def seed_project(host, *, name, slug, member=None, archived=False, one_liner=None):
    """An existing Project row as Project creation seeds it, with the owner's membership."""
    project = new_id()
    root = host.instance.paths.instance_root
    (root / "business" / slug / "hq").mkdir(parents=True, exist_ok=True)
    host.instance.paths.business_root(slug).mkdir(parents=True, exist_ok=True)
    with host.database.transaction() as uow:
        uow.repository("businesses").insert({
            "id": project, "slug": slug, "name": name, "stage": "operating",
            "workspace": str(root / "business" / slug), "hq_path": str(root / "business" / slug / "hq"),
            "config_json": json.dumps({"one_liner": one_liner or f"{name} is a synthetic composed-check project."}),
            "archived_at": utc_now() if archived else None})
        uow.repository("business_memberships", business_id=project).insert(
            {"principal_id": host.owner_id if member is None else member, "is_owner": True})
    return project


def owner_context(host, business):
    """The installation owner's current context for one Project (memberships refreshed like any invocation)."""
    with host.database.read() as uow:
        return host.runtime.policy.context_for(host.owner_id, business_id=business, review_business_id=business,
                                               uow=uow, source="dashboard")


def owner_global(host):
    with host.database.read() as uow:
        return host.runtime.policy.context_for(host.owner_id, business_id=None, review_business_id=None,
                                               uow=uow, source="dashboard")


def insert_tasks(host, project, count, *, prefix, updated="2026-10-20T00:00:00Z", status="open", task_type="task"):
    ids = [new_id() for _ in range(count)]
    with host.database.transaction() as uow:
        uow.connection.executemany(
            "INSERT INTO tasks(business_id,id,title,status,fields_json,archived,created_by,revision,created_at,updated_at,kind) "
            "VALUES(?,?,?,?, ?,0,?,1,?,?,?)",
            [(project, identity, f"{prefix} {position:02d}", status,
              json.dumps({"$task": {"task_type": task_type}} if task_type != "task" else {}), host.owner_id,
              updated, updated, "ordinary") for position, identity in enumerate(ids)])
    return ids


def insert_task_row(host, project, identity, title, *, updated="2026-10-20T00:00:00Z", fields=None):
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO tasks(business_id,id,title,status,fields_json,archived,created_by,revision,created_at,updated_at,kind) "
            "VALUES(?,?,?,'open',?,0,?,1,?,?, 'ordinary')",
            (project, identity, title, json.dumps(fields or {}), host.owner_id, updated, updated))


def link_row(host, project, *, target_type, target_id, target_business_id, created_by=None):
    """One stored explicit association, for a state the API itself never creates (an owned record's retained link)."""
    identity = new_id()
    namespace = "global" if target_business_id is None else "business:" + target_business_id
    now = utc_now()
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO project_item_links(id,business_id,target_type,target_id,target_business_id,target_namespace,"
            "revision,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?,?)",
            (identity, project, target_type, target_id, target_business_id, namespace,
             created_by or host.owner_id, now, now))
    return identity


def insert_reminder(host, project, text, at="2026-11-01T09:00:00+00:00"):
    identity, now = new_id(), "2026-10-30T09:00:00Z"
    source = ("business:" + project if project else "global")
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO telegram_outbox(id,chat_name,chat_id,kind,source_kind,source_key,part,payload_json,state,"
            "binding_fingerprint,config_generation,agent_id,created_at,updated_at,next_attempt_at) "
            "VALUES(?,'ordubashy',1,'text','owner_reminder',?,?,?,'queued',?,1,?,?,?,?)",
            (identity, source + "/" + identity, at, json.dumps({"text": text}), "0" * 64,
             host.coordinator_id, now, now, at))
    return identity


def insert_routine(host, project, name):
    identity, now = new_id(), "2026-10-01T00:00:00Z"
    namespace = project or "global"
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO routines(namespace,business_id,id,name,scope,target,schedule,timezone,status,job_kind,"
            "arguments_json,timeout_seconds,metadata_json,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,'business','business','0 6 * * *','UTC','paused','test.fixture','{}',900,'{}',1,?,?)",
            (namespace, project, identity, name, now, now))
    return identity


def insert_run(host, routine, project, *, state="succeeded", trigger="schedule", started="2026-10-27T09:00:00Z", job_id=None):
    identity = new_id()
    result = {"job_id": job_id} if job_id else {}
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO routine_runs(id,namespace,routine_id,business_id,trigger,slot,state,started_at,result_json,"
            "cost_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,NULL,?,?,?,'{}',1,?,?)",
            (identity, project or "global", routine, project, trigger, state, started, json.dumps(result), started, started))
    return identity


def insert_job(host, project, *, kind="test.fixture", state="succeeded"):
    identity, now = new_id(), "2026-10-27T09:00:00Z"
    namespace = "business:" + project if project else "global"
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO jobs(id,business_id,target_namespace,kind,principal_id,initiated_by_principal_id,"
            "arguments_json,definition_ref_json,dedupe_key,state,attempts,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'{}','{}',?,?,1,1,?,?)",
            (identity, project, namespace, kind, host.owner_id, host.owner_id, "composed-" + identity, state, now, now))
    return identity


def insert_table(host, project, title, columns, rows):
    """A Project table and its rows through the real table operations (bounded rows, real revisions)."""
    owner = owner_context(host, project)
    table = ok(call(host, owner, "tables_create", {"title": title, "description": "Synthetic launch plan.",
                                                   "columns": columns}))["table"]
    created = ok(call(host, owner, "tables_rows_write",
                      {"table_id": table["id"], "rows": [{"cells": cells} for cells in rows]},
                      revision=table["revision"]))
    return created["table"]


def insert_ticket(host, project, title, details, *, resolution="", kind="bug", status="closed"):
    identity, now = new_id(), "2026-10-20T00:00:00Z"
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO dev_tickets(id,title,details,kind,status,reporter_id,business_id,created_at,updated_at,revision,"
            "resolution) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
            (identity, title, details, kind, status, host.owner_id, project, now, now, resolution))
    return identity


SIGNOFF_PREVIEW = {
    "whatsapp": {"title": "Send the harbor update", "rationale": "The pilot group needs the confirmed time.",
                 "affected_scope": {"kind": "business", "business_ids": []},
                 "changes": [{"kind": "structured",
                              "resource": {"resource_type": "whatsapp_account", "resource_id": "wa-account-1",
                                           "label": "Owner WhatsApp"},
                              "fields": [{"path": "chat_id", "before": None, "after": "15550001111@s.whatsapp.net"},
                                         {"path": "chat_name", "before": None, "after": "Harbor Pilot"},
                                         {"path": "text", "before": None,
                                          "after": "Harbor update — the pilot starts at 09:00.\n\nBring the signed brief.\n\n— Ordubashy"}]}],
                 "effects": []},
    "email": {"title": "Send the harbor launch note", "rationale": "The owner's mailing list asked for the launch date.",
              "affected_scope": {"kind": "global", "business_ids": []},
              "changes": [{"kind": "structured",
                           "resource": {"resource_type": "fastmail_account", "resource_id": "mailbox-1",
                                        "label": "Owner mailbox"},
                           "fields": [{"path": "from_address", "before": None, "after": "owner@example.test"},
                                      {"path": "to", "before": None, "after": ["pilot@example.test"]},
                                      {"path": "subject", "before": None, "after": "The harbor pilot starts on Monday"},
                                      {"path": "body", "before": None,
                                       "after": "Hello from Harbor Light,\n\nThe pilot starts on Monday at 09:00. "
                                                "The signed brief is attached to this note.\n\n— Ordubashy"}]}],
              "effects": []},
}


def insert_signoff(host, project, key, *, preview=None, operation="whatsapp_send", state="pending"):
    """One retained canonical Signoff row as the provider operation stored it; no provider action is executed.

    The WhatsApp and historical email previews keep the exact frozen envelope the canonical Signoff card reads
    (``whatsapp_account``/``fastmail_account`` structured changes). No send, approval or provider operation follows.
    """
    from qorqut_operations.contracts import canonical_bytes

    identity, now = new_id(), "2026-10-26T09:00:00Z"
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO signoffs(id,review_business_id,review_scope_kind,proposer_principal_id,proposer_agent_id,"
            "initiated_by_principal_id,operation_name,operation_version,proposal_revision,payload_hash,change_set,"
            "preview_json,expected_targets_json,policy_binding_json,affected_scope_json,state,status_revision,"
            "updated_sequence,created_at,updated_at,category) "
            "VALUES(?,?,?,?,?,?,?,1,1,?,?,?,?,?,?,?,1,?,?,?,'internal_change')",
            (identity, project, "business" if project else "global", host.owner_id, host.coordinator_id,
             host.owner_id, operation, "sha256:" + "0" * 64, canonical_bytes({"composed": key}),
             json.dumps(preview), "[]",
             json.dumps({"operations": [{"binding": {"global_capability": "read_global_context"}}]}),
             json.dumps({"kind": "business" if project else "global",
                         "business_ids": [project] if project else []}),
             state, uow.next_sequence("signoffs"), now, now))
    return identity


# -- fixture control (stored-state changes only) ----------------------------------------------------------------------

async def _read_body(receive):
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.request":
            body += message.get("body", b"")
            if not message.get("more_body", False):
                return body
        elif message["type"] == "http.disconnect":
            return body


def _loopback(scope) -> bool:
    try:
        import ipaddress
        return bool(scope.get("client")) and ipaddress.ip_address(scope["client"][0]).is_loopback
    except (ValueError, TypeError):
        return False


class FixtureControl:
    """The fixture-only stored-state channel; every other request goes to the real composed ASGI application."""

    def __init__(self, app, host):
        self.app, self.host = app, host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and str(scope.get("path", "")).startswith("/__fixture/"):
            if not _loopback(scope):
                return await JSONResponse({"error": "loopback only"}, status_code=403)(scope, receive, send)
            body = json.loads(await _read_body(receive) or b"{}")
            action = str(scope["path"]).removeprefix("/__fixture/")
            try:
                data = self.apply(action, body)
            except Exception as exc:                       # the fixture reports its own mistake to the test
                return await JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)(scope, receive, send)
            return await JSONResponse(data)(scope, receive, send)
        return await self.app(scope, receive, send)

    def apply(self, action, body):
        host = self.host
        if action == "revoke-membership":
            with host.database.transaction() as uow:
                uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                                       (body["business_id"], host.owner_id))
            return {"revoked": body["business_id"]}
        if action == "restore-membership":
            with host.database.transaction() as uow:
                uow.connection.execute("UPDATE business_memberships SET active=1 WHERE business_id=? AND principal_id=?",
                                       (body["business_id"], host.owner_id))
            return {"restored": body["business_id"]}
        if action == "touch-task":
            # A concurrent writer changed the Task between the browser's read and Save.
            with host.database.transaction() as uow:
                row = uow.connection.execute("SELECT fields_json,revision FROM tasks WHERE id=?", (body["task_id"],)).fetchone()
                fields = json.loads(row["fields_json"])
                if body.get("task_type") or body.get("overview") is not None:
                    task = fields.setdefault("$task", {})
                    if body.get("task_type"):
                        task["task_type"] = body["task_type"]
                    if body.get("overview") is not None:
                        task["overview"] = body["overview"]
                uow.connection.execute("UPDATE tasks SET fields_json=?,revision=revision+1,updated_at=? WHERE id=?",
                                       (json.dumps(fields), utc_now(), body["task_id"]))
            return {"touched": body["task_id"]}
        raise ValueError("Unknown fixture action: " + action)


# -- composition ------------------------------------------------------------------------------------------------------

@dataclass
class ComposedHost:
    instance: object
    runtime: object
    database: object
    app: object
    origin: str
    owner_id: str
    coordinator_id: str
    fixture: dict = field(default_factory=dict)
    server: object = None
    thread: object = None
    sock: object = None
    closed: bool = False

    def context(self, business):
        return owner_context(self, business)

    def global_context(self):
        return owner_global(self)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.server is not None:
                self.server.should_exit = True
                if self.thread is not None:
                    self.thread.join(timeout=20)
        finally:
            try:
                self.runtime.release_instance()
            finally:
                self.database.close()
                if self.sock is not None:
                    try:
                        self.sock.close()
                    except OSError:
                        pass


def build_composed(tmp_path, *, deadline=20.0):
    """Build the synthetic host, seed it through real operations and serve the real combined dashboard."""
    installed_agentic = agentic_installed_identity()
    # Reserve the ephemeral loopback port before the origin policy is created; no request is served yet.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"

    instance, ids = build_instance(Path(tmp_path) / "instance", businesses=("harbor-light",))
    host = ComposedHost(instance=instance, runtime=instance.runtime, database=instance.database, app=None,
                        origin=origin, owner_id=instance.owner, coordinator_id="")
    harbor = ids["harbor-light"]
    with host.database.transaction() as uow:
        row = uow.repository("businesses", business_id=harbor).get(harbor)
        uow.repository("businesses", business_id=harbor).update(
            harbor, {"name": "Harbor Light", "config_json": json.dumps(
                {"one_liner": "A synthetic harbor pilot used by the composed qualification."})},
            expected_revision=row["revision"])
    register_sources(instance, harbor, "harbor-light")
    coordinator = create_registered_global(instance, "ordubashy")
    select_coordinator(instance, coordinator)
    host.coordinator_id = coordinator
    attach_background(instance.runtime, instance.runtime.programs)
    instance.runtime.freeze_registry()

    maple = seed_project(host, name="Maple Works", slug="maple-works")
    hidden = seed_project(host, name="Hidden Ridge", slug="hidden-ridge", member=_other_principal(host))
    for business in (harbor, maple, hidden):
        with host.database.transaction() as uow:
            instance.runtime.authority.register_workers(uow, [business])

    fixture = _seed(host, harbor=harbor, maple=maple, hidden=hidden)
    host.fixture = fixture

    authentication = Authentication(instance.database, instance.runtime.paths)
    app = create_app(instance.runtime, dashboard_root=DASHBOARD, origins=[origin],
                     authentication=authentication, compatibility=DashboardCompatibility(instance.runtime),
                     manage_lifecycle=False)
    host.app = FixtureControl(app, host)
    config = uvicorn.Config(host.app, host="127.0.0.1", port=port, log_level="warning", lifespan="auto",
                            access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="composed-project-http", daemon=True)
    thread.start()
    host.server, host.thread, host.sock = server, thread, sock
    limit = time.monotonic() + deadline
    while not server.started and thread.is_alive() and time.monotonic() < limit:
        time.sleep(0.02)
    assert server.started, "the composed HTTP server did not start"
    assert agentic_installed_identity() == installed_agentic, \
        "the composed fixture must not publish or shadow qorqut-agentic distribution metadata"
    return host


def _other_principal(host):
    identity = new_id()
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": identity, "kind": "human", "display_name": "Other Owner"})
    return identity


def _seed(host, *, harbor, maple, hidden):
    """Seed the composed fixture through real operations (plus the documented stored shapes)."""
    owner = host.context(harbor)
    owner_maple = host.context(maple)
    global_ctx = host.global_context()

    # Work: 27 bulk top-level Tasks (more than one 25-item page), a Story with a child, a Customer row and the
    # documents the ordinary composition registers.
    bulk = insert_tasks(host, harbor, 27, prefix="Harbor task", updated="2026-10-05T08:00:00Z")
    for position, identity in enumerate(bulk):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                                   (f"2026-10-20T10:{59 - position:02d}:00Z", identity))
    story = ok(call(host, owner, "tasks_create", {"title": "Harbor launch story", "task_type": "story",
                                                   "overview": "First paragraph of the launch story.\n\nSecond paragraph with the decision context."}))["task"]
    child = ok(call(host, owner, "tasks_create", {"title": "Draft the harbor brief", "parent_id": story["id"]}))["task"]
    ordinary = ok(call(host, owner, "tasks_create", {"title": "Ordinary harbor work"}))["task"]
    convertible = ok(call(host, owner, "tasks_create", {"title": "Convertible harbor work"}))["task"]
    customer_id = new_id()
    insert_task_row(host, harbor, customer_id, "Harbor Pilot Customer", updated="2026-10-20T11:30:00Z",
                    fields={"$task": {"task_type": "customer", "customer_kind": "company",
                                      "customer_profile": {"contact_name": "Pilot Buyer", "email": "buyer@example.test"}}})
    # A foreign Project's Task, a Global Task and a candidate retained for the UI's own Link/Unlink.
    foreign = ok(call(host, owner_maple, "tasks_create", {"title": "Maple shared deliverable"}))["task"]
    foreign_candidate = ok(call(host, owner_maple, "tasks_create", {"title": "Maple candidate deliverable"}))["task"]
    global_task = ok(call(host, global_ctx, "tasks_create", {"title": "Global follow-up", "scope": "global"}))["task"]
    global_linked = ok(call(host, global_ctx, "tasks_create", {"title": "Global harbor brief", "scope": "global"}))["task"]
    ok(call(host, owner, "project_items_link", {"project_id": harbor, "item": {
        "type": "task", "id": global_linked["id"], "business_id": None}}))
    # The same canonical id in two scopes: one owned by the Project, one linked from the other scope.
    shared_id = new_id()
    insert_task_row(host, harbor, shared_id, "Shared scoped deliverable", updated="2026-10-20T11:00:00Z")
    insert_task_row(host, maple, shared_id, "Maple shared-scope copy", updated="2026-10-20T11:01:00Z")
    linked_foreign = ok(call(host, owner, "project_items_link", {"project_id": harbor, "item": {
        "type": "task", "id": foreign["id"], "business_id": maple}}))
    linked_shared = ok(call(host, owner, "project_items_link", {"project_id": harbor, "item": {
        "type": "task", "id": shared_id, "business_id": maple}}))
    # An owned record keeps an explicit retained association (a stored link row; the API treats an owned target as
    # a no-op, so this state is seeded to exercise deduplication of the two associations).
    owned_linked_id = ok(call(host, owner, "tasks_create", {"title": "Owned and linked deliverable"}))["task"]["id"]
    link_row(host, harbor, target_type="task", target_id=owned_linked_id, target_business_id=harbor)

    # A reachable target linked while reach existed, then revoked: retained association, no label now.
    _grant_membership(host, hidden)
    hidden_task = ok(call(host, host.context(hidden), "tasks_create", {"title": "Hidden deliverable"}))["task"]
    ok(call(host, host.context(harbor), "project_items_link", {"project_id": harbor, "item": {
        "type": "task", "id": hidden_task["id"], "business_id": hidden}}))
    _revoke_membership(host, hidden)

    # Action and Context/document reads.
    action = ok(call(host, owner, "actions_record", {
        "text": "Confirmed the harbor pilot window with the supplier", "category": "business-operational",
        "real_time": "2026-10-20", "business_ids": [harbor]}))["action"]
    # The inventory's canonical Action identity is the Global-stored action_key, not its bare storage row id.
    action_item = next(item for item in ok(call(host, owner, "project_items_list", {
        "project_id": harbor, "section": "actions", "limit": 100}))["items"] if item["type"] == "action")
    register_docs = [row for row in _registered_documents(host, harbor)]
    topic = ok(call(host, owner, "context_topics_create", {"title": "Harbor pilot", "slug": "harbor-pilot"}))
    long_note = ok(call(host, owner, "context_notes_create", {
        "topic_id": topic["document"]["id"], "title": "Harbor pilot brief",
        "body": "The harbor pilot brief.\n\n" + "\n\n".join(
            f"Section {position}: " + ("the pilot check keeps the full authorized body " * 20) for position in range(1, 18))}))
    table = insert_table(host, harbor, "Harbor launch plan",
                         [{"id": "channel", "name": "Channel", "type": "text"},
                          {"id": "date", "name": "Date", "type": "text"}],
                         [{"channel": f"Channel {position}", "date": f"2026-11-{position:02d}"} for position in range(1, 61)])

    # Reminder, routine -> runs -> exact job, and a Dev report.
    reminder_id = insert_reminder(host, harbor, "Call the harbor pilot owner")
    routine = insert_routine(host, harbor, "Harbor pilot digest")
    job = insert_job(host, harbor)
    run_one = insert_run(host, routine, harbor, state="succeeded", started="2026-10-27T09:00:00Z", job_id=job)
    insert_run(host, routine, harbor, state="failed", trigger="manual", started="2026-10-26T09:00:00Z")
    # More runs than one page; the exact referenced job's successful run stays on the continuation page.
    for position in range(30):
        insert_run(host, routine, harbor, state="failed", started=f"2026-10-28T09:{position:02d}:00Z")
    ticket = insert_ticket(host, harbor, "Harbor checkout retry loop",
                           "First paragraph of the harbor report.\n\nSecond paragraph records the retry evidence.",
                           resolution="Replaced the retry guard and verified the harbor flow.", status="open")
    standalone_ticket = insert_ticket(host, None, "Standalone harbor report",
                                      "The standalone first paragraph.\n\nThe standalone second paragraph.",
                                      resolution="")

    # Signoffs: a Project-scoped WhatsApp proposal and a retained historical email proposal in Global scope,
    # explicitly linked into this Project. Neither is approved, applied or sent.
    whatsapp = insert_signoff(host, harbor, "harbor-whatsapp", preview=SIGNOFF_PREVIEW["whatsapp"])
    email = insert_signoff(host, None, "harbor-email", preview=SIGNOFF_PREVIEW["email"], operation="account_email_send")
    email_link = ok(call(host, owner, "project_items_link", {"project_id": harbor, "item": {
        "type": "signoff", "id": email, "business_id": None}}))

    # A Story's related Signoff/Action rows come from the existing relationship contract.
    ok(call(host, owner, "relationships_create", {
        "source": {"type": "task", "id": story["id"]},
        "target": {"type": "signoff", "id": whatsapp}, "role": "concerns"}))
    ok(call(host, owner, "relationships_create", {
        "source": {"type": "task", "id": story["id"]},
        "target": {"type": "action", "id": action_item["id"]}, "role": "concerns"}))

    # A private message on the Story and the Project creates their Threads entries; the ordinary Task gets one too.
    for source, text in (({"type": "project", "id": harbor, "business_id": harbor}, "Project private seed message"),
                         ({"type": "task", "id": story["id"], "business_id": harbor}, "Story private seed message"),
                         ({"type": "task", "id": foreign_candidate["id"], "business_id": maple}, "Foreign private seed"),
                         ({"type": "task", "id": global_linked["id"], "business_id": None}, "Global private seed")):
        ctx = global_ctx if source["business_id"] is None else owner_maple if source["business_id"] == maple else owner
        arguments = {"source": source, "text": text}
        if source["business_id"] is None:
            arguments["scope"] = "global"
        ok(call(host, ctx, "owner_conversation_send", arguments))

    documents = [{"id": row["id"], "path": row["path"]} for row in register_docs]
    with host.database.read() as uow:
        task_titles = sorted(row["title"] for row in uow.connection.execute("SELECT title FROM tasks"))
        link_targets = sorted((row["target_type"], row["target_id"]) for row in uow.connection.execute(
            "SELECT target_type,target_id FROM project_item_links WHERE business_id=?", (harbor,)))
    return {
        "origin": host.origin,
        "projects": {"harbor": harbor, "maple": maple, "hidden": hidden},
        "project_names": {"harbor": "Harbor Light", "maple": "Maple Works", "hidden": "Hidden Ridge"},
        "story": {"id": story["id"], "title": story["title"], "overview": story["overview"], "revision": story["revision"]},
        "child": {"id": child["id"], "title": child["title"]},
        "ordinary": {"id": ordinary["id"], "title": ordinary["title"], "revision": ordinary["revision"]},
        "convertible": {"id": convertible["id"], "title": convertible["title"], "revision": convertible["revision"]},
        "customer": {"id": customer_id, "title": "Harbor Pilot Customer"},
        "bulk": {"ids": bulk, "titles": [f"Harbor task {position:02d}" for position in range(27)]},
        "foreign": {"id": foreign["id"], "title": foreign["title"], "link_id": linked_foreign["link"]["id"]},
        "foreign_candidate": {"id": foreign_candidate["id"], "title": foreign_candidate["title"]},
        "global": {"id": global_task["id"], "title": global_task["title"]},
        "global_linked": {"id": global_linked["id"], "title": global_linked["title"]},
        "shared": {"id": shared_id, "link_id": linked_shared["link"]["id"]},
        "owned_linked": {"id": owned_linked_id, "title": "Owned and linked deliverable"},
        "hidden": {"task_id": hidden_task["id"], "title": hidden_task["title"]},
        "action": {"id": action_item["id"], "storage_id": action["action_id"],
                   "text": "Confirmed the harbor pilot window with the supplier"},
        "topic": {"id": topic["document"]["id"], "title": "Harbor pilot"},
        "document": {"id": long_note["document"]["id"], "title": "Harbor pilot brief"},
        "registered_documents": documents,
        "table": {"id": table["id"], "title": "Harbor launch plan", "rows": 60},
        "reminder": {"id": reminder_id, "text": "Call the harbor pilot owner"},
        "routine": {"id": routine, "name": "Harbor pilot digest", "run_id": run_one, "job_id": job},
        "ticket": {"id": ticket, "title": "Harbor checkout retry loop",
                   "details": "First paragraph of the harbor report.\n\nSecond paragraph records the retry evidence.",
                   "resolution": "Replaced the retry guard and verified the harbor flow."},
        "standalone_ticket": {"id": standalone_ticket, "title": "Standalone harbor report"},
        "signoffs": {"whatsapp": whatsapp, "email": email, "email_link_id": email_link["link"]["id"]},
        "task_titles": task_titles,
        "link_targets": link_targets,
    }


def _grant_membership(host, business):
    with host.database.transaction() as uow:
        row = uow.connection.execute("SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=?",
                                     (business, host.owner_id)).fetchone()
        if row:
            uow.connection.execute("UPDATE business_memberships SET active=1 WHERE business_id=? AND principal_id=?",
                                   (business, host.owner_id))
        else:
            uow.repository("business_memberships", business_id=business).insert(
                {"principal_id": host.owner_id, "is_owner": True})


def _revoke_membership(host, business):
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?",
                               (business, host.owner_id))


def _registered_documents(host, business):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(
            "SELECT id,path,kind FROM documents WHERE business_id=? AND kind!='journal' ORDER BY path", (business,))]


def write_fixture(host, path):
    Path(path).write_text(json.dumps(host.fixture, indent=2) + "\n")