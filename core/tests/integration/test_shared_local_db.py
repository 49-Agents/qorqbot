"""Runner/capture table coexistence, using a synthetic receiver in real tmux.

No recorder is attached: readiness/composer proofs and the host ACK are fixture
evidence only. Run with unittest discovery's top-level directory set to tests.
"""

from contextlib import ExitStack
from dataclasses import asdict
import hashlib
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from qorqut_core.runner.storage import connect, ensure_schema as ensure_runner
from qorqut_core.transcripts.spool import (
    CaptureSpool, RunBinding, Source, ensure_schema as ensure_capture,
)
from runner.support import Receiver, wait_for


@unittest.skipUnless(shutil.which("tmux"), "real tmux unavailable")
class SharedLocalDatabaseTests(unittest.TestCase):
    def test_capture_and_terminal_receipts_survive_both_initialization_orders(self):
        for first in ("runner", "capture"):
            with self.subTest(first=first), ExitStack() as cleanup:
                receiver = Receiver()
                cleanup.callback(receiver.close)
                binding = receiver.binding
                run = RunBinding(
                    installation_id=str(uuid4()), agent_id=str(binding.agent_id),
                    recovery_epoch=str(binding.recovery_epoch),
                    environment_generation=binding.environment_generation,
                    boot_id=str(binding.boot_id), run_id=str(binding.run_id),
                    run_generation=binding.run_generation,
                )
                source = Source(str(uuid4()), run.installation_id, run.agent_id, "terminal")
                epoch = str(uuid4())
                source_id = source.source_id
                chunks = (b"first fixture chunk\n", b"unacknowledged suffix\n")
                total = sum(map(len, chunks))
                body = "one shared-database fixture input"
                digest = hashlib.sha256(body.encode()).hexdigest()
                wire = b"\x1b[200~" + body.encode() + b"\x1b[201~\r"
                attempt_id, control_id, viewer_id = uuid4(), uuid4(), uuid4()

                with ExitStack() as opened:
                    if first == "runner":
                        runner = receiver.runner()
                    spool = opened.enter_context(CaptureSpool(receiver.database))
                    if first == "capture":
                        runner = receiver.runner()
                    spool.register_run(run)
                    spool.register_source(source, epoch, run.run_id,
                                          capability_evidence="fixture bytes only; no recorder")
                    self.assertEqual(len(chunks[0]), spool.append(source_id, epoch, 0, chunks[0]))
                    self.assertEqual(total, spool.append(source_id, epoch, len(chunks[0]), chunks[1]))
                    self.assertEqual(chunks, tuple(c.payload for c in spool.pending(source_id, epoch).chunks))

                    receipt = runner.attempt(binding, attempt_id, body, expected_epoch=0)
                    self.assertEqual(("submitted", "finished", digest, len(body.encode())),
                                     (receipt.state, receipt.phase, receipt.payload_sha256, receipt.payload_bytes))
                    wait_for(lambda: receiver.state()["submitted"] == [digest])
                    self.assertEqual(wire, receiver.bytes())
                    # Retain nondefault control state as well as the input receipt.
                    control = runner.control(binding, control_id, action="take",
                                             expected_epoch=0, viewer_id=viewer_id)
                    self.assertEqual(("completed", "human", 1, viewer_id),
                                     (control.outcome, control.state.mode, control.state.epoch,
                                      control.state.viewer_id))

                    # Simulated host receipt, not evidence of a real durable host archive.
                    spool.acknowledge(source_id, epoch, len(chunks[0]))
                    self.assertEqual(len(chunks[0]), spool.reclaim(source_id, epoch))
                    saved_state = spool.state(source_id, epoch)
                    self.assertEqual((total, len(chunks[0])),
                                     (saved_state["local_captured_offset"], saved_state["host_archived_offset"]))

                # TerminalRunner has short-lived connections; reconstruct both owners.
                runner = receiver.runner()
                with CaptureSpool(receiver.database) as spool, connect(receiver.database) as runner_db:
                    capture_db = spool._connection  # Verify the actual owning connection's pragmas.
                    for db in (runner_db, capture_db):
                        self.assertEqual(receiver.database.resolve(),
                                         Path(db.execute("PRAGMA database_list").fetchone()[2]).resolve())
                        self.assertEqual("wal", db.execute("PRAGMA journal_mode").fetchone()[0])
                        self.assertEqual(2, db.execute("PRAGMA synchronous").fetchone()[0])
                        self.assertEqual(1, db.execute("PRAGMA foreign_keys").fetchone()[0])
                        self.assertEqual(0, db.execute("PRAGMA user_version").fetchone()[0])
                    self.assertEqual(5000, runner_db.execute("PRAGMA busy_timeout").fetchone()[0])
                    self.assertEqual(2000, capture_db.execute("PRAGMA busy_timeout").fetchone()[0])

                    before = list(capture_db.iterdump())
                    for initialize, db in ((ensure_runner, runner_db), (ensure_capture, capture_db)):
                        initialize(db)
                    self.assertEqual(before, list(capture_db.iterdump()))
                    for db in (runner_db, capture_db):
                        self.assertEqual("wal", db.execute("PRAGMA journal_mode").fetchone()[0])
                        self.assertEqual(2, db.execute("PRAGMA synchronous").fetchone()[0])
                        self.assertEqual(1, db.execute("PRAGMA foreign_keys").fetchone()[0])
                        self.assertEqual(0, db.execute("PRAGMA user_version").fetchone()[0])
                        self.assertEqual([("ok",)], [tuple(r) for r in db.execute("PRAGMA integrity_check")])
                        self.assertEqual([], db.execute("PRAGMA foreign_key_check").fetchall())

                    saved_run = dict(capture_db.execute("SELECT * FROM capture_runs").fetchone())
                    self.assertEqual(asdict(run), {key: saved_run[key] for key in asdict(run)})
                    saved_source = dict(capture_db.execute("SELECT * FROM capture_sources").fetchone())
                    self.assertEqual(asdict(source), {key: saved_source[key] for key in asdict(source)})
                    self.assertEqual(epoch, saved_source["current_epoch"])
                    self.assertEqual([(run.run_id, source_id, epoch)], [tuple(r) for r in capture_db.execute(
                        "SELECT run_id,source_id,source_epoch FROM capture_run_sources")])
                    expected_chunks = [
                        (source_id, epoch, 0, len(chunks[0]), hashlib.sha256(chunks[0]).hexdigest(), None),
                        (source_id, epoch, len(chunks[0]), len(chunks[1]), hashlib.sha256(chunks[1]).hexdigest(), chunks[1]),
                    ]
                    self.assertEqual(expected_chunks, [tuple(r) for r in capture_db.execute(
                        "SELECT source_id,source_epoch,offset,length,sha256,payload FROM capture_chunks ORDER BY offset")])
                    self.assertEqual(saved_state, spool.state(source_id, epoch))
                    pending = spool.pending(source_id, epoch)
                    self.assertEqual((chunks[1],), tuple(c.payload for c in pending.chunks))
                    self.assertEqual(total, pending.next_offset)
                    self.assertEqual(total, spool.append(source_id, epoch, 0, chunks[0]))
                    self.assertEqual(0, spool.reclaim(source_id, epoch))
                    self.assertEqual(before, list(capture_db.iterdump()))

                    self.assertEqual(binding, runner.inspect_binding(binding.run_id))
                    self.assertEqual(control.state, runner.inspect_control(binding))
                    self.assertEqual(control, runner.control(binding, control_id, action="take",
                                                            expected_epoch=0, viewer_id=viewer_id))
                    self.assertEqual(receipt, runner.inspect_attempt(attempt_id))
                    self.assertEqual(receipt, runner.attempt(binding, attempt_id, body, expected_epoch=0))
                    self.assertEqual(wire, receiver.bytes())
                    self.assertEqual((1, [digest]),
                                     (receiver.state()["pastes"], receiver.state()["submitted"]))
                    self.assertEqual(before, list(capture_db.iterdump()))
                    self.assertEqual(b"", receiver.command_run("list-buffers", "-F", "#{buffer_name}"))
