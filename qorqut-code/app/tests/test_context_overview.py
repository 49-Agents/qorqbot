"""``context_overview`` is the owner's whole-graph view: one bounded read of every published node and edge in compact form.

It lists exactly what ``context_home`` pages through, in the same order, at one pinned revision, and it is never an agent
tool: no agent read, no MCP exposure, no tool-instruction message."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

from qorqut_agentic.behavior.tool_help import defaults as instruction_defaults
from qorqut_context import Pipeline
from qorqut_context.engine import Context
from qorqut_context.integration.reading import SharedContextReader, shared_context_contribution
from qorqut_context.integration.reading import operations as reading_operations
from qorqut_context.integration.reading import reader as reader_module
from qorqut_context.integration.reading import schemas
from qorqut_context.integration.registration import SHARED_CONTEXT_READS
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationOptions, new_id, plain

from qorqut.composition.catalog import builtin_registry, install_contribution
from qorqut.composition.policy import resolve_operation_policy

from private_thread_runtime import build_runtime

LONG = "n" * 300


def put(kind, record):
    return {"op": "put_" + kind, "record": record, "explanation": "test", "citations": ["self"], "support_after": ["self"]}


def node(node_id, **attributes):
    return put("node", {"id": node_id, "summary": "A long definition. " * 40, "attributes": attributes})


def edge(edge_id, source, target, **attributes):
    return put("edge", {"id": edge_id, "summary": "Edge.", "from_id": source, "to_id": target, "attributes": attributes})


OPERATIONS = [
    node("hub01", name=["Hub"], basic_entity_type=["company"], notes=["private detail " * 10]),
    node("leaf1", name=[LONG], basic_entity_type=["person"]),
    node("leaf2", basic_entity_type=["topic"]),
    node("leaf3", name=[42], basic_entity_type=["number"]),
    edge("e0001", "hub01", "leaf1", name=["works with"], tags=["ignored"]),
    edge("e0002", "hub01", "leaf2", tags=["about"]),
    edge("e0003", "leaf3", "hub01", name=[LONG]),
    edge("e0004", "hub01", "hub01"),
]


class Graph:
    """A real initialized Context with one published edit, read through a real reader."""

    def __init__(self, case):
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.context = Context.initialize(self.root / "context", self.root / "context-runtime")
        self.reader = SharedContextReader(self.root / "context", self.root / "context-runtime")
        case.addCleanup(self.reader.close)
        base = self.reader.status()["revision"]
        Pipeline(self.context).submit_edit({"format": "qorqut-graph-edit-v1", "expected_revision": base,
                                            "rationale": "test graph", "operations": OPERATIONS},
                                           submitted_by="test", event_id="evnt1")
        self.base = base

    def home_pages(self):
        nodes, cursor = [], None
        while True:
            page = self.reader.home(limit=2, cursor=cursor)
            nodes += page["nodes"]
            cursor = page["page"]["next_cursor"]
            if cursor is None:
                return page, nodes


class OverviewReadTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph(self)
        self.reader = self.graph.reader

    def test_the_shape_is_compact_and_matches_the_schema(self):
        overview = self.reader.overview()
        Draft202012Validator(schemas.OVERVIEW_OUTPUT).validate(overview)
        self.assertEqual(set(overview), {"status", "revision", "totals", "truncated", "nodes", "edges"})
        self.assertEqual((overview["status"], overview["truncated"]), ("ready", False))
        self.assertEqual(overview["revision"], self.reader.status()["revision"])
        self.assertEqual(overview["totals"], {"nodes": 5, "edges": 4})
        nodes = {item["id"]: item for item in overview["nodes"]}
        self.assertEqual(nodes["hub01"], {"id": "hub01", "name": "Hub", "type": "company", "degree": 4})
        self.assertEqual(nodes["leaf1"], {"id": "leaf1", "name": "n" * 120, "type": "person", "degree": 1})
        self.assertEqual(nodes["leaf2"], {"id": "leaf2", "name": "", "type": "topic", "degree": 1})
        self.assertEqual(nodes["leaf3"]["name"], "42")
        edges = {item["id"]: item for item in overview["edges"]}
        self.assertEqual([item["id"] for item in overview["edges"]], ["e0001", "e0002", "e0003", "e0004"])
        self.assertEqual(edges["e0001"], {"id": "e0001", "from": "hub01", "to": "leaf1", "name": "works with", "tag": "ignored"})
        self.assertEqual(edges["e0002"]["name"], "about")              # no name: the first tag
        self.assertEqual(edges["e0002"]["tag"], "about")
        self.assertEqual(edges["e0003"]["name"], "n" * 80)
        self.assertEqual(edges["e0004"], {"id": "e0004", "from": "hub01", "to": "hub01", "name": "", "tag": ""})
        # Definitions and other attributes stay with context_home and context_object.
        self.assertNotIn("A long definition", str(overview))
        self.assertNotIn("private detail", str(overview))

    def test_nodes_come_in_context_home_order_and_degree_is_its_relationship_count(self):
        _, home_nodes = self.graph.home_pages()
        overview = self.reader.overview()
        self.assertEqual([item["id"] for item in overview["nodes"]], [item["id"] for item in home_nodes])
        self.assertEqual([item["degree"] for item in overview["nodes"]],
                         [item["relationship_count"] for item in home_nodes])

    def test_it_lists_exactly_what_context_home_lists_and_nothing_more(self):
        last, home_nodes = self.graph.home_pages()
        overview = self.reader.overview()
        listed = {item["id"] for item in home_nodes}
        # The owner's record is a node of home too, and the overview adds none of its facts beyond name and type.
        owner = last["personal_context"]["ref"].split(":")[1]
        self.assertIn(owner, listed)
        self.assertEqual({item["id"] for item in overview["nodes"]}, listed)
        self.assertNotIn("personal_context", overview)
        home_edges = {relationship["id"] for item in home_nodes for relationship in item["relationships"]}
        self.assertEqual({item["id"] for item in overview["edges"]}, home_edges)
        self.assertTrue(all(item["from"] in listed and item["to"] in listed for item in overview["edges"]))
        self.assertEqual(overview["totals"], {key: last["totals"][key] for key in ("nodes", "edges")})

    def test_a_pinned_revision_is_read_as_it_was(self):
        before = self.reader.overview(revision=self.graph.base)
        self.assertEqual((before["revision"], before["totals"], before["edges"]), (self.graph.base, {"nodes": 1, "edges": 0}, []))
        with self.assertRaises(DomainError):
            self.reader.overview(revision="f" * 40)

    def test_the_lists_are_bounded_and_say_when_they_stop(self):
        with mock.patch.object(reader_module, "OVERVIEW_NODES", 2):
            overview = self.reader.overview()
        self.assertTrue(overview["truncated"])
        self.assertEqual([item["id"] for item in overview["nodes"]], [item["id"] for item in self.reader.home(limit=2)["nodes"]])
        listed = {item["id"] for item in overview["nodes"]}
        self.assertTrue(all(item["from"] in listed and item["to"] in listed for item in overview["edges"]))
        self.assertEqual(overview["totals"], {"nodes": 5, "edges": 4})       # totals still count the whole graph
        with mock.patch.object(reader_module, "OVERVIEW_EDGES", 1):
            overview = self.reader.overview()
        self.assertEqual((overview["truncated"], len(overview["edges"]), len(overview["nodes"])), (True, 1, 5))
        self.assertEqual(schemas.OVERVIEW_OUTPUT["oneOf"][0]["properties"]["nodes"]["maxItems"], schemas.OVERVIEW_NODES)
        self.assertEqual((schemas.OVERVIEW_NODES, schemas.OVERVIEW_EDGES), (20000, 50000))


class NotReadyTests(unittest.TestCase):
    def test_an_absent_repository_answers_like_context_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            reader = SharedContextReader(root / "context", root / "runtime")
            self.addCleanup(reader.close)
            overview = reader.overview()
            self.assertEqual(overview, reader.home())
            self.assertEqual(overview["status"], "not_initialized")
            Draft202012Validator(schemas.OVERVIEW_OUTPUT).validate(overview)
            self.assertFalse((root / "context").exists())


def role(name):
    return ArtifactRef(name, "1", "a" * 64)


class OwnerOnlyTests(unittest.TestCase):
    def test_it_is_an_owner_view_and_never_an_agent_tool(self):
        entry, = [item for item in reading_operations.ENTRIES if item.name == "context_overview"]
        self.assertTrue(entry.owner_only)
        self.assertEqual(entry.exposure, frozenset({"dashboard"}))
        self.assertNotIn("context_overview", reading_operations.AGENT_READS)
        self.assertNotIn("context_overview", SHARED_CONTEXT_READS)
        # Its description is plain text, so the retained tool-instruction library is unchanged.
        self.assertNotIn("context_overview", str(instruction_defaults()))
        self.assertNotIn(reading_operations.DESCRIPTIONS["context_overview"], instruction_defaults().values())
        registry = builtin_registry()
        self.assertTrue(registry.metadata("context_overview").owner_only)
        for agent in ("ordubashy", "global-worker", "learning-applier"):
            for scope in ("global", "project"):
                decision = resolve_operation_policy("context_overview", {}, {}, registry, role_id=agent, scope=scope)
                self.assertNotEqual(decision.mode, "allow", (agent, scope))

    def test_the_handler_refuses_anyone_but_a_human(self):
        operations = reading_operations.SharedContextOperations(SharedContextReader(Path("/nonexistent/c"), Path("/nonexistent/r")))
        with self.assertRaises(DomainError) as refused:
            operations._overview(SimpleNamespace(principal_kind="agent"), {})
        self.assertEqual(refused.exception.code, "forbidden")


class ThroughTheApplicationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.runtime = runtime = build_runtime(root)
        self.addCleanup(runtime.database.close)
        runtime.application.policy.refresh_context = lambda ctx, uow: ctx
        Context.initialize(root / "context", root / "context-runtime")
        self.reader = SharedContextReader(root / "context", root / "context-runtime")
        self.addCleanup(self.reader.close)
        install_contribution(runtime.application.registry, shared_context_contribution(self.reader))

    def call(self, ctx, arguments=None):
        return self.runtime.application.invoke(ctx, "context_overview", arguments or {},
                                               InvocationOptions(idempotency_key=new_id()))

    def test_the_owner_reads_it_and_no_agent_does(self):
        result = self.call(self.runtime.owner)
        self.assertEqual(result.status, "ok", plain(result.error))
        self.assertEqual(plain(result.data), self.reader.overview())
        lead = replace(self.runtime.coordinator, role_artifact_ref=role("ordubashy"))
        worker = replace(self.runtime.worker, role_artifact_ref=role("global-worker"))
        for agent in (lead, worker, replace(worker, source="worker"), replace(worker, source="dashboard")):
            result = self.call(agent)
            self.assertEqual(result.status, "error", agent.source)
        self.assertEqual(self.call(self.runtime.owner, {"revision": "a" * 40}).error["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
