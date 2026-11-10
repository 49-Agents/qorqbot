"""The archive hashes a payload's bytes once, not on every capture and read; what it gives up is bounded and explicit."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from qorqut.native_archive import CaptureBinding, CaptureError, FinalCaptureEvidence, TranscriptArchive
from qorqut.native_archive import _proven
from qorqut.native_archive._proven import PROOFS, Proofs


def line(index, size=200):
    return (json.dumps({"type": "event_msg", "payload": {"index": index, "text": "x" * size}}) + "\n").encode()


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "session.jsonl"
        self.source.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "conversation", "cwd": str(self.root)}}) + "\n")
        self.binding = CaptureBinding("agent", "codex", "conversation", 1, str(self.root), str(self.source))
        self.now = 1000.0
        saved = (PROOFS.clock, PROOFS.full_every)
        PROOFS.forget()
        PROOFS.clock = lambda: self.now
        self.addCleanup(lambda: (setattr(PROOFS, "clock", saved[0]), setattr(PROOFS, "full_every", saved[1]),
                                 PROOFS.forget()))
        self.archive = TranscriptArchive(str(self.root / "archive"), clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.payload = self.root / "archive/codex/conversation/payload.raw"
        self.counted = 0
        case, real = self, hashlib.sha256

        class Counting:
            """A running digest that counts what it is fed; a digest of given data is the real one."""

            def __new__(cls, *data, inner=None):
                if data:
                    return real(*data)
                counting = super().__new__(cls)
                counting.inner = real() if inner is None else inner
                return counting

            def update(self, data):
                case.counted += len(data)
                self.inner.update(data)

            def hexdigest(self):
                return self.inner.hexdigest()

            def copy(self):
                return Counting(inner=self.inner.copy())

        patch = mock.patch.object(hashlib, "sha256", Counting)
        patch.start()
        self.addCleanup(patch.stop)

    def grow(self, count=3, start=0):
        with self.source.open("ab") as handle:
            for index in range(start, start + count):
                handle.write(line(index))

    def rejected(self, code, call):
        with self.assertRaises(CaptureError) as caught:
            call()
        self.assertEqual(code, caught.exception.code)

    def damage(self, position=5):
        with self.payload.open("r+b") as handle:
            handle.seek(position)
            byte = handle.read(1)
            handle.seek(position)
            handle.write(bytes([byte[0] ^ 1]))

    def hashed(self, call):
        """The number of bytes ``call`` feeds to a running digest (the payload, the index and newly staged bytes)."""
        self.counted = 0
        call()
        return self.counted


class IncrementalVerificationTests(ArchiveCase):
    def test_receipts_are_the_same_with_and_without_remembered_proofs(self):
        other = TranscriptArchive(str(self.root / "plain"), clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
        for step in range(4):
            self.grow(start=step * 3)
            remembered = self.archive.capture(self.binding)
            PROOFS.forget()                          # the second archive verifies everything from byte zero
            plain = other.capture(self.binding)
            PROOFS.forget(((str(self.root / "plain")), "codex:conversation"))
            self.assertEqual(plain, remembered)
            self.assertEqual(self.source.read_bytes(), self.archive.read_raw(remembered, offset=0, limit=1 << 20).data)
            with self.archive.iter_records(remembered) as records:
                self.assertEqual(remembered.records, sum(1 for _ in records))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), remembered.prefix_sha256)

    def test_a_capture_and_a_read_hash_only_what_is_new(self):
        self.grow(200)
        first = self.archive.capture(self.binding)
        self.archive.read_raw(first, offset=0, limit=10)
        size = self.payload.stat().st_size
        self.assertEqual(0, self.hashed(lambda: self.archive.read_raw(first, offset=0, limit=10)))
        addition = line(999)
        with self.source.open("ab") as handle:
            handle.write(addition)
        # The capture hashes the new bytes into the receipt digest (and once more as index evidence at most).
        captured = []
        self.assertLess(self.hashed(lambda: captured.append(self.archive.capture(self.binding))), size // 4)
        second = captured[0]
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), second.prefix_sha256)
        # The first read of the new receipt hashes the appended bytes from disk; later reads hash nothing.
        self.assertEqual(len(addition), self.hashed(lambda: self.archive.read_raw(second, offset=0, limit=10)))
        self.assertEqual(0, self.hashed(lambda: self.archive.read_raw(second, offset=0, limit=10)))
        self.assertEqual(0, self.hashed(lambda: self.archive.read_raw(first, offset=0, limit=10)))

    def test_a_new_archive_object_and_an_earlier_receipt_use_the_same_proof(self):
        first = self.archive.capture(self.binding)
        self.grow()
        second = self.archive.capture(self.binding)
        again = TranscriptArchive(self.archive.archive_root)
        self.assertEqual(self.source.read_bytes()[:first.length], again.read_raw(first, offset=0, limit=1 << 20).data)
        self.assertEqual(self.source.read_bytes(), again.read_raw(second, offset=0, limit=1 << 20).data)
        self.assertEqual(0, self.hashed(lambda: again.read_raw(first, offset=0, limit=10)))

    def test_a_changed_source_prefix_is_refused_by_the_very_next_capture(self):
        self.grow()
        self.archive.capture(self.binding)
        self.archive.capture(self.binding)
        data = bytearray(self.source.read_bytes())
        data[5] ^= 1
        with self.source.open("r+b") as handle:      # same file, same size: only the comparison can notice
            handle.write(bytes(data))
        self.grow(start=10)
        self.rejected("source_prefix_changed", lambda: self.archive.capture(self.binding))

    def test_damage_to_proven_bytes_is_found_by_the_next_full_verification(self):
        self.grow()
        receipt = self.archive.capture(self.binding)
        self.archive.read_raw(receipt, offset=0, limit=10)
        self.damage()
        self.now += PROOFS.full_every - 1
        self.archive.read_raw(receipt, offset=20, limit=10)       # the proof still stands for these bytes
        self.now += 1
        self.rejected("archive_integrity", lambda: self.archive.read_raw(receipt, offset=20, limit=10))
        self.now -= 500                                           # a failed verification leaves no proof behind
        self.rejected("archive_integrity", lambda: self.archive.read_raw(receipt, offset=20, limit=10))

    def test_a_full_verification_renews_the_proof(self):
        self.grow()
        receipt = self.archive.capture(self.binding)
        self.now += PROOFS.full_every
        self.assertEqual(receipt.length, self.hashed(lambda: self.archive.read_raw(receipt, offset=0, limit=10)))
        self.now += PROOFS.full_every - 1
        self.assertEqual(0, self.hashed(lambda: self.archive.read_raw(receipt, offset=0, limit=10)))

    def test_a_final_capture_verifies_from_byte_zero(self):
        self.grow()
        self.archive.capture(self.binding)
        self.damage()
        final = FinalCaptureEvidence("agent", "codex", "conversation", 1, "process", datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.rejected("archive_integrity", lambda: self.archive.capture(self.binding, final_evidence=final))

    def test_a_final_capture_of_an_intact_archive_succeeds_and_hashes_everything(self):
        self.grow()
        first = self.archive.capture(self.binding)
        final = FinalCaptureEvidence("agent", "codex", "conversation", 1, "process", datetime(2026, 1, 2, tzinfo=timezone.utc))
        done = None

        def capture():
            nonlocal done
            done = self.archive.capture(self.binding, final_evidence=final)
        self.assertGreaterEqual(self.hashed(capture), first.length)
        self.assertEqual(final, done.final)
        self.assertEqual(first.prefix_sha256, done.prefix_sha256)

    def test_another_file_in_the_payload_s_place_is_verified_from_byte_zero(self):
        self.grow()
        receipt = self.archive.capture(self.binding)
        self.archive.read_raw(receipt, offset=0, limit=10)
        data = bytearray(self.payload.read_bytes())
        data[5] ^= 1
        replacement = self.payload.with_name("replacement")
        replacement.write_bytes(bytes(data))
        os.replace(replacement, self.payload)
        self.rejected("archive_integrity", lambda: self.archive.read_raw(receipt, offset=0, limit=10))

    def test_a_receipt_this_process_never_verified_is_verified_from_byte_zero(self):
        first = self.archive.capture(self.binding)
        self.grow()
        self.archive.capture(self.binding)
        self.grow(start=10)
        PROOFS.forget()
        third = self.archive.capture(self.binding)                # proves the two newest prefixes only
        self.archive.read_raw(third, offset=0, limit=10)
        self.damage()
        self.archive.read_raw(third, offset=0, limit=10)
        self.rejected("archive_integrity", lambda: self.archive.read_raw(first, offset=0, limit=10))

    def test_damage_to_new_bytes_is_found_at_once(self):
        self.grow()
        first = self.archive.capture(self.binding)
        self.archive.read_raw(first, offset=0, limit=10)
        self.grow(start=10)
        second = self.archive.capture(self.binding)
        self.damage(first.length + 5)
        self.rejected("archive_integrity", lambda: self.archive.read_raw(second, offset=0, limit=10))


class ProofsTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.proofs = Proofs(clock=lambda: self.now, full_every=10.0, lineages=2, marks=3)

    def state(self, data=b""):
        return hashlib.sha256(data)

    def test_resume_returns_a_private_copy(self):
        self.proofs.record("a", (1, 1), 3, self.state(b"abc"), {3: "three"})
        copy = self.proofs.resume("a", (1, 1), 3)
        copy.hasher.update(b"more")
        copy.marks[9] = "nine"
        again = self.proofs.resume("a", (1, 1), 5)
        self.assertEqual(hashlib.sha256(b"abc").hexdigest(), again.hasher.hexdigest())
        self.assertEqual({3: "three"}, again.marks)

    def test_another_file_or_a_shorter_one_drops_the_proof(self):
        self.proofs.record("a", (1, 1), 3, self.state(), {})
        self.assertIsNone(self.proofs.resume("a", (1, 2), 3))
        self.assertIsNone(self.proofs.resume("a", (1, 1), 3))
        self.proofs.record("a", (1, 1), 3, self.state(), {})
        self.assertIsNone(self.proofs.resume("a", (1, 1), 2))
        self.assertIsNone(self.proofs.resume("a", (1, 1), 3))

    def test_a_proof_expires_and_only_a_pass_from_byte_zero_renews_it(self):
        self.proofs.record("a", (1, 1), 3, self.state(), {})
        self.now = 9.0
        current = self.proofs.resume("a", (1, 1), 4)
        self.proofs.record("a", (1, 1), 4, self.state(), {4: "four"}, continued=current)
        self.assertEqual(4, self.proofs.resume("a", (1, 1), 4).length)
        self.now = 10.0
        self.assertIsNone(self.proofs.resume("a", (1, 1), 4))
        self.proofs.record("a", (1, 1), 4, self.state(), {4: "four"})
        self.now = 19.0
        self.assertEqual(4, self.proofs.resume("a", (1, 1), 4).length)

    def test_a_continuation_of_a_replaced_proof_is_not_kept(self):
        self.proofs.record("a", (1, 1), 3, self.state(), {})
        old = self.proofs.resume("a", (1, 1), 3)
        self.now = 1.0
        self.proofs.record("a", (1, 1), 3, self.state(), {})
        self.proofs.record("a", (1, 1), 8, self.state(), {8: "eight"}, continued=old)
        self.assertEqual((3, {}), (lambda proof: (proof.length, proof.marks))(self.proofs.resume("a", (1, 1), 8)))
        self.proofs.forget("a")
        self.proofs.record("a", (1, 1), 8, self.state(), {8: "eight"}, continued=old)
        self.assertIsNone(self.proofs.resume("a", (1, 1), 8))

    def test_a_shorter_pass_from_byte_zero_keeps_a_current_longer_proof(self):
        self.proofs.record("a", (1, 1), 8, self.state(b"long"), {8: "eight"})
        self.now = 5.0
        self.proofs.record("a", (1, 1), 3, self.state(b"short"), {3: "three"})
        proof = self.proofs.resume("a", (1, 1), 8)
        self.assertEqual((8, {8: "eight", 3: "three"}, 0.0), (proof.length, proof.marks, proof.verified_at))
        self.now = 10.0                              # expired: the shorter full pass is all that is proven now
        self.proofs.record("a", (1, 1), 3, self.state(b"short"), {3: "three"})
        proof = self.proofs.resume("a", (1, 1), 8)
        self.assertEqual((3, {3: "three"}, 10.0), (proof.length, proof.marks, proof.verified_at))

    def test_lineages_and_digests_are_bounded(self):
        for key in "abc":
            self.proofs.record(key, (1, 1), 1, self.state(), {})
        self.assertIsNone(self.proofs.resume("a", (1, 1), 1))
        self.assertIsNotNone(self.proofs.resume("c", (1, 1), 1))
        self.proofs.record("c", (1, 1), 9, self.state(), {mark: str(mark) for mark in range(1, 6)})
        self.assertEqual({3: "3", 4: "4", 5: "5"}, self.proofs.resume("c", (1, 1), 9).marks)

    def test_default_lifetime_is_a_quarter_of_an_hour(self):
        self.assertEqual(900.0, _proven.FULL_VERIFY_SECONDS)


if __name__ == "__main__":
    unittest.main()
