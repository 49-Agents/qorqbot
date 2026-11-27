"""The extension checker finds every way a folder breaks the extension contract (docs/EXTENSION-CONTRACT.md)."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

from qorqut import extension_contract as contract
from qorqut_agentic.files import split_path

EXAMPLE = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "network"
REPOSITORY = Path(__file__).resolve().parents[3]
PACKAGE_MAP = REPOSITORY / "qorqut-code" / "app" / "package-sources.json"
WORKFLOW = {"schema_version": 1, "id": "first-use", "description": "Set up the first Connections.",
            "steps": [{"id": "start", "instructions": "Ask the owner who matters most."},
                      {"id": "add", "instructions": "Add them.", "depends_on": ["start"]}]}


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.copy("network")

    def copy(self, name):
        target = self.root / name
        shutil.copytree(EXAMPLE, target)
        return target

    def manifest(self, mutate, folder=None):
        path = (folder or self.folder) / "extension.yaml"
        data = yaml.safe_load(path.read_text())
        mutate(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def write(self, relative, text, folder=None):
        path = (folder or self.folder) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def yaml_file(self, relative, mutate, folder=None):
        path = (folder or self.folder) / relative
        data = yaml.safe_load(path.read_text())
        mutate(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def add_code(self, folder=None):
        folder = folder or self.folder
        self.write("code/pyproject.toml", "[project]\nname = 'x'\n", folder)
        self.write("code/qorqut_network/extension.py", "", folder)
        self.manifest(lambda d: d.update(code={"factory": "qorqut_network.extension:create"}), folder)

    def findings(self):
        return contract.check_folder(self.folder)

    def assertFinding(self, *parts, found=None):
        found = self.findings() if found is None else found
        hits = [item for item in found if all(part in item for part in parts)]
        self.assertTrue(hits, f"no finding with {parts}; findings: {found}")
        return hits


class ExampleTest(Case):
    def test_example_passes(self):
        self.assertEqual(contract.check_folder(EXAMPLE), [])

    def test_a_relative_path_is_held_to_the_real_folder_name(self):
        with contextlib.chdir(self.folder):
            self.assertEqual(contract.check_folder(Path(".")), [])
            self.assertEqual(contract.load_manifest(Path(".")).id, "network")

    def test_load_manifest(self):
        manifest = contract.load_manifest(EXAMPLE)
        self.assertEqual((manifest.id, manifest.delivery, manifest.setup_kind), ("network", "downloadable", "none"))
        self.assertEqual(manifest.platforms, ("linux", "mac", "windows"))
        self.assertEqual(manifest.agents_allowed, ("ordubashy",))
        self.assertIsNone(manifest.factory)
        with self.assertRaises(Exception):
            manifest.id = "x"

    def test_load_manifest_raises_one_error_type(self):
        self.manifest(lambda d: d.update(delivery="free", contract=2))
        with self.assertRaises(contract.ContractError):
            contract.load_manifest(self.folder)
        (self.folder / "extension.yaml").write_text("a: [")
        with self.assertRaises(contract.ContractError):
            contract.load_manifest(self.folder)
        (self.folder / "extension.yaml").unlink()
        with self.assertRaises(contract.ContractError):
            contract.load_manifest(self.folder)

    def test_every_finding_is_reported_not_only_the_first(self):
        self.manifest(lambda d: d.update(delivery="free", contract=2, version="1.0"))
        self.assertEqual(len(self.findings()), 3)


class ManifestTest(Case):
    def test_rule_1_folder_name_equals_id(self):
        self.manifest(lambda d: d.update(id="other"))
        self.assertFinding("folder name", "other")

    def test_rule_1_id_form(self):
        self.manifest(lambda d: d.update(id="Net-work"))
        self.assertFinding("'Net-work'", "not lowercase letters")

    def test_unknown_fields_at_every_level(self):
        def add(data):
            data["colour"] = 1
            data["requires"]["x"] = 1
            data["setup"]["x"] = 1
            data["agents"]["x"] = 1
        self.manifest(add)
        for where in ("extension.yaml has", "`requires` has", "`setup` has", "`agents` has"):
            self.assertFinding(where, "unknown field")

    def test_unknown_field_in_code_and_setup_field(self):
        self.add_code()
        self.manifest(lambda d: (d["code"].update(x=1), d["setup"].update(kind="key", fields=[
            {"id": "k", "label": "Key", "x": 1}])))
        self.assertFinding("`code` has", "'x'")
        self.assertFinding("`setup.fields` entry 1 has", "'x'")

    def test_missing_required_field(self):
        self.manifest(lambda d: d.pop("summary"))
        self.assertFinding("missing the field 'summary'")

    def test_missing_sections(self):
        self.manifest(lambda d: (d.pop("requires"), d.pop("setup"), d.pop("agents")))
        for section in ("requires", "setup", "agents"):
            self.assertFinding(f"`{section}` is missing")

    def test_contract_is_exactly_1(self):
        for value in (2, "1", True):
            self.manifest(lambda d: d.update(contract=value))
            self.assertFinding("`contract` must be exactly 1")

    def test_name_and_summary_are_non_empty_without_a_limit(self):
        self.manifest(lambda d: d.update(name="", summary="  "))
        self.assertFinding("`name`")
        self.assertFinding("`summary`")
        self.manifest(lambda d: d.update(name="N" * 500, summary="Long. " * 500))
        self.assertEqual(self.findings(), [])

    def test_version_is_three_numbers(self):
        for value in ("1.0", "1.0.0.0", "a.b.c", 1):
            self.manifest(lambda d: d.update(version=value))
            self.assertFinding("`version`")

    def test_delivery(self):
        self.manifest(lambda d: d.update(delivery="free"))
        self.assertFinding("`delivery`")

    def test_platforms(self):
        for value in ([], ["linux", "linux"], ["freebsd"], "linux"):
            self.manifest(lambda d: d.update(platforms=value))
            self.assertFinding("`platforms`")

    def test_requires_qorqbot_is_a_non_empty_string(self):
        self.manifest(lambda d: d["requires"].update(qorqbot=""))
        self.assertFinding("`requires.qorqbot` must be a non-empty string")
        self.manifest(lambda d: d["requires"].pop("qorqbot"))
        self.assertFinding("`requires` is missing `qorqbot`")

    def test_requires_qorqbot_is_comparisons_against_a_version(self):
        for bad in ("newer please", "1.7.0", ">=1.7", ">=1.7.0,", "~=1.7.0"):
            self.manifest(lambda d: d["requires"].update(qorqbot=bad))
            self.assertFinding("`requires.qorqbot`", "must be comparisons against a version")
        for good in (">=1.7.0", ">= 1.7.0, <2.0.0", "==1.7.3", "!=1.8.0,>1.7.0"):
            self.manifest(lambda d: d["requires"].update(qorqbot=good))
            self.assertEqual(self.findings(), [], good)

    def test_requires_extensions(self):
        self.manifest(lambda d: d["requires"].update(extensions=["Bad Id"]))
        self.assertFinding("`requires.extensions` must be a list of extension ids")
        self.manifest(lambda d: d["requires"].update(extensions=["network"]))
        self.assertFinding("names the extension itself")
        self.manifest(lambda d: d["requires"].update(extensions=["whatsapp"]))
        self.assertEqual(self.findings(), [])

    def test_setup_kind(self):
        self.manifest(lambda d: d["setup"].update(kind="magic"))
        self.assertFinding("`setup.kind`")
        for kind in ("none", "sign-in", "pairing"):
            self.manifest(lambda d: d["setup"].update(kind=kind))
            self.assertEqual(self.findings(), [])

    def test_setup_fields_are_required_exactly_for_key(self):
        self.manifest(lambda d: d["setup"].update(kind="key"))
        self.assertFinding("`setup.fields` is required")
        self.manifest(lambda d: d["setup"].update(kind="pairing", fields=[{"id": "a", "label": "A"}]))
        self.assertFinding("`setup.fields` is allowed only")

    def test_setup_fields_content(self):
        good = [{"id": "token", "label": "Token", "secret": True}, {"id": "site", "label": "Site"}]
        self.manifest(lambda d: d["setup"].update(kind="key", fields=good))
        self.assertEqual(self.findings(), [])
        self.manifest(lambda d: d["setup"].update(fields=[]))
        self.assertFinding("non-empty list")
        self.manifest(lambda d: d["setup"].update(fields=[{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]))
        self.assertFinding("repeats the id 'a'")
        self.manifest(lambda d: d["setup"].update(fields=[{"id": "a", "label": "", "secret": "yes"}]))
        self.assertFinding("needs a non-empty `label`")
        self.assertFinding("`secret`")

    def test_agents_allowed(self):
        for value in ([], "ordubashy", ["Bad Role"], [3]):
            self.manifest(lambda d: d["agents"].update(allowed=value))
            self.assertFinding("`agents.allowed`")
        self.manifest(lambda d: d["agents"].pop("allowed"))
        self.assertFinding("`agents` is missing `allowed`")

    def test_agents_on_after_setup_is_inside_allowed(self):
        self.manifest(lambda d: d["agents"].update(on_after_setup=["ordubashy"]))
        self.assertEqual(self.findings(), [])
        self.manifest(lambda d: d["agents"].update(on_after_setup=["learning-applier"]))
        self.assertFinding("'learning-applier'", "not in `agents.allowed`")

    def test_role_names_are_the_role_ids_qorqbot_has(self):
        self.manifest(lambda d: d["agents"].update(allowed=["ordubashy", "learning-applier", "global-worker"]))
        self.assertEqual(self.findings(), [])
        for role in ("learning_applier", "coordinator", "Ordubashy"):
            self.manifest(lambda d: d["agents"].update(allowed=[role]))
            self.assertFinding("`agents.allowed` names", repr(role), "not a role Qorqbot has")

    def test_on_after_setup_names_role_ids_qorqbot_has(self):
        self.manifest(lambda d: d["agents"].update(allowed=["ordubashy"], on_after_setup=["owner"]))
        self.assertFinding("`agents.on_after_setup` names 'owner'", "not a role Qorqbot has")


class FolderTest(Case):
    def test_readme_exists(self):
        (self.folder / "README.md").unlink()
        self.assertFinding("README.md is missing")

    def test_plugin_or_code_part(self):
        shutil.rmtree(self.folder / "plugin")
        self.assertFinding("neither a `plugin/` directory nor a `code/` directory")

    def test_manifest_missing_and_unreadable(self):
        (self.folder / "extension.yaml").write_text("a: [")
        self.assertFinding("extension.yaml is not valid YAML")
        (self.folder / "extension.yaml").unlink()
        self.assertFinding("extension.yaml is missing")

    def test_code_part_passes_with_factory(self):
        self.add_code()
        self.assertEqual(self.findings(), [])
        manifest = contract.load_manifest(self.folder)
        self.assertEqual(manifest.factory, "qorqut_network.extension:create")

    def test_code_part_only_extension_passes(self):
        shutil.rmtree(self.folder / "plugin")
        self.add_code()
        self.assertEqual(self.findings(), [])

    def test_factory_is_required_with_a_code_directory(self):
        self.add_code()
        self.manifest(lambda d: d.pop("code"))
        self.assertFinding("needs `code.factory`")
        self.manifest(lambda d: d.update(code={}))
        self.assertFinding("`code` is missing `factory`")

    def test_factory_is_refused_without_a_code_directory(self):
        self.manifest(lambda d: d.update(code={"factory": "qorqut_network.extension:create"}))
        self.assertFinding("no `code/` directory")

    def test_factory_form_and_package(self):
        self.add_code()
        self.manifest(lambda d: d["code"].update(factory="qorqut_network.extension"))
        self.assertFinding("module.path:function")
        self.manifest(lambda d: d["code"].update(factory="other.extension:create"))
        self.assertFinding("package 'qorqut_network'")
        self.manifest(lambda d: d["code"].update(factory="qorqut_networks.extension:create"))
        self.assertFinding("package 'qorqut_network'")

    def test_code_files(self):
        self.add_code()
        (self.folder / "code" / "pyproject.toml").unlink()
        self.assertFinding("no pyproject.toml")
        (self.folder / "code" / "qorqut_network" / "extension.py").unlink()
        self.assertFinding("qorqut_network/extension.py")


class SkillTest(Case):
    skill = "plugin/skills/network/SKILL.md"

    def edit(self, old, new):
        path = self.folder / self.skill
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def test_extra_front_matter_line(self):
        self.edit("---\n\n# Network", "extra: 1\n---\n\n# Network")
        self.assertFinding("exactly the front matter lines")

    def test_missing_front_matter_line(self):
        self.edit("read_for: Keeping the owner's personal network of people, where you met and when to follow up\n", "")
        self.assertFinding("exactly the front matter lines")

    def test_no_front_matter(self):
        self.write(self.skill, "# Network\n")
        self.assertFinding("no usable front matter")

    def test_name(self):
        self.edit("name: qorqut-plugin-network", "name: network")
        self.assertFinding("must be named 'qorqut-plugin-network'")

    def test_skill_folder_starts_with_extension_id(self):
        shutil.move(self.folder / "plugin/skills/network", self.folder / "plugin/skills/people")
        self.write("plugin/skills/people/SKILL.md", (self.folder / "plugin/skills/people/SKILL.md").read_text()
                   .replace("qorqut-plugin-network", "qorqut-plugin-people"))
        self.assertFinding("'people'", "must be named 'network'")

    def test_skill_folder_that_only_shares_the_first_letters_is_refused(self):
        text = (self.folder / self.skill).read_text().replace("qorqut-plugin-network", "qorqut-plugin-networking")
        self.write("plugin/skills/networking/SKILL.md", text)
        self.assertFinding("'networking'", "must be named 'network'")

    def test_second_skill_with_the_id_as_prefix_passes(self):
        text = (self.folder / self.skill).read_text().replace("qorqut-plugin-network", "qorqut-plugin-network-groups")
        self.write("plugin/skills/network-groups/SKILL.md", text)
        self.assertEqual(self.findings(), [])

    def test_missing_skill_file(self):
        (self.folder / self.skill).unlink()
        self.assertFinding("no SKILL.md")

    def test_read_for_is_one_line_of_1_to_160_characters(self):
        self.edit("Keeping the owner's personal network of people, where you met and when to follow up", "x" * 161)
        self.assertFinding("read_for must be one line of 1 to 160")
        self.edit("x" * 161, "x" * 160)
        self.assertEqual(self.findings(), [])
        self.edit("x" * 160, "|\n  two\n  lines")
        self.assertFinding("read_for must be one line of 1 to 160")
        self.edit("|\n  two\n  lines", "''")
        self.assertFinding("read_for must be one line of 1 to 160")

    def test_description_within_the_managed_skill_bound(self):
        self.edit("description: How to keep the owner's personal network as Connection records.",
                  "description: " + "x" * 601)
        self.assertFinding("refused as a managed skill", "bounded description")

    def test_description_is_not_empty(self):
        self.edit("description: How to keep the owner's personal network as Connection records.", "description: ''")
        self.assertFinding("empty description")

    def test_skill_may_hold_files_of_the_managed_skill_types(self):
        self.write("plugin/skills/network/examples/people.csv", "name\nAda\n")
        self.write("plugin/skills/network/notes.md", "More notes.\n")
        self.assertEqual(self.findings(), [])

    def test_skill_file_of_another_type(self):
        self.write("plugin/skills/network/run.sh", "echo hi\n")
        self.assertFinding("skills/network/run.sh", "unsupported skill file")

    def test_executable_skill_file(self):
        (self.folder / self.skill).chmod(0o755)
        self.assertFinding("skills/network/SKILL.md is executable")

    def test_command_substitution_in_skill_text(self):
        self.write("plugin/skills/network/notes.txt", "Run !`date` first.\n")
        self.assertFinding("skills/network/notes.txt", "substitutions are not supported")

    def test_skill_file_over_the_managed_skill_size_bound(self):
        self.write("plugin/skills/network/big.md", "x" * 2_000_000)
        self.assertFinding("skills/network/big.md is refused", "size limit")

    def test_skill_name_of_a_package_that_ships_with_qorqbot(self):
        folder = self.copy("email")
        self.manifest(lambda d: d.update(id="email"), folder)
        text = (folder / self.skill).read_text().replace("qorqut-plugin-network", "qorqut-plugin-email-management")
        shutil.rmtree(folder / "plugin/skills/network")
        self.write("plugin/skills/email-management/SKILL.md", text, folder)
        # The names of this repository's own package map, as the service gives them from its installation's map.
        shipped = contract.skill_names_of(contract.load_sources(REPOSITORY, config_path=PACKAGE_MAP))
        self.assertFinding("'qorqut-plugin-email-management'", "ships with Qorqbot",
                           found=contract.check_folder(folder, shipped=shipped))
        self.assertEqual(contract.check_folder(folder, shipped=frozenset()), [])
        self.assertFinding("'qorqut-plugin-network'", "ships with Qorqbot",
                           found=contract.check_folders([self.folder], shipped=frozenset({"qorqut-plugin-network"}))[
                               self.folder.name])

    def test_no_source_map_beside_the_module_reads_nothing(self):
        # As an installed package the module has no package map beside it: the check must not fail on that.
        contract.shipped_skill_names.cache_clear()
        self.addCleanup(contract.shipped_skill_names.cache_clear)
        with mock.patch.object(contract, "APP", self.folder):
            self.assertEqual(contract.shipped_skill_names(), frozenset())
            self.assertEqual(contract.check_folder(self.folder), [])

    def test_skill_names_of_a_source_map(self):
        package = self.folder.parent / "package"
        package.mkdir()
        (package / "plugin.yaml").write_text("skills:\n  - id: letters\n")
        self.assertEqual(contract.skill_names_of({"one": ("plugin", package, "plugin.yaml")}),
                         frozenset({"qorqut-plugin-letters"}))
        with self.assertRaisesRegex(contract.DomainError, "Cannot read package manifest"):
            contract.skill_names_of({"one": ("plugin", package, "missing.yaml")})


class HooksTest(Case):
    def hooks(self, mutate):
        self.yaml_file("plugin/hooks.yaml", mutate)

    def test_hooks_file_is_optional(self):
        (self.folder / "plugin/hooks.yaml").unlink()
        self.assertEqual(self.findings(), [])

    def test_must_be_a_list(self):
        self.write("plugin/hooks.yaml", "id: x\n")
        self.assertFinding("hooks.yaml must be a list")

    def test_unknown_key(self):
        self.hooks(lambda d: d[0].update(colour="red"))
        self.assertFinding("unknown field 'colour'")

    def test_id_present_and_unique(self):
        self.hooks(lambda d: d[0].pop("id"))
        self.assertFinding("needs an `id`")
        self.yaml_file("plugin/hooks.yaml", lambda d: d.__setitem__(0, {**d[0], "id": d[1]["id"]}))
        self.assertFinding("repeats the id")

    def test_point(self):
        self.hooks(lambda d: d[0].update(at="sometimes"))
        self.assertFinding("`at` as start, owner_message")
        self.hooks(lambda d: d[0].pop("at"))
        self.assertFinding("`at` as start, owner_message")

    def test_every_point_is_accepted(self):
        for point in ("start", "owner_message", "stop"):
            self.hooks(lambda d: d[0].update(at=point))
            self.assertEqual(self.findings(), [])

    def test_text_is_required_and_exists(self):
        self.hooks(lambda d: d[0].pop("text"))
        self.assertFinding("needs `text`")
        self.hooks(lambda d: d[0].update(text="instructions/none.md"))
        self.assertFinding("'instructions/none.md'", "does not exist")

    def test_text_may_not_be_absolute_or_leave_plugin(self):
        self.write("outside.md", "x")
        self.hooks(lambda d: d[0].update(text=str(self.folder / "outside.md")))
        self.assertFinding("absolute")
        self.hooks(lambda d: d[0].update(text="../outside.md"))
        self.assertFinding("leaves the plugin directory")
        self.hooks(lambda d: d[0].update(text="instructions/../../outside.md"))
        self.assertFinding("leaves the plugin directory")

    def test_text_may_be_anywhere_inside_instructions_and_any_size(self):
        self.write("plugin/instructions/more/long.md", "word " * 20000)
        self.hooks(lambda d: d[0].update(text="instructions/more/long.md"))
        self.assertEqual(self.findings(), [])

    def test_text_must_be_in_instructions(self):
        for text in ("skills/network/SKILL.md", "hooks.yaml", "instructions/../records.yaml", "instructions", "."):
            self.hooks(lambda d: d[0].update(text=text))
            self.assertFinding("'note-people-mentioned'", f"{text!r}", "not in instructions/")

    def test_text_path_with_a_nul_is_a_finding(self):
        self.hooks(lambda d: d[0].update(text="instructions/a\0.md"))
        self.assertFinding("'note-people-mentioned'", "cannot be read")

    def test_every_that_cannot_be_a_length_of_time(self):
        for count in ("9" * 5000, "9" * 400):
            self.hooks(lambda d: d[3].update(every=count + "d"))
            self.assertFinding("'weekly-review'", "too long to be a length of time")

    def test_tool_names_follow_the_operation_name_rule(self):
        self.hooks(lambda d: d[1].update(tools=["crm_" + "x" * 100], if_used=["tasks_create"]))
        self.assertEqual(self.findings(), [])
        self.hooks(lambda d: d[1].update(tools=["crmEntries"]))
        self.assertFinding("`tools` as a non-empty list of tool names")

    def test_tools_required_for_after_tool_only(self):
        self.hooks(lambda d: d[1].pop("tools"))
        self.assertFinding("after_tool and needs `tools`")
        self.hooks(lambda d: d[0].update(tools=["crm_entries_update"]))
        self.assertFinding("`tools`, which is allowed only at after_tool")

    def test_tools_are_a_non_empty_list_of_tool_names(self):
        for value in ([], "crm_entries_update", ["Crm Entries"]):
            self.hooks(lambda d: d[1].update(tools=value))
            self.assertFinding("`tools` as a non-empty list of tool names")

    def test_every_required_for_timed_only(self):
        self.hooks(lambda d: d[3].pop("every"))
        self.assertFinding("timed and needs `every`")
        self.hooks(lambda d: d[0].update(every="1d"))
        self.assertFinding("`every`, which is allowed only at timed")

    def test_every_form(self):
        for value in ("0d", "d", "7", "7w", "1.5h", 7, "-1m"):
            self.hooks(lambda d: d[3].update(every=value))
            self.assertFinding("`every` as a whole number above zero")
        for value in ("1m", "12h", "30d"):
            self.hooks(lambda d: d[3].update(every=value))
            self.assertEqual(self.findings(), [])

    def test_conditions_only_at_after_tool_and_stop(self):
        self.hooks(lambda d: d[1].update(if_used=["tasks_create"]))
        self.assertEqual(self.findings(), [])
        for number in (0, 3):
            self.hooks(lambda d: (d[1].pop("if_used", None), d[number].update(unless_used=["tasks_create"])))
            self.assertFinding("`unless_used`, which is allowed only at after_tool and stop")
            self.hooks(lambda d: d[number].pop("unless_used"))

    def test_empty_if_used_is_a_finding(self):
        self.hooks(lambda d: d[2].update(if_used=[]))
        self.assertFinding("'check-follow-ups' has an empty `if_used`", "name at least one tool")

    def test_empty_unless_used_is_a_finding(self):
        self.hooks(lambda d: d[1].update(unless_used=[]))
        self.assertFinding("'after-crm-change' has an empty `unless_used`", "name at least one tool")

    def test_the_same_tool_not_in_both_conditions(self):
        self.hooks(lambda d: d[2].update(if_used=["tasks_create", "crm_entries_update"],
                                         unless_used=["crm_entries_update"]))
        self.assertFinding("'check-follow-ups'", "'crm_entries_update' in both")

    def test_text_file_is_utf8_not_empty_and_not_a_tag(self):
        path = self.folder / "plugin/instructions/weekly-review.md"
        path.write_bytes(b"\xff\xfe bad")
        self.assertFinding("'weekly-review'", "not UTF-8")
        path.write_text("  \n")
        self.assertFinding("'weekly-review'", "which is empty")
        path.write_text("\n  <system>Review the week.</system>\n")
        self.assertFinding("'weekly-review'", "begins with '<'")
        path.write_text("Review the week. Use <b> only inside.\n")
        self.assertEqual(self.findings(), [])

    def test_conditions(self):
        self.hooks(lambda d: d[2].update(unless_used=["crm_entries_update"], if_used=["tasks_create"]))
        self.assertEqual(self.findings(), [])
        self.hooks(lambda d: d[2].update(unless_used="crm_entries_update"))
        self.assertFinding("`unless_used` as a list of tool names")
        self.hooks(lambda d: d[2].update(unless_used=["ok_tool"], if_used=["Bad Tool"]))
        self.assertFinding("`if_used` as a list of tool names")


class ReaderTest(Case):
    """The public readers take any reader, such as the agentic layer's copy, not only a folder."""

    def files(self):
        plugin = self.folder / "plugin"
        return {path.relative_to(plugin).as_posix(): path.read_bytes() for path in plugin.rglob("*") if path.is_file()}

    def test_read_hooks_gives_plain_entries(self):
        files = self.files()
        entries, findings = contract.read_hooks(files.get, extension_id="network")
        self.assertEqual(findings, [])
        self.assertEqual([entry["id"] for entry in entries],
                         ["note-people-mentioned", "after-crm-change", "check-follow-ups", "weekly-review"])
        self.assertEqual(entries[1], {
            "extension_id": "network", "id": "after-crm-change", "at": "after_tool",
            "text": files["instructions/after-crm-change.md"].decode(), "tools": ["crm_entries_update"],
            "every_seconds": None, "if_used": [], "unless_used": []})
        self.assertEqual(entries[3]["every_seconds"], 7 * 86400)

    def test_read_hooks_leaves_out_an_entry_with_a_finding(self):
        files = self.files()
        files["hooks.yaml"] = files["hooks.yaml"].replace(b"every: 7d", b"every: 7w")
        files["instructions/check-follow-ups.md"] = b""
        entries, findings = contract.read_hooks(files.get, extension_id="network")
        self.assertEqual([entry["id"] for entry in entries], ["note-people-mentioned", "after-crm-change"])
        self.assertEqual(len(findings), 2)

    def test_read_hooks_leaves_out_a_repeated_id(self):
        files = self.files()
        files["hooks.yaml"] = files["hooks.yaml"].replace(b"id: check-follow-ups", b"id: weekly-review")
        entries, findings = contract.read_hooks(files.get, extension_id="network")
        self.assertEqual([entry["id"] for entry in entries], ["note-people-mentioned", "after-crm-change"])
        self.assertEqual(findings, ["hooks.yaml repeats the id 'weekly-review'."])

    def test_absent_files_give_nothing(self):
        self.assertEqual(contract.read_hooks({}.get, extension_id="network"), ([], []))
        self.assertEqual(contract.read_record_types({}.get, extension_id="network"), ([], []))

    def test_read_record_types_gives_plain_types(self):
        types, findings = contract.read_record_types(self.files().get, extension_id="network")
        self.assertEqual(findings, [])
        self.assertEqual(types, [{
            "extension_id": "network", "id": "connection", "based_on": "customer", "label": "Connection",
            "plural": "Connections", "fields": [
                {"id": "met_at", "label": "Where we met", "type": "string", "values": None},
                {"id": "closeness", "label": "Closeness", "type": "enum", "values": ["close", "friendly", "distant"]},
                {"id": "last_contact", "label": "Last contact", "type": "date", "values": None}]}])

    def test_read_record_types_leaves_out_a_type_with_a_finding(self):
        files = self.files()
        files["records.yaml"] += b"- {id: meeting, based_on: email, label: Meeting, plural: Meetings}\n"
        types, findings = contract.read_record_types(files.get, extension_id="network")
        self.assertEqual([item["id"] for item in types], ["connection"])
        self.assertEqual(len(findings), 1)


