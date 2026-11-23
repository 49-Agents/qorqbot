"""The owner's record has its own two tools: every agent reads it as plain facts, and Ordubashy changes it by naming facts.

``personal_get`` is a read of the published record alone. ``personal_set`` builds the same queued graph edit ``context_edit``
takes, from the facts that change, and refuses at once when the shared Context queue holds anything that would make it stale.
The last class runs both through the real application, store and Context."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from qorqut_context import Pipeline
from qorqut_context.engine import Context
from qorqut_context.integration.filing.source_operations import ContextSourceOperations
from qorqut_context.integration.graph_edits import personal_edit
from qorqut_context.integration.graph_edits.graph_edit import GraphEditPorts, GraphEdits
from qorqut_context.integration.reading import SharedContextReader, shared_context_contribution
from qorqut_context.integration.registration import personal_rule
from qorqut_operations.application.plans import utc_clock
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationOptions, PolicyDecision, new_id, plain

from qorqut.composition.catalog import builtin_registry, install_contribution
from qorqut.composition.policy import resolve_operation_policy

from private_thread_runtime import build_runtime

RECORD = {"id": "own3r", "summary": "Sam Example is the installation owner.",
          "attributes": {"basic_entity_type": ["person"], "phone": ["+1 555 0100"], "email": ["sam@example.com"]}}
SAID = {"statement": "My new number is +1 555 0199.", "source": "thread:example", "date": "2026-11-20"}


def changes(*facts, **more):
    return {"facts": list(facts), **SAID, **more}


class NamedFactsTests(unittest.TestCase):
    def test_add_keeps_what_the_field_holds_and_every_other_field(self):
        record, changed = personal_edit.merged(RECORD, changes({"field": "phone", "values": ["+1 555 0199", "+1 555 0100"]}))
        self.assertEqual(record["attributes"]["phone"], ["+1 555 0100", "+1 555 0199"])
        self.assertEqual(record["attributes"]["email"], ["sam@example.com"])
        self.assertEqual(record["attributes"]["basic_entity_type"], ["person"])
        self.assertEqual((record["id"], record["summary"], changed), ("own3r", RECORD["summary"], ["phone"]))

    def test_replace_is_a_correction_and_remove_takes_values_or_the_field_out(self):
        record, changed = personal_edit.merged(RECORD, changes(
            {"field": "phone", "values": ["+1 555 0199"], "mode": "replace"}, {"field": "email", "mode": "remove"}))
        self.assertEqual(record["attributes"], {"basic_entity_type": ["person"], "phone": ["+1 555 0199"]})
        self.assertEqual(changed, ["phone", "email"])
        record, _ = personal_edit.merged(RECORD, changes({"field": "phone", "values": ["+1 555 0100"], "mode": "remove"}))
        self.assertNotIn("phone", record["attributes"])

    def test_a_field_named_in_another_case_is_the_same_field(self):
        record, changed = personal_edit.merged(RECORD, changes({"field": " Phone ", "values": ["+1 555 0199"]}))
        self.assertEqual(sorted(record["attributes"]), ["basic_entity_type", "email", "phone"])
        self.assertEqual(changed, ["phone"])

    def test_what_the_record_already_holds_changes_nothing(self):
        for fact in ({"field": "phone", "values": ["+1 555 0100"]}, {"field": "fax", "mode": "remove"},
                     {"field": "email", "values": ["sam@example.com"], "mode": "replace"}):
            self.assertEqual(personal_edit.merged(RECORD, changes(fact)), (RECORD, []))

    def test_a_summary_alone_is_a_change(self):
        record, changed = personal_edit.merged(RECORD, {**SAID, "summary": "Sam Example, founder."})
        self.assertEqual((record["summary"], record["attributes"], changed),
                         ("Sam Example, founder.", RECORD["attributes"], ["summary"]))

    def test_the_type_marker_a_field_named_twice_and_an_empty_add_are_refused(self):
        for facts in ([{"field": "basic_entity_type", "values": ["company"], "mode": "replace"}],
                      [{"field": "phone", "values": ["1"]}, {"field": "PHONE", "values": ["2"]}],
                      [{"field": "phone", "values": []}], [{"field": "phone", "values": ["  "]}]):
            with self.assertRaises(DomainError) as refused:
                personal_edit.merged(RECORD, changes(*facts))
            self.assertEqual(refused.exception.code, "validation_error")


def snapshot(support=("a" * 40,)):
    return SimpleNamespace(personal_context_node="own3r", revision="b" * 40, nodes={"own3r": RECORD},
                           support={("node", "own3r"): list(support)})


class DerivedPlanTests(unittest.TestCase):
    def test_the_plan_is_one_put_of_the_whole_record_at_the_published_revision(self):
        plan, changed = personal_edit.plan_for(snapshot(), changes({"field": "phone", "values": ["+1 555 0199"]}))
        operation, = plan["operations"]
        self.assertEqual((plan["format"], plan["expected_revision"], changed), ("qorqut-graph-edit-v1", "b" * 40, ["phone"]))
        self.assertEqual((operation["op"], operation["record"]["id"]), ("put_node", "own3r"))
        self.assertEqual(operation["record"]["attributes"]["email"], ["sam@example.com"])
        # The record's present support is kept and this edit is cited as its own source.
        self.assertEqual((operation["citations"], operation["support_after"]), (["self"], ["a" * 40, "self"]))

    def test_the_statement_its_source_and_its_date_are_stored_with_the_change(self):
        plan, _ = personal_edit.plan_for(snapshot(), changes({"field": "phone", "values": ["+1 555 0199"]}))
        self.assertEqual(plan["rationale"], "My new number is +1 555 0199. Stated 2026-11-20. Source: thread:example.")
        explanation = plan["operations"][0]["explanation"]
        self.assertIn("phone", explanation)
        self.assertIn("Stated 2026-11-20. Source: thread:example.", explanation)
        long = personal_edit.plan_for(snapshot(), changes({"field": "phone", "values": ["1"]}, source="word " * 99))[0]
        self.assertLessEqual(len(long["operations"][0]["explanation"].split()), personal_edit.EXPLANATION_WORDS)

    def test_nothing_to_change_and_no_record_are_refused_before_anything_is_queued(self):
        with self.assertRaises(DomainError) as refused:
            personal_edit.plan_for(snapshot(), changes({"field": "phone", "values": ["+1 555 0100"]}))
        self.assertEqual(refused.exception.code, "no_change")
        with self.assertRaises(DomainError) as refused:
            personal_edit.plan_for(SimpleNamespace(personal_context_node=None, nodes={}, support={}, revision="b" * 40),
                                   changes({"field": "phone", "values": ["1"]}))
        self.assertEqual(refused.exception.code, "operation_unavailable")


class AuthorityTests(unittest.TestCase):
    def decide(self, role, edit="allow", **maps):
        resolve = lambda name, scope=None: PolicyDecision(edit, "test")      # noqa: E731
        return personal_rule("personal_set", maps.get("defaults", {}), maps.get("overrides", {}), None, role_id=role,
                             scope="global", resolve=resolve, coordinator_role="ordubashy")

    def test_only_the_coordinator_role_and_only_with_its_context_edit_authority(self):
        self.assertEqual(self.decide("ordubashy").mode, "allow")
        self.assertEqual(self.decide("ordubashy", edit="deny").mode, "deny")
        self.assertEqual(self.decide("ordubashy", edit="signoff").mode, "deny")
        self.assertEqual(self.decide("global-worker").mode, "deny")
        self.assertEqual(self.decide("ordubashy", overrides={"personal_set": "deny"}).mode, "deny")

    def test_the_rule_decides_nothing_else(self):
        self.assertIsNone(personal_rule("context_edit", {}, {}, None, role_id="ordubashy", scope="global",
                                        resolve=None, coordinator_role="ordubashy"))

    def test_the_host_gives_every_agent_the_read_and_only_ordubashy_the_write(self):
        registry, granted = builtin_registry(), {"personal_set": "allow", "context_edit": "allow"}
        decide = lambda name, defaults, role: resolve_operation_policy(      # noqa: E731
            name, defaults, {}, registry, role_id=role, scope="global").mode
        self.assertEqual(decide("personal_get", {}, "global-worker"), "allow")
        self.assertEqual(decide("personal_set", granted, "global-worker"), "deny")
        # Ordubashy needs no new grant: its existing context_edit authority is the grant.
        self.assertEqual(decide("personal_set", {"context_edit": "allow"}, "ordubashy"), "allow")
        # It still follows context_edit: a role that denies the edit denies this. A role that does not name the edit holds it
        # through the coordinator's owner parity, so the write is allowed too.
        self.assertEqual(decide("personal_set", {"context_edit": "deny"}, "ordubashy"), "deny")
        self.assertEqual(decide("personal_set", {}, "ordubashy"), "allow")


def role(name):
    return ArtifactRef(name, "1", "a" * 64)


class BothToolsOnARealInstallationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.runtime = runtime = build_runtime(root)
        self.addCleanup(runtime.database.close)
        # The fixture's policy allows everything, so what refuses a worker below is the handler's own check.
        runtime.application.policy.refresh_context = lambda ctx, uow: ctx
        self.context = Context.initialize(root / "context", root / "context-runtime")
        self.pipeline = Pipeline(self.context)
        reader = SharedContextReader(root / "context", root / "context-runtime")
        self.addCleanup(reader.close)
        coordinator = lambda uow: uow.connection.execute(        # noqa: E731
            "SELECT management_coordinator_id FROM installation WHERE id='service'").fetchone()[0]
        edits = GraphEdits(runtime.application, GraphEditPorts(self.context), roles={"ordubashy", "global-worker"},
                           coordinator=coordinator)
        ContextSourceOperations(filing=None, clock=utc_clock, graph_edits=edits,
                                instruct_roles={"ordubashy", "global-worker"}).register(runtime.application.registry)
        install_contribution(runtime.application.registry, shared_context_contribution(reader))
        self.lead = replace(runtime.coordinator, role_artifact_ref=role("ordubashy"))
        self.worker = replace(runtime.worker, role_artifact_ref=role("global-worker"))

    def call(self, ctx, operation, arguments=None):
        return self.runtime.application.invoke(ctx, operation, arguments or {}, InvocationOptions(idempotency_key=new_id()))

    def ok(self, ctx, operation, arguments=None):
        result = self.call(ctx, operation, arguments)
        self.assertEqual(result.status, "ok", plain(result.error))
        return plain(result.data)

    def publish(self, queued):
        """What the queue driver does with the input: run exactly the stored plan as its reserved event."""
        with self.runtime.database.transaction() as uow:
            row = uow.connection.execute("SELECT plan_json, reserved_event_id FROM context_graph_edits WHERE input_id=?",
                                         (queued["input"]["id"],)).fetchone()
        self.pipeline.submit_edit(json.loads(row["plan_json"]), submitted_by=self.lead.principal_id,
                                  event_id=row["reserved_event_id"],
                                  record_context={"scope": {"kind": "global"}, "provenance": {"operation": "personal_set"}})

    def test_a_worker_reads_what_ordubashy_saved_by_naming_only_the_new_fact(self):
        empty = self.ok(self.worker, "personal_get")
        self.assertEqual((empty["facts"], empty["fields"], empty["summary"]), ({}, [], "Installation owner"))
        queued = self.ok(self.lead, "personal_set", changes({"field": "phone", "values": ["+1 555 0199"]},
                                                           {"field": "email", "values": ["sam@example.com"]}))
        self.assertEqual((queued["status"], queued["fields"], queued["input"]["state"]), ("queued", ["phone", "email"], "queued"))
        self.assertEqual(queued["execution"]["expected_revision"], empty["revision"])
        self.assertEqual(self.ok(self.worker, "personal_get")["facts"], {})          # queued is not published
        self.publish(queued)
        record = self.ok(self.worker, "personal_get")
        self.assertEqual(record["facts"], {"phone": ["+1 555 0199"], "email": ["sam@example.com"]})
        self.assertEqual((record["fields"], record["ref"]), (["email", "phone"], empty["ref"]))
        self.assertNotEqual(record["revision"], empty["revision"])
        only = self.ok(self.worker, "personal_get", {"fields": ["Phone", "passport"]})
        self.assertEqual((only["facts"], only["missing"], only["fields"]),
                         ({"phone": ["+1 555 0199"]}, ["passport"], ["email", "phone"]))
        # The record stays the engine's person node: the type marker the caller never sent is still on it.
        self.assertEqual(self.context.snapshot().nodes[record["ref"].split(":")[1]]["attributes"]["basic_entity_type"], ["person"])

    def test_the_edit_is_attributed_to_the_tool_that_built_it(self):
        queued = self.ok(self.lead, "personal_set", changes({"field": "phone", "values": ["+1 555 0199"]}))
        with self.runtime.database.transaction() as uow:
            row = uow.connection.execute("SELECT attribution_json, plan_json FROM context_graph_edits WHERE input_id=?",
                                         (queued["input"]["id"],)).fetchone()
        attribution, plan = json.loads(row["attribution_json"]), json.loads(row["plan_json"])
        self.assertEqual((attribution["operation"], attribution["principal_id"]), ("personal_set", self.lead.principal_id))
        self.assertIn(SAID["statement"], plan["rationale"])

    def test_anything_waiting_in_the_queue_refuses_the_save_at_once(self):
        self.ok(self.lead, "personal_set", changes({"field": "phone", "values": ["+1 555 0199"]}))
        refused = self.call(self.lead, "personal_set", changes({"field": "email", "values": ["sam@example.com"]}))
        error = plain(refused.error)
        self.assertEqual((refused.status, error["code"], error["retryable"], error["details"]),
                         ("error", "busy", True, {"waiting": 1}))
        with self.runtime.database.transaction() as uow:
            self.assertEqual(uow.connection.execute("SELECT COUNT(*) FROM context_inputs").fetchone()[0], 1)

    def test_only_the_configured_ordubashy_may_save(self):
        refused = self.call(self.worker, "personal_set", changes({"field": "phone", "values": ["+1 555 0199"]}))
        self.assertEqual((refused.status, plain(refused.error)["code"]), ("error", "forbidden"))
        self.assertIn("Only the configured Ordubashy", plain(refused.error)["message"])

    def test_a_fact_the_record_holds_is_not_queued_again(self):
        self.publish(self.ok(self.lead, "personal_set", changes({"field": "phone", "values": ["+1 555 0199"]})))
        refused = self.call(self.lead, "personal_set", changes({"field": "PHONE", "values": ["+1 555 0199"]}))
        self.assertEqual(plain(refused.error)["code"], "no_change")


if __name__ == "__main__":
    unittest.main()
