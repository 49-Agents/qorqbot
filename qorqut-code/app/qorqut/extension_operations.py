"""The three tools on installed extensions: the list, the set-up, and switching one on or off for an agent.

They are the owner's and Ordubashy's. The owner reaches the list and the switch on his own route; Ordubashy has all three
as tools, and the Learning Applier may read the list. The policy gives them to no other role, and each handler proves the
caller in person on every call: the installation owner, the configured Ordubashy, or for the list the active Learning
Applier, never a delegated identity. While no extension is found on the installation they are listed for no agent and
callable by none (``qorqut.agents.resolver.contract_tools_in_effect``), so every agent's tools are as they were.

A set-up value never passes through these tools: ``extensions_setup`` starts a set-up and says what the owner does next on
his Connections card, where he types the values himself. A switch is written in the operation's own unit
(``ExtensionRegistry.switch``); after it commits, the open session of the agent is restarted when what the agent has in
effect changed (``native_chat/extension_restart.py``).
"""

from __future__ import annotations

from dataclasses import replace
import threading

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.contracts import DomainError, plain

from qorqut.extensions import SetupStart
from qorqut.native_chat.extension_restart import SESSION

TOOLS = frozenset({"extensions_list", "extensions_setup", "extensions_switch"})
COMMON = {"scope_kinds": frozenset({"global"}), "global_capability": "read_global_context", "supports_signoff": False,
          "resolve_namespace": lambda ctx, args: "global"}
SCOPE = {"scope": {"const": "global"}}
EXTENSION = {"type": "string", "minLength": 1, "maxLength": 32}
SETUP_ACTIONS = ("status", "start", "cancel")
LIST_DESCRIPTION = ("List the extensions found on this computer. For each one it gives its id, name, summary, delivery, "
                    "version, status with its note, and the agents it is on for.")
SETUP_DESCRIPTION = (
    "Read or start the set-up of one extension. action is status, start or cancel. start begins the set-up, and the "
    "result says what the owner does next. The owner types any key or signs in himself in Settings, under Connections. "
    "Never ask the owner for a key in Chat and never pass one to this tool. cancel stops a set-up that has not finished.")
SWITCH_DESCRIPTION = ("Switch one extension on or off for one agent. Switching on is refused for an agent whose role the "
                      "extension does not allow. The result says what happens to that agent's open session.")
NEXT = {
    "setting_up": "Ask the owner to open Settings, then Connections, and finish setting up {name} on its card.",
    "ready": "{name} is set up.",
    "needs_attention": "{name} needs attention. Ask the owner to open Settings, then Connections, and look at its card.",
    "not_set_up": "{name} is not set up.",
}


def entry_view(item) -> dict:
    """What the tools show of one extension. Never a set-up value."""
    return {"id": item.id, "name": item.manifest.name if item.manifest else None,
            "summary": item.manifest.summary if item.manifest else None, "delivery": item.delivery,
            "version": item.version, "status": item.status, "note": item.note, "agents": list(item.agents)}


