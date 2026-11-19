from types import SimpleNamespace
import unittest

from qorqut.native_chat import projection


def record(text, uuid, offset=0):
    value = {"type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": "2026-11-04T10:00:00Z",
             "message": {"role": "user", "content": text}}
    return SimpleNamespace(kind="json", value=value, offset=offset, length=10)


class CommandRecordTest(unittest.TestCase):
    def feed(self, text):
        return projection.Projector("claude", "s1").feed(record(text, "u1"))

    def test_a_slash_command_shows_as_the_owner_typed_it(self):
        result = self.feed("<command-name>/compact</command-name>\n            <command-message>compact</command-message>\n"
                           "            <command-args>keep only decisions</command-args>")
        self.assertEqual((result.entry.speaker, result.entry.text), (projection.SPEAKER_USER, "/compact keep only decisions"))
        bare = self.feed("<command-name>/cost</command-name>\n<command-message>cost</command-message>\n<command-args></command-args>")
        self.assertEqual(bare.entry.text, "/cost")

    def test_a_command_does_not_open_a_turn(self):
        projector = projection.Projector("claude", "s1")
        projector.feed(record("<command-name>/clear</command-name>\n<command-args></command-args>", "u1"))
        self.assertIsNone(projector.state.origin)

    def test_command_output_and_reminders_stay_hidden(self):
        for text in ("<local-command-stdout>Not enough messages to compact.</local-command-stdout>",
                     "<system-reminder>x</system-reminder>", "<command-name></command-name>"):
            result = self.feed(text)
            self.assertIsNone(result.entry, text)


class InterruptRecordTest(unittest.TestCase):
    """A Stop is recorded by Claude Code as a user message; it is not something the owner said."""

    @staticmethod
    def stopped(text, uuid, blocks=True):
        content = [{"type": "text", "text": text}] if blocks else text
        value = {"type": "user", "uuid": uuid, "sessionId": "s1", "timestamp": "2026-11-19T10:00:00Z", "parentUuid": "p",
                 "message": {"role": "user", "content": content}}
        return SimpleNamespace(kind="json", value=value, offset=0, length=10)

    def test_a_recorded_stop_is_dropped_in_both_of_its_forms_and_in_both_content_shapes(self):
        for text in ("[Request interrupted by user]", "[Request interrupted by user for tool use]", "  [Request interrupted by user]\n"):
            for blocks in (True, False):
                result = projection.Projector("claude", "s1").feed(self.stopped(text, "u1", blocks))
                self.assertIsNone(result.entry, (text, blocks))
                self.assertTrue(result.hidden, (text, blocks))

    def test_it_ends_the_stopped_turn_so_the_next_one_starts_fresh(self):
        projector = projection.Projector("claude", "s1")
        projector.feed(record("do the long thing", "u0"))
        self.assertEqual(projector.state.origin, projection.ORIGIN_USER)
        projector.feed(self.stopped("[Request interrupted by user]", "u1"))
        self.assertIsNone(projector.state.origin)
        self.assertFalse(projector.state.work)

    def test_text_that_only_mentions_it_is_still_what_the_owner_typed(self):
        for text in ("please [Request interrupted by user] ok", "stop"):
            result = projection.Projector("claude", "s1").feed(self.stopped(text, "u2"))
            self.assertEqual((result.entry.speaker, result.entry.text), (projection.SPEAKER_USER, text))


if __name__ == "__main__":
    unittest.main()
