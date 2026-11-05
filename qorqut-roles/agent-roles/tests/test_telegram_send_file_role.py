"""Ordubashy's outbound Telegram file grant and the shared skill's instructions (source only).

The host operation, its gates and the adoption are other work; nothing here needs the host or a live agent.
"""
from pathlib import Path
import hashlib
import json
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/telegram-owner-communication/SKILL.md"
GRANT = "telegram_send_file"
OWNER_COMMUNICATION = ("telegram_send", "telegram_history", "telegram_delivery_get", GRANT)
RETIRED_SETUP = ("business_setup_get", "business_setup_update", "business_setup_submit")
# The managed-browser dialog-recovery instructions, byte for byte.
ADOPTED_DIALOG_BLOCK = "b927d8b81b480a755a41cd97bc0f41e0a82223e02912ff8d832387fddafd026f"


def manifest(name):
    return yaml.safe_load((ROOT / "roles" / name / "role.yaml").read_text())


def flat(text):
    return " ".join(text.split())


def file_section():
    text = SKILL.read_text()
    start = text.index("## telegram_send_file\n")
    return text[start:text.index("\n## ", start + 1)]


def examples():
    """The fenced JSON blocks of the skill's file section, parsed as documentation only."""
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", file_section(), re.S)]


class OrdubashyGrantTest(unittest.TestCase):
    def test_only_ordubashy_holds_the_file_tool_and_the_owner_communication_grants(self):
        for path in sorted((ROOT / "roles").glob("*/role.yaml")):
            role = yaml.safe_load(path.read_text())
            with self.subTest(role=role["id"]):
                policy = role.get("default_tool_policy", {})
                if role["id"] == "ordubashy":
                    self.assertEqual(policy[GRANT], "allow")
                else:
                    self.assertFalse([name for name in policy if name.startswith("telegram_")])
        ordubashy = manifest("ordubashy")
        policy = ordubashy["default_tool_policy"]
        self.assertEqual(sorted(name for name in policy if name.startswith("telegram_")), sorted(OWNER_COMMUNICATION))
        for name in ("telegram_status", "telegram_delivery_resolve", "telegram_notify"):
            self.assertNotIn(name, policy)
        self.assertFalse(set(RETIRED_SETUP) & set(policy))                       # the checklist grants stay retired

    def test_the_chat_references_and_browser_guidance_are_preserved(self):
        ordubashy = manifest("ordubashy")
        self.assertEqual(ordubashy["default_tool_policy"]["request_user_input"], "allow")
        selected = {skill["id"]: skill["path"] for skill in ordubashy["skills"]}
        self.assertEqual(selected["chat-references"], "skills/chat-references/SKILL.md")
        self.assertEqual(selected["browser-control"], "skills/managed-browser-control/SKILL.md")
        managed = (ROOT / "skills/managed-browser-control/SKILL.md").read_text()
        start = managed.index("- A native JavaScript alert")
        block = managed[start:managed.index("- `goto <url>`", start)]
        self.assertEqual(hashlib.sha256(block.encode()).hexdigest(), ADOPTED_DIALOG_BLOCK)
        self.assertEqual(managed.count(block), 1)

    def test_brief_keeps_its_telegram_paragraph_and_names_no_file_tool_within_its_limit(self):
        brief = (ROOT / manifest("ordubashy")["brief"]).read_text()
        self.assertLessEqual(len(brief.strip()), 10000)
        self.assertIn("telegram_send (telegram-owner-communication)", flat(brief))
        self.assertIn("scoped owner input, not blanket authorization", flat(brief))
        # The file tool is documented in the shared skill only; the brief never carried it.
        self.assertNotIn(GRANT, brief)

    def test_the_selected_skill_is_one_packaged_file_for_the_only_selecting_role(self):
        entry = {"id": "telegram-owner-communication", "path": "skills/telegram-owner-communication/SKILL.md"}
        self.assertIn(entry, manifest("ordubashy")["skills"])
        for name in ("global-worker", "learning-applier"):
            self.assertNotIn(entry["id"], [skill["id"] for skill in manifest(name).get("skills", [])], name)
        self.assertEqual(sorted(path.name for path in SKILL.parent.rglob("*") if path.is_file()), ["SKILL.md"])
        meta = yaml.safe_load(SKILL.read_text().split("---")[1])
        self.assertEqual(meta["name"], "bazarlyk-role-telegram-owner-communication")
        self.assertTrue(1 <= len(meta["description"]) <= 600)
        self.assertIn(GRANT, meta["description"])


