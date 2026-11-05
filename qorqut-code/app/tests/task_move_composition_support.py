"""One small synthetic composed host for the Move to project browser-to-backend qualification.

The real host composition (assembled schema and catalog, owned mutation admission, ``create_app`` with the dashboard
compatibility routes, owner origin checks and ``manage_lifecycle=False``) serves the integrated dashboard's own static files
on one loopback ephemeral port. Everything is synthetic and disposable: a pytest ``tmp_path`` instance, rows created through
the real operations (a few small explicit stored shapes where no operation creates them: a same-id decoy Task, an Email
binding), and no model, native agent, provider delivery, routine/job execution or live data.

The one non-product route below is the fixture control channel (``/__fixture/...``). It never answers a product read and
never stands in for an operation handler. It only (a) applies the stored-state change a concurrent writer would cause, through
the real ``tasks_update`` operation, and (b) returns a read-only snapshot of stored rows so the browser run can prove that a
cancelled or blocked Move changed nothing. Serve it standalone with ``python task_move_composition_support.py DIRECTORY`` to
iterate on the browser driver; the harness prints the base URL and the fixture file and stops on a closed stdin.
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import uvicorn
from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain, utc_now
from starlette.responses import JSONResponse

from bazarlyk.background.integration import attach_background
from bazarlyk.native_chat import store
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from bazarlyk.transport.compatibility import DashboardCompatibility
from chat_input_support import present as chat_present, result_record, wire
from host_foundation_support import build_instance, create_registered_global, register_sources, select_coordinator
from native_chat_support import Native, Pane
from project_composition_support import (CODE, SIGNOFF_PREVIEW, ComposedHost, _loopback, _read_body, call, agentic_installed_identity,
                                         insert_task_row, ok, owner_context, owner_global, seed_project)

DASHBOARD = CODE / "biz-dashboard"
BROWSER_TEST = DASHBOARD / "tests" / "test_task_move_composition_browser.js"

# Every table a Move can touch or must leave alone; the control channel digests them so a cancelled or blocked Move is
# provably a no-op. Row order is stable (rowid), content is the full row.
WATCHED = ("tasks", "thread_messages", "thread_follows", "item_tags", "relationships", "owner_conversations",
           "owner_conversation_messages", "owner_conversation_reads", "focus_cards", "project_item_links", "task_moves",
           "task_snooze_wakes", "task_completions", "inbox_changes", "idempotency_receipts")


# -- stored-row helpers (storage truth, not an operation read) -------------------------------------------------------------

def rows(host, sql, *parameters):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters).fetchall()]


def row_of(host, business, identity):
    found = rows(host, "SELECT * FROM tasks WHERE business_id IS ? AND id=?", business, identity)
    return found[0] if found else None


def digest(host):
    """A SHA-256 per watched table over every stored row, plus a readable index of where each Task lives."""
    tables = {}
    for name in WATCHED:
        content = json.dumps(rows(host, f"SELECT * FROM {name} ORDER BY rowid"), sort_keys=True, default=str)
        tables[name] = hashlib.sha256(content.encode()).hexdigest()
    index = {f"{row['business_id'] or 'global'}/{row['id']}": [row["revision"], row["parent_id"], row["title"]]
             for row in rows(host, "SELECT business_id, id, revision, parent_id, title FROM tasks")}
    return {"tables": tables, "tasks": index, "moves": rows(host, "SELECT COUNT(*) AS n FROM task_moves")[0]["n"]}


# -- operations as the owner / as the configured Ordubashy ------------------------------------------------------------------

def scope_args(ctx):
    return {"scope": "global"} if ctx.business_id is None else {}


def make(host, ctx, title, parent=None, **extra):
    arguments = {"title": title, **scope_args(ctx), **({"parent_id": parent} if parent else {}), **extra}
    return ok(call(host, ctx, "tasks_create", arguments))["task"]


def revise(host, ctx, task, name, arguments):
    found = row_of(host, ctx.business_id, task["id"])
    return ok(call(host, ctx, name, {"task_id": task["id"], **scope_args(ctx), **arguments}, revision=found["revision"]))


def agent_context(host, business):
    """The exact configured Ordubashy, as MCP resolves it; Task discussion belongs to the agents."""
    return host.runtime.resolver.invocation_context(Principal(host.coordinator_id, "agent", "Ordubashy"), business_id=business, source="mcp")


def comment(host, business, task, text, refs=()):
    ctx = agent_context(host, business)
    arguments = {"text": text, **({"note_refs": [{"type": "task", "id": ref["id"]} for ref in refs]} if refs else {})}
    return revise(host, ctx, task, "tasks_comment", arguments)


def source_of(business, task):
    return {"type": "task", "id": task["id"], "business_id": business}


def private_message(host, business, task, text):
    ctx = owner_global(host) if business is None else owner_context(host, business)
    return ok(call(host, ctx, "owner_conversation_send", {**({"scope": "global"} if business is None else {}),
                                                         "source": source_of(business, task), "text": text}))


def present(host, business, task, reason):
    return ok(call(host, owner_global(host), "focus_present", {"scope": "global", "source": source_of(business, task),
                                                               "reason": reason, "requested_response": "Decide"}))["item"]


def bind_email(host, business, task, conversation="thread-synthetic-1"):
    """The stored shape a historical Email Task carries (the mailbox bridge, not an operation, creates it)."""
    with host.database.transaction() as uow:
        found = uow.repository("tasks", business_id=business).get(task["id"])
        uow.repository("tasks", business_id=business).update(
            task["id"], {"fields_json": {"$task": {"task_type": "email", "overview": "", "email_conversation_id": conversation}}},
            expected_revision=found["revision"])


def decoy(host, business, identity, title):
    """A different Task that only shares a bare id, in another scope (ids are unique per scope, not per installation)."""
    with host.database.transaction() as uow:
        uow.repository("tasks", business_id=business).insert(
            {"id": identity, "title": title, "status": "open", "created_by": host.owner_id})
    return {"id": identity, "title": title}


def recorder(host, business):
    """One synthetic project owner for the Action writes: a Global owner's Action cannot name a Project Task by ``task_id``."""
    identity = new_id()
    with host.database.transaction() as uow:
        uow.repository("principals").insert({"id": identity, "kind": "human", "display_name": "Launch Recorder"})
        uow.repository("business_memberships", business_id=business).insert({"principal_id": identity, "is_owner": True})
    with host.database.read() as uow:
        return host.runtime.policy.context_for(identity, business_id=business, review_business_id=business, uow=uow, source="dashboard")