class HostileInputTest(Case):
    """The public readers run on a copy that can be edited: whatever the bytes and whatever ``read`` does, they return
    two lists and never raise."""

    def files(self):
        plugin = self.folder / "plugin"
        return {path.relative_to(plugin).as_posix(): path.read_bytes() for path in plugin.rglob("*") if path.is_file()}

    def test_a_raising_reader_gives_a_finding_for_the_file_or_entry(self):
        files = self.files()

        def layer(path):  # Like the agentic layer's reader: it refuses some paths by raising.
            split_path(path)
            if path == "instructions/weekly-review.md":
                raise RuntimeError("too large")
            return files.get(path)
        entries, findings = contract.read_hooks(layer, extension_id="network")
        self.assertEqual(len(entries), 3)
        self.assertEqual(findings, ["hooks.yaml entry 'weekly-review' names the text file "
                                    "'instructions/weekly-review.md', which cannot be read: RuntimeError."])

        def broken(path):
            raise PermissionError(path)
        for reader in (contract.read_hooks, contract.read_record_types):
            self.assertEqual(reader(broken, extension_id="network")[0], [])
            self.assertFinding("cannot be read: PermissionError", found=reader(broken, extension_id="network")[1])

    def test_deeply_nested_yaml_is_a_finding(self):
        deep = b"[" * 5000 + b"]" * 5000
        for name, reader in (("hooks.yaml", contract.read_hooks), ("records.yaml", contract.read_record_types)):
            self.assertEqual(reader({name: deep}.get, extension_id="network"),
                             ([], [f"{name} cannot be read as a list: it is nested too deeply."]))
        (self.folder / "plugin/hooks.yaml").write_bytes(deep)
        self.assertFinding("hooks.yaml cannot be read as a list")

    def test_a_linked_plugin_folder_is_not_read(self):
        elsewhere = self.root / "elsewhere"
        shutil.move(self.folder / "plugin", elsewhere)
        (elsewhere / "hooks.yaml").write_text("not: a list\n")
        os.symlink(elsewhere, self.folder / "plugin")
        self.assertEqual(self.findings(), ["plugin is a symbolic link, which an extension folder may not hold."])

    def test_random_and_hostile_inputs_never_raise(self):
        rng = random.Random(7)
        atoms = ["null", "1", "[]", "{}", "'x'", "[a]", "{a: 1}", "start", "timed", "after_tool", "stop", "7d", "'0d'",
                 "'%sd'" % ("9" * 5000), "instructions/a.md", "'.'", "'instructions/a\\0.md'", "'C:/x'", "[crm_x]",
                 "true", "2024-01-01", "1.5", "enum", "customer", "[[a]]", "[{a: 1}]", "'" + "[" * 3000 + "'"]
        hook_keys = ["id", "at", "text", "tools", "every", "if_used", "unless_used", "x"]
        record_keys = ["id", "based_on", "label", "plural", "fields"]
        texts = [b"hello", b"", b"\xff\xfe", b"<tag>", b"  \n"]
        for _ in range(400):
            entry = lambda keys: "{" + ", ".join(f"{key}: {rng.choice(atoms)}" for key in rng.sample(keys, rng.randint(0, len(keys)))) + "}"
            files = {
                "hooks.yaml": rng.choice([("[" + ", ".join(entry(hook_keys) for _ in range(3)) + "]").encode(),
                                          b"[" * 4000, b"\xff", b"5", b"", b"a: 1"]),
                "records.yaml": rng.choice([("[" + entry(record_keys) + "]").encode(), b"]" * 10, b"\x00"]),
                "instructions/a.md": rng.choice(texts)}

            def read(path):
                choice = rng.random()
                if choice < 0.15:
                    raise rng.choice([OSError, ValueError, RuntimeError, KeyError, RecursionError])(path)
                if choice < 0.25:
                    split_path(path)
                return files.get(path)
            for reader in (contract.read_hooks, contract.read_record_types):
                result = reader(read, extension_id="network")
                self.assertIsInstance(result, tuple)
                self.assertEqual(len(result), 2)
                self.assertTrue(all(isinstance(part, list) for part in result))


