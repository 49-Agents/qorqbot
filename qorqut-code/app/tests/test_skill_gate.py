"""A Qorqut tool that a listed, unread skill names is refused until that skill is read in this conversation."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from qorqut_agentic.behavior import instructions as policy
from qorqut_agentic.behavior.instructions import from_catalog, skill_front_matter
from qorqut.bootstrap import activity_hook as hook
from qorqut.bootstrap import claude_session, qorqbot_guide

from test_preparation_modes import Fixture as PreparationFixture

ROOT = Path(__file__).resolve().parents[3]
DEFAULTS = ROOT / "qorqut-agentic/src/qorqut_agentic/resources/defaults"
INSTRUCTIONS = DEFAULTS / "operations/runtime-instructions"
SKILLS = DEFAULTS / "operations/imported-role-skills/native/skills"
HOST = DEFAULTS / "operations/source-instructions/host/app"
ROLE = HOST / "qorqut/agents/skills/role/SKILL.md"
BROWSER = SKILLS / "managed-browser-control/SKILL.md"
REMINDERS = SKILLS / "reminders/SKILL.md"
PERSONAL = SKILLS / "personal-record/SKILL.md"
# The real get-set-up skill names no tool; this stand-in does, to prove it is left out where the start loads it.
GET_SET_UP = ("---\nname: qorqut-role-get-set-up\ndescription: The first conversation.\n"
              "read_for: The first conversation with a new owner (`settings_set`)\n---\n\nBody.\n")
OWNER_INPUT = SKILLS / "owner-input/SKILL.md"
SESSION = "00000000-0000-4000-8000-000000000001"
Q = hook.SKILL_GATE_PREFIX


def catalog():
    return from_catalog(lambda path: (INSTRUCTIONS / path).read_text())


def skill(name, read_for=None, description="Does a thing."):
    lines = ["---", "name: " + name, "description: " + description]
    if read_for is not None:
        lines.append("read_for: " + read_for)
    return "\n".join([*lines, "---", "", "Body of " + name + "."]) + "\n"


class Workspace:
    """A prepared Claude Code agent folder: registration, the prepared instruction helper and snapshot, and skills."""

    def __init__(self, case, *, intake=False, snapshot=True, helper=True):
        temporary = tempfile.TemporaryDirectory()
        case.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        qorqut = self.root / ".qorqut"
        qorqut.mkdir()
        self.registration = qorqut / "registration.json"
        self.registration.write_text(json.dumps({"registration_id": "reg", "agent_id": "a", "workspace": str(self.root),
                                                 "operational_intake_enabled": intake}))
        if helper:
            shutil.copyfile(policy.__file__, qorqut / "agentic-instructions.py")
        if snapshot:
            (qorqut / "runtime-instructions.json").write_bytes(catalog().bytes())

    def install(self, *sources):
        for source in sources:
            text = source.read_text() if isinstance(source, Path) else source
            folder = self.root / ".claude/skills" / skill_front_matter(text)["name"]
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text(text)
        return self

    def record(self):
        path = hook.skill_gate_record_path(self.root, SESSION)
        return json.loads(path.read_text()) if path.exists() else {}


def call(tool, session=SESSION):
    return {"hook_event_name": "PreToolUse", "session_id": session, "tool_name": tool, "tool_input": {}}


def read(name, session=SESSION):
    return {"hook_event_name": "PostToolUse", "session_id": session, "tool_name": "Skill",
            "tool_input": {"skill": name}, "tool_response": {"success": True}}


def reason(output):
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse" and specific["permissionDecision"] == "deny"
    return specific["permissionDecisionReason"]


class MapTests(unittest.TestCase):
    def test_tokens_prefix_and_exact_several_in_one_line(self):
        self.assertEqual(policy.skill_gate_tools("Personal (`personal_get`, `personal_set`) and `reminders_*`"),
                         ("personal_get", "personal_set", "reminders_*"))
        self.assertTrue(policy.skill_gate_covers("reminders_*", "reminders_create"))
        self.assertFalse(policy.skill_gate_covers("reminders_*", "reminder"))
        self.assertTrue(policy.skill_gate_covers("personal_get", "personal_get"))
        self.assertFalse(policy.skill_gate_covers("personal_get", "personal_get_all"))
        self.assertEqual(policy.skill_gate_tools("A plain phrase, no tool; a `kebab-name` is not a tool"), ())
        self.assertEqual(policy.skill_gate_tools(None), ())

    def test_map_comes_from_the_installed_skills_the_section_lists(self):
        space = Workspace(self).install(BROWSER, REMINDERS, PERSONAL, ROLE,
                                        skill("qorqut-plain", "Plain work, no tool named"),
                                        skill("qorqut-described", None, "Read for `browser_turn` in the description."))
        found = policy.skill_gate_map(space.root, "claude-code")
        self.assertEqual(sorted(found), sorted([
            ("browser_turn", "qorqut-role-browser-control"), ("reminders_*", "qorqut-role-reminders"),
            ("personal_get", "qorqut-role-personal-record"), ("personal_set", "qorqut-role-personal-record")]))
        listed = {name for name, _ in policy.discovery_entries(space.root, "claude-code")}
        self.assertTrue({name for _, name in found} <= listed)
        self.assertNotIn("role", listed)

    def test_loaded_skills_are_left_out(self):
        space = Workspace(self).install(BROWSER, OWNER_INPUT, GET_SET_UP)
        everything = {name for _, name in policy.skill_gate_map(space.root, "claude-code")}
        self.assertIn("qorqut-role-owner-input", everything)
        self.assertIn("qorqut-role-get-set-up", everything)
        loaded = (*policy.ALWAYS_SKILLS, *policy.FIRST_CONVERSATION_SKILLS)
        ordubashy = {name for _, name in policy.skill_gate_map(space.root, "claude-code", loaded)}
        self.assertEqual(ordubashy, {"qorqut-role-browser-control"})

    def test_the_session_uses_the_same_first_conversation_constant(self):
        self.assertIs(claude_session.FIRST_CONVERSATION_SKILLS, policy.FIRST_CONVERSATION_SKILLS)


class GateTests(unittest.TestCase):
    def test_unread_refuses_and_names_the_skill_then_a_read_lets_it_through(self):
        space = Workspace(self).install(BROWSER)
        text = reason(hook.skill_gate(call(Q + "browser_turn"), space.registration, now=100))
        self.assertEqual(text, "Not run. `browser_turn` is covered by the skill qorqut-role-browser-control, which you "
                               "have not read in this conversation. Read it in full now with the Skill tool, by that "
                               "name, then make this call again.")
        hook.skill_gate_read(read("qorqut-role-browser-control"), space.registration)
        self.assertIsNone(hook.skill_gate(call(Q + "browser_turn"), space.registration, now=101))
        self.assertEqual(space.record()["read"], ["qorqut-role-browser-control"])

    def test_a_prefixed_skill_name_counts_as_read(self):
        space = Workspace(self).install(REMINDERS)
        hook.skill_gate_read(read("project:qorqut-role-reminders"), space.registration)
        self.assertIsNone(hook.skill_gate(call(Q + "reminders_create"), space.registration, now=1))

    def test_a_failed_skill_call_or_another_tool_records_nothing(self):
        space = Workspace(self).install(BROWSER)
        hook.skill_gate_read({**read("qorqut-role-browser-control"), "tool_response": {"success": False}}, space.registration)
        hook.skill_gate_read({**read("qorqut-role-browser-control"), "tool_name": "Read"}, space.registration)
        self.assertEqual(space.record(), {})

    def test_other_tools_and_uncovered_qorqut_tools_pass_without_a_record(self):
        space = Workspace(self).install(BROWSER, REMINDERS)
        for tool in ("Bash", "Read", "Skill", "mcp__claude_ai_Gmail__send_email", "mcp__other__browser_turn",
                     Q + "tasks_list", Q, "browser_turn"):
            with self.subTest(tool=tool):
                self.assertIsNone(hook.skill_gate(call(tool), space.registration, now=1))
        self.assertEqual(space.record(), {})

    def test_another_conversation_starts_unread(self):
        space = Workspace(self).install(BROWSER)
        hook.skill_gate_read(read("qorqut-role-browser-control"), space.registration)
        other = "00000000-0000-4000-8000-000000000002"
        self.assertIsNotNone(hook.skill_gate(call(Q + "browser_turn", other), space.registration, now=1))

    def test_several_unread_skills_are_all_named(self):
        space = Workspace(self).install(BROWSER, skill("qorqut-role-extra", "More browser work (`browser_*`)"))
        text = reason(hook.skill_gate(call(Q + "browser_turn"), space.registration, now=1))
        self.assertIn("the skills qorqut-role-browser-control and qorqut-role-extra,", text)
        self.assertIn("Read each of them in full now", text)
        hook.skill_gate_read(read("qorqut-role-extra"), space.registration)
        text = reason(hook.skill_gate(call(Q + "browser_turn"), space.registration, now=30))
        self.assertIn("the skill qorqut-role-browser-control,", text)
        self.assertNotIn("qorqut-role-extra", text)

    def test_ordubashy_loaded_skills_are_never_gated(self):
        space = Workspace(self, intake=True).install(OWNER_INPUT, GET_SET_UP, BROWSER)
        self.assertIsNone(hook.skill_gate(call(Q + "signoffs_input_create"), space.registration, now=1))
        self.assertIsNone(hook.skill_gate(call(Q + "settings_set"), space.registration, now=1))
        self.assertIsNotNone(hook.skill_gate(call(Q + "browser_turn"), space.registration, now=1))
        worker = Workspace(self).install(OWNER_INPUT)
        self.assertIsNotNone(hook.skill_gate(call(Q + "signoffs_input_create"), worker.registration, now=1))

    def test_a_batch_is_checked_by_the_calls_inside_it(self):
        space = Workspace(self).install(BROWSER, REMINDERS)

        def batch(*operations):
            entries = [{"id": str(index), "operation": operation, "arguments": {}}
                       for index, operation in enumerate(operations)]
            return {**call(Q + "operations_batch"), "tool_input": {"operations": entries}}

        self.assertIsNone(hook.skill_gate(batch("tasks_list", "inbox_list"), space.registration, now=1))
        self.assertEqual(space.record(), {})
        text = reason(hook.skill_gate(batch("tasks_list", "reminders_create", "browser_turn"), space.registration, now=1))
        self.assertIn("`reminders_create` is covered by the skill qorqut-role-reminders,", text)
        hook.skill_gate_read(read("qorqut-role-reminders"), space.registration)
        text = reason(hook.skill_gate(batch("tasks_list", "reminders_create", "browser_turn"), space.registration, now=2))
        self.assertIn("`browser_turn` is covered by the skill qorqut-role-browser-control,", text)
        hook.skill_gate_read(read("qorqut-role-browser-control"), space.registration)
        self.assertIsNone(hook.skill_gate(batch("tasks_list", "reminders_create", "browser_turn"), space.registration, now=3))
        for malformed in ({}, {"operations": "browser_turn"}, {"operations": [None, {"operation": 5}]}):
            with self.subTest(malformed=malformed):
                self.assertIsNone(hook.skill_gate({**call(Q + "operations_batch"), "tool_input": malformed},
                                                  space.registration, now=4))

    def test_a_call_whose_skill_is_read_writes_nothing(self):
        space = Workspace(self).install(BROWSER)
        hook.skill_gate_read(read("qorqut-role-browser-control"), space.registration)
        with mock.patch.object(hook, "skill_gate_save") as save:
            self.assertIsNone(hook.skill_gate(call(Q + "browser_turn"), space.registration, now=1))
        save.assert_not_called()

    def test_a_snapshot_without_the_message_uses_the_built_in_wording(self):
        space = Workspace(self, snapshot=False).install(BROWSER)
        self.assertIn("covered by the skill qorqut-role-browser-control",
                      reason(hook.skill_gate(call(Q + "browser_turn"), space.registration, now=1)))

    def test_the_built_in_wording_is_the_message_asset(self):
        for key, built_in in (("skill-gate-refusal", policy.SKILL_GATE_REFUSAL),
                              ("skill-gate-refusal-several", policy.SKILL_GATE_REFUSAL_SEVERAL)):
            with self.subTest(key=key):
                self.assertEqual((INSTRUCTIONS / "messages" / (key + ".md")).read_text(), built_in)
                self.assertIn(key, catalog().document["messages"])


class RoundTests(unittest.TestCase):
    def test_a_parallel_batch_is_one_round_and_three_rounds_then_the_calls_go_through(self):
        space = Workspace(self).install(BROWSER)
        gate = lambda now: hook.skill_gate(call(Q + "browser_turn"), space.registration, now=now)
        for now in (100, 100.2, 100.5, 109):  # Round 1, one batch.
            self.assertIsNotNone(gate(now))
        self.assertEqual(space.record()["refusals"]["qorqut-role-browser-control"]["rounds"], 1)
        self.assertIsNotNone(gate(111))  # Round 2.
        self.assertIsNotNone(gate(130))  # Round 3.
        self.assertIsNotNone(gate(135))  # Still round 3.
        self.assertEqual(space.record()["refusals"]["qorqut-role-browser-control"]["rounds"], 3)
        self.assertIsNone(gate(141))  # The valve opens and stays open, counted.
        self.assertIsNone(gate(142))
        entry = space.record()["refusals"]["qorqut-role-browser-control"]
        self.assertEqual((entry["released_at"], entry["passed"], entry["rounds"]), (141, 2, 3))

    def test_fast_repeated_retries_cannot_hold_a_round_open_forever(self):
        space = Workspace(self).install(BROWSER)
        results = [hook.skill_gate(call(Q + "browser_turn"), space.registration, now=100 + step * 5)
                   for step in range(12)]
        self.assertIsNone(results[-1])


class FailOpenTests(unittest.TestCase):
    def test_every_fault_lets_the_call_continue(self):
        space = Workspace(self).install(BROWSER)
        missing = space.root / ".qorqut/missing.json"
        broken = Workspace(self).install(BROWSER)
        broken.registration.write_text("{not json")
        no_helper = Workspace(self, helper=False).install(BROWSER)
        old_helper = Workspace(self).install(BROWSER)
        (old_helper.root / ".qorqut/agentic-instructions.py").write_text("ALWAYS_SKILLS = ()\n")
        bad_record = Workspace(self).install(BROWSER)
        path = hook.skill_gate_record_path(bad_record.root, SESSION)
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2")
        cases = {"missing registration": (call(Q + "browser_turn"), missing),
                 "unreadable registration": (call(Q + "browser_turn"), broken.registration),
                 "no prepared helper": (call(Q + "browser_turn"), no_helper.registration),
                 "helper from before the gate": (call(Q + "browser_turn"), old_helper.registration),
                 "unreadable record": (call(Q + "browser_turn"), bad_record.registration),
                 "no session": ({**call(Q + "browser_turn"), "session_id": None}, space.registration),
                 "no tool name": ({"hook_event_name": "PreToolUse", "session_id": SESSION}, space.registration)}
        for label, (event, registration) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.run_main(event, registration), "")
        for raw in ("", "{", "[]", "null", json.dumps({"hook_event_name": "PostToolUse", "session_id": SESSION,
                                                       "tool_name": "Skill", "tool_input": "x"})):
            with self.subTest(raw=raw):
                self.assertEqual(self.run_main(raw, space.registration), "")

    def run_main(self, event, registration):
        raw = event if isinstance(event, str) else json.dumps(event)
        with mock.patch.object(sys, "stdin", mock.Mock(read=lambda *_: raw)), \
                mock.patch.object(sys, "stdout", new_callable=__import__("io").StringIO) as out:
            hook.skill_gate_main(registration)
        return out.getvalue()

    def test_a_skill_folder_change_is_seen_at_the_next_call(self):
        space = Workspace(self).install(BROWSER)
        self.assertIsNone(hook.skill_gate(call(Q + "reminders_create"), space.registration, now=1))
        space.install(REMINDERS)  # As a bootstrap skill refresh would.
        self.assertIsNotNone(hook.skill_gate(call(Q + "reminders_create"), space.registration, now=2))


class StandaloneTests(unittest.TestCase):
    def test_the_prepared_hook_file_runs_isolated_with_the_standard_library_only(self):
        space = Workspace(self).install(BROWSER)
        script = space.root / ".qorqut/activity-hook.py"
        shutil.copyfile(hook.__file__, script)

        def run(event):
            done = subprocess.run([sys.executable, "-I", "-S", str(script), "--registration", str(space.registration),
                                   "--runtime", "claude-code", "--skill-gate"], input=json.dumps(event),
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual((done.returncode, done.stderr), (0, ""))
            return done.stdout
        first = json.loads(run(call(Q + "browser_turn")))
        self.assertIn("qorqut-role-browser-control", reason(first))
        self.assertEqual(run(read("qorqut-role-browser-control")), "")
        self.assertEqual(run(call(Q + "browser_turn")), "")


class RegistrationTests(PreparationFixture):
    def test_claude_code_gets_the_gate_and_codex_does_not(self):
        projection = self.build(False)
        local = json.loads(projection.files[".claude/settings.local.json"])["hooks"]
        pre = [entry for entry in local["PreToolUse"] if entry.get("matcher") == "mcp__qorqut__.*"]
        post = [entry for entry in local["PostToolUse"] if entry.get("matcher") == "Skill"]
        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)
        for entry in (*pre, *post):
            (command,) = entry["hooks"]
            self.assertTrue(command["command"].endswith(" --runtime claude-code --skill-gate"))
            self.assertLessEqual(command["timeout"], 10)
        self.assertEqual(len([e for e in local["PreToolUse"] if e.get("matcher") == "mcp__claude_ai_.*"]), 1)
        self.assertEqual(len([e for e in local["PreToolUse"] if "matcher" not in e]), 1)  # The activity hook, unchanged.
        codex = projection.files[".codex/hooks.json"].decode() + projection.files[".qorqut/codex-hook"].decode()
        self.assertNotIn("skill-gate", codex)
        self.assertNotIn("mcp__qorqut__", codex)


class DefaultSkillTests(unittest.TestCase):
    """Every backticked tool in a default skill's `read_for` names a real Qorqut tool, or the gate covers nothing."""

    def known_tools(self):
        from qorqut.agents import terminal_control
        from qorqut.composition import policy as composition
        from qorqut.composition.catalog import builtin_registry
        names = {entry.name for entry in builtin_registry().entries()}
        # Tools installed later by their own contributions, not declared in the built-in catalog.
        names |= set(terminal_control.TOOLS) | composition.ORDUBASHY_SETTINGS
        names |= composition.EXPERIENCE_WORK | composition.EXPERIENCE_READS
        return names

    def test_every_token_matches_a_real_tool(self):
        known = self.known_tools()
        texts = {path.relative_to(DEFAULTS).as_posix(): path.read_text()
                 for path in DEFAULTS.rglob("SKILL.md") if "instance-template" not in path.parts}
        texts["qorqbot_guide"] = qorqbot_guide.SKILL
        checked = 0
        for where, text in texts.items():
            for token in policy.skill_gate_tools(skill_front_matter(text).get("read_for")):
                checked += 1
                with self.subTest(skill=where, token=token):
                    self.assertTrue(any(policy.skill_gate_covers(token, name) for name in known))
        self.assertGreaterEqual(checked, 18)

    def test_every_backticked_token_is_read_as_a_tool(self):
        """No backticked text in a default `read_for` is silently dropped by the token pattern."""
        for path in DEFAULTS.rglob("SKILL.md"):
            phrase = skill_front_matter(path.read_text()).get("read_for", "")
            with self.subTest(skill=path.relative_to(DEFAULTS).as_posix()):
                self.assertEqual(len(policy.skill_gate_tools(phrase)), len(set(re.findall(r"`([^`]*)`", phrase))))


if __name__ == "__main__":
    unittest.main()