def decided_signoff(host, business, task, title):
    """One retained, decided Signoff whose frozen expected target is this Task (stored as a proposal stores it; nothing is sent)."""
    preview = copy.deepcopy(SIGNOFF_PREVIEW["whatsapp"])
    preview["title"] = title
    identity, now = new_id(), utc_now()
    target = [{"resource": {"resource_type": "tasks", "resource_id": task["id"], "business_id": business, "namespace": None},
               "revision": None, "content_hash": None, "exists": True}]
    from qorqut_operations.contracts import canonical_bytes

    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO signoffs(id,review_business_id,review_scope_kind,proposer_principal_id,proposer_agent_id,"
            "initiated_by_principal_id,operation_name,operation_version,proposal_revision,payload_hash,change_set,"
            "preview_json,expected_targets_json,policy_binding_json,affected_scope_json,state,status_revision,"
            "updated_sequence,created_at,updated_at,category) "
            "VALUES(?,?,?,?,?,?,?,1,1,?,?,?,?,?,?,?,1,?,?,?,'internal_change')",
            (identity, business, "business", host.owner_id, host.coordinator_id, host.owner_id, "whatsapp_send",
             "sha256:" + "0" * 64, canonical_bytes({"composed": title}), json.dumps(preview), json.dumps(target),
             json.dumps({"operations": [{"binding": {"global_capability": "read_global_context"}}]}),
             json.dumps({"kind": "business", "business_ids": [business]}), "rejected", uow.next_sequence("signoffs"), now, now))
    return identity