class RecordsTest(Case):
    def records(self, mutate):
        self.yaml_file("plugin/records.yaml", mutate)

    def test_records_file_is_optional(self):
        (self.folder / "plugin/records.yaml").unlink()
        self.assertEqual(self.findings(), [])

    def test_must_be_a_list(self):
        self.write("plugin/records.yaml", "id: x\n")
        self.assertFinding("records.yaml must be a list")

    def test_unknown_keys(self):
        self.records(lambda d: (d[0].update(colour="red"), d[0]["fields"][0].update(shape="round")))
        self.assertFinding("unknown field 'colour'")
        self.assertFinding("unknown field 'shape'")

    def test_id_present_form_and_unique(self):
        self.records(lambda d: d[0].pop("id"))
        self.assertFinding("needs an `id`")
        self.records(lambda d: d[0].update(id="Bad-Id"))
        self.assertFinding("`id` that is not lowercase")
        self.records(lambda d: (d[0].update(id="connection"), d.append(dict(d[0]))))
        self.assertFinding("repeats the record type id 'connection'")

    def test_based_on(self):
        for value in ("email", "table", None):
            self.records(lambda d: d[0].update(based_on=value))
            self.assertFinding("based_on one of task, story, customer")
        for value in ("task", "story", "customer"):
            self.records(lambda d: d[0].update(based_on=value))
            self.assertEqual(self.findings(), [])

    def test_label_and_plural(self):
        self.records(lambda d: d[0].update(label="", plural=3))
        self.assertFinding("non-empty `label`")
        self.assertFinding("non-empty `plural`")

    def test_fields_are_optional(self):
        self.records(lambda d: d[0].pop("fields"))
        self.assertEqual(self.findings(), [])

    def test_field_parts(self):
        self.records(lambda d: d[0]["fields"][0].update(label=""))
        self.assertFinding("field 1 needs a non-empty `label`")
        self.records(lambda d: d[0]["fields"][1].pop("id"))
        self.assertFinding("field 2 needs a non-empty `id`")

    def test_field_type(self):
        self.records(lambda d: d[0]["fields"][0].update(type="float"))
        self.assertFinding("field 1 must have `type` as one of string, int, bool, date, enum")
        for kind in ("string", "int", "bool", "date"):
            self.records(lambda d: d[0]["fields"][0].update(type=kind))
            self.assertEqual(self.findings(), [])

    def test_values_exactly_for_enum(self):
        self.records(lambda d: d[0]["fields"][1].pop("values"))
        self.assertFinding("field 2 is an enum and needs `values`")
        self.records(lambda d: d[0]["fields"][1].update(values=[]))
        self.assertFinding("field 2 is an enum and needs `values`")
        self.records(lambda d: d[0]["fields"][1].update(values=[1, 2]))
        self.assertFinding("field 2 is an enum and needs `values`")
        self.records(lambda d: d[0]["fields"][0].update(values=["a"]))
        self.assertFinding("field 1 has `values`")

    def test_field_id_follows_the_custom_field_name_rule(self):
        for bad in ("Met At", "title", "x" * 33):
            self.yaml_file("plugin/records.yaml", lambda d: d[0]["fields"][0].update(id=bad))
            self.assertFinding("does not accept as a custom field name")
        self.yaml_file("plugin/records.yaml", lambda d: d[0]["fields"][0].update(id="met-at_2"))
        self.assertEqual(self.findings(), [])

    def test_field_ids_are_unique_within_the_type(self):
        self.records(lambda d: d[0]["fields"][1].update(id="met_at"))
        self.assertFinding("repeats the field id 'met_at'")


