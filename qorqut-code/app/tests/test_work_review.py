"""What the records say was done over a period, set beside the owner's own messages for the same period."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain

from qorqut.domains.owner_inputs import OwnerInputs
from qorqut.domains.work_review import WorkReview
from qorqut.native_chat import projection, store

from test_side_chat_rename import build_runtime, create, invoke, reply

NOW = datetime(2026, 11, 12, 18, tzinfo=timezone.utc)
BEFORE = "2026-11-01T09:00:00.000000Z"
# Records the services write themselves carry the real time, whatever it is.
EVER = {"since": "2020-01-01T00:00:00Z", "until": "2100-01-01T00:00:00Z"}


def at(hour, minute=0):
    return f"2026-11-12T{hour:02d}:{minute:02d}:00.000000Z"


class WorkReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = runtime = build_runtime(Path(directory.name))
        self.addCleanup(runtime.database.close)
        runtime.registry = registry = runtime.application.registry
        # The reads each section stands for; the Task and Action engines themselves are not part of this fixture.
        for name in ("tasks_overview", "actions_list"):
            registry.register(OperationDescriptor(name, schema({"scope": {"const": "global"}}), scope_kinds=frozenset({"global"}),
                                                  global_capability="read_global_context", effect_kind="read", supports_signoff=False,
                                                  resolve_namespace=lambda ctx, args: "global", read=lambda *a: {}))
        OwnerInputs(runtime, clock=lambda: NOW).register()
        WorkReview(runtime).register()
        self.owner, self.lead, self.worker = (ctx.principal_id for ctx in (runtime.owner, runtime.coordinator, runtime.worker))

    # ----- fixtures --------------------------------------------------------------------------------------------------

    def write(self, sql, values):
        with self.runtime.database.transaction() as uow:
            uow.connection.execute(sql, values)

    def task(self, title, *, by=None, created=BEFORE, status="open", workers=(), kind=None, line=None):
        identity = new_id()
        fields = {"$task": {**({"task_type": kind} if kind else {}), **({"status_line": line} if line else {})}}
        self.write("INSERT INTO tasks(business_id,id,title,status,worker_ids_json,fields_json,created_by,created_at,updated_at) "
                   "VALUES(NULL,?,?,?,?,?,?,?,?)", (identity, title, status, json.dumps(list(workers)), json.dumps(fields),
                                                    by or self.lead, created, created))
        return identity

    def change(self, task, by, when, body, *, moved=(None, None), fields=()):
        self.write("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,recorder_id,change_json,created_at) "
                   "VALUES(NULL,?,'task',?,'system',?,?,?,?)",
                   (new_id(), task, body, by, json.dumps({"from": moved[0], "to": moved[1], "details": bool(fields),
                                                          "fields": list(fields)}), when))

    def action(self, text, by, when):
        identity = new_id()
        self.write("INSERT INTO action_logs(business_id,id,action_key,action_text,performed_by,recorded_by,origin,occurred_at,"
                   "created_at,updated_at) VALUES(NULL,?,?,?,?,?,'agent',?,?,?)",
                   (identity, "global/" + identity, text, by, self.lead, when, when, when))
        return identity

    def read(self, args=None, *, ctx=None):
        return invoke(self.runtime, "work_review_get", args, ctx=ctx or self.runtime.coordinator)

    # ----- tests -----------------------------------------------------------------------------------------------------

    def test_task_work_of_the_period_is_listed_by_what_happened_and_who_did_it(self):
        contract = self.task("Send the Linden contract", status="done", line="Sent and countersigned.")
        invoice = self.task("Chase the invoice", status="working", workers=[self.worker])
        reopened = self.task("Fix the landing page")
        made = self.task("Book the venue", by=self.worker, created=at(10), workers=[self.worker])
        self.task("Acme Ltd", created=at(10), kind="customer")                           # a CRM row record, not work
        self.task("Old task", created=BEFORE)
        self.change(contract, self.worker, at(9), "State changed from working to done.", moved=("working", "done"))
        self.change(invoice, self.lead, at(11), "Workers: added Worker.", fields=("workers",))
        self.change(reopened, self.lead, at(12), "State changed from done to open.", moved=("done", "open"))
        self.change(contract, self.worker, "2026-11-10T09:00:00.000000Z", "State changed from open to working.", moved=("open", "working"))
        self.change(invoice, self.worker, at(13), "Due set to 2026-11-20.", fields=("due",))    # none of the listed kinds
        tasks = self.read()["tasks"]
        shown = lambda name: [(item["task"]["id"], item["by"]["id"], item["at"]) for item in tasks[name]["items"]]
        self.assertEqual(shown("closed"), [(contract, self.worker, at(9))])
        self.assertEqual((tasks["closed"]["items"][0]["status_line"], tasks["closed"]["items"][0]["change"]),
                         ("Sent and countersigned.", "State changed from working to done."))
        self.assertEqual(shown("workers_changed"), [(invoice, self.lead, at(11))])
        self.assertEqual(shown("reopened"), [(reopened, self.lead, at(12))])
        self.assertEqual(shown("created"), [(made, self.worker, at(10))])
        self.assertEqual(tasks["created"]["items"][0]["workers"], ["agent"])
        self.assertEqual({name: (part["count"], part["more"]) for name, part in tasks.items()},
                         dict.fromkeys(("created", "closed", "reopened", "workers_changed"), (1, 0)))
        self.assertEqual(tasks["closed"]["by"], [{"id": self.worker, "name": "agent", "count": 1}])
        # One agent's work only.
        mine = self.read({"agent_id": self.lead})["tasks"]
        self.assertEqual([mine[name]["count"] for name in ("created", "closed", "reopened", "workers_changed")], [0, 0, 1, 1])

    def test_a_long_list_keeps_its_newest_items_and_says_how_many_it_left_out(self):
        ids = [self.action(f"Sent message {minute}", self.worker if minute % 2 else self.lead, at(9, minute)) for minute in range(5)]
        self.action("Earlier", self.lead, BEFORE)
        actions = self.read({"limit": 2})["actions"]
        self.assertEqual(([item["id"] for item in actions["items"]], actions["count"], actions["more"]), (ids[3:], 5, 3))
        self.assertEqual([(who["id"], who["count"]) for who in actions["by"]], [(self.lead, 3), (self.worker, 2)])
        self.assertEqual(actions["items"][0]["text"], "Sent message 3")
        self.assertEqual(self.read({"since": at(9, 2), "until": at(9, 4)})["actions"]["count"], 2)

    def test_proposals_and_decisions_are_listed_and_side_chats_are_not(self):
        create(self.runtime, "Just a chat")
        question = invoke(self.runtime, "signoffs_input_create", {"request_key": new_id(), "form": {
            "title": "Approve the venue?", "explanation": "Choose one", "kind": "question", "selection_mode": "none"}},
            ctx=self.runtime.coordinator)["signoff"]
        signoffs = self.read(EVER)["signoffs"]
        self.assertEqual([(item["id"], item["title"], item["state"]) for item in signoffs["proposed"]["items"]],
                         [(question["id"], "Approve the venue?", "pending")])
        self.assertEqual((signoffs["proposed"]["count"], signoffs["decided"]["count"]), (1, 0))
        answered = self.runtime.signoffs.respond(self.runtime.owner, question["id"], proposal_revision=question["proposal_revision"],
                                                 payload_hash=question["payload_hash"], expected_status_revision=question["status_revision"],
                                                 idempotency_key=new_id(), notes="Yes, book it.")
        self.assertEqual(answered.status, "ok", answered.error)
        review = self.read(EVER)
        self.assertEqual([(item["id"], item["decision_state"]) for item in review["signoffs"]["decided"]["items"]],
                         [(question["id"], "answered")])
        self.assertEqual(review["standing_now"]["pending_signoffs"], 0)
        # Nothing before the period.
        self.assertEqual(self.read({"since": "2020-01-01T00:00:00Z", "until": "2020-01-02T00:00:00Z"})["signoffs"]["proposed"]["count"], 0)

    def test_reports_to_read_are_counted_apart_from_decisions_and_the_last_one_is_named(self):
        runtime = self.runtime
        self.assertEqual({name: self.read()["standing_now"][name] for name in ("pending_signoffs", "unacknowledged", "last_report")},
                         {"pending_signoffs": 0, "unacknowledged": 0, "last_report": None})
        report = lambda title: invoke(runtime, "signoffs_input_create", {"request_key": "daily-report", "form": {
            "title": title, "explanation": "Nothing was dropped.", "kind": "acknowledge", "selection_mode": "none"}},
            ctx=runtime.coordinator)["signoff"]
        invoke(runtime, "signoffs_input_create", {"request_key": "date", "form": {
            "title": "Choose a date", "explanation": "Which date works?", "kind": "question", "selection_mode": "none"}},
            ctx=runtime.coordinator)
        report("Daily report, 11 Nov")
        second = report("Daily report, 12 Nov")
        standing = self.read()["standing_now"]
        # The replaced report is gone; the question is the only decision waiting.
        self.assertEqual((standing["pending_signoffs"], standing["unacknowledged"]), (1, 1))
        self.assertEqual((standing["last_report"]["id"], standing["last_report"]["title"], standing["last_report"]["state"],
                          standing["last_report"]["acknowledged_at"]), (second["id"], "Daily report, 12 Nov", "pending", None))
        result = runtime.signoffs.respond(runtime.owner, second["id"], proposal_revision=second["proposal_revision"],
            payload_hash=second["payload_hash"], expected_status_revision=second["status_revision"], idempotency_key=new_id())
        self.assertEqual(result.status, "ok", result.error)
        review = self.read(EVER)
        standing = review["standing_now"]
        self.assertEqual((standing["pending_signoffs"], standing["unacknowledged"], standing["last_report"]["state"]), (1, 0, "answered"))
        self.assertIsNotNone(standing["last_report"]["acknowledged_at"])
        self.assertIn((second["id"], "acknowledged"),
                      [(item["id"], item["decision_state"]) for item in review["signoffs"]["decided"]["items"]])

    def test_owner_facing_output_is_counted_per_agent(self):
        task = self.task("Send the Linden contract")
        for author, audience, when in ((self.worker, "owner", at(9)), (self.worker, "owner", at(10)), (self.lead, "owner", at(11)),
                                       (self.worker, None, at(12)), (self.owner, None, at(13)), (self.worker, "owner", BEFORE)):
            self.write("INSERT INTO thread_messages(business_id,id,item_type,item_id,kind,body,author_id,recorder_id,attribution_json,"
                       "created_at) VALUES(NULL,?,'task',?,'comment','A note',?,?,?,?)",
                       (new_id(), task, author, author, json.dumps({"kind": "self", **({"audience": audience} if audience else {})}), when))
        chat = create(self.runtime, "Did the supplier confirm?")
        reply(self.runtime, chat, "Yes.")
        with self.runtime.database.transaction() as uow:
            for position, (origin, user_work) in enumerate((("user", False), ("nudge", True), ("nudge", False))):
                store.insert_reply(uow.connection, lineage=projection.Lineage(self.worker, "claude", "session", "archive"),
                                   kind="raw_range", key=str(position), session_id=None, receipt_id="receipt", offset=position * 10,
                                   length=5, completed_at=at(14, position), preview="Done.", origin=origin, user_work=user_work)
        found = self.read(EVER)["owner_facing"]
        self.assertEqual(found["notes_for_owner"]["count"], 4)
        # In the period: the notes agents posted for the owner, not working notes and not the owner's own.
        self.assertEqual(self.read()["owner_facing"]["notes_for_owner"], {"count": 3, "by": [
            {"id": self.worker, "name": "agent", "count": 2}, {"id": self.lead, "name": "agent", "count": 1}]})
        self.assertEqual(found["thread_replies"], 1)
        self.assertEqual(found["chat_replies"], {"count": 2, "by": [{"id": self.worker, "name": "agent", "count": 2}]})

    def test_standing_now_counts_open_work_by_the_same_rules_as_records_health(self):
        self.task("No worker")
        self.task("Held", workers=[self.worker])
        self.task("Stalled", status="working", workers=[self.worker])            # written on 1 November
        self.task("Parked", status="waiting", workers=[self.worker])
        self.task("Needs the owner", status="needs_input", workers=[self.lead])
        self.task("Closed", status="done")
        self.task("Acme Ltd", kind="customer")
        standing = self.read()["standing_now"]
        self.assertEqual(standing, {"as_of": "2026-11-12T18:00:00.000000Z", "open_work_tasks": 5, "open_without_worker": 1,
                                    "working_untouched": 1, "parked_untouched": 1, "parked_without_wake_time": 1,
                                    "needs_owner_input": 1, "pending_signoffs": 0, "unacknowledged": 0, "last_report": None})

    def test_a_section_that_cannot_be_read_is_named_and_the_others_still_answer(self):
        self.action("Sent the contract", self.worker, at(9))
        application = self.runtime.application
        authorize = application.authorize

        def refuse_actions(ctx, descriptor, *args, **kwargs):
            if descriptor.name == "actions_list":
                raise DomainError("policy_denied", "Not granted")
            return authorize(ctx, descriptor, *args, **kwargs)

        with patch.object(application, "authorize", side_effect=refuse_actions):
            review = self.read()
        self.assertEqual(review["actions"], {"unavailable": "policy_denied"})
        self.assertEqual(review["tasks"]["closed"]["count"], 0)
        self.assertEqual((review["since"], review["until"]), ("2026-11-11T18:00:00.000000Z", "2026-11-12T18:00:00.000000Z"))

    def test_only_the_owner_and_the_coordinator_in_person_may_read(self):
        self.assertEqual(self.read(ctx=self.runtime.owner)["tasks"]["created"]["count"], 0)
        refused = self.runtime.application.invoke(self.runtime.worker, "work_review_get", {"scope": "global"},
                                                  InvocationOptions(idempotency_key=new_id()))
        self.assertEqual(plain(refused.error)["code"], "forbidden")
        delegated = replace(self.runtime.coordinator, delegation_binding_id=new_id(), delegation_assignment_id=new_id(),
                            delegation_generation=1)
        with self.runtime.database.read() as uow, self.assertRaises(DomainError) as refusal:
            self.runtime.work_review.read(delegated, {"scope": "global"}, InvocationOptions(), uow)
        self.assertEqual(refusal.exception.code, "forbidden")


if __name__ == "__main__":
    unittest.main()
