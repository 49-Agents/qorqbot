"""Per-agent launch options and the client table (``managed_runtime/options.py``, ``clients.py``), what the manager puts into a launch
because of them, and the owner-only routes ``GET/POST /api/root/agents/<id>/chat/options`` (``native_chat/launch_options.py``).

Nothing here starts a host or a CLI. The routes run through the real ``HTTPAPI`` dispatch and the real ``native_chat.http.route``
with a stand-in for the Chat facade (as ``test_managed_http.py`` does); without the service extras they are skipped.
"""
import isolated_cli_environment  # before any Qorqbot import

from contextlib import contextmanager
import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    SERVICE = True
except ImportError:  # the service extras are not installed
    SERVICE = False

from qorqut.managed_runtime import clients, options
from qorqut.managed_runtime import manager as hosts

import test_managed_manager as base

AGENT = base.AGENT
OTHER = base.OTHER
OWNER = "owner-principal"


def profile(runtime):
    return {"agent_id": AGENT, "profile_json": json.dumps({"runtime": runtime})}


class OptionsFile(base.Fixture):
    def test_no_file_is_no_options(self):
        self.assertEqual(options.read(self.paths, AGENT), options.Options())
        self.assertEqual(options.read(None, AGENT), options.Options())
        self.assertEqual(options.read(self.paths, "../escape"), options.Options())
        self.assertFalse((self.paths.runtime / "managed-agents").exists(), "reading creates nothing")

    def test_a_write_is_private_atomic_and_read_back(self):
        written = options.write(self.paths, AGENT, client="codex", model="claude-haiku-4-5", effort="low")
        self.assertEqual(written, options.Options("codex", "claude-haiku-4-5", "low"))
        path = self.paths.runtime / "managed-agents" / AGENT / "options.json"
        self.assertEqual(path, options.options_file(self.paths, AGENT))
        self.assertEqual(json.loads(path.read_text()),
                         {"schema_version": 1, "client": "codex", "model": "claude-haiku-4-5", "effort": "low"})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(options.read(self.paths, AGENT), written)
        self.assertEqual(options.read(self.paths, OTHER), options.Options(), "options are per agent")
        self.assertEqual([item.name for item in path.parent.iterdir()], ["options.json"], "no temporary file is left")

    def test_a_change_touches_only_the_named_fields_and_none_clears(self):
        options.write(self.paths, AGENT, client="claude", model="opus", effort="high")
        self.assertEqual(options.write(self.paths, AGENT, model="sonnet"), options.Options("claude", "sonnet", "high"))
        self.assertEqual(options.write(self.paths, AGENT, effort=None), options.Options("claude", "sonnet", None))
        self.assertEqual(options.write(self.paths, AGENT, model="  "), options.Options("claude", None, None), "blank clears")
        self.assertEqual(options.write(self.paths, AGENT), options.Options("claude", None, None))

    def test_the_host_state_files_are_left_alone(self):
        folder = self.paths.runtime / "managed-agents" / AGENT
        folder.mkdir(parents=True, mode=0o700)
        (folder / "host.json").write_text("{}")
        options.write(self.paths, AGENT, model="opus")
        self.assertEqual((folder / "host.json").read_text(), "{}")

    def test_validation(self):
        for changes in ({"client": "gemini"}, {"client": 3}, {"effort": "extreme"}, {"effort": "LOW"}, {"model": "-x"},
                        {"model": "a b"}, {"model": "a;b"}, {"model": "a" * 101}, {"model": 5}, {"model": "x\n"[0] + "é"},
                        {"unknown": "1"}, {"model": "ok", "effort": "bad"}):
            with self.subTest(changes=changes):
                with self.assertRaises(options.OptionsError) as raised:
                    options.write(self.paths, AGENT, **changes)
                self.assertEqual(raised.exception.code, "validation_error")
        self.assertFalse((self.paths.runtime / "managed-agents" / AGENT / "options.json").exists(), "nothing was written")
        for model in ("a" * 100, "claude-haiku-4-5", "claude-opus-4-1-20250805", "opus[1m]", "vendor/model:tag.v1_x"):
            with self.subTest(model=model):
                self.assertEqual(options.write(self.paths, AGENT, model=model).model, model)
        for effort in options.EFFORTS:
            self.assertEqual(options.write(self.paths, AGENT, effort=effort).effort, effort)
        self.assertEqual(options.EFFORTS, ("minimal", "low", "medium", "high", "xhigh", "max"))

    def test_a_rejected_change_leaves_what_was_stored(self):
        options.write(self.paths, AGENT, model="opus")
        with self.assertRaises(options.OptionsError):
            options.write(self.paths, AGENT, model="sonnet", effort="bad")
        self.assertEqual(options.read(self.paths, AGENT).model, "opus")

    def test_a_broken_or_foreign_file_is_no_options(self):
        folder = self.paths.runtime / "managed-agents" / AGENT
        folder.mkdir(parents=True, mode=0o700)
        path = folder / "options.json"
        for text in ("not json", "[]", json.dumps({"schema_version": 2, "client": "codex"}), "x" * 5000):
            path.write_text(text)
            path.chmod(0o600)
            self.assertEqual(options.read(self.paths, AGENT), options.Options(), text[:20])
        path.write_text(json.dumps({"schema_version": 1, "client": "codex", "model": "-bad", "effort": "low"}))
        self.assertEqual(options.read(self.paths, AGENT), options.Options("codex", None, "low"), "a bad field is dropped, not the rest")
        path.chmod(0o644)
        self.assertEqual(options.read(self.paths, AGENT), options.Options(), "a file others can read is not ours")


