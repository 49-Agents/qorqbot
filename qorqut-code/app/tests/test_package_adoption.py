"""The offline package adoption: which pins move, that they only move forward, that nothing is done twice."""
import json
import sqlite3
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from qorqut.migration import package_adoption as adoption
from qorqut_operations.contracts import DomainError

REFS = {("role", "ordubashy"): {"id": "ordubashy", "version": "1.79.0", "digest": "sha256:" + "a" * 64},
        ("role", "global-worker"): {"id": "global-worker", "version": "1.9.0", "digest": "sha256:" + "b" * 64},
        ("plugin", "agent-terminal-control"): {"id": "agent-terminal-control", "version": "1.2.0", "digest": "sha256:" + "c" * 64},
        ("plugin", "email-management"): {"id": "email-management", "version": "3.2.0", "digest": "sha256:" + "d" * 64}}


def pin(id_, version, digest="x"):
    return {"id": id_, "version": version, "digest": "sha256:" + digest * 64}


def grant(id_, version, config=None):
    return {**pin(id_, version), "config": config or {}}


def profile(role, version, plugins=()):
    return {"role": pin(role, version), "plugins": list(plugins)}


class ProfileChangesTest(unittest.TestCase):
    def test_an_installation_made_by_a_released_version_gets_the_control_plugin_and_the_new_role(self):
        changes, notes = adoption.profile_changes(profile("ordubashy", "1.78.0", [grant("email-management", "3.2.0")]), REFS)
        self.assertEqual(changes["role"], REFS[("role", "ordubashy")])
        self.assertEqual([item["id"] for item in changes["plugins"]], ["email-management", "agent-terminal-control"])
        self.assertEqual(changes["plugins"][1], {**REFS[("plugin", "agent-terminal-control")], "config": {}})
        self.assertEqual(changes["plugins"][0], grant("email-management", "3.2.0"))
        self.assertEqual(notes, ["role ordubashy 1.78.0 -> 1.79.0", "plugin agent-terminal-control added at 1.2.0"])

    def test_an_existing_pin_moves_up_and_keeps_its_config(self):
        held = grant("agent-terminal-control", "1.1.0", {"kept": True})
        changes, notes = adoption.profile_changes(profile("ordubashy", "1.79.0", [held, grant("email-management", "3.2.0")]), REFS)
        self.assertNotIn("role", changes)
        self.assertEqual(changes["plugins"][0], {**REFS[("plugin", "agent-terminal-control")], "config": {"kept": True}})
        self.assertEqual(notes, ["plugin agent-terminal-control 1.1.0 -> 1.2.0"])

    def test_a_current_profile_is_not_touched(self):
        current = profile("ordubashy", "1.79.0", [grant("agent-terminal-control", "1.2.0"), grant("email-management", "3.2.0")])
        self.assertEqual(adoption.profile_changes(current, REFS), ({}, []))

    def test_a_pin_newer_than_the_release_is_never_moved_back(self):
        newer = profile("ordubashy", "1.80.0", [grant("agent-terminal-control", "1.10.0")])
        self.assertEqual(adoption.profile_changes(newer, REFS), ({}, []))

    def test_only_ordubashy_is_given_the_control_plugin(self):
        changes, _ = adoption.profile_changes(profile("global-worker", "1.8.0"), REFS)
        self.assertEqual(changes, {"role": REFS[("role", "global-worker")]})

    def test_a_role_the_release_does_not_ship_is_left_alone(self):
        self.assertEqual(adoption.profile_changes(profile("retired-role", "1.0.0"), REFS), ({}, []))


class Application:
    def __init__(self, refuse=None, fail_with=None):
        self.calls, self.refuse, self.fail_with = [], refuse, fail_with

    def invoke(self, context, operation, arguments, options):
        if self.fail_with:
            raise self.fail_with
        self.calls.append((operation, arguments, options.idempotency_key, options.expected_revision))
        return SimpleNamespace(status="refused" if operation == self.refuse else "ok", error="no", data={})


