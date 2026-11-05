import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qorqut_context import Context, Conflict, ContextError
from qorqut_context.engine import atomic_write
from qorqut_context.schema import encode
from qorqut_context.pipeline import Pipeline, PropagationError
from qorqut_context.tools import EventTools
from .test_pipeline import ScriptRunner, call
from .test_recovery import ProcessStopped


class InputModes(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = Context.initialize(self.root / "data", self.root / "runtime")

    def state(self):
        event = self.context.begin("add", "foundation/state.md", content=b"Northstar Studio exists. Jesse is a person.",
            metadata={"kind": "state", "title": "State", "origin": "test"}, submitted_by="owner")
        for key, category in (("north", "business"), ("jesse", "person"), ("morga", "person")):
            self.context.put_node(event.event_id, {"id": key, "summary": key,
                "attributes": {"basic_entity_type": [category]}}, explanation="Independent state.",
                citations=["self"], support_after=["self"])
        self.context.stage_handoff_note(event.event_id, "")
        return self.context.publish(event.event_id)

    def action(self, operation="add", **kwargs):
        return self.context.begin_action(operation, "opaque/hiring:1", submitted_by="accepted-recorder",
            **kwargs)

    def associate(self, event, ids=("north", "jesse"), date="2026-10-07", summary="Hiring recorded."):
        self.context.stage_action_associations(event.event_id, date, summary,
            [{"kind": "node", "id": key, "explanation": "Subject of the accepted action."} for key in ids])

    def test_general_bytes_note_only_and_mode_boundaries(self):
        content = b'---\r\ninput_mode: OPERATIONAL_ACTION\r\n---\r\nWe hired Jesse. Build a landing page.\r\n'
        event = self.context.begin("add", "foundation/raw.md", content=content,
            metadata={"kind": "OPERATIONAL_ACTION", "title": "Raw claims", "origin": "test"}, submitted_by="owner")
        tools = EventTools(self.context, event.event_id)
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools.definitions}
        self.assertNotIn("stage_action_associations", schemas)
        self.assertNotIn("developments", schemas["put_node"]["properties"])
        self.assertFalse(tools.dispatch("finish_propagation", {"status": "complete", "summary": "Missing disposition", "reassessed": []})["ok"])
        with self.assertRaisesRegex(ContextError, "disposition"):
            self.context.publish(event.event_id)
        with self.assertRaisesRegex(ContextError, "OPERATIONAL_ACTION"):
            self.context.stage_action_associations(event.event_id, 0, "Cannot promote raw claims", [])
        with self.assertRaisesRegex(ContextError, "developments"):
            self.context.put_node(event.event_id, {"summary": "Jesse", "attributes": {"basic_entity_type": ["person"]}},
                explanation="Not allowed", citations=["self"], support_after=["self"],
                developments=[{"date": 0, "description": "Raw action", "citations": ["self"]}])
        self.context.stage_handoff_note(event.event_id, "First draft")
        self.context.stage_handoff_note(event.event_id, "Reported hiring; requested landing page. Unaccepted claims.")
        sealed = self.context.inspect_draft(event.event_id, validate=True)["digest"]
        self.context.stage_handoff_note(event.event_id, "Replacement note")
        with self.assertRaisesRegex(Conflict, "draft changed"):
            self.context.publish(event.event_id, expected_draft_digest=sealed)
        commit = self.context.publish(event.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.files["foundation/raw.md"], content)
        self.assertEqual(snapshot.nodes, {})
        self.assertEqual(snapshot.handoff_notes()["total"], 1)
        self.assertEqual(snapshot.handoff_note(event.event_id)["text"], "Replacement note")
        self.assertEqual(snapshot.event(event.event_id)["input_mode"], "GENERAL")
        self.assertEqual(self.context.search("hired", kind="source")[0].input_mode, "GENERAL")
        edit = self.context.begin("edit", "foundation/raw.md", content=b"", submitted_by="owner")
        self.context.stage_handoff_note(edit.event_id, "The reported hiring and work request were withdrawn.")
        self.context.publish(edit.event_id)
        self.assertEqual(self.context.snapshot().handoff_note(edit.event_id)["previous_source_event"]["commit"], commit)
        self.assertEqual(self.context.snapshot(commit).handoff_notes()["total"], 1)

    def test_shared_action_correction_with_removed_and_history_only_targets(self):
        state_commit = self.state()
        original_text = 'Northstar hired Jesse.\r\n"Exact" Unicode: café\n'
        first = self.action(text=original_text, revision_ref="upstream arbitrary", related_objects=[
            {"kind": "node", "id": "zzzzz"}, {"kind": "node", "id": "morga"}])
        self.assertEqual(json.loads((self.context.runtime / "events" / first.event_id / "input.bin").read_bytes())["text"], original_text)
        with self.assertRaisesRegex(ContextError, "association draft"):
            self.context.publish(first.event_id)
        self.associate(first)
        with self.assertRaisesRegex(ContextError, "handoff note disposition"):
            self.context.publish(first.event_id)
        self.context.stage_handoff_note(first.event_id, "Which subject does unresolved pointer zzzzz identify?")
        first_commit = self.context.publish(first.event_id)
        self.assertEqual(self.context.snapshot().event(first.event_id)["changes"], [])
        self.assertEqual(self.context.snapshot().handoff_note(first.event_id)["input_mode"], "OPERATIONAL_ACTION")
        self.assertEqual(self.context.snapshot().handoff_notes()["items"][0]["event_id"], first.event_id)
        # Independently remove Jesse from the current graph; action links remain historical.
        removed = self.context.begin("edit", "foundation/state.md", content=b"Northstar and Morgan exist.", submitted_by="owner")
        self.context.remove_node(removed.event_id, "jesse", explanation="State corrected.", citations=["self"])
        self.context.stage_handoff_note(removed.event_id, "")
        self.context.publish(removed.event_id)
        edit = self.action("edit", text="Northstar hired Morgan on 2026-10-06.", expected_previous_commit=first_commit)
        draft = self.context.inspect_draft(edit.event_id)
        self.assertEqual(self.context.required_reassessment(draft), [{"kind": "node", "id": "jesse"}, {"kind": "node", "id": "north"}])
        tools = EventTools(self.context, edit.event_id)
        self.assertTrue(call(tools, "read_object", kind="node", id="jesse")["removed"])
        self.assertEqual(call(tools, "object_history", kind="node", id="jesse")["items"][-1]["record_before"]["id"], "jesse")
        self.assertEqual(call(tools, "action_timeline", kind="node", id="jesse")["total"], 1)
        self.associate(edit, ids=("north", "morga"), date="2026-10-06")
        self.context.stage_handoff_note(edit.event_id, "")
        self.assertFalse(tools.dispatch("finish_propagation", {"status": "complete", "summary": "Missing historical coverage", "reassessed": []})["ok"])
        call(tools, "finish_propagation", status="complete", summary="History reassessed", reassessed=tools.required_reassessment)
        edited_commit = self.context.publish(edit.event_id)
        self.assertEqual(edit.source_id, first.source_id)
        self.assertEqual(self.context.snapshot().action_timeline("node", "jesse")["total"], 0)
        self.assertEqual(self.context.snapshot().action_timeline("node", "jesse", audit=True)["total"], 2)
        self.assertEqual(self.context.snapshot(first_commit).action_timeline("node", "jesse")["total"], 1)
        self.assertEqual(self.context.snapshot().action("opaque/hiring:1")["action_associations"]["date"], "2026-10-06T00:00:00Z")
        with self.assertRaisesRegex(Conflict, "Stale"):
            self.action("remove", expected_previous_commit=first_commit)
        withdrawal = self.action("remove", expected_previous_commit=edited_commit)
        self.assertEqual({item["id"] for item in self.context.required_reassessment(self.context.inspect_draft(withdrawal.event_id))},
                         {"north", "jesse", "morga"})
        with self.assertRaisesRegex(ContextError, "previous action date"):
            self.associate(withdrawal, ids=(), date=0)
        self.associate(withdrawal, ids=(), date="2026-10-06", summary="Hiring claim withdrawn; no firing asserted.")
        self.context.stage_handoff_note(withdrawal.event_id, "")
        withdrawn_commit = self.context.publish(withdrawal.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.action_timeline("node", "jesse", audit=True)["total"], 3)
        self.assertTrue(snapshot.action("opaque/hiring:1")["withdrawn"])
        self.assertNotIn(f"foundation/actions/{first.source_id}.txt", snapshot.files)
        self.assertEqual(snapshot.read("node", "north")["support"], [state_commit])
        self.assertEqual(snapshot.action("opaque/hiring:1", audit=True, limit=1)["next_offset"], 1)
        replay = self.action("edit", text="Northstar hired Morgan on 2026-10-06.", expected_previous_commit=first_commit, event_id=edit.event_id)
        self.assertEqual(replay.published_commit, edited_commit)
        self.assertEqual(self.context.git.head(), withdrawn_commit)
        with self.assertRaisesRegex(Conflict, "different input"):
            self.action("edit", text="Changed", event_id=edit.event_id)
        with self.assertRaisesRegex(Conflict, "source identity"):
            self.action(text="Restoring is deferred")
        with self.assertRaisesRegex(ContextError, "reserved"):
            self.context.begin("remove", f"foundation/actions/{first.source_id}.txt", submitted_by="owner")

    def test_action_output_validation_and_seals(self):
        self.state()
        with self.assertRaisesRegex(ContextError, "pointer"):
            self.action(text="Routine", related_objects=[{"kind": "business", "id": "north"}])
        event = self.action(text="Routine business review.")
        self.context.stage_handoff_note(event.event_id, "")
        with self.assertRaisesRegex(ContextError, "published history"):
            self.associate(event, ids=("xxxxx",))
        self.associate(event, ids=("north",), date=0)
        sealed = self.context.inspect_draft(event.event_id, validate=True)["digest"]
        self.associate(event, ids=("north",), date=0, summary="Changed output")
        with self.assertRaisesRegex(Conflict, "draft changed"):
            self.context.publish(event.event_id, expected_draft_digest=sealed)
        job = self.context.pending()
        job["draft"] = {}
        job["handoff_note"] = {"disposition": "empty", "text": "An unresolved question"}
        atomic_write(self.context.job_path, encode(job))
        with self.assertRaisesRegex(ContextError, "Invalid handoff note"):
            self.context.publish(event.event_id)

    def test_pipeline_action_effects_and_recovery_do_not_duplicate_history(self):
        state_commit = self.state()
        edge_id = []

        def action_turn(tools, payload):
            self.assertEqual(payload["input_mode"], "OPERATIONAL_ACTION")
            self.assertIn("stage_handoff_note", {item["name"] for item in tools.definitions})
            operation = payload["source_change"]["operation"]
            call(tools, "stage_handoff_note", text="Which entity does pointer zzzzz refer to?" if operation == "add" else "")
            if operation == "add":
                self.assertEqual(payload["source_change"]["pointer_resolution"][0]["status"], "unresolved")
                edge_id.append(call(tools, "put_edge", record={"from_id": "jesse", "to_id": "north",
                    "summary": "Jesse works at Northstar.", "attributes": {"relationship": ["employment"]}},
                    explanation="Accepted hiring.", citations=["self"], support_after=["self"],
                    developments=[{"date": "2026-10-07", "description": "Employment began.", "citations": ["self"]}])["id"])
                call(tools, "stage_action_associations", date="2026-10-07", summary="Hiring accepted.", associations=[
                    {"kind": kind, "id": key, "explanation": "Relevant subject."}
                    for kind, key in (("node", "north"), ("node", "jesse"), ("edge", edge_id[0]))])
            else:
                call(tools, "remove_edge", id=edge_id[0], explanation="Hiring claim withdrawn.", citations=["self"])
                call(tools, "stage_action_associations", date="2026-10-07", summary="Hiring withdrawn.", associations=[])
            call(tools, "finish_propagation", status="complete", summary="Accepted revision interpreted.", reassessed=tools.required_reassessment)

        runner = ScriptRunner(action_turn)
        exposed = []
        pipeline = Pipeline(self.context, runner=runner, on_event=exposed.append)
        def stop(phase):
            if phase == "after_publication":
                raise ProcessStopped()
        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            pipeline.submit_action("add", "hire", text="Northstar hired Jesse on 2026-10-07.",
                submitted_by="owner", related_objects=[{"kind": "node", "id": "zzzzz"}])
        event_id, calls_before = exposed[-1], list(runner.calls)
        receipt = pipeline.retry(event_id)
        self.assertEqual(runner.calls, calls_before)
        self.assertEqual(receipt["handoff_disposition"], "prepared")
        self.assertEqual(self.context.snapshot().handoff_note(event_id)["text"], "Which entity does pointer zzzzz refer to?")
        self.assertEqual(self.context.snapshot().action_timeline("node", "jesse", audit=True)["total"], 1)
        attempt = json.loads((Path(receipt["diagnostics"]) / "attempt.json").read_bytes())
        import hashlib
        from qorqut_context.pipeline import INSTRUCTIONS, MODE_INSTRUCTIONS
        self.assertEqual(attempt["prompt_digests"]["propagation"], hashlib.sha256(
            (INSTRUCTIONS + "\n\n" + MODE_INSTRUCTIONS["OPERATIONAL_ACTION"]).encode()).hexdigest())
        self.assertEqual(self.context.snapshot().history(edge_id[0])[0]["developments"][0]["date"], "2026-10-07T00:00:00Z")
        # Ready recovery seals the exact mode output as well as graph changes.
        with patch.object(pipeline, "_checkpoint", side_effect=ProcessStopped()), self.assertRaises(ProcessStopped):
            pipeline.submit_action("remove", "hire", submitted_by="owner")
        removal = exposed[-1]
        calls_before = list(runner.calls)
        output = self.context.inspect_draft(removal)["action_associations"]
        self.context.stage_action_associations(removal, output["date"], "Changed after completion", [])
        with self.assertRaisesRegex(PropagationError, "does not match"):
            pipeline.retry(removal)
        self.context.stage_action_associations(removal, output["date"], output["summary"], [])
        self.context.stage_handoff_note(removal, "Changed after completion")
        with self.assertRaisesRegex(PropagationError, "does not match"):
            pipeline.retry(removal)
        self.context.stage_handoff_note(removal, "")
        pipeline.retry(removal)
        self.assertEqual(runner.calls, calls_before)
        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.edges, {})
        self.assertEqual(snapshot.read("node", "jesse")["support"], [state_commit])
        self.assertEqual(snapshot.action_timeline("edge", edge_id[0], audit=True)["total"], 2)
        self.assertEqual(snapshot.action_timeline("node", "north")["total"], 0)
        self.assertEqual(snapshot.handoff_note(removal)["disposition"], "empty")

    def test_blocked_action_retains_questions_without_publication(self):
        base = self.state()
        question = "Which of the two people named Jesse performed this action?"

        def blocked(tools, payload):
            args = {"status": "blocked", "summary": "Actor cannot be resolved safely.", "reassessed": []}
            self.assertFalse(tools.dispatch("finish_propagation", args)["ok"])
            call(tools, "stage_handoff_note", text=question)
            call(tools, "finish_propagation", **args)

        pipeline = Pipeline(self.context, runner=ScriptRunner(blocked))
        with self.assertRaisesRegex(PropagationError, "Actor cannot be resolved"):
            pipeline.submit_action("add", "ambiguous", text="Jesse acted.",
                submitted_by="owner")
        pending = self.context.pending()
        self.assertEqual(self.context.inspect_draft(pending["id"])["handoff_note"]["text"], question)
        self.assertEqual(self.context.git.head(), base)
        self.assertEqual(self.context.snapshot().actions, {})
        self.assertEqual(self.context.snapshot().handoff_notes()["total"], 0)
        self.assertEqual(pipeline.recover()["status"], "needs_retry")
        self.assertEqual(self.context.pending()["handoff_note"]["text"], question)

    def publish_fixture(self, files):
        head = self.context.git.head()
        commit = self.context.git.commit(files, self.context.runtime, head, "Fixture")
        self.context.git.publish(commit, head)
        self.context.git.checkout(head, commit)
        return commit

    def preserved(self):
        """Everything an incompatible repository must keep byte-for-byte."""
        config = self.context.root / "graph" / "config.json"
        return (self.context.git.head(), self.context.git.run("show-ref"), config.read_bytes(),
                self.context.job_path.read_bytes() if self.context.job_path.exists() else None)

    def test_only_format_3_reads_with_explicit_input_modes(self):
        self.state()
        action = self.action(text="Northstar hired Jesse.")
        self.associate(action)
        self.context.stage_handoff_note(action.event_id, "")
        self.context.publish(action.event_id)
        snapshot = self.context.snapshot()
        self.assertEqual(json.loads(snapshot.files["graph/config.json"]),
                         {"format_version": 3, "published_ref": "refs/heads/main"})
        self.assertEqual(sorted(snapshot.input_mode(event) for event in snapshot.events.values()),
                         ["GENERAL", "OPERATIONAL_ACTION"])
        self.assertEqual(snapshot.event(action.event_id)["input_mode"], "OPERATIONAL_ACTION")
        self.assertEqual({hit.input_mode for hit in self.context.search("", kind="source")}, {"GENERAL", "OPERATIONAL_ACTION"})

    def test_other_repository_formats_are_rejected_without_any_change(self):
        for index, config in enumerate(({"format_version": 1, "published_ref": "refs/heads/main"},
                                        {"format_version": 4, "published_ref": "refs/heads/main"},
                                        {"format_version": "3", "published_ref": "refs/heads/main"},
                                        {"published_ref": "refs/heads/main"})):
            with self.subTest(config=config):
                self.context = Context.initialize(self.root / f"other{index}", self.root / f"runtime{index}")
                self.publish_fixture({"graph/config.json": encode(config)})
                before = self.preserved()
                for operation in (lambda: self.context.snapshot(), lambda: self.context.recover(),
                                  lambda: self.context.search("anything"),
                                  lambda: self.context.begin("add", "foundation/new.md", content=b"x", submitted_by="owner"),
                                  lambda: self.context.begin_action("add", "opaque/a:1", text="x", submitted_by="owner")):
                    with self.assertRaisesRegex(ContextError, "Unsupported context format version"):
                        operation()
                    self.assertEqual(self.preserved(), before)
        self.context = Context.initialize(self.root / "extra", self.root / "runtime-extra")
        self.publish_fixture({"graph/config.json": encode({"format_version": 3, "published_ref": "refs/heads/other"})})
        before = self.preserved()
        with self.assertRaisesRegex(ContextError, "not the supported format 3 configuration"):
            self.context.snapshot()
        self.assertEqual(self.preserved(), before)

    def test_published_events_need_the_current_format_and_an_explicit_input_mode(self):
        base = self.context.git.head()
        event = {"format_version": 3, "id": "old01", "base_revision": base, "operation": "add",
                 "submitted_by": "owner", "submission_digest": "digest",
                 "source": {"id": "old02", "path": "foundation/old.md", "kind": "note", "title": "Old", "origin": "test"},
                 "changes": []}
        for index, (variant, message) in enumerate((
                (event, "no supported input mode"),
                ({**event, "input_mode": "LEGACY"}, "no supported input mode"),
                ({**event, "input_mode": "GENERAL", "format_version": 2}, "not format 3"))):
            with self.subTest(variant=variant.get("input_mode"), version=variant["format_version"]):
                self.context = Context.initialize(self.root / f"events{index}", self.root / f"events-runtime{index}")
                event_file = encode({**variant, "base_revision": self.context.git.head()})
                self.publish_fixture({"graph/events/old01.json": event_file, "foundation/old.md": b"Old statement"})
                before = self.preserved()
                for operation in (lambda: self.context.snapshot(), lambda: self.context.recover()):
                    with self.assertRaisesRegex(ContextError, message):
                        operation()
                    self.assertEqual(self.preserved(), before)
                self.assertEqual((self.context.root / "graph" / "events" / "old01.json").read_bytes(), event_file)

    def test_pending_work_of_another_format_is_reported_and_left_unchanged(self):
        commit = self.context.git.head()
        for job in (encode({"format_version": 1, "id": "old03", "phase": "draft", "error": None}),
                    encode({"id": "old03", "phase": "draft", "error": None}), b"[]\n"):
            with self.subTest(job=job):
                atomic_write(self.context.job_path, job)
                for operation in (lambda: self.context.pending(), lambda: self.context.recover(),
                                  lambda: self.context.discard("old03"), lambda: self.context.publish("old03"),
                                  lambda: self.context.inspect_draft("old03"),
                                  lambda: self.context.begin("add", "foundation/new.md", content=b"", submitted_by="owner")):
                    with self.assertRaisesRegex(Conflict, "Unsupported pending event format version"):
                        operation()
                    self.assertEqual(self.context.job_path.read_bytes(), job)
                    self.assertEqual(self.context.git.head(), commit)

    def test_recovery_record_of_another_format_cannot_finish_a_published_event(self):
        event = self.context.begin("add", "foundation/state.md", content=b"Northstar Studio exists.",
            metadata={"kind": "state", "title": "State", "origin": "test"}, submitted_by="owner")
        self.context.put_node(event.event_id, {"id": "north", "summary": "north",
            "attributes": {"basic_entity_type": ["business"]}}, explanation="Independent state.",
            citations=["self"], support_after=["self"])
        self.context.stage_handoff_note(event.event_id, "")

        def stop(phase):
            if phase == "after_publication":
                raise ProcessStopped()

        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.context.publish(event.event_id)
        commit = self.context.git.head()
        self.assertIn(event.event_id, self.context.snapshot().events)
        current = self.context.job_path.read_bytes()
        old = encode({**json.loads(current), "format_version": 1})
        atomic_write(self.context.job_path, old)
        runtime_event = self.context.runtime / "events" / event.event_id
        for operation in (lambda: self.context.pending(), lambda: self.context.recover(),
                          lambda: self.context.publish(event.event_id)):
            with self.assertRaisesRegex(Conflict, "Unsupported pending event format version 1"):
                operation()
            self.assertEqual(self.context.job_path.read_bytes(), old)
            self.assertEqual(self.context.git.head(), commit)
            self.assertTrue(runtime_event.exists())
        # The same published event with its current-format record still recovers normally.
        atomic_write(self.context.job_path, current)
        self.assertEqual(self.context.recover(), {"status": "completed", "commit": commit, "event_id": event.event_id})
        self.assertIsNone(self.context.pending())