class EffectiveClient(base.Fixture):
    def test_a_stored_client_wins(self):
        options.write(self.paths, AGENT, client="codex")
        asked = []
        self.assertEqual(options.effective_client(self.paths, AGENT, profile("claude-code"), lambda c: asked.append(c) or False), "codex")
        self.assertEqual(asked, [], "readiness is not even asked")

    def test_the_profile_runtime_decides_while_its_client_is_ready(self):
        for runtime, expected in (("claude-code", "claude"), ("codex", "codex")):
            self.assertEqual(options.effective_client(self.paths, AGENT, profile(runtime), lambda c: True), expected)

    def test_the_other_client_is_used_when_the_profiles_is_not_ready(self):
        self.assertEqual(options.effective_client(self.paths, AGENT, profile("claude-code"), lambda c: c == "codex"), "codex")
        self.assertEqual(options.effective_client(self.paths, AGENT, profile("codex"), lambda c: c == "claude"), "claude")

    def test_the_profiles_client_when_neither_is_ready(self):
        self.assertEqual(options.effective_client(self.paths, AGENT, profile("codex"), lambda c: False), "codex")
        self.assertEqual(options.effective_client(self.paths, AGENT, profile("claude-code"), lambda c: False), "claude")

    def test_a_profile_without_a_known_runtime_is_claude_and_a_failing_readiness_is_not_ready(self):
        for item in (None, {}, {"agent_id": AGENT}, {"profile_json": "garbage"}, profile("gemini")):
            self.assertEqual(options.effective_client(self.paths, AGENT, item, lambda c: True), "claude")

        def broken(client):
            raise RuntimeError("status unreadable")
        self.assertEqual(options.effective_client(self.paths, AGENT, profile("codex"), broken), "codex")

    def test_the_first_successful_open_makes_the_client_sticky(self):
        self.assertTrue(options.remember(self.paths, AGENT, "codex"))
        self.assertEqual(options.read(self.paths, AGENT).client, "codex")
        self.assertFalse(options.remember(self.paths, AGENT, "claude"), "an agent that has a client keeps it")
        self.assertEqual(options.read(self.paths, AGENT).client, "codex")
        self.assertFalse(options.remember(self.paths, OTHER, "gemini"))
        self.assertEqual(options.read(self.paths, OTHER).client, None)
        # and a CLI becoming ready later does not move it
        self.assertEqual(options.effective_client(self.paths, AGENT, profile("claude-code"), lambda c: True), "codex")

    def test_ready_function_reads_the_status_once(self):
        view = {"claude": {"installed": True, "signed_in": True}, "codex": {"installed": True, "signed_in": False}}
        with mock.patch("qorqut.owner_settings.agent_clients_view", return_value=view) as read:
            ready = options.ready_function()
            self.assertEqual((ready("claude"), ready("codex"), ready("other")), (True, False, False))
        self.assertEqual(read.call_count, 1)
        with mock.patch("qorqut.owner_settings.agent_clients_view", side_effect=RuntimeError("no")):
            self.assertEqual(options.clients_ready(), {"claude": {"ready": False}, "codex": {"ready": False}})


