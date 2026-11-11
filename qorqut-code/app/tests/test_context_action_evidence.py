"""New Action evidence through real admission, authorized reads and Context publication."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

from qorqut_context import Context
from qorqut_context.integration.action_evidence import EXCERPT_BYTES, ActionEvidence
from qorqut_context.integration.driver import submission_of
from qorqut_context.integration.queue.queue import action_payload, enqueue_action, input_view
from qorqut_context.integration.service import ContextLocations, ContextSettings, compose_context
from qorqut_context.pipeline import PropagationError
from qorqut_context.runner import RunResult, RunnerConfig
from qorqut_operations.actions.operations import ActionOperations
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, PolicyDecision, new_id, plain
from qorqut_operations.documents.library import SourceLibrary
from qorqut_operations.documents.operations import DocumentSources
from qorqut_operations.documents.store import DocumentStore
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks import TaskOperations, TaskPorts
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed


def noop(*args, **kwargs):
    return None


class Policy:
    def __init__(self):
        self.denied = set()

    def resolve(self, ctx, descriptor, targets, uow):
        return PolicyDecision("deny" if descriptor.name in self.denied else "allow", "test")


class FilingRunner:
    """Deterministic model boundary: tests transport/publication, not an LLM quality score."""
    config = RunnerConfig()

    def __init__(self):
        self.calls = []
        self.fail_once = False

    def run(self, *, stage, instructions, payload, directory, tools=None, **kwargs):
        self.calls.append((stage, deepcopy(payload), instructions))
        if stage == "bootstrap":
            output = json.dumps({"node_ids": [item["record"]["id"] for item in payload["candidates"]]})
        elif self.fail_once:
            self.fail_once = False
            return RunResult("failed", "", "test-thread", "test-turn", self.config.model)
        else:
            meta = payload["source_change"]["record_context"]["provenance"]["action_metadata"]
            for name, args in (
                ("stage_action_associations", {"date": meta["occurred"]["at"],
                    "summary": "Owner sent the Example project request.", "associations": []}),
                ("stage_handoff_note", {"text": ""}),
                ("finish_propagation", {"status": "complete", "summary": "Filed the supplied occurrence and evidence.",
                                       "reassessed": []}),
            ):
                result = tools.dispatch(name, args)
                if not result["ok"]:
                    raise AssertionError(result)
            output = ""
        return RunResult("completed", output, "test-thread", "test-turn", self.config.model)


class ActionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "state.sqlite", assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.business, self.document = new_id(), new_id(), new_id()
        self.ctx = InvocationContext(Principal(self.owner, "human"), source="dashboard",
            allowed_business_ids=frozenset({self.business}), global_capabilities=frozenset({"read_global_context"}))
        with self.db.transaction() as uow:
            uow.repository("principals").insert({"id": self.owner, "kind": "human", "display_name": "Synthetic owner"})
            uow.repository("installation").insert({"id": "service", "installation_id": new_id(),
                                                   "owner_principal_id": self.owner})
            uow.repository("businesses").insert({"id": self.business, "name": "Example", "slug": "example",
                                                 "workspace": "example", "hq_path": "hq"})
            uow.repository("business_memberships", business_id=self.business).insert(
                {"principal_id": self.owner, "is_owner": True})
            uow.repository("tasks", business_id=None).insert({"id": "evidence-task", "title": "Example request",
                "status": "working", "created_by": self.owner,
                "fields_json": {"$task": {"overview": "Request details: https://example.test/evidence"}}})
        self.registry, self.policy = OperationRegistry(), Policy()
        self.app = Application(self.db, self.registry, policy=self.policy)
        self.runner = FilingRunner()
        locations = ContextLocations(self.root / "context", self.root / "runtime", self.root / "questions.sqlite",
                                     self.root / "locks", self.root / "operations")
        self.composed = compose_context(self.app, locations, ContextSettings(runner=self.runner),
                                       instruct_roles=(), coordinator=lambda uow: self.owner)
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution("test", ("task",), lookup)], candidates=NS(candidates=lambda *_: []))
        ports = TaskPorts(assignees=NS(protect=noop, owner_directed=lambda *_: False, worker_excluded=lambda *_: False),
            focus=NS(schema={}, creation_access=noop, present_created=noop), incidents=NS(link_task=noop),
            references=NS(types=(), host_types=(), resolve=noop, derived=noop),
            routing=NS(routes=lambda *_: ((), ()), snooze_routes=lambda *_: ()))
        self.tasks = TaskOperations(self.app, items, ports)
        self.actions = ActionOperations(self.app, self.tasks.note_references(), on_revision=self.composed.on_revision,
                                        clock=lambda: datetime(2026, 11, 10, 12, tzinfo=timezone.utc))
        for contribution in (self.tasks.contribution(), self.actions.contribution()):
            for entry in contribution.entries:
                self.registry.declare(entry)
            for descriptor in contribution.descriptors:
                self.registry.register(descriptor)
        self.store = DocumentStore(self.db, operations_root=self.root / "sources",
                                  document_root=lambda business, path: self.root / "sources")
        self.library = SourceLibrary(self.store)
        sources = DocumentSources(self.store, self.library, agent_workspace=noop, scope_root=noop)
        self.registry.register(sources.descriptors["context_get"])
        self.registry.freeze()
        self.path = self.root / "sources/context/evidence.md"
        self.path.parent.mkdir(parents=True)
        self.path.write_text("The request concerns Example. Evidence: https://example.test/evidence\n")
        with self.db.transaction() as uow:
            uow.repository("documents", business_id=self.business).insert({
                "id": self.document, "namespace": "business:" + self.business, "kind": "context_note",
                "path": "context/evidence.md", "content_hash": hashlib.sha256(self.path.read_bytes()).hexdigest(),
                "metadata_json": {"title": "Example request evidence"}})

    def invoke(self, name, args, **options):
        result = self.app.invoke(self.ctx, name, args, {"idempotency_key": new_id(), **options})
        self.assertEqual("ok", result.status, result.error)
        return plain(result.data)

    def record(self, **changes):
        return self.invoke("actions_record", {"text": "Owner sent the Example project request.",
            "category": "business-operational", "business_ids": [self.business], "real_time": "2026-11-04T10",
            "evidence": {"task": "evidence-task", "document": self.document, "url": "https://example.test/evidence",
                         "signoff": "accepted-signoff"}, **changes})

    def queued(self, result):
        with self.db.read() as uow:
            row = uow.connection.execute("SELECT * FROM context_inputs WHERE id=?", (result["context_input_id"],)).fetchone()
            return input_view(row, payload=True)

    def revision(self, result):
        payload = self.queued(result)["payload"]
        with self.db.read() as uow:
            return uow.repository("action_revisions", business_id=None).get(
                {"action_key": payload["action_ref"], "revision": result["revision"]})

    def metadata(self, result):
        return self.queued(result)["payload"]["provenance"]["action_metadata"]

    def test_admission_keeps_exact_revision_and_versioned_authorized_excerpts(self):
        result = self.record()
        revision, meta = self.revision(result), self.metadata(result)
        for field in ("occurred", "refs", "business_ids"):
            self.assertEqual(revision["snapshot_json"][field], meta[field])
        self.assertEqual(revision["created_at"], meta["recorded_at"])
        self.assertEqual("hour", meta["occurred"]["precision"])
        task, document = meta["evidence"]
        self.assertEqual(["available", "available"], [item["status"] for item in meta["evidence"]])
        self.assertIn("Request details", task["excerpt"])
        self.assertEqual(self.path.read_text(), document["excerpt"])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), document["content_hash"])
        self.assertEqual(self.business, document["business_id"])
        self.assertEqual(1, document["revision"])
        self.assertEqual({"kind": "global"}, self.queued(result)["payload"]["scope"])
        self.assertNotIn("notes", task)
        envelope = action_payload(revision)
        envelope["provenance"]["action_metadata"]["refs"]["url"] = "changed"
        self.assertEqual("https://example.test/evidence", revision["snapshot_json"]["refs"]["url"])

    def test_supplied_metadata_cannot_forge_the_canonical_queue_payload(self):
        result = self.record()
        revision = self.revision(result)
        payload = action_payload(revision)
        payload["provenance"]["action_metadata"]["evidence"] = [{"excerpt": "forged"}]
        with self.db.transaction() as uow:
            with self.assertRaises(DomainError):
                enqueue_action(uow, action_id=revision["action_key"], revision=revision["revision"],
                               operation="add", payload=payload, capture_evidence=lambda _: self.fail("read before validation"))

    def test_policy_denial_scope_and_private_documents_do_not_leak_content(self):
        self.policy.denied = {"tasks_get", "context_get"}
        denied = self.metadata(self.record())["evidence"]
        self.assertEqual(["unavailable", "unavailable"], [item["status"] for item in denied])
        self.assertTrue(all(set(item) == {"kind", "id", "status", "observed_at"} for item in denied))
        self.policy.denied.clear()
        self.ctx = replace(self.ctx, allowed_business_ids=frozenset())
        # Preserve a reference without granting access or a business association.
        denied = self.metadata(self.record(category="global-operational", business_ids=[]))["evidence"]
        self.assertEqual("unavailable", denied[1]["status"])
        self.ctx = replace(self.ctx, allowed_business_ids=frozenset({self.business}))
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE documents SET kind='journal' WHERE id=?", (self.document,))
        private = self.metadata(self.record())["evidence"][1]
        self.assertEqual({"kind", "id", "status", "observed_at"}, set(private))
        self.assertEqual("unavailable", private["status"])

    def test_changed_bytes_are_unavailable_and_excerpts_have_utf8_byte_bounds(self):
        self.path.write_text("Unregistered changed bytes")
        self.assertEqual("unavailable", self.metadata(self.record())["evidence"][1]["status"])
        body = "é" * 6000
        self.path.write_text(body)
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE documents SET content_hash=?,revision=2 WHERE id=?",
                                   (hashlib.sha256(self.path.read_bytes()).hexdigest(), self.document))
            uow.connection.execute("UPDATE tasks SET fields_json=?,revision=2 WHERE id=?",
                                   (json.dumps({"$task": {"overview": body}}), "evidence-task"))
        for item in self.metadata(self.record())["evidence"]:
            self.assertEqual(2, item["revision"])
            self.assertEqual(EXCERPT_BYTES, len(item["excerpt"].encode("utf-8")))
            self.assertTrue(item["truncated"])

    def test_task_ids_resolve_in_scope_and_ambiguous_references_stay_unavailable(self):
        result = self.record()
        revision = self.revision(result)
        with self.db.transaction() as uow:
            uow.repository("tasks", business_id=self.business).insert(
                {"id": "evidence-task", "title": "Business task", "status": "open", "created_by": self.owner})
            evidence = ActionEvidence(self.app)(self.ctx, revision, uow)
            self.assertIsNone(evidence[0]["business_id"])  # the Action owns the Global ID
            revision["business_id"] = self.business
            self.assertEqual(self.business, ActionEvidence(self.app)(self.ctx, revision, uow)[0]["business_id"])
            revision["business_id"] = new_id()
            self.assertEqual("unavailable", ActionEvidence(self.app)(self.ctx, revision, uow)[0]["status"])

    def test_correction_gets_new_evidence_withdrawal_does_not_reread_and_old_input_is_immutable(self):
        first = self.record()
        original = self.queued(first)
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET title='Revised request',revision=2 WHERE id=?", ("evidence-task",))
        corrected = self.invoke("actions_correct", {"action_id": original["payload"]["action_ref"],
            "text": "Owner sent the corrected Example project request.", "real_time": "2026-11-05"},
            expected_revision=1)
        self.assertEqual(2, self.metadata(corrected)["evidence"][0]["revision"])
        self.assertEqual("day", self.metadata(corrected)["occurred"]["precision"])
        withdrawn = self.invoke("actions_withdraw", {"action_id": original["payload"]["action_ref"]}, expected_revision=2)
        self.assertTrue(self.metadata(withdrawn)["withdrawn"])
        self.assertNotIn("evidence", self.metadata(withdrawn))
        self.assertNotIn("text", self.queued(withdrawn)["payload"])
        self.assertEqual(original, self.queued(first))

    def test_metadata_reaches_both_model_stages_retry_and_published_source(self):
        result = self.record()
        queued = self.queued(result)
        context = Context.initialize(self.composed.locations.repository, self.composed.locations.runtime)
        self.runner.fail_once = True
        args, kwargs = submission_of(queued, "ev001")
        with self.assertRaises(PropagationError):
            self.composed.pipeline.submit_action(*args, **kwargs)
        # Neither a retry nor publication can observe a later document/task mutation.
        self.path.write_text("Later contents that must never reach this filing")
        with self.db.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET title='Later task',revision=2 WHERE id=?", ("evidence-task",))
        receipt = self.composed.pipeline.retry("ev001")
        self.assertEqual("published", receipt["status"], receipt)
        self.assertEqual(["bootstrap", "propagation", "propagation"], [call[0] for call in self.runner.calls])
        expected = queued["payload"]["provenance"]["action_metadata"]
        for _, payload, _ in self.runner.calls:
            self.assertEqual(expected, payload["source_change"]["record_context"]["provenance"]["action_metadata"])
        snapshot = context.snapshot()
        event = snapshot.events["ev001"]
        self.assertEqual(expected, event["record_context"]["provenance"]["action_metadata"])
        source = json.loads(snapshot.files[event["source"]["path"]])
        self.assertEqual(queued["payload"]["revision_ref"], source["revision_ref"])
        self.assertEqual(expected, source["record_context"]["provenance"]["action_metadata"])
        action = snapshot.actions[queued["payload"]["action_ref"]]
        self.assertNotEqual(0, action["action_associations"]["date"])
        # The fake runner supplies a grounded date and no questions; this verifies the publication contract.
        self.assertEqual([], context.question_output("ev001")["questions"])

    def test_default_occurrence_and_legacy_driver_inputs_remain_distinguishable(self):
        args = {"text": "Owner sent the Example project request.", "category": "business-operational",
                "business_ids": [self.business]}
        result = self.invoke("actions_record", args)
        meta = self.metadata(result)
        self.assertEqual("instant", meta["occurred"]["precision"])
        self.assertEqual(meta["recorded_at"], meta["occurred"]["at"])
        legacy = deepcopy(self.queued(result))
        del legacy["payload"]["provenance"]["action_metadata"]
        _, kwargs = submission_of(legacy, "old01")
        self.assertEqual(legacy["payload"]["provenance"], kwargs["record_context"]["provenance"])


if __name__ == "__main__":
    unittest.main()
