"""The installed extensions in a real runtime: a new instance seeded as ``qorqut.bootstrap.initialize`` seeds one, the
``network`` example in its ``extensions/`` folder, then two starts of ``build_runtime``.

The instance is made from an export of this checkout's last commit (``tools/export-unified.py``), which gives the role
and instruction sources; the code that runs is the code being tested. Without a Git checkout the test is skipped."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain

REPOSITORY = Path(__file__).resolve().parents[3]
EXAMPLE = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "network"


def _commit():
    try:
        return subprocess.run(["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class ExtensionRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        shutil.copytree(EXAMPLE, cls.paths.installed_extensions / "network")

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

    def test_install_switch_compose_and_removal(self):
        ordubashy, applier = self.created["agents"]["ordubashy"], self.created["agents"]["learning-applier"]
        runtime = self.start()
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

        # With no extension installed, an agent's configuration is what it is with no registry at all; only the
        # operations installed (the two of the owner's) are new, and no agent can use them.
        def composed(agent_id):
            resolver = later.resolver
            return resolver._compose(resolver._snapshot(Principal(agent_id, "agent"), None)).to_dict()
        configs = {agent_id: composed(agent_id) for agent_id in (ordubashy, applier)}
        later.resolver.extensions = later.agentic_view.extensions = None
        for agent_id, config in configs.items():
            self.assertEqual(config, composed(agent_id))
            self.assertNotIn("extensions_in_effect", config)
            self.assertFalse(any(tool["available"] for tool in config["tools"]
                                 if tool["canonical_operation"] in ("extensions_list", "extensions_switch")))


if __name__ == "__main__":
    unittest.main()
