"""Controlled deterministic graph edits: synthetic Git stores and a runner that fails if it is ever invoked."""
from contextlib import contextmanager
from copy import deepcopy
import json
from unittest.mock import patch

from qorqut_context import Conflict, ContextError
from qorqut_context import graph_edits
from qorqut_context.git_store import Snapshot
from qorqut_context.pipeline import Pipeline, PropagationError
from qorqut_context.runner import RunnerConfig
from qorqut_context.schema import encode
from .helpers import ContextCase
from .test_pipeline import ScriptRunner, finish, new_node
from .test_recovery import ProcessStopped

NORTH = {"scope": {"kind": "business", "business_id": "business:north"}, "provenance": {"channel": "chat"}}
SOUTH = {"scope": {"kind": "business", "business_id": "business:south"}, "provenance": {"channel": "email"}}


class NoModel:
    """An injected runner that fails the test if a model call is ever attempted."""
    config = RunnerConfig()

    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs.get("stage"))
        raise AssertionError("No model may run for a deterministic graph edit")


def plan(base, *operations, rationale="Reconcile the recorded people."):
    return {"format": graph_edits.FORMAT, "expected_revision": base, "rationale": rationale,
            "operations": list(operations)}


def put(kind, record, *, citations=("self",), support=("self",), explanation="Corrected by the owner."):
    return {"op": "put_" + kind, "record": record, "explanation": explanation,
            "citations": list(citations), "support_after": list(support)}


def remove(kind, object_id, *, citations=("self",), explanation="No longer true."):
    return {"op": "remove_" + kind, "id": object_id, "explanation": explanation, "citations": list(citations)}


def person(object_id, summary, **attributes):
    return {"id": object_id, "summary": summary,
            "attributes": {"basic_entity_type": ["person"], "name": [summary], **attributes}}


def link(object_id, left, right, summary="Related"):
    return {"id": object_id, "from_id": left, "to_id": right, "summary": summary,
            "attributes": {"relation": ["family", "manager"]}}


class GraphEditCase(ContextCase):
    """A published base of three people and two edges, and a pipeline that must never reach a model."""

    def setUp(self):
        super().setUp()
        event = self.source("one.md")
        self.node(event, "jesse", summary="Jesse")
        self.node(event, "alice", summary="Alice")
        self.node(event, "jess1", summary="Jess")
        self.edge(event, "edgea", "jesse", "alice")
        self.edge(event, "edgeb", "jess1", "alice")
        self.base = self.context.publish(event.event_id)
        self.runner = NoModel()
        self.pipeline = Pipeline(self.context, runner=self.runner)
        # Deterministic work must not reach bootstrap sampling or reserve per-call usage receipts either.
        for target in ("qorqut_context.pipeline.bootstrap.prepare", "qorqut_context.pipeline.usage.AttemptCalls"):
            forbidden = patch(target, side_effect=AssertionError("model machinery reached"))
            forbidden.start()
            self.addCleanup(forbidden.stop)

    def submit(self, value, event_id="edit1", **kwargs):
        return self.pipeline.submit_edit(value, submitted_by="host-owner", event_id=event_id, **kwargs)

    def assertUntouched(self, head=None):
        self.assertIsNone(self.context.pending())
        self.assertEqual(self.context.git.head(), head or self.base)
        events = self.context.runtime / "events"
        self.assertEqual(list(events.iterdir()) if events.exists() else [], [])
        self.assertFalse((self.context.runtime / "runs").exists())
        self.assertEqual(self.runner.calls, [])

    def advance(self):
        other = self.source("two.md", b"A later, unrelated note.")
        self.node(other, "later")
        return self.context.publish(other.event_id)

    def three(self):
        return plan(self.base,
                    put("node", person("bob12", "Bob"), citations=[self.base]),
                    put("edge", link("edgen", "alice", "bob12")),
                    remove("edge", "edgeb"))

    def stop_after(self, phase, count=1):
        seen = []

        def stop(name):
            if name == phase:
                seen.append(name)
                if len(seen) == count:
                    raise ProcessStopped()
        return patch.object(self.pipeline, "_checkpoint", side_effect=stop)

    def interrupted(self, value=None, event_id="edit1"):
        """A pending controlled edit with exactly its first operation staged."""
        with self.stop_after("edit_staged"), self.assertRaises(ProcessStopped):
            self.submit(value or self.three(), event_id)
        pending = self.context.pending()
        self.assertEqual((pending["id"], len(pending["draft"])), (event_id, 1))
        self.assertEqual(self.context.git.head(), self.base)
        return pending


