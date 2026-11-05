"""Contract for the Projects transition in the role sources: source-only, no host needed.

The organization concept is a Project; Ordubashy creates one directly with the existing ``business_create``; the setup
checklist and its operations are gone; the founder and marketing roles and the founding plugin are not in this source;
versions move for every package whose selected bytes changed.
"""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_ROLES = ("ordubashy", "global-worker", "learning-applier")
RETIRED_ROLES = ("founder", "global-marketing")
RETIRED_SETUP_OPERATIONS = ("business_setup_get", "business_setup_update", "business_setup_submit")
RETIRED = re.compile(r"business_setup|business-setup|manage_business_setup|founding_document_id|setup_origin|"
                     r"operations/businesses|setup_prerequisite|cloudflare_access|domain_purchase|"
                     r"(?i:setup checklist|setup parent|setup summary)")
VERSIONS = {"ordubashy": "1.72.0", "global-worker": "1.6.0", "learning-applier": "1.16.0"}


def role(name):
    return yaml.safe_load((ROOT / "roles" / name / "role.yaml").read_text())


def flat(path):
    return " ".join((ROOT / path).read_text().split())


def frontmatter(path):
    return yaml.safe_load(re.match(r"\A---\n(.*?)\n---\n", (ROOT / path).read_text(), re.S)[1])


def active_files():
    """Every authored file an active role publishes (manifests, briefs, skill directories)."""
    files = set()
    for name in ACTIVE_ROLES:
        manifest = role(name)
        files |= {f"roles/{name}/role.yaml", manifest["brief"]}
        for skill in manifest["skills"]:
            files |= {str(path.relative_to(ROOT)) for path in (ROOT / skill["path"]).parent.rglob("*") if path.is_file()}
    return files


class VersionsTest(unittest.TestCase):
    def test_changed_packages_carry_their_new_versions(self):
        for name, version in VERSIONS.items():
            self.assertEqual(role(name)["version"], version, name)

    def test_the_source_document_and_readme_list_every_changed_version(self):
        document = flat("docs/V1.3-ROLES.md")
        for phrase in ("`roles/ordubashy/` | 1.65.0 | 1.68.0", "`roles/global-worker/` | 1.4.0 | 1.5.0",
                       "`roles/learning-applier/` | 1.12.0 | 1.13.0",
                       "It publishes, adopts, prepares and reloads nothing"):
            self.assertIn(phrase, document)
        readme = flat("README.md")
        for phrase in ("| `roles/ordubashy/` | 1.72.0 |", "| `roles/global-worker/` | 1.6.0 |",
                       "| `roles/learning-applier/` | 1.16.0 |", "docs/V1.3-ROLES.md"):
            self.assertIn(phrase, readme)


