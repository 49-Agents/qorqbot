"""One bounded raw transfer flow using the actual accepted capture spool."""

from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from qorqut_core.contracts import CoreError
from qorqut_core.transcripts.spool import CaptureSpool, CHUNK_BYTES
from test_foundation import InjectedCrash
from .support import ArchiveCase


class TransferTests(ArchiveCase):
    def test_pending_receive_ack_reclaim_restart_retains_large_exact_raw_stream(self):
        # Use a real initialized VM-kind registration; archival never launches it.
        result = self.create(start=False)
        self.agent = next(row for row in self.rows("agents") if row["id"] == result.data["agent"]["id"])
        self.assertEqual(str(self.app.jobs.run_next(worker_id=uuid4())), result.data["provisioning_job_id"])
        self.binding = replace(self.binding, agent_id=self.agent["id"])
        self.source = replace(self.source, agent_id=self.agent["id"])
        self.register()
        runner_path = self.root / "runner.sqlite3"
        giant = b'{"giant":"' + b"g" * (9 * 1024 * 1024) + b'"}\n'
        payload = bytes(range(256)) + "Unicode: türkmençe 日本語 🌿\n".encode() + giant + b'{"incomplete":"\xe2\x82'
        self.assertGreater(len(payload), 8 * 1024 * 1024)
        with CaptureSpool(runner_path) as spool:
            spool.register_run(self.binding)
            spool.register_source(self.source, self.epoch, self.binding.run_id,
                                  **self.metadata.model_dump(exclude={"previous_epoch", "divergence_reason"}))
            self.assertNotEqual(spool._connection.execute("PRAGMA database_list").fetchone()[2], str(self.app.db.path))
            spool.append(self.source.source_id, self.epoch, 0, payload)
            first_page = spool.pending(self.source.source_id, self.epoch)
            # A fetched page or saved cursor is neither host intake nor ACK.
            spool.pending(self.source.source_id, self.epoch, after=first_page.next_offset)
            self.assertEqual(spool.state(self.source.source_id, self.epoch)["host_archived_offset"], 0)
            self.assertEqual(self.state().host_archived_offset, 0)
            self.assertEqual(spool.reclaim(self.source.source_id, self.epoch), 0)
            # SQL committed, caller lost response: no returned ACK is applied.
            self.fault.point = "archive_after_receipt_commit"
            with self.assertRaises(InjectedCrash):
                self.receive(first_page.chunks[0])
            self.assertEqual(spool.state(self.source.source_id, self.epoch)["host_archived_offset"], 0)
            # Drop the first successfully returned ACK as well, then restart host.
            self.receive(first_page.chunks[0])
            self.restart()
            reclaimed = 0
            coordinates = []
            before = self.counts()
            while (page := spool.pending(self.source.source_id, self.epoch, limit=4)).chunks:
                for chunk in page.chunks:
                    self.assertLessEqual(chunk.length, CHUNK_BYTES)
                    # receive itself must never invoke runner ACK or reclamation.
                    with patch.object(CaptureSpool, "acknowledge", side_effect=AssertionError("implicit ACK")), \
                            patch.object(CaptureSpool, "reclaim", side_effect=AssertionError("implicit reclaim")):
                        receipt = self.receive(chunk)
                    self.assertEqual((receipt.binding, receipt.source_id, receipt.source_epoch),
                                     (self.binding, self.source.source_id, self.epoch))
                    spool.acknowledge(receipt.source_id, receipt.source_epoch, receipt.host_archived_offset)
                    reclaimed += spool.reclaim(receipt.source_id, receipt.source_epoch)
                    coordinates.append(chunk.offset)
            self.assertEqual(reclaimed, len(payload))
            self.assertEqual(self.counts(), before)
        self.restart()
        with CaptureSpool(runner_path) as restarted:
            self.assertEqual(restarted.pending(self.source.source_id, self.epoch).chunks, ())
            self.assertEqual(restarted._connection.execute("SELECT count(*) FROM capture_chunks WHERE payload IS NOT NULL").fetchone()[0], 0)
            self.assertEqual(restarted.state(self.source.source_id, self.epoch)["host_archived_offset"], len(payload))
        self.assertEqual(b"".join(self.read(offset).payload for offset in coordinates), payload)
        self.assertEqual(self.state().host_archived_offset, len(payload))
        self.assertEqual(self.get(self.agent["id"])["capture_health"], "unknown")
        self.assertEqual(self.get(self.agent["id"])["lifecycle"], "provisioning")