class SkillContractTest(unittest.TestCase):
    def test_closed_contract_source_hash_mode_caption_and_reply(self):
        text = flat(file_section())
        for phrase in (
                "Sends one file to your own topic, as a downloadable document or a displayed photo",
                "Loading this skill grants no tool: use it only when `telegram_send_file` is among your granted tools",
                "only from the currently configured, active Global Ordubashy bound to the owner's topic",
                "a role name or an added permission on any other agent is not enough, and every other caller is refused",
                "The arguments are a closed set; any other field is refused",
                "The framework `idempotency_key` is required on every call",
                "a file is sent once per call (no albums, edits or multiple files)",
                "You cannot supply a chat, a topic, raw bytes, base64, a Telegram file id or a URL",
                '`{"type": "workspace", "path": "exports/report.pdf"}`', "relative POSIX path inside the `exports/` or `downloads/` subtree only",
                "Absolute paths, `.` or `..` segments, empty segments, backslashes, control characters, hidden (dot-leading) names",
                "internal receipt names are refused", "directories, symbolic links, special files and files with more than one hard link",
                '`{"type": "document", "document_id": "<document-id>", "business_id": "<project-id>"}`',
                "`business_id` is required: the owning project, or `null` to name Global explicitly",
                "assets (`assets_get`), data uploads (`data_uploads_get`), agent and project notes (`notes_get`) and content or context notes (`content_get`)",
                "Every other kind is refused, including owner journal entries, working memory, raw email and Context graph or history data",
                "The project must be active, the document ready and not archived",
                "checked again when the send is queued and again just before upload; lost access cancels the send",
                "`expected_sha256`: required, exactly 64 lowercase hexadecimal characters",
                "for example the output of `sha256sum`", "without any `sha256:` prefix", "refuses a mismatch",
                "read it again deliberately, confirm it is still the deliverable, recompute the digest and send as a new intent (fresh key)",
                "`send_as`: required, `document` or `photo`",
                "`document` for the original, downloadable file or image (1 to 50,000,000 bytes, bytes preserved)",
                "`photo` only to display a static JPEG or PNG (1 to 10,000,000 bytes, a single frame, width plus height at most 10000 pixels, "
                "the longer side at most 20 times the shorter)",
                "it is never converted, resized or silently sent as a document, so decide the mode yourself",
                "`purpose`: required, `reply` or `update`", "`blocking_input` is not available for files",
                "`caption`: optional, empty by default. Plain text, nothing is interpreted as formatting",
                "at most 1024 characters and at most 1024 UTF-16 code units",
                "`reply_to`: optional, a local `message_id` from `telegram_history` in your own topic that Telegram has already accepted",
                "is refused before anything is queued",
                "The filename is the source's own basename (at most 255 UTF-8 bytes, no separators or control characters); "
                "there is no argument to rename it"):
            self.assertIn(phrase, text)

    def test_workflow_is_clear_and_truthful(self):
        text = flat(file_section())
        for phrase in (
                "Finish and check the deliverable the owner asked for or your work covers",
                "needs no extra approval step, but it is not approval of anything either",
                "must never carry a secret, a credential or content you were not meant to share",
                "`document` for the original file or an image the owner should download unchanged, `photo` for a static JPEG or PNG meant to be seen in the chat",
                "Call `telegram_send_file` once with a stable `idempotency_key` for this intent",
                "Reuse the same key only to retry the identical call after a lost response; a duplicate key returns the original receipt",
                "Any change of source, digest, mode, purpose, caption or reply target is a new intent and needs a fresh key",
                "the same key with different arguments is a conflict",
                "`queued` means accepted for sending, not sent: tell the owner it is queued, never that it was delivered",
                "read `telegram_delivery_get` once later instead of polling",
                "never expose its bytes or where the application keeps them",
                "seals a private copy of the validated bytes, so later edits, removal or replacement of the original file do not change what is delivered",
                "there is no tool to edit or recall a file already sent",
                "If the outcome is `uncertain`, do not resend it, retry under a new key or switch to the other mode",
                "the owner reconciles it and the original bytes and destination stay frozen",
                "Tell the owner through the channel they are watching",
                "send again only as a new intent with a fresh key once the cause is fixed",
                "affects only that file; `telegram_send` keeps working",
                "Never look for another route: no owner web link or HTTP call, no bot token, no URL, no other topic",
                "tell the owner in text what could not be sent and why"):
            self.assertIn(phrase, text)

    def test_existing_text_communication_guidance_stays_accurate(self):
        text = flat(SKILL.read_text())
        for phrase in (
                "The application chooses your topic; you never supply a chat, a topic, a token, your identity or a formatting mode, "
                "and only `telegram_send_file` names a source",
                "Text only. A message with an attachment reaches you as its caption plus a marker that the attachment was not delivered; "
                "files, images and voice are never downloaded, so ask for the content as text. File sending is outbound only.",
                "Use Signoffs for approvals of prepared actions, durable questions and anything that needs an auditable decision",
                "Agents do not write to Urgent Notifications; the application posts those",
                "It expires within 10 minutes", "It is never shown to you in `telegram_history`",
                "never repeat a secret you received in any message, note, file or command output",
                "A terminal you were not running is never started for a Telegram message",
                "a reply that points at a message from another topic is refused",
                "A reply to a post that is an action approval is an ordinary comment, not an approval",
                "Do not resend the same message (or file)",
                "Text, questions and blocking input always use `telegram_send`",
                "A file you send is stored by Telegram like any message. Keep secrets, credentials and content outside the authorized deliverable "
                "out of files and captions",
                "Text over 16000 characters or containing terminal control characters is refused",
                "`text` (plain text, 1-4000 characters"):
            self.assertIn(phrase, text)

    def test_fenced_examples_parse_as_documentation(self):
        # Documentation parsing only: this is not schema acceptance. The real validator belongs to the assembled host operation.
        found = examples()
        self.assertEqual(len(found), 3)
        self.assertEqual([(item["source"]["type"], item["send_as"]) for item in found],
                         [("workspace", "document"), ("workspace", "photo"), ("document", "document")])
        self.assertIsNone(found[2]["source"]["business_id"])                      # Global is named explicitly
        for item in found:
            self.assertNotIn("idempotency_key", item)                              # the framework supplies it

    def test_instructions_carry_no_installation_facts(self):
        readme = (ROOT / "README.md").read_text()
        paragraph = readme[readme.index("Telegram: Ordubashy grants"):readme.index("Chat references:")]
        for label, text in (("skill", SKILL.read_text()), ("readme paragraph", paragraph)):
            for pattern in (r"\b\d{9,}:[A-Za-z0-9_-]{30,}", r"-100\d{8,}", r"qorqut:1071", r"/home/", r"\.local/state",
                            r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", r"\.coordination", r"worktrees", r"@[A-Za-z0-9.-]+\.[a-z]{2,}",
                            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r"\b[0-9a-f]{64}\b"):
                self.assertIsNone(re.search(pattern, text), (label, pattern))


class DocumentationTest(unittest.TestCase):
    def test_readme_versions_match_the_manifests_and_claim_no_adoption(self):
        readme = (ROOT / "README.md").read_text()
        for name in ("ordubashy", "global-worker", "learning-applier"):
            self.assertIn(f"| `roles/{name}/` | {manifest(name)['version']} |", readme)
        text = flat(readme[readme.index("Telegram: Ordubashy grants"):readme.index("Chat references:")])
        for phrase in (
                "`telegram_send_file`; no other role holds any of them",
                "a downloadable `document` or a displayed `photo` (static JPEG or PNG)",
                "(relative path under `exports/` or `downloads/`)", "an explicit `business_id` (`null` for Global)",
                "`expected_sha256` is required", "at most 1024 characters and 1024 UTF-16 code units",
                "there is no chat, topic, token, URL, bytes or file-id argument",
                "Receiving media is unchanged", "the skill grants nothing",
                "allows only the currently configured, active Global Ordubashy bound to the owner's topic to call it "
                "(a role name or an added permission on another agent is not enough)"):
            self.assertIn(phrase, text)
        self.assertIn("source edits\nnever change live pins, grants or sessions", readme)


if __name__ == "__main__":
    unittest.main()
