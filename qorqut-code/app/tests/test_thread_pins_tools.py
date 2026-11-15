"""The owner's pinned-threads row as tools: the coordinator reads, pins and unpins; nobody else does."""
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain

from qorqut.dashboard_preferences import thread_pins
from qorqut.domains.owner_inputs import OwnerInputs
from qorqut.domains.thread_pins import ThreadPins
from qorqut.inbox.private_conversations import PrivateConversations
from qorqut.service.changes import _coarse_kinds_for_operation

from test_side_chat_rename import build_runtime, create, invoke, source


class ThreadPinsToolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = runtime = build_runtime(Path(directory.name))
        self.addCleanup(runtime.database.close)
        runtime.registry = runtime.application.registry
        runtime.application.attention = SimpleNamespace(legacy=SimpleNamespace(
            resolve=lambda ctx, found, uow: {**found, "label": "Supplier question", "business_slug": None}))
        OwnerInputs(runtime).register()
        ThreadPins(runtime).register()
        self.owner, self.lead = runtime.owner.principal_id, runtime.coordinator.principal_id

    def pins(self, owner=None):
        return thread_pins(self.runtime.paths.runtime, owner or self.owner)["threads"]

    def refused(self, ctx, operation, args):
        result = self.runtime.application.invoke(ctx, operation, {"scope": "global", **args}, InvocationOptions(idempotency_key=new_id()))
        return None if result.status == "ok" else plain(result.error)["code"]

    def pin(self, src, pinned=True, ctx=None, **more):
        return invoke(self.runtime, "thread_pins_set", {"source": src, "pinned": pinned, **more}, ctx=ctx or self.runtime.coordinator)

    def test_the_coordinator_lists_pins_and_unpins_in_the_owners_file(self):
        src = source(create(self.runtime, "Did the supplier confirm?"))
        self.assertEqual(invoke(self.runtime, "thread_pins_list", ctx=self.runtime.coordinator), {"threads": []})
        done = self.pin(src)
        self.assertEqual((done["pinned"], done["pinned_count"]), (True, 1))
        # The owner's file changed: the record's own title was stored; the coordinator's own file did not exist.
        self.assertEqual(self.pins(), [{"source": src, "title": "Supplier question"}])
        self.assertEqual(self.pins(self.lead), [])
        listed = invoke(self.runtime, "thread_pins_list", ctx=self.runtime.coordinator)["threads"]
        self.assertEqual(listed, [{"source": src, "title": "Supplier question", "readable": True}])
        self.pin(src, title="Chosen name")
        self.assertEqual(self.pins(), [{"source": src, "title": "Chosen name"}])
        self.pin(src, False)
        self.pin(src, False)
        self.assertEqual(self.pins(), [])

    def test_a_worker_and_a_delegated_identity_are_refused(self):
        src = source(create(self.runtime))
        self.assertEqual(self.refused(self.runtime.worker, "thread_pins_list", {}), "forbidden")
        self.assertEqual(self.refused(self.runtime.worker, "thread_pins_set", {"source": src, "pinned": True}), "forbidden")
        delegated = replace(self.runtime.coordinator, delegation_binding_id=new_id(), delegation_assignment_id=new_id(),
                            delegation_generation=1)
        with self.runtime.database.read() as uow:
            with self.assertRaises(DomainError) as refusal:
                self.runtime.thread_pins.caller(delegated, uow)
        self.assertEqual(refusal.exception.code, "forbidden")
        self.assertEqual(self.pins(), [])

    def test_a_missing_or_unreadable_record_is_not_pinned_but_can_be_unpinned(self):
        src = source(create(self.runtime))
        missing = {**src, "id": new_id()}
        self.assertEqual(self.refused(self.runtime.coordinator, "thread_pins_set", {"source": missing, "pinned": True}), "not_found")
        self.assertEqual(self.refused(self.runtime.coordinator, "thread_pins_set",
                                      {"source": {**src, "type": "nonsense"}, "pinned": True}), "not_found")
        self.pin(src)
        with patch.object(PrivateConversations, "source_check", side_effect=DomainError("forbidden", "No access")):
            self.assertEqual(self.refused(self.runtime.coordinator, "thread_pins_set",
                                          {"source": {**src, "id": new_id()}, "pinned": True}), "not_found")
            # A stored pin the caller can no longer read is listed without a title, and can still be removed.
            self.assertEqual(invoke(self.runtime, "thread_pins_list", ctx=self.runtime.coordinator)["threads"],
                             [{"source": src, "title": None, "readable": False}])
            self.pin(src, False)
        self.assertEqual(self.pins(), [])

    def test_the_arguments_are_checked_and_pin_writes_wake_the_dashboard(self):
        src = source(create(self.runtime))
        for bad in ({"source": {**src, "extra": 1}, "pinned": True}, {"source": src, "pinned": "yes"},
                    {"source": src, "pinned": True, "title": "x" * 201}, {"source": {**src, "type": "Task"}, "pinned": True}):
            self.assertEqual(self.refused(self.runtime.coordinator, "thread_pins_set", bad), "validation_error")
        self.assertEqual(_coarse_kinds_for_operation("thread_pins_set"), {"thread_pins"})


if __name__ == "__main__":
    unittest.main()
