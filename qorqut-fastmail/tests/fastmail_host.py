"""A synthetic composed host with the Fastmail provider selected and the fake engine behind it (no mailbox, Project mail or table)."""

from __future__ import annotations

from types import SimpleNamespace

from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id, plain

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from bazarlyk.background.integration import attach_background
from bazarlyk.extensions import select
from email_guidance_support import owner
from fake_engine import TOKEN, FakeEngine
from host_foundation_support import (WORKER_ROLE, build_instance, grant_capabilities, publish_role, register_sources,
                                     revoke_capabilities, select_coordinator, synthetic_ordubashy)
from qorqut_fastmail.extension import create

CAPABILITIES = ["read_global_context", "manage_fastmail"]


def selection(tmp_path, engine, **config):
    token = tmp_path / "engine.token"
    token.write_text(TOKEN + "\n")
    token.chmod(0o600)
    extension = create({"engine_url": "http://127.0.0.1:8787", "admin_token_file": str(token), **config},
                       client_factory=engine.client_factory)
    return extension, select(instances=[extension])


def make_agent(instance, role, name, capabilities=CAPABILITIES):
    created = instance.invoke(owner(instance), "agents_create", {
        "role": publish_role(instance, role), "plugins": [], "name": name, "display_name": name.title(),
        "assignment_brief": "Synthetic fixture agent.",
        "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": list(capabilities)}})
    assert created.status == "ok", created.error
    return created.data["agent_id"]


def build(tmp_path, engine=None, *, with_background=True, **config):
    engine = engine or FakeEngine()
    extension, chosen = selection(tmp_path, engine, **config)
    instance, ids = build_instance(tmp_path, extensions=chosen)
    register_sources(instance, instance.business, "first")
    synthetic_ordubashy(instance)
    runtime = instance.runtime
    ordubashy = make_agent(instance, "ordubashy", "ordubashy")
    select_coordinator(instance, ordubashy)
    worker = make_agent(instance, WORKER_ROLE, "scribe", CAPABILITIES)
    plain_worker = make_agent(instance, WORKER_ROLE, "bystander", ["read_global_context"])
    if with_background:
        attach_background(runtime, runtime.programs)
    runtime.freeze_registry()
    with instance.database.transaction() as uow:
        runtime.authority.register_workers(uow, [instance.business])
    return SimpleNamespace(instance=instance, runtime=runtime, engine=engine, extension=extension, ordubashy=ordubashy,
                           worker=worker, plain_worker=plain_worker, owner_context=owner(instance),
                           ids=ids, tmp=tmp_path)


def agent_context(host, agent_id):
    return host.runtime.resolver.invocation_context(Principal(agent_id, "agent"))


def call(host, ctx, name, arguments, key=None):
    return host.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=key or new_id()))


def send_arguments(**changes):
    return {"account_id": "acct-test", "identity_id": "id-1", "from_address": "owner@example.test", "to": ["friend@example.test"],
            "subject": "Hello", "body": "Synthetic body", **changes}


def job_id(result):
    assert result.status == "ok", result.to_dict()
    return result.data["job_id"]


def detail(host, identity):
    return plain(host.runtime.signoffs.get(host.owner_context, identity).data['signoff'])


def approve(host, proposal):
    return host.runtime.signoffs.review(host.owner_context, proposal['id'], decision='approve',
        proposal_revision=proposal['proposal_revision'], payload_hash=proposal['payload_hash'],
        expected_status_revision=proposal['status_revision'], idempotency_key=new_id())


def approved_job(host, result):
    """Execution tests explicitly approve sending before exercising the existing engine/recovery path."""
    if result.status == 'ok':
        return job_id(result)  # organizing/drafting retains its previous behavior
    assert result.status == 'signoff_required', result.to_dict()
    proposal = detail(host, result.signoff['id'])
    if proposal['state'] == 'pending':
        accepted = approve(host, proposal)
        assert accepted.status == 'accepted', accepted.to_dict()
        host.runtime.signoffs.run_one(proposal['id'])
        proposal = detail(host, proposal['id'])
    assert proposal['state'] == 'applied', proposal
    return proposal['apply_result']['data']['job_id']
