"""Small synthetic records matching the accepted pinned-native rehearsal."""

import json
import unittest
from uuid import uuid4

from qorqut_core.adapters.native_records import MAX_RECORD_BYTES, parse_native_record


SESSION = "11111111-2222-4333-8444-555555555555"


def claude(text=" exact 文🙂\n\t", *, role="user", native_id=None, **changes):
    record = {"type": role, "sessionId": SESSION, "uuid": native_id or str(uuid4()),
        "isSidechain": False, "version": "2.1.287", "timestamp": "2026-10-08T12:00:00Z",
        "origin": {"kind": "human"}, "promptSource": "typed",
        "message": {"role": role, "content": text if role == "user" else [{"type": "text", "text": text}],
            "stop_reason": "end_turn"}}
    return {**record, **changes}


def codex(text=" exact 文🙂\n\t", *, role="UserMessage", native_id=None, **changes):
    return {"type": "event_msg", "timestamp": "2026-10-08T12:00:00Z", "ordinal": 9,
        "payload": {"type": "item_completed", "thread_id": SESSION, "turn_id": str(uuid4()),
            "item": {"type": role, "id": native_id or str(uuid4()), "content": [
                {"type": "text" if role == "UserMessage" else "Text", "text": text}]}}, **changes}


def raw(record):
    return json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"


class NativeRecords(unittest.TestCase):
    def test_observed_profiles_identity_echo_exclusion_parts_and_explicit_limits(self):
        for provider, version, make, assistant in (
                ("claude", "2.1.287", claude, "assistant"),
                ("codex", "0.160.0", codex, "AgentMessage")):
            def parse(record, **kwargs):
                return parse_native_record(provider, kwargs.get("version", version), SESSION,
                    record if isinstance(record, bytes) else raw(record))

            text = " exact 文🙂\n\t"
            first, second = parse(make(text)), parse(make(text))
            self.assertEqual((first.disposition, first.items[0].role, first.items[0].text), ("messages", "user", text))
            self.assertNotEqual(first.items[0].native_id, second.items[0].native_id)
            self.assertEqual(parse(make(role=assistant)).items[0].role, "agent")
            long_text = "a" * 65535 + "文🙂" * 14000 + "\t\n "
            parts = parse(make(long_text)).items
            self.assertEqual("".join(item.text for item in parts), long_text)
            self.assertEqual([item.part_index for item in parts], list(range(len(parts))))
            self.assertTrue(all(len(item.text.encode("utf-8")) <= 65536 for item in parts))
            for timestamp in (None, "unusable", "2026-10-08T12:00:00"):
                item = parse(make(timestamp=timestamp)).items[0]
                self.assertEqual((item.occurred_at, item.timestamp_origin), (None, "ingestion"))
            self.assertEqual(parse(make(timestamp="2026-10-08T08:00:00-04:00")).items[0].occurred_at,
                             "2026-10-08T12:00:00.000000Z")
            for malformed in (b"{", b"\xff", b'{"type":"a","type":"b"}', b"[]",
                              b"[" * 2000, b"x" * (MAX_RECORD_BYTES + 1)):
                self.assertEqual(parse(malformed).disposition, "gap")
            self.assertEqual(parse(make(), version="future").reason, "unsupported_profile")

        def cp(record):
            return parse_native_record("claude", "2.1.287", SESSION, raw(record))

        self.assertEqual(cp(claude(isSidechain=True)).reason, "sidechain")
        self.assertEqual(cp(claude(sessionId=str(uuid4()))).reason, "session_mismatch")
        self.assertEqual(cp(claude(origin={"kind": "system"})).disposition, "gap")
        self.assertEqual(cp(claude(isMeta=True)).disposition, "ignored")
        self.assertEqual(cp(claude(uuid=None)).disposition, "gap")
        mixed = claude(role="assistant")
        mixed["message"]["content"].append({"type": "tool_use", "id": "call-1", "name": "never_execute"})
        self.assertEqual(cp(mixed).reason, "unsupported_content")
        self.assertEqual(cp({"type": "attachment", "sessionId": SESSION}).disposition, "gap")

        def xp(record):
            return parse_native_record("codex", "0.160.0", SESSION, raw(record))

        for role in ("user", "assistant", "developer", "system"):
            echo = {"type": "response_item", "payload": {"type": "message", "id": "echo-id",
                "role": role, "content": [{"type": "input_text", "text": "same"}]}}
            self.assertEqual(xp(echo).disposition, "ignored")
        self.assertEqual(xp({"type": "event_msg", "payload": {
            "type": "task_complete", "last_agent_message": "same"}}).disposition, "ignored")
        wrong = codex()
        wrong["payload"]["thread_id"] = str(uuid4())
        self.assertEqual(xp(wrong).reason, "session_mismatch")
        wrong["payload"].pop("thread_id")
        self.assertEqual(xp(wrong).disposition, "gap")
        self.assertEqual(xp(codex(role="Reasoning")).disposition, "gap")
        self.assertEqual(xp({"type": "response_item", "payload": {"type": "function_call"}}).disposition, "gap")
        self.assertEqual(xp({"type": "event_msg", "payload": {"type": "agent_message_delta"}}).disposition, "gap")
