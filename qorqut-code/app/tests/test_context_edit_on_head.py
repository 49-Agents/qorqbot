"""A queued graph edit runs on the published head when nothing it depends on changed since its base.

Every input ahead of an edit in the shared Context queue publishes and moves the head. The driver's gate reads what those
publications changed: when none of them touched a record the plan puts or removes, an ID it creates, an endpoint it points at,
the edges of a node it removes or a source it cites, the edit publishes on the head exactly as a plan prepared there would;
otherwise it is refused as ``stale_base`` and nothing is published. ``personal_set`` is queued whatever waits ahead of it.

The cases run the real application, store, Context and queue driver. What moves the head in between is a graph edit published
straight through the library, which is what any input ahead of the edit does to the published Context."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from qorqut_context import Pipeline, graph_edits as library
from qorqut_context.engine import Context
from qorqut_context.git_store import CONFIG_PATH
from qorqut_context.integration.driver import ContextQueueDriver
from qorqut_context.integration.filing.outcomes import FilingOutcomes
from qorqut_context.integration.filing.source_operations import ContextSourceOperations
from qorqut_context.integration.graph_edits.graph_edit import (GraphEditPorts, GraphEdits, MergeConflict, merged_put, on_head,
                                                               plan_on, rebase_conflict)
from qorqut_context.integration.queue import transitions
from qorqut_context.integration.reading import SharedContextReader, shared_context_contribution
from qorqut_operations.application.plans import utc_clock
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationOptions, new_id, plain

from qorqut.composition.catalog import install_contribution

from private_thread_runtime import build_runtime

SAID = {"statement": "My new number is +1 555 0199.", "source": "thread:example", "date": "2026-11-20"}


def role(name):
    return ArtifactRef(name, "1", "a" * 64)


def node(object_id, summary="A test record.", **attributes):
    return {"id": object_id, "summary": summary, "attributes": {"basic_entity_type": ["organization"], **attributes}}


def edge(object_id, from_id, to_id, summary="Related."):
    return {"id": object_id, "summary": summary, "attributes": {}, "from_id": from_id, "to_id": to_id}


def put(verb, record):
    return {"op": verb, "record": record, "explanation": "Test change.", "citations": ["self"], "support_after": ["self"]}


def remove(verb, object_id):
    return {"op": verb, "id": object_id, "explanation": "Test removal.", "citations": ["self"]}


def plan(revision, *operations):
    return {"format": library.FORMAT, "expected_revision": revision, "rationale": "Test edit.", "operations": list(operations)}


class Instance:
    """One installation: application, store, Context and the queue driver wired as the host wires them."""

    def __init__(self, test: unittest.TestCase, ids=None):
        directory = tempfile.TemporaryDirectory()
        test.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.runtime = runtime = build_runtime(root)
        test.addCleanup(runtime.database.close)
        runtime.application.policy.refresh_context = lambda ctx, uow: ctx
        self.context = Context.initialize(root / "context", root / "context-runtime")
        self.pipeline = Pipeline(self.context)
        reader = SharedContextReader(root / "context", root / "context-runtime")
        test.addCleanup(reader.close)
        coordinator = lambda uow: uow.connection.execute(        # noqa: E731
            "SELECT management_coordinator_id FROM installation WHERE id='service'").fetchone()[0]
        supply = iter(ids or ())
        self.edits = GraphEdits(runtime.application, GraphEditPorts(self.context), roles={"ordubashy", "global-worker"},
                                coordinator=coordinator, id_source=(lambda: next(supply)) if ids else None)
        ContextSourceOperations(filing=None, clock=utc_clock, graph_edits=self.edits,
                                instruct_roles={"ordubashy", "global-worker"}).register(runtime.application.registry)
        install_contribution(runtime.application.registry, shared_context_contribution(reader))
        self.lead = replace(runtime.coordinator, role_artifact_ref=role("ordubashy"))
        # The driver re-resolves the original caller from its registration; the fixture's policy has no attention work to do it.
        runtime.application.policy.context_for = lambda principal_id, *, project_id, review_project_id, uow: replace(  # noqa: E731
            self.lead, project_id=project_id)
        system = new_id()
        with runtime.database.transaction() as uow:
            uow.repository("principals").insert({"id": system, "kind": "system", "display_name": "Context driver"})
        self.driver = ContextQueueDriver(runtime.database, self.context, self.pipeline, driver_id=system, outcomes=FilingOutcomes(),
                                         programs={}, coordinator=coordinator, graph_authority=self.edits.authority)
        self.test = test

    def head(self):
        return self.context.snapshot()

    def direct(self, event_id, *operations):
        """Publish an edit straight through the library, as any input ahead of a queued edit does."""
        self.pipeline.submit_edit(plan(self.head().revision, *operations), submitted_by=self.lead.principal_id, event_id=event_id,
                                  record_context={"scope": {"kind": "global"}, "provenance": {"test": "ahead"}})
        return self.head()

    def call(self, operation, arguments):
        return self.runtime.application.invoke(self.lead, operation, arguments, InvocationOptions(idempotency_key=new_id()))

    def ok(self, operation, arguments):
        result = self.call(operation, arguments)
        self.test.assertEqual(result.status, "ok", plain(result.error))
        return plain(result.data)

    def queue(self, *operations):
        return self.ok("context_edit", {"edit": plan(self.head().revision, *operations)})

    def stored_plan(self, queued):
        with self.runtime.database.read() as uow:
            row = uow.connection.execute("SELECT plan_json FROM context_graph_edits WHERE input_id=?",
                                         (queued["input"]["id"],)).fetchone()
        return json.loads(row["plan_json"])

    def drive(self):
        return self.driver.process_next()


class RunsOnTheHeadTests(unittest.TestCase):
    def setUp(self):
        self.one = Instance(self, ids=["evaaa", "evbbb", "evccc"])
        self.one.direct("seed1", put("put_node", node("nodea")), put("put_node", node("nodeb")),
                        put("put_node", node("nodec")), put("put_edge", edge("edgeb", "nodeb", "nodec")))

    def test_an_edit_applies_when_only_unrelated_records_changed_in_between(self):
        queued = self.one.queue(put("put_node", node("nodea", "Changed by the queued edit.")),
                                put("put_node", node("nnew1")), put("put_edge", edge("edge1", "nodea", "nnew1")))
        base = queued["execution"]["expected_revision"]
        moved = self.one.direct("ahead", put("put_node", node("nodeb", "Changed ahead of the edit.")),
                                put("put_node", node("other")))
        self.assertNotEqual(moved.revision, base)
        result = self.one.drive()
        self.assertEqual(result["status"], "published", result)
        after = self.one.head()
        event = after.events[queued["execution"]["reserved_event_id"]]
        self.assertEqual(event["base_revision"], moved.revision)
        self.assertEqual(after.nodes["nodea"]["summary"], "Changed by the queued edit.")
        self.assertEqual(after.nodes["nodeb"]["summary"], "Changed ahead of the edit.")
        self.assertEqual(after.edges["edge1"], edge("edge1", "nodea", "nnew1"))
        # The published plan is the stored plan with only its base moved to the head it ran on.
        published = library.load(after.files[event["source"]["path"]])
        self.assertEqual(published, on_head(self.one.stored_plan(queued), moved))
        self.assertEqual(event["execution"], library.marker(published))

    def test_an_edit_of_a_node_an_edge_points_at_is_not_a_conflict(self):
        queued = self.one.queue(put("put_edge", edge("edge2", "nodea", "nodeb")))
        self.one.direct("ahead", put("put_node", node("nodeb", "Edited, still there.")))
        self.assertEqual(self.one.drive()["status"], "published")
        self.assertIn("edge2", self.one.head().edges)
        self.assertIsNotNone(queued)

    def refused(self, *, why):
        result = self.one.drive()
        self.assertEqual(result["status"], "discarded", result)
        self.assertEqual(result["error"]["code"], "stale_base")
        self.assertIn(why, result["error"]["message"])
        return result

    def test_refused_when_a_record_the_edit_changes_was_changed(self):
        queued = self.one.queue(put("put_node", node("nodea", "From the queued edit.")))
        self.one.direct("ahead", put("put_node", node("nodea", "From the input ahead.")))
        self.refused(why="a node or edge this edit changes or removes was changed")
        head = self.one.head()
        self.assertEqual(head.nodes["nodea"]["summary"], "From the input ahead.")
        self.assertNotIn(queued["execution"]["reserved_event_id"], head.events)

    def test_refused_when_a_record_the_edit_removes_was_changed(self):
        self.one.queue(remove("remove_edge", "edgeb"))
        self.one.direct("ahead", put("put_edge", edge("edgeb", "nodeb", "nodec", "Restated.")))
        self.refused(why="was changed")
        self.assertIn("edgeb", self.one.head().edges)

    def test_refused_when_an_id_the_edit_creates_was_taken(self):
        self.one.queue(put("put_node", node("nnew1", "Mine.")))
        self.one.direct("ahead", put("put_node", node("nnew1", "Taken first.")))
        self.refused(why="an ID this edit creates has been used")
        self.assertEqual(self.one.head().nodes["nnew1"]["summary"], "Taken first.")

    def test_refused_when_an_endpoint_the_edit_points_at_was_removed(self):
        self.one.queue(put("put_edge", edge("edge3", "nodea", "nodeb")))
        self.one.direct("ahead", remove("remove_edge", "edgeb"), remove("remove_node", "nodeb"))
        self.refused(why="a node an edge of this edit points at was removed")
        self.assertNotIn("edge3", self.one.head().edges)

    def test_refused_when_an_edge_now_points_at_a_node_the_edit_removes(self):
        self.one.queue(remove("remove_node", "nodea"))
        self.one.direct("ahead", put("put_edge", edge("edge4", "nodec", "nodea")))
        self.refused(why="an edge now points at a node this edit removes")
        self.assertIn("nodea", self.one.head().nodes)

    def test_a_refusal_names_the_kind_of_conflict_and_no_record_content(self):
        self.one.queue(put("put_node", node("nodea", "Secret summary text.")))
        self.one.direct("ahead", put("put_node", node("nodea", "Other secret words.")))
        message = self.refused(why="was changed")["error"]["message"]
        self.assertNotIn("Secret", message)
        self.assertNotIn("secret", message)


class SameResultAsPlannedOnTheHeadTests(unittest.TestCase):
    """The edit queued on an older base publishes the same graph as the same edit prepared on the head."""

    def build(self, *, queue_first):
        instance = Instance(self, ids=["evaaa"])
        instance.direct("seed1", put("put_node", node("nodea")), put("put_node", node("nodeb")))
        operations = (put("put_node", node("nodea", "Edited.", color=["blue"])), put("put_node", node("nnew1", tag=["fresh"])),
                      put("put_edge", edge("edge1", "nodea", "nnew1")))
        if queue_first:
            queued = instance.queue(*operations)
            instance.direct("ahead", put("put_node", node("nodeb", "Moved.", size=["large"])))
        else:
            instance.direct("ahead", put("put_node", node("nodeb", "Moved.", size=["large"])))
            queued = instance.queue(*operations)
        self.assertEqual(instance.drive()["status"], "published")
        head = instance.head()
        event = head.events[queued["execution"]["reserved_event_id"]]
        return head, event

    def test_result_on_head_equals_planning_on_head(self):
        (rebased, moved_event), (planned, planned_event) = self.build(queue_first=True), self.build(queue_first=False)
        # Each installation draws its own personal-record ID; everything else is the same.
        graph = lambda snapshot: {key: value for key, value in snapshot.nodes.items() if key != snapshot.personal_context_node}  # noqa: E731
        self.assertEqual(graph(rebased), graph(planned))
        self.assertEqual(len(rebased.nodes), len(planned.nodes))
        self.assertEqual(rebased.edges, planned.edges)
        self.assertEqual(rebased.catalogs, planned.catalogs)
        self.assertEqual(moved_event["changes"], planned_event["changes"])
        source = lambda snapshot, event: library.load(snapshot.files[event["source"]["path"]])      # noqa: E731
        strip = lambda value: {key: item for key, item in value.items() if key != "expected_revision"}      # noqa: E731
        self.assertEqual(strip(source(rebased, moved_event)), strip(source(planned, planned_event)))
        self.assertEqual(source(rebased, moved_event)["expected_revision"], moved_event["base_revision"])


class StaleRefusalEndsTheInputTests(unittest.TestCase):
    """A stale_base refusal cannot succeed on retry, so the driver discards the input itself and the queue moves on."""

    def setUp(self):
        self.one = Instance(self, ids=["evaaa", "evbbb"])
        self.one.direct("seed1", put("put_node", node("nodea")))

    def rows(self, sql, *args):
        with self.one.runtime.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, args)]

    def test_the_refused_edit_is_discarded_with_its_reason_and_the_next_input_runs(self):
        stale = self.one.queue(put("put_node", node("nodea", "Queued first.")))
        self.one.queue(put("put_node", node("nnew2")))
        self.one.direct("ahead", put("put_node", node("nodea", "Changed ahead.")))
        result = self.one.drive()
        self.assertEqual((result["status"], result["error"]["code"], result["error"]["retryable"]), ("discarded", "stale_base", False))
        row, = self.rows("SELECT state, attempt, error_json, result_json FROM context_inputs WHERE id=?", stale["input"]["id"])
        saved = json.loads(row["result_json"])
        self.assertEqual((row["state"], row["attempt"], row["error_json"]), ("discarded", 1, None))
        self.assertEqual((saved["outcome"], saved["refusal"]["code"], saved["context"]), ("discarded", "stale_base", "no_event"))
        self.assertEqual(saved["request_evidence"]["reason"], "refused_unretryable")
        attempt, = self.rows("SELECT outcome, error_json FROM context_input_attempts WHERE input_id=?", stale["input"]["id"])
        self.assertEqual((attempt["outcome"], json.loads(attempt["error_json"])["code"]), ("blocked", "stale_base"))
        control, = self.rows("SELECT intent, status, requested_by FROM context_input_controls WHERE input_id=?", stale["input"]["id"])
        self.assertEqual((control["intent"], control["status"], control["requested_by"]), ("discard", "applied", self.one.driver.driver_id))
        notice, = self.rows("SELECT id, event_type, summary FROM inbox_events WHERE source_id=?", stale["input"]["id"])
        self.assertEqual((notice["event_type"], notice["id"]), ("context.input_discarded", saved["notice_event_id"]))
        self.assertIn("stale_base", notice["summary"])
        delivered = self.rows("SELECT agent_id, required FROM inbox_deliveries WHERE event_id=?", notice["id"])
        self.assertEqual(delivered, [{"agent_id": self.one.lead.principal_id, "required": 1}])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM inbox_events WHERE event_type='context.input_blocked'"), [{"n": 0}])
        # Nothing waits behind it: the next input publishes on the following step.
        self.assertEqual(self.one.drive()["status"], "published")
        self.assertIn("nnew2", self.one.head().nodes)

    def test_a_stale_retry_of_a_blocked_edit_is_discarded_too(self):
        stale = self.one.queue(put("put_node", node("nodea", "Queued first.")))
        policy = self.one.runtime.application.policy
        allow = policy.context_for
        policy.context_for = lambda *args, **kwargs: (_ for _ in ()).throw(DomainError("forbidden", "Revoked for this test"))
        blocked = self.one.drive()
        self.assertEqual((blocked["status"], blocked["error"]["code"]), ("input_blocked", "authority_revoked"))
        policy.context_for = allow
        self.one.direct("ahead", put("put_node", node("nodea", "Changed meanwhile.")))
        # The row context_queue_retry records (the queue operations are not installed in this fixture).
        with self.one.runtime.database.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO context_input_controls(id,project_id,input_id,intent,expected_state,requested_by,requested_at,status) "
                "VALUES (?,?,?,?,?,?,?,?)", (new_id(), None, stale["input"]["id"], "retry", "blocked", self.one.lead.principal_id,
                                             "2026-11-25T00:00:00Z", "pending"))
        result = self.one.drive()
        self.assertEqual((result["status"], result["error"]["code"]), ("discarded", "stale_base"))
        controls = self.rows("SELECT intent, status FROM context_input_controls WHERE input_id=? ORDER BY sequence", stale["input"]["id"])
        self.assertEqual(controls, [{"intent": "retry", "status": "applied"}, {"intent": "discard", "status": "applied"}])

    def test_other_refusals_still_block(self):
        self.one.queue(put("put_node", node("nodea", "Queued first.")))
        self.one.runtime.application.policy.context_for = lambda *args, **kwargs: (_ for _ in ()).throw(
            DomainError("forbidden", "Revoked for this test"))
        result = self.one.drive()
        self.assertEqual((result["status"], result["error"]["code"]), ("input_blocked", "authority_revoked"))
        self.assertEqual(self.one.drive()["status"], "blocked")


def personal(instance, *facts, **more):
    return instance.ok("personal_set", {"facts": list(facts), **SAID, **more})


def owner(snapshot):
    return snapshot.nodes[snapshot.personal_context_node]


class PersonalSetBehindTheQueueTests(unittest.TestCase):
    def setUp(self):
        self.one = Instance(self)

    def test_a_save_with_inputs_waiting_is_accepted_and_published_in_its_turn(self):
        ahead = self.one.queue(put("put_node", node("nodea")))
        queued = self.one.ok("personal_set", {"facts": [{"field": "phone", "values": ["+1 555 0199"]}], **SAID})
        self.assertEqual((queued["status"], queued["input"]["state"]), ("queued", "queued"))
        self.assertEqual(queued["execution"]["expected_revision"], ahead["execution"]["expected_revision"])
        self.assertEqual(self.one.drive()["status"], "published")
        self.assertEqual(self.one.drive()["status"], "published")
        head = self.one.head()
        self.assertIn("nodea", head.nodes)
        self.assertEqual(head.nodes[head.personal_context_node]["attributes"]["phone"], ["+1 555 0199"])
        self.assertEqual(self.one.drive()["status"], "idle")

    def test_two_saves_of_different_facts_back_to_back_both_publish(self):
        personal(self.one, {"field": "phone", "values": ["+1 555 0199"]})
        second = personal(self.one, {"field": "email", "values": ["sam@example.com"]}, summary="Sam Example, founder.")
        self.assertEqual(self.one.drive()["status"], "published")
        middle = self.one.head()
        self.assertEqual(self.one.drive()["status"], "published")
        head = self.one.head()
        record = owner(head)
        self.assertEqual((record["attributes"]["phone"], record["attributes"]["email"], record["summary"]),
                         (["+1 555 0199"], ["sam@example.com"], "Sam Example, founder."))
        self.assertEqual(record["attributes"]["basic_entity_type"], ["person"])
        # Support: what the record had after the first save, plus this save's own event.
        event = head.events[second["execution"]["reserved_event_id"]]
        self.assertEqual(head.support[("node", head.personal_context_node)],
                         [*middle.support[("node", head.personal_context_node)], head.event_commits[event["id"]]])

    def test_the_same_fact_changed_twice_refuses_the_second(self):
        personal(self.one, {"field": "phone", "values": ["+1 555 0199"]})
        personal(self.one, {"field": "phone", "values": ["+1 555 0142"], "mode": "replace"})
        self.assertEqual(self.one.drive()["status"], "published")
        result = self.one.drive()
        self.assertEqual((result["status"], result["error"]["code"]), ("discarded", "stale_base"))
        self.assertIn("a field or the support of the owner's record", result["error"]["message"])
        self.assertEqual(owner(self.one.head())["attributes"]["phone"], ["+1 555 0199"])

    def filing_style_change(self, **attributes):
        """What a filing ahead does to the owner's node: another field changed, the present support kept plus its own."""
        head = self.one.head()
        key = head.personal_context_node
        record = owner(head)
        change = {**put("put_node", {**record, "attributes": {**record["attributes"], **attributes}}),
                  "support_after": [*head.support[("node", key)], "self"]}
        return self.one.direct("filed", change)

    def test_a_change_to_another_field_of_the_owner_in_between_is_merged(self):
        queued = personal(self.one, {"field": "phone", "values": ["+1 555 0199"]})
        filed = self.filing_style_change(employer=["Example Ltd"])
        self.assertEqual(self.one.drive()["status"], "published")
        head = self.one.head()
        record = owner(head)
        self.assertEqual((record["attributes"]["phone"], record["attributes"]["employer"]), (["+1 555 0199"], ["Example Ltd"]))
        event = head.events[queued["execution"]["reserved_event_id"]]
        self.assertEqual(event["base_revision"], filed.revision)
        self.assertEqual(head.support[("node", head.personal_context_node)],
                         [*filed.support[("node", head.personal_context_node)], head.event_commits[event["id"]]])

    def test_a_change_to_the_same_field_in_between_refuses_it(self):
        personal(self.one, {"field": "phone", "values": ["+1 555 0199"]})
        self.filing_style_change(phone=["+1 555 0000"])
        result = self.one.drive()
        self.assertEqual((result["status"], result["error"]["code"]), ("discarded", "stale_base"))
        self.assertEqual(owner(self.one.head())["attributes"]["phone"], ["+1 555 0000"])

    def test_recovery_rebuilds_the_same_merged_plan(self):
        queued = personal(self.one, {"field": "phone", "values": ["+1 555 0199"]})
        self.filing_style_change(employer=["Example Ltd"])
        driver, real = self.one.driver, self.one.driver._write

        def failing(function, **arguments):
            if function is transitions.record_published:
                raise OSError("recording failed for this test")
            return real(function, **arguments)

        driver._write = failing
        self.assertEqual(driver.process_next()["status"], "unrecorded")
        driver._write = real
        # A later input moves the head past the published edit before recovery runs.
        self.one.direct("after", put("put_node", node("later")))
        head = self.one.head()
        event = head.events[queued["execution"]["reserved_event_id"]]
        published = library.load(head.files[event["source"]["path"]])
        stored = self.one.stored_plan(queued)
        attribution = {"operation": "personal_set"}
        self.assertEqual(published, plan_on(stored, attribution, event["base_revision"], head))
        self.assertEqual(driver.recover()["status"], "published")
        with self.one.runtime.database.read() as uow:
            state = uow.connection.execute("SELECT state FROM context_inputs WHERE id=?", (queued["input"]["id"],)).fetchone()[0]
        self.assertEqual(state, "published")