class MoveNative(Native):
    """The configured Ordubashy's live native session, adopting this composed host (no provider, model or terminal runs)."""

    def __init__(self, host, provider="claude"):
        self.tmp_path, self.provider = None, provider
        self.instance, self.businesses = host.instance, None
        self.runtime, self.owner, self.agent = host.instance.runtime, host.owner_id, host.coordinator_id
        with self.runtime.database.read() as uow:
            self.profile = store.active_profile(uow.connection, self.agent, self.runtime.paths)
        self.workspace = Path(self.profile["workspace"])
        (self.workspace / ".bazarlyk").mkdir(parents=True, exist_ok=True)
        self.write_registration(True)
        self.runtime_name = {"claude": "claude-code", "codex": "codex"}[provider]
        self.set_runtime(self.runtime_name)
        self.pid, self.started, self.alive, self.transport, self.thread_owner = 4242, "99999", True, "terminal", None
        self.session_id = str(uuid.uuid4())
        self.pane, self.queued, self.codex_alive = Pane(self), [], True
        self.build()


def move_roots(host, business, destination, roots):
    """A real Move through the owner's preview and mutation operations (used for the history this fixture starts from)."""
    ctx = owner_context(host, business)
    selection = {"source_business_id": business, "destination_project_id": destination, "task_ids": [*roots]}
    shown = ok(call(host, ctx, "tasks_move_to_project_preview", selection))["preview"]
    assert shown["can_move"], shown["blockers"]
    return ok(call(host, ctx, "tasks_move_to_project", {**selection, "preview_token": shown["token"]}, key=new_id()))


# -- fixture control (stored-state changes and a read-only digest) ---------------------------------------------------------

class MoveFixtureControl:
    """The fixture-only channel; every other request goes to the real composed ASGI application."""

    def __init__(self, app, host):
        self.app, self.host = app, host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and str(scope.get("path", "")).startswith("/__fixture/"):
            if not _loopback(scope):
                return await JSONResponse({"error": "loopback only"}, status_code=403)(scope, receive, send)
            body = json.loads(await _read_body(receive) or b"{}")
            try:
                data = self.apply(str(scope["path"]).removeprefix("/__fixture/"), body)
            except Exception as exc:                       # the fixture reports its own mistake to the test
                return await JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)(scope, receive, send)
            return await JSONResponse(data)(scope, receive, send)
        return await self.app(scope, receive, send)

    def apply(self, action, body):
        host = self.host
        if action == "digest":
            return digest(host)
        if action == "revise-title":
            # A concurrent owner edit of one Task, through the real revision-checked operation.
            ctx = owner_global(host) if body.get("business_id") is None else owner_context(host, body["business_id"])
            found = row_of(host, body.get("business_id"), body["task_id"])
            result = call(host, ctx, "tasks_update", {"task_id": body["task_id"], **scope_args(ctx), "changes": {"title": body["title"]}},
                          revision=found["revision"])
            assert result.status == "ok", result.error
            return {"revision": plain(result.data)["task"]["revision"]}
        raise ValueError("Unknown fixture action: " + action)


# -- composition ------------------------------------------------------------------------------------------------------------