class Execution(GraphEditCase):
    def test_multi_object_edit_publishes_the_exact_graph_and_attribution(self):
        value = plan(self.base,
                     put("node", person("bob12", "Bob"), citations=[self.base]),
                     put("node", person("alice", "Alice Smith"), citations=["self", self.base],
                         support=[self.base, "self"]),
                     put("edge", link("edgen", "alice", "bob12")),
                     remove("edge", "edgeb"),
                     remove("node", "jess1", citations=[self.base]))
        receipt = self.submit(value, record_context=NORTH)
        commit = receipt["published_commit"]
        marker = graph_edits.marker(graph_edits.normalize(value))
        self.assertEqual((receipt["status"], receipt["event_id"], receipt["base_revision"], receipt["input_mode"]),
                         ("published", "edit1", self.base, "GENERAL"))
        self.assertEqual((receipt["execution"], receipt["model"], receipt["diagnostics"], receipt["recovered"]),
                         (marker, None, None, False))
        self.assertNotIn("model_execution", receipt)
        self.assertEqual(receipt["result_counts"], {"node": {"add": 1, "edit": 1, "remove": 1},
                                                   "edge": {"add": 1, "edit": 0, "remove": 1},
                                                   "note": {"add": 0, "edit": 0, "remove": 0}})
        snapshot = self.context.snapshot()
        self.assertEqual(sorted(snapshot.nodes), ["alice", "bob12", "jesse"])
        self.assertEqual(sorted(snapshot.edges), ["edgea", "edgen"])
        self.assertEqual(snapshot.nodes["alice"]["summary"], "Alice Smith")
        self.assertEqual(snapshot.support[("node", "alice")], [self.base, commit])
        self.assertEqual(snapshot.support[("node", "bob12")], [commit])
        self.assertEqual(snapshot.support[("node", "jesse")], [self.base])
        event = snapshot.event("edit1")
        self.assertEqual({(change["kind"], change["id"]): (change["operation"], change["citations"], change["support_after"])
                          for change in event["changes"]},
                         {("edge", "edgeb"): ("remove", [commit], []),
                          ("edge", "edgen"): ("add", [commit], [commit]),
                          ("node", "alice"): ("edit", [commit, self.base], [self.base, commit]),
                          ("node", "bob12"): ("add", [self.base], [commit]),
                          ("node", "jess1"): ("remove", [self.base], [])})
        self.assertTrue(all(change["developments"] == [] for change in event["changes"]))
        self.assertEqual((event["submitted_by"], event["record_context"], event["execution"]),
                         ("host-owner", NORTH, marker))
        self.assertEqual(event["handoff_note"], {"disposition": "empty", "text": ""})
        self.assertEqual(event["source"]["path"], "foundation/edits/edit1.txt")
        self.assertEqual({key: event["source"][key] for key in ("kind", "title", "origin")}, graph_edits.METADATA)
        self.assertEqual(snapshot.files["foundation/edits/edit1.txt"], graph_edits.canonical(graph_edits.normalize(value)))
        self.assertEqual(json.loads(snapshot.files["foundation/edits/edit1.txt"]), value)
        history = snapshot.history(event["source"]["id"], kind="source")
        self.assertEqual((history[0]["execution"], history[0]["record_context"]), (marker, NORTH))
        # The marker, not an empty usage page, establishes that no model ran.
        self.assertEqual(self.pipeline.usage_receipts("edit1")["items"], [])
        self.assertFalse((self.context.runtime / "runs").exists())
        self.assertEqual(self.runner.calls, [])
        self.assertIsNone(self.context.pending())
        self.assertEqual([hit.id for hit in self.context.search("Bob") if hit.kind == "node"], ["bob12"])

    def test_support_only_edit_keeps_the_record_and_adds_the_edit_source(self):
        record = deepcopy(self.context.snapshot().nodes["jesse"])
        receipt = self.submit(plan(self.base, put("node", record, support=[self.base, "self"])))
        commit = receipt["published_commit"]
        self.assertEqual(receipt["result_counts"]["node"], {"add": 0, "edit": 1, "remove": 0})
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.nodes["jesse"], record)
        self.assertEqual(snapshot.support[("node", "jesse")], [self.base, commit])
        self.assertEqual(self.context.git.files(commit)["graph/nodes/jesse.json"],
                         self.context.git.files(self.base)["graph/nodes/jesse.json"])
        self.assertEqual([change["id"] for change in snapshot.event("edit1")["changes"]], ["jesse"])

    def test_a_merge_is_composed_from_explicit_object_and_edge_changes(self):
        merged = person("jesse", "Jesse")
        merged["attributes"]["name"] = ["Jesse", "Jess"]
        moved = link("edgeb", "jesse", "alice", "Merged relationship")
        with self.assertRaisesRegex(ContextError, "endpoint"):
            self.submit(plan(self.base, put("node", merged, support=[self.base, "self"]),
                             remove("node", "jess1", citations=[self.base, "self"])))
        self.assertUntouched()
        receipt = self.submit(plan(self.base, put("node", merged, support=[self.base, "self"]),
                                   put("edge", moved, support=[self.base, "self"]),
                                   remove("node", "jess1", citations=[self.base, "self"])))
        snapshot = self.context.snapshot()
        self.assertEqual(sorted(snapshot.nodes), ["alice", "jesse"])
        self.assertEqual(snapshot.nodes["jesse"]["attributes"]["name"], ["Jesse", "Jess"])
        self.assertEqual((snapshot.edges["edgeb"]["from_id"], snapshot.edges["edgeb"]["summary"]),
                         ("jesse", "Merged relationship"))
        self.assertEqual({(change["kind"], change["id"]): change["operation"] for change in snapshot.event("edit1")["changes"]},
                         {("node", "jesse"): "edit", ("edge", "edgeb"): "edit", ("node", "jess1"): "remove"})
        self.assertEqual(receipt["result_counts"]["node"], {"add": 0, "edit": 1, "remove": 1})

    def test_edit_source_is_add_only_and_the_reserved_prefix_is_not_public(self):
        for operation, content in (("add", b"Ordinary text."), ("edit", b"Changed."), ("remove", None)):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ContextError, "reserved"):
                    self.context.begin(operation, "foundation/edits/abcde.txt", content=content,
                                       metadata={"kind": "note", "title": "t", "origin": "o"}, submitted_by="owner")
                with self.assertRaisesRegex(ContextError, "reserved"):
                    self.pipeline.submit(operation, "foundation/edits/abcde.txt", content=content,
                                         metadata={"kind": "note", "title": "t", "origin": "o"}, submitted_by="owner")
        self.submit(plan(self.base, remove("edge", "edgeb")))
        for operation, content in (("edit", b"Changed."), ("remove", None)):
            with self.assertRaisesRegex(ContextError, "reserved"):
                self.context.begin(operation, "foundation/edits/edit1.txt", content=content, submitted_by="owner")
        self.assertUntouched(self.context.git.head())


