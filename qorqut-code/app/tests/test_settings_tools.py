"""The owner's Settings page as tools: the coordinator reads and changes it through the functions behind the owner's routes."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain

from qorqut.composition.policy import resolve_operation_policy
from qorqut.dashboard_preferences import display, testing_mode
from qorqut.domains.settings_tools import SETTINGS, SHAPES, SettingsTools, check_value
from qorqut.service.changes import GLOBAL_ONLY_KINDS, _coarse_kinds_for_operation

from private_thread_runtime import build_runtime, invoke


class Keyed:
    """A keyed extension as ``qorqut_openrouter`` and ``qorqut_treg`` declare one."""
    def __init__(self, identity):
        self.id, self.saved, self.refuse = identity, None, False

    def status(self):
        return "connected" if self.saved else "not_set_up"

    def change_key(self, change):
        if change["key"] is not None and self.refuse:
            raise DomainError("openrouter_key_refused", "OpenRouter did not accept this key")
        self.saved = change["key"]
        return {"status": self.status()}


class Switch:
    def __init__(self):
        self.calls = []

    def view(self, principal_id):
        return {"mode": "managed", "recommended": "managed", "terminals": {}, "busy": [], "switching": None, "last": None}

    def start(self, principal_id, to, force=False, trust=False):
        self.calls.append((principal_id, to, force, trust))
        return {"accepted": True, **self.view(principal_id), "switching": {"to": to}}


class SettingsToolsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = runtime = build_runtime(Path(directory.name))
        self.addCleanup(runtime.database.close)
        runtime.registry = runtime.application.registry
        self.openrouter = Keyed("openrouter")
        runtime.selection = SimpleNamespace(extensions=(self.openrouter,))
        runtime.runtime_switch = self.switch = Switch()
        self.environment = {"OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 40}
        self.tools = SettingsTools(runtime, environment=self.environment.get).register()
        self.owner, self.lead = runtime.owner.principal_id, runtime.coordinator.principal_id

    def change(self, setting, value, ctx=None):
        return invoke(self.runtime, "settings_set", {"setting": setting, "value": value}, ctx=ctx or self.runtime.coordinator)

    def refused(self, ctx, operation, args):
        result = self.runtime.application.invoke(ctx, operation, {"scope": "global", **args}, InvocationOptions(idempotency_key=new_id()))
        return None if result.status == "ok" else plain(result.error)["code"]

    def test_only_the_coordinator_in_person_reads_or_changes_settings(self):
        change = {"setting": "testing_mode", "value": {"enabled": True}}
        # The handler refuses everyone but the coordinator in person; the owner uses the Settings page itself.
        for ctx in (self.runtime.worker, self.runtime.owner):
            self.assertEqual(self.refused(ctx, "settings_get", {}), "forbidden")
            self.assertEqual(self.refused(ctx, "settings_set", change), "forbidden")
        delegated = replace(self.runtime.coordinator, delegation_binding_id=new_id(), delegation_assignment_id=new_id(),
                            delegation_generation=1)
        with self.runtime.database.read() as uow:
            with self.assertRaises(DomainError) as refusal:
                self.tools.caller(delegated, uow)
        self.assertEqual(refusal.exception.code, "forbidden")
        self.assertEqual(testing_mode(self.runtime.paths.runtime, self.owner), {"enabled": False})

    def test_among_agents_only_the_ordubashy_role_is_given_the_tools(self):
        registry = self.runtime.application.registry
        for name in ("settings_get", "settings_set"):
            decide = lambda role, overrides=None: resolve_operation_policy(      # noqa: E731
                name, {}, overrides or {}, registry, role_id=role, scope="global")
            self.assertEqual((decide("ordubashy").mode, decide("ordubashy", {name: "deny"}).mode), ("allow", "deny"))
            for role in ("global-worker", "learning-applier", None):
                self.assertEqual((decide(role).mode, decide(role).provenance), ("deny", "owner_settings_ordubashy_only"), role)
                self.assertEqual(decide(role, {name: "allow"}).mode, "deny", role)

    def test_an_extension_is_connected_with_the_key_a_settings_variable_holds(self):
        done = self.change("extension_key", {"extension": "openrouter", "environment_key": "OPENROUTER_API_KEY"})
        self.assertEqual(done, {"setting": "extension_key", "result": {"status": "connected"}})
        self.assertEqual(self.openrouter.saved, self.environment["OPENROUTER_API_KEY"])
        # The key is in no argument and no result; a receipt replay returns the same answer without it.
        self.assertNotIn("sk-or-", str(done))
        self.assertEqual(self.change("extension_key", {"extension": "openrouter", "remove": True})["result"], {"status": "not_set_up"})
        self.assertIsNone(self.openrouter.saved)

    def test_a_missing_variable_a_refused_key_and_an_absent_extension_change_nothing(self):
        args = lambda value: {"setting": "extension_key", "value": value}   # noqa: E731
        lead = self.runtime.coordinator
        self.assertEqual(self.refused(lead, "settings_set", args({"extension": "openrouter", "environment_key": "NO_SUCH"})), "not_found")
        self.openrouter.refuse = True
        self.assertEqual(self.refused(lead, "settings_set", args({"extension": "openrouter", "environment_key": "OPENROUTER_API_KEY"})),
                         "openrouter_key_refused")
        self.assertIsNone(self.openrouter.saved)
        # treg is a keyed extension, but it is not selected in this installation.
        self.assertEqual(self.refused(lead, "settings_set", args({"extension": "treg", "environment_key": "OPENROUTER_API_KEY"})),
                         "operation_unavailable")
        self.assertEqual(self.refused(lead, "settings_set", args({"extension": "whatsapp", "environment_key": "X"})), "validation_error")
        # A key itself is never an accepted value.
        self.assertEqual(self.refused(lead, "settings_set", args({"extension": "openrouter", "key": "sk-or-v1-abc"})), "validation_error")

    def test_testing_mode_is_written_to_the_owners_file(self):
        self.assertEqual(self.change("testing_mode", {"enabled": True})["result"], {"enabled": True})
        self.assertEqual(testing_mode(self.runtime.paths.runtime, self.owner), {"enabled": True})
        self.assertEqual(testing_mode(self.runtime.paths.runtime, self.lead), {"enabled": False})

    def test_appearance_and_alerts_are_stored_as_requests_to_the_owners_browsers(self):
        shown = self.change("appearance", {"theme": "light"})["result"]
        self.assertEqual(shown["appearance"], {"theme": "light"})
        self.change("notifications", {"replies": True, "routines": False})
        self.change("appearance", {"text_size": "large"})
        saved = display(self.runtime.paths.runtime, self.owner)
        self.assertEqual({name: item["value"] for name, item in saved["fields"].items()},
                         {"theme": "light", "notify_replies": True, "notify_routines": False, "text_size": "large"})
        # Every changed field has its own, later revision, so a browser applies only what it has not seen.
        self.assertEqual(saved["fields"]["theme"]["revision"], 1)
        self.assertEqual(saved["fields"]["text_size"]["revision"], saved["revision"])
        self.assertEqual(len({item["revision"] for item in saved["fields"].values()}), 4)
        read = invoke(self.runtime, "settings_get", {"sections": ["display"]}, ctx=self.runtime.coordinator)
        self.assertEqual(read["display"]["appearance"], {"theme": "light", "text_size": "large"})
        self.assertEqual(read["display"]["notifications"], {"replies": True, "routines": False})
        self.assertEqual(display(self.runtime.paths.runtime, self.lead), {"fields": {}, "revision": 0})

    def test_the_switch_of_how_agents_run_is_started_as_the_owner(self):
        done = self.change("agent_run_mode", {"mode": "terminal", "force": True})
        self.assertEqual(self.switch.calls, [(self.owner, "terminal", True, False)])
        self.assertEqual((done["result"]["accepted"], done["result"]["switching"]), (True, {"to": "terminal"}))

    def test_agent_options_clients_trust_browser_and_account_call_the_owners_functions(self):
        self.runtime.native_chat = chat = object()
        with patch("qorqut.native_chat.launch_options.change", return_value={"model": "opus"}) as options:
            self.assertEqual(self.change("agent_options", {"agent_id": "a1", "model": "opus", "effort": None})["result"], {"model": "opus"})
        options.assert_called_once_with(chat, self.runtime, self.owner, "a1", {"model": "opus", "effort": None})
        with patch("qorqut.owner_settings.agent_clients_action", return_value={"claude": {}}) as clients:
            self.change("agent_client", {"provider": "codex", "action": "connect"})
        clients.assert_called_once_with("codex", "connect")
        with patch("qorqut.agent_trust.owner_view", return_value={"granted": True}) as trust:
            with patch("qorqut.managed_runtime.mode.is_terminal", return_value=False):
                self.assertEqual(self.refused(self.runtime.coordinator, "settings_set",
                                              {"setting": "agent_folder_trust", "value": {"granted": True}}), "terminal_mode_off")
            trust.assert_not_called()
            with patch("qorqut.managed_runtime.mode.is_terminal", return_value=True):
                self.change("agent_folder_trust", {"granted": True})
        trust.assert_called_once_with(self.runtime, {"granted": True})
        with patch("qorqut.browser.sign_in.sign_out", return_value={"sites": []}) as sign_out:
            self.change("website_sign_out", {"site": "example.com"})
        sign_out.assert_called_once_with(self.runtime, "example.com")
        with patch("qorqut.browser.setup.ensure_all", return_value={"state": "healthy", "ready": True, "agents": [1]}) as retry:
            self.assertEqual(self.change("agent_browser", {"action": "retry"})["result"]["state"], "healthy")
        retry.assert_called_once_with(self.runtime)
        directory = self.runtime.paths.runtime
        with patch("qorqut.account.start", return_value={"sent": True}) as start, \
                patch("qorqut.account.verify", return_value={"signed_in": True}) as verify, \
                patch("qorqut.account.logout", return_value={"signed_in": False}) as logout:
            self.change("account", {"action": "start", "email": "owner@example.com"})
            self.change("account", {"action": "verify", "email": "owner@example.com", "code": "123456", "use": "personal"})
            self.change("account", {"action": "sign_out"})
        start.assert_called_once_with(directory, {"email": "owner@example.com"})
        verify.assert_called_once_with(directory, {"email": "owner@example.com", "code": "123456", "use": "personal"})
        logout.assert_called_once_with(directory)

    def test_a_part_that_cannot_be_read_does_not_hide_the_rest(self):
        with patch("qorqut.connectors.view", return_value={"connectors": [{"id": "openrouter", "status": "not_set_up"}]}), \
                patch("qorqut.owner_settings.agent_clients_view", side_effect=DomainError("operation_unavailable", "No clients here")), \
                patch("qorqut.owner_settings.about_view", return_value={"version": "1.6.0"}), \
                patch("qorqut.account.owner_view", return_value={"signed_in": True}):
            read = invoke(self.runtime, "settings_get", ctx=self.runtime.coordinator)
        self.assertEqual(read["connections"]["extensions"], [{"id": "openrouter", "status": "not_set_up"}])
        self.assertEqual(read["agents"]["run_mode"]["mode"], "managed")
        self.assertEqual(read["agents"]["clients"], {"unavailable": "No clients here"})
        self.assertEqual(read["agents"]["testing_mode"], False)
        self.assertIn("unavailable", read["agents"]["agents"])        # no Chat service in this fixture
        self.assertEqual((read["about"]["version"], read["about"]["account"]), ("1.6.0", {"signed_in": True}))
        self.assertIn("environment_set", read["environment"])

    def test_values_are_checked_before_anything_runs(self):
        self.assertEqual(set(SHAPES), set(SETTINGS))
        for setting, value in (("testing_mode", {"enabled": "yes"}), ("testing_mode", {}), ("appearance", {}),
                               ("appearance", {"theme": "neon"}), ("notifications", {"replies": 1}), ("agent_options", {"agent_id": "a1"}),
                               ("agent_run_mode", {"mode": "tmux"}), ("account", {"action": "verify", "email": "a@b.co"}),
                               ("agent_client", {"provider": "claude", "action": "delete"}), ("website_sign_out", {"site": ""})):
            with self.assertRaises(DomainError, msg=(setting, value)) as refusal:
                check_value(setting, value)
            self.assertEqual(refusal.exception.code, "validation_error")
        self.assertEqual(self.refused(self.runtime.coordinator, "settings_set", {"setting": "testing_mode", "value": {"on": True}}),
                         "validation_error")
        self.assertEqual(self.switch.calls, [])

    def test_a_change_tells_the_dashboard_to_reread_the_owners_settings(self):
        self.assertEqual(_coarse_kinds_for_operation("settings_set"), {"owner_settings"})
        self.assertIn("owner_settings", GLOBAL_ONLY_KINDS)
        self.assertEqual(_coarse_kinds_for_operation("settings_get"), set())


class DisplayPreferenceTests(unittest.TestCase):
    def test_only_known_fields_and_values_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            for change in ({}, {"theme": "neon"}, {"notify_replies": "yes"}, {"zoom": 2}, {"theme": True}, None.__class__):
                with self.assertRaises(DomainError):
                    display(directory, "owner", change)
            self.assertEqual(display(directory, "owner"), {"fields": {}, "revision": 0})
            self.assertEqual(display(directory, "owner", {"theme": "system"})["revision"], 1)
            self.assertEqual(display(directory, "owner", {"theme": "dark", "notify_signoffs": True})["fields"],
                             {"theme": {"value": "dark", "revision": 3}, "notify_signoffs": {"value": True, "revision": 2}})


if __name__ == "__main__":
    unittest.main()
