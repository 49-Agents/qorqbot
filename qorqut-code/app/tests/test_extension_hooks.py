"""The entries of an extension's hooks, read through a reader over a copy of its plugin part, their conditions, their
composition for the room at a point, the timed due rule, and extension texts in the stop correction."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import tempfile
import unittest

from qorqut import extension_hooks as hooks
from qorqut_agentic.behavior import completion
from qorqut_agentic.behavior.seeds import runtime_defaults

NETWORK = Path(__file__).parent / "fixtures/extension-contract/network/plugin"
FRAME = "From the {} extension:\n{}"


def entry(entry_id, at="owner_message", text="Text.", name="Network", **fields):
    return hooks.Entry(extension_id=name.lower(), extension_name=name, entry_id=entry_id, at=at, text=text, **fields)


class Reading(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.copy = Path(temporary.name) / "plugins/network"
        shutil.copytree(NETWORK, self.copy)
        self.reads = []

    def read(self, path):
        self.reads.append(path)
        target = self.copy / path
        return target.read_bytes() if target.is_file() else None

    def test_the_network_example_gives_its_four_entries(self):
        entries, findings = hooks.read_entries("network", "Network", self.read)
        self.assertEqual(findings, [])
        self.assertEqual([(e.entry_id, e.at) for e in entries], [
            ("note-people-mentioned", "owner_message"), ("after-crm-change", "after_tool"),
            ("check-follow-ups", "stop"), ("weekly-review", "timed")])
        self.assertEqual(entries[1].tools, ("crm_entries_update",))
        self.assertEqual(entries[3].every_seconds, 7 * 86400)
        self.assertTrue(all(e.if_used == () and e.unless_used == () for e in entries))
        self.assertEqual(entries[0].text, (NETWORK / "instructions/note-people-mentioned.md").read_text().strip())
        self.assertEqual({e.extension_id for e in entries} | {e.extension_name for e in entries}, {"network", "Network"})

    def test_a_text_changed_in_the_copy_is_what_is_read(self):
        (self.copy / "instructions/after-crm-change.md").write_text("If this entry is a Connection, check it.\n")
        entries, _ = hooks.read_entries("network", "Network", self.read)
        self.assertEqual(entries[1].text, "If this entry is a Connection, check it.")
        self.assertTrue(all(not Path(path).is_absolute() and ".." not in path for path in self.reads))

    def test_conditions_and_every_are_read(self):
        (self.copy / "hooks.yaml").write_text(
            "- id: check-follow-ups\n  at: stop\n  unless_used: [network_follow_up_set]\n"
            "  text: instructions/check-follow-ups.md\n"
            "- id: after\n  at: after_tool\n  tools: [crm_entries_update]\n  if_used: [tasks_update]\n"
            "  text: instructions/after-crm-change.md\n"
            "- id: hourly\n  at: timed\n  every: 90m\n  text: instructions/weekly-review.md\n")
        entries, findings = hooks.read_entries("network", "Network", self.read)
        self.assertEqual(findings, [])
        self.assertEqual(entries[0].unless_used, ("network_follow_up_set",))
        self.assertEqual(entries[1].if_used, ("tasks_update",))
        self.assertEqual(entries[2].every_seconds, 90 * 60)
        self.assertEqual(hooks.watched_tools(entries), ["network_follow_up_set", "tasks_update"])

    def test_no_hooks_file_gives_no_entries(self):
        (self.copy / "hooks.yaml").unlink()
        self.assertEqual(hooks.read_entries("network", "Network", self.read), ((), []))

    def test_a_copy_that_breaks_a_hook_rule_gives_no_entries_for_its_extension_and_one_log_line(self):
        cases = {
            "- id: a\n  at: later\n  text: instructions/weekly-review.md\n": "must have `at`",
            "- id: a\n  at: timed\n  every: 0d\n  text: instructions/weekly-review.md\n": "`every`",
            "- id: a\n  at: owner_message\n  text: instructions/missing.md\n": "does not exist",
            "- id: a\n  at: owner_message\n  text: ../../outside.md\n": "leaves the plugin directory",
            "- id: a\n  at: owner_message\n  text: /etc/hostname\n": "absolute",
            "- id: a\n  at: after_tool\n  text: instructions/weekly-review.md\n": "needs `tools`",
            "not: [a, list\n": "not valid YAML",
        }
        (self.copy.parent / "outside.md").write_text("Outside.")
        for body, finding in cases.items():
            with self.subTest(body=body):
                (self.copy / "hooks.yaml").write_text(body)
                self.reads.clear()
                _, findings = hooks.read_entries("network", "Network", self.read)
                self.assertTrue(any(finding in sentence for sentence in findings), findings)
                self.assertTrue(all(not path.startswith(("/", "..")) for path in self.reads), self.reads)
                with self.assertLogs("qorqut.extension_hooks", "WARNING") as logged:
                    self.assertEqual(hooks.entries_for(FakeRegistry({"network": self.read}), "agent-1"), ())
                self.assertEqual(len(logged.records), 1)
                self.assertIn("network", logged.output[0])

    def test_a_text_that_is_not_utf8_gives_no_entries(self):
        (self.copy / "instructions/weekly-review.md").write_bytes(b"\xff\xfe")
        _, findings = hooks.read_entries("network", "Network", self.read)
        self.assertTrue(findings)
        with self.assertLogs("qorqut.extension_hooks", "WARNING"):
            self.assertEqual(hooks.entries_for(FakeRegistry({"network": self.read}), "agent-1"), ())


class FakeRegistry:
    """The registry's ``found``, ``in_effect``, ``plugin_file``, ``refuse`` and ``layer.catalog.change_marker``, over readers
    by extension id; counts what is asked. ``marker`` and ``revisions`` stand for the layer copy changing."""

    def __init__(self, readers, names=None):
        from types import SimpleNamespace
        self.readers, self.names, self.calls, self.refused = readers, names or {}, [], []
        self.found = tuple(SimpleNamespace(id=ext_id, manifest=SimpleNamespace(name=self.names.get(ext_id, ext_id.title())))
                           for ext_id in sorted(readers))
        self.marker, self.revisions = 1, {}
        self.layer = SimpleNamespace(catalog=SimpleNamespace(change_marker=self.change_marker))

    def change_marker(self):
        self.calls.append(("change_marker",))
        return self.marker

    def in_effect(self, agent_id, *, connection=None, role=None):
        self.calls.append(("in_effect", agent_id))
        return tuple((ext_id, "1.0.0", self.revisions.get(ext_id, "r1")) for ext_id in sorted(self.readers)
                     if ext_id not in self.refused)

    def on_for(self, agent_id, *, connection=None, role=None):
        return tuple(item[0] for item in self.in_effect(agent_id, connection=connection, role=role))

    def plugin_file(self, ext_id, path):
        self.calls.append(("plugin_file", ext_id, path))
        return self.readers[ext_id](path)

    def refuse(self, ext_id, finding):
        self.calls.append(("refuse", ext_id))
        self.refused.append(ext_id)


class EntriesFor(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.copy = Path(temporary.name) / "network"
        shutil.copytree(NETWORK, self.copy)

    def read(self, path):
        target = self.copy / path
        return target.read_bytes() if target.is_file() else None

    def test_with_no_extension_found_nothing_is_read_and_with_none_in_effect_only_the_registry(self):
        nothing = FakeRegistry({})
        self.assertEqual(hooks.entries_for(nothing, "agent-1", "stop"), ())
        self.assertEqual(nothing.calls, [])  # no store read and no layer read
        self.assertEqual(hooks.entries_for(None, "agent-1"), ())
        registry = FakeRegistry({"network": self.read})
        registry.refused.append("network")  # found, but in effect for nobody
        self.assertEqual(hooks.entries_for(registry, "agent-1"), ())
        self.assertEqual(registry.calls, [("in_effect", "agent-1")])

    def test_parsed_entries_are_kept_until_the_layer_copy_changes(self):
        registry = FakeRegistry({"network": self.read})
        first = hooks.entries_for(registry, "agent-1")
        reads = lambda: sum(call[0] == "plugin_file" for call in registry.calls)
        once = reads()
        self.assertGreater(once, 0)
        for _ in range(5):
            self.assertEqual(hooks.entries_for(registry, "agent-1", "after_tool"),
                             tuple(e for e in first if e.at == "after_tool"))
        self.assertEqual(reads(), once)  # no layer file read and no parse per call while nothing changed
        (self.copy / "instructions/after-crm-change.md").write_text("Changed.\n")
        registry.marker = 2  # the layer's change marker moved
        self.assertEqual(hooks.entries_for(registry, "agent-1", "after_tool")[0].text, "Changed.")
        self.assertEqual(reads(), 2 * once)
        registry.revisions["network"] = "r2"  # a new package revision is read again too
        hooks.entries_for(registry, "agent-1")
        self.assertEqual(reads(), 3 * once)
        registry.marker = None  # a marker that cannot be read reuses nothing
        hooks.entries_for(registry, "agent-1")
        hooks.entries_for(registry, "agent-1")
        self.assertEqual(reads(), 5 * once)

    def test_entries_are_read_from_the_layer_copy_and_filtered_by_point(self):
        registry = FakeRegistry({"network": self.read}, {"network": "Network"})
        self.assertEqual([e.entry_id for e in hooks.entries_for(registry, "agent-1", "stop")], ["check-follow-ups"])
        self.assertEqual(len(hooks.entries_for(registry, "agent-1")), 4)
        self.assertTrue(all(call[0] != "plugin_file" or call[1] == "network" for call in registry.calls))
        self.assertTrue(hooks.needs_owner_turn(hooks.entries_for(registry, "agent-1")))
        self.assertFalse(hooks.needs_owner_turn(hooks.entries_for(registry, "agent-1", "after_tool")))

    def test_a_broken_extension_leaves_the_others(self):
        broken = lambda path: b"not: [a, list\n" if path == "hooks.yaml" else None
        registry = FakeRegistry({"network": self.read, "broken": broken})
        with self.assertLogs("qorqut.extension_hooks", "WARNING"):
            entries = hooks.entries_for(registry, "agent-1")
        self.assertEqual({e.extension_id for e in entries}, {"network"})
        self.assertEqual(registry.refused, ["broken"])  # shown as needing attention, out of effect from now on
        self.assertEqual({e.extension_id for e in hooks.entries_for(registry, "agent-1")}, {"network"})


class Conditions(unittest.TestCase):
    def test_applies(self):
        plain = entry("plain")
        if_used = entry("if", if_used=("a", "b"))
        unless = entry("unless", unless_used=("c",))
        both = entry("both", if_used=("a",), unless_used=("c",))
        empty_if = entry("empty-if", if_used=())  # the reader gives an empty list when the key is not given
        table = [
            (plain, None, True), (plain, [], True), (plain, ["a"], True),
            (if_used, None, False), (if_used, [], False), (if_used, ["b"], True), (if_used, ["x"], False),
            (unless, None, False), (unless, [], True), (unless, ["c"], False), (unless, ["a"], True),
            (both, ["a"], True), (both, ["a", "c"], False), (both, [], False),
            (empty_if, ["a"], True), (entry("empty-unless", unless_used=()), ["a"], True),
        ]
        for value, used, expected in table:
            with self.subTest(entry=value.entry_id, used=used):
                self.assertIs(hooks.applies(value, used), expected)


class Composing(unittest.TestCase):
    def setUp(self):
        self.instructions = runtime_defaults()
        self.pointer = self.instructions.render("extension-hook-rest", FILE=".qorqut/extension-notes-owner_message.md")
        self.first, self.second, self.third = ("First text. " + "a" * 200, "Second text. " + "b" * 200,
                                               "Third text. " + "c" * 200)
        self.entries = (entry("one", text=self.first), entry("two", text=self.second, name="Other"),
                        entry("three", text=self.third))

    def test_everything_that_fits_follows_qorqbots_text_in_order(self):
        framed = [FRAME.format("Network", self.first), FRAME.format("Other", self.second), FRAME.format("Network", self.third)]
        for own in ("", "Qorqbot's own reminder."):
            with self.subTest(own=own):
                composed = hooks.compose(own, self.entries, 10_000, self.instructions, "owner_message")
                self.assertEqual(composed.text, "\n\n".join(filter(None, [own, *framed])))
                self.assertEqual((composed.rest, composed.rest_file, composed.spilled), (None, None, ()))
                self.assertEqual(composed.delivered, self.entries)

    def test_what_does_not_fit_goes_to_the_rest_file_in_order(self):
        own = "Qorqbot's own reminder."
        first = FRAME.format("Network", self.first)
        limit = len(own) + 2 + len(first) + 2 + len(self.pointer)
        composed = hooks.compose(own, self.entries, limit, self.instructions, "owner_message")
        self.assertEqual(composed.text, own + "\n\n" + first + "\n\n" + self.pointer)
        self.assertEqual(composed.rest, FRAME.format("Other", self.second) + "\n\n" + FRAME.format("Network", self.third))
        self.assertEqual(composed.rest_file, ".qorqut/extension-notes-owner_message.md")
        self.assertEqual((composed.delivered, composed.spilled), (self.entries[:1], self.entries[1:]))
        self.assertEqual(len(composed.text), limit)
        # A later text that would fit alone never jumps ahead of one that does not.
        long = (entry("long", text="x" * 500),) + self.entries
        composed = hooks.compose(own, long, limit, self.instructions, "owner_message")
        self.assertEqual(composed.text, own + "\n\n" + self.pointer)
        self.assertEqual(composed.spilled, long)

    def test_the_pointing_sentence_is_always_delivered(self):
        own = "o" * 300
        limit = len(self.pointer) + 100
        composed = hooks.compose(own, self.entries, limit, self.instructions, "owner_message")
        self.assertEqual(composed.text, "o" * 98 + "\n\n" + self.pointer)  # Qorqbot's text cut only as far as needed
        self.assertEqual(len(composed.text), limit)
        self.assertEqual(composed.spilled, self.entries)
        composed = hooks.compose("", self.entries, 10, self.instructions, "owner_message")
        self.assertEqual(composed.text, self.pointer)

    def test_no_entries_leave_qorqbots_text_as_it_is(self):
        for own in ("", "o" * 300):
            with self.subTest(own=len(own)):
                composed = hooks.compose(own, (), 10, self.instructions, "start")
                self.assertEqual((composed.text, composed.rest, composed.delivered), (own, None, ()))

    def test_the_templates_are_managed_messages(self):
        self.assertEqual(self.instructions.render("extension-hook-text", NAME="Network", TEXT="Hello."),
                         FRAME.format("Network", "Hello."))
        self.assertIn(".qorqut/x.md", self.instructions.render("extension-hook-rest", FILE=".qorqut/x.md"))


class Timed(unittest.TestCase):
    def test_the_first_nudge_comes_one_every_after_the_anchor(self):
        weekly = entry("weekly", at="timed", every_seconds=7 * 86400)
        switched_on = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        self.assertFalse(hooks.due(weekly, switched_on, switched_on))
        self.assertFalse(hooks.due(weekly, switched_on, switched_on + timedelta(days=7, seconds=-1)))
        self.assertTrue(hooks.due(weekly, switched_on, switched_on + timedelta(days=7)))
        self.assertFalse(hooks.due(entry("not-timed"), switched_on, switched_on + timedelta(days=30)))


class StopCorrection(unittest.TestCase):
    def setUp(self):
        self.instructions = runtime_defaults()
        self.issues = {"records": {"code": "records", "message": "Record the work."}}
        self.saved = {"records": {"claimed_at": "now"}}

    def result(self, claimed, notes=(), **fields):
        return completion.result(self.instructions, self.issues, self.saved, claimed, "checked", notes=notes,
                                 rest_file=".qorqut/extension-notes-stop.md", **fields)

    def test_without_notes_the_answer_is_as_before(self):
        for claimed in ([], ["Record the work.", "Record the work.", "Close the task."], ["y" * 3000]):
            with self.subTest(claimed=len(claimed)):
                before = completion.result(self.instructions, self.issues, self.saved, claimed, "checked")
                self.assertEqual(self.result(claimed), before)
                self.assertEqual(before["correction"], "\n".join(dict.fromkeys(claimed))[:2500] if claimed else None)
                self.assertNotIn("correction_rest", before)

    def test_own_messages_first_then_extension_texts(self):
        answer = self.result(["Record the work."], [("Network", "Check follow ups.")])
        self.assertEqual(answer["correction"], "Record the work.\n\n" + FRAME.format("Network", "Check follow ups."))
        self.assertNotIn("correction_rest", answer)
        answer = self.result([], [("Network", "Check follow ups.")])
        self.assertEqual(answer["correction"], FRAME.format("Network", "Check follow ups."))

    def test_texts_that_do_not_fit_go_to_the_rest_file_uncut(self):
        own = "z" * 2000
        notes = [("Network", "a" * 100), ("Other", "b" * 400)]
        answer = self.result([own], notes)
        pointer = self.instructions.render("extension-hook-rest", FILE=".qorqut/extension-notes-stop.md")
        self.assertEqual(answer["correction"], own + "\n\n" + FRAME.format("Network", "a" * 100) + "\n\n" + pointer)
        self.assertLessEqual(len(answer["correction"]), completion.CORRECTION_LIMIT)
        self.assertEqual(answer["correction_rest"], {"path": ".qorqut/extension-notes-stop.md",
                                                     "text": FRAME.format("Other", "b" * 400)})

    def test_own_messages_that_fill_the_bound_are_cut_for_the_pointer(self):
        pointer = self.instructions.render("extension-hook-rest", FILE=".qorqut/extension-notes-stop.md")
        answer = self.result(["y" * 3000], [("Network", "Check follow ups.")])
        self.assertEqual(answer["correction"], "y" * (2500 - 2 - len(pointer)) + "\n\n" + pointer)
        self.assertEqual(len(answer["correction"]), completion.CORRECTION_LIMIT)
        self.assertEqual(answer["correction_rest"]["text"], FRAME.format("Network", "Check follow ups."))


if __name__ == "__main__":
    unittest.main()