class WorkflowTest(Case):
    def test_valid_workflow_passes(self):
        self.write("plugin/workflows/first-use.yaml", yaml.safe_dump(WORKFLOW))
        self.assertEqual(self.findings(), [])

    def test_not_yaml(self):
        self.write("plugin/workflows/first-use.yaml", "a: [")
        self.assertFinding("first-use.yaml", "not valid YAML")

    def test_schema_violation(self):
        self.write("plugin/workflows/first-use.yaml", yaml.safe_dump({**WORKFLOW, "colour": 1}))
        self.assertFinding("first-use.yaml", "not a valid workflow", "colour")

    def test_semantic_violation(self):
        broken = {**WORKFLOW, "steps": [{"id": "a", "instructions": "x", "depends_on": ["nowhere"]}]}
        self.write("plugin/workflows/first-use.yaml", yaml.safe_dump(broken))
        self.assertFinding("first-use.yaml", "unknown step 'nowhere'")

    def test_file_must_be_yaml(self):
        self.write("plugin/workflows/first-use.md", "text")
        self.assertFinding("first-use.md", "must end in .yaml")

    def test_id_equals_the_file_name(self):
        self.write("plugin/workflows/first-use.yaml", yaml.safe_dump({**WORKFLOW, "id": "start-here"}))
        self.assertFinding("first-use.yaml has the id 'start-here'", "'first-use'")

    def step(self, **fields):
        return {**WORKFLOW, "steps": [{"id": "start", **fields}]}

    def test_steps_may_refer_to_the_extensions_own_skills_and_workflows(self):
        self.write("plugin/workflows/first-use.yaml", yaml.safe_dump(self.step(instruction_ref="network/network")))
        self.write("plugin/workflows/again.yaml",
                   yaml.safe_dump({**self.step(instruction_ref="network/network"), "id": "again"}))
        self.assertEqual(self.findings(), [])

    def test_references_outside_the_generated_package(self):
        for ref in ("network/nowhere", "other/network", "network/weekly-review"):
            self.write("plugin/workflows/first-use.yaml", yaml.safe_dump(self.step(instruction_ref=ref)))
            self.assertFinding("step 'start'", f"`instruction_ref` {ref!r}")
        self.write("plugin/workflows/first-use.yaml", yaml.safe_dump(self.step(instructions="x", verifier="crm/check")))
        self.assertFinding("step 'start'", "`verifier` 'crm/check'")


