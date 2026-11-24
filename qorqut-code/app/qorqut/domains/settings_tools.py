"""The owner's Settings page as two tools for the configured coordinator (Ordubashy): ``settings_get`` and ``settings_set``.

Each part of Settings has its own owner-only dashboard route, and each of those refuses every agent. These two tools give the
configured coordinator the same reads and the same changes, for when the owner asks him to change a setting instead of opening
Settings. Nothing is decided or stored here that the dashboard does not already decide and store: every change calls the
function behind the owner's own route with the installation owner's identity, so the same checks and the same refusals apply.

Only the configured coordinator in person may call them. No other agent, no delegated identity, and no role grant or policy
override widens that; the handler proves the caller against the installation record on every call.

A key never travels through these tools. An extension's key is named by the Global Settings (Environment) variable that holds
it and is read on the server, so the arguments, the plan and the history hold the variable's name only, and no result returns
a key. A key the owner gives in Chat is first saved with ``environment_set`` and then named here.

Theme, text size and desktop alerts live in each of the owner's browsers, not on the server. ``settings_set`` stores what the
coordinator chose (``dashboard_preferences.display``) and every open dashboard applies it once; see that function.

Environment variables keep their own tools (``environment_list``, ``environment_get``, ``environment_set``).
"""
from __future__ import annotations

import re
import threading
from dataclasses import replace

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.contracts import DomainError, plain

from qorqut.management.identity import configured_ordubashy, installation_owner

SECTIONS = ("connections", "agents", "display", "about")
NOTIFY = ("replies", "signoffs", "reminders", "routines")
SETTINGS = ("extension_key", "agent_options", "agent_client", "agent_run_mode", "agent_folder_trust", "testing_mode",
            "website_sign_out", "agent_browser", "appearance", "notifications", "account")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,199}")

GET_DESCRIPTION = (
    "Coordinator read. The owner's Settings page as it is now. sections is optional (connections, agents, display, about; "
    "all when omitted). Never returns a key, a password or a token. Changes nothing. How to use it: the qorqut-role-owner-settings skill.")
SET_DESCRIPTION = (
    "Coordinator write. Changes one of the owner's settings through the same checks as the owner's own Settings page. "
    "How to use it: the qorqut-role-owner-settings skill. setting and the fields of value:\n"
    "- extension_key: {extension: openrouter or treg, environment_key: a Global Settings variable holding the key} or "
    "{extension, remove: true}. Never a key itself.\n"
    "- agent_options: {agent_id, client?: claude or codex, model?, effort?}; null clears a field.\n"
    "- agent_client: {provider: claude or codex, action: download, cancel_download, connect or cancel_connect}.\n"
    "- agent_run_mode: {mode: managed or terminal, force?, trust?}. Ends every session, yours too; needs force true.\n"
    "- agent_folder_trust: {granted}. testing_mode: {enabled}. Both true or false.\n"
    "- website_sign_out: {site}. agent_browser: {action: retry}.\n"
    "- appearance: {theme?: dark, light or system, text_size?: small, default or large}.\n"
    "- notifications: {replies?, signoffs?, reminders?, routines?}, each true or false.\n"
    "- account: {action: start, email}, {action: verify, email, code, use: personal or work, company?} or {action: sign_out}.\n"
    "Returns setting and result, the new state of what changed.")

GET_SCHEMA = schema({"scope": {"const": "global"},
                     "sections": {"type": "array", "items": {"enum": list(SECTIONS)}, "uniqueItems": True, "minItems": 1}})
SET_SCHEMA = schema({"scope": {"const": "global"}, "setting": {"enum": list(SETTINGS)},
                     "value": {"type": "object"}}, ("setting", "value"))

_BOOL = lambda value: type(value) is bool                                       # noqa: E731
_TEXT = lambda limit: (lambda value: isinstance(value, str) and 0 < len(value) <= limit)  # noqa: E731
_CLEARABLE = lambda limit: (lambda value: value is None or (isinstance(value, str) and len(value) <= limit))  # noqa: E731
_ONE_OF = lambda *allowed: (lambda value: isinstance(value, (str, bool)) and value in allowed)  # noqa: E731

