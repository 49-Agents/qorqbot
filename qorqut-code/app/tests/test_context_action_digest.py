"""Recorded Actions reach Context once a day, as one GENERAL digest input filed by one model run."""
from datetime import datetime, timedelta, timezone
from functools import partial
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from zoneinfo import ZoneInfo

from qorqut_context import Context
from qorqut_context.integration.action_digest import ActionDigests
from qorqut_context.integration.action_digest.digest import PART_BYTES
from qorqut_context.integration.driver import ContextQueueDriver
from qorqut_context.integration.queue.queue import admit_action_revision
from qorqut_context.integration.service import ContextLocations, ContextSettings, compose_context, driver_principal_id
from qorqut_context.integration.usage.collector import ContextUsageCollector
from qorqut_context.runner import RunResult, RunnerConfig
from qorqut_operations.actions.operations import ActionOperations
from qorqut_operations.application.engine import Application
from qorqut_operations.application.registry import OperationRegistry
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, PolicyDecision, new_id, plain
from qorqut_operations.items import ItemContribution, ItemResolver
from qorqut_operations.storage.database import Database
from qorqut_operations.tasks import TaskOperations, TaskPorts
from qorqut.composition.assembly import host_assembly
from qorqut.service.changes import ChangeFeed

UTC = timezone.utc
ZONE = ZoneInfo("America/Puerto_Rico")      # UTC-4, no daylight saving time


def noop(*args, **kwargs):
    return None


class Allow:
    def resolve(self, ctx, descriptor, targets, uow):
        return PolicyDecision("allow", "test")


