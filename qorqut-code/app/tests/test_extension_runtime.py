"""The installed extensions in a real runtime: a new instance seeded as ``qorqut.bootstrap.initialize`` seeds one, with
the ``network`` example (no code part) or the ``keyed`` example (a code part set up with a key) in its ``extensions/``
folder, then starts of ``build_runtime``.

The instance is made from an export of this checkout's last commit (``tools/export-unified.py``), which gives the role
and instruction sources; the code that runs is the code being tested. Without a Git checkout the test is skipped."""
import importlib.util
import json
import logging
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain

from qorqut.service.runtime import SERVICES_DO_NOT_FIT

REPOSITORY = Path(__file__).resolve().parents[3]
EXAMPLE = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "network"
KEYED = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "keyed"
STATUS_TOOL = "keyed_status"
TEST_TOKEN = "keyed-test-token"
# Appended to the example: its install also registers a tool on the real registry, past the view it is given.
STRAY = """
_install = Keyed.install


def _stray(self, runtime):
    _install(self, runtime)
    runtime._runtime.registry.register(OperationDescriptor(
        "keyed_stray", schema({"scope": {"const": "global"}}), effect_kind="read", supports_signoff=False,
        scope_kinds={"global"}, exposure=EXPOSURE, resolve_namespace=lambda ctx, args: "global",
        global_capability="read_global_context", default_mode="allow", description="Stray.",
        read=lambda ctx, args, options, uow: {"stray": True}))


Keyed.install = _stray
"""