class MinimalCreationTest(unittest.TestCase):
    def test_creation_uses_the_existing_tool_and_grant_only(self):
        policy = role("ordubashy")["default_tool_policy"]
        self.assertEqual(policy["business_create"], "allow")
        for operation in RETIRED_SETUP_OPERATIONS:
            self.assertNotIn(operation, policy)
        # No role other than Ordubashy can create: not the worker or Learning Applier.
        for name in ACTIVE_ROLES:
            if name != "ordubashy":
                self.assertNotIn("business_create", role(name)["default_tool_policy"], name)
        # Capabilities live in adoption, never in a manifest: nothing here names or adds one.
        for name in ACTIVE_ROLES:
            text = (ROOT / "roles" / name / "role.yaml").read_text()
            self.assertNotIn("manage_business_setup", text)
            self.assertNotIn("capabilit", text)

    def test_ordubashy_selects_project_creation_directly_and_only_he_does(self):
        for name in ACTIVE_ROLES:
            selected = {skill["id"]: skill["path"] for skill in role(name)["skills"]}
            with self.subTest(role=name):
                if name == "ordubashy":
                    self.assertEqual(selected["project-creation"], "skills/project-creation/SKILL.md")
                    self.assertEqual(selected["service-provisioning"], "skills/service-provisioning/SKILL.md")
                    self.assertNotIn("business-setup", selected)
                else:
                    self.assertFalse({"project-creation", "service-provisioning", "business-setup"} & set(selected))
        self.assertFalse((ROOT / "skills/business-setup").exists())
        self.assertNotIn("plugins", role("ordubashy"))        # creation needs no plugin enablement

    def test_the_creation_procedure_is_concise_and_complete(self):
        skill = "skills/project-creation/SKILL.md"
        meta = frontmatter(skill)
        self.assertEqual((set(meta), meta["name"]), ({"name", "description"}, "bazarlyk-role-project-creation"))
        self.assertLessEqual(len(meta["description"]), 600)
        self.assertLess(len((ROOT / skill).read_text()), 6000)
        body = flat(skill)
        for phrase in (
                # authority
                "Only you, the configured Ordubashy, create projects",
                "The owner has no creation tool: they ask you, and you create the project for them",
                "You hold `coordinate_businesses` and `business_create` is `allow`",
                "the host also checks your configured identity on every step",
                "No worker, other human, the installation owner included, retired role or system program can create one",
                "A worker that needs a project asks you (`agents_message_send`)",
                # the call
                "Call `business_create` in Global (select no project)", "`name` (required)", "1 to 200 characters",
                "`summary` (optional): shown as the Description", "At most 2,000 characters", "`slug` (optional)",
                "`projects/<slug>`", "at most 64 characters", "It is fixed at creation", "a stable `idempotency_key`",
                "Nothing else is accepted",
                # retry and conflicts
                "call again with the same key and identical arguments: it returns the same project",
                "`revision_conflict` with `slug` and `suggested_slug`", "Never reuse or take over it",
                # result and what exists
                "`business_id`, `slug` and `business`", "`workspace_path`, `hq_path`",
                "an empty project folder holding empty `hq/` and `code/` folders",
                "no agent (projects have no resident agents; never create, request or revive one)",
                "no checklist, Task or question", "it calls no provider",
                # afterwards
                "Create ordinary Tasks", "upload assets, create documents and tables, and create routines",
                "starts only from an explicit request under its own procedure"):
            self.assertIn(phrase, body)
        # No retired creation input, setup record or ceremony is carried.
        for retired in ("owner_confirmation", "known_decisions", "source_refs", "founding_document_id", "business_setup",
                        "manage_business_setup", "setup_id"):
            self.assertNotIn(retired, body)
        self.assertNotIn("Founder", body)

    def test_minimal_creation_needs_no_plugin_and_no_retired_role_exists(self):
        self.assertFalse((ROOT / "plugins").exists())
        for name in RETIRED_ROLES:
            self.assertFalse((ROOT / "roles" / name).exists(), name)

    def test_workers_ask_ordubashy_and_gain_creation_from_nothing(self):
        brief = " ".join((ROOT / "roles/global-worker/BRIEF.md").read_text().split())
        self.assertIn("You cannot create a project: ask Ordubashy, who creates one only for the owner's request or agreement", brief)
        self.assertLessEqual(len(brief), 2500)
        brief = flat("roles/ordubashy/BRIEF.md")
        self.assertIn("Only you create a project (project-creation; no provisioning); the owner and workers ask you.", brief)
        self.assertIn("Projects have no resident agents", brief)
        for name in ("global-worker", "learning-applier"):
            policy = role(name)["default_tool_policy"]
            self.assertFalse([key for key in policy if key.startswith(("business_create", "business_setup_"))], name)


    def test_only_the_configured_ordubashy_creates_and_the_owner_and_workers_ask_him(self):
        stale = re.compile(r"(?i)only (?:the )?(?:installation )?owner and (?:you|ordubashy)|only the owner and you create|"
                           r"creator besides the owner|exact owner or|stays with the installation owner")
        files = [path for path in sorted(active_files()) if path.endswith((".md", ".yaml"))]
        for path in files + ["README.md"]:
            text = (ROOT / path).read_text()
            with self.subTest(path=path):
                self.assertIsNone(stale.search(" ".join(text.split())))
        creation = flat("skills/project-creation/SKILL.md")
        self.assertIn("Only you, the configured Ordubashy, create projects", creation)
        self.assertIn("The owner has no creation tool: they ask you, and you create the project for them", creation)
        self.assertIn("Only you create a project (project-creation; no provisioning); the owner and workers ask you.",
                      flat("roles/ordubashy/BRIEF.md"))
        self.assertIn("You cannot create a project: ask Ordubashy", flat("roles/global-worker/BRIEF.md"))
        self.assertIn("Only Ordubashy creates a project, through project-creation; the owner and other roles ask Ordubashy",
                      flat("skills/business-operations/SKILL.md"))
        # The grant and the coordination capability are unchanged: the change is in who is told to call, not in what is granted.
        self.assertEqual(role("ordubashy")["default_tool_policy"]["business_create"], "allow")
        self.assertIn("project-creation", {skill["id"] for skill in role("ordubashy")["skills"]})
        self.assertNotIn("project-creation", {skill["id"] for name in ("global-worker", "learning-applier") for skill in role(name)["skills"]})


