from types import SimpleNamespace
import unittest

from bazarlyk.native_chat import projection


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


if __name__ == "__main__":
    unittest.main()
