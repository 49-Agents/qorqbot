"""The five tools that manage extensions in a real runtime, on a new seeded instance with nothing installed: with no
index none is any agent's, so every agent's tools are as before; with an index offering one entry Ordubashy has the list
and the install, the list shows the entry as not installed, and the Learning Applier has the list only."""
import importlib.util
import json
from pathlib import Path
import shutil
from unittest import mock

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain

from test_extension_runtime import EXAMPLE, RuntimeCase

DISTRIBUTION = Path(__file__).resolve().parents[1].parent / "tools" / "distribution"


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"tool_{name}", DISTRIBUTION / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IndexRuntimeTest(RuntimeCase):
    FOLDER = EXAMPLE

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Nothing installed: the example is only the source of the index entry.
        shutil.rmtree(cls.paths.installed_extensions / EXAMPLE.name)
        cls.index = cls.paths.instance_root.parent / "index.json"
        out = cls.paths.instance_root.parent / "release"
        out.mkdir()
        bundle = load_tool("extension_bundle").build_folder(EXAMPLE, "any", out / "network.tar.gz")["path"]
        cls.offering = load_tool("extension_index").build_index([EXAMPLE], [bundle], "1.7.0", out.as_uri())

    def call(self, runtime, agent_id, operation, args=None):
        context = runtime.resolver.invocation_context(Principal(agent_id, "agent"))
        return runtime.application.invoke(context, operation, {"scope": "global", **(args or {})},
                                          InvocationOptions(idempotency_key=new_id()))

    def test_no_index_none_of_the_five_and_one_entry_all_five_by_policy(self):
        from qorqut.extension_operations import MANAGING
        ordubashy, applier = self.created["agents"]["ordubashy"], self.created["agents"]["learning-applier"]
        with mock.patch("qorqut.extension_index.index_path", return_value=self.index):
            runtime = self.start()
            for agent_id in self.created["agents"].values():
                self.assertEqual(self.tools(runtime, agent_id) & MANAGING, set(), agent_id)
            self.assertNotEqual(self.call(runtime, ordubashy, "extensions_list").status, "ok")
            runtime.stop()

            self.index.write_text(json.dumps(self.offering))
            later = self.start()
            self.assertEqual(self.tools(later, ordubashy) & {"extensions_list", "extensions_install"},
                             {"extensions_list", "extensions_install"})
            self.assertEqual(self.tools(later, applier) & MANAGING, {"extensions_list"})
            listed = self.call(later, ordubashy, "extensions_list")
            self.assertEqual(listed.status, "ok", plain(listed.error) if listed.error else None)
            self.assertEqual([(item["id"], item["status"]) for item in plain(listed.data)["extensions"]],
                             [("network", "not_installed")])