class ExtensionOperations:
    def __init__(self, extensions, restarts=None, *, runtime=None):
        # ``runtime`` gives the application and ``extension_setups``, read when a call needs them.
        self.extensions, self.restarts, self.runtime = extensions, restarts, runtime
        self.outcomes, self.guard = {}, threading.Lock()

    def register(self, registry):
        registry.register(OperationDescriptor("extensions_list", schema(SCOPE), description=LIST_DESCRIPTION,
                                              effect_kind="read", read=self.list,
                                              exposure=frozenset({"dashboard", "mcp"}), **COMMON))
        registry.register(OperationDescriptor(
            "extensions_setup", schema({**SCOPE, "extension": EXTENSION, "action": {"enum": list(SETUP_ACTIONS)}},
                                       ["extension", "action"]),
            description=SETUP_DESCRIPTION, effect_kind="records", prepare=self.prepare_setup, external=self.external_setup,
            apply=self.apply_setup, exposure=frozenset({"mcp"}), **COMMON))
        registry.register(OperationDescriptor(
            "extensions_switch",
            schema({**SCOPE, "extension": EXTENSION, "agent_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    "on": {"type": "boolean"}}, ["extension", "agent_id", "on"]),
            description=SWITCH_DESCRIPTION, effect_kind="records", prepare=self.prepare, apply=self.apply,
            exposure=frozenset({"dashboard", "mcp"}), **COMMON))
        return self

    # ----- who may call ------------------------------------------------------------------------------------------------

    def caller(self, ctx, uow, *, reading=False) -> str:
        """``owner`` or ``ordubashy``, after proving the caller in person at Global; for the list also ``applier``."""
        from qorqut.agents.global_scope import active_role_holder
        from qorqut.agents.roles import LEARNING_APPLIER_ROLE
        from qorqut.management.identity import configured_ordubashy, installation_owner
        app = getattr(self.runtime, "application", None)
        if app is not None:
            ctx = app.current_context(replace(ctx, project_id=None, review_project_id=None), uow)
        if ctx.delegation_assignment_id is None:
            if ctx.principal_kind == "human" and ctx.principal_id == installation_owner(uow):
                return "owner"
            if ctx.principal_kind == "agent" and ctx.principal_id == configured_ordubashy(uow):
                return "ordubashy"
            if (reading and ctx.principal_kind == "agent"
                    and ctx.principal_id == active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)):
                return "applier"
        raise DomainError("forbidden", "Extensions are managed by the owner and Ordubashy.")

    # ----- the list ----------------------------------------------------------------------------------------------------

    def list(self, ctx, args, options, uow):
        self.caller(ctx, uow, reading=True)
        return {"extensions": [entry_view(item) for item in self.extensions.list(connection=uow.connection)]}

    # ----- the set-up --------------------------------------------------------------------------------------------------

    def prepare_setup(self, ctx, args, options, uow):
        if self.caller(ctx, uow) != "ordubashy":
            raise DomainError("forbidden", "The owner sets up an extension on its Connections card.")
        self.extensions.get(args["extension"], connection=uow.connection)
        return PlanBuilder(ctx, "extensions_setup", args, f"Set-up of the extension {args['extension']}: {args['action']}",
                           namespace="global", affected_scope={"kind": "global"}).build()

    def external_setup(self, ctx, plan):
        """The set-up step itself, outside any transaction: ``ExtensionSetups`` writes the status in its own."""
        setups = getattr(self.runtime, "extension_setups", None)
        if setups is None:
            raise DomainError("operation_unavailable", "Extensions cannot be set up in this composition.")
        args = plain(plan.arguments)
        ext_id, action = args["extension"], args["action"]
        if action == "start":
            entry = setups.start(ext_id, SetupStart("ordubashy", agent_id=ctx.principal_id))
        elif action == "cancel":
            entry = setups.cancel(ext_id)
        else:
            entry = setups.status(ext_id)
        with self.guard:
            self.outcomes[plan.payload_hash] = entry
            while len(self.outcomes) > 64:      # an outcome whose apply never ran is not kept forever
                self.outcomes.pop(next(iter(self.outcomes)))

    def apply_setup(self, ctx, plan, uow):
        with self.guard:
            entry = self.outcomes.pop(plan.payload_hash, None)
        if entry is None:
            raise DomainError("operation_failed", "The set-up was not changed. Try again.", retryable=True)
        name = entry.manifest.name if entry.manifest else entry.id
        sentence = NEXT.get(entry.status)
        return {"extension": entry_view(entry), "next": sentence.format(name=name) if sentence else entry.note}

    # ----- the switch --------------------------------------------------------------------------------------------------

    def prepare(self, ctx, args, options, uow):
        self.caller(ctx, uow)
        # Refused here, before a plan exists, as it would be when applied.
        self.extensions.check_switch(args["extension"], args["agent_id"], args["on"], connection=uow.connection)
        title = f"Switch the extension {args['extension']} {'on' if args['on'] else 'off'}"
        return PlanBuilder(ctx, "extensions_switch", args, title, namespace="global",
                           affected_scope={"kind": "global"}).build()

    def apply(self, ctx, plan, uow):
        self.caller(ctx, uow)
        args = plain(plan.arguments)
        before = self.extensions.is_on(args["extension"], args["agent_id"], connection=uow.connection)
        in_effect = self.extensions.switch(args["extension"], args["agent_id"], args["on"], uow=uow)
        # ``session`` says in one sentence what happens to the agent's session once this commits.
        session = (SESSION[self.restarts.expected(args["agent_id"], in_effect != before)]
                   if self.restarts is not None else None)
        return {"extension": args["extension"], "agent_id": args["agent_id"], "on": args["on"], "in_effect": in_effect,
                "session": session}


def register(registry, extensions, restarts=None, *, runtime=None):
    return ExtensionOperations(extensions, restarts, runtime=runtime).register(registry)
