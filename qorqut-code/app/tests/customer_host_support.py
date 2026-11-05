"""One synthetic composed host behind the real HTTP application, for the Customer/Story/Email record qualification.

The fixture builds the real runtime (assembled schema, trusted operation catalog, retained Signoff service, Focus, policy), puts the
real ``create_app`` in front of it with the dashboard compatibility routes. The owner routes treat a same-origin request as the
installation owner (there is no cookie login; the origin and CSRF check is their boundary), so a check reaches the host exactly as
the dashboard does: ``POST /api/biz/<slug>/task`` (the create form),
``/api/biz/<slug>/operations/<name>`` and ``/api/root/operations/<name>`` (the generic operation routes the record panel calls).
Agent and denial cases go through the same ``Application`` the agent transports call (an agent credential cannot reach the owner
routes). Synthetic only: a tmp instance and rows a test appends; no model, provider, network, native session or operational data."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id, plain
from starlette.testclient import TestClient

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.background.integration import attach_background
from bazarlyk.service.web import create_app
from bazarlyk.transport.auth import Authentication
from bazarlyk.transport.compatibility import DashboardCompatibility
from host_foundation_support import build_instance, create_registered_global, global_agent_without_capability, register_sources

ORIGIN = {"Origin": "http://testserver"}
PROFILE = {"company": "Acme Ltd", "contact_name": "A. Buyer", "email": "buyer@example.test", "phone": "+000",
           "website": "https://example.test", "notes": "PRIVATE-CUSTOMER-NOTES prefers morning calls"}
NOTES = PROFILE["notes"]


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first", "second"))
    first, second = ids["first"], ids["second"]
    register_sources(instance, first, "first")
    register_sources(instance, second, "second")
    agent = create_registered_global(instance, "ordubashy")
    bare = global_agent_without_capability(instance, "learning-applier")
    runtime = instance.runtime
    attach_background(runtime, runtime.programs)
    runtime.freeze_registry()
    with instance.database.transaction() as uow:
        runtime.authority.register_workers(uow, [first, second])
    authentication = Authentication(instance.database, runtime.paths)
    dashboard = tmp_path / "dashboard"
    (dashboard / "js").mkdir(parents=True)
    (dashboard / "css").mkdir()
    app = create_app(runtime, dashboard_root=dashboard, origins=["http://testserver"], authentication=authentication,
                     compatibility=DashboardCompatibility(runtime), manage_lifecycle=False)
    with TestClient(app, base_url="http://testserver") as client:
        yield SimpleNamespace(
            instance=instance, runtime=runtime, client=client, first=first, second=second, agent=agent, bare=bare,
            owner=instance.human(first), owner_second=instance.human(second),
            owner_global=InvocationContext(Principal(instance.owner, "human"), allowed_business_ids=frozenset({first, second}),
                                           global_capabilities=GLOBAL_CAPABILITIES, source="dashboard"),
            agent_context=runtime.resolver.invocation_context(Principal(agent, "agent"), business_id=first),
            bare_context=runtime.resolver.runtime_context(Principal(bare, "agent"), business_id=first))


# -- the real HTTP application ---------------------------------------------------------------------------------------------

def post(host, path, **body):
    """``POST /api/<path>`` as the signed-in owner: ``(status, json)``. Every request carries an idempotency key unless it names one."""
    body.setdefault("idempotency_key", new_id())
    response = host.client.post("/api/" + path, json=body, headers=ORIGIN)
    return response.status_code, response.json()


def get(host, path):
    response = host.client.get("/api/" + path, headers=ORIGIN)
    return response.status_code, response.json()


def operate(host, slug, name, /, **body):
    """One generic operation call exactly as the dashboard's ``workCall`` makes it: a business slug, or ``None`` for Global."""
    return post(host, ("biz/" + slug if slug else "root") + "/operations/" + name, **body)


