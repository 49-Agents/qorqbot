"""D59 factual form classification and owner-fenced, read-only observation."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from pydantic import ValidationError

from runtime.support import RuntimeCase
from runtime.test_input_dispatch import InputFixture, assert_external
from qorqut_core.adapters.attention import classify_attention
from qorqut_core.adapters.input_layout import SGR
from qorqut_core.runner.models import TerminalSnapshot
from qorqut_core.runner.shell import ShellManager
from qorqut_core.runtime.input_dispatch import InputUnavailable
from qorqut_core.runtime.models import NativeAttentionView


def capture(name="mcp_approval"):
    data = json.loads((Path(__file__).parents[1] / "adapters/fixtures/native-attention.json").read_text())
    return TerminalSnapshot.model_validate(data[name])


FIXTURES = Path(__file__).parents[1] / "adapters/fixtures"
ARROW = "\x1b[38;5;153m❯\x1b[39m "


def claude(name="ask-user-question-single", fixture="claude-attention.json", **changes):
    data = next(c for c in json.loads((FIXTURES / fixture).read_text())["captures"] if c["name"] == name)
    x, y, visible = data["cursor"]
    return TerminalSnapshot(columns=data["columns"], rows=data["rows"], cursor_x=x, cursor_y=y,
        cursor_visible=visible, in_mode=False, input_off=False, synchronized=False,
        screen=data["screen"]).model_copy(update=changes)


def rewrite(snapshot, edits=None, **changes):
    """Replace whole rows ({index: text, None deletes}) of a copy of a fixture."""
    rows = snapshot.screen.removesuffix("\n").split("\n")
    for index, text in (edits or {}).items():
        rows[index] = text
    rows = [row for row in rows if row is not None]
    return snapshot.model_copy(update={"rows": len(rows), "screen": "\n".join(rows) + "\n", **changes})


def select(snapshot, number):
    """SYNTHETIC selection: the live capture only has option 1 selected, so its arrow is moved."""
    row = {2: 47, 3: 49, 4: 51}[number]
    line = snapshot.screen.split("\n")[row].removeprefix("\x1b[39m").removeprefix("  ")
    return rewrite(snapshot, {45: "  \x1b[38;5;246m1.\x1b[39m Maple", row: ARROW + line}, cursor_y=row)


class Classifier(unittest.TestCase):
    def test_actual_plan_questions_notes_switching_and_clear(self):
        for names in (("question-choice", "question-selected", "question-notes", "question-multi-first", "question-narrow"),
                      ("question-multi-second", "question-multi-notes")):
            verdicts = [classify_attention("codex", "0.160.0", capture(name)) for name in names]
            self.assertTrue(all(v == verdicts[0] for v in verdicts))
            self.assertEqual((verdicts[0].state, verdicts[0].kind), ("waiting", "question"))
            self.assertNotIn("Add notes", verdicts[0].prompt)
        notes = capture("question-notes")
        typed = notes.model_copy(update={"screen": notes.screen.replace("Add notes", "Synthetic answer")})
        self.assertEqual(classify_attention("codex", "0.160.0", notes),
                         classify_attention("codex", "0.160.0", typed))
        self.assertEqual(classify_attention("codex", "0.160.0", capture("question-multi-after")).state, "waiting")
        for name in ("question-cleared", "plan-mode"):
            self.assertEqual(classify_attention("codex", "0.160.0", capture(name)).state, "none")
        # Plain transcript text cannot imitate the selected native form.
        from qorqut_core.adapters.input_layout import SGR
        plain = notes.model_copy(update={"screen": SGR.sub("", notes.screen)})
        self.assertNotEqual(classify_attention("codex", "0.160.0", plain).state, "waiting")

    def test_actual_mcp_approval_and_selection_normalization(self):
        snapshot = capture()
        verdict = classify_attention("codex", "0.160.0", snapshot)
        self.assertEqual((verdict.state, verdict.kind), ("waiting", "approval"))
        self.assertIn('"fixture_check"?', verdict.prompt)
        self.assertIn("4. Cancel", verdict.prompt)
        self.assertNotIn("Field 1/1", verdict.prompt)
        screen = snapshot.screen.replace("\x1b[1;7m› 1.", "\x1b[1;7m  1.").replace(
            "    2. Allow", "  \x1b[1;7m› 2. Allow")
        selected = classify_attention("codex", "0.160.0", snapshot.model_copy(update={"screen": screen}))
        self.assertEqual(selected, verdict)

    def test_history_and_unsupported_observations(self):
        snapshot = capture()
        # The captured form, replayed above the normal composer, is history.
        rows = snapshot.screen.removesuffix("\n").split("\n")
        rows = rows[6:] + ["", "› Ask Codex to do anything", "", "  fixture · /fixture", "  ? for shortcuts", ""]
        history = snapshot.model_copy(update={"screen": "\n".join(rows) + "\n"})
        self.assertEqual(classify_attention("codex", "0.160.0", history).state, "none")
        for provider, version, changed in (
            ("claude", "2.1.287", snapshot), ("codex", "future", snapshot),
            ("codex", "0.160.0", snapshot.model_copy(update={"in_mode": True})),
            ("codex", "0.160.0", snapshot.model_copy(update={"screen": snapshot.screen + "\x1b[2J"})),
            ("codex", "0.160.0", snapshot.model_copy(update={"screen": snapshot.screen.replace("Field 1/1", "Field 2/3")})),
        ):
            with self.subTest(provider=provider, version=version, screen=changed.screen[-40:]):
                self.assertEqual(classify_attention(provider, version, changed).state, "unknown")

    def test_actual_claude_question_and_selection_normalization(self):
        snapshot = claude()
        verdict = classify_attention("claude", "2.1.287", snapshot)
        self.assertEqual((verdict.state, verdict.kind, verdict.truncated), ("waiting", "question", False))
        self.assertEqual(verdict.prompt, "Which label should be used for the test: Maple or Cedar?\n\n"
            "1. Maple\nUse Maple as the test label\n2. Cedar\nUse Cedar as the test label\n"
            "3. Type something.\n4. Chat about this")
        for number in (2, 3, 4):
            with self.subTest(selected=number):
                self.assertEqual(classify_attention("claude", "2.1.287", select(snapshot, number)), verdict)

    def test_claude_composer_clears_and_other_layouts_stay_unknown(self):
        captures = json.loads((FIXTURES / "claude-floating-composers.json").read_text())["captures"]
        for name in (c["name"] for c in captures):
            with self.subTest(composer=name):
                composer = claude(name, "claude-floating-composers.json")
                self.assertEqual(classify_attention("claude", "2.1.287", composer).state, "none")
        # The answered form, replayed as scrollback above the empty composer, is history.
        form = claude().screen.split("\n")[40:55]
        composer = claude("post-turn-dim-suggestion", "claude-floating-composers.json")
        history = rewrite(composer, dict(enumerate(form)))
        self.assertEqual(classify_attention("claude", "2.1.287", history).state, "none")
        typed = rewrite(composer, {50: "\x1b[39m❯\xa0typed text\x1b[7m \x1b[0m"}, cursor_x=13)
        self.assertEqual(classify_attention("claude", "2.1.287", typed).state, "unknown")
        # Codex screens are never read as Claude's.
        for name in json.loads((FIXTURES / "native-attention.json").read_text()):
            with self.subTest(codex_capture=name):
                self.assertEqual(classify_attention("claude", "2.1.287", capture(name)).state, "unknown")

    def test_claude_impostors_and_malformed_forms_are_not_waiting(self):
        snapshot = claude()
        rule = "─" * snapshot.columns
        arrow2 = ARROW + "\x1b[38;5;246m2.\x1b[39m Cedar"
        for label, changed in {
            "plain text": snapshot.model_copy(update={"screen": SGR.sub("", snapshot.screen)}),
            "plain upper rule": rewrite(snapshot, {40: rule}),
            "plain lower rule": rewrite(snapshot, {50: rule}),
            "plain tab": rewrite(snapshot, {41: " ☐ Test label "}),
            "plain question": rewrite(snapshot, {43: "Which label should be used for the test: Maple or Cedar?"}),
            "plain arrow": rewrite(snapshot, {45: "❯ \x1b[38;5;246m1.\x1b[39m \x1b[38;5;153mMaple\x1b[39m"}),
            "plain footer": rewrite(snapshot, {53: "Enter to select · ↑/↓ to navigate · Esc to cancel"}),
            "plain description": rewrite(snapshot, {46: "     Use Maple as the test label"}),
            "no arrow": rewrite(snapshot, {45: "  \x1b[38;5;246m1.\x1b[39m Maple"}),
            "two arrows": rewrite(snapshot, {47: arrow2}),
            "number gap": rewrite(snapshot, {47: "  \x1b[38;5;246m3.\x1b[39m Cedar"}),
            "missing Chat option": rewrite(snapshot, {51: ""}),
            "missing Type option": rewrite(snapshot, {49: "  \x1b[38;5;246m3.\x1b[39m Other"}),
            "one ordinary choice": rewrite(snapshot, {47: None, 48: None, 49: "  \x1b[38;5;246m2.\x1b[39m "
                "\x1b[38;5;246mType\x1b[39m \x1b[38;5;246msomething.\x1b[39m", 51: "\x1b[39m  3. Chat about this"}),
            "text below the form": rewrite(snapshot, {54: "● Maple"}),
            "unsupported style": rewrite(snapshot, {43: snapshot.screen.split("\n")[43].replace("\x1b[1m", "\x1b[1;3m")}),
            "unsupported escape": snapshot.model_copy(update={"screen": snapshot.screen + "\x1b[2J"}),
            # SYNTHETIC unsupported variants; none was captured, so none may be recognized.
            "multi-question tabs": rewrite(snapshot, {41: snapshot.screen.split("\n")[41] + "  ☐ Second  ✔ Submit →"}),
            "free-text answer": rewrite(snapshot, {49: ARROW + "\x1b[38;5;246m3.\x1b[39m my own answer"}),
            "visible cursor": snapshot.model_copy(update={"cursor_visible": True}),
            "cursor outside the form": snapshot.model_copy(update={"cursor_y": 39}),
            "pane in a mode": snapshot.model_copy(update={"in_mode": True}),
        }.items():
            with self.subTest(label):
                self.assertEqual(classify_attention("claude", "2.1.287", changed).state, "unknown")
        for provider, version in (("claude", "2.1.288"), ("claude", "future"), ("other", "2.1.287")):
            with self.subTest(provider=provider, version=version):
                self.assertEqual(classify_attention(provider, version, snapshot).state, "unknown")

    def test_claude_prompt_is_explicitly_bounded(self):
        snapshot = claude()
        lines = snapshot.screen.removesuffix("\n").split("\n")
        lines[46:46] = ["     \x1b[38;5;246m" + "x" * 60 + "\x1b[39m"] * 120
        changed = snapshot.model_copy(update={"rows": len(lines), "screen": "\n".join(lines) + "\n"})
        verdict = classify_attention("claude", "2.1.287", changed)
        self.assertEqual(verdict.state, "waiting")
        self.assertEqual(len(verdict.prompt), 6000)
        self.assertTrue(verdict.truncated)

    def test_wire_invariants(self):
        for data in ({"state": "waiting"}, {"state": "none"}, {"state": "unknown", "prompt": "old"},
                     {"state": "unknown", "truncated": True}):
            with self.assertRaises(ValidationError):
                NativeAttentionView(**data)

    def test_visible_prompt_is_explicitly_bounded(self):
        snapshot = capture()
        lines = snapshot.screen.removesuffix("\n").split("\n")
        option = next(i for i, line in enumerate(lines) if "› 1. Allow" in line)
        lines[option:option] = ["  " + "x" * 60] * 120
        changed = snapshot.model_copy(update={"rows": len(lines), "screen": "\n".join(lines) + "\n"})
        verdict = classify_attention("codex", "0.160.0", changed)
        self.assertEqual(verdict.state, "waiting")
        self.assertEqual(len(verdict.prompt), 6000)
        self.assertTrue(verdict.truncated)


class Observer(RuntimeCase):
    def setUp(self):
        super().setUp()
        self.candidate_owner = self.candidate()
        self.owners.connect(self.candidate_owner)
        self.owner_row = self.rows("session_owners")[0]
        self.service = self.app.native_attention
        self.snapshot = capture()
        self.observation = patch.object(self.app.runtime_input, "attention_snapshot",
            side_effect=lambda _: (self.owner_row, self.snapshot))
        self.observation.start()
        self.addCleanup(self.observation.stop)

    def attention_events(self):
        return self.events(self.host["id"], "native.attention_changed")

    def test_cached_reads_changes_clear_and_authored_status(self):
        before = self.get(self.host["id"])["status"]
        self.service.tick()
        with patch.object(self.app.runtime_input, "attention_snapshot", side_effect=AssertionError("Read did I/O")):
            runtime = self.runtime()
            agent = self.get(self.host["id"])
            listing = self.app.invoke("agents_list", self.context).data["items"][0]
        self.assertFalse(runtime["input_ready"])
        self.assertEqual(runtime["attention"]["state"], "waiting")
        self.assertEqual((agent["needs_user_input"], agent["activity"]), (True, "waiting_user"))
        self.assertEqual(listing["activity_observed_at"], runtime["attention"]["observed_at"])
        self.assertEqual(agent["status"], before)
        self.service.tick()
        self.assertEqual(len(self.attention_events()), 1)
        self.snapshot = self.snapshot.model_copy(update={"screen": self.snapshot.screen.replace("fixture_check", "fixture_other")})
        self.service.tick()
        self.assertEqual(len(self.attention_events()), 2)
        for name in ("question-choice", "question-selected", "question-notes"):
            self.snapshot = capture(name)
            self.service.tick()
            self.assertEqual(len(self.attention_events()), 3)
        self.snapshot = capture("question-multi-second")
        self.service.tick()
        self.assertEqual(len(self.attention_events()), 4)
        self.snapshot = self.snapshot.model_copy(update={"screen": "\n" * (self.snapshot.rows - 1) + "› Ask Codex to do anything\n"})
        self.service.tick()
        self.assertEqual(self.runtime()["attention"]["state"], "none")
        self.assertFalse(self.get(self.host["id"])["needs_user_input"])
        self.assertEqual(len(self.attention_events()), 5)
        self.assertEqual(self.get(self.host["id"])["status"], before)

    def test_stale_failure_owner_replacement_and_recovery(self):
        self.service.tick()
        with patch("qorqut_core.runtime.native_attention.time.monotonic", return_value=self.service._cache[self.host["id"]][-1] + 5.01):
            self.assertEqual(self.runtime()["attention"]["state"], "unknown")
            self.assertFalse(self.get(self.host["id"])["needs_user_input"])
        with patch.object(self.app.runtime_input, "attention_snapshot", side_effect=InputUnavailable("session_unavailable")):
            self.service.tick()
            self.service.tick()
        self.assertEqual(self.runtime()["attention"]["state"], "unknown")
        self.assertEqual(len(self.attention_events()), 2)
        self.service.tick()
        self.state(self.candidate_owner, "exited")
        self.owners.reconcile()
        self.assertEqual(self.runtime()["status"], "disconnected")
        self.owners.connect(self.candidate())
        self.assertIsNone(self.runtime()["attention"]["prompt"])
        self.assertFalse(self.get(self.host["id"])["needs_user_input"])
        self.owner_row = next(row for row in self.rows("session_owners") if row["state"] == "active")
        self.service.tick()
        self.assertEqual(self.runtime()["attention"]["state"], "waiting")
        with self.app.db.transaction() as tx:
            tx.connection.execute("UPDATE installation SET recovery_epoch=?", (str(uuid4()),))
        self.assertEqual(self.runtime()["attention"]["state"], "unknown")


class Snapshot(InputFixture):
    provider = "codex"

    def test_observes_human_control_and_pending_hook_without_input_effects(self):
        viewer = uuid4()
        self.manager.take_control(self.shell.terminal_id, expected_epoch=0, viewer_id=viewer)
        self.hook("PermissionRequest")
        self.tmux.snapshot = capture()
        saved = self.rows("session_owners")[0]

        def target(manager, row):
            assert_external(self, self.database, self.shell.terminal_id)
            return self.shell.tmux, None

        with patch.object(ShellManager, "_target", target), \
                patch("qorqut_core.runtime.input_dispatch._program"), \
                patch.object(ShellManager, "_inspect", side_effect=AssertionError("Binding adoption")), \
                patch.object(self.input, "_idle", side_effect=AssertionError("Input readiness")), \
                patch.object(self.input, "_configuration", side_effect=AssertionError("Hook preparation")):
            owner, screen = self.input.attention_snapshot(self.host["id"])
            self.assertEqual(owner, saved)
            self.assertEqual(screen, capture())
            self.assertEqual(self.tmux.effects, [])
            self.assertEqual(self.manager.control(self.shell.terminal_id).viewer_id, viewer)
            with patch.object(self.input, "_owner", side_effect=[saved, saved, {**saved, "owner_id": str(uuid4())}]):
                with self.assertRaises(InputUnavailable):
                    self.input.attention_snapshot(self.host["id"])
