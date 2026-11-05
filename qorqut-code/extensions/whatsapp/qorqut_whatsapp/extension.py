"""Optional WhatsApp selection; no provider access or service starts during composition."""
from pathlib import Path

from bazarlyk.extensions import Assets, Extension, NativeConnection, ServiceDeclaration
from bazarlyk.agentic.text import render
from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.application.plans import schema
from qorqut_operations.background.programs import Program
from qorqut_operations.contracts import DomainError, PolicyDecision

from . import binding
from .operations import WhatsAppSendOperations, OPERATION, PROGRAM
from .program import WhatsAppSendProgram


def policy(name, defaults, overrides, registry, *, role_id, scope, resolve):
    granted = overrides.get(name, defaults.get(name, "deny")) != "deny"
    return PolicyDecision("signoff" if role_id == "ordubashy" and scope == "global" and granted else "deny",
                          "whatsapp_owner_approval")


def register_program(programs, operations):
    program = WhatsAppSendProgram(operations)
    programs.register(Program(PROGRAM, 1, schema({
        "scope": {"const": "global"}, "account_ref": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "chat_id": {"type": "string", "minLength": 3, "maxLength": 160},
        "text": {"type": "string", "minLength": 1, "maxLength": 16384}},
        ("scope", "account_ref", "chat_id", "text")), program.run, {}, (), (), scope_kinds=("global",),
        external=True, recover=program.recover, timeout_seconds=120, routine_allowed=False, background_queue="whatsapp"))
    return program


def launch(paths, agent_id):
    return binding.launch(paths, {"agent_id": agent_id, "role": {"id": "ordubashy"}, "scope": {"kind": "ecosystem"}})


def status(paths, agent_id):
    return launch(paths, agent_id)[0] or {"prepared": False, "reason": "not_bound"}


def entry(paths, workspace, agent_id):
    ready, client = launch(paths, agent_id)
    if not ready or not ready["prepared"] or client is None:
        raise DomainError("operation_unavailable", "WhatsApp reader is not prepared for this agent")
    return {"type": "stdio", **client, "required": False}


def guidance(*, agentic_view):
    return "\n" + render(agentic_view, "whatsapp", "qorqut_whatsapp/guidance.md") + "\n"


class WhatsApp(Extension):
    id = "whatsapp"

    def __init__(self):
        self.operations = self.receiver = None

    def catalog(self):
        return (CatalogContribution(self.id, entries=(CatalogEntry(OPERATION, True, "compound", frozenset({"mcp"})),)),)

    def policy_rules(self):
        return {OPERATION: policy}

    def global_discovery_operations(self):
        return (OPERATION,)

    def private_signoffs(self):
        return {OPERATION: self.visible}

    def visible(self, context, uow):
        return bool(self.operations is not None and context.principal_kind == "agent"
                    and context.delegation_assignment_id is None
                    and self.operations.bound_agent(uow) == context.principal_id)

    def native_connections(self):
        return (NativeConnection(binding.NAME, binding.NAME, status, entry, lambda *_: {},
            lambda prepared, ordubashy, **_: "" if prepared else "\nThe optional WhatsApp reader is unavailable for this agent.\n",
            guidance, roles=("ordubashy",), tools=tuple(binding.TOOLS), require_new_binding=True),)

    def assets(self):
        return Assets(Path(__file__).parent / "assets", scripts=("whatsapp.js",), styles=("whatsapp.css",))

    def install(self, runtime):
        self.operations = WhatsAppSendOperations(runtime.application, runtime.paths, runtime.programs).register(runtime.registry)
        register_program(runtime.programs, self.operations)
        from .receiver import Receiver
        self.receiver = Receiver(runtime.paths)

    def services(self):
        if self.receiver is None:
            return ()
        return (ServiceDeclaration("whatsapp_receiver", self.receiver, start_before="background", stop_before="background",
            running=lambda receiver: receiver.process is not None and receiver.process.poll() is None),)


def create(config=None):
    if config is not None and (not isinstance(config, dict) or config):
        raise DomainError("invalid_installation", "WhatsApp extension takes an empty configuration; its private binding is configured separately")
    return WhatsApp()
