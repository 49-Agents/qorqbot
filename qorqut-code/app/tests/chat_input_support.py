"""Synthetic fixtures for ``request_user_input`` and the browser targets Chat can reference.

A real assembled host over temporary data with the registered Ordubashy as the configured coordinator (``Native``), real Tasks
and input Signoffs created through their operations, and a temporary ``browser-surfaces.json`` that only names invented
``https://*.example.test`` viewers. The operator desktop configs are copies of the repository's own examples with invented CDP
ports in a temporary directory. No browser, model, provider, service or network is started and no operational data is read."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

from host_foundation_support import owner_global_context
from native_chat_records import claude_result, claude_results, codex_send_call, text_content
from native_chat_support import Native

from bazarlyk.transport import mcp_presentation
from qorqut_operations.contracts import InvocationOptions, new_id

TOOL = "request_user_input"
EXAMPLES = Path(__file__).resolve().parents[1] / "tools/browser/desktops"
# The invented CDP ports the temporary operator configs name; none is a real port of any installation.
CDP_PORTS = {"ordubashy": 19225, "outlook": 19226, "second": 19230}
VIEWERS = {"ordubashy-browser": "https://desktop.example.test/ordubashy", "outlook-browser": "https://desktop.example.test/outlook",
           "second-browser": "https://desktop.example.test/second"}
# The synthetic third desktop is not one of the shipped examples: its config is the Outlook example under another session name,
# and a test that selects it adds ``SECOND_TARGET`` to the managed-desktop mapping.
SECOND_TARGET = ("second-browser", "second")
FORM = {"title": "Pick one", "explanation": "Answer.", "kind": "question", "selection_mode": "none", "response_policy": "owner_only"}


def ordubashy(tmp_path, provider="claude"):
    """A native Chat conversation whose registered agent is the configured, active Ordubashy."""
    native = Native(tmp_path, provider, role="ordubashy")
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (native.agent,))
    if provider == "codex":
        native.transport, native.thread_owner = "codex_queue", "owner-1"
    native.publish()
    if provider == "codex":
        native.write(native.codex_session_meta())
    return native


def context(native, principal, business=None):
    with native.runtime.database.read() as uow:
        return native.runtime.policy.context_for(principal, business_id=business, review_business_id=None, uow=uow)


def invoke(native, ctx, name, args):
    return native.runtime.application.invoke(ctx, name, args, InvocationOptions(idempotency_key=new_id()))


def ok(result):
    assert result.status == "ok", result.to_dict()
    return result.data


def add_business(native, slug):
    """One more active business with the owner as its member, as ``build_instance`` registers one."""
    identity, paths = new_id(), native.runtime.paths
    with native.runtime.database.transaction() as uow:
        uow.repository("businesses").insert({"id": identity, "slug": slug, "name": slug.title(), "workspace": str(paths.business_root(slug)),
                                              "hq_path": str(paths.business_hq(slug))})
        uow.repository("business_memberships", business_id=identity).insert({"principal_id": native.owner, "is_owner": True})
    return identity


def task(native, title="A synthetic task", business=None):
    ctx = context(native, native.owner, business)
    args = {"title": title} if business else {"title": title, "scope": "global"}
    return ok(invoke(native, ctx, "tasks_create", args))["task"]["id"]


def revise(native, task_id, business, **changes):
    """The owner edits a Task through ``tasks_update`` (title, ...) or completes it with ``done=True``."""
    ctx = context(native, native.owner, business)
    revision = ok(invoke(native, ctx, "tasks_get", {"task_id": task_id}))["task"]["revision"]
    options = InvocationOptions(idempotency_key=new_id(), expected_revision=revision)
    if changes.pop("done", False):
        return ok(native.runtime.application.invoke(ctx, "tasks_complete", {"task_id": task_id}, options))
    return ok(native.runtime.application.invoke(ctx, "tasks_update", {"task_id": task_id, "changes": changes}, options))


def signoff(native, business, key=None):
    ctx = context(native, native.agent, business)
    return ok(invoke(native, ctx, "signoffs_input_create", {"request_key": key or new_id(), "form": FORM}))["signoff"]["id"]


def edit_agent(native, agent_id, **changes):
    """The owner edits one registered agent (``display_name``, ``presentation``, ...) through the real ``agents_update``."""
    with native.runtime.database.read() as uow:
        revision = uow.connection.execute("SELECT revision FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()["revision"]
    options = InvocationOptions(idempotency_key=new_id(), expected_revision=revision)
    return ok(native.runtime.application.invoke(owner_global_context(native.instance), "agents_update",
                                                {"agent_id": agent_id, "changes": changes}, options))


def write_surfaces(native, *, business=None, agents=None, extra=None, desktops=("ordubashy", "outlook")):
    """The operator's ``browser-surfaces.json`` (private, schema 2): the managed desktops' browsers, optionally one business
    browser, and ``agents`` (agent id -> own browser ids)."""
    surfaces = {f"{name}-browser": {"label": f"{name.title()} browser", "kind": "browser", "viewer_url": VIEWERS[f"{name}-browser"],
                                    **({"width": 1440, "height": 900} if name == "ordubashy" else {})} for name in desktops}
    businesses = {}
    if business:
        surfaces["launch-browser"] = {"label": "Launch browser", "kind": "browser", "viewer_url": "https://desktop.example.test/launch"}
        businesses[business] = {"browser": "launch-browser"}
    surfaces.update(extra or {})
    document = {"schema_version": 2, "surfaces": surfaces,
                "agents": {identity: {"browsers": browsers} for identity, browsers in (agents or {}).items()}, "businesses": businesses}
    path = native.runtime.paths.runtime / "browser-surfaces.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    os.chmod(path, 0o600)
    return document


def write_desktops(directory, *, names=("ordubashy", "outlook")):
    """Operator-owned desktop session configs: the repository's examples with the invented CDP ports above."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        value = json.loads((EXAMPLES / f"{'outlook' if name == 'second' else name}.json.example").read_text())
        value["name"] = name
        value["chrome"]["cdp_port"] = CDP_PORTS[name]
        path = directory / f"{name}.json"
        path.write_text(json.dumps(value))
        os.chmod(path, 0o600)
    return directory


def wire(result):
    """The exact text the MCP transport returns for a result: presentation, then ``finalize`` and ``serialize``."""
    presented = mcp_presentation.present(TOOL, result.to_dict(), "compact", arguments={})
    return mcp_presentation.serialize(mcp_presentation.finalize(presented))


def present(native, message, references=(), *, ctx=None):
    """Run the real tool as the registered Ordubashy and return its result."""
    return invoke(native, ctx or context(native, native.agent), TOOL, {"message": message, "references": list(references)})


def result_record(native, text, *, uuid_=None, tool=TOOL, **options):
    """One provider tool-result record that holds ``text`` as the tool's result, in the running provider's own shape."""
    if native.provider == "claude":
        return claude_results(native.session_id, claude_result(text_content(text), **options), uuid_=uuid_ or new_id(),
                              cwd=str(native.workspace))
    return codex_send_call(text, thread=native.session_id, tool=tool, item_id="call_" + new_id(), **options)


def shown(native, **options):
    return native.chat.state(native.owner, native.agent, **options)["entries"]


__all__ = ["CDP_PORTS", "EXAMPLES", "SimpleNamespace", "TOOL", "VIEWERS", "add_business", "context", "edit_agent", "invoke", "ok", "ordubashy",
           "present", "result_record", "revise", "shown", "signoff", "task", "wire", "write_desktops", "write_surfaces"]