class Rejection(GraphEditCase):
    def test_invalid_plans_are_refused_before_staging_and_publish_nothing(self):
        def good():
            return plan(self.base, put("node", person("bob12", "Bob"), citations=[self.base]))

        def with_op(operation):
            return plan(self.base, operation)

        def mutate(**changes):
            value = good()
            value.update(changes)
            return value

        extra_field = good()
        extra_field["operations"][0]["developments"] = []
        old_vocabulary = good()
        old_vocabulary["operations"][0]["home_ref"] = "bob12"
        unknown = {"hello": "world"}
        record_without_id = person("bob12", "Bob")
        del record_without_id["id"]
        no_type = person("bob12", "Bob")
        no_type["attributes"] = {"name": ["Bob"]}
        many = [self.base] + ["%040x" % number for number in range(1, 17)]
        cases = {
            "unknown top-level field": mutate(home_brief_patches=[]),
            "missing rationale": {key: value for key, value in good().items() if key != "rationale"},
            "blank rationale": mutate(rationale="  "),
            "unsupported format": mutate(format="qorqut-graph-edit-v2"),
            "empty operations": mutate(operations=[]),
            "operations not a list": mutate(operations={"op": "put_node"}),
            "operation not an object": mutate(operations=["put_node"]),
            "branch name base": mutate(expected_revision="main"),
            "self base": mutate(expected_revision="self"),
            "unsupported verb": with_op({"op": "merge_nodes", "from": "jess1", "into": "jesse"}),
            "unhashable verb": with_op({"op": ["put_node"]}),
            "development on a put": extra_field,
            "implicit home_ref": old_vocabulary,
            "unknown verb field": with_op(dict(remove("node", "jess1"), cascade=True)),
            "put without a record id": with_op(put("node", record_without_id, citations=[self.base])),
            "record with an unknown field": with_op(put("node", dict(person("bob12", "Bob"), home_brief="x"))),
            "node without a category": with_op(put("node", no_type)),
            "duplicate object": plan(self.base, remove("edge", "edgeb"), remove("edge", "edgeb")),
            "one ID in two kinds": plan(self.base, put("node", person("bob12", "Bob")),
                                        put("edge", link("bob12", "jesse", "alice"))),
            "removal of an absent object": with_op(remove("node", "zzzzz")),
            "removal with the wrong kind": with_op(remove("edge", "jesse")),
            "creation reusing the event ID": with_op(put("node", person("edit1", "Bob"))),
            "creation reusing a published ID": with_op(put("node", person("edgea", "Bob"))),
            "edge to an absent node": with_op(put("edge", link("edgen", "alice", "nobod"))),
            "node removal without its edges": with_op(remove("node", "jess1")),
            "no support": with_op(put("node", person("bob12", "Bob"), support=[])),
            "unpublished citation": with_op(put("node", person("bob12", "Bob"), citations=["a" * 40])),
            "unpublished support": with_op(put("node", person("bob12", "Bob"), support=["b" * 40])),
            "unchanged put": with_op(put("node", self.context.snapshot().nodes["jesse"], support=[self.base])),
            "blank explanation": with_op(put("node", person("bob12", "Bob"), explanation=" ")),
            "no citations": with_op(put("node", person("bob12", "Bob"), citations=[])),
            "too many citations": with_op(put("node", person("bob12", "Bob"), citations=many)),
            "citation by branch name": with_op(put("node", person("bob12", "Bob"), citations=["main"])),
            "non-JSON value": mutate(rationale=object()),
            "non-plan": unknown,
        }
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(ContextError):
                self.submit(value)
            self.assertUntouched()

    def test_stale_base_is_refused_and_never_rebased(self):
        stale = plan(self.base, put("node", person("bob12", "Bob"), citations=[self.base]))
        head = self.advance()
        with self.assertRaisesRegex(Conflict, "Stale expected revision"):
            self.submit(stale)
        self.assertUntouched(head)
        self.assertNotIn("bob12", self.context.snapshot().nodes)

    def test_the_source_byte_limit_bounds_the_canonical_envelope_only(self):
        value = plan(self.base, put("node", person("bob12", "Bob"), citations=[self.base]))
        size = len(graph_edits.canonical(graph_edits.normalize(value)))
        with self.assertRaisesRegex(ContextError, "byte limit"):
            self.context.begin_edit(value, submitted_by="host-owner", event_id="edit1", source_bytes=size - 1)
        self.assertUntouched()
        self.assertEqual(self.context.begin_edit(value, submitted_by="host-owner", event_id="edit1",
                                                 source_bytes=size).event_id, "edit1")
        self.assertEqual(len(json.loads((self.context.runtime / "events/edit1/input.bin").read_bytes())["operations"]), 1)
        self.context.discard("edit1")

    def test_caller_attribution_is_validated(self):
        value = plan(self.base, remove("edge", "edgeb"))
        for label, kwargs in {"event ID": {"event_id": "ab"}, "submitter": {"submitted_by": " "},
                              "record context": {"record_context": {"scope": {"kind": "global"}}}}.items():
            with self.subTest(label), self.assertRaises(ContextError):
                self.pipeline.submit_edit(value, **{"submitted_by": "host-owner", "event_id": "edit1", **kwargs})
            self.assertUntouched()
        with self.assertRaises(TypeError):
            self.pipeline.submit_edit(value, submitted_by="host-owner")


