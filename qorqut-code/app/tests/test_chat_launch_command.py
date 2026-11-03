"""Chat names every CLI that connects in an agent's own terminal, the profile's default first."""
import json
import unittest

from bazarlyk.native_chat.chat import launch_commands


def profile(data):
    return {"profile_json": json.dumps(data)}


class LaunchCommandsTests(unittest.TestCase):
    def test_codex_default_lists_codex_first(self):
        self.assertEqual(launch_commands(profile({"runtime": "codex"})), ["codex", "claude"])

    def test_claude_code_default_lists_claude_first(self):
        self.assertEqual(launch_commands(profile({"runtime": "claude-code"})), ["claude", "codex"])

    def test_unknown_or_missing_default_still_lists_both(self):
        self.assertEqual(sorted(launch_commands(profile({"runtime": "gemini"}))), ["claude", "codex"])
        self.assertEqual(sorted(launch_commands(profile({}))), ["claude", "codex"])


if __name__ == "__main__":
    unittest.main()
