"""Shared Action revisions: synthetic Git stores and an injected fake runner only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qorqut_context import Context, Conflict, ContextError
from qorqut_context.__main__ import execute, parser
from qorqut_context.engine import atomic_write
from qorqut_context.pipeline import Pipeline
from qorqut_context.schema import encode
from qorqut_context.tools import EventTools
from .test_pipeline import ScriptRunner, call
from .test_recovery import ProcessStopped


ORDINARY = {"id": "tag:research", "name": "Research", "business_id": None}
BUSINESS = {"id": "tag:north", "name": "North", "business_id": "business:north"}
SECOND_BUSINESS = {"id": "tag:south", "name": "South", "business_id": "business:south"}


class SharedActions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = Context.initialize(self.root / "data", self.root / "runtime")

    def publish(self, ref, *, operation="add", tags=None, revision_ref=None, text="Routine recorded.", **args):
        event = self.context.begin_action(operation, ref, text=None if operation == "remove" else text,
            tags=tags, revision_ref=revision_ref, submitted_by="synthetic-recorder", **args)
        self.context.stage_action_associations(event.event_id, 0, "Withdrawn." if operation == "remove" else "Recorded.", [])
        self.context.stage_handoff_note(event.event_id, "")
        commit = self.context.publish(event.event_id)
        return event, commit

    def test_no_subject_pipeline_add_edit_withdrawal_and_exact_replay(self):
        def interpret(tools, payload):
            self.assertEqual(payload["required_reassessment"], [])
            self.assertIn("tags", payload["source_change"]["action"])
            call(tools, "stage_action_associations", date=0, summary="Revision interpreted.", associations=[])
            call(tools, "stage_handoff_note", text="")
            call(tools, "finish_propagation", status="complete", summary="No suitable graph subject.", reassessed=[])

        runner = ScriptRunner(interpret)
        pipeline = Pipeline(self.context, runner=runner)
        original_text = 'Routine completed.\r\nExact café "text".\n'
        first_args = dict(text=original_text, revision_ref="revision/add:opaque",
                          submitted_by="recorder", event_id="first")
        first = pipeline.submit_action("add", "opaque/activity:1", **first_args)
        first_commit = first["published_commit"]
        second_args = dict(text=original_text, revision_ref="revision/edit:opaque", tags=[ORDINARY],
                           submitted_by="recorder", expected_previous_commit=first_commit, event_id="editt")
        second = pipeline.submit_action("edit", "opaque/activity:1", **second_args)
        second_commit = second["published_commit"]
        with self.assertRaisesRegex(Conflict, "Stale"):
            pipeline.submit_action("remove", "opaque/activity:1", submitted_by="recorder",
                                   expected_previous_commit=first_commit)
        remove_args = dict(revision_ref="revision/remove:opaque", tags=[ORDINARY], submitted_by="recorder",
                           expected_previous_commit=second_commit, event_id="remov")
        removed = pipeline.submit_action("remove", "opaque/activity:1", **remove_args)
        before = list(runner.calls)
        for operation, args, expected in (("add", first_args, first), ("edit", second_args, second),
                                           ("remove", remove_args, removed)):
            self.assertEqual(pipeline.submit_action(operation, "opaque/activity:1", **args)["published_commit"],
                             expected["published_commit"])
        self.assertEqual(runner.calls, before)
        self.assertEqual(self.context.actions_history()["total"], 0)
        history = self.context.actions_history(audit=True)["items"]
        self.assertEqual([item["operation"] for item in history], ["add", "edit", "remove"])
        self.assertEqual(len({item["source"]["id"] for item in history}), 1)
        self.assertEqual([item["action"]["revision_ref"] for item in history],
                         ["revision/add:opaque", "revision/edit:opaque", "revision/remove:opaque"])
        self.assertEqual([item["text"] for item in history], [original_text, original_text, None])
        self.assertEqual(history[1]["previous_source_event"], {"event_id": "first", "commit": first_commit})
        self.assertEqual(history[2]["previous_source_event"], {"event_id": "editt", "commit": second_commit})
        self.assertTrue(history[-1]["withdrawn"])
        for item in history:
            self.assertEqual(item["action_associations"]["associations"], [])
            self.assertEqual(item["association_transition"], {key: [] for key in ("before", "after", "added", "removed")})
            self.assertEqual(self.context.snapshot().handoff_note(item["id"])["disposition"], "empty")
        self.assertEqual(self.context.snapshot().nodes, {})
        self.assertEqual(self.context.snapshot().edges, {})
        self.assertEqual(self.context.actions_history(revision=first_commit)["items"][0]["action"]["tags"], [])
        with self.assertRaisesRegex(Conflict, "different input"):
            pipeline.submit_action("edit", "opaque/activity:1", **(second_args | {"tags": [BUSINESS]}))
        with self.assertRaisesRegex(Conflict, "source identity"):
            pipeline.submit_action("add", "opaque/activity:1", text="Reversal is a new Action.", submitted_by="recorder")

    def test_tag_only_revision_changes_evidence_and_seals(self):
        tags = [deepcopy(ORDINARY)]
        first, first_commit = self.publish("tag-only", tags=tags, revision_ref="exact/unchanged")
        tags[0]["name"] = "Caller mutation"
        self.assertEqual(self.context.actions_history()["items"][0]["action"]["tags"], [ORDINARY])
        edited = self.context.begin_action("edit", "tag-only", text="Routine recorded.", tags=[BUSINESS],
            revision_ref="exact/unchanged", submitted_by="synthetic-recorder", expected_previous_commit=first_commit)
        self.assertEqual(self.context.begin_action("edit", "tag-only", text="Routine recorded.", tags=[BUSINESS],
            revision_ref="exact/unchanged", submitted_by="synthetic-recorder", expected_previous_commit=first_commit,
            event_id=edited.event_id), edited)
        with self.assertRaisesRegex(Conflict, "pending"):
            self.context.begin_action("edit", "tag-only", text="Routine recorded.", tags=[ORDINARY],
                revision_ref="exact/unchanged", submitted_by="synthetic-recorder", event_id=edited.event_id)
        old = self.context.snapshot().event(first.event_id)
        self.assertNotEqual(old["input_digest"], self.context.pending()["input_digest"])
        document = json.loads((self.context.runtime / "events" / edited.event_id / "input.bin").read_bytes())
        self.assertEqual(document["tags"], [BUSINESS])
        self.assertEqual(document["text"], "Routine recorded.")
        self.context.stage_action_associations(edited.event_id, 0, "Tags corrected.", [])
        self.context.stage_handoff_note(edited.event_id, "")
        bound = self.context.pending()
        changed = deepcopy(bound)
        changed["action"]["tags"] = [ORDINARY]
        atomic_write(self.context.job_path, encode(changed))
        self.assertNotEqual(self.context._draft_digest(bound), self.context.inspect_draft(edited.event_id)["digest"])
        with self.assertRaisesRegex(Conflict, "evidence does not match"):
            self.context.publish(edited.event_id)
        self.assertEqual(self.context.git.head(), first_commit)
        atomic_write(self.context.job_path, encode(bound))
        commit = self.context.publish(edited.event_id)
        self.assertEqual(self.context.actions_history()["items"][0]["commit"], commit)
        self.assertEqual(self.context.actions_history(tag_id=ORDINARY["id"])["total"], 0)
        self.assertEqual(self.context.actions_history(tag_id=ORDINARY["id"], audit=True)["total"], 1)

    def test_history_filters_snapshots_pagination_and_withdrawals(self):
        plain, _ = self.publish("plain")
        ordinary, _ = self.publish("ordinary", tags=[ORDINARY])
        multi, multi_commit = self.publish("multi", tags=[ORDINARY, BUSINESS, SECOND_BUSINESS])
        corrected, _ = self.publish("ordinary", operation="edit", tags=[BUSINESS], revision_ref="moved")
        removed, _ = self.publish("multi", operation="remove", tags=[ORDINARY, BUSINESS, SECOND_BUSINESS])
        def ids(**args):
            return [item["id"] for item in self.context.actions_history(**args)["items"]]
        self.assertEqual(ids(), [plain.event_id, corrected.event_id])
        self.assertEqual(ids(global_only=True), [plain.event_id])
        self.assertEqual(ids(global_only=True, audit=True), [plain.event_id, ordinary.event_id])
        self.assertEqual(ids(tag_id=ORDINARY["id"]), [])
        self.assertEqual(ids(tag_id=ORDINARY["id"], global_only=True, audit=True), [ordinary.event_id])
        self.assertEqual(ids(tag_id=BUSINESS["id"], global_only=True, audit=True), [])
        self.assertEqual(ids(tag_id=BUSINESS["id"], audit=True), [multi.event_id, corrected.event_id, removed.event_id])
        self.assertEqual(ids(tag_id=SECOND_BUSINESS["id"], audit=True), [multi.event_id, removed.event_id])
        self.assertEqual(ids(tag_id="Research", audit=True), [])  # IDs, not mutable/display names.
        self.assertEqual(ids(global_only=True, revision=multi_commit), [plain.event_id, ordinary.event_id])
        page = self.context.actions_history(audit=True, offset=1, limit=2)
        self.assertEqual([item["id"] for item in page["items"]], [ordinary.event_id, multi.event_id])
        self.assertEqual((page["total"], page["offset"], page["limit"], page["has_more"], page["next_offset"]),
                         (5, 1, 2, True, 3))
        end = self.context.actions_history(audit=True, offset=5, limit=2)
        self.assertEqual((end["items"], end["has_more"], end["next_offset"]), ([], False, None))
        for args in ({"offset": -1}, {"limit": 0}, {"limit": 101}, {"offset": True},
                     {"audit": 1}, {"global_only": "yes"}, {"tag_id": ""}):
            with self.subTest(args=args), self.assertRaises(ContextError):
                self.context.actions_history(**args)

    def test_explicit_empty_outputs_required_and_draft_excluded(self):
        event = self.context.begin_action("add", "global", text="Routine done.", submitted_by="recorder")
        with self.assertRaisesRegex(Conflict, "pending"):
            self.context.begin_action("add", "other", text="Other.", submitted_by="recorder")
        self.assertEqual(self.context.actions_history(audit=True)["total"], 0)
        with self.assertRaisesRegex(ContextError, "association draft"):
            self.context.publish(event.event_id)
        self.context.stage_action_associations(event.event_id, 0, "Routine done.", [])
        with self.assertRaisesRegex(ContextError, "handoff note disposition"):
            self.context.publish(event.event_id)
        self.context.stage_handoff_note(event.event_id, "")
        self.context.publish(event.event_id)
        removal = self.context.begin_action("remove", "global", submitted_by="recorder")
        with self.assertRaisesRegex(ContextError, "previous action date"):
            self.context.stage_action_associations(removal.event_id, "2026-10-09", "Withdrawn.", [])
        self.context.stage_action_associations(removal.event_id, 0, "Withdrawn.", [])
        with self.assertRaisesRegex(ContextError, "handoff note disposition"):
            self.context.publish(removal.event_id)
        self.assertEqual(self.context.actions_history()["items"][0]["id"], event.event_id)
        self.context.stage_handoff_note(removal.event_id, "")
        self.context.publish(removal.event_id)

    def test_tags_validate_without_graph_binding_and_cli_has_no_old_parameter(self):
        for tags in ({}, [None], [{"id": "x", "name": "X"}],
                     [{"id": "", "name": "X", "business_id": None}],
                     [{"id": "x", "name": "", "business_id": None}],
                     [{"id": "x", "name": "X", "business_id": 3}], [ORDINARY, ORDINARY]):
            with self.subTest(tags=tags), self.assertRaises(ContextError):
                self.context.begin_action("add", "invalid", text="Done.", tags=tags, submitted_by="recorder")
        self.assertIsNone(self.context.pending())
        event, _ = self.publish("no-business-node", tags=[BUSINESS])
        self.assertEqual(self.context.actions_history(tag_id=BUSINESS["id"])["items"][0]["id"], event.event_id)
        self.assertEqual(self.context.snapshot().nodes, {})
        cli = parser()
        args = cli.parse_args(["--repo", str(self.context.root), "submit-action", "add", "new",
                               "--input-file", "synthetic.txt", "--tags", json.dumps([ORDINARY]),
                               "--submitted-by", "recorder"])
        self.assertEqual(args.tags, [ORDINARY])
        with self.assertRaisesRegex(ContextError, "unrecognized"):
            cli.parse_args(["--repo", str(self.context.root), "submit-action", "add", "new",
                            "--submitted-by", "recorder", "--business-entity-id", "north"])

    def test_cli_and_tool_read_same_published_history(self):
        first, commit = self.publish("visible", tags=[ORDINARY])
        pending = self.context.begin_action("add", "not-published", text="Pending.", submitted_by="recorder")
        tools = EventTools(self.context, pending.event_id)
        result = call(tools, "actions_history", tag_id=ORDINARY["id"], global_only=True, audit=True, limit=1)
        expected = self.context.actions_history(tag_id=ORDINARY["id"], global_only=True, audit=True, limit=1)
        self.assertEqual(result["items"], expected["items"])
        self.assertEqual(result["items"][0]["source"]["id"], first.source_id)
        self.assertEqual(result["items"][0]["commit"], commit)
        args = parser().parse_args(["--repo", str(self.context.root), "--runtime", str(self.context.runtime),
            "actions-history", "--tag-id", ORDINARY["id"], "--global-only", "--audit", "--limit", "1", "--revision", commit])
        self.assertEqual(execute(args), expected)

    def test_subjectless_publication_recovery_and_retry_use_same_event(self):
        def interpret(tools, payload):
            call(tools, "stage_action_associations", date=0, summary="Done.", associations=[])
            call(tools, "stage_handoff_note", text="")
            call(tools, "finish_propagation", status="complete", summary="Done.", reassessed=[])
        runner = ScriptRunner(interpret)
        pipeline = Pipeline(self.context, runner=runner)
        def stop(phase):
            if phase == "after_publication":
                raise ProcessStopped()
        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            pipeline.submit_action("add", "recover", text="Routine done.", tags=[ORDINARY],
                                   submitted_by="recorder", event_id="recov")
        before = list(runner.calls)
        receipt = pipeline.retry("recov")
        self.assertEqual(runner.calls, before)
        self.assertEqual(self.context.actions_history(audit=True)["total"], 1)
        self.assertEqual(self.context.actions_history()["items"][0]["commit"], receipt["published_commit"])
        self.assertIsNone(self.context.pending())

    def test_empty_correction_reassesses_prior_subject_and_withdrawal_keeps_history(self):
        first = self.context.begin_action("add", "corrected", text="North exists.",
                                          tags=[BUSINESS], submitted_by="recorder")
        self.context.put_node(first.event_id, {"id": "north", "summary": "North",
            "attributes": {"basic_entity_type": ["business"]}},
            explanation="Accepted assertion.", citations=["self"], support_after=["self"])
        self.context.stage_action_associations(first.event_id, 0, "North recorded.",
            [{"kind": "node", "id": "north", "explanation": "Subject of the assertion."}])
        self.context.stage_handoff_note(first.event_id, "")
        first_commit = self.context.publish(first.event_id)

        edit = self.context.begin_action("edit", "corrected", text="A request was sent; no business established.",
                                         tags=[BUSINESS], submitted_by="recorder", expected_previous_commit=first_commit)
        tools = EventTools(self.context, edit.event_id)
        self.assertEqual(tools.required_reassessment, [{"kind": "node", "id": "north"}])
        call(tools, "remove_node", id="north", explanation="Correction removes unsupported identity.", citations=["self"])
        call(tools, "stage_action_associations", date=0, summary="Request only; no suitable subject.", associations=[])
        call(tools, "stage_handoff_note", text="")
        self.assertFalse(tools.dispatch("finish_propagation", {"status": "complete", "summary": "Incomplete coverage.",
                                                              "reassessed": []})["ok"])
        call(tools, "finish_propagation", status="complete", summary="Prior subject reassessed.",
             reassessed=tools.required_reassessment)
        self.context.publish(edit.event_id)
        self.assertEqual(self.context.snapshot().nodes, {})
        self.assertEqual(self.context.snapshot().action_timeline("node", "north")["total"], 0)
        self.assertEqual(self.context.snapshot().action_timeline("node", "north", audit=True)["total"], 2)
        self.assertEqual(self.context.actions_history(tag_id=BUSINESS["id"])["items"][0]["id"], edit.event_id)

        withdrawal = self.context.begin_action("remove", "corrected", tags=[BUSINESS], submitted_by="recorder")
        tools = EventTools(self.context, withdrawal.event_id)
        self.assertEqual(tools.required_reassessment, [{"kind": "node", "id": "north"}])
        self.assertTrue(call(tools, "read_object", kind="node", id="north")["removed"])
        call(tools, "stage_action_associations", date=0, summary="Claim withdrawn.", associations=[])
        call(tools, "stage_handoff_note", text="")
        call(tools, "finish_propagation", status="complete", summary="Historical subject reassessed.",
             reassessed=tools.required_reassessment)
        self.context.publish(withdrawal.event_id)
        self.assertEqual(self.context.actions_history()["total"], 0)
        self.assertEqual(self.context.snapshot().action_timeline("node", "north", audit=True)["total"], 3)
        self.assertEqual(self.context.snapshot().source_change(first_commit)["after"],
                         self.context.snapshot(first_commit).files[f"foundation/actions/{first.source_id}.txt"])