class Recovery(GraphEditCase):
    def test_submit_retry_and_recover_never_start_a_model(self):
        receipt = self.submit(self.three())
        self.assertEqual(self.pipeline.retry("edit1")["published_commit"], receipt["published_commit"])
        self.assertEqual(self.pipeline.recover(), {"status": "idle", "revision": receipt["published_commit"]})
        self.assertEqual(self.pipeline.pending(), {"pending": None, "attempt": None, "diagnostics": None})
        self.assertEqual(self.runner.calls, [])
        self.assertFalse((self.context.runtime / "runs").exists())

    def test_interruption_after_partial_staging_is_retained_for_a_retry_of_the_same_plan(self):
        pending = self.interrupted()
        marker = graph_edits.marker(graph_edits.normalize(self.three()))
        self.assertEqual(pending["execution"], marker)
        self.assertEqual(self.context.inspect_draft("edit1")["execution"], marker)
        self.assertEqual(list(pending["draft"]), ["node:bob12"])
        # Recovery is mechanical: it neither executes the plan nor starts a model.
        recovered = self.pipeline.recover()
        self.assertEqual((recovered["status"], recovered["event_id"]), ("needs_retry", "edit1"))
        self.assertEqual(len(self.context.pending()["draft"]), 1)
        self.assertEqual(self.context.git.head(), self.base)
        with self.assertRaisesRegex(Conflict, "pending event"):
            self.submit(plan(self.base, remove("edge", "edgeb")))
        self.assertEqual(self.pipeline.pending()["attempt"], None)
        receipt = Pipeline(self.context, runner=self.runner).retry("edit1")
        self.assertEqual((receipt["execution"], receipt["model"], receipt["diagnostics"], receipt["recovered"]),
                         (graph_edits.marker(graph_edits.normalize(self.three())), None, None, False))
        snapshot = self.context.snapshot()
        self.assertEqual((sorted(snapshot.nodes), sorted(snapshot.edges)),
                         (["alice", "bob12", "jess1", "jesse"], ["edgea", "edgen"]))
        self.assertEqual(len(snapshot.event("edit1")["changes"]), 3)
        self.assertEqual(snapshot.support[("edge", "edgen")], [receipt["published_commit"]])
        self.assertIsNone(self.context.pending())
        self.assertEqual(self.runner.calls, [])

    def test_resubmitting_the_same_plan_resumes_the_pending_event(self):
        self.interrupted()
        receipt = self.submit(self.three())
        self.assertEqual(self.context.snapshot().event_commits["edit1"], receipt["published_commit"])
        self.assertEqual(len(self.context.snapshot().event("edit1")["changes"]), 3)
        self.assertIsNone(self.context.pending())

    def test_a_staging_failure_is_reported_as_deterministic_work_with_no_diagnostics(self):
        with patch.object(self.context, "put_node", side_effect=ContextError("storage refused")):
            with self.assertRaises(PropagationError) as caught:
                self.submit(self.three())
        self.assertEqual((caught.exception.phase, caught.exception.diagnostics), ("edit_staging", None))
        self.assertEqual(self.context.pending()["id"], "edit1")
        self.assertFalse((self.context.runtime / "runs").exists())

    def test_interruption_after_publication_returns_the_original_commit_without_a_second_event(self):
        events = len(self.context.snapshot().events)

        def stop(phase):
            if phase == "after_publication":
                raise ProcessStopped()
        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.submit(self.three())
        published = self.context.git.head()
        self.assertNotEqual(published, self.base)
        self.assertEqual(self.context.pending()["id"], "edit1")
        self.assertEqual(self.pipeline.recover(), {"status": "completed", "commit": published, "event_id": "edit1"})
        self.assertIsNone(self.context.pending())
        self.assertEqual((self.context.git.head(), len(self.context.snapshot().events)), (published, events + 1))
        again = self.submit(self.three())
        self.assertEqual((again["published_commit"], again["recovered"]), (published, True))
        self.assertEqual(self.pipeline.retry("edit1")["published_commit"], published)
        self.assertEqual(self.context.git.head(), published)
        self.assertEqual([hit.id for hit in self.context.search("Bob") if hit.kind == "node"], ["bob12"])
        self.assertEqual(self.runner.calls, [])

    def test_publication_completed_by_a_retry_after_losing_the_process(self):
        def stop(phase):
            if phase == "after_publication":
                raise ProcessStopped()
        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.submit(self.three())
        published = self.context.git.head()
        self.assertEqual(self.pipeline.retry("edit1")["published_commit"], published)
        self.assertIsNone(self.context.pending())