class PackageTest(Case):
    def test_example_loads_in_the_agentic_layer(self):
        self.write("plugin/workflows/first-use.yaml", yaml.safe_dump(WORKFLOW))
        self.assertEqual(contract._package_findings(self.folder), [])
        self.assertEqual(self.findings(), [])

    def test_what_only_the_layer_refuses_is_reported(self):
        # The layer takes a verifier only from a tool entry, and a generated package has none.
        broken = {**WORKFLOW, "steps": [{"id": "start", "instructions": "x", "verifier": "network/network"}]}
        self.write("plugin/workflows/first-use.yaml", yaml.safe_dump(broken))
        self.assertFinding("does not load in the agentic layer", "verifier must be a tool entry")

    def test_plugin_part_may_not_hold_its_own_package_manifest(self):
        self.write("plugin/agentic.yaml", "id: network\n")
        self.assertFinding("holds its own agentic.yaml")

    def test_layer_is_not_asked_while_other_rules_fail(self):
        self.write("plugin/agentic.yaml", "id: network\n")
        (self.folder / "README.md").unlink()
        self.assertEqual(self.findings(), ["README.md is missing at the top of the folder."])


class LinkTest(Case):
    def test_no_symbolic_link_anywhere(self):
        os.symlink(self.folder / "plugin/instructions/weekly-review.md", self.folder / "plugin/instructions/again.md")
        os.symlink(self.root, self.folder / "plugin/skills/network/outside")
        os.symlink(self.folder / "README.md", self.folder / "NOTES.md")
        found = self.findings()
        for name in ("plugin/instructions/again.md", "plugin/skills/network/outside", "NOTES.md"):
            self.assertFinding(f"{name} is a symbolic link", found=found)


