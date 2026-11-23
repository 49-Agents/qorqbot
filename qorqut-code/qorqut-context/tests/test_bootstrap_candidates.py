"""The bootstrap shortlist: ranked, pinned, bounded by the input and never by the size of the graph."""

import json
import unittest

from qorqut_context import bootstrap
from qorqut_context.schema import ContextError, encode


class Graph:
    """The parts of a published snapshot the bootstrap reads."""

    revision = "r1"

    def __init__(self, nodes, edges=(), personal=None):
        self.nodes = {key: {"id": key, "summary": summary, "attributes": attributes} for key, (summary, attributes) in nodes.items()}
        self.edges = {f"e{index}": {"id": f"e{index}", "from_id": a, "to_id": b, "summary": "", "attributes": {}}
                      for index, (a, b) in enumerate(edges)}
        self.notes, self.personal_context_node = {}, personal

    def read(self, kind, key):
        return {"record": (self.nodes if kind == "node" else self.edges)[key], "support": [], "revision": self.revision}


def change(text):
    return {"before": None, "after": text}


class Runner:
    def __init__(self, select):
        self.select, self.payloads = select, []

    def run(self, **arguments):
        self.payloads.append(arguments["payload"])
        ids = [item["record"]["id"] for item in arguments["payload"]["candidates"]]
        return type("Result", (), {"status": "completed", "output": json.dumps({"node_ids": self.select(ids)})})()


def filler(count, text="the owner and the project are with this"):
    return {f"f{index:03d}": (text, {"basic_entity_type": ["topic"]}) for index in range(count)}


class Ranking(unittest.TestCase):
    def test_a_rare_shared_word_outranks_common_ones(self):
        graph = Graph({**filler(30), "rare": ("A note about the zeppelin hangar", {})})
        ordered, pinned = bootstrap.rank(graph, change("The owner asked about the zeppelin and the project"))
        self.assertEqual(ordered[0], "rare")
        self.assertEqual(pinned, [])

    def test_a_long_record_gains_nothing_from_its_length(self):
        graph = Graph({"long": ("zeppelin " + "padding words about nothing in particular " * 200, {}),
                       "short": ("zeppelin hangar", {}), **filler(10)})
        ordered, _ = bootstrap.rank(graph, change("the zeppelin"))
        self.assertLess(ordered.index("short"), ordered.index("long"))

    def test_a_named_node_is_pinned_first(self):
        graph = Graph({**filler(30, "gianni zeppelin hangar consultancy agency"),
                       "named": ("A person. " + "unrelated filler text " * 300, {"name": ["Gianni Rossi"]})})
        ordered, pinned = bootstrap.rank(graph, change("Met Gianni Rossi about the zeppelin hangar consultancy agency"))
        self.assertEqual((ordered[0], pinned), ("named", ["named"]))

    def test_a_name_needs_whole_words(self):
        graph = Graph({"named": ("", {"name": ["Ann"]}), **filler(3)})
        self.assertEqual(bootstrap.rank(graph, change("The annual plan"))[1], [])
        self.assertEqual(bootstrap.rank(graph, change("Ann sent the plan"))[1], ["named"])

    def test_a_value_shared_by_many_nodes_names_none_of_them(self):
        graph = Graph({f"p{index}": ("", {"basic_entity_type": ["person"]}) for index in range(bootstrap.EXACT_SHARED + 1)})
        self.assertEqual(bootstrap.rank(graph, change("a person called"))[1], [])

    def test_the_personal_node_and_hubs_are_pinned_whatever_they_share(self):
        nodes = {"me": ("Installation owner", {}), "hub": ("A project", {}), "leaf": ("zeppelin", {}), **filler(5)}
        graph = Graph(nodes, edges=[("hub", f"f{index:03d}") for index in range(5)], personal="me")
        ordered, pinned = bootstrap.rank(graph, change("zeppelin"))
        self.assertEqual(pinned[0], "me")
        self.assertIn("hub", pinned)
        self.assertEqual(ordered[len(pinned)], "leaf")

    def test_the_order_is_a_function_of_graph_and_input(self):
        graph = Graph({**filler(40), "x": ("zeppelin", {}), "y": ("zeppelin", {})})
        self.assertEqual(bootstrap.rank(graph, change("the zeppelin")), bootstrap.rank(graph, change("the zeppelin")))