class Tampering(GraphEditCase):
    @contextmanager
    def altered(self, change):
        """Rewrite the pending job record for the duration of the block, then restore its exact bytes."""
        original = self.context.job_path.read_bytes()
        job = json.loads(original)
        change(job)
        self.context.job_path.write_bytes(encode(job))
        try:
            yield
        finally:
            self.context.job_path.write_bytes(original)

    def refused(self, event_id="edit1"):
        """Neither the pipeline nor lower-level publication may publish the altered pending event."""
        with self.assertRaises(ContextError):
            self.pipeline.retry(event_id)
        with self.assertRaises(ContextError):
            self.context.publish(event_id)
        self.assertEqual(self.context.git.head(), self.base)
        self.assertEqual(self.runner.calls, [])

    def test_extra_draft_content_is_refused_by_retry_and_lower_level_publication(self):
        self.interrupted()
        self.context.put_node("edit1", person("extra", "Extra"), explanation="Not in the plan.",
                              citations=["self"], support_after=["self"])
        with self.assertRaises(PropagationError) as caught:
            self.pipeline.retry("edit1")
        self.assertEqual(caught.exception.phase, "edit_staging")
        self.assertIsInstance(caught.exception.__cause__, Conflict)
        with self.assertRaises(Conflict):
            self.context.publish("edit1")
        self.assertEqual(sorted(self.context.pending()["draft"]), ["node:bob12", "node:extra"])
        self.assertEqual(self.context.git.head(), self.base)
        self.context.remove_node("edit1", "extra", explanation="Back to the plan.", citations=["self"])
        self.assertEqual(self.pipeline.retry("edit1")["status"], "published")
        self.assertNotIn("extra", self.context.snapshot().nodes)

    def test_an_incomplete_draft_cannot_be_published_below_the_pipeline(self):
        self.interrupted()
        with self.assertRaisesRegex(Conflict, "not exactly the bound graph edit"):
            self.context.publish("edit1")
        with self.assertRaisesRegex(Conflict, "not exactly the bound graph edit"):
            self.context.inspect_draft("edit1", validate=True)

    def test_altered_staged_records_and_support_are_refused(self):
        self.interrupted()
        for label, change in {
                "record": lambda job: job["draft"]["node:bob12"]["record"].update(summary="Robert"),
                "support": lambda job: job["draft"]["node:bob12"].update(support_after=[self.base]),
                "citations": lambda job: job["draft"]["node:bob12"].update(citations=["self"]),
                "development": lambda job: job["draft"]["node:bob12"].update(
                    developments=[{"date": 0, "description": "Added", "citations": ["self"]}])}.items():
            with self.subTest(label), self.altered(change):
                with self.assertRaises(PropagationError):
                    self.pipeline.retry("edit1")
                with self.assertRaises(ContextError):
                    self.context.publish("edit1")
                self.assertEqual(self.context.git.head(), self.base)
        self.assertEqual(self.pipeline.retry("edit1")["status"], "published")

    def test_a_foreign_handoff_note_cannot_be_published_but_retry_restores_the_empty_note(self):
        self.interrupted()
        for operation in graph_edits.normalize(self.three())["operations"][1:]:
            graph_edits.stage(self.context, "edit1", operation)
        self.context.stage_handoff_note("edit1", "Please check this with the owner.")
        with self.assertRaisesRegex(Conflict, "not exactly the bound graph edit"):
            self.context.publish("edit1")
        self.pipeline.retry("edit1")
        self.assertEqual(self.context.snapshot().event("edit1")["handoff_note"], {"disposition": "empty", "text": ""})

    def test_altered_source_bytes_or_marker_are_refused(self):
        self.interrupted()
        sealed = self.context.inspect_draft("edit1")["digest"]
        original = (self.context.runtime / "events/edit1/input.bin").read_bytes()
        other = graph_edits.canonical(graph_edits.normalize(dict(self.three(), rationale="A different rationale.")))
        (self.context.runtime / "events/edit1/input.bin").write_bytes(other)
        self.refused()
        (self.context.runtime / "events/edit1/input.bin").write_bytes(original)
        flipped = "0" * 64
        with self.altered(lambda job: job["execution"].update(plan_digest=flipped)):
            # The marker is part of what the draft digest seals.
            self.assertNotEqual(self.context.inspect_draft("edit1")["digest"], sealed)
            self.refused()
        with self.altered(lambda job: job["execution"].update(kind="model_run")):
            self.refused()
        with self.altered(lambda job: job["execution"].update(extra=True)):
            self.refused()
        with self.altered(lambda job: job.update(execution=None)):
            self.refused()
        self.assertEqual(self.pipeline.retry("edit1")["status"], "published")

    def test_a_marker_is_required_for_the_reserved_source_and_cannot_be_forged_for_ordinary_text(self):
        self.interrupted()
        with self.altered(lambda job: job.pop("execution")):
            self.refused()
        marker = self.context.pending()["execution"]
        self.pipeline.discard("edit1")
        ordinary = self.source("plain.md", graph_edits.canonical(graph_edits.normalize(self.three())))
        with self.altered(lambda job: job.update(execution=marker)):
            self.refused(ordinary.event_id)
            with self.assertRaisesRegex(ContextError, "not bound"):
                self.context.pending()
        self.assertEqual(self.context.pending()["id"], ordinary.event_id)
        self.pipeline.discard(ordinary.event_id)
        self.assertUntouched()

    def test_a_base_that_moved_while_pending_is_retained_never_rebased(self):
        self.interrupted()
        # An out-of-band base change must not rebase the retained plan.
        head = self.context.git.commit({"graph/note.json": b"{}\n"}, self.context.runtime, self.base, "moved")
        self.context.git.run("update-ref", "refs/heads/main", head, self.base)
        with self.assertRaisesRegex(ContextError, "base changed"):
            self.pipeline.retry("edit1")
        self.assertEqual(self.context.git.head(), head)
        self.assertEqual(self.context.pending()["id"], "edit1")


