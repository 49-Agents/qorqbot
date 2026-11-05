"""Supervised edits: a preserved supporting document plus explicit node/edge/note operations, never a model."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from qorqut_context import Conflict, ContextError, graph_edits, supervised_edits
from qorqut_context.__main__ import main
from qorqut_context.git_store import Snapshot
from qorqut_context.pipeline import Pipeline, PropagationError
from qorqut_context.schema import encode
from qorqut_context.tools import EventTools
from .helpers import ContextCase
from .test_graph_edits import NORTH, SOUTH, NoModel, link, person, put, remove
from .test_recovery import ProcessStopped

DOCUMENT = "# Owner correction\r\n\r\nBob joined on a Tuesday — Jesse confirmed it.\r\n\r\n".encode()


def plan(base, *operations, rationale="Record the owner's correction."):
    return {"format": supervised_edits.FORMAT, "expected_revision": base, "rationale": rationale,
            "operations": list(operations)}


def note(note_id, text, objects=(), *, citations=("self",), support=("self",), explanation="The owner states this."):
    record = {"id": note_id, "text": text, "objects": [{"kind": kind, "id": target} for kind, target in objects]}
    return put("note", record, citations=citations, support=support, explanation=explanation)


def crafted_clone(context, event_id, change=lambda event, files: None, path=None):
    """A crafted, unpublished commit adding a copy of the published ``sup01`` supervised event under another ID."""
    head = context.git.head()
    files = context.git.files(head)
    event = json.loads(files["graph/events/sup01.json"])
    event.update(id=event_id)
    event["source"] = dict(event["source"], path=path or supervised_edits.source_path(event_id),
                           title=f"Supervised edit {event_id}")
    added = {f"graph/events/{event_id}.json": encode(event),
             event["source"]["path"]: files["foundation/supervised-edits/sup01.md"]}
    change(event, added)
    added[f"graph/events/{event_id}.json"] = encode(event)
    return context.git.commit(added, context.runtime, head, "crafted")


def tree(*roots):
    """Every file under the roots with its bytes, to prove a call changed nothing."""
    return {str(path): path.read_bytes() for root in roots for path in sorted(Path(root).rglob("*")) if path.is_file()}


class SupervisedCase(ContextCase):
    """A published base (people, two edges, one note) and a pipeline that fails the test if a model is reached."""

    def setUp(self):
        super().setUp()
        event = self.source("one.md", b"Jesse met Alice.")
        self.node(event, "jesse", summary="Jesse")
        self.node(event, "alice", summary="Alice")
        self.node(event, "jess1", summary="Jess")
        self.edge(event, "edgea", "jesse", "alice")
        self.edge(event, "edgeb", "jess1", "alice")
        self.note(event, "Jesse reports meeting Alice; the date is not anchored.", [("node", "jesse"), ("edge", "edgea")],
                  note_id="notea")
        self.base = self.context.publish(event.event_id)
        self.runner = NoModel()
        self.pipeline = Pipeline(self.context, runner=self.runner)
        for target in ("qorqut_context.pipeline.bootstrap.prepare", "qorqut_context.pipeline.usage.AttemptCalls"):
            forbidden = patch(target, side_effect=AssertionError("model machinery reached"))
            forbidden.start()
            self.addCleanup(forbidden.stop)

    def mixed(self, **options):
        return plan(self.base,
                    put("node", person("bob12", "Bob")),
                    put("edge", link("edgen", "alice", "bob12")),
                    note("noteb", "Bob reportedly joined on a Tuesday; the owner has not confirmed the date.",
                         [("node", "bob12"), ("edge", "edgen")]),
                    note("notea", "Jesse met Alice on a Tuesday.", [("node", "jesse"), ("node", "alice"), ("edge", "edgea")],
                         citations=["self", self.base], support=[self.base, "self"]),
                    remove("edge", "edgeb", citations=[self.base]),
                    remove("node", "jess1", citations=[self.base]), **options)

    def submit(self, value=None, event_id="sup01", content=DOCUMENT, **kwargs):
        return self.pipeline.submit_supervised(value or self.mixed(), content=content, submitted_by="host-owner",
                                               event_id=event_id, **kwargs)

    def preview(self, value=None, event_id="sup01", content=DOCUMENT, **kwargs):
        return self.context.preview_supervised(value or self.mixed(), content=content, submitted_by="host-owner",
                                               event_id=event_id, **kwargs)

    def advance(self):
        other = self.source("two.md", b"An unrelated later source.")
        self.node(other, "later")
        return self.context.publish(other.event_id)

    def assertUntouched(self, head=None):
        self.assertIsNone(self.context.pending())
        self.assertEqual(self.context.git.head(), head or self.base)
        events = self.context.runtime / "events"
        self.assertEqual(list(events.iterdir()) if events.exists() else [], [])
        self.assertFalse((self.context.runtime / "runs").exists())
        self.assertEqual(self.runner.calls, [])

    def stop_after(self, phase, count=1):
        seen = []

        def stop(name):
            if name == phase:
                seen.append(name)
                if len(seen) == count:
                    raise ProcessStopped()
        return patch.object(self.pipeline, "_checkpoint", side_effect=stop)

    def interrupted(self, count=1, event_id="sup01"):
        with self.stop_after("edit_staged", count), self.assertRaises(ProcessStopped):
            self.submit(event_id=event_id)
        pending = self.context.pending()
        self.assertEqual((pending["id"], len(pending["draft"])), (event_id, count))
        self.assertEqual(self.context.git.head(), self.base)
        return pending


class Preview(SupervisedCase):
    def test_preview_is_pure_and_describes_the_exact_batch(self):
        before = tree(self.context.root, self.context.runtime)
        result = self.preview(record_context=NORTH)
        self.assertEqual(tree(self.context.root, self.context.runtime), before)
        self.assertUntouched()
        normalized = supervised_edits.normalize(self.mixed())
        self.assertEqual((result["revision"], result["plan"], result["document_digest"]),
                         (self.base, normalized, hashlib.sha256(DOCUMENT).hexdigest()))
        self.assertEqual((result["status"], result["event_id"], result["source_path"]),
                         ("ready", "sup01", "foundation/supervised-edits/sup01.md"))
        self.assertEqual(result["plan_digest"], supervised_edits.marker(normalized, DOCUMENT)["plan_digest"])
        self.assertEqual(result["document_bytes"], len(DOCUMENT))
        self.assertTrue(result["cites_supporting_document"])
        self.assertEqual(result["result_counts"], {"node": {"add": 1, "edit": 0, "remove": 1},
                                                   "edge": {"add": 1, "edit": 0, "remove": 1},
                                                   "note": {"add": 1, "edit": 1, "remove": 0}})
        by_id = {item["id"]: item for item in result["operations"]}
        self.assertEqual([(item["op"], item["operation"]) for item in result["operations"]],
                         [("put_node", "add"), ("put_edge", "add"), ("put_note", "add"), ("put_note", "edit"),
                          ("remove_edge", "remove"), ("remove_node", "remove")])
        self.assertIsNone(by_id["noteb"]["before"])
        self.assertEqual(by_id["notea"]["before"]["text"], "Jesse reports meeting Alice; the date is not anchored.")
        self.assertEqual(by_id["notea"]["after"]["text"], "Jesse met Alice on a Tuesday.")
        self.assertEqual((by_id["notea"]["support_before"], by_id["notea"]["support_after"]), ([self.base], [self.base, "self"]))
        self.assertIsNone(by_id["edgeb"]["after"])
        self.assertEqual(self.preview(record_context=NORTH)["submission_digest"], result["submission_digest"])
        self.assertNotEqual(self.preview(record_context=SOUTH)["submission_digest"], result["submission_digest"])
        self.assertNotEqual(self.preview(content=DOCUMENT + b"x", record_context=NORTH)["submission_digest"],
                            result["submission_digest"])

    def test_preview_reports_stale_state_as_a_conflict_and_bad_input_as_a_context_error(self):
        self.advance()
        with self.assertRaisesRegex(Conflict, "Stale expected revision"):
            self.preview()
        with self.assertRaisesRegex(Conflict, "Stale expected revision"):
            self.submit()
        self.assertUntouched(self.context.git.head())
        head = self.context.git.head()
        fresh = self.mixed()
        fresh["expected_revision"] = head
        self.assertEqual(self.preview(fresh)["revision"], head)

    def test_invalid_plans_documents_and_notes_are_refused_without_effect(self):
        good = self.mixed()

        def altered(change):
            value = deepcopy(good)
            change(value)
            return value
        cases = {
            "unknown plan field": altered(lambda v: v.update(extra=1)),
            "missing rationale": altered(lambda v: v.pop("rationale")),
            "graph edit format": altered(lambda v: v.update(format=graph_edits.FORMAT)),
            "empty operations": altered(lambda v: v.update(operations=[])),
            "short revision": altered(lambda v: v.update(expected_revision="abc")),
            "unknown verb": altered(lambda v: v["operations"][0].update(op="merge_node")),
            "extra operation field": altered(lambda v: v["operations"][0].update(developments=[])),
            "duplicate id": altered(lambda v: v["operations"].append(deepcopy(v["operations"][0]))),
            "note with attributes": altered(lambda v: v["operations"][2]["record"].update(attributes={})),
            "note without text": altered(lambda v: v["operations"][2]["record"].update(text="  ")),
            "note id missing": altered(lambda v: v["operations"][2]["record"].pop("id")),
            "invented link": altered(lambda v: v["operations"][2]["record"].update(objects=[{"kind": "node", "id": "zzzzz"}])),
            "wrong kind link": altered(lambda v: v["operations"][2]["record"].update(objects=[{"kind": "edge", "id": "jesse"}])),
            "note citation": altered(lambda v: v["operations"][2].update(citations=["notea"])),
            "unpublished citation": altered(lambda v: v["operations"][2].update(citations=["f" * 40])),
            "note without support": altered(lambda v: v["operations"][2].update(support_after=[])),
            "removing a note that is not there": altered(lambda v: v["operations"].append(remove("note", "zzzzz"))),
            "orphaned endpoint": altered(lambda v: v["operations"].pop(4)),
        }
        before = tree(self.context.root, self.context.runtime)
        for label, value in cases.items():
            with self.subTest(label), self.assertRaises(ContextError):
                self.preview(value)
        for label, content in {"empty": b"", "blank": b" \r\n", "binary": b"\xff\xfe", "text type": "text"}.items():
            with self.subTest(label), self.assertRaises(ContextError):
                self.preview(content=content)
        with self.assertRaisesRegex(ContextError, "byte limit"):
            self.context.preview_supervised(good, content=DOCUMENT, submitted_by="o", event_id="sup01", source_bytes=len(DOCUMENT) - 1)
        for event_id in ("SUP01", "toolong"):
            with self.subTest(event_id), self.assertRaises(ContextError):
                self.preview(event_id=event_id)
        with self.assertRaises(ContextError):
            self.preview(record_context={"scope": "business"})
        self.assertEqual(tree(self.context.root, self.context.runtime), before)
        self.assertUntouched()

    def test_an_operation_that_changes_nothing_is_refused_naming_the_right_edit_kind(self):
        unchanged = note("notea", "Jesse reports meeting Alice; the date is not anchored.",
                         [("node", "jesse"), ("edge", "edgea")], citations=["self"], support=[self.base])
        for attempt in (lambda: self.preview(plan(self.base, unchanged)),
                        lambda: self.submit(plan(self.base, unchanged))):
            with self.assertRaisesRegex(ContextError, "Every supervised edit operation must change the context"):
                attempt()
        self.assertUntouched()
        legacy = {"format": graph_edits.FORMAT, "expected_revision": self.base, "rationale": "Legacy.",
                  "operations": [put("node", person("jesse", "Jesse"), citations=["self"], support=[self.base])]}
        with self.assertRaisesRegex(ContextError, "Every graph edit operation must change the context"):
            self.pipeline.submit_edit(legacy, submitted_by="host-owner", event_id="edit1")
        self.assertUntouched()

    def test_preview_of_published_and_pending_events_reports_their_state(self):
        interrupted = self.interrupted()
        status = self.preview()
        self.assertEqual(status["status"], "pending")
        self.assertEqual(self.context.pending()["id"], interrupted["id"])
        with self.assertRaisesRegex(Conflict, "pending"):
            self.preview(event_id="sup02")
        with self.assertRaisesRegex(Conflict, "pending"):
            self.preview(content=DOCUMENT + b"more")
        receipt = self.pipeline.retry("sup01")
        head = self.advance()
        done = self.preview()
        self.assertEqual((done["status"], done["published_commit"]), ("published", receipt["published_commit"]))
        self.assertEqual(done["revision"], head)
        with self.assertRaisesRegex(Conflict, "published event ID cannot be reused"):
            self.preview(content=DOCUMENT + b"more")

    def test_a_preview_digest_is_informative_and_never_an_approval(self):
        previewed = self.preview()["submission_digest"]
        receipt = self.submit(content=DOCUMENT + b"Extra.")
        self.assertEqual(receipt["status"], "published")
        self.assertNotEqual(self.context.snapshot().event("sup01")["submission_digest"], previewed)
        with self.assertRaises(TypeError):
            self.pipeline.submit_supervised(self.mixed(), content=DOCUMENT, submitted_by="host-owner",
                                            event_id="sup02", approved_digest=previewed)
        with self.assertRaises(TypeError):
            self.context.begin_supervised(self.mixed(), content=DOCUMENT, submitted_by="host-owner",
                                          event_id="sup02", approved_digest=previewed)


class Publication(SupervisedCase):
    def test_the_batch_publishes_atomically_with_the_exact_document_and_honest_provenance(self):
        value = self.mixed()
        receipt = self.submit(value, record_context=NORTH)
        commit = receipt["published_commit"]
        normalized = supervised_edits.normalize(value)
        self.assertEqual((receipt["status"], receipt["event_id"], receipt["base_revision"], receipt["input_mode"]),
                         ("published", "sup01", self.base, "GENERAL"))
        self.assertEqual((receipt["model"], receipt["diagnostics"], receipt["recovered"]), (None, None, False))
        self.assertEqual(receipt["execution"], supervised_edits.marker(normalized, DOCUMENT))
        self.assertEqual(receipt["execution"]["plan"], normalized)
        self.assertEqual(receipt["execution"]["document_digest"], hashlib.sha256(DOCUMENT).hexdigest())
        self.assertNotIn("model_execution", receipt)
        self.assertEqual(receipt["result_counts"], {"node": {"add": 1, "edit": 0, "remove": 1},
                                                   "edge": {"add": 1, "edit": 0, "remove": 1},
                                                   "note": {"add": 1, "edit": 1, "remove": 0}})
        self.assertEqual(self.runner.calls, [])
        self.assertFalse((self.context.runtime / "runs").exists())
        self.assertEqual(self.context.git.run("rev-list", "--count", f"{self.base}..{commit}").decode().strip(), "1")
        snapshot = self.context.snapshot()
        self.assertEqual(sorted(snapshot.nodes), ["alice", "bob12", "jesse"])
        self.assertEqual(sorted(snapshot.edges), ["edgea", "edgen"])
        self.assertEqual(sorted(snapshot.notes), ["notea", "noteb"])
        self.assertEqual(snapshot.notes["notea"]["objects"], [{"kind": "node", "id": "jesse"},
                                                              {"kind": "node", "id": "alice"}, {"kind": "edge", "id": "edgea"}])
        self.assertEqual(snapshot.read("note", "notea")["support"], [self.base, commit])
        self.assertEqual(snapshot.read("note", "noteb")["support"], [commit])
        path = "foundation/supervised-edits/sup01.md"
        self.assertEqual(snapshot.files[path], DOCUMENT)
        event = snapshot.event("sup01")
        self.assertEqual(event["source"]["path"], path)
        self.assertEqual({key: event["source"][key] for key in ("kind", "title", "origin")},
                         {"kind": "supervised_edit", "title": "Supervised edit sup01", "origin": "controlled_caller"})
        self.assertEqual(event["execution"], receipt["execution"])
        self.assertEqual(event["input_digest"], hashlib.sha256(DOCUMENT).hexdigest())
        self.assertEqual((event["handoff_note"], event["input_mode"], event["operation"]),
                         ({"disposition": "empty", "text": ""}, "GENERAL", "add"))
        self.assertEqual(event["record_context"], NORTH)
        self.assertEqual({(c["kind"], c["id"], c["operation"]) for c in event["changes"]},
                         {("node", "bob12", "add"), ("node", "jess1", "remove"), ("edge", "edgen", "add"),
                          ("edge", "edgeb", "remove"), ("note", "noteb", "add"), ("note", "notea", "edit")})
        self.assertNotIn("action", event)
        self.assertEqual(snapshot.actions_history()["total"], 0)

    def test_ordinary_reads_search_and_tools_expose_the_supervised_prose(self):
        commit = self.submit()["published_commit"]
        snapshot = self.context.snapshot()
        source_id = snapshot.event("sup01")["source"]["id"]
        evidence = snapshot.source_change(commit)
        self.assertEqual((evidence["before"], evidence["after"]), (None, DOCUMENT))
        hits = self.context.search("Tuesday", kind="source")
        self.assertEqual([(hit.kind, hit.id) for hit in hits], [("source", source_id)])
        self.assertEqual([hit.id for hit in self.context.search("Tuesday", kind="note")], ["notea", "noteb"])
        tools = EventTools(self.context, self.source("three.md", b"Third source.").event_id)
        read = tools.dispatch("read_source", {"id": source_id})
        self.assertTrue(read["ok"], read)
        self.assertEqual(read["result"]["text"], DOCUMENT.decode())
        evidence = tools.dispatch("read_evidence", {"commit": commit, "version": "after"})
        self.assertEqual(evidence["result"]["text"], DOCUMENT.decode())
        self.assertEqual([item["id"] for item in snapshot.supported_by(source_id)],
                         ["edgen", "bob12", "notea", "noteb"])
        self.assertEqual([item["operation"] for item in snapshot.history("notea", kind="note")], ["add", "edit"])
        self.assertEqual(snapshot.history("noteb", kind="note")[0]["citations"], [commit])

    def test_note_only_and_note_removal_plans_are_valid_batches(self):
        only = plan(self.base, note("noten", "An unlinked remark made by the owner."))
        commit = self.submit(only, event_id="sup02")["published_commit"]
        self.assertEqual(self.context.snapshot().notes["noten"]["objects"], [])
        self.assertEqual(self.context.snapshot().event("sup02")["changes"][0]["kind"], "note")
        value = plan(commit, remove("note", "noten", citations=[commit]), remove("note", "notea", citations=[self.base]))
        self.submit(value, event_id="sup03")
        self.assertEqual(self.context.snapshot().notes, {})
        self.assertEqual([item["operation"] for item in self.context.snapshot().history("noten", kind="note")], ["add", "remove"])

    def test_a_note_may_link_a_historical_target_removed_by_an_earlier_event(self):
        first = self.submit(plan(self.base, remove("edge", "edgeb", citations=[self.base]),
                                 remove("node", "jess1", citations=[self.base])), event_id="sup02")["published_commit"]
        self.assertNotIn("jess1", self.context.snapshot().nodes)
        second = self.submit(plan(first, note("notec", "Jess was once recorded as related to Alice.",
                                              [("node", "jess1"), ("edge", "edgeb")])), event_id="sup03")["published_commit"]
        self.assertEqual(self.context.snapshot().read("note", "notec")["links"], [
            {"kind": "node", "id": "jess1", "status": "removed"}, {"kind": "edge", "id": "edgeb", "status": "removed"}])
        self.assertEqual(self.context.snapshot(second).notes["notec"]["objects"][0]["id"], "jess1")

    def test_identity_survives_a_supervised_update_and_node_reuse_is_refused(self):
        self.submit(plan(self.base, note("notea", "Clarified: Jesse met Alice.", [("node", "jesse")],
                                         citations=["self"], support=[self.base, "self"])))
        self.assertEqual(self.context.snapshot().notes["notea"]["text"], "Clarified: Jesse met Alice.")
        value = plan(self.context.git.head(), put("node", person("bob12", "Bob")), remove("node", "bob12", citations=["self"]))
        with self.assertRaisesRegex(ContextError, "once"):
            self.submit(value, event_id="sup02")
        removed = plan(self.context.git.head(), put("node", person("jess2", "Jess Two")))
        commit = self.submit(removed, event_id="sup03")["published_commit"]
        reuse = plan(commit, remove("node", "jess2", citations=["self"]))
        commit = self.submit(reuse, event_id="sup04")["published_commit"]
        with self.assertRaisesRegex(Conflict, "reserved or has been published"):
            self.submit(plan(commit, put("node", person("jess2", "Jess Again"))), event_id="sup05")


class Execution(SupervisedCase):
    def test_exact_replay_is_idempotent_and_every_difference_conflicts(self):
        first = self.submit(record_context=NORTH)
        again = self.submit(record_context=NORTH)
        self.assertEqual((again["published_commit"], again["recovered"]), (first["published_commit"], True))
        self.advance()
        self.assertEqual(self.submit(record_context=NORTH)["published_commit"], first["published_commit"])
        for label, arguments in {"document": dict(content=DOCUMENT + b"!"),
                                 "plan": dict(value=self.mixed(rationale="Other.")),
                                 "context": dict(record_context=SOUTH)}.items():
            with self.subTest(label), self.assertRaises(Conflict):
                self.submit(**arguments)
        with self.assertRaises(Conflict):
            self.pipeline.submit_supervised(self.mixed(), content=DOCUMENT, submitted_by="another", event_id="sup01",
                                            record_context=NORTH)
        self.assertEqual(self.runner.calls, [])

    def test_a_stale_or_unfinished_proposal_is_never_rebased(self):
        self.interrupted()
        with self.assertRaisesRegex(Conflict, "pending"):
            self.submit(event_id="sup02")
        self.context.discard("sup01")
        head = self.advance()
        with self.assertRaisesRegex(Conflict, "Stale expected revision"):
            self.submit()
        self.assertUntouched(head)
        self.assertNotIn("foundation/supervised-edits/sup01.md", self.context.snapshot().files)

    def test_interruption_is_completed_by_retry_and_recover_without_a_model(self):
        self.interrupted(count=2)
        self.assertEqual(self.pipeline.recover()["status"], "needs_retry")
        self.assertEqual(self.context.git.head(), self.base)
        receipt = Pipeline(self.context, runner=NoModel()).retry("sup01")
        self.assertEqual((receipt["status"], receipt["model"], receipt["recovered"]), ("published", None, False))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.context.snapshot().files["foundation/supervised-edits/sup01.md"], DOCUMENT)
        self.assertEqual(sorted(self.context.snapshot().notes), ["notea", "noteb"])

    def test_a_crash_after_publication_is_recovered_with_the_same_receipt(self):
        with self.stop_after("after_publication"), self.assertRaises(ProcessStopped):
            self.submit()
        self.assertIn("sup01", self.context.snapshot().events)
        self.pipeline.recover()
        self.assertIsNone(self.context.pending())
        receipt = self.pipeline.retry("sup01")
        self.assertEqual((receipt["status"], receipt["recovered"], receipt["model"]), ("published", True, None))
        self.assertEqual(receipt["execution"]["plan"], supervised_edits.normalize(self.mixed()))

    def test_discard_leaves_nothing_behind_and_the_id_stays_unused(self):
        self.interrupted()
        self.context.discard("sup01")
        self.assertUntouched()
        self.assertNotIn("sup01", self.context.snapshot().used_ids)
        with self.assertRaisesRegex(PropagationError, "No matching pending event"):
            self.pipeline.retry("sup01")
        self.assertEqual(self.submit()["status"], "published")

    @contextmanager
    def altered_commit(self, change):
        original = self.context.git.commit

        def commit(files, *args, **kwargs):
            altered = dict(files)
            change(altered)
            return original(altered, *args, **kwargs)
        with patch.object(self.context.git, "commit", side_effect=commit):
            yield

    def test_a_commit_whose_records_differ_from_the_plan_is_never_promoted(self):
        original = self.context.git.files(self.base)
        removal = plan(self.base, remove("note", "notea", citations=[self.base]))
        scenarios = {
            "note text": (None, lambda f: f.update({"notes/noteb.json": encode({
                "id": "noteb", "text": "Different from the approved explicit plan.", "objects": []})})),
            "node record": (None, lambda f: f.update({"graph/nodes/bob12.json": original["graph/nodes/jesse.json"]})),
            "edge record": (None, lambda f: f.update({"graph/edges/edgen.json": original["graph/edges/edgea.json"]})),
            "removed edge remains": (None, lambda f: f.pop("graph/edges/edgeb.json")),
            "removed node remains": (None, lambda f: f.pop("graph/nodes/jess1.json")),
            "removed note remains": (removal, lambda f: f.pop("notes/notea.json")),
        }
        for label, (value, mutate) in scenarios.items():
            with self.subTest(label):
                with self.altered_commit(mutate), self.assertRaises(PropagationError) as caught:
                    self.submit(value)
                self.assertEqual(caught.exception.phase, "publication")
                self.assertIsInstance(caught.exception.__cause__, ContextError)
                self.assertEqual(self.context.git.head(), self.base)
                self.assertEqual(self.context.pending()["id"], "sup01")
                self.assertNotIn("sup01", self.context.snapshot().events)
                self.assertEqual(self.context.snapshot().notes["notea"]["text"],
                                 "Jesse reports meeting Alice; the date is not anchored.")
                self.context.discard("sup01")
        self.assertUntouched()
        receipt = self.submit()
        self.assertEqual(self.context.snapshot().notes["noteb"]["text"],
                         "Bob reportedly joined on a Tuesday; the owner has not confirmed the date.")
        self.assertEqual(receipt["status"], "published")

    def test_a_retained_pending_event_publishes_correctly_once_the_tampering_stops(self):
        with self.altered_commit(lambda f: f.update({"notes/noteb.json": encode({
                "id": "noteb", "text": "Different from the approved explicit plan.", "objects": []})})):
            with self.assertRaises(PropagationError):
                self.submit()
        self.assertEqual(self.pipeline.retry("sup01")["status"], "published")
        self.assertEqual(self.context.snapshot().notes["noteb"]["text"],
                         "Bob reportedly joined on a Tuesday; the owner has not confirmed the date.")

    def test_no_model_machinery_is_reachable_from_any_supervised_path(self):
        self.interrupted()
        self.pipeline.retry("sup01")
        self.pipeline.recover()
        self.assertEqual(self.runner.calls, [])
        self.assertFalse((self.context.runtime / "runs").exists())


class Boundaries(SupervisedCase):
    def test_ordinary_input_cannot_claim_the_supervised_route_or_prefix(self):
        for operation in ("add", "edit", "remove"):
            with self.subTest(operation), self.assertRaisesRegex(ContextError, "supervised"):
                self.context.begin(operation, "foundation/supervised-edits/sup01.md", content=None if operation == "remove" else DOCUMENT,
                                   metadata={"kind": "supervised_edit", "title": "t", "origin": "controlled_caller"},
                                   submitted_by="host-owner")
        self.assertUntouched()
        mimic = json.dumps(self.mixed()).encode()
        event = self.source("mimic.md", mimic)
        self.assertEqual(self.context.pending()["id"], event.event_id)
        self.assertNotIn("execution", self.context.pending())
        self.assertEqual(self.context.inspect_draft(event.event_id)["draft"], {})

    def test_legacy_graph_edits_stay_separate_from_the_supervised_format(self):
        with self.assertRaisesRegex(ContextError, "graph edit|operation"):
            self.pipeline.submit_edit(self.mixed(), submitted_by="host-owner", event_id="edit1")
        legacy = {"format": graph_edits.FORMAT, "expected_revision": self.base, "rationale": "Legacy.",
                  "operations": [note("noteb", "A note in a legacy plan.")]}
        with self.assertRaises(ContextError):
            self.pipeline.submit_edit(legacy, submitted_by="host-owner", event_id="edit2")
        mixed = plan(self.base, put("node", person("bob12", "Bob")))
        mixed["format"] = graph_edits.FORMAT
        receipt = self.pipeline.submit_edit(mixed, submitted_by="host-owner", event_id="edit3")
        self.assertEqual(receipt["execution"]["kind"], graph_edits.EXECUTION)
        self.assertEqual(self.context.snapshot().files["foundation/edits/edit3.txt"],
                         graph_edits.canonical(graph_edits.normalize(mixed)))
        self.assertEqual(self.runner.calls, [])

    @contextmanager
    def altered(self, change):
        original = self.context.job_path.read_bytes()
        job = json.loads(original)
        change(job)
        self.context.job_path.write_bytes(encode(job))
        try:
            yield
        finally:
            self.context.job_path.write_bytes(original)

    def refused(self):
        with self.assertRaises(ContextError):
            self.pipeline.retry("sup01")
        with self.assertRaises(ContextError):
            self.context.publish("sup01")
        self.assertEqual(self.context.git.head(), self.base)
        self.assertEqual(self.runner.calls, [])

    def test_a_tampered_pending_event_is_refused_by_retry_and_lower_level_publication(self):
        self.interrupted()
        input_path = self.context.runtime / "events/sup01/input.bin"
        original = input_path.read_bytes()
        self.assertEqual(original, DOCUMENT)
        input_path.write_bytes(DOCUMENT + b"tampered")
        self.refused()
        input_path.write_bytes(original)
        for label, change in {
                "plan digest": lambda job: job["execution"].update(plan_digest="0" * 64),
                "document digest": lambda job: job["execution"].update(document_digest="0" * 64),
                "plan rationale": lambda job: job["execution"]["plan"].update(rationale="Altered."),
                "plan base": lambda job: job["execution"]["plan"].update(expected_revision="2" * 40),
                "kind": lambda job: job["execution"].update(kind="model_run"),
                "marker removed": lambda job: job.pop("execution"),
                "extra marker field": lambda job: job["execution"].update(extra=1),
                "staged record": lambda job: job["draft"]["node:bob12"]["record"].update(summary="Robert"),
                "staged support": lambda job: job["draft"]["node:bob12"].update(support_after=[self.base]),
                "action mode": lambda job: job.update(input_mode="OPERATIONAL_ACTION")}.items():
            with self.subTest(label), self.altered(change):
                self.refused()
        self.assertEqual(self.pipeline.retry("sup01")["status"], "published")

    def test_foreign_draft_content_is_refused_and_removing_it_lets_retry_complete(self):
        self.interrupted()
        self.context.put_note("sup01", {"text": "Not in the plan.", "objects": []}, explanation="Foreign.",
                              citations=["self"], support_after=["self"])
        with self.assertRaises(PropagationError) as caught:
            self.pipeline.retry("sup01")
        self.assertIsInstance(caught.exception.__cause__, Conflict)
        with self.assertRaisesRegex(Conflict, "not exactly the bound supervised edit"):
            self.context.publish("sup01")
        extra = next(key for key in self.context.pending()["draft"] if key.startswith("note:") and key != "note:noteb")
        self.context.remove_note("sup01", extra.split(":")[1], explanation="Back to the plan.", citations=["self"])
        self.assertEqual(self.pipeline.retry("sup01")["status"], "published")

    def test_a_foreign_handoff_note_cannot_be_published(self):
        self.interrupted()
        self.context.stage_handoff_note("sup01", "Please action this.")
        with self.assertRaises(Conflict):
            self.context.publish("sup01")
        self.pipeline.retry("sup01")
        self.assertEqual(self.context.snapshot().event("sup01")["handoff_note"], {"disposition": "empty", "text": ""})

    def test_questions_cannot_be_attached_to_a_supervised_event(self):
        self.interrupted()
        with self.assertRaises(ContextError):
            self.context.emit_question("sup01", "Is this right?", [])
        self.pipeline.retry("sup01")
        self.assertEqual(self.pipeline.question_output("sup01")["questions"], [])


class PublishedReaders(SupervisedCase):
    def setUp(self):
        super().setUp()
        self.submit()

    def clone(self, event_id, change=lambda event, files: None, path=None):
        return crafted_clone(self.context, event_id, change, path)

    def altered(self, path, **changes):
        record = json.loads(self.context.git.files(self.context.git.head())[path])
        return {path: encode({**record, **changes})}

    def test_the_untouched_clone_is_valid_and_every_inconsistency_is_refused(self):
        self.assertIn("sup02", Snapshot(self.context.git, self.clone("sup02")).events)
        cases = {
            "reserved source without a marker": dict(event_id="sup03", change=lambda e, f: e.pop("execution")),
            "marker on an ordinary path": dict(event_id="sup04", path="foundation/ordinary.md"),
            "document bytes differ": dict(event_id="sup05", change=lambda e, f: f.update({e["source"]["path"]: DOCUMENT + b"x"})),
            "plan digest differs": dict(event_id="sup06", change=lambda e, f: e["execution"].update(plan_digest="1" * 64)),
            "plan altered": dict(event_id="sup07", change=lambda e, f: e["execution"]["plan"].update(rationale="Altered.")),
            "malformed marker": dict(event_id="sup08", change=lambda e, f: e["execution"].update(kind="model_run")),
            "different base": dict(event_id="sup09", change=lambda e, f: e.update(base_revision="2" * 40)),
            "altered attribution": dict(event_id="sup0a", change=lambda e, f: e["changes"][0].update(explanation="Altered.")),
            "missing change": dict(event_id="sup0b", change=lambda e, f: e["changes"].pop()),
            "extra change": dict(event_id="sup0c", change=lambda e, f: e["changes"].append(deepcopy(e["changes"][0]))),
            "recorded handoff": dict(event_id="sup0d", change=lambda e, f: e.update(handoff_note={"disposition": "prepared", "text": "x"})),
            "action mode": dict(event_id="sup0e", change=lambda e, f: e.update(input_mode="OPERATIONAL_ACTION")),
            "other source title": dict(event_id="sup0f", change=lambda e, f: e["source"].update(title="Other")),
            "graph edit marker on supervised path": dict(event_id="sup0g", change=lambda e, f: e["execution"].update(kind=graph_edits.EXECUTION)),
        }
        for label, arguments in cases.items():
            with self.subTest(label), self.assertRaises(ContextError):
                Snapshot(self.context.git, self.clone(**arguments))

    def test_records_that_differ_from_the_plan_in_the_publishing_commit_are_refused(self):
        original = self.context.git.files(self.base)
        cases = {
            "note text": lambda f: f.update(self.altered("notes/noteb.json", text="Different from the plan.")),
            "note links": lambda f: f.update(self.altered("notes/noteb.json", objects=[])),
            "updated note": lambda f: f.update(self.altered("notes/notea.json", text="Different from the plan.")),
            "node summary": lambda f: f.update(self.altered("graph/nodes/bob12.json", summary="Robert")),
            "edge summary": lambda f: f.update(self.altered("graph/edges/edgen.json", summary="Another relationship")),
            "removed edge remains": lambda f: f.update({"graph/edges/edgeb.json": original["graph/edges/edgeb.json"]}),
            "removed node remains": lambda f: f.update({"graph/nodes/jess1.json": original["graph/nodes/jess1.json"]}),
        }
        for index, (label, mutate) in enumerate(cases.items()):
            with self.subTest(label), self.assertRaisesRegex(ContextError, "do not match its plan"):
                Snapshot(self.context.git, self.clone(f"sx{index:03d}", lambda e, f, mutate=mutate: mutate(f)))
        self.assertIn("sx900", Snapshot(self.context.git, self.clone("sx900")).events)

    def test_later_legitimate_edits_and_removals_keep_the_history_readable(self):
        first = self.context.git.head()
        value = plan(first,
                     note("notea", "A later account of the meeting.", [("node", "jesse")],
                          citations=["self", first], support=[self.base, first, "self"]),
                     put("node", person("bob12", "Bobby"), citations=["self", first], support=[first, "self"]),
                     remove("edge", "edgen", citations=[first]),
                     remove("note", "noteb", citations=[first]))
        second = self.submit(value, event_id="sup02")["published_commit"]
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.notes["notea"]["text"], "A later account of the meeting.")
        self.assertEqual((snapshot.nodes["bob12"]["summary"], "edgen" in snapshot.edges, "noteb" in snapshot.notes),
                         ("Bobby", False, False))
        self.assertEqual(snapshot.event("sup01")["execution"]["document_digest"], hashlib.sha256(DOCUMENT).hexdigest())
        earlier = Snapshot(self.context.git, first)
        self.assertEqual((earlier.notes["noteb"]["text"], earlier.nodes["bob12"]["summary"]),
                         ("Bob reportedly joined on a Tuesday; the owner has not confirmed the date.", "Bob"))
        self.assertEqual({change["id"] for change in self.context.snapshot(second).event("sup02")["changes"]},
                         {"bob12", "edgen", "notea", "noteb"})


class CommandLine(SupervisedCase):
    def run_cli(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--repo", str(self.context.root), "--runtime", str(self.context.runtime), *arguments])
        return status, json.loads(output.getvalue())

    def files(self, value=None, document=DOCUMENT):
        plan_file, document_file = self.directory / "plan.json", self.directory / "document.md"
        plan_file.write_text(json.dumps(value or self.mixed()))
        document_file.write_bytes(document)
        return ["--plan-file", str(plan_file), "--document-file", str(document_file), "--submitted-by", "host-owner",
                "--event-id", "sup01"]

    def test_preview_then_explicit_submit_of_the_reviewed_files(self):
        before = tree(self.context.root, self.context.runtime)
        status, previewed = self.run_cli("preview-supervised", *self.files())
        self.assertEqual((status, previewed["ok"], previewed["result"]["status"]), (0, True, "ready"))
        self.assertEqual(tree(self.context.root, self.context.runtime), before)
        status, submitted = self.run_cli("submit-supervised", *self.files())
        self.assertEqual((status, submitted["ok"], submitted["result"]["status"], submitted["result"]["model"]),
                         (0, True, "published", None))
        self.assertEqual(self.context.snapshot().files["foundation/supervised-edits/sup01.md"], DOCUMENT)
        status, shown = self.run_cli("show-object", "note", "noteb")
        self.assertEqual((status, shown["result"]["record"]["id"]), (0, "noteb"))
        status, listed = self.run_cli("notes", "--object-kind", "node", "--object-id", "bob12")
        self.assertEqual([item["record"]["id"] for item in listed["result"]["items"]], ["noteb"])
        status, found = self.run_cli("search", "Tuesday", "--kind", "note")
        self.assertEqual([item["id"] for item in found["result"]["items"]], ["notea", "noteb"])
        status, output = self.run_cli("question-output", "sup01")
        self.assertEqual(output["result"]["questions"], [])
        status, acknowledged = self.run_cli("ack-question-output", "sup01", submitted["result"]["published_commit"])
        self.assertEqual((status, acknowledged["result"]["acknowledged"]), (0, True))

    def test_an_omitted_runtime_is_refused_before_any_context_or_file_access(self):
        commands = {
            "preview-supervised": ["--plan-file", str(self.directory / "missing.json"),
                                   "--document-file", str(self.directory / "missing.md"),
                                   "--submitted-by", "host-owner", "--event-id", "sup01"],
            "submit-supervised": ["--plan-file", str(self.directory / "missing.json"),
                                  "--document-file", str(self.directory / "missing.md"),
                                  "--submitted-by", "host-owner", "--event-id", "sup01"],
            "question-output": ["sup01"],
            "ack-question-output": ["sup01", "f" * 40],
        }
        before = tree(self.directory)
        for command, arguments in commands.items():
            with self.subTest(command), patch("qorqut_context.__main__.Context", side_effect=AssertionError("Context built")):
                output = io.StringIO()
                with redirect_stdout(output):
                    status = main(["--repo", str(self.context.root), command, *arguments])
                result = json.loads(output.getvalue())
                self.assertEqual((status, result["ok"]), (1, False))
                self.assertIn("--runtime", result["error"]["reason"])
        self.assertEqual(tree(self.directory), before)
        self.assertUntouched()

    def test_legacy_commands_keep_their_default_runtime(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--repo", str(self.context.root), "pending"])
        self.assertEqual((status, json.loads(output.getvalue())["ok"]), (0, True))

    def test_bad_arguments_and_stale_plans_exit_with_a_json_error(self):
        status, stale = self.run_cli("submit-supervised", *self.files(plan("f" * 40, note("noten", "Remark."))))
        self.assertEqual((status, stale["ok"]), (1, False))
        status, error = self.run_cli("preview-supervised", "--plan-file", str(self.directory / "missing.json"),
                                     "--document-file", str(self.directory / "missing.md"),
                                     "--submitted-by", "host-owner", "--event-id", "sup01")
        self.assertEqual((status, error["ok"]), (1, False))
        status, usage = self.run_cli("preview-supervised", "--plan-file", "x")
        self.assertEqual((status, usage["ok"]), (1, False))
        self.assertUntouched()