# Per setting: the shapes ``value`` may have, each ``(required fields, optional fields)`` of name -> check. Anything the
# function behind the owner's route checks further (a model's name, a site, an email) is left to that function.
SHAPES = {
    "extension_key": (({"extension": _TEXT(40), "environment_key": lambda value: isinstance(value, str) and bool(NAME.fullmatch(value))}, {}),
                      ({"extension": _TEXT(40), "remove": _ONE_OF(True)}, {})),
    "agent_options": (({"agent_id": _TEXT(200)}, {"client": _CLEARABLE(20), "model": _CLEARABLE(100), "effort": _CLEARABLE(20)}),),
    "agent_client": (({"provider": _ONE_OF("claude", "codex"),
                       "action": _ONE_OF("download", "cancel_download", "connect", "cancel_connect")}, {}),),
    "agent_run_mode": (({"mode": _ONE_OF("managed", "terminal")}, {"force": _BOOL, "trust": _BOOL}),),
    "agent_folder_trust": (({"granted": _BOOL}, {}),),
    "testing_mode": (({"enabled": _BOOL}, {}),),
    "website_sign_out": (({"site": _TEXT(300)}, {}),),
    "agent_browser": (({"action": _ONE_OF("retry")}, {}),),
    "appearance": (({}, {"theme": _ONE_OF("dark", "light", "system"), "text_size": _ONE_OF("small", "default", "large")}),),
    "notifications": (({}, {kind: _BOOL for kind in NOTIFY}),),
    "account": (({"action": _ONE_OF("start"), "email": _TEXT(320)}, {}),
                ({"action": _ONE_OF("verify"), "email": _TEXT(320), "code": _TEXT(20), "use": _ONE_OF("personal", "work")},
                 {"company": _TEXT(200), "marketing_ok": _BOOL}),
                ({"action": _ONE_OF("sign_out")}, {})),
}
AT_LEAST_ONE = frozenset({"agent_options", "appearance", "notifications"})


def check_value(setting: str, value) -> dict:
    """The value of one setting, checked against that setting's shapes. Refuses with the fields a shape allows."""
    if isinstance(value, dict):
        for required, optional in SHAPES[setting]:
            if set(required) <= set(value) <= set(required) | set(optional) and all(
                    check(value[name]) for name, check in {**required, **optional}.items() if name in value):
                if setting in AT_LEAST_ONE and not set(value) & set(optional):
                    break
                return dict(value)
    shapes = "; or ".join(", ".join([*required, *(name + "?" for name in optional)]) for required, optional in SHAPES[setting])
    raise DomainError("validation_error", f"The value of {setting} is not valid. It takes: {shapes}. "
                                          "settings_set's description gives the allowed values.")


