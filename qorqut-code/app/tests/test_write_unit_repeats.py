"""A write asks each question once per database unit: its second unit does not repeat, inside itself, what it already
established there. The re-validation between the first and the second unit stays complete."""
from collections import Counter
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.application.engine import Application
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain
from qorqut_operations.storage.database import unit_memo

from qorqut.domains.settings_tools import SettingsTools

from private_thread_runtime import build_runtime

COUNTED = ("ensure_writes_open", "current_context", "_scope_plan", "_decisions", "check_targets")


class Units:
    """Count calls of the engine's checks by the database unit they ran in."""

    def __init__(self, case, *, after=None):
        self.calls = Counter()
        self.kinds = {}
        self.kept = []  # each unit's memo stays alive, so no two units share an id
        for name in COUNTED:
            real = getattr(Application, name)

            def counted(*args, _name=name, _real=real, **options):
                memo = unit_memo()
                self.kept.append(memo)
                uow = next((value for value in args if hasattr(value, "writable")), None)
                if uow is not None:
                    self.kinds[id(memo)] = "write" if uow.writable else "read"
                self.calls[(_name, id(memo))] += 1
                result = _real(*args, **options)
                if after is not None:
                    after(_name, uow)
                return result
            patch = mock.patch.object(Application, name, staticmethod(counted) if name == "ensure_writes_open" else counted)
            patch.start()
            case.addCleanup(patch.stop)

    def in_writes(self, name):
        return [count for (called, unit), count in self.calls.items()
                if called == name and self.kinds.get(unit) == "write"]


class SecondUnitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = runtime = build_runtime(Path(directory.name))
        self.addCleanup(runtime.database.close)
        runtime.registry = runtime.application.registry
        runtime.selection = SimpleNamespace(extensions=())
        SettingsTools(runtime, environment={}.get).register()

    def write(self):
        return self.runtime.application.invoke(
            self.runtime.coordinator, "settings_set",
            {"scope": "global", "setting": "testing_mode", "value": {"enabled": True}},
            InvocationOptions(idempotency_key=new_id()))

    def test_the_write_unit_reads_the_maintenance_state_once(self):
        units = Units(self)
        result = self.write()
        self.assertEqual("ok", result.status, plain(result.error))
        self.assertEqual([1], units.in_writes("ensure_writes_open"))          # it was [2] before
        # Still asked inside the write unit, once each, against the state that unit sees.
        self.assertEqual([1], units.in_writes("_decisions"))
        self.assertEqual([1], units.in_writes("check_targets"))
        # Refreshing the context twice is not a repeat: the second refresh can answer differently (its result carries
        # `context_refresh_required`), so both stay. The third is the settings handler's own.
        self.assertEqual([3], units.in_writes("current_context"))

    def test_maintenance_closed_between_the_two_units_still_refuses_the_write(self):
        database = self.runtime.database

        def close_after_preparing(name, uow):
            if name == "check_targets" and uow is not None and not uow.writable:
                with database.transaction() as other:
                    other.repository("maintenance_state").insert({"id": "service", "state": "closed",
                                                                  "message": "Closed for the test"})
        Units(self, after=close_after_preparing)
        result = self.write()
        self.assertEqual("error", result.status)
        self.assertEqual("maintenance", plain(result.error)["code"])

    def test_every_other_caller_of_the_validation_still_reads_the_maintenance_state(self):
        refused = DomainError("maintenance", "Closed", retryable=True)
        engine = SimpleNamespace(ensure_writes_open=mock.Mock(side_effect=refused))
        with self.assertRaises(DomainError) as caught:
            Application.validate_prepared(engine, object(), object(), object())
        self.assertEqual("maintenance", caught.exception.code)
        engine.ensure_writes_open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
