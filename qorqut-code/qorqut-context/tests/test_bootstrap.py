import random
from types import SimpleNamespace
from unittest import TestCase

from qorqut_context import ContextError
from qorqut_context.bootstrap import candidates, prepare, sample, INVITATION


class Graph:
    revision = "a" * 40

    def __init__(self):
        self.nodes = {f"{i:05d}": {"id": f"{i:05d}", "summary": "Unrelated",
                                 "attributes": {"basic_entity_type": ["entity"]}}
                      for i in range(50)}
        self.nodes["00000"]["attributes"]["alias"] = ["ACME", "AI"]
        self.nodes["00001"]["summary"] = "Straße"
        self.edges = {f"e{i:04d}": {"id": f"e{i:04d}", "from_id": "00000", "to_id": f"{i % 50:05d}"}
                      for i in range(150)}

    def read(self, kind, key):
        return {"record": (self.nodes if kind == "node" else self.edges)[key],
                "support": ["b" * 40], "revision": self.revision}


class Bootstrap(TestCase):
    def test_candidates_selection_caps_and_explicit_omissions(self):
        graph = Graph()
        change = {"before": "acme and STRASSE", "after": "An AI idea"}
        self.assertEqual([c["record"]["id"] for c in candidates(graph, change)], ["00000", "00001"])
        runner = SimpleNamespace(run=lambda **kwargs: SimpleNamespace(status="completed", output='{"node_ids":["00000","00000"]}'))
        result = prepare(graph, change, runner, directory=None, rng=random.Random(123))
        self.assertEqual(result["X"], ["00000"])
        self.assertEqual(len(set(result["Y"])), 40)
        self.assertNotIn("00000", result["Y"])
        self.assertEqual(len(set(result["edge_ids"])), 60)
        self.assertEqual(result["truncated"], {"neighbors": True, "edges": True})
        included = set(result["X"] + result["Y"])
        self.assertTrue(all(item["record"]["from_id"] in included and item["record"]["to_id"] in included for item in result["edges"]))
        self.assertIn(INVITATION, result["notice"])
        runner.run = lambda **kwargs: SimpleNamespace(status="completed", output='{"node_ids":["99999"]}')
        with self.assertRaisesRegex(ContextError, "outside"):
            prepare(graph, change, runner, directory=None)
        with self.assertRaisesRegex(ContextError, "no candidates were omitted"):
            prepare(graph, change, runner, directory=None, max_payload_bytes=10)
        # Edges wholly inside X share the same 60-edge cap, including parallel/self edges.
        self.assertEqual(len(sample(graph, list(graph.nodes), rng=random.Random(1))["edges"]), 60)

    def test_empty_graph_needs_no_model(self):
        graph = Graph()
        graph.nodes, graph.edges = {}, {}
        result = prepare(graph, {"before": None, "after": "New context"}, None, directory=None)
        self.assertEqual(result["X"], [])
        self.assertFalse(result["model_called"])
        self.assertEqual(result["notice"], INVITATION)