class FilingRunner:
    """Deterministic model boundary: files nothing, so the tests check transport, policy selection and recording."""
    config = RunnerConfig()

    def __init__(self):
        self.calls = []

    def run(self, *, stage, instructions, payload, directory, tools=None, **kwargs):
        self.calls.append((stage, payload, instructions))
        if stage == "bootstrap":
            return RunResult("completed", json.dumps({"node_ids": []}), "thread", "turn", self.config.model)
        steps = [("stage_handoff_note", {"text": ""}),
                 ("finish_propagation", {"status": "complete", "summary": "No lasting change.", "reassessed": []})]
        if payload["input_mode"] == "OPERATIONAL_ACTION":
            steps.insert(0, ("stage_action_associations", {"date": 0, "summary": "Routine work.", "associations": []}))
        for name, arguments in steps:
            result = tools.dispatch(name, arguments)
            if not result["ok"]:
                raise AssertionError(result)
        return RunResult("completed", "", "thread", "turn", self.config.model)


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = Database(self.root / "state.sqlite", assembly=host_assembly(ChangeFeed())).initialize_target()
        self.addCleanup(self.db.close)
        self.owner, self.project, self.installation = new_id(), new_id(), new_id()
        self.driver_id = driver_principal_id(self.installation)
        self.ctx = InvocationContext(Principal(self.owner, "human"), source="dashboard",
            allowed_project_ids=frozenset({self.project}), global_capabilities=frozenset({"read_global_context"}))
        with self.db.transaction() as uow:
            uow.repository("principals").insert({"id": self.owner, "kind": "human", "display_name": "Synthetic owner"})
            uow.repository("principals").insert({"id": self.driver_id, "kind": "system", "display_name": "Context queue driver"})
            uow.repository("installation").insert({"id": "service", "installation_id": self.installation,
                                                   "owner_principal_id": self.owner})
            uow.repository("projects").insert({"id": self.project, "name": "Website relaunch", "slug": "web", "workspace": "web"})
            uow.repository("tasks", project_id=None).insert({"id": "pricing-task", "title": "Publish pricing page",
                "status": "working", "created_by": self.owner,
                "fields_json": {"$task": {"overview": "Three plans; Acme asked for an annual price."}}})
        self.now = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
        registry = OperationRegistry()
        self.app = Application(self.db, registry, policy=Allow())
        self.runner = FilingRunner()
        locations = ContextLocations(self.root / "context", self.root / "runtime", self.root / "questions.sqlite",
                                     self.root / "locks", self.root / "operations")
        self.composed = compose_context(self.app, locations, ContextSettings(runner=self.runner),
                                       instruct_roles=(), coordinator=lambda uow: self.owner)
        lookup = NS(resource=noop, resolve=noop, relationship_item=noop, fingerprint=noop)
        items = ItemResolver(self.app, [ItemContribution("test", ("task",), lookup)], candidates=NS(candidates=lambda *_: []))
        ports = TaskPorts(assignees=NS(protect=noop, owner_directed=lambda *_: False, worker_excluded=lambda *_: False),
            references=NS(types=(), host_types=(), resolve=noop, derived=noop),
            routing=NS(routes=lambda *_: ((), ()), snooze_routes=lambda *_: ()))
        tasks = TaskOperations(self.app, items, ports)
        self.actions = ActionOperations(self.app, tasks.note_references(), tasks=tasks, on_revision=self.composed.on_revision,
                                        clock=lambda: self.now)
        for contribution in (tasks.contribution(), self.actions.contribution()):
            for entry in contribution.entries:
                registry.declare(entry)
            for descriptor in contribution.descriptors:
                registry.register(descriptor)
        registry.freeze()
        self.digests = self.job()

    def job(self, **options):
        return ActionDigests(source_bytes=RunnerConfig().source_bytes, zone=lambda: ZONE, clock=lambda: self.now, **options)

    # -- helpers -------------------------------------------------------------------------------------------------------

    def invoke(self, name, args, **options):
        result = self.app.invoke(self.ctx, name, args, {"idempotency_key": new_id(), **options})
        self.assertEqual("ok", result.status, result.error)
        return plain(result.data)

    def record(self, text, at, **changes):
        self.now = at
        return self.invoke("actions_record", {"text": text, "category": "project-operational", "project_ids": [self.project],
                                              **changes})

    def run_at(self, at, job=None):
        self.now = at
        with self.db.transaction() as uow:
            return (job or self.digests).run(uow)

    def rows(self, sql, *parameters):
        with self.db.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, parameters)]

    def digest_inputs(self):
        return [{**row, "payload": json.loads(row["payload_json"])} for row in self.rows(
            "SELECT i.*, d.day, d.part, d.through_sequence FROM context_inputs i JOIN context_action_digests d ON d.input_id = i.id "
            "ORDER BY i.sequence")]

    @staticmethod
    def local(day, hour, minute=0):
        return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}").replace(tzinfo=ZONE).astimezone(UTC)

    # -- admission -----------------------------------------------------------------------------------------------------

    def test_recording_correcting_and_withdrawing_an_action_queue_no_context_input(self):
        recorded = self.record("Published the pricing page with three plans.", self.local("2026-11-10", 10))
        self.assertNotIn("context_input_id", recorded)
        action_id = recorded["action"]["action_id"]
        self.invoke("actions_correct", {"action_id": action_id, "text": "Published the pricing page with four plans."},
                    expected_revision=1)
        self.invoke("actions_withdraw", {"action_id": action_id}, expected_revision=2)
        self.assertEqual([], self.rows("SELECT * FROM context_inputs"))
        self.assertEqual([(action_id, 1), (action_id, 2), (action_id, 3)],
                         [(row["action_id"], row["revision"]) for row in
                          self.rows("SELECT * FROM context_action_backlog ORDER BY sequence")])
        # Everything else about the Action stays as it was: its log row, its revisions and its timeline event.
        self.assertEqual(3, len(self.rows("SELECT * FROM action_revisions WHERE action_id=?", action_id)))
        self.assertEqual(1, self.rows("SELECT withdrawn FROM action_logs WHERE id=?", action_id)[0]["withdrawn"])

    # -- the daily digest ----------------------------------------------------------------------------------------------

    def test_the_day_after_one_digest_lists_the_day_s_actions_and_the_next_carries_corrections(self):
        first = self.record("Published the pricing page with three plans.", self.local("2026-11-10", 9, 30),
                            evidence={"task": "pricing-task"},
                            related_objects=[{"kind": "node", "id": "abcde", "explanation": "the pricing page"}])
        second = self.record("Sent the annual price to Dana Reed at Acme.", self.local("2026-11-10", 16),
                             evidence={"task": "pricing-task"})
        global_line = self.record("Renewed the example.test domain for two years.", self.local("2026-11-10", 23, 50),
                                  category="global-operational", project_ids=[])
        # Not before 00:15 the next local day.
        self.assertEqual([], self.run_at(self.local("2026-11-11", 0, 10)))
        [created] = self.run_at(self.local("2026-11-11", 0, 20))
        [digest] = self.digest_inputs()
        self.assertEqual(created, digest["id"])
        self.assertEqual(("general", "add", None, self.driver_id), (digest["input_kind"], digest["operation"],
                                                                     digest["project_id"], digest["submitted_by"]))
        payload = digest["payload"]
        self.assertEqual("foundation/action-digests/2026-11-10.md", payload["path"])
        self.assertEqual({"kind": "action_digest", "title": "Action digest 2026-11-10", "origin": "action_log",
                          "origin_ref": "2026-11-10/1"}, payload["metadata"])
        content = payload["content"]
        self.assertIn("(America/Puerto_Rico time)", content)
        self.assertIn("## Project: Website relaunch", content)
        self.assertIn("### Task: Publish pricing page\nTask excerpt: Three plans; Acme asked for an annual price.", content)
        self.assertIn("- 09:30, Synthetic owner: Published the pricing page with three plans. (ref " + first["action"]["action_id"][:8], content)
        self.assertIn("Related Context objects: node abcde (the pricing page).", content)
        self.assertIn("- 16:00, Synthetic owner: Sent the annual price to Dana Reed at Acme.", content)
        self.assertIn("## Global (no project)\n\n### Without a Task\n\n- 23:50, Synthetic owner: Renewed the example.test domain",
                      content)
        self.assertLess(content.index("Published the pricing"), content.index("Sent the annual price"))
        self.assertLess(content.index("## Project: Website relaunch"), content.index("## Global (no project)"))
        self.assertNotIn("Corrections to earlier Actions", content)
        # The next day: one correction, one withdrawal and one new Action.
        self.now = self.local("2026-11-11", 9)
        self.invoke("actions_correct", {"action_id": first["action"]["action_id"], "text": "Published the pricing page with four plans."},
                    expected_revision=1)
        self.invoke("actions_withdraw", {"action_id": global_line["action"]["action_id"]}, expected_revision=1)
        self.record("Booked the launch call with Acme.", self.local("2026-11-11", 11))
        [created] = self.run_at(self.local("2026-11-12", 1))
        content = self.digest_inputs()[-1]["payload"]["content"]
        self.assertIn("## Corrections to earlier Actions", content)
        self.assertIn('- Corrected: the Action recorded 2026-11-10 by Synthetic owner (project Website relaunch, Task '
                      '"Publish pricing page") now reads "Published the pricing page with four plans.". It replaces '
                      '"Published the pricing page with three plans.".', content)
        self.assertIn('- Withdrawn: the Action recorded 2026-11-10 by Synthetic owner (Global), "Renewed the example.test '
                      'domain for two years.", did not happen as recorded.', content)
        self.assertIn("Booked the launch call with Acme.", content)
        self.assertLess(content.index("Booked the launch call"), content.index("## Corrections to earlier Actions"))
        self.assertNotIn(second["action"]["action_id"][:8], content)

    def test_a_day_with_no_actions_or_only_an_action_withdrawn_the_same_day_creates_nothing(self):
        self.assertEqual([], self.run_at(self.local("2026-11-12", 3)))
        recorded = self.record("Sent the draft to Acme.", self.local("2026-11-12", 10))
        self.invoke("actions_withdraw", {"action_id": recorded["action"]["action_id"]}, expected_revision=1)
        self.assertEqual([], self.run_at(self.local("2026-11-13", 3)))
        self.assertEqual([], self.rows("SELECT * FROM context_inputs"))
        self.assertEqual([], self.rows("SELECT * FROM context_action_digests"))

    def test_running_again_creates_no_second_input_for_the_same_day(self):
        self.record("Sent the draft to Acme.", self.local("2026-11-10", 10))
        self.assertEqual(1, len(self.run_at(self.local("2026-11-11", 1))))
        self.assertEqual([], self.run_at(self.local("2026-11-11", 2)))
        self.assertEqual([], self.run_at(self.local("2026-11-12", 2)))
        self.assertEqual(1, len(self.digest_inputs()))
        # The table itself refuses a second row for the same day and part.
        with self.assertRaises(Exception):
            with self.db.transaction() as uow:
                uow.connection.execute("INSERT INTO context_action_digests(id,day,part,time_zone,through_sequence,entry_count,"
                                       "input_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                                       (new_id(), "2026-11-10", 1, "UTC", 1, 1, new_id(), "now"))

    def test_missed_days_are_caught_up_one_digest_per_day_oldest_first_with_a_cap_per_run(self):
        for day in ("2026-11-10", "2026-11-12", "2026-11-13", "2026-11-15"):
            self.record(f"Sent the {day} report to Acme.", self.local(day, 12))
        capped = self.job(max_days=2)
        self.assertEqual(2, len(self.run_at(self.local("2026-11-17", 1), capped)))
        self.assertEqual(2, len(self.run_at(self.local("2026-11-17", 1, 1), capped)))
        self.assertEqual([], self.run_at(self.local("2026-11-17", 1, 2), capped))
        inputs = self.digest_inputs()
        self.assertEqual(["2026-11-10", "2026-11-12", "2026-11-13", "2026-11-15"], [row["day"] for row in inputs])
        for row in inputs:
            self.assertIn(f"Sent the {row['day']} report to Acme.", row["payload"]["content"])
            self.assertEqual(1, row["payload"]["content"].count("report to Acme."))

    def test_days_that_add_nothing_do_not_use_up_the_cap(self):
        for day in ("2026-11-10", "2026-11-11", "2026-11-12"):
            recorded = self.record(f"Sent the {day} draft to Acme.", self.local(day, 10))
            self.invoke("actions_withdraw", {"action_id": recorded["action"]["action_id"]}, expected_revision=1)
        self.record("Sent the final draft to Acme.", self.local("2026-11-13", 10))
        [created] = self.run_at(self.local("2026-11-14", 1), self.job(max_days=1))
        [digest] = self.digest_inputs()
        self.assertEqual(("2026-11-13", created), (digest["day"], digest["id"]))
        self.assertEqual(max(row["sequence"] for row in self.rows("SELECT sequence FROM context_action_backlog")),
                         digest["through_sequence"])

    def test_a_large_day_is_split_into_numbered_parts_without_dropping_a_line(self):
        refs = [self.record(f"Sent quote {number} to Acme.", self.local("2026-11-10", 8 + number // 10, number % 60))["action"]["action_id"]
                for number in range(25)]
        job = self.job(part_entries=10)
        self.assertEqual(3, len(self.run_at(self.local("2026-11-11", 1), job)))
        inputs = self.digest_inputs()
        self.assertEqual(["foundation/action-digests/2026-11-10.md", "foundation/action-digests/2026-11-10-part-2.md",
                          "foundation/action-digests/2026-11-10-part-3.md"], [row["payload"]["path"] for row in inputs])
        self.assertEqual([1, 2, 3], [row["part"] for row in inputs])
        self.assertEqual("Action digest 2026-11-10, part 2 of 3", inputs[1]["payload"]["metadata"]["title"])
        joined = "".join(row["payload"]["content"] for row in inputs)
        self.assertEqual(sorted(identity[:8] for identity in refs), sorted(re.findall(r"\(ref ([0-9a-f]{8})\)", joined)))
        for row in inputs:
            # Every part repeats the headings its lines sit under.
            self.assertIn("## Project: Website relaunch", row["payload"]["content"])
        # A byte limit splits as well, and every part stays within it.
        limit = 1500
        small = ActionDigests(source_bytes=limit, zone=lambda: ZONE, clock=lambda: self.now)
        with self.db.read() as uow:
            rows = small.due_days(uow.connection, 0, "", "2026-11-11", ZONE)[0][1]
            entries = small.entries(uow.connection, rows, "2026-11-10", ZONE)
        parts = small.pack("2026-11-10", "America/Puerto_Rico", entries)
        self.assertGreater(len(parts), 1)
        self.assertEqual(25, sum(len(part) for part in parts))
        for number, part in enumerate(parts, 1):
            rendered = small.render("2026-11-10", "America/Puerto_Rico", part, small.label(number, len(parts)))
            self.assertLessEqual(len(rendered.encode()), limit)
        self.assertEqual(PART_BYTES, self.digests.limit)
        # A single line that fits no part is refused, never cut short.
        with self.assertRaises(DomainError):
            ActionDigests(source_bytes=200, zone=lambda: ZONE).pack("2026-11-10", "UTC", entries)

    def test_the_cursor_never_skips_or_repeats_an_action(self):
        self.record("Sent quote 1 to Acme.", self.local("2026-11-10", 10))
        self.run_at(self.local("2026-11-11", 1))
        # A revision stamped on the digested day that committed after its digest (a prepared plan applied late).
        late = self.record("Sent quote 2 to Acme.", self.local("2026-11-10", 23, 59))
        self.record("Sent quote 3 to Acme.", self.local("2026-11-11", 12))
        self.run_at(self.local("2026-11-12", 1))
        self.run_at(self.local("2026-11-12", 2))
        inputs = self.digest_inputs()
        self.assertEqual([("2026-11-10", 1), ("2026-11-10", 2), ("2026-11-11", 1)], [(row["day"], row["part"]) for row in inputs])
        self.assertEqual("foundation/action-digests/2026-11-10-part-2.md", inputs[1]["payload"]["path"])
        self.assertIn(late["action"]["action_id"][:8], inputs[1]["payload"]["content"])
        joined = "".join(row["payload"]["content"] for row in inputs)
        for number in (1, 2, 3):
            self.assertEqual(1, joined.count(f"Sent quote {number} to Acme."))
        # The stored cursor is the last backlog sequence read, and it only grows.
        backlog = [row["sequence"] for row in self.rows("SELECT sequence FROM context_action_backlog ORDER BY sequence")]
        self.assertEqual([backlog[0], backlog[1], backlog[2]], [row["through_sequence"] for row in inputs])

    def test_the_local_day_follows_the_installation_time_zone(self):
        # 02:00 UTC on 11 November is 22:00 on 10 November in Puerto Rico.
        self.record("Sent the late quote to Acme.", datetime(2026, 11, 11, 2, tzinfo=UTC))
        self.run_at(self.local("2026-11-11", 1))
        [digest] = self.digest_inputs()
        self.assertEqual("2026-11-10", digest["day"])
        self.assertIn("- 22:00, Synthetic owner: Sent the late quote to Acme.", digest["payload"]["content"])

    def test_nothing_is_created_before_the_context_driver_is_registered(self):
        self.record("Sent the draft to Acme.", self.local("2026-11-10", 10))
        job = ActionDigests(source_bytes=RunnerConfig().source_bytes, zone=lambda: ZONE, clock=lambda: self.now,
                            submitter=lambda uow: None)
        self.assertEqual([], self.run_at(self.local("2026-11-11", 1), job))
        self.assertEqual(1, len(self.run_at(self.local("2026-11-11", 1))))

    def test_only_the_digest_admission_places_an_input_in_the_digest_directory(self):
        with self.assertRaises(DomainError):
            with self.db.read() as uow:
                self.composed.queue.prepare_submit(self.ctx, {"operation": "add", "path": "foundation/action-digests/x.md",
                                                              "content": "forged", "metadata": {"kind": "note", "title": "x",
                                                                                                "origin": "test"}}, None, uow)
        with self.assertRaises(Exception):
            with self.db.transaction() as uow:
                uow.connection.execute(
                    "INSERT INTO context_inputs(id,project_id,input_kind,operation,payload_json,submitted_by,created_at) "
                    "VALUES (?,NULL,'general','add',?,?,'now')",
                    (new_id(), json.dumps({"path": "foundation/action-digests/2026-11-10.md"}), self.driver_id))

    # -- filing through the normal queue -------------------------------------------------------------------------------

    def test_an_old_action_input_still_drains_and_the_digest_is_filed_by_one_run_with_its_own_policy(self):
        recorded = self.record("Sent the draft to Acme.", self.local("2026-11-10", 10))
        # An Action input queued before the daily digest existed, through the admission that queued one per revision.
        with self.db.transaction() as uow:
            revision = uow.repository("action_revisions", project_id=None).get(
                {"action_id": recorded["action"]["action_id"], "revision": 1})
            old_input = admit_action_revision(self.ctx, revision, uow)
        [digest_input] = self.run_at(self.local("2026-11-11", 1))
        Context.initialize(self.composed.locations.repository, self.composed.locations.runtime)
        driver = ContextQueueDriver(self.db, self.composed.context, self.composed.pipeline, driver_id=self.driver_id,
                                    outcomes=self.composed.outcomes, programs=NS(get=lambda *a, **k: None),
                                    coordinator=lambda uow: self.owner)
        self.assertEqual(("published", old_input), (driver.process_next()["status"], driver.last["input_id"]))
        second = driver.process_next()
        self.assertEqual(("published", digest_input), (second["status"], driver.last["input_id"]), second)
        self.assertEqual("idle", driver.process_next()["status"])
        states = {row["id"]: row for row in self.rows("SELECT id, state, input_kind FROM context_inputs")}
        self.assertEqual({old_input: ("published", "action"), digest_input: ("published", "general")},
                         {key: (value["state"], value["input_kind"]) for key, value in states.items()})
        # One model run for the digest, under the digest policy rather than the general one.
        propagations = [(payload, instructions) for stage, payload, instructions in self.runner.calls if stage == "propagation"]
        self.assertEqual(2, len(propagations))
        payload, instructions = propagations[1]
        self.assertEqual("foundation/action-digests/2026-11-10.md", payload["source_change"]["source"]["path"])
        self.assertIn("## GENERAL input policy: daily Action digest", instructions)
        self.assertNotIn("This event is a general context submission", instructions)
        self.assertIn("OPERATIONAL_ACTION input policy", propagations[0][1])
        snapshot = self.composed.context.snapshot()
        self.assertIn("foundation/action-digests/2026-11-10.md", snapshot.files)
        # The run's calls land in the same Context filing usage ledger as every other filing.
        collector = ContextUsageCollector(self.db, self.composed.pipeline, driver_id=self.driver_id,
                                          lock_path=self.root / "usage.lock")
        for _ in range(6):
            collector.collect_next()
        ledger = {row["input_id"] for row in self.rows("SELECT input_id FROM context_usage_calls")}
        self.assertIn(digest_input, ledger)
        self.assertIn(old_input, ledger)


class DigestOrderTests(unittest.TestCase):
    def test_every_digest_is_one_source_so_a_blocked_digest_holds_later_digests_only(self):
        from qorqut_context.integration.queue.transitions import source_key
        row = lambda path: {"input_kind": "general", "item_id": None, "path": path}
        first = source_key(row("foundation/action-digests/2026-11-10.md"))
        self.assertEqual(first, source_key(row("foundation/action-digests/2026-11-10-part-2.md")))
        self.assertEqual(first, source_key(row("foundation/action-digests/2026-11-11.md")))
        self.assertNotEqual(first, source_key(row("foundation/notes/other.md")))


class CompositionTests(unittest.TestCase):
    def test_the_composed_revision_port_is_the_digest_backlog(self):
        from qorqut_context.integration.action_digest import admit_revision
        with TemporaryDirectory() as temp:
            root = Path(temp)
            locations = ContextLocations(root / "c", root / "r", root / "q.sqlite", root / "l", root / "o")
            composed = compose_context(NS(), locations, ContextSettings(runner=FilingRunner()), instruct_roles=(),
                                       coordinator=lambda uow: None)
        self.assertIs(admit_revision, composed.on_revision)
        self.assertIsInstance(composed.action_digests, ActionDigests)
        self.assertNotIsInstance(composed.on_revision, partial)


if __name__ == "__main__":
    unittest.main()
