"""What the Connections card of a contract extension reads and sends: the one place for its shapes.

The card is drawn by Qorqbot from the manifest and the status (EXTENSION-CONTRACT.md section 10, rule 13). Today's
hand-written cards (WhatsApp, treg, OpenRouter) keep their own entries in ``qorqut.connectors``; a contract extension adds
one entry of ``kind`` ``contract`` after them:

    {"id": "keyed", "kind": "contract", "name": "Keyed", "summary": "...", "version": "1.0.0",
     "delivery": "downloadable", "status": "not_set_up", "note": null, "can_set_up": true,
     "setup": {"kind": "key", "fields": [{"id": "token", "label": "Token", "secret": true}]},
     "agents": [{"agent_id": "...", "name": "Ordubashy", "role": "ordubashy", "on": true, "in_effect": false}],
     "offers": [{"action": "update", "version": "1.0.1"}, {"action": "remove"}], "result": null, "restarting": false}

``setup`` is None for a folder that is refused before its manifest could be read. ``can_set_up`` is false when set-up
cannot work (``extension_setup.can_set_up``: the folder or its code was refused, or it cannot run here); the card then
shows the note with no set-up step and no switches. ``name`` of an agent is the name the dashboard shows for it. ``agents`` are the agents that exist
whose role the manifest allows; ``on`` is the switch somebody made, or with none whether the role is in
``agents.on_after_setup``; ``in_effect`` is ``on`` while the extension is ``ready``. Nothing in an entry is a value the
owner typed, an image or a private path. ``offers`` are the downloads the owner can make himself now
(``ExtensionTools.offers``): ``install`` for an index entry that is not installed, ``update`` when the index has a newer
version that fits, ``remove`` for a downloaded one; none while a change waits for the restart. ``result`` is the sentence
of the last change of this extension in this process: that Qorqbot restarts to apply it, then, after the restart, what
happened. ``restarting`` is true while a change of this extension waits for the restart.

The card sends one action to ``POST /api/root/connections/<id>`` (``parse_action`` checks it) and is answered with the
entry as it is then:

    {"action": "start"}
    {"action": "accept", "values": {"token": "..."}}
    {"action": "cancel"}
    {"action": "disconnect"}
    {"action": "switch", "agent_id": "...", "on": true}
    {"action": "install", "version": "1.0.0"}       the version the card showed; "update" the same
    {"action": "remove", "delete_data": false}

The owner's click on install, update or remove is his approval: no Signoff is made, and the change is staged at once.

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
ACTIONS = ("start", "accept", "cancel", "disconnect", "switch", "install", "update", "remove")
_AGENTS = "SELECT agent_id, name, profile_json FROM agent_profiles WHERE status='active' ORDER BY name, agent_id"


def agents(connection) -> list[dict]:
    """The agents that exist (as the owner's agents list shows them), each with its shown name and role."""
    return [{"agent_id": row["agent_id"], "name": profile_label(row), "role": profile_role(row)}
            for row in connection.execute(_AGENTS)]


def item(entry, existing, downloads=None) -> dict:
    """The card entry of one ``InstalledExtension``; ``existing`` is ``agents(connection)`` of the same read and
    ``downloads`` the ``offers``, ``result``, ``failed`` and ``restarting`` of the entry."""
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
    downloads = downloads or {"offers": [], "result": None, "failed": False, "restarting": False}
    return {"id": entry.id, "kind": KIND, "name": entry.name or entry.id,
            "summary": entry.summary or "", "version": entry.version, "delivery": entry.delivery,
            "status": entry.status, "note": entry.note, "can_set_up": can_set_up(entry), "setup": setup, "agents": rows,
            **downloads}


def downloads(runtime, ext_id, connection, index=None):
    """The ``offers``, ``result``, ``failed`` and ``restarting`` of one extension; none in a composition without the download tools."""
    tools = getattr(runtime, "extension_tools", None)
    if tools is None:
        return None
    from qorqut.bootstrap.extension_change import pending
    from qorqut.domains.extension_tools import RESTARTING
    waiting = pending(tools.paths)
    return {"offers": tools.offers(ext_id, connection, index), "result": tools.results.get(ext_id),
            # Whether that result says the change failed, before the restart or at the start that applied it.
            "failed": tools.failures.get(ext_id, False),
            # Not while Qorqbot could not be asked to restart: the result then says it is applied at the next start.
            "restarting": waiting is not None and waiting["id"] == ext_id
                          and tools.results.get(ext_id, RESTARTING) == RESTARTING}


def items(registry, connection, *, runtime=None) -> list[dict]:
    """The card entries of every found extension and every index entry not installed, from one read; with ``runtime``
    each with its downloads."""
    existing = agents(connection)
    tools = getattr(runtime, "extension_tools", None)
    index = tools.index() if tools is not None else None
    return [item(entry, existing, downloads(runtime, entry.id, connection, index))
            for entry in registry.list(connection=connection)]


def parse_action(body) -> dict:
    """The card's action, checked for its shape only. Refusals never quote what was sent."""
    if not isinstance(body, Mapping) or body.get("action") not in ACTIONS:
        raise DomainError("validation_error", "The action is one of " + ", ".join(ACTIONS) + ".")
    action = body["action"]
    allowed = {"accept": {"action", "values"}, "switch": {"action", "agent_id", "on"}, "install": {"action", "version"},
               "update": {"action", "version"}, "remove": {"action", "delete_data"}}.get(action, {"action"})
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
    if action in ("install", "update"):
        if not isinstance(body["version"], str) or not body["version"]:
            raise DomainError("validation_error", "An install or update names the version the card showed.")
        return {"action": action, "version": body["version"]}
    if action == "remove":
        if type(body["delete_data"]) is not bool:
            raise DomainError("validation_error", "A removal says whether to delete its private data, true or false.")
        return {"action": action, "delete_data": body["delete_data"]}
    return {"action": action}


def _entry(registry, ext_id, connection):
    """The installed extension ``ext_id``, or its index entry when it is not installed."""
    try:
        return registry.get(ext_id, connection=connection)
    except DomainError:
        offered = next((entry for entry in registry.offered if entry.id == ext_id), None)
        if offered is None:
            raise
        return offered


def current(runtime, ext_id) -> dict:
    """The card entry of one extension as the registry holds it now, from one read."""
    with runtime.database.read() as uow:
        return item(_entry(runtime.extension_registry, ext_id, uow.connection), agents(uow.connection),
                    downloads(runtime, ext_id, uow.connection))


def act(runtime, context, ext_id, request):
    """One action of the owner's card (``request`` from ``parse_action``), as the owner ``context``. Answers the entry as
    it is then, or the refused ``OperationResult`` of a switch. Typed values go to the set-up handler and nowhere else."""
    from qorqut_operations.contracts import InvocationOptions, new_id
    current(runtime, ext_id)                    # an unknown id is refused before anything is asked
    action = request["action"]
    if action in ("install", "update", "remove"):
        tools = getattr(runtime, "extension_tools", None)
        if tools is None:
            raise DomainError("operation_unavailable", "Extensions cannot be downloaded in this composition.")
        try:
            tools.owner_change(context, ext_id, action, version=request.get("version"),
                               delete_data=request.get("delete_data", False))
        except DomainError as problem:
            # A change refused or failed before the restart is an ordinary answer: the card shows its sentence.
            return {**current(runtime, ext_id), "result": problem.message, "failed": True}
    elif action == "switch":
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

