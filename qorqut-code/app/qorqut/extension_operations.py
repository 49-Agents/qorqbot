"""The owner's two operations on installed extensions: the list, and switching one on or off for an agent.

Both are the owner's own (``owner_only``: the engine refuses anyone else), reached on the owner's route only and given
to no agent. A switch is written in the operation's
own unit (``ExtensionRegistry.switch``); after it commits, the open session of the agent is restarted when what the agent
has in effect changed (``native_chat/extension_restart.py``).
"""

from __future__ import annotations

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.contracts import plain

from qorqut.native_chat.extension_restart import SESSION

COMMON = {"scope_kinds": frozenset({"global"}), "global_capability": "read_global_context", "owner_only": True,
          "exposure": frozenset({"dashboard"}), "supports_signoff": False, "resolve_namespace": lambda ctx, args: "global"}
SCOPE = {"scope": {"const": "global"}}
LIST_DESCRIPTION = ("List the extensions found on this computer: for each, its id, name, summary, delivery, version, status "
                    "with its note, and the agents it is on for.")
SWITCH_DESCRIPTION = ("Switch one extension on or off for one agent. Switching on is refused for an agent whose role the "
                      "extension does not allow.")


class ExtensionOperations:
    def __init__(self, extensions, restarts=None):
        self.extensions, self.restarts = extensions, restarts

    def register(self, registry):
        registry.register(OperationDescriptor("extensions_list", schema(SCOPE), description=LIST_DESCRIPTION,
                                              effect_kind="read", read=self.list, **COMMON))
        registry.register(OperationDescriptor(
            "extensions_switch",
            schema({**SCOPE, "extension": {"type": "string", "minLength": 1, "maxLength": 32},
                    "agent_id": {"type": "string", "minLength": 1, "maxLength": 200}, "on": {"type": "boolean"}},
                   ["extension", "agent_id", "on"]),
            description=SWITCH_DESCRIPTION, effect_kind="records", prepare=self.prepare, apply=self.apply, **COMMON))
        return self

    def list(self, ctx, args, options, uow):
        return {"extensions": [{"id": item.id, "name": item.manifest.name if item.manifest else None,
                                "summary": item.manifest.summary if item.manifest else None, "delivery": item.delivery,
                                "version": item.version, "status": item.status, "note": item.note,
                                "agents": list(item.agents)}
                               for item in self.extensions.list(connection=uow.connection)]}

    def prepare(self, ctx, args, options, uow):
        # Refused here, before a plan exists, as it would be when applied.
        self.extensions.check_switch(args["extension"], args["agent_id"], args["on"], connection=uow.connection)
        title = f"Switch the extension {args['extension']} {'on' if args['on'] else 'off'}"
        return PlanBuilder(ctx, "extensions_switch", args, title, namespace="global",
                           affected_scope={"kind": "global"}).build()

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        before = self.extensions.is_on(args["extension"], args["agent_id"], connection=uow.connection)
        in_effect = self.extensions.switch(args["extension"], args["agent_id"], args["on"], uow=uow)
        # ``session`` says in one sentence what happens to the agent's session once this commits.
        session = (SESSION[self.restarts.expected(args["agent_id"], in_effect != before)]
                   if self.restarts is not None else None)
        return {"extension": args["extension"], "agent_id": args["agent_id"], "on": args["on"], "in_effect": in_effect,
                "session": session}


def register(registry, extensions, restarts=None):
    return ExtensionOperations(extensions, restarts).register(registry)
