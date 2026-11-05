"""Format 2 compatibility: a legacy Context is read as it is, upgraded only by its first new publication, never migrated."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from qorqut_context import Conflict, Context, ContextError, Pipeline, recovery, supervised_edits
from qorqut_context.engine import atomic_write
from qorqut_context.recovery import RecoveryError
from qorqut_context.schema import encode
from . import legacy_format2 as legacy
from .test_capture_recovery import Barrier, Recovered, sha
from .test_graph_edits import NoModel, person, put
from .test_recovery import ProcessStopped
from .test_supervised_edits import DOCUMENT, note, plan

CONFIG = "graph/config.json"
OLD_EVENTS = ["graph/events/leg01.json", "graph/events/leg02.json"]
NOTE = "Jesse reportedly leads the Berlin studio from March; the date is unconfirmed."


def config(**values):
    return encode({"format_version": 2, "published_ref": "refs/heads/main", **values})


class LegacyCase(Recovered):
    """A format 2 Context replayed from its recorded history, and a pipeline that fails the test if a model is reached."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.git = legacy.build(self.directory / "legacy")
        self.context = Context(self.directory / "legacy", self.directory / "legacy-runtime")
        self.model = NoModel()
        self.pipeline = Pipeline(self.context, runner=self.model)
        self.barrier = Barrier()
        self.capture_directory = self.directory / "generation" / "context"
        self.capture_directory.parent.mkdir()
        self.context.search("Jesse")

    def proposal(self, base=legacy.HEAD):
        """A supervised edit that updates an existing node and adds a note linking a node and an edge."""
        return plan(base,
                    put("node", person("jesse", "Jesse", role=["studio lead"]), citations=["self", legacy.SECOND],
                        support=[legacy.FIRST, legacy.SECOND, "self"]),
                    note("notea", NOTE, [("node", "jesse"), ("edge", "mgr01")]))

    def edit(self, event_id="sup01"):
        return self.pipeline.submit_supervised(self.proposal(), content=DOCUMENT, submitted_by="owner", event_id=event_id)

    @contextmanager
    def crafted(self, files):
        """The published head temporarily replaced by a hand-made commit on top of it; the original returns afterwards."""
        head = self.git.head()
        commit = self.git.commit(files, self.directory, head, "crafted")
        self.git.publish(commit, head)
        try:
            yield
        finally:
            self.git.publish(head, commit)

    def configuration(self, revision):
        return json.loads(self.git.read(revision, [CONFIG])[CONFIG])["format_version"]

    def stopping(self, phase):
        def checkpoint(name):
            if name == phase:
                raise ProcessStopped()
        return patch.object(self.context, "_checkpoint", side_effect=checkpoint)