class CrossFolderTest(Case):
    def test_clean_folders_have_no_findings(self):
        other = self.copy("other")
        self.manifest(lambda d: d.update(id="other"), other)
        self.yaml_file("plugin/records.yaml", lambda d: d[0].update(id="other_connection"), other)
        self.write("plugin/skills/other/SKILL.md", (other / "plugin/skills/network/SKILL.md").read_text()
                   .replace("network", "other"), other)
        shutil.rmtree(other / "plugin/skills/network")
        self.assertEqual(contract.check_folders([self.folder, other]), {"network": [], "other": []})

    def test_same_extension_id(self):
        other = self.root / "copy" / "network"
        shutil.copytree(EXAMPLE, other)
        result = contract.check_folders([self.folder, other])
        self.assertEqual(len(result), 2)
        for findings in result.values():
            self.assertFinding("extension id 'network' is also used", found=findings)

    def test_record_type_id_used_by_two_extensions(self):
        other = self.copy("other")
        self.manifest(lambda d: d.update(id="other"), other)
        self.write("plugin/skills/other/SKILL.md", (other / "plugin/skills/network/SKILL.md").read_text()
                   .replace("network", "other"), other)
        shutil.rmtree(other / "plugin/skills/network")
        result = contract.check_folders([self.folder, other])
        self.assertFinding("record type id 'connection' is also used by the extension 'other'", found=result["network"])
        self.assertFinding("record type id 'connection' is also used by the extension 'network'", found=result["other"])

    def test_findings_of_each_folder_are_kept(self):
        other = self.copy("other")
        result = contract.check_folders([self.folder, other])
        self.assertFinding("folder name 'other' does not equal", found=result["other"])
        self.assertEqual([item for item in result["network"] if "folder name" in item], [])