def _commit():
    try:
        return subprocess.run(["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class RuntimeCase(unittest.TestCase):
    """A new seeded instance with ``FOLDER`` in its ``extensions/`` folder."""
    FOLDER = None

    @classmethod
    def setUpClass(cls):
        if cls.FOLDER is None:
            raise unittest.SkipTest("the base of the runtime tests")
        commit = _commit()
        tool = REPOSITORY / "qorqut-code" / "tools" / "export-unified.py"
        if commit is None or not tool.is_file():
            raise unittest.SkipTest("needs a Git checkout of the repository")
        from qorqut.agentic.installation import initialize_installation, prepare_installation
        from qorqut.bootstrap.initialize import COMPONENT_SOURCES, seed_instance
        from qorqut.extensions import NO_EXTENSIONS
        from qorqut.instance_paths import InstancePaths
        from qorqut.source_snapshot import validate_export

        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        spec = importlib.util.spec_from_file_location("export_unified", tool)
        exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(exporter)
        cls.export = root / "export"
        exporter.export(REPOSITORY, commit, cls.export)
        manifest, _ = validate_export(cls.export)
        sources = {item["name"]: item["destination"] for item in manifest["components"]}
        components = {name: (cls.export / sources[source] / sub).resolve()
                      for name, (source, sub) in COMPONENT_SOURCES.items()}
        instance = root / "instance"
        instance.mkdir(mode=0o700)
        cls.paths = InstancePaths(instance, components)
        initialize_installation(cls.paths, prepare_installation(cls.export, ()))
        cls.created = seed_instance(cls.paths, cls.export, NO_EXTENSIONS, "Owner")
        shutil.copytree(cls.FOLDER, cls.paths.installed_extensions / cls.FOLDER.name)

    def start(self, before=None):
        from qorqut.service.runtime import build_runtime
        runtime = build_runtime(self.export, paths=self.paths)
        if before is not None:
            before(runtime)
        runtime.start()
        self.addCleanup(runtime.stop)
        return runtime

    def invoke(self, runtime, operation, args):
        from qorqut.bootstrap.initialize import owner_context
        return runtime.application.invoke(owner_context(self.created["owner_principal_id"]), operation,
                                          {"scope": "global", **args}, InvocationOptions(idempotency_key=new_id()))

    def skills(self, runtime, agent_id):
        config = runtime.resolver.resolve_agent_config(Principal(agent_id, "agent"), include_preparation=False)
        return {item["name"] for item in config.to_dict()["skills"]}

    def composed(self, runtime, agent_id):
        resolver = runtime.resolver
        return resolver._compose(resolver._snapshot(Principal(agent_id, "agent"), None)).to_dict()

    def tools(self, runtime, agent_id):
        return {tool["canonical_operation"] for tool in self.composed(runtime, agent_id)["tools"] if tool["available"]}


class ExtensionRuntimeTest(RuntimeCase):
    FOLDER = EXAMPLE

    def test_install_switch_compose_and_removal(self):
        ordubashy, applier = self.created["agents"]["ordubashy"], self.created["agents"]["learning-applier"]
        import sys
        path = list(sys.path)
        runtime = self.start()
        # A folder with no code part loads nothing: the selection and the import path are as they were.
        self.assertEqual(sys.path, path)
        self.assertEqual(runtime.extension_registry.code, {})
        self.assertEqual(runtime.extension_gates, {})
        # With an extension installed, Ordubashy has the three tools on extensions and the Learning Applier the list.
        from qorqut.extension_operations import TOOLS
        self.assertEqual(self.tools(runtime, ordubashy) & TOOLS, TOOLS)
        self.assertEqual(self.tools(runtime, applier) & TOOLS, {"extensions_list"})
        self.assertIsNotNone(runtime.extension_registry)
        self.assertIs(runtime.resolver.extensions, runtime.extension_registry)
        self.assertTrue((runtime.agentic_layer.root / "plugins" / "network" / "agentic.yaml").is_file())
        subjects = [item["subject"] for item in runtime.agentic_layer.history.log(paths=["plugins/network"])["commits"]]
        self.assertEqual(subjects, ["Install extension network 1.0.0"])
        listed = self.invoke(runtime, "extensions_list", {})
        self.assertEqual(listed.status, "ok", plain(listed.error))
        [entry] = plain(listed.data)["extensions"]
        # The example names no role for after set-up, so it is on for nobody.
        self.assertEqual((entry["id"], entry["status"], entry["agents"]), ("network", "ready", []))
        self.assertNotIn("qorqut-plugin-network", self.skills(runtime, ordubashy))

        switched = self.invoke(runtime, "extensions_switch", {"extension": "network", "agent_id": ordubashy, "on": True})
        self.assertEqual(switched.status, "ok", plain(switched.error))
        self.assertEqual(plain(switched.data)["session"],
                         "It has no session open, so it gets the change when its session next opens.")
        self.assertIn("qorqut-plugin-network", self.skills(runtime, ordubashy))
        self.assertNotIn("qorqut-plugin-network", self.skills(runtime, applier))
        refused = self.invoke(runtime, "extensions_switch", {"extension": "network", "agent_id": applier, "on": True})
        self.assertEqual(plain(refused.error)["code"], "forbidden")
        runtime.stop()

        # The next start with the folder gone forgets the extension and removes its package. Ordubashy's session is
        # open (a fake one): the change found at start is held until Chat has started, then restarts it once.
        shutil.rmtree(self.paths.installed_extensions / "network")
        from qorqut.native_chat import extension_restart
        restarted, held = [], []

        def before(runtime):
            restarts = runtime.extension_restarts
            restarts._start = lambda work: (held.append(restarts._held is not None), work())
            restarts._open = lambda agent_id: agent_id == ordubashy
        fakes = (mock.patch.object(extension_restart.mode, "is_terminal", return_value=False),
                 mock.patch.object(extension_restart.store, "active_profile",
                                   side_effect=lambda connection, agent_id, paths: {"agent_id": agent_id}),
                 mock.patch.object(extension_restart, "restart_session",
                                   side_effect=lambda chat, profile, text, key, after: restarted.append(
                                       (profile["agent_id"], text)) or True))
        for fake in fakes:
            fake.start()
            self.addCleanup(fake.stop)
        later = self.start(before)
        self.assertIsNone(later.extension_restarts._held)
        self.assertEqual(held, [False])
        self.assertEqual(restarted, [(ordubashy, extension_restart.NOTICE.format(
            changes="The extension network was removed."))])
        self.assertEqual(plain(self.invoke(later, "extensions_list", {}).data), {"extensions": []})
        with later.database.read() as uow:
            self.assertEqual(uow.connection.execute("SELECT count(*) FROM extensions").fetchone()[0], 0)
        self.assertFalse((later.agentic_layer.root / "plugins" / "network").exists())
        self.assertNotIn("qorqut-plugin-network", self.skills(later, ordubashy))

        # With no extension installed, an agent's configuration is what it is with no registry at all: the three
        # tools on extensions are listed for no agent, and no agent can call them.
        from qorqut.extension_operations import TOOLS
        configs = {agent_id: self.composed(later, agent_id) for agent_id in (ordubashy, applier)}
        context = later.resolver.invocation_context(Principal(ordubashy, "agent"))
        refused = later.application.invoke(context, "extensions_list", {"scope": "global"},
                                           InvocationOptions(idempotency_key=new_id()))
        self.assertNotEqual(refused.status, "ok")
        later.resolver.extensions = later.agentic_view.extensions = None
        for agent_id, config in configs.items():
            self.assertEqual(config, self.composed(later, agent_id))
            self.assertNotIn("extensions_in_effect", config)
            self.assertFalse({tool["canonical_operation"] for tool in config["tools"]} & TOOLS)
        self.assertEqual(sys.path, path)



class KeyedRuntimeTest(RuntimeCase):
    """The ``keyed`` example end to end: its code is loaded at start and stays dormant until the owner types its token on
    the Connections card; then its tool is Ordubashy's and its service runs, until the owner disconnects it."""
    FOLDER = KEYED

    def owner_client(self, runtime):
        try:
            from starlette.applications import Starlette
            from starlette.routing import Route
            from starlette.testclient import TestClient
        except ImportError:
            self.skipTest("Starlette is not installed")
        from qorqut.transport.http import HTTPAPI
        from types import SimpleNamespace
        owner = self.created["owner_principal_id"]
        api = HTTPAPI.__new__(HTTPAPI)
        api.runtime = runtime
        api.compatibility, api.guards = None, ()
        api.authentication = SimpleNamespace(installation_owner=lambda: (SimpleNamespace(id=owner, display_name="Owner"),))
        api.origins = SimpleNamespace(validate=lambda request, mutation=False: None)
        api.statuses, api.owner_routes = {}, {}
        api.route_counters = SimpleNamespace(record=lambda *args, **options: None)

        async def handler(request):
            return await api(request)
        client = TestClient(Starlette(routes=[Route("/api/{path:path}", handler, methods=["GET", "POST"])]))
        self.addCleanup(client.close)
        return client

    def call(self, runtime, agent_id, operation, args=None):
        context = runtime.resolver.invocation_context(Principal(agent_id, "agent"))
        return runtime.application.invoke(context, operation, {"scope": "global", **(args or {})},
                                          InvocationOptions(idempotency_key=new_id()))

    def until(self, check):
        deadline = time.monotonic() + 10
        while not check() and time.monotonic() < deadline:
            time.sleep(0.05)
        return check()

    def test_set_up_on_the_card_then_disconnect(self):
        import sys
        ordubashy, applier = self.created["agents"]["ordubashy"], self.created["agents"]["learning-applier"]
        logs = Logs()
        logging.getLogger().addHandler(logs)
        self.addCleanup(logging.getLogger().removeHandler, logs)
        path = list(sys.path)
        runtime = self.start()
        registry = runtime.extension_registry
        extension = registry.code["keyed"].extension
        self.assertEqual(sys.path, [*path, str(self.paths.installed_extensions.resolve() / "keyed" / "code")])
        # Selected, installed and dormant: not set up, the tool is nobody's, the service waits and nothing is saved.
        self.assertIn("keyed", runtime.selection.ids)
        self.assertEqual(registry.get("keyed").status, "not_set_up")
        [gate] = runtime.extension_gates["keyed"]
        self.assertTrue(gate.running())
        self.assertFalse(extension.ticker.running())
        self.assertNotIn(STATUS_TOOL, self.tools(runtime, ordubashy))
        self.assertEqual(plain(self.call(runtime, ordubashy, STATUS_TOOL).error)["code"], "policy_denied")
        self.assertFalse((self.paths.instance_root / "private" / "extensions" / "keyed").exists())

        # Ordubashy starts the set-up; the owner types the token on the card.
        started = self.call(runtime, ordubashy, "extensions_setup", {"extension": "keyed", "action": "start"})
        self.assertEqual(started.status, "ok", plain(started.error))
        self.assertEqual(plain(started.data)["next"],
                         "Ask the owner to open Settings, then Connections, and finish setting up Keyed on its card.")
        client = self.owner_client(runtime)

        def post(body):
            return client.post("/api/root/connections/keyed", content=json.dumps(body),
                               headers={"Content-Type": "application/json"})
        wrong = post({"action": "accept", "values": {"token": "wrong-token-canary"}}).json()
        self.assertEqual((wrong["data"]["status"], wrong["data"]["note"]),
                         ("needs_attention", "Keyed did not accept this token."))
        answer = post({"action": "accept", "values": {"token": TEST_TOKEN}})
        self.assertEqual(answer.headers["cache-control"], "private, no-store")
        entry = answer.json()["data"]
        self.assertEqual(entry["status"], "ready")
        self.assertEqual([(agent["agent_id"], agent["in_effect"]) for agent in entry["agents"]], [(ordubashy, True)])

        # Ready: in effect for Ordubashy by the role default, so the tool is his and not the Learning Applier's.
        self.assertEqual(registry.on_for(ordubashy), ("keyed",))
        self.assertIn(STATUS_TOOL, self.tools(runtime, ordubashy))
        self.assertNotIn(STATUS_TOOL, self.tools(runtime, applier))
        status = self.call(runtime, ordubashy, STATUS_TOOL)
        self.assertEqual((status.status, plain(status.data)), ("ok", {"token_saved": True}))
        self.assertEqual(plain(self.call(runtime, applier, STATUS_TOOL).error)["code"], "policy_denied")
        # The service runs and ticks.
        self.assertTrue(self.until(lambda: extension.ticker.ticks >= 1))

        # The token is in the private file only, which only this user can read.
        saved = self.paths.instance_root / "private" / "extensions" / "keyed" / "setup.json"
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        for folder in (saved.parent, saved.parent.parent, saved.parent.parent.parent):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(self.holding(TEST_TOKEN), [saved])
        self.assertEqual(self.holding("wrong-token-canary"), [])
        listed = self.call(runtime, ordubashy, "extensions_list")
        shown = json.dumps([plain(listed.data), plain(status.data), entry, plain(started.data),
                            client.get("/api/root/connections/keyed").json()])
        self.assertNotIn(TEST_TOKEN, shown)
        # Only the owner's dashboard: a bearer token is refused, and a key set-up has no image.
        bearer = client.get("/api/root/connections/keyed", headers={"Authorization": "Bearer x"}).json()
        self.assertEqual(bearer["error"]["code"], "forbidden")
        image = client.get("/api/root/connections/keyed/image")
        self.assertEqual((image.status_code, image.headers["x-content-type-options"]), (404, "nosniff"))

        # Switched off for Ordubashy, the tool is neither listed for him nor callable; switched on again, it is.
        off = post({"action": "switch", "agent_id": ordubashy, "on": False}).json()
        self.assertEqual(off["data"]["agents"][0]["in_effect"], False, off)
        self.assertNotIn(STATUS_TOOL, self.tools(runtime, ordubashy))
        self.assertEqual(plain(self.call(runtime, ordubashy, STATUS_TOOL).error)["code"], "policy_denied")
        post({"action": "switch", "agent_id": ordubashy, "on": True})
        self.assertEqual(self.call(runtime, ordubashy, STATUS_TOOL).status, "ok")

        # Disconnecting hides the tool, refuses its call and stops the service.
        self.assertEqual(post({"action": "disconnect"}).json()["data"]["status"], "not_set_up")
        self.assertNotIn(STATUS_TOOL, self.tools(runtime, ordubashy))
        self.assertEqual(plain(self.call(runtime, ordubashy, STATUS_TOOL).error)["code"], "policy_denied")
        self.assertTrue(self.until(lambda: not extension.ticker.running()))
        self.assertEqual(self.holding(TEST_TOKEN), [])
        runtime.stop()
        self.assertFalse(any(TEST_TOKEN in line or "wrong-token-canary" in line for line in logs.lines))

        # An install that reaches the real registry past the view registers a tool outside the gate's sight: the
        # extension is refused, Qorqbot starts, and the stray tool is routed to it, so no agent has it or can call it.
        code = self.paths.installed_extensions / "keyed" / "code" / "qorqut_keyed" / "extension.py"
        original = code.read_text()
        code.write_text(original + STRAY)
        later = self.restart(path)
        entry = later.extension_registry.get("keyed")
        self.assertEqual((entry.status, entry.note),
                         ("needs_attention", "Its code could not be loaded: it registers tools outside its install."))
        self.assertEqual(later.extension_registry.extension_of("keyed_stray"), "keyed")
        for agent_id in (ordubashy, applier):
            self.assertNotIn("keyed_stray", self.tools(later, agent_id))
            self.assertEqual(plain(self.call(later, agent_id, "keyed_stray").error)["code"], "policy_denied")
        self.assertFalse(later.registry.known(STATUS_TOOL))
        self.assertEqual((later.extension_registry.code, later.extension_gates), ({}, {"keyed": ()}))
        self.assertEqual(sys.path, path)
        self.assertEqual([name for name in sys.modules if name.split(".")[0] == "qorqut_keyed"], [])
        self.assertIn("extensions_list", self.tools(later, ordubashy))
        later.stop()

        # A service whose name is taken by a step of Qorqbot leaves Qorqbot starting and the extension needing attention.
        code.write_text(original.replace('ServiceDeclaration("keyed_ticker"', 'ServiceDeclaration("background"'))
        later = self.restart(path)
        entry = later.extension_registry.get("keyed")
        self.assertEqual((entry.status, entry.note), ("needs_attention", SERVICES_DO_NOT_FIT))
        self.assertEqual(later.extension_gates, {"keyed": ()})
        # Its tool was registered before its services were checked; it stays routed to it, so nobody has it.
        self.assertEqual(later.extension_registry.extension_of(STATUS_TOOL), "keyed")
        self.assertNotIn(STATUS_TOOL, self.tools(later, ordubashy))
        self.assertEqual(plain(self.call(later, ordubashy, STATUS_TOOL).error)["code"], "policy_denied")
        later.stop()

    def restart(self, path):
        import sys
        for name in [name for name in sys.modules if name.split(".")[0] == "qorqut_keyed"]:
            del sys.modules[name]
        sys.path[:] = path
        return self.start()

    def holding(self, value):
        """Every file of the instance holding ``value``, leaving out the extension's own folder (its code names the
        test token)."""
        own = self.paths.installed_extensions / "keyed"
        return sorted(path for path in self.paths.instance_root.rglob("*")
                      if path.is_file() and not path.is_symlink() and not path.is_relative_to(own)
                      and value.encode() in path.read_bytes())


class Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


if __name__ == "__main__":
    unittest.main()