class ReadLegacy(LegacyCase):
    def test_a_legacy_snapshot_and_its_history_read_as_they_are(self):
        snapshot = self.context.snapshot()
        self.assertEqual((snapshot.revision, snapshot.format_version), (legacy.HEAD, 2))
        self.assertEqual((sorted(snapshot.nodes), sorted(snapshot.edges), snapshot.notes), (["alice", "jesse"], ["mgr01"], {}))
        self.assertEqual(snapshot.support[("node", "jesse")], [legacy.FIRST, legacy.SECOND])
        self.assertEqual({key: event["format_version"] for key, event in snapshot.events.items()}, {"leg01": 2, "leg02": 2})
        self.assertEqual(snapshot.event_commits, {"leg01": legacy.FIRST, "leg02": legacy.SECOND})
        source = snapshot.source_change(legacy.FIRST)
        self.assertEqual((source["before"], source["after"]), (None, b"Jesse manages Alice at Northstar."))
        history = snapshot.history("jesse", kind="node")
        self.assertEqual([item["event_id"] for item in history], ["leg01", "leg02"])
        self.assertEqual(history[1]["record_before"]["summary"], "Jesse")
        reassess = snapshot.supported_by(snapshot.events["leg01"]["source"]["id"])
        self.assertEqual([(item["kind"], item["id"]) for item in reassess], [("edge", "mgr01"), ("node", "alice"), ("node", "jesse")])
        earlier = self.context.snapshot(legacy.FIRST)
        self.assertEqual((earlier.format_version, earlier.nodes["jesse"]["summary"]), (2, "Jesse"))
        self.assertEqual([hit.id for hit in self.context.search("Berlin", kind="node")], ["jesse"])
        self.assertEqual(self.context.search("Berlin", kind="note"), [])

    def test_previews_invalid_and_abandoned_drafts_leave_the_legacy_configuration_alone(self):
        shown = self.context.preview_supervised(self.proposal(), content=DOCUMENT, submitted_by="owner", event_id="sup01")
        self.assertEqual((shown["status"], shown["result_counts"]["note"]["add"]), ("ready", 1))
        with self.assertRaises(Conflict):
            self.pipeline.submit_supervised(self.proposal(legacy.FIRST), content=DOCUMENT, submitted_by="owner", event_id="sup01")
        with self.assertRaises(ContextError):
            self.pipeline.submit_supervised(
                plan(legacy.HEAD, note("notea", NOTE, [("node", "nobody")])), content=DOCUMENT, submitted_by="owner", event_id="sup01")
        self.assertIsNone(self.context.pending())
        self.context.begin_supervised(self.proposal(), content=DOCUMENT, submitted_by="owner", event_id="sup01")
        for operation in self.context.pending_edit("sup01")["operations"]:
            supervised_edits.stage(self.context, "sup01", operation)
        self.context.stage_handoff_note("sup01", "")
        self.assertTrue(self.context.inspect_draft("sup01", validate=True)["validated"])
        self.context.discard("sup01")
        self.assertIsNone(self.context.pending())
        self.assertEqual((self.git.head(), self.configuration(legacy.HEAD), self.model.calls), (legacy.HEAD, 2, []))
        self.assertEqual(json.loads((self.context.root / CONFIG).read_bytes())["format_version"], 2)
        self.git.require_clean()


class UpgradeOnFirstPublication(LegacyCase):
    def test_the_first_supervised_publication_upgrades_the_configuration_in_its_own_commit(self):
        before = self.context.snapshot()
        jesse = before.history("jesse", kind="node")
        old_files = self.git.read(legacy.HEAD, OLD_EVENTS)
        receipt = self.edit()
        self.assertEqual((receipt["status"], receipt["model"], self.model.calls), ("published", None, []))
        head = self.git.head()
        self.assertEqual(receipt["published_commit"], head)
        self.assertEqual(self.git.run("rev-list", "--reverse", head).decode().split(),
                         [item["commit"] for item in legacy.COMMITS] + [head])
        changed = set(self.git.run("diff-tree", "--no-commit-id", "--name-only", "-r", legacy.HEAD, head).decode().split())
        self.assertEqual(changed, {CONFIG, "graph/events/sup01.json", "foundation/supervised-edits/sup01.md",
                                   "graph/nodes/jesse.json", "notes/notea.json"})
        self.assertEqual((self.configuration(legacy.HEAD), self.configuration(head)), (2, 3))
        self.assertEqual(self.git.read(head, OLD_EVENTS), old_files)

        snapshot = self.context.snapshot()
        self.assertEqual(snapshot.format_version, 3)
        self.assertEqual(snapshot.event_commits, {"leg01": legacy.FIRST, "leg02": legacy.SECOND, "sup01": head})
        self.assertEqual({key: event["format_version"] for key, event in snapshot.events.items()},
                         {"leg01": 2, "leg02": 2, "sup01": 3})
        self.assertEqual({(item["kind"], item["id"], item["operation"]) for item in snapshot.events["sup01"]["changes"]},
                         {("node", "jesse", "edit"), ("note", "notea", "add")})
        self.assertEqual(snapshot.history("jesse", kind="node")[:2], jesse)
        self.assertEqual(snapshot.history("jesse", kind="node")[2]["event_id"], "sup01")
        self.assertEqual(snapshot.history("notea", kind="note")[0]["record_after"]["text"], NOTE)
        self.assertEqual([hit.id for hit in self.context.search("Berlin", kind="note")], ["notea"])
        self.assertEqual(snapshot.linked_notes("edge", "mgr01")["items"][0]["record"]["id"], "notea")
        for revision in (legacy.FIRST, legacy.HEAD):
            earlier = self.context.snapshot(revision)
            self.assertEqual((earlier.format_version, earlier.notes), (2, {}))
        self.assertEqual(json.loads((self.context.root / CONFIG).read_bytes())["format_version"], 3)
        self.assertIsNone(self.context.pending())
        self.git.require_clean()

    def test_an_interruption_before_the_ref_moves_retries_and_upgrades(self):
        with self.stopping("before_publication"), self.assertRaises(ProcessStopped):
            self.edit()
        self.assertEqual((self.git.head(), self.context.pending()["phase"]), (legacy.HEAD, "ready"))
        self.assertEqual(self.context.recover()["status"], "needs_retry")
        self.assertEqual(self.configuration(legacy.HEAD), 2)
        receipt = self.pipeline.retry("sup01")
        self.assertEqual((receipt["status"], self.configuration(self.git.head())), ("published", 3))
        self.assertEqual(sorted(self.context.snapshot().notes), ["notea"])

    def test_an_interruption_after_the_ref_moves_completes_the_upgrade(self):
        with self.stopping("after_publication"), self.assertRaises(ProcessStopped):
            self.edit()
        head = self.git.head()
        self.assertEqual((self.configuration(head), self.context.snapshot().format_version), (3, 3))
        self.assertEqual(json.loads((self.context.root / CONFIG).read_bytes())["format_version"], 2)
        self.assertEqual(self.context.recover(), {"status": "completed", "commit": head, "event_id": "sup01"})
        self.assertEqual(json.loads((self.context.root / CONFIG).read_bytes())["format_version"], 3)
        self.assertIsNone(self.context.pending())
        self.assertEqual([hit.id for hit in self.context.search("Berlin", kind="note")], ["notea"])
        self.git.require_clean()


