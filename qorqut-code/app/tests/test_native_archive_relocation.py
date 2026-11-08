"""A copied session may move only after explicit verification; prior history stays immutable."""
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from qorqut.native_archive import CaptureBinding, CaptureError, TranscriptArchive


class RelocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "original.jsonl"
        self.source.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "conversation", "cwd": str(self.root)}}) + "\n")
        self.binding = CaptureBinding("agent", "codex", "conversation", 1, str(self.root), str(self.source))
        self.archive = TranscriptArchive(str(self.root / "archive"))
        self.first = self.archive.capture(self.binding)
        self.copy = self.root / "copy.jsonl"
        shutil.copyfile(self.source, self.copy)
        self.moved = replace(self.binding, generation=2, source_path=str(self.copy))
        self.lineage = self.root / "archive/codex/conversation"

    def rejected(self, code, call):
        with self.assertRaises(CaptureError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)

    def test_move_preserves_receipts_offsets_and_continues(self):
        original = (self.lineage / "receipts/000000000001.rcpt").read_bytes()
        self.rejected("source_path_changed", lambda: self.archive.capture(self.moved))
        self.copy.write_bytes(self.copy.read_bytes() + b'{"type":"event_msg","payload":{}}\n')
        second = self.archive.relocate_source(self.moved, expected=self.first)
        self.assertEqual(self.first.record_sha256, second.predecessor_sha256)
        self.assertEqual(original, (self.lineage / "receipts/000000000001.rcpt").read_bytes())
        self.assertEqual(self.copy.read_bytes(), self.archive.read_raw(second, offset=0, limit=4096).data)
        self.assertEqual(self.first, self.archive.load_receipt(self.first.receipt_id))
        self.assertEqual(second, TranscriptArchive(self.archive.archive_root).capture(self.moved))
        # Recovery must validate the source transition even after losing the pointer.
        (self.lineage / "latest.json").unlink()
        self.assertEqual(second, self.archive.capture(self.moved))

    def test_changed_prefix_or_workspace_never_authorized(self):
        self.copy.write_bytes(b'X' + self.copy.read_bytes()[1:])
        self.rejected("source_prefix_changed", lambda: self.archive.relocate_source(self.moved, expected=self.first))
        self.assertFalse(list(self.lineage.glob("relocation-*")))
        self.rejected("workspace_mismatch", lambda: self.archive.relocate_source(
            replace(self.moved, workspace=str(self.root / "other")), expected=self.first))

    def test_receipt_and_generation_must_match(self):
        self.rejected("receipt_mismatch", lambda: self.archive.relocate_source(
            self.moved, expected=replace(self.first, record_sha256="0" * 64)))
        self.rejected("invalid_binding", lambda: self.archive.relocate_source(
            replace(self.moved, generation=1), expected=self.first))

    def test_replaced_copy_and_missing_evidence_still_rejected(self):
        second = self.archive.relocate_source(self.moved, expected=self.first)
        self.copy.rename(self.root / "keep.jsonl")
        shutil.copyfile(self.root / "keep.jsonl", self.copy)
        self.rejected("source_replaced", lambda: self.archive.capture(self.moved))
        (self.lineage / "relocation-000000000001.json").unlink()
        self.rejected("archive_integrity", lambda: self.archive.capture(self.moved))
        self.assertEqual(second, self.archive.load_receipt(second.receipt_id))

    def test_published_receipt_recovers_after_crash(self):
        class CrashArchive(TranscriptArchive):
            def _crash_point(self, name):
                if name == "receipt_published":
                    raise RuntimeError("simulated crash")
        with self.assertRaises(RuntimeError):
            CrashArchive(self.archive.archive_root).relocate_source(self.moved, expected=self.first)
        second = self.archive.capture(self.moved)
        self.assertEqual(2, second.sequence)
        self.assertEqual(str(self.copy), second.source_path)


if __name__ == "__main__":
    unittest.main()
