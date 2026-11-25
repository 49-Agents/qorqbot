"""The owner's timezone: one runtime-changeable clock. Owner setting > QORQUT_TIMEZONE > UTC."""
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut_operations import local_time
from qorqut_operations.contracts import DomainError

from qorqut.dashboard_preferences import apply_operational_timezone, operational_timezone
from qorqut.experience.runs import zone_name
from qorqut.transport.http import HTTPAPI


class OwnerTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(local_time.set_owner_timezone, None)
        local_time.set_owner_timezone(None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def test_precedence_is_owner_then_environment_then_utc(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("QORQUT_TIMEZONE", None)
            with patch.object(local_time, "ENVIRONMENT_TIMEZONE", "UTC"):
                self.assertEqual(operational_timezone(self.directory), {"timezone": "UTC", "source": "default"})
        with patch.dict(os.environ, {"QORQUT_TIMEZONE": "Europe/Berlin"}), patch.object(local_time, "ENVIRONMENT_TIMEZONE", "Europe/Berlin"):
            self.assertEqual(operational_timezone(self.directory), {"timezone": "Europe/Berlin", "source": "environment"})
            self.assertEqual(operational_timezone(self.directory, {"timezone": "America/Puerto_Rico"}),
                             {"timezone": "America/Puerto_Rico", "source": "owner"})
            self.assertEqual(local_time.current_timezone(), "America/Puerto_Rico")
            local_time.set_owner_timezone(None)
            self.assertEqual(local_time.current_timezone(), "Europe/Berlin")

    def test_a_change_takes_effect_without_restart_in_every_reader(self):
        instant = datetime(2026, 11, 25, 2, 30, tzinfo=timezone.utc)
        operational_timezone(self.directory, {"timezone": "UTC"})
        self.assertEqual(local_time.local_date(instant), "2026-11-25")
        operational_timezone(self.directory, {"timezone": "America/Puerto_Rico"})
        self.assertEqual(local_time.local_date(instant), "2026-11-24")
        self.assertEqual(local_time.now_view(lambda: instant)["timezone"], "America/Puerto_Rico")
        self.assertEqual(local_time.now_view(lambda: instant)["utc_offset"], "-0400")
        self.assertEqual(local_time.current_label(), "America/Puerto_Rico time")
        self.assertEqual(local_time.TIMEZONE, "America/Puerto_Rico")          # compatibility attribute is live
        self.assertEqual(local_time.recorded_time("2026-11-25T02:30:00Z")["time_zone"], "America/Puerto_Rico")

    def test_the_saved_value_is_applied_at_startup_and_survives(self):
        operational_timezone(self.directory, {"timezone": "Asia/Tokyo"})
        local_time.set_owner_timezone(None)
        apply_operational_timezone(self.directory)
        self.assertEqual(local_time.current_timezone(), "Asia/Tokyo")
        self.assertEqual(local_time.timezone_source(), "owner")

    def test_an_unusable_saved_value_does_not_stop_startup(self):
        path = Path(self.directory, "dashboard-preferences")
        path.mkdir()
        path.joinpath("operational-timezone.json").write_text('{"timezone": "Mars/Base"}')
        apply_operational_timezone(self.directory)
        self.assertNotEqual(local_time.timezone_source(), "owner")

    def test_invalid_zones_are_refused_and_change_nothing(self):
        operational_timezone(self.directory, {"timezone": "Europe/Paris"})
        for bad in ("Mars/Base", "", " UTC", "../../etc/passwd", "/etc/localtime", None, 5, "UTC\n"):
            with self.assertRaises(DomainError, msg=repr(bad)):
                operational_timezone(self.directory, {"timezone": bad})
        for bad in ({}, {"timezone": "UTC", "x": 1}, "UTC"):
            with self.assertRaises(DomainError):
                operational_timezone(self.directory, bad)
        self.assertEqual(operational_timezone(self.directory)["timezone"], "Europe/Paris")

    def test_the_session_reports_the_zone_and_its_source(self):
        class Uow:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def repository(self, name): return SimpleNamespace(get=lambda key: {"owner_principal_id": "me"})
        runtime = SimpleNamespace(database=SimpleNamespace(read=lambda: Uow()))
        actor = SimpleNamespace(principal=SimpleNamespace(id="me", display_name="Me"))
        api = SimpleNamespace(runtime=runtime)
        self.assertEqual(HTTPAPI.session_data(api, actor)["operational_timezone_source"],
                         "environment" if "QORQUT_TIMEZONE" in os.environ else "default")
        operational_timezone(self.directory, {"timezone": "America/Puerto_Rico"})
        session = HTTPAPI.session_data(api, actor)
        self.assertEqual((session["operational_timezone"], session["operational_timezone_source"]), ("America/Puerto_Rico", "owner"))

    def test_experience_schedule_zone_order(self):
        system = lambda: "America/La_Paz"
        operational_timezone(self.directory, {"timezone": "Asia/Tokyo"})
        self.assertEqual(zone_name({"QORQUT_TIMEZONE": "Europe/Berlin"}, system), "Asia/Tokyo")
        self.assertEqual(zone_name({"QORQUT_EXPERIENCE_TIMEZONE": "Europe/Paris", "QORQUT_TIMEZONE": "Europe/Berlin"}, system), "Europe/Paris")
        local_time.set_owner_timezone(None)
        self.assertEqual(zone_name({"QORQUT_TIMEZONE": "Europe/Berlin"}, system), "Europe/Berlin")


if __name__ == "__main__":
    unittest.main()