def data(answer):
    status, body = answer
    assert status in (200, 201) and body.get("status", "ok") != "error", body
    return body["data"] if "data" in body else body


def refused(answer):
    status, body = answer
    assert status >= 400 and body.get("status") == "error", body
    return body["error"]["code"]


# -- the application layer (agents, denials, and replays that need exact options) -----------------------------------------------

def invoke(host, ctx, name, arguments=None, *, revision=None, key=None):
    return host.runtime.application.invoke(ctx, name, arguments or {}, InvocationOptions(
        idempotency_key=key or new_id(), expected_revision=revision))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def code(result):
    assert result.status == "error", result.data
    return result.error["code"]


# -- stored facts, read straight from the database ----------------------------------------------------------------------------

def rows(host, sql, *parameters):
    with host.instance.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(sql, parameters)]


def vocabulary(host, business=None):
    """The raw stored ``task_customer_states`` row of one scope (a business id, or ``None`` for Global), or ``None``."""
    found = rows(host, "SELECT config_json, revision FROM business_configs WHERE domain='task_customer_states' AND business_id IS ?",
                 business)
    return {"config": json.loads(found[0]["config_json"]), "revision": found[0]["revision"]} if found else None


def stored_attributes(host, identity):
    """The Task row's reserved ``$task`` payload as stored."""
    return json.loads(rows(host, "SELECT fields_json FROM tasks WHERE id=?", identity)[0]["fields_json"]).get("$task", {})


def snapshot(host):
    """Everything a refused Customer write must leave alone: every Task row and every configuration row."""
    return (rows(host, "SELECT * FROM tasks ORDER BY id"), rows(host, "SELECT * FROM business_configs ORDER BY business_id, domain"))


def choices(host, slug):
    """The scope's Customer state choices exactly as the dashboard's ``customerStatesMount`` reads them."""
    return data(operate(host, slug, "tasks_config_get"))["customer_states"]


# -- records the dashboard creates ---------------------------------------------------------------------------------------------

def customer(host, slug="first", **fields):
    """A Customer created as the dashboard does: the create form for a business, the generic operation for Global."""
    fields = {"title": "Acme Ltd", "task_type": "customer", "customer_kind": "company", "customer_profile": PROFILE, **fields}
    if slug:
        status, body = post(host, f"biz/{slug}/task", **fields)
        assert status == 201, body
        return body["task"]
    return data(operate(host, None, "tasks_create", **fields))["task"]


def task(host, slug="first", **fields):
    return data(operate(host, slug, "tasks_create", **{"title": "Work", **fields}))["task"]


def read(host, identity, slug="first"):
    return data(operate(host, slug, "tasks_get", task_id=identity))["task"]


def seed_email(host, business=None, conversation="conversation-test", subject="Project proposal", mailbox=None):
    """One real email conversation row (with its message) in a business or, for ``None``, in Global.

    Global has exactly one owner mailbox, so every Global conversation shares it; a business conversation gets its own mailbox
    unless ``mailbox`` names an existing one."""
    with host.instance.database.transaction() as uow:
        if business is None:
            mailbox = mailbox or "mail-owner"
        else:
            mailbox = mailbox or "mail-" + conversation
        exists = uow.connection.execute("SELECT 1 FROM email_mailboxes WHERE id=? AND business_id IS ?", (mailbox, business)).fetchone()
        if not exists:
            uow.repository("email_mailboxes", business_id=business).insert({
                "id": mailbox, "address": f"{mailbox}@example.test", **({"purpose": "owner_mail"} if business is None else {})})
        uow.repository("email_conversations", business_id=business).insert({
            "id": conversation, "mailbox": mailbox, "provider_id": "thread-" + conversation, "subject": subject})
        uow.repository("email_messages", business_id=business).insert({
            "id": new_id(), "mailbox": mailbox, "conversation_id": conversation, "provider_id": "message-" + conversation,
            "message_json": {"subject": subject}})
    return conversation