class ConflictRulesTests(unittest.TestCase):
    """The rules that the driver cases above cannot reach without a model-filed source or a configuration change."""

    BASE, HEAD = "a" * 40, "b" * 40

    def snapshot(self, later, *, ancestor=True, config=b"{}", nodes=("nodea",), edges=()):
        commits = {"old01": "c" * 40, **{event["id"]: "d" * 40 for event in later}}

        def run(*args):
            if not ancestor:
                from qorqut_context import ContextError
                raise ContextError("Git merge-base failed")
            return b""

        git = SimpleNamespace(run=run, event_commits=lambda revision: {"old01": "c" * 40},
                              read=lambda revision, paths: {CONFIG_PATH: b"{}"})
        events = {"old01": {"id": "old01", "operation": "add", "source": {"id": "src01"}, "changes": []},
                  **{event["id"]: event for event in later}}
        return SimpleNamespace(revision=self.HEAD, git=git, event_commits=commits, events=events, files={CONFIG_PATH: config},
                               used_ids=set(), nodes={key: {} for key in nodes}, edges={key: value for key, value in edges},
                               by_commit={"c" * 40: events["old01"]})

    def edit(self):
        return plan(self.BASE, {**put("put_node", node("nodea")), "citations": ["c" * 40], "support_after": ["c" * 40]})

    TARGETS = [{"kind": "node", "id": "nodea", "change": "update", "roles": ["target"], "ownership": {}}]

    def test_nothing_in_between_is_no_conflict(self):
        self.assertIsNone(rebase_conflict(self.edit(), self.TARGETS, self.snapshot([])))

    def test_a_cited_source_removed_in_between(self):
        later = [{"id": "rm001", "operation": "remove", "source": {"id": "src01"}, "changes": []}]
        self.assertEqual(rebase_conflict(self.edit(), self.TARGETS, self.snapshot(later)), "source_removed")

    def test_a_cited_source_edited_in_between_is_no_conflict(self):
        later = [{"id": "ed001", "operation": "edit", "source": {"id": "src01"}, "changes": []}]
        self.assertIsNone(rebase_conflict(self.edit(), self.TARGETS, self.snapshot(later)))

    def test_a_changed_configuration(self):
        self.assertEqual(rebase_conflict(self.edit(), self.TARGETS, self.snapshot([], config=b'{"x": 1}')), "configuration")

    def test_a_base_outside_the_published_history(self):
        self.assertEqual(rebase_conflict(self.edit(), self.TARGETS, self.snapshot([], ancestor=False)), "unpublished_base")

    def test_the_same_revision_is_never_a_conflict(self):
        edit = {**self.edit(), "expected_revision": self.HEAD}
        self.assertIsNone(rebase_conflict(edit, self.TARGETS, SimpleNamespace(revision=self.HEAD)))