class SettingsTools:
    def __init__(self, runtime, *, environment=None):
        self.runtime, self.app = runtime, runtime.application
        self.environment = environment or self._global_environment
        self.outcomes, self.guard = {}, threading.Lock()

    def register(self):
        common = dict(scope_kinds=frozenset({"global"}), global_capability="read_global_context", supports_signoff=False,
                      exposure=frozenset({"mcp"}), resolve_namespace=lambda ctx, args: "global")
        self.runtime.registry.register(OperationDescriptor(
            "settings_get", GET_SCHEMA, description=GET_DESCRIPTION, effect_kind="read", read=self.read, **common))
        self.runtime.registry.register(OperationDescriptor(
            "settings_set", SET_SCHEMA, description=SET_DESCRIPTION, effect_kind="records", prepare=self.prepare,
            external=self.external, apply=self.apply, **common))
        self.runtime.settings_tools = self
        return self

    # ----- who may call ------------------------------------------------------------------------------------------------

    def caller(self, ctx, uow) -> str:
        """The installation owner's principal id, after proving the caller is the configured coordinator in person, at Global.
        A delegated identity acts for its parent on one assignment and never touches the owner's settings."""
        ctx = self.app.current_context(replace(ctx, project_id=None, review_project_id=None), uow)
        owner, coordinator = installation_owner(uow), configured_ordubashy(uow)
        if (ctx.delegation_assignment_id is not None or not owner or not coordinator or ctx.principal_kind != "agent"
                or ctx.principal_id != coordinator):
            raise DomainError("forbidden", "The owner's settings are for the owner and Ordubashy")
        return owner

    # ----- reading -----------------------------------------------------------------------------------------------------

    def read(self, ctx, args, options, uow):
        owner = self.caller(ctx, uow)
        readers = {"connections": self._connections, "agents": self._agents, "display": self._display, "about": self._about}
        result = {name: readers[name](owner) for name in args.get("sections") or SECTIONS}
        result["environment"] = "Read with environment_list and environment_get; change with environment_set."
        return plain(result)

    @staticmethod
    def _try(reader):
        """One part of a section. A part that cannot be read says so instead of failing the whole read."""
        try:
            return reader()
        except DomainError as problem:
            return {"unavailable": str(problem)}
        except Exception:  # noqa: BLE001 - one unreadable card never hides the others
            return {"unavailable": "This could not be read right now."}

    def _part(self, name, message):
        part = getattr(self.runtime, name, None)
        if part is None:
            raise DomainError("operation_unavailable", message)
        return part

    def _directory(self):
        return self.runtime.paths.runtime

    def _connections(self, owner):
        def extensions():
            from qorqut import connectors
            return connectors.view(self.runtime)["connectors"]

        def browser():
            from qorqut.browser import setup
            view = setup.view(self.runtime)
            return {name: view.get(name) for name in ("state", "ready", "explanation", "diagnostic")}

        def websites():
            from qorqut.browser import sign_in
            return sign_in.accounts(self.runtime)

        return {"extensions": self._try(extensions), "agent_browser": self._try(browser), "websites": self._try(websites)}

    def _roster(self):
        from qorqut.agents.global_scope import active_global
        with self.runtime.database.read() as uow:
            rows = uow.connection.execute(
                "SELECT agent_id,name FROM agent_profiles WHERE status='active' ORDER BY name").fetchall()
            return [(row["agent_id"], row["name"]) for row in rows if active_global(uow.connection, row["agent_id"]) is not None]

    def _agents(self, owner):
        def run_mode():
            view = self._part("runtime_switch", "Switching how agents run is not available").view(owner)
            return {name: view.get(name) for name in ("mode", "recommended", "busy", "switching", "last")}

        def clients():
            from qorqut import owner_settings
            return owner_settings.agent_clients_view()

        def trust():
            from qorqut import agent_trust
            return agent_trust.consent(self._directory())["granted"]

        def testing():
            from qorqut.dashboard_preferences import testing_mode
            return testing_mode(self._directory(), owner)["enabled"]

        def options():
            from qorqut.managed_runtime import options as stored
            from qorqut.native_chat import launch_options
            chat, ready = self._part("native_chat", "Agent options are not available"), stored.ready_function()
            rows = []
            for agent_id, name in self._roster():
                view = self._try(lambda: launch_options.view(chat, self.runtime, owner, agent_id, ready=ready))
                rows.append({"agent_id": agent_id, "name": name, **{key: view.get(key) for key in (
                    "client", "model", "effort", "effective_client", "session_open", "unavailable") if key in view}})
            return rows

        return {"run_mode": self._try(run_mode), "clients": self._try(clients), "folder_trust": self._try(trust),
                "testing_mode": self._try(testing), "agents": self._try(options)}

    def _display(self, owner):
        def chosen():
            from qorqut.dashboard_preferences import display
            fields = display(self._directory(), owner)["fields"]
            return {"appearance": {name: fields[name]["value"] for name in ("theme", "text_size") if name in fields},
                    "notifications": {kind: fields["notify_" + kind]["value"] for kind in NOTIFY if "notify_" + kind in fields},
                    "note": "Only what you set is listed. Each of the owner's browsers keeps its own choice after applying yours."}
        return self._try(chosen)

    def _about(self, owner):
        def about():
            from qorqut import owner_settings
            return owner_settings.about_view(runtime_directory=self._directory())

        def account():
            from qorqut import account as service
            return service.owner_view(self._directory())

        return {**_mapping(self._try(about)), "account": self._try(account)}

    # ----- changing ----------------------------------------------------------------------------------------------------

    def prepare(self, ctx, args, options, uow):
        self.caller(ctx, uow)
        value = check_value(args["setting"], args["value"])
        if args["setting"] == "extension_key":
            from qorqut import connectors
            if value["extension"] not in connectors.KEYED:
                raise DomainError("validation_error", "A key can be set for: " + ", ".join(sorted(connectors.KEYED)))
        return PlanBuilder(ctx, "settings_set", args, "Change the owner's setting " + args["setting"]).build()

    def external(self, ctx, plan):
        """The change itself, outside any transaction: the functions behind the owner's routes take their own locks and
        transactions, and some ask a provider. ``apply`` then only hands the outcome back and records the receipt."""
        with self.runtime.database.read() as uow:
            owner = self.caller(ctx, uow)
        args = plain(plan.arguments)
        outcome = getattr(self, "_set_" + args["setting"])(owner, check_value(args["setting"], args["value"]))
        with self.guard:
            self.outcomes[plan.payload_hash] = outcome
            while len(self.outcomes) > 64:      # an outcome whose apply never ran (a failed write) is not kept forever
                self.outcomes.pop(next(iter(self.outcomes)))

    def apply(self, ctx, plan, uow):
        self.caller(ctx, uow)
        with self.guard:
            if plan.payload_hash not in self.outcomes:
                raise DomainError("operation_failed", "The setting was not changed. Try again.", retryable=True)
            outcome = self.outcomes.pop(plan.payload_hash)
        return plain({"setting": plan.arguments["setting"], "result": outcome})

    def _global_environment(self, name):
        """The value of one ordinary Global Settings variable, or None. Runtime identity bindings are never read."""
        from qorqut.service.local import IDENTITY_KEYS, LocalFiles
        if name in IDENTITY_KEYS:
            return None
        try:
            pairs = LocalFiles(self.runtime).environment_pairs(None)
        except (OSError, UnicodeError):
            raise DomainError("configuration_unavailable", "Settings could not be read") from None
        return next((pair["value"] for pair in pairs if pair["key"] == name), None)

    def _set_extension_key(self, owner, value):
        selected = getattr(getattr(self.runtime, "selection", None), "extensions", ())
        extension = next((item for item in selected if item.id == value["extension"]), None)
        if extension is None or not callable(getattr(extension, "change_key", None)):
            raise DomainError("operation_unavailable", f"{value['extension']} is not part of this installation")
        if value.get("remove"):
            return extension.change_key({"key": None})
        key = self.environment(value["environment_key"])
        if not key:
            raise DomainError("not_found", f"Global Settings has no value named {value['environment_key']}. Save the key there "
                                           "first with environment_set, or ask the owner which variable holds it.")
        return extension.change_key({"key": key})

    def _set_agent_options(self, owner, value):
        from qorqut.native_chat import launch_options
        chat = self._part("native_chat", "Agent options are not available")
        body = {name: value[name] for name in ("client", "model", "effort") if name in value}
        return launch_options.change(chat, self.runtime, owner, value["agent_id"], body)

    def _set_agent_client(self, owner, value):
        from qorqut import owner_settings
        return owner_settings.agent_clients_action(value["provider"], value["action"])

    def _set_agent_run_mode(self, owner, value):
        switch = self._part("runtime_switch", "Switching how agents run is not available")
        view = switch.start(owner, value["mode"], force=bool(value.get("force")), trust=bool(value.get("trust")))
        return {name: view.get(name) for name in ("accepted", "changed", "mode", "busy", "switching", "last") if name in view}

    def _set_agent_folder_trust(self, owner, value):
        from qorqut import agent_trust
        from qorqut.managed_runtime import mode
        if not mode.is_terminal(self.runtime.paths):     # as the owner's route: folder trust is for agents in terminals
            raise DomainError("terminal_mode_off", "Agents run as managed sessions on this installation, so folder trust "
                                                   "is not needed.")
        return agent_trust.owner_view(self.runtime, {"granted": value["granted"]})

    def _set_testing_mode(self, owner, value):
        from qorqut.dashboard_preferences import testing_mode
        return testing_mode(self._directory(), owner, {"enabled": value["enabled"]})

    def _set_website_sign_out(self, owner, value):
        from qorqut.browser import sign_in
        return sign_in.sign_out(self.runtime, value["site"])

    def _set_agent_browser(self, owner, value):
        from qorqut.browser import setup
        view = setup.ensure_all(self.runtime)
        return {name: view.get(name) for name in ("state", "ready", "explanation", "diagnostic")}

    def _set_appearance(self, owner, value):
        from qorqut.dashboard_preferences import display
        display(self._directory(), owner, dict(value))
        return self._display(owner)

    def _set_notifications(self, owner, value):
        from qorqut.dashboard_preferences import display
        display(self._directory(), owner, {"notify_" + kind: on for kind, on in value.items()})
        return self._display(owner)

    def _set_account(self, owner, value):
        from qorqut import account as service
        body = {name: item for name, item in value.items() if name != "action"}
        if value["action"] == "start":
            return service.start(self._directory(), body)
        if value["action"] == "verify":
            return service.verify(self._directory(), body)
        return service.logout(self._directory())


def _mapping(value):
    return value if isinstance(value, dict) else {"about": value}
