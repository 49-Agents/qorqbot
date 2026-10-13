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
from qorqut_core.runner.models import TerminalSnapshot
from qorqut_core.runner.shell import ShellManager
from qorqut_core.runtime.input_dispatch import InputUnavailable
from qorqut_core.runtime.models import NativeAttentionView


def capture(name="mcp_approval"):
    data = json.loads((Path(__file__).parents[1] / "adapters/fixtures/native-attention.json").read_text())
    return TerminalSnapshot.model_validate(data[name])


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
