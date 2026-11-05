"""Contract for the Telegram owner communication skill and its grants."""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SELECTING = {"ordubashy": "1.72.0"}
AGENT_GRANTS = ("telegram_send", "telegram_history", "telegram_delivery_get")
# Only Ordubashy holds the outbound file tool; no other role holds any Telegram operation.
FILE_GRANT = "telegram_send_file"
NEVER_GRANTED = ("telegram_status", "telegram_delivery_resolve", "telegram_notify")
SKILL = ROOT / "skills/telegram-owner-communication/SKILL.md"


def manifest(role):
    return yaml.safe_load((ROOT / f"roles/{role}/role.yaml").read_text())


class TelegramOwnerCommunicationTest(unittest.TestCase):
    def test_only_ordubashy_selects_the_skill_and_holds_the_telegram_grants(self):
        for path in sorted((ROOT / "roles").glob("*/role.yaml")):
            role = yaml.safe_load(path.read_text())
            selected = "telegram-owner-communication" in [item["id"] for item in role.get("skills", [])]
            grants = {name: mode for name, mode in role.get("default_tool_policy", {}).items() if name.startswith("telegram_")}
            with self.subTest(role=role["id"]):
                if role["id"] in SELECTING:
                    self.assertTrue(selected)
                    self.assertEqual(role["version"], SELECTING[role["id"]])
                    self.assertEqual(grants, dict.fromkeys((*AGENT_GRANTS, FILE_GRANT), "allow"))
                    self.assertIn({"id": "telegram-owner-communication", "path": "skills/telegram-owner-communication/SKILL.md"}, role["skills"])
                else:
                    self.assertFalse(selected)
                    self.assertEqual(grants, {})

    def test_owner_only_and_worker_operations_are_never_role_grants(self):
        for role in SELECTING:
            policy = manifest(role)["default_tool_policy"]
            for name in NEVER_GRANTED:
                self.assertNotIn(name, policy)

    def test_unrelated_content_is_preserved(self):
        ordubashy = manifest("ordubashy")
        self.assertLessEqual(len(ordubashy["skills"]), 20)
        # The browser procedure stays beside the Telegram additions.
        self.assertIn({"id": "browser-control", "path": "skills/managed-browser-control/SKILL.md"}, ordubashy["skills"])
        self.assertEqual(ordubashy["default_tool_policy"]["browser_turn"], "allow")
        for role in SELECTING:
            brief = (ROOT / manifest(role)["brief"]).read_text()
            # The Global Tables guidance from main and the Telegram paragraph live side by side in every brief.
            self.assertIn("Global Tables:", brief)
            self.assertIn("scope=global", brief)
            self.assertIn("telegram_send (telegram-owner-communication)", brief)
            data = manifest(role)
            ids = [item["id"] for item in data["skills"]]
            self.assertEqual(len(ids), len(set(ids)))
            for name in ("signoffs_input_create", "signoffs_input_cancel", "signoffs_attention_set"):
                self.assertEqual(data["default_tool_policy"][name], "allow")
        self.assertIn("owner-input", [item["id"] for item in manifest("ordubashy")["skills"]])
        generic = " ".join((ROOT / "skills/owner-input/SKILL.md").read_text().split())
        self.assertIn("Use `signoffs_input_create` for an owner response that is not an approval", generic)
        self.assertNotIn("Telegram", generic)

    def test_skill_frontmatter_and_briefs_link_to_it(self):
        text = SKILL.read_text()
        meta = yaml.safe_load(text.split("---")[1])
        self.assertEqual(meta["name"], "bazarlyk-role-telegram-owner-communication")
        self.assertTrue(1 <= len(meta["description"]) <= 600)
        for role in SELECTING:
            brief = " ".join((ROOT / manifest(role)["brief"]).read_text().split())
            self.assertIn("telegram_send (telegram-owner-communication)", brief)
            self.assertIn("scoped owner input, not blanket authorization", brief)

    def test_skill_states_the_contract(self):
        text = " ".join(SKILL.read_text().split())
        for phrase in (
                "you never supply a chat, a topic, a token, your identity or a formatting mode",
                "only `telegram_send_file` names a source",
                "not blanket authorization", "does not approve a prepared action",
                "Use Signoffs for approvals of prepared actions, durable questions",
                "When you are blocked, say so once and continue independent work",
                "`text` (plain text, 1-4000 characters", "The framework `idempotency_key` is required",
                "`blocking_input`", "`request_key`", "`input_mode`", "`expires_in_seconds` (60-3600)",
                "returns the existing request", "materially different question under a pending key is refused",
                "It expires within 10 minutes", "only one secret request may be outstanding in your topic",
                "never repeat a secret you received in any message, note, file or command output",
                "It is never shown to you in `telegram_history`", "`screen_action`",
                "`uncertain` means the request may or may not have reached Telegram", "Do not resend the same message",
                "up to your last 10 messages in your own topic, oldest first", "never downloaded",
                "over 16000 characters or containing terminal control characters is refused",
                "A reply never redirects to another agent", "A terminal you were not running is never started",
                "A reply to a post that is an action approval is an ordinary comment, not an approval",
                "the request stays pending unless the notes alone satisfy it",
                "not end-to-end encrypted", "48-hour limit", "no exactly-once guarantee", "Respect snooze", "verify the result yourself",
                "Agents do not write to Urgent Notifications; the application posts those",
                "one private Telegram topic with the owner", "one private forum with a separate topic for each conversation",
                "A message from any other topic is refused", "a reply that points at a message from another topic is refused",
                "never reach you either", "A reaction counts only on such a post", "a reply must be in that topic to that post",
                "a response that did not confirm your topic", "anything from before the owner moved your conversation into a topic",
                "you cannot create, rename or close a topic", "a topic is not a separate room"):
            self.assertIn(phrase, text)

    def test_skill_is_generic_and_carries_no_installation_facts(self):
        text = SKILL.read_text()
        for pattern in (r"\b\d{9,}:[A-Za-z0-9_-]{30,}", r"-100\d{8,}", r"qorqut:1071", r"/home/", r"\.local/state",
                        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"):
            self.assertIsNone(re.search(pattern, text), pattern)


if __name__ == "__main__":
    unittest.main()
