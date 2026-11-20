"""Chat names the one command that starts an agent in its own terminal: Claude Code is the only client that runs there."""
import json
import unittest

from qorqut.native_chat.chat import launch_commands


def profile(data):
    return {"profile_json": json.dumps(data)}


class LaunchCommandsTests(unittest.TestCase):
    def test_every_default_lists_claude_only(self):
        for data in ({"runtime": "codex"}, {"runtime": "claude-code"}, {"runtime": "gemini"}, {}):
            self.assertEqual(launch_commands(profile(data)), ["claude"])


if __name__ == "__main__":
    unittest.main()