def build_move_composed(tmp_path, *, deadline=20.0):
    """Build the small synthetic host, seed it through real operations and serve the integrated dashboard."""
    agentic_installed_identity()
    # Reserve the ephemeral loopback port before the origin policy is created; no request is served yet.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"

    instance, ids = build_instance(Path(tmp_path) / "instance", businesses=("alpha",))
    host = ComposedHost(instance=instance, runtime=instance.runtime, database=instance.database, app=None,
                        origin=origin, owner_id=instance.owner, coordinator_id="")
    alpha = ids["alpha"]
    with host.database.transaction() as uow:
        row = uow.repository("businesses", business_id=alpha).get(alpha)
        uow.repository("businesses", business_id=alpha).update(
            alpha, {"name": "Alpha Studio", "config_json": json.dumps({"one_liner": "A synthetic source project."})},
            expected_revision=row["revision"])
    register_sources(instance, alpha, "alpha")
    coordinator = create_registered_global(instance, "ordubashy")
    select_coordinator(instance, coordinator)
    host.coordinator_id = coordinator
    attach_background(instance.runtime, instance.runtime.programs)
    instance.runtime.freeze_registry()
    beta = seed_project(host, name="Beta Lab", slug="beta", one_liner="A synthetic destination project.")
    gamma = seed_project(host, name="Gamma Works", slug="gamma", one_liner="A second synthetic destination project.")
    for business in (alpha, beta, gamma):
        with host.database.transaction() as uow:
            instance.runtime.authority.register_workers(uow, [business])
    host.fixture = seed(host, alpha=alpha, beta=beta, gamma=gamma)

    authentication = Authentication(instance.database, instance.runtime.paths)
    app = create_app(instance.runtime, dashboard_root=DASHBOARD, origins=[origin], authentication=authentication,
                     compatibility=DashboardCompatibility(instance.runtime), manage_lifecycle=False)
    host.app = MoveFixtureControl(app, host)
    config = uvicorn.Config(host.app, host="127.0.0.1", port=port, log_level="warning", lifespan="auto", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="composed-task-move-http", daemon=True)
    thread.start()
    host.server, host.thread, host.sock = server, thread, sock
    limit = time.monotonic() + deadline
    while not server.started and thread.is_alive() and time.monotonic() < limit:
        time.sleep(0.02)
    assert server.started, "the composed HTTP server did not start"
    seed_chat(host)
    return host


