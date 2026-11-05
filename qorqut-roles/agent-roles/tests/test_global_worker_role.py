"""Guard the repeatable worker role and Ordubashy's management of it."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
RETIRED_SKILLS = ("content-creation", "marketing-plan", "business-testing", "seo-autopilot", "business-founding",
                  "business-products", "strategy-exploration", "customer-approach")
# Administration, owner-channel, Context-write, publication and send operations a new worker never receives by default.
WORKER_FORBIDDEN_PREFIXES = ("agents_create", "agents_update", "agents_deactivate", "permissions_set", "plugins_",
                             "roles_", "owner_", "signoffs_", "focus_", "telegram_", "delegated_assignment",
                             "email_", "business_email_", "browser_", "shared_instructions_", "notes_", "products_", "ledger_", "metrics_",
                             "tables_", "tasks_create", "tasks_assign", "routines_", "agent_terminal_")
CONTEXT_WRITES = ("context_upload", "context_instruct", "context_edit")


def manifest(role):
    return yaml.safe_load((ROOT / "roles" / role / "role.yaml").read_text())


def text(role):
    return " ".join((ROOT / manifest(role)["brief"]).read_text().split())


class GlobalWorkerRoleTest(unittest.TestCase):
    def test_manifest_is_a_repeatable_general_role_with_only_the_browser_procedure(self):
        role = manifest("global-worker")
        self.assertEqual((role["id"], role["version"]), ("global-worker", "1.6.0"))
        self.assertEqual((role["scope_kinds"], role["scope_brief"]), (["ecosystem"], "ecosystem-agent"))
        self.assertNotIn("external_connections", role)
        self.assertEqual([skill["id"] for skill in role["skills"]], ["browser-control"])
        selected = {skill["id"]: skill["path"] for skill in role["skills"]}
        # The managed copy serves Ordubashy and the workers; Learning Applier keeps the original file.
        self.assertEqual(selected["browser-control"], "skills/managed-browser-control/SKILL.md")
        # The procedure is reachable, but browser use is an ordinary grant Ordubashy gives: no default browser operation.
        self.assertFalse([name for name in role["default_tool_policy"] if name.startswith("browser_")])
        self.assertEqual([name for name in role["default_tool_policy"] if "delegated_assignment" in name], [])

    def test_only_the_three_general_roles_and_no_specialist_skill_remain(self):
        self.assertEqual(sorted(path.parent.name for path in (ROOT / "roles").glob("*/role.yaml")),
                         ["global-worker", "learning-applier", "ordubashy"])
        directories = sorted(path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(len(directories), len(set(directories)))
        self.assertFalse(any("worker" in name for name in directories))
        for name in RETIRED_SKILLS:
            self.assertFalse((ROOT / "skills" / name).exists(), name)
        for role in ("global-worker", "ordubashy", "learning-applier"):
            self.assertFalse({skill["id"] for skill in manifest(role).get("skills", [])} & set(RETIRED_SKILLS), role)

    def test_brief_states_the_agreed_worker_behavior_briefly(self):
        brief = text("global-worker")
        self.assertLessEqual(len(brief), 2500)
        for phrase in ("one atomic assignment or topic at a time", "chooses the next one",
                       "Keep your shared status and Task accurate", "`agent_work_update`",
                       "at most 100 words", "keep private material out",
                       "Ask Ordubashy", "you cannot grant yourself access",
                       "loading a skill grants nothing", "Verify your result", "record the outcome on the Task",
                       "tell Ordubashy it is complete", "The owner may message you directly",
                       "Honor their instructions and tell Ordubashy how they change your work",
                       "Shared Context is readable by the team; changing it needs a grant",
                       "You cannot create a project: ask Ordubashy, who creates one only for the owner's request or agreement"):
            self.assertIn(phrase, brief)
        for phrase in ("confidence", "citation", "second review", "Global Marketing", "marketing", "Telegram"):
            self.assertNotIn(phrase, brief)

    def test_default_policy_is_own_work_and_reads_only(self):
        policy = manifest("global-worker")["default_tool_policy"]
        self.assertEqual(set(policy.values()), {"allow"})
        for name in policy:
            with self.subTest(operation=name):
                self.assertFalse(name.startswith(WORKER_FORBIDDEN_PREFIXES), name)
        for name in CONTEXT_WRITES:
            self.assertNotIn(name, policy)
        for name in ("context_get", "context_list", "context_search", "tasks_get", "tasks_update", "tasks_complete"):
            self.assertEqual(policy[name], "allow")

    def test_ordubashy_manages_workers_directly_and_briefly_owns_the_team(self):
        role = manifest("ordubashy")
        self.assertEqual(role["version"], "1.72.0")
        policy = role["default_tool_policy"]
        for name in ("agents_create", "agents_update", "agents_deactivate", "permissions_set",
                     "plugins_enable", "plugins_disable"):
            self.assertEqual(policy[name], "allow", name)
        for name in ("roles_publish", "plugins_publish"):
            self.assertEqual(policy[name], "signoff", name)
        self.assertEqual((role.get("external_connections"), policy["agents_icons_list"]), (None, "allow"))
        self.assertFalse({skill["id"] for skill in role["skills"]} & set(RETIRED_SKILLS))
        brief = text("ordubashy")
        self.assertLessEqual(len((ROOT / role["brief"]).read_text().strip()), 10000)
        for phrase in ("Ordubashy owns owner dialogue, plans, escalations and delivery. You represent the owner.", "agents_activity_list",
                       "Reuse suitable workers across kinds of work", "one atomic active assignment at a time", "access, tools, subscriptions and start/stop/restart",
                       "Answer a worker only when the owner's actual inputs and intent make you highly confident, otherwise ask the owner",
                       "Owner-only approvals stay the owner's", "trust its verified completion absent concrete reason to investigate",
                       "reach you in full as notices, not assignments",
                       "Choose each new worker's display name and agents_icons_list icon",
                       "Assign a task the owner authorized through a worker; a tentative need is no authorization",
                       "bind workers too", "Follow browser-control/exclusive turns",
                       'Native Codex helpers need the owner\'s explicit "native Codex subagent" request',
                       "use a legacy delegated assignment (operations-delegation) only when selected explicitly",
                       "assign authorized browser work to a worker you granted browser access; that needs no second owner request"):
            self.assertIn(phrase, " ".join(brief.split()))
        # One team definition: ordinary delegation is not the old delegated-assignment rule, and no stale Marketing text.
        for phrase in ("independent peer", "current Marketing assignee", "marketing to Global Marketing", "Global Marketing", "Founder",
                       "Delegate/worker/helper/subagent", "means a durable Qorqut assignment", "your delegated worker",
                       "Delegated workers cannot", "not delegated workers", "use a durable worker",
                       "Browser work stays direct unless the owner delegates"):
            self.assertNotIn(phrase, " ".join(brief.split()))
        self.assertEqual(" ".join(brief.split()).count("Reuse suitable workers across kinds of work"), 1)

    def test_browser_skill_routes_authorized_work_to_workers_without_granting_browser_use(self):
        guidance = " ".join((ROOT / "skills/managed-browser-control/SKILL.md").read_text().split())
        for phrase in ("Learning Applier has no durable delegation",
                       "Ordubashy can assign already authorized browser work to a permanent worker through the team",
                       "that needs no second owner request",
                       "once Ordubashy has granted it that operation",
                       "This skill grants no browser use and creates no personal browser binding",
                       "only an explicitly selected legacy delegated assignment (operations-delegation) uses assignment-bound dispatch",
                       "The workers of an explicitly selected legacy delegated assignment use the assignment-bound `browser_turn`",
                       "an exclusive lease, one operator at a time"):
            self.assertIn(phrase, guidance)
        for phrase in ("Ordubashy or Global Marketing delegates", '"Delegate", "worker", "helper" or "subagent" means',
                       "A global peer's delegated workers", "only when the owner explicitly asks to delegate"):
            self.assertNotIn(phrase, guidance)

    def test_active_shared_skills_name_no_specialist_agent_and_keep_the_worker_routing(self):
        def flat(name):
            return " ".join((ROOT / "skills" / name / "SKILL.md").read_text().split())

        for path in sorted((ROOT / "skills").glob("*/SKILL.md")):
            with self.subTest(skill=path.parent.name):
                for phrase in ("Global Marketing", "Founder", "independent peer", "Marketing agent"):
                    self.assertNotIn(phrase, flat(path.parent.name))
        expected = {
            "business-operations": ("A worker has no automatic plugin",
                                    "Ordubashy manages worker plugins (email-management)",
                                    "with expected_revision and an idempotency_key",
                                    "approval of a plugin change never approves message content"),
            "owner-focus": ("A permanent worker never decides an owner Signoff",
                            "an ordinary worker's questions go through Ordubashy",
                            "each eligible permanent worker has its own, bound to its exact agent UUID",
                            "never share history, delivery, counters or read position",
                            "the full notice reaches Ordubashy through his own separate private inbox, never through the worker's conversation"),
            "ordubashy-browser-control": ("Learning Applier and the worker executing assigned browser work do it directly by default",
                                          "For Learning Applier and a worker, task complexity and generic delegation guidance do not override direct execution",
                                          "Ordubashy can assign already authorized browser work to a permanent worker"),
            "managed-browser-control": ("Learning Applier and the worker executing assigned browser work do it directly by default",
                                        "For Learning Applier and a worker, task complexity and generic delegation guidance do not override direct execution",
                                        "Ordubashy can assign already authorized browser work to a permanent worker"),
        }
        for name, phrases in expected.items():
            for phrase in phrases:
                with self.subTest(skill=name, phrase=phrase[:40]):
                    self.assertIn(phrase, flat(name))
        for name in ("ordubashy-browser-control", "managed-browser-control"):
            self.assertNotIn("Every global agent executes its browser work directly", flat(name))

    def test_only_the_manager_gains_the_management_grants(self):
        for path in sorted((ROOT / "roles").glob("*/role.yaml")):
            role = yaml.safe_load(path.read_text())
            if role["id"] == "ordubashy":
                continue
            with self.subTest(role=role["id"]):
                for name in ("agents_create", "agents_update", "agents_deactivate", "permissions_set",
                             "plugins_enable", "plugins_disable"):
                    self.assertNotEqual(role["default_tool_policy"].get(name), "allow", name)


if __name__ == "__main__":
    unittest.main()