class MergeRulesTests(unittest.TestCase):
    BASE = ({"id": "own3r", "summary": "Owner.", "attributes": {"basic_entity_type": ["person"], "phone": ["1"]}}, ["c" * 40])

    def operation(self, attributes, support=("c" * 40, "self"), summary="Owner."):
        return {"op": "put_node", "record": {"id": "own3r", "summary": summary, "attributes": attributes},
                "explanation": "x", "citations": ["self"], "support_after": list(support)}

    def test_unrelated_fields_and_support_merge_deterministically(self):
        head = ({"id": "own3r", "summary": "Owner.", "attributes": {"basic_entity_type": ["person"], "phone": ["1"], "city": ["X"]}},
                ["c" * 40, "d" * 40])
        mine = self.operation({"basic_entity_type": ["person"], "phone": ["1"], "email": ["e"]})
        first, again = merged_put(mine, self.BASE, head), merged_put(mine, self.BASE, head)
        self.assertEqual(first, again)
        self.assertEqual(first["record"]["attributes"], {"basic_entity_type": ["person"], "phone": ["1"], "city": ["X"], "email": ["e"]})
        self.assertEqual(first["support_after"], ["c" * 40, "d" * 40, "self"])

    def test_a_removed_field_stays_removed_and_overlaps_conflict(self):
        head = ({"id": "own3r", "summary": "Owner.", "attributes": {"basic_entity_type": ["person"], "phone": ["1"], "city": ["X"]}},
                ["c" * 40])
        dropped = merged_put(self.operation({"basic_entity_type": ["person"]}), self.BASE, head)
        self.assertEqual(dropped["record"]["attributes"], {"basic_entity_type": ["person"], "city": ["X"]})
        changed = ({**head[0], "attributes": {**head[0]["attributes"], "phone": ["2"]}}, head[1])
        for operation, state in ((self.operation({"basic_entity_type": ["person"], "phone": ["3"]}), changed),
                                 (self.operation(self.BASE[0]["attributes"], summary="New."), ({**head[0], "summary": "Other."}, head[1])),
                                 (self.operation(self.BASE[0]["attributes"], support=("self",)), (head[0], ["d" * 40]))):
            with self.assertRaises(MergeConflict):
                merged_put(operation, self.BASE, state)


if __name__ == "__main__":
    unittest.main()
