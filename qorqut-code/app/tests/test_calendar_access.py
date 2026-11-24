"""Who may use the calendar tools, and how often one calendar call reads the caller's agent profile per database unit."""
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from uuid import uuid4

from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal, new_id, plain
from qorqut_operations.storage.database import unit_memo

from qorqut.calendar.operations import CAPABILITY, CalendarOperations

from private_thread_runtime import build_runtime


class Provider:
    """A calendar provider that answers without any network."""

    def __init__(self, connection_id, config, *, transport=None):
        self.connection_id = connection_id

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def calendars(self):
        return [{"connection_id": self.connection_id, "calendar_id": "primary"}]

    def mutate(self, operation, arguments):
        return {"event": {"event_id": "event-1", "revision": "1"}}


class CalendarAccessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = runtime = build_runtime(Path(directory.name))
        self.addCleanup(runtime.database.close)
        runtime.registry = runtime.application.registry
        self.profile_reads = Counter()
        self.kept = []  # each unit's memo stays alive, so no two units share an id

        def profile(principal, *, connection):
            memo = unit_memo()
            self.kept.append(memo)
            self.profile_reads[id(memo)] += 1
            row = connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (principal.id,)).fetchone()
            return SimpleNamespace(data=json.loads(row[0])), None
        runtime.resolver = SimpleNamespace(profile=profile)
        self.calendar = CalendarOperations(runtime, config={"main": {"provider": "test"}},
                                           providers={"test": Provider}).register()
        granted = {"read_global_context", CAPABILITY}
        for agent in (runtime.coordinator, runtime.worker):
            self.grant(agent.principal_id, granted)
        self.owner = replace(runtime.owner, global_capabilities=frozenset(granted))
        self.coordinator = replace(runtime.coordinator, global_capabilities=frozenset(granted))
        self.worker = replace(runtime.worker, global_capabilities=frozenset(granted))

    def grant(self, agent_id, capabilities):
        with self.runtime.database.transaction() as uow:
            row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
            data = json.loads(row[0])
            data["scope"]["global_capabilities"] = sorted(capabilities)
            uow.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", (json.dumps(data), agent_id))

    def call(self, ctx, operation, arguments):
        return self.runtime.application.invoke(ctx, operation, arguments,
                                               InvocationOptions(idempotency_key=new_id()))

    def create(self, ctx):
        return self.call(ctx, "calendar_event_create", {"connection_id": "main", "calendar_id": "primary",
                                                        "title": "Meeting", "start": "2026-12-01", "end": "2026-12-02"})

    def refused(self, ctx):
        with self.runtime.database.read() as uow:
            with self.assertRaises(DomainError) as caught:
                self.calendar.access(ctx, uow)
        return caught.exception.code

    def test_the_owner_the_coordinator_and_a_worker_may_use_the_calendar(self):
        for ctx in (self.owner, self.coordinator, self.worker):
            result = self.create(ctx)
            self.assertEqual("ok", result.status, plain(result.error))
            listed = self.call(ctx, "calendar_calendars_list", {})
            self.assertEqual("ok", listed.status, plain(listed.error))

    def test_anyone_else_is_refused(self):
        stranger = InvocationContext(Principal(str(uuid4()), "human"), source="dashboard",
                                     global_capabilities={CAPABILITY})
        self.assertEqual("forbidden", self.refused(stranger))
        self.assertEqual("forbidden", self.refused(replace(self.coordinator, global_capabilities=frozenset())))
        with self.runtime.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET status='inactive', profile_json=json_set(profile_json,"
                                   "'$.status','inactive') WHERE agent_id=?", (self.worker.principal_id,))
        self.assertEqual("forbidden", self.refused(self.worker))

    def test_a_profile_changed_inside_the_unit_is_seen_by_the_next_check_in_that_unit(self):
        class Undo(Exception):
            pass
        with self.assertRaises(Undo):
            with self.runtime.database.transaction() as uow:
                self.calendar.access(self.worker, uow)
                uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.status','inactive') "
                                       "WHERE agent_id=?", (self.worker.principal_id,))
                with self.assertRaises(DomainError) as caught:
                    self.calendar.access(self.worker, uow)
                self.assertEqual("forbidden", caught.exception.code)
                raise Undo()
        self.assertIsNone(self.refused_or_none(self.worker))      # the change was rolled back with its unit

    def refused_or_none(self, ctx):
        with self.runtime.database.read() as uow:
            try:
                self.calendar.access(ctx, uow)
            except DomainError as error:
                return error.code
        return None

    def test_a_calendar_write_reads_the_agent_profile_per_unit(self):
        self.profile_reads.clear()
        result = self.create(self.worker)
        self.assertEqual("ok", result.status, plain(result.error))
        # The read unit checks in prepare and in validate_targets, the write unit in validate_targets and in apply:
        # each unit reads the profile once (it was twice before the grant was kept for the unit).
        self.assertEqual([1, 1], sorted(self.profile_reads.values()))


if __name__ == "__main__":
    unittest.main()
