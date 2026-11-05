"""Controlled record_context evidence and the expected_revision base: synthetic Git stores and injected runners only."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import patch

from qorqut_context import Conflict, ContextError
from qorqut_context.pipeline import Pipeline
from qorqut_context.schema import digest
from qorqut_context.tools import EventTools
from .helpers import ContextCase
from .test_pipeline import ScriptRunner, call, finish, new_node
from .test_recovery import ProcessStopped

NORTH = {"scope": {"kind": "business", "business_id": "business:north"},
         "provenance": {"channel": "chat", "message": 7, "nested": {"flags": [True, None, 1.5], "note": "café"}}}
SOUTH = {"scope": {"kind": "business", "business_id": "business:south"}, "provenance": {"channel": "email"}}
GLOBAL = {"scope": {"kind": "global"}, "provenance": {"recorded_by": "synthetic-caller"}}
NORTH_TAG = {"id": "tag:north", "name": "North", "business_id": "business:north"}
METADATA = {"kind": "note", "title": "a.md", "origin": "focused-test"}


def sha(content):
    return hashlib.sha256(content).hexdigest()


class RecordContextEvidence(ContextCase):
    def general(self, name="a.md", content=b"Jesse is a person.", **kwargs):
        event = self.source(name, content, **kwargs)
        return event, self.context.publish(event.event_id)

    def action(self, ref, operation="add", **kwargs):
        event = self.context.begin_action(operation, ref, text=None if operation == "remove" else "Routine recorded.",
                                          submitted_by="synthetic-recorder", **kwargs)
        self.context.stage_action_associations(event.event_id, 0, "Withdrawn." if operation == "remove" else "Recorded.", [])
        self.context.stage_handoff_note(event.event_id, "")
        return event, self.context.publish(event.event_id)

    def assertUntouched(self, head):
        self.assertIsNone(self.context.pending())
        self.assertEqual(self.context.git.head(), head)
        self.assertFalse((self.context.runtime / "events").exists() and any((self.context.runtime / "events").iterdir()))

    # ------------------------------------------------------------------------ GENERAL

    def test_ordinary_source_bytes_stay_exact_and_the_context_lives_in_event_metadata(self):
        content = "Jesse said:\r\n---\r\ncafé — exact bytes\n".encode()
        supplied = deepcopy(NORTH)
        event = self.source("exact.md", content, record_context=supplied)
        supplied["provenance"]["channel"] = "mutated by the caller after submission"
        self.assertEqual(self.context.pending()["record_context"], NORTH)
        commit = self.context.publish(event.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.files["foundation/exact.md"], content)
        published = snapshot.event(event.event_id)
        self.assertEqual((published["record_context"], published["input_digest"]), (NORTH, sha(content)))
        self.assertNotIn("expected_revision", published)
        history = snapshot.history(event.source_id, kind="source")
        self.assertEqual((history[0]["record_context"], history[0]["commit"]), (NORTH, commit))
        self.assertEqual(snapshot.source_change(commit)["event"]["record_context"], NORTH)
        self.assertEqual(snapshot.source_change(commit)["after"], content)
        plain, _ = self.general("plain.md")
        self.assertNotIn("record_context", self.context.snapshot().event(plain.event_id))
        self.assertNotIn("record_context", self.context.snapshot().history(plain.source_id, kind="source")[0])

    def test_absent_evidence_leaves_the_historic_submission_digest_unchanged(self):
        # Digests recorded from the unmodified baseline 24f3744 for exactly these inputs.
        general = self.source("a.md", b"Jesse is a person.")
        job = self.context.pending()
        self.assertEqual(job["submission_digest"], "8ccf1ae25b77ad7fce22af9be1548bce523fbf293a8a24e637b98200b0505a0e")
        self.assertFalse({"record_context", "expected_revision"} & set(job))
        self.context.publish(general.event_id)
        self.context.begin_action("add", "plain", text="Recorded.", submitted_by="synthetic-recorder")
        job = self.context.pending()
        self.assertEqual(job["submission_digest"], "dcbe62b70ddb36ffc1207219c4df2a8e3eb0c4f093b5a24ffb24e0f1a846a45c")
        self.assertNotIn("record_context", job)

    def test_evidence_is_bound_into_the_submission_digest(self):
        head, digests = self.context.git.head(), []
        reordered = {"provenance": {"nested": NORTH["provenance"]["nested"], "message": 7, "channel": "chat"},
                     "scope": {"business_id": "business:north", "kind": "business"}}
        for kwargs in ({}, {"record_context": NORTH}, {"record_context": reordered}, {"record_context": SOUTH},
                       {"expected_revision": head}, {"record_context": NORTH, "expected_revision": head}):
            event = self.source("bound.md", **kwargs)
            digests.append(self.context.pending()["submission_digest"])
            self.context.discard(event.event_id)
        self.assertEqual(digests[1], digests[2])                  # canonical JSON: key order is not identity
        self.assertEqual(len(set(digests)), 5)

    def test_changed_evidence_with_a_reused_event_id_is_refused(self):
        event, commit = self.general("first.md", event_id="first", record_context=NORTH)
        replay = self.context.begin("add", "foundation/first.md", content=b"Jesse is a person.", metadata={
            "kind": "note", "title": "first.md", "origin": "test", "created_at": "2026"},
            submitted_by="test-owner", event_id="first", record_context=deepcopy(NORTH))
        self.assertEqual((replay.event_id, replay.published_commit), ("first", commit))
        for changed in (SOUTH, GLOBAL, None):
            with self.assertRaisesRegex(Conflict, "different input"):
                self.source("first.md", event_id="first", record_context=changed)
        self.assertIsNone(self.context.pending())

        self.action("tracked", event_id="actio", record_context=NORTH)
        for changed in (SOUTH, None):
            with self.assertRaisesRegex(Conflict, "different input"):
                self.context.begin_action("add", "tracked", text="Routine recorded.", submitted_by="synthetic-recorder",
                                          event_id="actio", record_context=changed)

        pending = self.context.begin("add", "foundation/next.md", content=b"Another source.", metadata=METADATA,
                                     submitted_by="test-owner", event_id="pendg", record_context=NORTH)
        self.assertEqual(self.context.begin("add", "foundation/next.md", content=b"Another source.", metadata=METADATA,
                                            submitted_by="test-owner", event_id="pendg", record_context=NORTH), pending)
        with self.assertRaisesRegex(Conflict, "pending"):
            self.context.begin("add", "foundation/next.md", content=b"Another source.", metadata=METADATA,
                               submitted_by="test-owner", event_id="pendg", record_context=SOUTH)
        self.assertEqual(self.context.pending()["record_context"], NORTH)

    def test_pending_retry_and_recovery_keep_the_original_evidence_and_base(self):
        base = self.context.git.head()
        args = dict(content=b"Recoverable.", metadata=METADATA, submitted_by="test-owner", event_id="recov",
                    record_context=NORTH, expected_revision=base)
        event = self.context.begin("add", "foundation/recover.md", **args)
        self.assertEqual(self.context.begin("add", "foundation/recover.md", **args), event)
        self.assertEqual((self.context.pending()["record_context"], self.context.pending()["base_revision"]), (NORTH, base))
        self.assertEqual(self.context.pending()["expected_revision"], base)
        self.context.stage_handoff_note(event.event_id, "")

        def stop(phase):
            if phase == "after_publication":
                raise ProcessStopped()
        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.context.publish(event.event_id)
        self.assertEqual(self.context.pending()["record_context"], NORTH)       # published, bookkeeping unfinished
        recovered = self.context.recover()
        self.assertEqual(recovered["status"], "completed")
        published = self.context.snapshot().event(event.event_id)
        self.assertEqual((published["record_context"], published["base_revision"]), (NORTH, base))
        self.assertIsNone(self.context.pending())
        self.assertEqual(self.context.begin("add", "foundation/recover.md", **args).published_commit, recovered["commit"])

    def test_pipeline_gives_the_model_the_evidence_and_a_retry_publishes_it_without_another_turn(self):
        seen = []

        def interpret(tools, payload):
            seen.append(payload["source_change"].get("record_context"))
            self.assertEqual(call(tools, "read_draft")["record_context"], NORTH)
            new_node(tools)
            finish(tools)
        runner = ScriptRunner(interpret)
        pipeline = Pipeline(self.context, runner=runner)

        def stop(phase):
            if phase == "after_ready":
                raise ProcessStopped()
        with patch.object(pipeline, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            pipeline.submit("add", "foundation/a.md", content=b"Jesse is a person.", metadata=METADATA,
                            submitted_by="test", record_context=NORTH)
        receipt = pipeline.retry(self.context.pending()["id"])
        self.assertEqual((seen, runner.calls), ([NORTH], ["propagation"]))
        self.assertEqual(self.context.snapshot().event(receipt["event_id"])["record_context"], NORTH)

    def test_stale_expected_revision_leaves_no_pending_state_and_runs_no_model(self):
        old = self.context.git.head()
        self.general("moves-head.md")
        head = self.context.git.head()
        self.assertNotEqual(old, head)
        with self.assertRaisesRegex(Conflict, "Stale expected revision"):
            self.source("late.md", expected_revision=old)
        self.assertUntouched(head)
        runner = ScriptRunner(lambda tools, payload: finish(tools))
        with self.assertRaisesRegex(Conflict, "Stale expected revision"):
            Pipeline(self.context, runner=runner).submit(
                "add", "foundation/late.md", content=b"Late.", metadata=METADATA, submitted_by="test",
                record_context=NORTH, expected_revision=old)
        self.assertEqual(runner.calls, [])
        self.assertUntouched(head)
        event = self.source("fresh.md", expected_revision=head)
        self.assertEqual((event.base_revision, self.context.pending()["expected_revision"]), (head, head))

    def test_published_replay_after_the_head_advances_returns_the_original_commit_without_a_model_call(self):
        runner = ScriptRunner(lambda tools, payload: (new_node(tools), finish(tools)))
        pipeline = Pipeline(self.context, runner=runner)
        base = self.context.git.head()
        args = dict(content=b"Jesse appears here.", metadata=METADATA, submitted_by="test", event_id="first",
                    record_context=NORTH, expected_revision=base)
        original = pipeline.submit("add", "foundation/first.md", **args)
        pipeline.submit("add", "foundation/second.md", content=b"A later source.", metadata=METADATA,
                        submitted_by="test")
        head, calls = self.context.git.head(), list(runner.calls)
        self.assertNotEqual(head, original["published_commit"])
        replay = pipeline.submit("add", "foundation/first.md", **args)
        self.assertEqual((replay["published_commit"], replay["base_revision"]), (original["published_commit"], base))
        self.assertEqual((runner.calls, self.context.git.head()), (calls, head))
        for changed in ({"expected_revision": head}, {"expected_revision": None}, {"record_context": SOUTH},
                        {"record_context": None}):
            with self.assertRaisesRegex(Conflict, "different input"):
                pipeline.submit("add", "foundation/first.md", **(args | changed))
        self.assertEqual((runner.calls, self.context.git.head()), (calls, head))
        self.assertIsNone(self.context.pending())

    def test_invalid_record_context_and_expected_revision_fail_before_any_state_is_created(self):
        head = self.context.git.head()
        deep = []
        for _ in range(5000):
            deep = [deep]
        invalid = [[], "north", {"scope": GLOBAL["scope"]}, {**GLOBAL, "extra": 1}, {"provenance": {}, "scope": None},
                   {"scope": {"kind": "team"}, "provenance": {}}, {"scope": {"kind": "global", "business_id": "b"}, "provenance": {}},
                   {"scope": {"kind": "business"}, "provenance": {}},
                   {"scope": {"kind": "business", "business_id": ""}, "provenance": {}},
                   {"scope": {"kind": "business", "business_id": " "}, "provenance": {}},
                   {"scope": {"kind": "business", "business_id": 7}, "provenance": {}},
                   {"scope": {"kind": "business", "business_id": "b" * 257}, "provenance": {}},
                   {"scope": {"kind": "business", "business_id": "b", "extra": 1}, "provenance": {}},
                   {"scope": GLOBAL["scope"], "provenance": []}, {"scope": GLOBAL["scope"], "provenance": None},
                   {"scope": GLOBAL["scope"], "provenance": "text"},
                   {"scope": GLOBAL["scope"], "provenance": {1: "integer key"}},
                   {"scope": GLOBAL["scope"], "provenance": {"v": (1, 2)}},
                   {"scope": GLOBAL["scope"], "provenance": {"v": {1, 2}}},
                   {"scope": GLOBAL["scope"], "provenance": {"v": b"bytes"}},
                   {"scope": GLOBAL["scope"], "provenance": {"v": object()}},
                   {"scope": GLOBAL["scope"], "provenance": {"v": float("nan")}},
                   {"scope": GLOBAL["scope"], "provenance": {"v": [float("inf")]}},
                   {"scope": GLOBAL["scope"], "provenance": {"v": "\ud800"}},
                   {"scope": GLOBAL["scope"], "provenance": {"\ud800": "key"}},
                   {"scope": GLOBAL["scope"], "provenance": {"v": deep}}]
        for index, value in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaises(ContextError):
                    self.context.begin("add", "foundation/bad.md", content=b"Text.", metadata=METADATA,
                                       submitted_by="test-owner", record_context=value)
                with self.assertRaises(ContextError):
                    self.context.begin_action("add", "bad", text="Text.", submitted_by="synthetic-recorder",
                                              record_context=value)
                self.assertUntouched(head)
        too_big = {"scope": GLOBAL["scope"], "provenance": {name: "x" * 100 for name in "abcd"}}
        with self.assertRaisesRegex(ContextError, "byte limit"):
            self.context.begin("add", "foundation/bad.md", content=b"Text.", metadata=METADATA,
                               submitted_by="test-owner", source_bytes=300, record_context=too_big)
        with self.assertRaisesRegex(ContextError, "byte limit"):
            self.context.begin_action("add", "bad", text="Text.", submitted_by="synthetic-recorder",
                                      source_bytes=300, record_context=too_big)
        self.assertUntouched(head)
        for value in ("self", "main", "HEAD", head[:12], head.upper(), head + "0", "", 7, ["x"]):
            with self.subTest(expected_revision=value), self.assertRaises(ContextError):
                self.source("bad.md", expected_revision=value)
            self.assertUntouched(head)

    # ------------------------------------------------------------------------- Action

    def test_action_evidence_is_separate_from_label_only_tags_and_survives_edit_and_removal(self):
        first, first_commit = self.action("scoped", tags=[NORTH_TAG], record_context=GLOBAL, revision_ref="r1")
        document = json.loads(self.context.snapshot().files[f"foundation/actions/{first.source_id}.txt"])
        self.assertEqual((document["record_context"], document["tags"], document["text"]), (GLOBAL, [NORTH_TAG], "Routine recorded."))
        self.assertEqual(sorted(document), ["action_ref", "pointers", "record_context", "revision_ref", "tags", "text"])
        edited, edit_commit = self.action("scoped", "edit", tags=[], record_context=SOUTH, revision_ref="r2",
                                          expected_previous_commit=first_commit)
        document = json.loads(self.context.snapshot().files[f"foundation/actions/{first.source_id}.txt"])
        self.assertEqual((document["record_context"], document["tags"]), (SOUTH, []))
        removed, _ = self.action("scoped", "remove", tags=[], record_context=NORTH, expected_previous_commit=edit_commit)
        snapshot = self.context.snapshot()
        self.assertNotIn(f"foundation/actions/{first.source_id}.txt", snapshot.files)
        event = snapshot.event(removed.event_id)
        self.assertEqual((event["record_context"], event["input_digest"], event["operation"]), (NORTH, None, "remove"))
        revisions = snapshot.action("scoped", audit=True)["items"]
        self.assertEqual([item["record_context"] for item in revisions], [GLOBAL, SOUTH, NORTH])
        self.assertEqual([item["action"]["tags"] for item in revisions], [[NORTH_TAG], [], []])
        self.assertEqual(snapshot.actions_history(audit=True)["items"][0]["record_context"], GLOBAL)
        # Tags remain the only label filters: the recorded scope neither selects nor hides an Action.
        self.assertEqual(self.context.actions_history(revision=first_commit, global_only=True)["total"], 0)
        self.assertEqual(self.context.actions_history(revision=first_commit, tag_id=NORTH_TAG["id"])["total"], 1)
        self.assertEqual(self.context.actions_history(revision=edit_commit, global_only=True)["items"][0]["record_context"], SOUTH)
        plain, _ = self.action("plain")
        self.assertNotIn("record_context", self.context.snapshot().event(plain.event_id))
        self.assertNotIn("record_context", self.context.snapshot().action("plain"))
        self.assertNotIn("record_context", json.loads(self.context.snapshot().files[f"foundation/actions/{plain.source_id}.txt"]))

    def test_action_evidence_alone_makes_an_edit_a_new_revision_and_removal_keeps_its_own_digest(self):
        _, first_commit = self.action("only-context", record_context=NORTH)
        edited = self.context.begin_action("edit", "only-context", text="Routine recorded.", submitted_by="synthetic-recorder",
                                           expected_previous_commit=first_commit, record_context=SOUTH)
        self.assertNotEqual(self.context.pending()["input_digest"],
                            self.context.snapshot().actions["only-context"]["input_digest"])
        self.context.discard(edited.event_id)
        with self.assertRaisesRegex(ContextError, "identical-byte"):
            self.context.begin_action("edit", "only-context", text="Routine recorded.", submitted_by="synthetic-recorder",
                                      expected_previous_commit=first_commit, record_context=NORTH)
        digests = []
        for value in (NORTH, SOUTH, None):
            removal = self.context.begin_action("remove", "only-context", submitted_by="synthetic-recorder",
                                                expected_previous_commit=first_commit, record_context=value)
            digests.append(self.context.pending()["submission_digest"])
            self.context.discard(removal.event_id)
        self.assertEqual(len(set(digests)), 3)

    def test_tampered_action_or_draft_evidence_cannot_publish(self):
        event = self.context.begin_action("add", "sealed", text="Routine recorded.", submitted_by="synthetic-recorder",
                                          record_context=NORTH)
        self.context.stage_action_associations(event.event_id, 0, "Recorded.", [])
        self.context.stage_handoff_note(event.event_id, "")
        bound = self.context.pending()
        sealed = self.context.inspect_draft(event.event_id)["digest"]
        changed = deepcopy(bound)
        changed["record_context"] = SOUTH
        self.context._save(changed)
        self.assertNotEqual(self.context.inspect_draft(event.event_id)["digest"], sealed)
        head = self.context.git.head()
        with self.assertRaisesRegex(Conflict, "evidence does not match"):
            self.context.publish(event.event_id)
        self.assertEqual(self.context.git.head(), head)
        dropped = deepcopy(bound)
        del dropped["record_context"]
        self.context._save(dropped)
        with self.assertRaisesRegex(Conflict, "evidence does not match"):
            self.context.publish(event.event_id)
        self.context._save(bound)
        self.assertEqual(self.context.publish(event.event_id), self.context.git.head())
        self.assertEqual(self.context.snapshot().event(event.event_id)["record_context"], NORTH)

    # ------------------------------------------------------------------ model-facing view

    def test_event_tools_expose_the_evidence_but_cannot_alter_it(self):
        source, commit = self.general("evidence.md", record_context=NORTH)
        event = self.context.begin_action("add", "viewed", text="Routine recorded.", submitted_by="synthetic-recorder",
                                          record_context=GLOBAL)
        tools = EventTools(self.context, event.event_id)
        self.assertEqual(call(tools, "read_draft")["record_context"], GLOBAL)
        self.assertEqual(call(tools, "read_evidence", commit=commit, version="after")["event"]["record_context"], NORTH)
        self.assertEqual(call(tools, "object_history", id=source.source_id, kind="source")["items"][0]["record_context"], NORTH)
        before = self.context.inspect_draft(event.event_id)["digest"]
        for name, arguments in (("put_node", {"record": {"summary": "x", "attributes": {"basic_entity_type": ["t"]}},
                                              "support_after": ["self"], "explanation": "x", "citations": ["self"],
                                              "record_context": SOUTH}),
                                ("stage_handoff_note", {"text": "", "record_context": SOUTH}),
                                ("stage_action_associations", {"date": 0, "summary": "x", "associations": [],
                                                               "record_context": SOUTH}),
                                ("finish_propagation", {"status": "complete", "summary": "x", "reassessed": [],
                                                        "record_context": SOUTH}),
                                ("record_context", {"value": SOUTH})):
            result = tools.dispatch(name, arguments)
            self.assertFalse(result["ok"], name)
        self.assertEqual(self.context.pending()["record_context"], GLOBAL)
        self.assertEqual(self.context.inspect_draft(event.event_id)["digest"], before)