class ApplyTest(unittest.TestCase):
    PLAN = {"packages": [{"source": "agent-terminal-control", "kind": "plugin", "id": "agent-terminal-control", "version": "1.2.0", "action": "publish"},
                         {"source": "ordubashy", "kind": "role", "id": "ordubashy", "version": "1.79.0", "action": "publish"},
                         {"source": "email-management", "kind": "plugin", "id": "email-management", "version": "3.2.0", "action": "present"}],
            "profiles": [{"agent_id": "a1", "name": "ordubashy", "revision": 4, "changes": {"role": REFS[("role", "ordubashy")]},
                          "summary": ["role ordubashy 1.78.0 -> 1.79.0"]}], "conflicts": []}

    def test_publishes_what_is_missing_then_updates_each_profile_once_at_its_revision(self):
        application = Application()
        done = adoption.apply(SimpleNamespace(application=application), object(), self.PLAN)
        self.assertEqual([(call[0], call[1].get("source_id") or call[1]["agent_id"], call[3]) for call in application.calls],
                         [("plugins_publish", "agent-terminal-control", None), ("roles_publish", "ordubashy", None), ("agents_update", "a1", 4)])
        self.assertEqual(done["published"], ["plugin agent-terminal-control@1.2.0", "role ordubashy@1.79.0"])
        self.assertEqual(done["profiles"][0]["name"], "ordubashy")

    def test_a_current_installation_has_nothing_to_do(self):
        application = Application()
        nothing = {"packages": [{**self.PLAN["packages"][2]}], "profiles": [], "conflicts": []}
        self.assertEqual(adoption.apply(SimpleNamespace(application=application), object(), nothing), {"published": [], "profiles": []})
        self.assertEqual(application.calls, [])

    def test_a_refusal_stops_the_run_with_the_reason(self):
        with self.assertRaises(DomainError) as stopped:
            adoption.apply(SimpleNamespace(application=Application(refuse="agents_update")), object(), self.PLAN)
        self.assertEqual(stopped.exception.code, "adoption_failed")

    def test_instructions_that_do_not_match_the_code_are_named(self):
        with self.assertRaises(DomainError) as stopped:
            adoption.apply(SimpleNamespace(application=Application(fail_with=ValueError("Tool instruction IDs do not match"))), object(), self.PLAN)
        self.assertEqual(stopped.exception.code, "instructions_out_of_date")
        self.assertIn("instruction update", str(stopped.exception))


class PlanTest(unittest.TestCase):
    def runtime(self, artifacts, profiles):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE artifacts(kind TEXT, artifact_id TEXT, version TEXT, digest TEXT)")
        connection.execute("CREATE TABLE agent_profiles(agent_id TEXT, name TEXT, revision INTEGER, status TEXT, profile_json TEXT)")
        connection.executemany("INSERT INTO artifacts VALUES(?,?,?,?)", artifacts)
        connection.executemany("INSERT INTO agent_profiles VALUES(?,?,?,?,?)", profiles)

        @contextmanager
        def read():
            yield SimpleNamespace(connection=connection)
        return SimpleNamespace(database=SimpleNamespace(read=read), registry=object())

    def plan(self, runtime):
        sources = {name: (kind, "/x", name) for (kind, name) in REFS}
        packages = {name: SimpleNamespace(ref=REFS[(kind, name)]) for (kind, name) in REFS}
        with patch("qorqut.catalog.sources.load_sources", return_value=sources), \
             patch("qorqut.catalog.packages.load_package", side_effect=lambda root, manifest, kind, registry: packages[manifest]):
            return adoption.plan(runtime, "/workspace", None)

    def test_reports_what_to_publish_and_which_profiles_change(self):
        held = [("plugin", "email-management", "3.2.0", REFS[("plugin", "email-management")]["digest"])]
        row = ("a1", "ordubashy", 1, "active", json.dumps(profile("ordubashy", "1.78.0", [grant("email-management", "3.2.0")])))
        result = self.plan(self.runtime(held, [row, ("a2", "gone", 1, "inactive", json.dumps(profile("ordubashy", "1.1.0")))]))
        actions = {package["id"]: package["action"] for package in result["packages"]}
        self.assertEqual(actions, {"agent-terminal-control": "publish", "email-management": "present", "global-worker": "publish", "ordubashy": "publish"})
        self.assertEqual([item["name"] for item in result["profiles"]], ["ordubashy"])
        self.assertEqual(result["conflicts"], [])

    def test_a_version_published_with_other_bytes_is_a_conflict_and_is_not_adopted(self):
        held = [("plugin", "agent-terminal-control", "1.2.0", "sha256:" + "0" * 64)]
        row = ("a1", "ordubashy", 1, "active", json.dumps(profile("ordubashy", "1.79.0", [grant("email-management", "3.2.0")])))
        result = self.plan(self.runtime(held, [row]))
        self.assertEqual([item["source"] for item in result["conflicts"]], ["agent-terminal-control"])
        self.assertNotIn("agent-terminal-control", [package["id"] for package in result["packages"]])
        self.assertEqual(result["profiles"], [])


if __name__ == "__main__":
    unittest.main()