class Shortlist(unittest.TestCase):
    def test_the_shortlist_is_capped_and_counted(self):
        graph = Graph(filler(250))
        cards, counts = bootstrap.candidates(graph, change("the owner"), limit=100)
        self.assertEqual((len(cards), counts), (100, {"matched": 250, "exact": 0, "shown": 100}))

    def test_a_card_is_bounded_and_says_it_was_cut(self):
        graph = Graph({"big": ("s" * 5000, {"name": ["Big"], "body": ["b" * 20000], "notes": ["n" * 300, "m" * 300]})})
        card = bootstrap.card(graph, "big")
        self.assertLessEqual(len(encode(card["record"])), bootstrap.CARD_BYTES)
        self.assertTrue(card["truncated"])
        self.assertEqual(card["record"]["attributes"]["name"], ["Big"])
        small = bootstrap.card(Graph({"s": ("short", {"name": ["S"]})}), "s")
        self.assertEqual(small, {"record": {"id": "s", "summary": "short", "attributes": {"name": ["S"]}}, "truncated": False})


class Prepare(unittest.TestCase):
    def test_the_model_sees_the_shortlist_and_its_counts(self):
        graph, runner = Graph({**filler(150), "rare": ("zeppelin", {})}), Runner(lambda ids: ids[:1])
        result = bootstrap.prepare(graph, change("the zeppelin"), runner, directory=".")
        payload = runner.payloads[0]
        self.assertEqual(payload["candidate_counts"], {"matched": 151, "exact": 0, "shown": 100})
        self.assertIn("100 best-matching of 151", payload["candidate_notice"])
        self.assertEqual((result["X"], result["candidate_counts"]), (["rare"], payload["candidate_counts"]))

    def test_an_oversized_request_drops_its_lowest_ranked_candidates(self):
        graph, runner = Graph({**filler(150, "the owner " * 30), "rare": ("zeppelin", {})}), Runner(lambda ids: [])
        result = bootstrap.prepare(graph, change("the zeppelin"), runner, directory=".", max_payload_bytes=20000)
        shown = runner.payloads[0]["candidates"]
        self.assertLess(len(shown), 100)
        self.assertEqual(shown[0]["record"]["id"], "rare")
        self.assertEqual(result["candidate_counts"]["shown"], len(shown))
        self.assertLessEqual(len(encode({"instructions": bootstrap.INSTRUCTIONS, "payload": runner.payloads[0]})), 20000)

    def test_only_a_source_that_does_not_fit_fails(self):
        with self.assertRaisesRegex(ContextError, "source change exceeds"):
            bootstrap.prepare(Graph(filler(5)), change("the " * 20000), Runner(lambda ids: []), directory=".", max_payload_bytes=20000)

    def test_a_selection_outside_the_shortlist_is_refused(self):
        with self.assertRaisesRegex(ContextError, "outside the supplied candidates"):
            bootstrap.prepare(Graph({**filler(150), "rare": ("zeppelin", {})}), change("the zeppelin"),
                              Runner(lambda ids: ["f149"]), directory=".", limit=10)

    def test_no_candidate_needs_no_model(self):
        runner = Runner(lambda ids: ids)
        result = bootstrap.prepare(Graph({"a": ("alpha", {})}), change("zeppelin"), runner, directory=".")
        self.assertEqual((runner.payloads, result["model_called"], result["X"]), ([], False, []))


if __name__ == "__main__":
    unittest.main()
