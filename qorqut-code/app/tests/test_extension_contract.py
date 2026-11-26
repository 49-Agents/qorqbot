"""The extension checker finds every way a folder breaks the extension contract (docs/EXTENSION-CONTRACT.md)."""
import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from qorqut import extension_contract as contract

EXAMPLE = Path(__file__).resolve().parent / "fixtures" / "extension-contract" / "network"
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

    def test_role_names_may_use_hyphens_as_extensions_does(self):
        self.manifest(lambda d: d["agents"].update(allowed=["learning-applier"]))
        self.assertEqual(self.findings(), [])


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
        self.assertFinding("read_for as one line of 1 to 160")
        self.edit("x" * 161, "x" * 160)
        self.assertEqual(self.findings(), [])
        self.edit("x" * 160, "|\n  two\n  lines")
        self.assertFinding("read_for as one line of 1 to 160")
        self.edit("|\n  two\n  lines", "''")
        self.assertFinding("read_for as one line of 1 to 160")

    def test_description_is_not_empty(self):
        self.edit("description: How to keep the owner's personal network as Connection records.", "description: ''")
        self.assertFinding("empty description")


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

    def test_text_may_be_anywhere_inside_plugin_and_any_size(self):
        self.write("plugin/other/long.md", "word " * 20000)
        self.hooks(lambda d: d[0].update(text="other/long.md"))
        self.assertEqual(self.findings(), [])

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

    def test_conditions(self):
        self.hooks(lambda d: d[2].update(unless_used=["crm_entries_update"], if_used=["tasks_create"]))
        self.assertEqual(self.findings(), [])
        self.hooks(lambda d: d[2].update(unless_used="crm_entries_update"))
        self.assertFinding("`unless_used` as a list of tool names")
        self.hooks(lambda d: d[2].update(unless_used=["ok_tool"], if_used=["Bad Tool"]))
        self.assertFinding("`if_used` as a list of tool names")


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

    def test_runs_as_a_module(self):
        done = subprocess.run([sys.executable, "-B", "-m", "qorqut.extension_contract", str(EXAMPLE)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Not checked yet", done.stdout)


if __name__ == "__main__":
    unittest.main()
