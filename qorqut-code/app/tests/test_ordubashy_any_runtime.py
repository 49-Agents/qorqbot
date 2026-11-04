"""Ordubashy is the same coordinator whichever supported CLI the owner starts it with."""
from datetime import datetime, timezone
import json
import sqlite3
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from bazarlyk.management import identity as identity_module
from bazarlyk.management.service import ManagementService

COORDINATOR = "agent-ordubashy"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


class Installation:
    def __init__(self, coordinator):
        self.row = {"management_coordinator_id": coordinator}

    def get(self, key):
        return self.row if key == "service" else None


class FakeUow:
    def __init__(self, coordinator=COORDINATOR):
        self.installation = Installation(coordinator)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE principals (id TEXT PRIMARY KEY, display_name TEXT, active INTEGER);
            CREATE TABLE agent_profiles (agent_id TEXT PRIMARY KEY, status TEXT, profile_json TEXT);
            CREATE TABLE agent_activity (agent_id TEXT, state TEXT, last_seen_at TEXT, runtime TEXT);
        """)

    def repository(self, name):
        assert name == "installation"
        return self.installation


def global_profile(runtime, role="ordubashy"):
    return {"role": {"id": role}, "runtime": runtime, "scope": {"kind": "ecosystem"}}


class IdentityTests(unittest.TestCase):
    def check(self, data):
        with mock.patch.object(identity_module, "active_global", return_value=data):
            uow = FakeUow()
            return identity_module.configured_ordubashy(uow), uow

    def test_coordinator_is_configured_on_codex_and_claude_code(self):
        for runtime in ("codex", "claude-code"):
            with self.subTest(runtime=runtime):
                configured, uow = self.check(global_profile(runtime))
                self.assertEqual(configured, COORDINATOR)
                with mock.patch.object(identity_module, "active_global", return_value=global_profile(runtime)):
                    self.assertEqual(identity_module.require_coordinator(uow), COORDINATOR)

    def test_another_role_is_never_the_coordinator(self):
        configured, uow = self.check(global_profile("claude-code", role="global-worker"))
        self.assertIsNone(configured)
        with mock.patch.object(identity_module, "active_global", return_value=global_profile("codex", role="global-worker")):
            with self.assertRaisesRegex(DomainError, "not an active Ordubashy"):
                identity_module.require_coordinator(uow)

    def test_inactive_or_missing_profile_is_not_the_coordinator(self):
        configured, _ = self.check(None)
        self.assertIsNone(configured)

    def test_no_configured_coordinator_keeps_its_own_refusal(self):
        with self.assertRaisesRegex(DomainError, "No management coordinator is configured"):
            identity_module.require_coordinator(FakeUow(coordinator=None))


class CoordinatorAvailabilityTests(unittest.TestCase):
    def dto(self, profile_runtime, observed_runtime, state="running", seen=NOW):
        uow = FakeUow()
        uow.connection.execute("INSERT INTO principals VALUES (?,?,1)", (COORDINATOR, "Ordubashy"))
        uow.connection.execute("INSERT INTO agent_profiles VALUES (?, 'active', ?)",
                               (COORDINATOR, json.dumps(global_profile(profile_runtime))))
        uow.connection.execute("INSERT INTO agent_activity VALUES (?,?,?,?)",
                               (COORDINATOR, state, seen.isoformat().replace("+00:00", "Z"), observed_runtime))
        service = ManagementService.__new__(ManagementService)
        service.clock = lambda: NOW
        with mock.patch.object(identity_module, "active_global", return_value=global_profile(profile_runtime)):
            return service.coordinator_dto(uow)

    def test_live_session_on_either_cli_makes_the_coordinator_available(self):
        for profile_runtime in ("codex", "claude-code"):
            for observed_runtime in ("codex", "claude-code"):
                with self.subTest(profile=profile_runtime, observed=observed_runtime):
                    dto = self.dto(profile_runtime, observed_runtime)
                    self.assertEqual(dto["status"], "available")
                    self.assertTrue(dto["available"])

    def test_offline_or_stale_session_is_configured_but_unavailable(self):
        self.assertEqual(self.dto("claude-code", "claude-code", state="offline")["status"], "configured_unobserved")
        stale = datetime(2026, 11, 3, 11, 0, tzinfo=timezone.utc)
        self.assertEqual(self.dto("claude-code", "claude-code", seen=stale)["status"], "configured_unobserved")

    def test_unknown_runtime_is_not_a_live_coordinator(self):
        self.assertFalse(self.dto("codex", "gemini")["available"])


if __name__ == "__main__":
    unittest.main()
