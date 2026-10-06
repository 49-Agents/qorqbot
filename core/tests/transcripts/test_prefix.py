from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import errno
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import traceback
import unittest
from unittest.mock import patch

from qorqut_core.transcripts.prefix import FilePrefixChanged, verify_file_prefix
from qorqut_core.transcripts.spool import (
    CHUNK_BYTES, MAX_OFFSET, BindingConflict, CaptureBlocked, CaptureSpool,
    ChunkReceipt, CoordinateConflict,
)
from support import new_id, registered


class PrefixTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "spool.sqlite3"
        self.spool, self.source, self.epoch, self.run = registered(self.path)
        self.addCleanup(self.spool.close)
        self.sid = self.source.source_id

    def append(self, data):
        offset = self.spool.state(self.sid, self.epoch)["local_captured_offset"]
        return self.spool.append(self.sid, self.epoch, offset, data)

    def file(self, data, name="native.bin"):
        path = self.root / name
        path.write_bytes(data)
        stream = path.open("r+b", buffering=0)
        self.addCleanup(stream.close)
        stream.seek(19)
        return stream

    @contextmanager
    def unchanged(self):
        connection = self.spool._connection
        before = tuple(connection.iterdump()), connection.total_changes, self.spool.last_error
        # Enforce read-only SQL, including on failure; compare all durable rows
        # as well as the connection's write count and in-memory health latch.
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                   sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION}
        connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK
                                  if action in allowed else sqlite3.SQLITE_DENY)
        try:
            yield
        finally:
            connection.set_authorizer(None)
            self.assertFalse(connection.in_transaction)
            self.assertEqual((tuple(connection.iterdump()), connection.total_changes,
                              self.spool.last_error), before)

    def verify(self, stream, **kwargs):
        fd = stream.fileno()
        initial, position = os.fstat(fd), stream.tell()
        args = dict(expected_device=initial.st_dev, expected_inode=initial.st_ino, **kwargs)
        try:
            return verify_file_prefix(self.spool, self.sid, self.epoch, fd, **args)
        finally:
            self.assertEqual(stream.tell(), position)
            current = os.fstat(fd)
            self.assertEqual((current.st_dev, current.st_ino), (initial.st_dev, initial.st_ino))

    def test_receipt_snapshot_boundaries_and_retired_health(self):
        for part in (b"first", b"second", b"third"):
            self.append(part)
        with CaptureSpool(self.path) as writer:
            real_epoch = self.spool._epoch

            def interleave(sid, epoch):
                row = real_epoch(sid, epoch)
                self.assertTrue(self.spool._connection.in_transaction)
                writer.append(sid, epoch, 16, b"later")
                writer.acknowledge(sid, epoch, 21)
                self.assertEqual(writer.reclaim(sid, epoch), 21)
                return row

            with patch.object(self.spool, "_epoch", side_effect=interleave):
                page = self.spool.receipts(self.sid, self.epoch, limit=1)
            self.assertEqual(tuple(self.spool._connection.iterdump()),
                             tuple(writer._connection.iterdump()))
        self.assertEqual((page.source, page.source_epoch, page.through_offset, page.next_offset),
                         (self.source, self.epoch, 16, 5))
        self.assertEqual(page.receipts, (ChunkReceipt(0, 5, hashlib.sha256(b"first").hexdigest()),))
        for value, field in ((page, "next_offset"), (page.source, "source_type"),
                             (page.receipts[0], "length")):
            with self.assertRaises(FrozenInstanceError):
                setattr(value, field, None)
        successor = new_id()
        self.spool.begin_epoch(self.sid, successor, previous_epoch=self.epoch,
                               reason="synthetic replacement", run_id=self.run.run_id,
                               capability_evidence="synthetic successor")
        self.spool.block(self.sid, self.epoch, "historically blocked")
        with self.unchanged():
            rest = self.spool.receipts(self.sid, self.epoch, after=5,
                                       through=page.through_offset)
            self.assertEqual([(r.offset, r.length) for r in rest.receipts], [(5, 6), (11, 5)])
            self.assertEqual((rest.next_offset, rest.through_offset), (16, 16))
            self.assertEqual(self.spool.receipts(self.sid, self.epoch).through_offset, 21)
            for end in (0, 5, 16, 21):
                done = self.spool.receipts(self.sid, self.epoch, after=end, through=end)
                self.assertEqual((done.receipts, done.next_offset, done.through_offset), ((), end, end))
            self.assertEqual(self.spool.receipts(self.sid, successor).receipts, ())

    def test_argument_and_lineage_errors_are_read_only(self):
        self.append(b"first")
        with self.unchanged():
            for kwargs in ({"after": True}, {"after": -1}, {"after": MAX_OFFSET + 1},
                           {"through": 1.5}, {"through": True}, {"through": 6},
                           {"after": 5, "through": 0}, {"limit": True},
                           {"limit": 0}, {"limit": 17}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    self.spool.receipts(self.sid, self.epoch, **kwargs)
            for kwargs in ({"after": 2}, {"through": 2}, {"after": 2, "through": 2}):
                with self.subTest(kwargs=kwargs), self.assertRaises(CoordinateConflict):
                    self.spool.receipts(self.sid, self.epoch, **kwargs)
            for sid, epoch in ((new_id(), self.epoch), (self.sid, new_id())):
                with self.assertRaises(BindingConflict):
                    self.spool.receipts(sid, epoch)
            for sid, epoch in (("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", self.epoch),
                               (self.sid, "not-an-epoch")):
                with self.assertRaises(ValueError):
                    self.spool.receipts(sid, epoch)

    def test_corrupt_or_missing_receipts_fail_without_latching(self):
        for part in (b"one", b"two", b"end"):
            self.append(part)
        self.spool.acknowledge(self.sid, self.epoch, 9)
        self.spool.reclaim(self.sid, self.epoch)
        stream = self.file(b"onetwoend")
        connection = self.spool._connection
        rows = connection.execute("SELECT * FROM capture_chunks ORDER BY offset").fetchall()
        faults = (
            "DELETE FROM capture_chunks WHERE offset=3",
            "DELETE FROM capture_chunks WHERE offset=6",
            "UPDATE capture_chunks SET length=1.5 WHERE offset=3",
            f"UPDATE capture_chunks SET length={CHUNK_BYTES + 1} WHERE offset=3",
            "UPDATE capture_chunks SET sha256=upper(sha256) WHERE offset=3",
        )
        for sql in faults:
            with self.subTest(sql=sql):
                connection.execute("PRAGMA ignore_check_constraints=ON")
                connection.execute(sql)
                connection.execute("PRAGMA ignore_check_constraints=OFF")
                with self.unchanged():
                    with self.assertRaises(CaptureBlocked):
                        self.spool.receipts(self.sid, self.epoch)
                    with self.assertRaises(CaptureBlocked):
                        self.verify(stream)
                    if sql.endswith("offset=6"):
                        # A saved cursor without its ending receipt also blocks
                        # an empty/earlier selection, without a history scan.
                        with self.assertRaises(CaptureBlocked):
                            self.spool.receipts(self.sid, self.epoch, through=0)
                connection.execute("DELETE FROM capture_chunks")
                connection.executemany("INSERT INTO capture_chunks VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        # A page limit cannot silently skip the gap on the following page.
        connection.execute("DELETE FROM capture_chunks WHERE offset=3")
        with self.unchanged():
            page = self.spool.receipts(self.sid, self.epoch, limit=1)
            self.assertEqual(page.next_offset, 3)
            with self.assertRaises(CaptureBlocked):
                self.spool.receipts(self.sid, self.epoch, after=3, through=page.through_offset)

    def test_large_reclaimed_prefix_keeps_original_boundaries_and_bounded_reads(self):
        parts = (bytes(range(256)) * 37,
                 b'{"giant":"' + b"x" * (9 * 1024 * 1024 + 123) + b'"}\n',
                 b'{"partial":"\xf0\x9f')
        data = b"".join(parts)
        expected, offset = [], 0
        for part in parts:
            self.append(part)
            for start in range(0, len(part), CHUNK_BYTES):
                chunk = part[start:start + CHUNK_BYTES]
                expected.append(ChunkReceipt(offset, len(chunk), hashlib.sha256(chunk).hexdigest()))
                offset += len(chunk)
        self.assertGreater(offset, 8 * 1024 * 1024)
        stream = self.file(data)
        self.spool.acknowledge(self.sid, self.epoch, offset)
        self.assertEqual(self.spool.reclaim(self.sid, self.epoch), offset)
        self.spool.close()
        self.spool = CaptureSpool(self.path)
        self.addCleanup(self.spool.close)
        self.assertEqual(self.spool._connection.execute(
            "SELECT count(*) FROM capture_chunks WHERE payload IS NOT NULL").fetchone()[0], 0)
        seen, reads = [], []
        real_page, real_read, real_stat = self.spool.receipts, os.pread, os.fstat

        def page(*args, **kwargs):
            result = real_page(*args, **kwargs)
            self.assertLessEqual(len(result.receipts), 16)
            self.assertEqual(result.through_offset, len(data))
            seen.extend(result.receipts)
            return result

        def read(fd, count, position):
            self.assertFalse(self.spool._connection.in_transaction)
            self.assertEqual(fd, stream.fileno())
            self.assertLessEqual(count, 65536)
            self.assertEqual(position, sum(reads))
            result = real_read(fd, count, position)
            reads.append(len(result))
            return result

        def observe(fd):
            self.assertFalse(self.spool._connection.in_transaction)
            return real_stat(fd)

        with self.unchanged(), patch.object(self.spool, "receipts", side_effect=page), \
                patch("qorqut_core.transcripts.prefix.os.pread", side_effect=read), \
                patch("qorqut_core.transcripts.prefix.os.fstat", side_effect=observe):
            proof = self.verify(stream)
        self.assertEqual(seen, expected)
        self.assertEqual(sum(reads), len(data))
        self.assertEqual((proof.source_id, proof.source_epoch, proof.through_offset),
                         (self.sid, self.epoch, len(data)))
        observed = os.fstat(stream.fileno())
        self.assertEqual((proof.device, proof.inode, proof.file_size, proof.mtime_ns, proof.ctime_ns),
                         (observed.st_dev, observed.st_ino, len(data), observed.st_mtime_ns, observed.st_ctime_ns))
        self.assertEqual(proof.prefix_sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual((proof.tail_offset, proof.tail_length), (len(data) - 65536, 65536))
        self.assertEqual(proof.tail_sha256, hashlib.sha256(data[-65536:]).hexdigest())
        with self.assertRaises(FrozenInstanceError):
            proof.through_offset = 0

    def test_rewrite_truncation_identity_and_read_failures(self):
        data = b"x" * (2 * 65536 + 31)
        self.append(data)
        stream = self.file(data)
        fd = stream.fileno()
        with self.unchanged():
            os.pwrite(fd, b"z", 0)
            self.assertEqual(os.pread(fd, 65536, len(data) - 65536), data[-65536:])
            with self.assertRaises(FilePrefixChanged) as failure:
                self.verify(stream)
            self.assertEqual(failure.exception.reason, "prefix_mismatch")
            with self.assertRaises(AttributeError):
                failure.exception.reason = "identity_changed"
            os.pwrite(fd, b"x", 0)
            os.ftruncate(fd, len(data) - 1)
            with self.assertRaises(FilePrefixChanged) as failure:
                self.verify(stream)
            self.assertEqual(failure.exception.reason, "prefix_shorter")
            os.pwrite(fd, b"x", len(data) - 1)
            initial, position = os.fstat(fd), stream.tell()
            for inode in (initial.st_ino + 1, 2**80):
                with self.assertRaises(FilePrefixChanged) as failure:
                    verify_file_prefix(self.spool, self.sid, self.epoch, fd,
                                       expected_device=initial.st_dev, expected_inode=inode)
                self.assertEqual(failure.exception.reason, "identity_changed")
                self.assertEqual(stream.tell(), position)
                self.assertEqual(os.fstat(fd).st_ino, initial.st_ino)
            with patch("qorqut_core.transcripts.prefix.os.pread", return_value=b""):
                with self.assertRaises(FilePrefixChanged) as failure:
                    self.verify(stream)
                self.assertEqual(failure.exception.reason, "prefix_shorter")
            with patch("qorqut_core.transcripts.prefix.os.pread",
                       side_effect=OSError(errno.EIO, "synthetic private path/content")):
                with self.assertRaises(CaptureBlocked) as failure:
                    self.verify(stream)
                self.assertEqual(str(failure.exception), "file prefix read failed")
                self.assertNotIn("synthetic private", "".join(traceback.format_exception(failure.exception)))

    def test_descriptor_validation_and_native_declaration(self):
        stream = self.file(b"")
        fd, initial = stream.fileno(), os.fstat(stream.fileno())
        args = dict(expected_device=initial.st_dev, expected_inode=initial.st_ino)
        with self.unchanged():
            for bad in (-1, True, 1.5):
                for field in ("fd", "expected_device", "expected_inode"):
                    supplied = {"fd": fd, **args, field: bad}
                    with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                        verify_file_prefix(self.spool, self.sid, self.epoch, **supplied)
            for kind in ("terminal", "attachment"):
                other, source, epoch, _ = registered(self.root / f"{kind}.sqlite3", kind=kind)
                with other, patch("qorqut_core.transcripts.prefix.os.fstat") as observe:
                    with self.assertRaises(BindingConflict):
                        verify_file_prefix(other, source.source_id, epoch, fd, **args)
                    observe.assert_not_called()
            read_fd, write_fd = os.pipe()
            try:
                with self.assertRaises(CaptureBlocked):
                    verify_file_prefix(self.spool, self.sid, self.epoch, read_fd, **args)
                os.fstat(read_fd)  # Borrowed nonregular descriptor is still open.
            finally:
                os.close(read_fd)
                os.close(write_fd)
            closed = os.dup(fd)
            os.close(closed)
            with self.assertRaises(CaptureBlocked):
                verify_file_prefix(self.spool, self.sid, self.epoch, closed, **args)
            self.assertEqual(stream.tell(), 19)
            self.assertEqual(os.fstat(fd).st_ino, initial.st_ino)

    def test_empty_short_reads_suffix_and_concurrent_spool_append(self):
        stream = self.file(b"preexisting suffix")
        with self.unchanged():
            empty = self.verify(stream)
            self.assertEqual((empty.through_offset, empty.tail_offset, empty.tail_length), (0, 0, 0))
            self.assertEqual((empty.prefix_sha256, empty.tail_sha256),
                             (hashlib.sha256(b"").hexdigest(),) * 2)
            self.assertEqual(empty.file_size, len(b"preexisting suffix"))
        data, suffix = b"abcdefgh" * 17, b"uncaptured suffix"
        for start in range(0, len(data), 8):
            self.append(data[start:start + 8])
        os.pwrite(stream.fileno(), data + suffix, 0)
        real_read, total, calls = os.pread, 0, 0
        with CaptureSpool(self.path) as writer:
            def read(fd, count, position):
                nonlocal total, calls
                self.assertFalse(self.spool._connection.in_transaction)
                self.assertFalse(writer._connection.in_transaction)
                self.assertEqual(position, total)
                self.assertLessEqual(position + count, len(data))
                if calls == 0:
                    writer.append(self.sid, self.epoch, len(data), suffix)
                calls += 1
                result = real_read(fd, min(3, count), position)
                total += len(result)
                return result

            changes = self.spool._connection.total_changes
            with patch("qorqut_core.transcripts.prefix.os.pread", side_effect=read):
                proof = self.verify(stream)
            self.assertEqual(self.spool._connection.total_changes, changes)
            self.assertEqual(tuple(self.spool._connection.iterdump()),
                             tuple(writer._connection.iterdump()))
        self.assertEqual((proof.through_offset, total), (len(data), len(data)))
        self.assertEqual(proof.file_size, len(data + suffix))
        self.assertEqual(proof.prefix_sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual((proof.tail_offset, proof.tail_length, proof.tail_sha256),
                         (0, len(data), proof.prefix_sha256))
        self.assertGreater(calls, 17)
        self.assertEqual(self.spool.state(self.sid, self.epoch)["local_captured_offset"], len(data + suffix))
        with self.unchanged():
            earlier = self.verify(stream, through=16)
            self.assertEqual((earlier.through_offset, earlier.prefix_sha256),
                             (16, hashlib.sha256(data[:16]).hexdigest()))

    def test_held_renamed_unlinked_file_and_replacement_identity(self):
        data = b"held original"
        self.append(data)
        stream = self.file(data)
        original = os.fstat(stream.fileno())
        path, renamed = self.root / "native.bin", self.root / "renamed.bin"
        path.rename(renamed)
        replacement = self.file(data)
        with self.unchanged():
            self.assertEqual(self.verify(stream).inode, original.st_ino)
            renamed.unlink()
            self.assertEqual(self.verify(stream).prefix_sha256, hashlib.sha256(data).hexdigest())
            with self.assertRaises(FilePrefixChanged) as failure:
                verify_file_prefix(self.spool, self.sid, self.epoch, replacement.fileno(),
                                   expected_device=original.st_dev, expected_inode=original.st_ino)
            self.assertEqual(failure.exception.reason, "identity_changed")
            self.assertEqual(replacement.tell(), 19)
            self.assertNotEqual(os.fstat(replacement.fileno()).st_ino, original.st_ino)

    def test_final_stat_rejects_growth_and_suffix_mutation_during_reads(self):
        data, suffix = b"captured", b"suffix"
        self.append(data)
        real_read = os.pread
        for mutation in ("growth", "same_size_suffix"):
            with self.subTest(mutation=mutation):
                stream = self.file(data + suffix, mutation)
                initial = os.fstat(stream.fileno())

                def read(fd, count, offset):
                    result = real_read(fd, count, offset)
                    if mutation == "growth":
                        os.pwrite(fd, b"growth", initial.st_size)
                    else:
                        os.pwrite(fd, b"change", len(data))
                        os.utime(fd, ns=(initial.st_atime_ns, initial.st_mtime_ns + 1_000_000_000))
                    return result

                with self.unchanged(), patch("qorqut_core.transcripts.prefix.os.pread", side_effect=read):
                    with self.assertRaises(FilePrefixChanged) as failure:
                        self.verify(stream)
                    self.assertEqual(failure.exception.reason, "changed_during_read")


if __name__ == "__main__":
    unittest.main()