class ClientTable(base.Fixture):
    def test_the_entries(self):
        self.assertEqual(clients.CLIENTS, ("claude", "codex"))
        self.assertEqual((clients.driver("claude"), clients.receipt_client("claude"), clients.role_command("claude")),
                         ("qorqut.managed_runtime.claude_driver:ClaudeSession", "claude", "/role"))
        self.assertEqual((clients.driver("codex"), clients.receipt_client("codex"), clients.role_command("codex")),
                         ("qorqut.managed_runtime.codex_driver:CodexSession", "codex", "$role"))
        self.assertEqual((clients.by_provider("claude"), clients.by_provider("codex"), clients.by_provider("gemini")),
                         ("claude", "codex", None))
        self.assertEqual((clients.runtime_client("claude-code"), clients.runtime_client("codex"), clients.runtime_client(None)),
                         ("claude", "codex", None))
        with self.assertRaises(clients.UnknownClient):
            clients.entry("gemini")

    def test_both_clients_have_a_driver(self):
        self.assertEqual((clients.available("claude"), clients.available("codex"), clients.available("gemini")), (True, True, False))

    def test_prepared_allow_rules_go_on_the_command_line_for_an_untrusted_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(clients.allowed_tool_arguments("claude", folder), [], "no settings file: nothing")
            (Path(folder) / ".claude").mkdir()
            settings = Path(folder) / ".claude/settings.json"
            settings.write_text(json.dumps({"permissions": {"allow": ["mcp__qorqut__a", "Bash(touch x)", 5, "", "-bad"], "deny": ["Bash"]}}))
            self.assertEqual(clients.allowed_tool_arguments("claude", folder), ["--allowedTools", "mcp__qorqut__a", "Bash(touch x)"])
            self.assertEqual(clients.allowed_tool_arguments("codex", folder), [])
            for bad in ("{", "[]", json.dumps({"permissions": {"allow": "x"}}), json.dumps({"permissions": {"allow": []}})):
                settings.write_text(bad)
                self.assertEqual(clients.allowed_tool_arguments("claude", folder), [])

    def test_claude_gets_model_and_effort_flags_only_when_set(self):
        self.assertEqual(clients.launch_arguments("claude", None, None), [])
        self.assertEqual(clients.launch_arguments("claude", "claude-haiku-4-5", None), ["--model", "claude-haiku-4-5"])
        self.assertEqual(clients.launch_arguments("claude", None, "high"), ["--effort", "high"])
        self.assertEqual(clients.launch_arguments("claude", "opus", "xhigh"), ["--model", "opus", "--effort", "xhigh"])
        self.assertEqual(clients.launch_arguments("claude", None, "minimal"), ["--effort", "low"], "the lowest level Claude has")
        self.assertEqual(clients.launch_arguments("codex", "gpt", "high"), ["--model", "gpt", "--effort", "high", "--scoped-permissions"],
                         "Codex takes both, always together, plus its permission mode, through its driver")

    def test_permission_mode_fails_closed(self):
        workspace = self.base / "ws"
        (workspace / ".qorqut").mkdir(parents=True)
        settings = workspace / ".qorqut" / "claude-launch-settings.json"
        self.assertEqual(clients.permission_mode("claude", workspace), "deny", "no file")
        for text, expected in ((json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}), "bypass"),
                               (json.dumps({"permissions": {"defaultMode": "dontAsk"}}), "deny"),
                               (json.dumps({"permissions": {"defaultMode": "BYPASSPERMISSIONS"}}), "deny"),
                               (json.dumps({"permissions": {}}), "deny"), (json.dumps({"permissions": 1}), "deny"),
                               (json.dumps({}), "deny"), ("[]", "deny"), ("not json", "deny"), ("", "deny")):
            settings.write_text(text)
            self.assertEqual(clients.permission_mode("claude", workspace), expected, text)
        self.assertEqual(clients.permission_mode("codex", workspace), "deny", "no Codex config")
        (workspace / ".codex").mkdir()
        config = workspace / ".codex" / "config.toml"
        for text, expected in (('default_permissions = ":danger-full-access"\n', "bypass"), ('default_permissions = "workspace"\n', "deny"),
                               ("", "deny"), ("not = toml = at all", "deny")):
            config.write_text(text)
            self.assertEqual(clients.permission_mode("codex", workspace), expected, text)
        self.assertEqual(clients.permission_mode("gemini", workspace), "deny")