class Replay(GraphEditCase):
    def test_exact_replay_after_another_event_returns_the_original_result(self):
        value = self.three()
        first = self.submit(value, record_context=NORTH)
        head = self.advance()
        events = len(self.context.snapshot().events)
        again = self.submit(value, record_context=NORTH)
        self.assertEqual({**again, "recovered": False}, first)
        self.assertTrue(again["recovered"])
        self.assertEqual((self.context.git.head(), len(self.context.snapshot().events)), (head, events))
        self.assertEqual(self.pipeline.retry("edit1")["published_commit"], first["published_commit"])
        self.assertEqual(self.runner.calls, [])

    def test_changed_plan_attribution_context_or_base_under_the_same_event_is_a_conflict(self):
        value = self.three()
        self.submit(value, record_context=NORTH)
        head = self.advance()
        variants = {
            "rationale": (dict(value, rationale="Something else."), {"record_context": NORTH}),
            "operation": (plan(self.base, remove("edge", "edgeb")), {"record_context": NORTH}),
            "base": (dict(value, expected_revision=head), {"record_context": NORTH}),
            "submitter": (value, {"record_context": NORTH, "submitted_by": "someone-else"}),
            "other context": (value, {"record_context": SOUTH}),
            "removed context": (value, {}),
        }
        for label, (changed, kwargs) in variants.items():
            with self.subTest(label), self.assertRaisesRegex(Conflict, "cannot be reused with different input"):
                self.pipeline.submit_edit(changed, **{"submitted_by": "host-owner", "event_id": "edit1", **kwargs})
        self.assertEqual(self.context.git.head(), head)
        self.assertUntouched(head)

    def test_a_pending_event_accepts_only_its_exact_plan(self):
        self.interrupted()
        with self.assertRaisesRegex(Conflict, "Complete, retry, or discard"):
            self.submit(dict(self.three(), rationale="Something else."))
        with self.assertRaisesRegex(Conflict, "Complete, retry, or discard"):
            self.submit(self.three(), record_context=NORTH)
        self.assertEqual(self.context.pending()["id"], "edit1")
        self.assertEqual(self.submit(self.three())["status"], "published")