class Impersonation(LegacyCase):
    def recorded(self, path, revision=legacy.HEAD):
        return json.loads(self.git.read(revision, [path])[path])

    def event(self, **values):
        """A copy of the recorded second event under a new ID, as a file of a crafted commit."""
        return {"graph/events/leg03.json": encode({**self.recorded(OLD_EVENTS[1]), "id": "leg03", **values})}

    def refused(self, files, message):
        with self.crafted(files), self.assertRaisesRegex(ContextError, message):
            self.context.snapshot()

    def test_only_the_two_exact_configurations_are_read(self):
        for version in (1, 4, "2", 2.0, True, None, [2]):
            with self.subTest(version=version):
                self.refused({CONFIG: config(format_version=version)}, "Unsupported context format version")
        self.refused({CONFIG: None}, "Missing or invalid record")
        self.refused({CONFIG: config(published_ref="refs/heads/other")}, "not the supported format 2 configuration")
        self.refused({CONFIG: config(extra=True)}, "not the supported format 2 configuration")

    def test_format_2_data_admits_no_notes_supervised_edits_or_other_formats(self):
        record = {"id": "noteb", "text": "A remark.", "objects": []}
        self.refused({"notes/noteb.json": encode(record)}, "format 2 context cannot hold notes")
        self.refused(self.event(format_version=3), "A published event is not format 2")
        self.refused({**self.event(), CONFIG: config(format_version=3)}, "A published event is not format 3")
        changes = self.recorded(OLD_EVENTS[1])["changes"]
        self.refused(self.event(changes=[{**changes[0], "kind": "note"}]), "unsupported change kind")
        marked = self.event(source={"id": "zzzzz", "path": supervised_edits.source_path("leg03"),
                                    **supervised_edits.metadata("leg03")},
                            execution={"kind": supervised_edits.EXECUTION, "plan_digest": "0" * 64,
                                       "document_digest": "0" * 64, "plan": {}})
        self.refused(marked, "format 2 event cannot be a supervised edit")

    def test_a_changed_version_cannot_move_published_history_between_formats(self):
        self.edit()
        head = self.git.head()
        old, new = (self.recorded(path, head) for path in (OLD_EVENTS[0], "graph/events/sup01.json"))
        self.refused({OLD_EVENTS[0]: encode({**old, "format_version": 3})}, "A published event is not format 2")
        self.refused({"graph/events/sup01.json": encode({**new, "format_version": 2})}, "A published event is not format 3")
        self.refused({CONFIG: config(), "notes/notea.json": None}, "format decreased along the published history")
        self.assertEqual(self.context.snapshot().format_version, 3)


