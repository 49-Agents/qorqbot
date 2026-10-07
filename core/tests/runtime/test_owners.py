import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from pydantic import ValidationError

from qorqut_core.adapters.capabilities import VERSIONS
from qorqut_core.contracts import CoreError
from qorqut_core.runtime import OwnerCandidate, SessionConflict, SessionOwners, observe_process
from runtime.support import RuntimeCase
from test_foundation import InjectedCrash


class Owners(RuntimeCase):
    def test_owned_children_race_reconnect_reopen_exit_and_stale_release(self):
        children = [self.child(), self.child()]
        first = self.candidate(children[0][1])
        # Two actual processes resuming the same native UUID still compete.
        candidates = [first, first.model_copy(update={"process": children[1][1]})]
        barrier, seen, lock = threading.Barrier(2), set(), threading.Lock()

        def observe(process):
            with lock:
                fresh = threading.get_ident() not in seen
                seen.add(threading.get_ident())
            if fresh:
                barrier.wait(timeout=5)
            return observe_process(process, clock=self.clock)

        owners = SessionOwners(self.app, observe_process=observe)

        def connect(candidate):
            try:
                return owners.connect(candidate)
            except SessionConflict as error:
                return error

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(connect, candidates))
        winner = next(i for i, result in enumerate(results) if not isinstance(result, SessionConflict))
        connected, conflict = results[winner], results[1 - winner]
        self.assertEqual((conflict.code, conflict.reason), ("invalid_transition", "session_conflict"))
        self.assertEqual(str(conflict), "Another session is connected to this agent. Tell the user and wait until that session is closed.")
        self.assertEqual(connected.run_id, connected.owner_id)
        self.assertEqual(connected.run_generation, 1)
        self.assertEqual(len(self.rows("session_owners")), 1)
        self.assertEqual([e["kind"] for e in self.session_events()], ["session.connected"])

        owners = self.app.session_owners
        candidate = candidates[winner]
        self.assertEqual(owners.connect(candidate).outcome, "reconnected")
        self.assertFalse(owners.release(connected.owner_id, candidate.process))  # Stop hook is insufficient.
        for change in ({"native_session_id": uuid4()}, {"terminal_id": uuid4()},
                       {"provider": "claude", "cli_version": VERSIONS["claude"]},
                       {"environment_generation": 2}):
            with self.subTest(change=change), self.assertRaises(SessionConflict):
                owners.connect(candidate.model_copy(update=change))
        self.assertEqual(len(self.session_events()), 1)
        self.restart()
        owners = self.app.session_owners
        self.assertEqual(owners.connect(candidate).owner_id, connected.owner_id)
        self.exit_child(children[winner][0])
        replacement = owners.connect(candidates[1 - winner])
        self.assertEqual(replacement.run_generation, 2)
        self.assertNotEqual(replacement.owner_id, connected.owner_id)
        self.assertFalse(owners.release(connected.owner_id, candidate.process))
        self.assertFalse(owners.release(replacement.owner_id, candidate.process))
        with self.assertRaises(CoreError) as dead:
            owners.connect(candidate)
        self.assertEqual(dead.exception.code, "not_ready")
        self.exit_child(children[1 - winner][0])
        owners.reconcile()
        self.assertEqual(self.runtime()["status"], "disconnected")
        rows = self.rows("session_owners")
        self.assertEqual([row["state"] for row in rows], ["closed", "closed"])
        self.assertTrue(all(row["observation"] == "exited" for row in rows))
        self.assertTrue(all(event["actor_id"] == self.host["actor_id"] for event in self.session_events()))

    def test_unknown_retention_freshness_and_local_admission(self):
        candidate = self.candidate()
        self.state(candidate, "unknown")
        with self.assertRaises(CoreError) as unknown:
            self.owners.connect(candidate)
        self.assertEqual(unknown.exception.code, "unavailable")
        self.assertEqual(self.rows("session_owners"), [])
        self.state(candidate, "live")
        connected = self.owners.connect(candidate)
        self.assertEqual(self.runtime()["status"], "connected")
        self.clock.advance(5)
        self.assertEqual(self.runtime()["status"], "connected")
        self.clock.advance(0.000001)
        self.assertEqual(self.runtime()["status"], "unknown")
        self.assertEqual(self.rows("session_owners")[0]["state"], "active")
        before = self.rows("session_owners"), self.session_events(), list(self.observed)
        view = self.runtime()
        self.assertEqual((self.rows("session_owners"), self.session_events(), self.observed), before)
        self.assertFalse(view["input_ready"])
        self.assertEqual(view["current_owner"]["run_id"], str(connected.owner_id))
        self.assertNotIn("process_json", view["current_owner"])
        self.assertNotIn("process", view["current_owner"])

        self.state(candidate, PermissionError("synthetic private process inspection failure"))
        self.owners.reconcile()
        events = len(self.session_events())
        self.owners.reconcile()
        self.assertFalse(self.owners.release(connected.owner_id, candidate.process))
        self.assertEqual(len(self.session_events()), events)
        rival = self.candidate()
        with self.assertRaises(CoreError) as unknown:
            self.owners.connect(rival)
        self.assertEqual(unknown.exception.code, "unavailable")
        self.assertNotIsInstance(unknown.exception, SessionConflict)
        self.assertIn("Existing connection could not be verified", str(unknown.exception))
        self.assertEqual(self.rows("session_owners")[0]["owner_id"], str(connected.owner_id))
        self.state(candidate, "live")
        self.assertEqual(self.owners.connect(candidate).outcome, "reconnected")
        self.assertEqual(self.runtime()["status"], "connected")
        self.assertEqual([json.loads(e["payload_json"])["observation"] for e in self.session_events()
                          if e["kind"] == "session.observation"], ["unknown", "live"])

        vm = self.create(start=False).data["agent"]["id"]
        for change, code in (({"agent_id": UUID(vm)}, "unsupported_capability"),
                             ({"agent_id": uuid4()}, "not_found"),
                             ({"recovery_epoch": uuid4()}, "target_changed"),
                             ({"cli_version": "unsupported"}, "unsupported_capability")):
            seen = len(self.observed)
            with self.subTest(change=change), self.assertRaises(CoreError) as failure:
                self.owners.connect(candidate.model_copy(update=change))
            self.assertEqual(failure.exception.code, code)
            self.assertEqual(len(self.observed), seen)
        with self.assertRaises(ValidationError):
            OwnerCandidate.model_validate({**candidate.model_dump(), "environment_generation": True})
        self.assertEqual(len(self.app.registry), 15)
        self.assertEqual(len(self.app.jobs.handlers), 5)
        self.assertNotIn("session_connect", self.app.registry)

    def test_stale_release_and_reconcile_cannot_touch_replacement(self):
        first, second = self.candidate(), self.candidate()
        old = self.owners.connect(first)
        entered, resume = threading.Event(), threading.Event()

        def delayed(process):
            observed = self.observe(process)
            entered.set()
            self.assertTrue(resume.wait(5))
            return observed

        stale = SessionOwners(self.app, observe_process=delayed)
        self.state(first, "exited")
        with ThreadPoolExecutor(1) as pool:
            release = pool.submit(stale.release, old.owner_id, first.process)
            self.assertTrue(entered.wait(5))
            try:
                replacement = self.owners.connect(second)
            finally:
                resume.set()
            self.assertFalse(release.result(timeout=5))
        self.assertEqual(self.runtime()["current_owner"]["owner_id"], str(replacement.owner_id))

        entered.clear()
        resume.clear()
        self.state(second, "exited")
        with ThreadPoolExecutor(1) as pool:
            reconciliation = pool.submit(stale.reconcile)
            self.assertTrue(entered.wait(5))
            try:
                self.assertTrue(self.recovery().ok)
                third = self.candidate()
                final = self.owners.connect(third)
            finally:
                resume.set()
            reconciliation.result(timeout=5)
        self.assertEqual(self.runtime()["current_owner"]["owner_id"], str(final.owner_id))
        self.assertEqual([row["state"] for row in self.rows("session_owners")], ["closed", "retired", "active"])

    def test_three_retries_and_epoch_change_never_apply_stale_observations(self):
        first, second = self.candidate(), self.candidate()
        old = self.owners.connect(first)
        calls = []

        def changes_owner_observation(process):
            calls.append(process)
            self.clock.advance(1)
            self.owners.connect(first)
            return self.observe(process)

        changing = SessionOwners(self.app, observe_process=changes_owner_observation)
        with self.assertRaises(CoreError) as retry:
            changing.connect(second)
        self.assertEqual(retry.exception.code, "not_ready")
        self.assertTrue(retry.exception.error.retryable)
        self.assertEqual(len(calls), 6)  # Candidate and saved incumbent, at most three tries.
        self.assertEqual(self.runtime()["current_owner"]["owner_id"], str(old.owner_id))
        self.assertEqual(len(self.session_events()), 1)

        def restore_during_observation(process):
            self.recovery()
            return self.observe(process)

        changing = SessionOwners(self.app, observe_process=restore_during_observation)
        with self.assertRaises(CoreError) as retired:
            changing.connect(second)
        self.assertEqual(retired.exception.code, "target_changed")
        self.assertEqual(self.runtime()["status"], "disconnected")
        self.assertEqual(self.rows("session_owners")[0]["state"], "retired")

    def test_claim_replacement_and_recovery_are_atomic_with_events(self):
        first, second = self.candidate(), self.candidate()
        before = self.rows("session_owners"), self.session_events()
        self.fault.point = "session_before_commit"
        with self.assertRaises(InjectedCrash):
            self.owners.connect(first)
        self.assertEqual((self.rows("session_owners"), self.session_events()), before)
        connected = self.owners.connect(first)
        self.state(first, "exited")
        before = self.rows("session_owners"), self.session_events()
        self.fault.point = "session_before_commit"
        with self.assertRaises(InjectedCrash):
            self.owners.connect(second)
        self.assertEqual((self.rows("session_owners"), self.session_events()), before)
        original_epoch = self.config()["recovery_epoch"]
        self.fault.point = "operation_after_result"
        with self.assertRaises(InjectedCrash):
            self.recovery()
        self.assertEqual(self.config()["recovery_epoch"], original_epoch)
        self.assertEqual((self.rows("session_owners"), self.session_events()), before)
        self.assertTrue(self.recovery().ok)
        retired = self.session_events()[-1]
        self.assertEqual((retired["kind"], retired["actor_id"]), ("session.retired", str(self.owner.actor_id)))
        self.assertEqual(json.loads(retired["payload_json"])["owner_id"], str(connected.owner_id))
        self.assertIsNotNone(retired["operation_id"])
        self.restart()
        self.owners = SessionOwners(self.app, observe_process=self.observe)
        replacement = self.owners.connect(self.candidate(second.process))
        self.assertEqual(replacement.run_generation, 2)