class OrdinaryInput(ContextCase):
    def test_general_text_containing_a_plan_never_selects_deterministic_execution(self):
        event = self.source("one.md")
        self.node(event, "jesse", summary="Jesse")
        base = self.context.publish(event.event_id)
        value = plan(base, put("node", person("bob12", "Bob"), citations=[base]))
        runner = ScriptRunner(lambda tools, payload: (new_node(tools, "Model made"), finish(tools)))
        pipeline = Pipeline(self.context, runner=runner)
        for path in ("foundation/plan.md", "foundation/edits.txt", "foundation/other/edits/plan.txt"):
            with self.subTest(path):
                receipt = pipeline.submit("add", path, content=graph_edits.canonical(graph_edits.normalize(value)),
                                          metadata={"kind": "graph_edit", "title": "Explicit graph edit",
                                                    "origin": "controlled_caller"}, submitted_by="owner")
                self.assertNotIn("execution", receipt)
                self.assertNotIn("execution", self.context.snapshot().event(receipt["event_id"]))
                self.assertIn("propagation", runner.calls)
                self.assertTrue((self.context.runtime / "runs" / receipt["event_id"]).exists())
        self.assertNotIn("bob12", self.context.snapshot().nodes)
        self.assertEqual(len(self.context.snapshot().nodes), 4)

    def test_ordinary_submit_receipts_and_events_are_unchanged_by_the_marker(self):
        runner = ScriptRunner(lambda tools, payload: (new_node(tools), finish(tools)))
        receipt = Pipeline(self.context, runner=runner).submit(
            "add", "foundation/one.md", content=b"Jesse appears here.",
            metadata={"kind": "note", "title": "one", "origin": "test"}, submitted_by="owner")
        self.assertEqual(set(receipt), {"status", "event_id", "source_id", "base_revision", "published_commit", "input_mode",
                                        "handoff_disposition", "result_counts", "model", "diagnostics", "recovered",
                                        "model_execution"})
        self.assertNotIn("execution", self.context.snapshot().event(receipt["event_id"]))
        self.assertIsNotNone(receipt["model"])