class CommandLineTest(Case):
    def run_main(self, *folders):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = contract.main([str(folder) for folder in folders])
        return status, out.getvalue().splitlines()

    def test_passing_folder_exits_0_and_names_unchecked_rules(self):
        status, lines = self.run_main(EXAMPLE)
        self.assertEqual(status, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn("Not checked yet: rules 5, 8, 9, 10, 11, 12, 13 of section 12", lines[0])
        self.assertIn("checked: 1, 2, 3, 4, 6, 7", lines[0])
        self.assertIn("that the plugin part loads in the agentic layer", lines[0])

    def test_findings_are_printed_with_the_folder_name_and_exit_1(self):
        (self.folder / "README.md").unlink()
        self.manifest(lambda d: d.update(delivery="free"))
        status, lines = self.run_main(self.folder)
        self.assertEqual(status, 1)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.startswith("network: ") for line in lines[:2]))
        self.assertIn("Not checked yet", lines[-1])

    def test_no_folder_is_an_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(contract.main([]), 2)

    def test_with_load_the_code_part_is_loaded_in_a_process_of_its_own(self):
        # A copy: the example's own folder may hold compiled files from another test that loaded it in this process.
        tablenote = self.root / "copied" / "tablenote"
        shutil.copytree(EXAMPLE.parent / "tablenote", tablenote, ignore=shutil.ignore_patterns("__pycache__"))
        status, lines = self.run_main("--load", EXAMPLE, tablenote)
        # In an installed app the line on the names comes first (the next test); here only the summary is looked at.
        lines = [line for line in lines if line != contract.NAMES_NOT_CHECKED]
        self.assertEqual((status, len(lines)), (0, 1), lines)
        self.assertIn("with --load also 5 (the tool names), 8 and 10", lines[0])
        self.assertIn("Not checked yet: rules 5 (that a skill's read_for covers each tool), 9, 11, 12, 13", lines[0])
        # Nothing of its code was imported here.
        self.assertEqual([name for name in sys.modules if name.startswith("qorqut_tablenote")], [])
        self.assertNotIn(str(tablenote / "code"), sys.path)
        # Nor was anything written into the folder it checked.
        self.assertEqual(list(tablenote.rglob("__pycache__")), [])
        code = self.root / "tablenote"
        shutil.copytree(tablenote, code)
        source = code / "code" / "qorqut_tablenote" / "extension.py"
        original = source.read_text()
        for changed, said in (
                (original.replace('return TableNote()', 'raise ZeroDivisionError()'),
                 "Its code could not be loaded: ZeroDivisionError."),
                (original + '\nSTATEMENTS = ("CREATE TABLE notes (id TEXT PRIMARY KEY)",)\n',
                 "Its table notes is not named tablenote_… (The tables of tablenote are refused: statement "),
                (original + '\nTableNote.tables = lambda self: (_ for _ in ()).throw(KeyError())\n',
                 "Its code could not be loaded: KeyError.")):
            with self.subTest(said):
                source.write_text(changed)
                status, lines = self.run_main("--load", code)
                self.assertEqual(status, 1)
                self.assertTrue(lines[0].startswith("tablenote: " + said), lines)
        # Without --load the code is not loaded.
        self.assertEqual(self.run_main(code)[0], 0)

    def test_with_load_an_id_that_a_name_qorqbot_can_put_into_a_database_starts_with_is_refused(self):
        from test_extension_tables_runtime import rename
        folder = rename(self.root / "variants", "agent", "agent_x")
        # The name rules of the checkout these tests are in, also where the app under test is an installed copy.
        spec = importlib.util.spec_from_file_location(
            "contract_test_names", REPOSITORY / "qorqut-code" / "tools" / "distribution" / "extension_names.py")
        names = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(names)
        with mock.patch.object(contract, "_names_tool", lambda: names):
            status, lines = self.run_main("--load", folder)
        self.assertEqual(status, 1)
        self.assertTrue(lines[0].startswith("agent: The new extension id agent cannot be used, as the name agent_"), lines)
        self.assertEqual(self.run_main(folder)[0], 0)
        self.assertNotIn(contract.NAMES_NOT_CHECKED, lines)
        # Where the app has no checkout, --load says the id was not checked against the names.
        with mock.patch.object(contract, "_names_tool", lambda: None):
            status, lines = self.run_main("--load", folder)
        self.assertEqual((status, lines[0]), (0, contract.NAMES_NOT_CHECKED))

    def test_runs_as_a_module(self):
        done = subprocess.run([sys.executable, "-B", "-m", "qorqut.extension_contract", str(EXAMPLE)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Not checked yet", done.stdout)


if __name__ == "__main__":
    unittest.main()
