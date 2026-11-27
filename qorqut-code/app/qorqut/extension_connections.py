"""What the Connections card of a contract extension reads and sends: the one place for its shapes.

The card is drawn by Qorqbot from the manifest and the status (EXTENSION-CONTRACT.md section 10, rule 13). Today's
hand-written cards (WhatsApp, treg, OpenRouter) keep their own entries in ``qorqut.connectors``; a contract extension adds
one entry of ``kind`` ``contract`` after them:

    {"id": "keyed", "kind": "contract", "name": "Keyed", "summary": "...", "version": "1.0.0",
     "delivery": "downloadable", "status": "not_set_up", "note": null, "can_set_up": true,
     "setup": {"kind": "key", "fields": [{"id": "token", "label": "Token", "secret": true}]},
     "agents": [{"agent_id": "...", "name": "Ordubashy", "role": "ordubashy", "on": true, "in_effect": false}]}

``setup`` is None for a folder that is refused before its manifest could be read. ``can_set_up`` is false when set-up
cannot work (``extension_setup.can_set_up``: the folder or its code was refused, or it cannot run here); the card then
shows the note with no set-up step and no switches. ``name`` of an agent is the name the dashboard shows for it. ``agents`` are the agents that exist
whose role the manifest allows; ``on`` is the switch somebody made, or with none whether the role is in
``agents.on_after_setup``; ``in_effect`` is ``on`` while the extension is ``ready``. Nothing in an entry is a value the
owner typed, an image or a private path.

The card sends one action to ``POST /api/root/connections/<id>`` (``parse_action`` checks it) and is answered with the
entry as it is then:

    {"action": "start"}
    {"action": "accept", "values": {"token": "..."}}
    {"action": "cancel"}
    {"action": "disconnect"}
    {"action": "switch", "agent_id": "...", "on": true}

``GET /api/root/connections/<id>`` answers the entry; ``GET /api/root/connections/<id>/image`` the pairing image.
"""

from __future__ import annotations

from collections.abc import Mapping

from qorqut_operations.contracts import DomainError

from qorqut.agents.roles import profile_role
from qorqut.extension_setup import can_set_up
from qorqut.extensions import SetupStart
from qorqut.native_chat.store import profile_label

KIND = "contract"
ACTIONS = ("start", "accept", "cancel", "disconnect", "switch")
_AGENTS = "SELECT agent_id, name, profile_json FROM agent_profiles WHERE status='active' ORDER BY name, agent_id"


def agents(connection) -> list[dict]:
    """The agents that exist (as the owner's agents list shows them), each with its shown name and role."""
    return [{"agent_id": row["agent_id"], "name": profile_label(row), "role": profile_role(row)}
            for row in connection.execute(_AGENTS)]


def item(entry, existing) -> dict:
    """The card entry of one ``InstalledExtension``; ``existing`` is ``agents(connection)`` of the same read."""
    manifest = entry.manifest
    setup = None
    rows = []
    if manifest is not None:
        setup = {"kind": manifest.setup_kind,
                 "fields": [{"id": field.id, "label": field.label, "secret": field.secret}
                            for field in manifest.setup_fields]}
        for agent in existing:
            if agent["role"] in manifest.agents_allowed:
                on = agent["agent_id"] in entry.agents
                rows.append({**agent, "on": on, "in_effect": on and entry.status == "ready"})
    return {"id": entry.id, "kind": KIND, "name": manifest.name if manifest else entry.id,
            "summary": manifest.summary if manifest else "", "version": entry.version, "delivery": entry.delivery,
            "status": entry.status, "note": entry.note, "can_set_up": can_set_up(entry), "setup": setup, "agents": rows}


def items(registry, connection) -> list[dict]:
    """The card entries of every found extension, from one read."""
    existing = agents(connection)
    return [item(entry, existing) for entry in registry.list(connection=connection)]


def parse_action(body) -> dict:
    """The card's action, checked for its shape only. Refusals never quote what was sent."""
    if not isinstance(body, Mapping) or body.get("action") not in ACTIONS:
        raise DomainError("validation_error", "The action is one of " + ", ".join(ACTIONS) + ".")
    action = body["action"]
    allowed = {"accept": {"action", "values"}, "switch": {"action", "agent_id", "on"}}.get(action, {"action"})
    if set(body) != allowed:
        raise DomainError("validation_error", f"The action {action} takes only: " + ", ".join(sorted(allowed)) + ".")
    if action == "accept":
        values = body["values"]
        if not isinstance(values, Mapping) or not all(isinstance(key, str) and isinstance(value, str)
                                                      for key, value in values.items()):
            raise DomainError("validation_error", "The values are text, each under the id of its field.")
        return {"action": action, "values": dict(values)}
    if action == "switch":
        if not isinstance(body["agent_id"], str) or not body["agent_id"] or type(body["on"]) is not bool:
            raise DomainError("validation_error", "A switch names the agent and is true or false.")
        return {"action": action, "agent_id": body["agent_id"], "on": body["on"]}
    return {"action": action}


def current(runtime, ext_id) -> dict:
    """The card entry of one extension as the registry holds it now, from one read."""
    with runtime.database.read() as uow:
        return item(runtime.extension_registry.get(ext_id, connection=uow.connection), agents(uow.connection))


def act(runtime, context, ext_id, request):
    """One action of the owner's card (``request`` from ``parse_action``), as the owner ``context``. Answers the entry as
    it is then, or the refused ``OperationResult`` of a switch. Typed values go to the set-up handler and nowhere else."""
    from qorqut_operations.contracts import InvocationOptions, new_id
    current(runtime, ext_id)                    # an unknown id is refused before anything is asked
    action = request["action"]
    if action == "switch":
        result = runtime.application.invoke(context, "extensions_switch", {
            "scope": "global", "extension": ext_id, "agent_id": request["agent_id"], "on": request["on"]},
            InvocationOptions(idempotency_key=new_id()))
        if result.status != "ok":
            return result
    else:
        setups = runtime.extension_setups
        if setups is None:
            raise DomainError("operation_unavailable", "Extensions cannot be set up in this composition.")
        if action == "start":
            setups.start(ext_id, SetupStart("owner"))
        elif action == "accept":
            setups.accept(ext_id, request["values"])
        else:
            getattr(setups, action)(ext_id)
    return current(runtime, ext_id)