class PublishedReaders(GraphEditCase):
    def setUp(self):
        super().setUp()
        self.submit(self.three())

    def clone(self, event_id, change=lambda event: None, path=None):
        """A crafted, unpublished commit that adds a copy of the published edit event under another ID."""
        head = self.context.git.head()
        files = self.context.git.files(head)
        event = json.loads(files["graph/events/edit1.json"])
        event.update(id=event_id)
        event["source"] = dict(event["source"], path=path or graph_edits.source_path(event_id))
        change(event)
        added = {f"graph/events/{event_id}.json": encode(event),
                 event["source"]["path"]: files["foundation/edits/edit1.txt"]}
        return self.context.git.commit(added, self.context.runtime, head, "crafted")

    def test_the_untouched_clone_is_a_valid_snapshot_and_every_inconsistency_is_refused(self):
        self.assertIn("edit2", Snapshot(self.context.git, self.clone("edit2")).events)
        cases = {
            "reserved source without a marker": dict(event_id="edit3", change=lambda event: event.pop("execution")),
            "marker on an ordinary path": dict(event_id="edit4", path="foundation/ordinary.txt"),
            "marker for other bytes": dict(event_id="edit5", change=lambda event: event["execution"].update(plan_digest="1" * 64)),
            "malformed marker": dict(event_id="edit6", change=lambda event: event["execution"].update(kind="model_run")),
            "marker on a different base": dict(event_id="edit7", change=lambda event: event.update(base_revision="2" * 40)),
            "altered attribution": dict(event_id="edit8", change=lambda event: event["changes"][0].update(explanation="Altered.")),
            "missing change": dict(event_id="edit9", change=lambda event: event["changes"].pop()),
            "recorded note": dict(event_id="editx", change=lambda event: event.update(
                handoff_note={"disposition": "prepared", "text": "x"})),
            "action mode": dict(event_id="edity", change=lambda event: event.update(input_mode="OPERATIONAL_ACTION")),
        }
        for label, arguments in cases.items():
            with self.subTest(label), self.assertRaises(ContextError):
                Snapshot(self.context.git, self.clone(**arguments))