def count_rows(host, table, where, *parameters):
    return rows(host, f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", *parameters)[0]["n"]


def counts_for(host, business, ids):
    """What a Move of these Tasks should preserve, counted straight from storage (never from the preview operation)."""
    marks = ",".join("?" * len(ids))
    namespace = "global" if business is None else business
    keys = [f"source:{namespace}:task:{identity}" for identity in ids]
    return {
        "tasks": len(ids),
        "comments": count_rows(host, "thread_messages", f"business_id IS ? AND item_type='task' AND item_id IN ({marks})", business, *ids),
        "private_messages": count_rows(host, "owner_conversation_messages",
                                       "conversation_id IN (SELECT id FROM owner_conversations WHERE source_type='task' "
                                       f"AND business_id IS ? AND source_id IN ({marks}))", business, *ids),
        "project_links": count_rows(host, "project_item_links",
                                    f"target_type='task' AND target_business_id IS ? AND target_id IN ({marks})", business, *ids),
        "focus_cards": count_rows(host, "focus_cards", f"source_key IN ({','.join('?' * len(keys))})", *keys),
        "crm_entries": 0}


def seed(host, *, alpha, beta, gamma):
    """Seed through real operations: a Global Story tree (scenario 1), a project Task with a descendant and notes plus two
    same-id decoys (scenario 2), an Email-blocked Story and a Story whose descendant changes after its preview (scenario 3)."""
    owner_alpha, owner_global_ctx = owner_context(host, alpha), owner_global(host)

    # -- 1. Global Story: descendants (completed + archived), an outside parent, discussion, private chats, Focus and links.
    outside = make(host, owner_global_ctx, "Outside parent")
    unrelated_child = make(host, owner_global_ctx, "Sibling under the outside parent", outside["id"])
    story = make(host, owner_global_ctx, "Funding story", outside["id"], task_type="story", tags=["funding"],
                 overview="Everything about the funding round.", assignee_ids=[host.owner_id])
    done = make(host, owner_global_ctx, "Completed child", story["id"])
    revise(host, owner_global_ctx, done, "tasks_complete", {})
    grandchild = make(host, owner_global_ctx, "Grandchild of the completed child", done["id"])
    archived = make(host, owner_global_ctx, "Archived child", story["id"])
    revise(host, owner_global_ctx, archived, "tasks_update", {"changes": {"archived": True}})
    revise(host, owner_global_ctx, grandchild, "tasks_update", {"changes": {"blocked_by": [archived["id"]]}})
    comment(host, None, story, "Root discussion note", refs=[grandchild])
    comment(host, None, done, "Done discussion note")
    comment(host, None, grandchild, "Deep discussion note")
    private_message(host, None, story, "Private note about the funding story.")
    private_message(host, None, done, "Private note about the completed child.")
    story_card = present(host, None, story, "Funding story needs a decision")
    grandchild_card = present(host, None, grandchild, "Watch the deep child")
    # One project links the Story from outside; the destination links it too, which becomes redundant once it owns the Story.
    outside_link = ok(call(host, owner_alpha, "project_items_link", {"project_id": alpha, "item": source_of(None, story)}))["link"]
    owned_link = ok(call(host, owner_context(host, beta), "project_items_link", {"project_id": beta, "item": source_of(None, story)}))["link"]
    story_ids = [story["id"], done["id"], grandchild["id"], archived["id"]]

    # -- 2. A project Task moved through the drawer: a descendant that moves with it, notes that cite it and a Task left behind,
    #       and two unrelated Tasks that only share a bare id with those cited Tasks (one Global, one in the destination).
    holder = make(host, owner_alpha, "Pilot holder")
    follower = make(host, owner_alpha, "Pilot follow-up", holder["id"])
    behind = make(host, owner_alpha, "Left behind reference")
    plain_task = make(host, owner_alpha, "Alpha plain task")
    global_decoy = decoy(host, None, follower["id"], "Global decoy of the follow-up")
    gamma_decoy = decoy(host, gamma, behind["id"], "Gamma decoy of the left-behind task")
    comment(host, alpha, holder, "Agreed next step is the follow-up", refs=[follower])
    comment(host, alpha, holder, "Also see the task we left behind", refs=[behind])
    comment(host, alpha, follower, "FOLLOWER discussion that moved with its holder")
    comment(host, alpha, behind, "BEHIND discussion that stayed in Alpha")
    comment(host, None, global_decoy, "GLOBAL DECOY discussion that must never be borrowed")
    comment(host, gamma, gamma_decoy, "GAMMA DECOY discussion that must never be borrowed")
    holder_card = present(host, alpha, holder, "Pilot holder follow-up")

    # -- 3a. An Email-bound descendant blocks the Story that contains it.
    blocked = make(host, owner_alpha, "Mailbox story", task_type="story", overview="Contains a mailbox-bound historical Task.")
    mail = make(host, owner_alpha, "Historical email thread", blocked["id"])
    bind_email(host, alpha, mail)
    blocked_card = present(host, alpha, blocked, "Blocked by a mailbox binding")

    # -- 3b. A Story whose descendant is edited by someone else after the preview.
    stale = make(host, owner_alpha, "Pricing story", task_type="story", overview="Pricing decisions.")
    stale_child = make(host, owner_alpha, "Pricing child", stale["id"])
    stale_card = present(host, alpha, stale, "Pricing story needs a decision")

    # -- 4. A project Story whose child already has an Action and a decided Signoff in Alpha when the Story moves to Beta.
    launch = make(host, owner_alpha, "Launch story", task_type="story", overview="The launch plan.")
    launch_child = make(host, owner_alpha, "Launch child", launch["id"])
    launch_action = ok(call(host, recorder(host, alpha), "actions_record", {
        "text": "Validated the launch child before the move", "category": "business-operational", "business_ids": [alpha],
        "task_id": launch_child["id"], "real_time": "2026-10-28"}))["action"]
    launch_signoff = decided_signoff(host, alpha, launch_child, "Approve the launch child")
    launch_card = present(host, alpha, launch, "Launch story needs a decision")

    names = {"alpha": "Alpha Studio", "beta": "Beta Lab", "gamma": "Gamma Works"}
    expected = {"story": counts_for(host, None, story_ids), "holder": counts_for(host, alpha, [holder["id"], follower["id"]]),
                "blocked": counts_for(host, alpha, [blocked["id"], mail["id"]]),
                "stale": counts_for(host, alpha, [stale["id"], stale_child["id"]])}
    return {
        "origin": host.origin,
        "projects": {"alpha": alpha, "beta": beta, "gamma": gamma},
        "project_names": names,
        "story": {"id": story["id"], "title": "Funding story", "ids": story_ids, "outside": outside["id"],
                  "unrelated_child": unrelated_child["id"], "done": done["id"], "grandchild": grandchild["id"],
                  "archived": archived["id"], "titles": {"done": "Completed child", "archived": "Archived child"},
                  "card": story_card["id"], "grandchild_card": grandchild_card["id"],
                  "outside_link": outside_link["id"], "owned_link": owned_link["id"], "expected_counts": expected["story"]},
        "holder": {"id": holder["id"], "title": "Pilot holder", "card": holder_card["id"], "expected_counts": expected["holder"], "follower": follower["id"],
                   "follower_title": "Pilot follow-up", "behind": behind["id"], "behind_title": "Left behind reference",
                   "global_decoy": global_decoy, "gamma_decoy": gamma_decoy, "plain": plain_task["id"],
                   "plain_title": "Alpha plain task",
                   "texts": {"follower": "FOLLOWER discussion that moved with its holder",
                             "behind": "BEHIND discussion that stayed in Alpha",
                             "global_decoy": "GLOBAL DECOY discussion that must never be borrowed",
                             "gamma_decoy": "GAMMA DECOY discussion that must never be borrowed"}},
        "blocked": {"id": blocked["id"], "title": "Mailbox story", "card": blocked_card["id"], "mail": mail["id"],
                    "mail_title": "Historical email thread", "expected_counts": expected["blocked"]},
        "stale": {"id": stale["id"], "title": "Pricing story", "card": stale_card["id"], "child": stale_child["id"],
                  "child_title": "Pricing child", "edited_title": "Pricing child (edited elsewhere)", "expected_counts": expected["stale"]},
        "launch": {"id": launch["id"], "title": "Launch story", "card": launch_card["id"], "child": launch_child["id"],
                   "child_title": "Launch child", "action": {"id": launch_action["action_id"], "label": launch_action["summary"]},
                   "signoff": {"id": launch_signoff, "label": "Approve the launch child"}},
    }


def seed_chat(host):
    """Scenario 5's native Chat: Ordubashy shows four Task references. One names a Task that will move while the page is open,
    one a Task id that was reused in Alpha after its original moved away, and two that are already unavailable."""
    fixture = host.fixture
    alpha, beta, gamma = (fixture["projects"][name] for name in ("alpha", "beta", "gamma"))
    owner = owner_context(host, alpha)
    live = make(host, owner, "Chat pilot task")
    reused = make(host, owner, "Chat original of a reused id")
    gone = make(host, owner, "Chat task that moved before the page loaded")
    gamma_decoy = decoy(host, gamma, live["id"], "Gamma decoy of the chat pilot task")
    references = [source_of(alpha, task) for task in (live, reused, gone)]
    native = MoveNative(host)
    native.publish()
    native.client_user_turn("Show me the chat tasks")
    native.write(result_record(native, wire(chat_present(native, "Look at these tasks.", references))))
    native.pass_()
    host.native = native
    # The original of the reused id and the Task that is gone move away for real; Alpha then holds a different Task with the first id.
    moved = move_roots(host, alpha, beta, [reused["id"], gone["id"]])
    assert sorted(row["id"] for row in moved["moved"]) == sorted([reused["id"], gone["id"]])
    current = decoy(host, alpha, reused["id"], "Alpha Task that reuses the id")
    fixture["chat"] = {"agent_id": host.coordinator_id, "live": {"id": live["id"], "title": "Chat pilot task"},
                       "reused": {"id": reused["id"], "title": current["title"], "original_title": "Chat original of a reused id"},
                       "gone": {"id": gone["id"], "title": "Chat task that moved before the page loaded"},
                       "gamma_decoy": gamma_decoy, "seed_move": {"move_id": moved["move_id"], "ids": [reused["id"], gone["id"]]}}


def write_fixture(host, path):
    Path(path).write_text(json.dumps(host.fixture, indent=2, sort_keys=True))


def close(host):
    host.close()


if __name__ == "__main__":      # python task_move_composition_support.py DIRECTORY: serve for browser-driver iteration
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    served = build_move_composed(directory)
    write_fixture(served, directory / "fixture.json")
    print(json.dumps({"base": served.origin, "fixture": str(directory / "fixture.json")}), flush=True)
    try:
        sys.stdin.read()
    finally:
        served.close()