class RefuseLegacyPending(LegacyCase):
    def test_an_unfinished_format_2_job_is_refused_with_guidance_and_never_converted(self):
        job = encode({"format_version": 2, "id": "leg03", "phase": "draft", "error": None})
        atomic_write(self.context.job_path, job)
        for operation in (lambda: self.context.pending(), lambda: self.context.recover(),
                          lambda: self.context.publish("leg03"), lambda: self.context.discard("leg03"),
                          lambda: self.context.inspect_draft("leg03"),
                          lambda: self.context.begin("add", "foundation/new.md", content=b"x", submitted_by="owner"),
                          lambda: self.pipeline.submit_supervised(self.proposal(), content=DOCUMENT, submitted_by="owner",
                                                                  event_id="sup01")):
            with self.assertRaisesRegex(Conflict, "format version 2; this engine supports format 3 only; "
                                                  "complete this unfinished format 2 job with the older engine before switching"):
                operation()
            self.assertEqual((self.context.job_path.read_bytes(), self.git.head()), (job, legacy.HEAD))
        with self.assertRaises(RecoveryError) as raised:
            self.capture()
        self.assertEqual(raised.exception.code, "source_invalid")
        self.assertIn("older engine", str(raised.exception))
        self.assertFalse(self.capture_directory.exists())
        self.assertEqual(self.configuration(self.git.head()), 2)


class CaptureLegacyAndUpgraded(LegacyCase):
    def tampered(self, directory, **component):
        manifest = json.loads((directory / "manifest.json").read_bytes())
        manifest["component"].update(component)
        data = recovery.encode(manifest)
        (directory / "manifest.json").write_bytes(data)
        return sha(data)

    def test_legacy_and_upgraded_heads_capture_verify_and_restore_and_a_mismatched_format_is_refused(self):
        before = self.authority(self.context)
        legacy_directory = self.capture_directory
        summary = self.capture()
        self.assertEqual(json.loads((legacy_directory / "manifest.json").read_bytes())["component"]["context_format"], 2)
        self.assertEqual(recovery.verify(legacy_directory, expected_manifest_sha256=summary["manifest_sha256"])["published_commit"],
                         legacy.HEAD)
        self.restore(summary)
        repository, runtime = self.targets()
        restored = Context(repository, runtime)
        self.assertEqual((restored.snapshot().format_version, self.authority(restored)), (2, before))

        self.edit()
        later = self.authority(self.context)
        self.capture_directory = self.directory / "generation2" / "context"
        self.capture_directory.parent.mkdir()
        upgraded = self.capture()
        self.assertEqual(json.loads((self.capture_directory / "manifest.json").read_bytes())["component"]["context_format"], 3)
        self.assertEqual(recovery.verify(self.capture_directory, expected_manifest_sha256=upgraded["manifest_sha256"])["events"], 3)
        self.restore(upgraded, name="restored-upgraded")
        repository, runtime = self.targets("restored-upgraded")
        copy = Context(repository, runtime)
        self.assertEqual((copy.snapshot().format_version, sorted(copy.snapshot().notes), self.authority(copy)),
                         (3, ["notea"], later))
        self.assertEqual(copy.snapshot().event_commits["leg01"], legacy.FIRST)

        for directory, wrong in ((legacy_directory, 3), (self.capture_directory, 2)):
            with self.subTest(wrong=wrong):
                digest = self.tampered(directory, context_format=wrong)
                with self.assertRaises(RecoveryError) as raised:
                    recovery.verify(directory, expected_manifest_sha256=digest)
                self.assertEqual(raised.exception.code, "invalid_capture")
        for wrong in (1, 4, "3", 3.0, True, None):
            with self.subTest(wrong=wrong):
                digest = self.tampered(legacy_directory, context_format=wrong)
                with self.assertRaises(RecoveryError) as raised:
                    recovery.verify(legacy_directory, expected_manifest_sha256=digest)
                self.assertEqual(raised.exception.code, "invalid_capture")