class ManagerLaunch(base.ManagerFixture):
    def open(self, **options_):
        if options_:
            options.write(self.paths, AGENT, **options_)
        return self.manager.ensure_open(base.PROFILE, 30)

    def spec(self):
        return self.launcher.started[0][1]

    def test_without_options_the_launch_is_what_it_always_was(self):
        self.open()
        spec = self.spec()
        self.assertEqual(spec["args"], ["--settings", str(self.workspace / ".qorqut/claude-launch-settings.json")])
        self.assertEqual((spec["client"], spec["driver"], spec["send_role"], spec["permission_mode"]),
                         ("claude", "qorqut.managed_runtime.claude_driver:ClaudeSession", "/role", "deny"))
        self.assertEqual((spec["model"], spec["effort"]), (None, None))

    def test_allow_rules_of_the_prepared_settings_reach_the_command_line(self):
        (self.workspace / ".claude").mkdir()
        (self.workspace / ".claude/settings.json").write_text(json.dumps({"permissions": {"allow": ["mcp__qorqut__a"]}}))
        self.open(model="opus")
        self.assertEqual(self.spec()["args"], ["--settings", str(self.workspace / ".qorqut/claude-launch-settings.json"),
                                               "--allowedTools", "mcp__qorqut__a", "--model", "opus"])

    def test_model_and_effort_reach_the_command_line(self):
        self.open(model="claude-haiku-4-5", effort="low")
        spec = self.spec()
        self.assertEqual(spec["args"], ["--settings", str(self.workspace / ".qorqut/claude-launch-settings.json"),
                                        "--model", "claude-haiku-4-5", "--effort", "low"])
        self.assertEqual((spec["model"], spec["effort"]), ("claude-haiku-4-5", "low"))

    def test_only_one_of_them(self):
        self.open(effort="max")
        self.assertEqual(self.spec()["args"][-2:], ["--effort", "max"])
        self.assertNotIn("--model", self.spec()["args"])

    def test_the_hosts_permission_mode_comes_from_the_prepared_settings(self):
        (self.workspace / ".qorqut" / "claude-launch-settings.json").write_text(
            json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}))
        self.open()
        self.assertEqual(self.spec()["permission_mode"], "bypass")

    def test_a_resumed_session_gets_no_role_command(self):
        self.resume = base.SESSION
        self.open()
        self.assertIs(self.spec()["send_role"], False)

    def test_the_first_successful_open_stores_the_client_used(self):
        self.assertIsNone(options.read(self.paths, AGENT).client)
        self.open()
        self.assertEqual(options.read(self.paths, AGENT).client, "claude")

    def test_an_open_that_fails_stores_nothing(self):
        self.launcher.start_error = base.LaunchError("host_did_not_start")
        with self.assertRaises(hosts.ManagedError):
            self.open()
        self.assertIsNone(options.read(self.paths, AGENT).client)

    def test_readiness_picks_the_client_of_an_agent_with_none_stored(self):
        manager = hosts.ManagedRuntime(self.runtime, launcher=self.launcher, prepare=self.manager._prepare_override, resume=lambda a: None,
                                       executable=str(self.cli), clock=lambda: self.now[0], sleep=self.advance,
                                       ready=lambda client: client == "codex")
        self.assertEqual(manager.client_for(profile("claude-code")), "codex")
        manager.ensure_open(profile("claude-code"), 30)
        self.assertEqual(self.spec()["client"], "codex")
        self.assertEqual(options.read(self.paths, AGENT).client, "codex")

    def test_with_a_driver_the_codex_entry_launches_codex_pieces(self):
        options.write(self.paths, AGENT, client="codex", model="gpt-x", effort="high")
        with mock.patch.object(hosts.ManagedRuntime, "_resume_id", return_value=None) as resume:
            self.manager.ensure_open(base.PROFILE, 30)
        spec = self.spec()
        self.assertEqual((spec["client"], spec["driver"], spec["send_role"], spec["model"], spec["effort"]),
                         ("codex", "qorqut.managed_runtime.codex_driver:CodexSession", "$role", "gpt-x", "high"))
        self.assertEqual(spec["permission_mode"], "deny")
        self.assertEqual(spec["args"], ["--model", "gpt-x", "--effort", "high", "--scoped-permissions"])
        self.assertEqual(spec["ready_seconds"], 0)
        self.assertEqual(resume.call_args.args[-1], "codex")

    def test_codex_always_gets_model_and_effort_together_from_the_prepared_config(self):
        (self.workspace / ".codex").mkdir()
        (self.workspace / ".codex/config.toml").write_text(
            'model = "gpt-5.5"\nmodel_reasoning_effort = "high"\ndefault_permissions = ":danger-full-access"\n')
        for chosen, expected in (({}, ["--model", "gpt-5.5", "--effort", "high"]),
                                 ({"model": "gpt-x"}, ["--model", "gpt-x", "--effort", "high"]),
                                 ({"effort": "low"}, ["--model", "gpt-5.5", "--effort", "low"])):
            with self.subTest(chosen=chosen):
                self.assertEqual(clients.launch_arguments("codex", chosen.get("model"), chosen.get("effort"), self.workspace),
                                 expected + ["--bypass-permissions"])
        self.assertEqual(clients.launch_arguments("codex", None, None, self.base / "nothing"), ["--scoped-permissions"],
                         "no prepared config and nothing chosen: the driver's own defaults, never half of a pair")

    def test_a_codex_agent_opens_through_the_client_table_with_the_bypass_of_its_prepared_mode(self):
        (self.workspace / ".codex").mkdir()
        (self.workspace / ".codex/config.toml").write_text('default_permissions = ":danger-full-access"\n')
        self.open(client="codex")
        spec = self.spec()
        self.assertEqual((spec["client"], spec["permission_mode"], spec["send_role"]), ("codex", "bypass", "$role"))
        self.assertIn("--bypass-permissions", spec["args"])
        self.assertNotIn("--settings", spec["args"], "the Claude settings file is a Claude flag")

    def test_the_clients_own_home_folders_reach_the_launch_environment(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/scratch/codex-home", "CLAUDE_CONFIG_DIR": "/scratch/claude-config"}):
            self.open()
        env = self.spec()["env"]
        self.assertEqual((env["CODEX_HOME"], env["CLAUDE_CONFIG_DIR"]), ("/scratch/codex-home", "/scratch/claude-config"))

    def test_every_supported_role_opens_alike(self):
        # Ordubashy, a global worker, the learning applier: the manager no longer asks the role. What differs is the prepared
        # workspace: an ecosystem scope is prepared to bypass, any other scope to deny (and the allowed tools on the command line).
        for role, mode_text, expected in (("ordubashy", "bypassPermissions", "bypass"), ("global-worker", "dontAsk", "deny"),
                                          ("learning-applier", "dontAsk", "deny")):
            with self.subTest(role=role):
                self.launcher.started.clear()
                self.launcher.inspections.clear()
                (self.workspace / ".qorqut" / "claude-launch-settings.json").write_text(
                    json.dumps({"permissions": {"defaultMode": mode_text}}))
                self.manager.ensure_open({"agent_id": AGENT, "workspace": "/w", "role": role}, 30)
                self.assertEqual(self.spec()["permission_mode"], expected)

    def test_the_host_record_names_its_client_and_identity_follows_it(self):
        self.assertEqual(hosts.identity_of(AGENT, {"client": "codex"})["provider"], "codex")
        self.assertEqual(hosts.identity_of(AGENT, {})["provider"], "claude")
        self.assertEqual(hosts.identity_of(AGENT, {"client": "gemini"})["provider"], "claude")
        found = base.verified()
        found.agent_client = "codex"
        self.assertEqual(hosts.ManagedRuntime._record(found)["client"], "codex")


class ResumeByClient(base.Fixture):
    def test_a_conversation_of_another_client_is_not_resumed(self):
        capture = {"transcript_path": __file__, "receipt_id": 1, "captured_length": 1}

        @contextmanager
        def reading():
            yield SimpleNamespace(connection=object())
        runtime = SimpleNamespace(paths=self.paths, database=SimpleNamespace(read=lambda: reading()))
        manager = hosts.ManagedRuntime(runtime, ready=lambda c: True)
        chosen = SimpleNamespace(provider="codex", conversation_id=base.SESSION)
        with mock.patch("qorqut.native_chat.selection.selected", return_value=chosen), \
                mock.patch("qorqut.native_chat.store.get_capture", return_value=capture):
            self.assertIsNone(manager._resume_id(AGENT, False, "claude"))
            chosen.provider = "claude"
            self.assertEqual(manager._resume_id(AGENT, False, "claude"), base.SESSION)
            self.assertIsNone(manager._resume_id(AGENT, False, "codex"), "a Claude conversation is not resumed by Codex")
            chosen.provider = "codex"
            self.assertEqual(manager._resume_id(AGENT, False, "codex"), base.SESSION, "a Codex thread id is resumed by Codex")


# ----- the routes -----------------------------------------------------------------------------------------------------

class FakeManaged:
    def __init__(self):
        self.open = False
        self.held = 0

    def handle(self, agent_id):
        return SimpleNamespace(state="ready") if self.open else None

    @contextmanager
    def hold(self, agent_id, timeout=5.0):
        self.held += 1
        yield


class FakeChat:
    def __init__(self, paths):
        self.managed = FakeManaged()
        self.principal = OWNER
        self.agent = AGENT
        self.owner_ok = True

        @contextmanager
        def read():
            yield SimpleNamespace()
        self.database = SimpleNamespace(read=read)
        self.paths = paths

    def _target(self, uow, principal_id, agent_id):
        if principal_id != OWNER or not self.owner_ok:
            raise DomainError("forbidden", "Agent chat is for the installation owner")
        if agent_id != self.agent:
            raise DomainError("not_found", "Select an active registered agent")
        return profile("claude-code")


@unittest.skipUnless(SERVICE, "Starlette is not installed")
class Routes(base.Fixture):
    def setUp(self):
        super().setUp()
        from qorqut.transport.http import HTTPAPI
        self.principal = OWNER
        self.chat = FakeChat(self.paths)
        self.ready = {"claude": True, "codex": False}
        patcher = mock.patch.object(options, "clients_ready", lambda environ=None, run=None: {
            name: {"ready": value} for name, value in self.ready.items()})
        patcher.start()
        self.addCleanup(patcher.stop)

        @contextmanager
        def read():
            repository = SimpleNamespace(get=lambda name: {"owner_principal_id": self.principal})
            yield SimpleNamespace(repository=lambda name, **options_: repository)

        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = SimpleNamespace(database=SimpleNamespace(read=read), require_authority=lambda: None, native_chat=self.chat,
                                      paths=self.paths)
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id=self.principal, display_name="Owner"),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options_: None)
        api._report_transport_success = lambda *args: None
        api._report_transport_failure = lambda *args: None
        api.context = lambda actor, slug=None, owner=False: (SimpleNamespace(principal_kind="human", principal_id=self.principal), None)

        async def handler(request):
            return await api(request)
        self.client = TestClient(Starlette(routes=[Route("/api/{path:path}", handler, methods=["GET", "POST"])]))
        self.addCleanup(self.client.close)

    url = f"/api/root/agents/{AGENT}/chat/options"

    def get(self, headers=None, agent=AGENT):
        return self.client.get(f"/api/root/agents/{agent}/chat/options", headers=headers or {})

    def post(self, body, headers=None, agent=AGENT):
        return self.client.post(f"/api/root/agents/{agent}/chat/options", content=body if isinstance(body, str) else json.dumps(body),
                                headers={"Content-Type": "application/json", **(headers or {})})

    def test_the_view_of_an_agent_with_nothing_set(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.json()["data"], {
            "client": None, "model": None, "effort": None, "effective_client": "claude",
            "clients": {"claude": {"ready": True}, "codex": {"ready": False}}, "session_open": False})
        self.assertFalse((self.paths.runtime / "managed-agents").exists(), "a read writes nothing")

    def test_a_change_is_stored_and_the_view_returned(self):
        response = self.post({"model": "claude-haiku-4-5", "effort": "low"})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual((data["model"], data["effort"], data["client"]), ("claude-haiku-4-5", "low", None))
        self.assertEqual(options.read(self.paths, AGENT), options.Options(None, "claude-haiku-4-5", "low"))
        self.assertEqual(self.get().json()["data"]["model"], "claude-haiku-4-5")
        cleared = self.post({"model": None}).json()["data"]
        self.assertEqual((cleared["model"], cleared["effort"]), (None, "low"))

    def test_the_effective_client_follows_readiness_until_one_is_chosen(self):
        self.ready = {"claude": False, "codex": True}
        self.assertEqual(self.get().json()["data"]["effective_client"], "codex")
        data = self.post({"client": "claude"}).json()["data"]
        self.assertEqual((data["client"], data["effective_client"]), ("claude", "claude"))

    def test_model_and_effort_may_change_with_a_session_open_and_the_client_may_not(self):
        self.chat.managed.open = True
        self.assertTrue(self.get().json()["data"]["session_open"])
        self.assertEqual(self.post({"model": "opus", "effort": "high"}).status_code, 200)
        refused = self.post({"client": "codex"})
        self.assertEqual((refused.status_code, refused.json()["error"]["code"]), (422, "session_open"))
        self.assertIsNone(options.read(self.paths, AGENT).client)
        self.assertEqual(self.post({"client": "claude"}).status_code, 200, "the client already in use is no change")
        options.write(self.paths, AGENT, client="claude")
        self.assertEqual(self.post({"client": None}).status_code, 200, "clearing what resolves to the same client")
        self.chat.managed.open = False
        self.assertEqual(self.post({"client": "codex"}).json()["data"]["client"], "codex")

    def test_a_host_that_cannot_be_verified_counts_as_open(self):
        def broken(agent_id):
            raise hosts.HostUnverified(agent_id)
        self.chat.managed.handle = broken
        self.assertTrue(self.get().json()["data"]["session_open"])
        self.assertEqual(self.post({"client": "codex"}).json()["error"]["code"], "session_open")

    def test_a_client_change_holds_off_an_open_in_progress(self):
        self.post({"model": "opus"})
        self.assertEqual(self.chat.managed.held, 0)
        self.post({"client": "claude"})
        self.assertEqual(self.chat.managed.held, 1)
        self.chat.managed.hold = mock.MagicMock(side_effect=DomainError("session_opening", "opening", retryable=True))
        response = self.post({"client": "codex"})
        self.assertEqual(response.json()["error"]["code"], "session_opening")
        self.assertEqual(options.read(self.paths, AGENT).client, "claude", "nothing changed")

    def test_invalid_bodies_and_values_are_refused_and_nothing_is_written(self):
        for body in ({"client": "gemini"}, {"effort": "extreme"}, {"model": "-x"}, {"model": "a b"}, {"model": "a" * 101}, {"model": 3},
                     {"principal_id": "someone-else"}, {"model": "opus", "extra": 1}):
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "validation_error"))
        for body in ([], "[]", "3", "not json"):          # not an object: the shared body reader refuses it
            with self.subTest(body=body):
                self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse((self.paths.runtime / "managed-agents" / AGENT / "options.json").exists())

    def test_an_authorization_header_is_refused_before_anything_runs(self):
        for call in (lambda: self.get({"Authorization": "Bearer agent-credential"}),
                     lambda: self.post({"model": "opus"}, {"Authorization": "Bearer agent-credential"})):
            response = call()
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "forbidden"))
        self.assertFalse((self.paths.runtime / "managed-agents").exists())

    def test_only_the_owner_and_only_a_registered_agent(self):
        self.chat.owner_ok = False
        for response in (self.get(), self.post({"model": "opus"})):
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "forbidden"))
        self.chat.owner_ok = True
        for response in (self.get(agent=OTHER), self.post({"model": "opus"}, agent=OTHER)):
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, "not_found"))
        response = self.client.get("/api/root/agents/not-a-uuid/chat/options")
        self.assertEqual(response.status_code, 404)
        self.assertFalse((self.paths.runtime / "managed-agents").exists())

    def test_other_verbs_are_not_routes(self):
        self.assertGreaterEqual(self.client.delete(self.url).status_code, 400)
        self.assertFalse((self.paths.runtime / "managed-agents").exists())

    def test_an_unwritable_state_folder_is_a_plain_refusal(self):
        with mock.patch.object(options, "write", side_effect=OSError("disk")):
            response = self.post({"model": "opus"})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "operation_unavailable"))


if __name__ == "__main__":
    unittest.main()