class SetupRetirementTest(unittest.TestCase):
    def test_no_active_file_carries_the_setup_flow_or_the_old_business_path(self):
        files = active_files()
        self.assertGreater(len(files), 30)
        for path in sorted(files):
            if path.endswith((".md", ".yaml", ".json")):
                with self.subTest(path=path):
                    self.assertIsNone(RETIRED.search((ROOT / path).read_text()))

    def test_setup_only_selections_instructions_and_references_are_gone(self):
        for path in ("skills/business-setup", "plugins/founding"):
            self.assertFalse((ROOT / path).exists(), path)
        for name in (*ACTIVE_ROLES,):
            self.assertNotIn("business-setup", {skill["id"] for skill in role(name)["skills"]})
        provisioning = ROOT / "skills/service-provisioning"
        self.assertEqual(sorted(path.name for path in provisioning.rglob("*") if path.is_file()),
                         ["SKILL.md", "cloudflare.md", "openrouter.md", "railway.md", "resend.md"])
        body = flat("skills/service-provisioning/SKILL.md")
        self.assertIn("Creating a project provisions none of it and there is no checklist that implies it", body)
        self.assertIn("There are no owner-question forms or readiness protocol here", body)

    def test_the_retired_roles_and_their_specialist_skills_are_gone(self):
        for name in RETIRED_ROLES:
            self.assertFalse((ROOT / "roles" / name).exists(), name)
        for skill in ("business-founding", "business-core", "business-planning", "business-measurement", "business-products",
                      "strategy-exploration", "seo-autopilot", "stripe-management", "stripe-read", "daily-report",
                      "personal-context", "pull-strategy", "content-creation"):
            self.assertFalse((ROOT / "skills" / skill).exists(), skill)

    def test_every_skill_directory_is_selected_and_named_for_its_id(self):
        selected = {}
        for name in ACTIVE_ROLES:
            for skill in role(name)["skills"]:
                selected[skill["path"]] = skill["id"]
                self.assertEqual(frontmatter(skill["path"])["name"], f"bazarlyk-role-{skill['id']}", skill["path"])
        self.assertEqual({str(path.relative_to(ROOT)) for path in (ROOT / "skills").glob("*/SKILL.md")}, set(selected))

    def test_briefs_stay_within_their_limit(self):
        for name in ACTIVE_ROLES:
            self.assertLessEqual(len((ROOT / role(name)["brief"]).read_text().strip()), 10000, name)


class ExplicitServiceInputsTest(unittest.TestCase):
    """Manager decision 2026-10-17: Cloudflare takes explicit inputs; no setup evidence or setup_prerequisite."""

    def test_both_cloudflare_operations_take_the_required_exact_zone_domain(self):
        cloudflare = flat("skills/service-provisioning/references/cloudflare.md")
        for phrase in ("Input: a required `domain`, the exact Cloudflare zone name the owner named for this work",
                       "canonical lower-case DNS hostname such as `example.com`: no scheme, port, path, wildcard or underscore label",
                       "A missing or malformed `domain` is a `validation_error`",
                       "A recorded purchase, an email-name answer or any other setup evidence is not required, and none is written",
                       "A `zone_id` and `account_id` you pass are assertions that must match that discovery; they never replace it",
                       "It returns verified nonsecret evidence",
                       # the DNS operation
                       "Input: the required `domain` (the same exact zone name), `purpose`",
                       "takes no zone or account ID", "it verifies that the token is active and queries the saved zone with it",
                       "that zone's exact name must equal `domain` and its account must equal the saved account",
                       "Every record must sit at `domain` or at a true subdomain of it", "A zone is never inferred from record text",
                       "`configuration_required`: the saved token, zone or account key is missing",
                       "`zone_mismatch`: the saved zone or account is not this `domain`'s",
                       "`verification_failed`: the saved token is not an active exact-zone token",
                       "A provider error keeps its provider code",
                       # existing recovery and reconciliation behavior is kept
                       "Identical existing records are reused and missing ones are created",
                       "Existing records are never modified or deleted", "`recovery_required` has two meanings"):
            self.assertIn(phrase, cloudflare)
        provisioning = flat("skills/service-provisioning/SKILL.md")
        self.assertIn("each with the required exact zone `domain` the owner named", provisioning)
        self.assertIn("a project's creation never calls them", provisioning)
        self.assertIn("the same `domain`, purpose `resend`", flat("skills/service-provisioning/references/resend.md"))


class ProjectVocabularyTest(unittest.TestCase):
    def test_the_container_concept_is_a_project_and_the_identifiers_stay(self):
        brief = flat("roles/ordubashy/BRIEF.md")
        self.assertIn("work directly with each project's files, resources, accounts and integrations", brief)
        self.assertNotIn("Businesses have no resident agents", brief)
        creation = flat("skills/project-creation/SKILL.md")
        for identifier in ("business_create", "business_id", "coordinate_businesses"):
            self.assertIn(identifier, creation)
        self.assertIn("`projects/<slug>`", creation)
        for path in ("skills/business-operations/SKILL.md", "skills/ecosystem-review/SKILL.md"):
            self.assertIn("Projects have no resident agents" if "ecosystem" in path else "Project channel capabilities", flat(path))

    def test_genuine_commercial_business_language_survives(self):
        self.assertNotIn("business-planning", flat("roles/ordubashy/BRIEF.md"))


if __name__ == "__main__":
    unittest.main()
