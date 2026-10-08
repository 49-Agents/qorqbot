"""D40 service-level composition; synthetic records and positively owned children.

Explicit job pumping below checks publication bytes, not normal-server acceptance.
No native executable, model provider, personal HOME or existing session is used.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from pydantic import ValidationError

from adapters.test_native_records import claude, codex, raw
from messaging.support import ChatCase
from runtime.support import RuntimeCase
from qorqut_core.adapters.capabilities import VERSIONS
from qorqut_core.adapters.discovery import discover
from qorqut_core.adapters.manual_hooks import NativeProgram
from qorqut_core.app import Application
from qorqut_core.config import LocalTerminalSettings
from qorqut_core.contracts import CoreError, Invocation
from qorqut_core.runtime.native_collector import NativeCollector
from qorqut_core.storage.database import assert_external_io_allowed
from qorqut_core.transcripts.native_capture import NativeCaptureError
from test_foundation import InjectedCrash


class NativeCollection(RuntimeCase):
    pump = ChatCase.pump

    def setUp(self):
        super().setUp()
        # The existing fixture owns storage/jobs; this test adds the frozen D40
        # configuration in a fresh installation, without starting its terminals.
        self.app.close()
        programs = tuple(NativeProgram(provider=p, cli_version=v, executable=self.root / ("unused-" + p))
                         for p, v in VERSIONS.items())
        local = LocalTerminalSettings(runtime_root=Path("/tmp") / uuid4().hex, python_executable=Path(sys.executable),
            tmux_executable=self.root / "unused-tmux", bash_executable=Path("/usr/bin/bash"),
            path=os.defpath, native_programs=programs)
        self.settings = self.settings.model_copy(update={"data_root": self.root / "configured", "local_terminal": local})
        self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock, fault=self.fault)
        self.pump()
        self.host = next(row for row in self.rows("agents") if row["kind"] == "host")
        self.base = self.settings.data_root / "agents" / self.host["id"]
        self.home = self.base / "runtime/home"
        self.home.mkdir(mode=0o700)
        self.elapsed = 100.0
        timer = patch("qorqut_core.runtime.native_collector.time", SimpleNamespace(monotonic=lambda: self.elapsed))
        timer.start()
        self.addCleanup(timer.stop)
        self.collector = NativeCollector(self.app)
        self.child_process = None

    def tearDown(self):
        self.collector.close()
        super().tearDown()

    def bind(self, provider="codex", session=None):
        if self.child_process is not None:
            self.exit_child(self.child_process)
            self.app.session_owners.reconcile()
        child, process = self.child()
        self.child_process = child
        candidate = self.candidate(process).model_copy(update={"provider": provider,
            "cli_version": VERSIONS[provider], "native_session_id": session or uuid4()})
        owner = self.app.session_owners.connect(candidate)
        return candidate, owner

    def source(self, candidate, data):
        session = str(candidate.native_session_id)
        relative = (f".claude/projects/synthetic/{session}.jsonl" if candidate.provider == "claude"
                    else f".codex/sessions/2026/10/08/rollout-2026-10-08T12-00-00-{session}.jsonl")
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def record(self, candidate, text, *, assistant=False):
        session = str(candidate.native_session_id)
        if candidate.provider == "claude":
            return raw(claude(text, sessionId=session, role="assistant" if assistant else "user"))
        record = codex(text, role="AgentMessage" if assistant else "UserMessage")
        record["payload"]["thread_id"] = session
        return raw(record)

    def header(self, candidate):
        return (raw({"type": "session_meta", "payload": {"id": str(candidate.native_session_id), "source": "cli"}})
                if candidate.provider == "codex" else b"")

    def tick(self):
        self.elapsed += 1.01
        self.clock.advance(1)
        self.collector.tick()
        return self.collector.view(self.host["id"])

    def reopen(self):
        self.collector.close()
        self.restart()
        self.collector = NativeCollector(self.app)

    def capture_states(self):
        return self.collector._capture.list_states()

    def entries(self):
        return self.app.invoke("conversation_list", self.context, {"agent_id": self.host["id"]}).data["items"]

    def archived(self, state):
        context = Invocation(actor_id=self.host["actor_id"])
        chunks = [row for row in self.rows("archive_chunks")
                  if (row["source_id"], row["source_epoch"]) == (state.source_id, state.source_epoch)]
        result, offset = b"", 0
        for row in sorted(chunks, key=lambda r: r["offset"]):
            chunk = self.app.archive.read_chunk(context, state.source_id, state.source_epoch, row["offset"])
            stored = (self.settings.data_root / row["object_path"]).read_bytes()
            self.assertEqual((row["offset"], row["length"], row["sha256"]),
                             (offset, len(stored), hashlib.sha256(stored).hexdigest()))
            self.assertEqual(chunk.payload, stored)
            self.assertEqual(row["received_by"], self.host["actor_id"])
            offset += len(stored)
            result += stored
        return result

    def local(self, table):
        with sqlite3.connect(self.base / "runtime/native.sqlite3") as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM " + table)]

    def published(self):
        self.pump()  # Explicit service-level job pumping, not a running server.
        path = self.base / "transcripts" / (self.clock()[:10] + ".jsonl")
        return [json.loads(line) for line in path.read_bytes().splitlines()]

    def test_both_providers_delayed_append_partial_reopen_exit_resume_and_publication(self):
        with patch.object(self.app.db, "read", side_effect=AssertionError("passive construction/view")):
            self.assertEqual(self.collector.view(self.host["id"]).reason, "observation_stale")
            other = NativeCollector(self.app)
            other.close()
        self.assertFalse((self.base / "runtime/native.sqlite3").exists())
        self.assertEqual(self.tick().reason, "waiting_session")
        expected_text = []
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                candidate, owner = self.bind(provider)
                with patch("qorqut_core.runtime.native_collector.discover", wraps=discover) as discovery:
                    self.assertEqual(self.tick().reason, "waiting_source")
                    self.collector.tick()
                    self.assertEqual(discovery.call_count, 1)  # One missing retry per second/owner.
                first = self.header(candidate) + self.record(candidate, provider + " user 文🙂\n")
                path = self.source(candidate, first)
                self.assertEqual(self.tick().source_count, len(expected_text) // 4 + 1)
                state = next(s for s in self.capture_states() if s.native_session_id == str(candidate.native_session_id))
                self.assertEqual((self.base / "runtime/native.sqlite3").stat().st_mode & 0o077, 0)
                self.assertEqual(self.archived(state), first)
                tail = self.record(candidate, provider + " reply 文🙂", assistant=True)
                split = tail.index("🙂".encode()) + 2
                with path.open("ab") as stream:
                    stream.write(tail[:split])
                with patch("qorqut_core.runtime.native_collector.discover", side_effect=AssertionError("append rediscovery")):
                    view = self.tick()
                self.assertEqual((view.pending_archive_bytes, view.pending_parse_bytes), (0, split))
                self.assertEqual(self.archived(state), first + tail[:split])
                self.reopen()
                with path.open("ab") as stream:
                    stream.write(tail[split:])
                with patch("qorqut_core.runtime.native_collector.discover", side_effect=AssertionError("restart rediscovery")):
                    self.assertEqual(self.tick().pending_parse_bytes, 0)
                self.exit_child(self.child_process)
                self.app.session_owners.reconcile()
                late = self.record(candidate, provider + " late", assistant=True)
                with path.open("ab") as stream:
                    stream.write(late)
                self.tick()  # Closed admitted owner remains collectible.
                before = self.entries()
                resumed, new_owner = self.bind(provider, candidate.native_session_id)
                self.assertNotEqual(owner.owner_id, new_owner.owner_id)
                self.tick()
                self.assertEqual(self.entries(), before)
                final = self.record(resumed, provider + " resumed", assistant=True)
                with path.open("ab") as stream:
                    stream.write(final)
                self.assertEqual(self.tick().pending_parse_bytes, 0)
                state = next(s for s in self.capture_states() if s.native_session_id == str(candidate.native_session_id))
                self.assertEqual([r.run_id for r in state.runs], [str(owner.run_id), str(new_owner.run_id)])
                self.assertEqual(self.archived(state), first + tail + late + final)
                self.assertEqual(state.host_archived_offset, len(first + tail + late + final))
                self.assertEqual(self.collector._capture.spool.receipts(state.source_id, state.source_epoch).through_offset,
                                 state.host_archived_offset)
                links = [r for r in self.rows("archive_run_sources") if r["source_id"] == state.source_id]
                self.assertEqual([r["run_id"] for r in links], [str(owner.run_id), str(new_owner.run_id)])
                self.assertTrue(all(e["source"]["run_id"] == str(owner.run_id) for e in self.entries()
                                    if e["source"]["source_id"] == state.source_id))
                expected_text.extend([provider + " user 文🙂\n", provider + " reply 文🙂",
                                      provider + " late", provider + " resumed"])
                published = self.published()
                self.assertEqual([e["text"] for e in published], expected_text)
                self.assertEqual([e["entry_id"] for e in published], [e["entry_id"] for e in self.entries()])
                self.assertTrue(all(e["format_version"] == 1 for e in published))
                self.assertTrue(all(r["actor_id"] == self.host["actor_id"] for r in self.rows("operations")
                                    if r["name"] == "native_entry_import"))
        capture = self.collector._capture
        changes = capture._catalog.total_changes
        self.assertEqual(len(capture.list_states()), 2)
        self.assertEqual(capture._catalog.total_changes, changes)
        view = self.collector.view(self.host["id"])
        with patch.object(self.app.db, "read", side_effect=AssertionError("view SQL")), \
                patch.object(capture, "list_states", side_effect=AssertionError("view capture I/O")), ThreadPoolExecutor(1) as pool:
            self.assertEqual(pool.submit(self.collector.view, UUID(self.host["id"])).result(), view)
            self.assertEqual(self.collector.view(uuid4()).reason, "unsupported_target")
            with self.assertRaises(ValidationError):
                view.source_count = 99
            self.elapsed += 5.01
            self.assertEqual(self.collector.view(self.host["id"]).reason, "observation_stale")
        self.assertEqual((len(self.app.registry), len(self.app.jobs.handlers)), (17, 5))
        with self.app.db.read() as tx:
            self.assertEqual(tx.connection.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_lost_receipt_recovery_retains_bytes_intent_and_honest_lag(self):
        candidate, _ = self.bind()
        payload = self.header(candidate) + self.record(candidate, "retained after recovery")
        self.source(candidate, payload)
        self.fault.point = "archive_after_receipt_commit"
        with self.assertRaises(InjectedCrash):  # True service failure reaches the monitor.
            self.tick()
        state = self.capture_states()[0]
        self.assertEqual(state.host_archived_offset, 0)
        self.assertEqual(self.archived(state), payload)
        intent = self.local("native_transfers")
        retained = self.local("capture_chunks")
        self.assertEqual(b"".join(r["payload"] for r in retained), payload)
        self.assertEqual(len(intent), 1)
        self.assertIsNone(intent[0]["receipt"])
        self.assertTrue(self.recovery().ok)
        self.reopen()
        with patch("qorqut_core.transcripts.spool.CaptureSpool.acknowledge", side_effect=AssertionError("held ACK")), \
                patch("qorqut_core.transcripts.spool.CaptureSpool.reclaim", side_effect=AssertionError("held reclaim")):
            view = self.tick()
        self.assertEqual((view.state, view.reason, view.source_count), ("blocked", "recovery_review", 1))
        self.assertEqual((view.pending_archive_bytes, view.pending_parse_bytes), (len(payload), len(payload)))
        self.assertEqual(self.local("capture_chunks"), retained)
        self.assertEqual(self.local("native_transfers"), intent)
        self.assertEqual(self.entries(), [])
        self.assertTrue(self.app.archive.state(self.context, state.source_id, state.source_epoch).recovery_held)
        self.assertEqual(self.archived(state), payload)

    def test_fair_bounds_divergent_epochs_gaps_and_independent_failures(self):
        # Seventeen closed owners plus the active eighteenth exercise both finite
        # rings with real admission, including exit before the first tick.
        candidates = [self.bind()[0] for _ in range(18)]
        paths = [self.source(c, self.header(c) + self.record(c, "source " + str(i)))
                 for i, c in enumerate(candidates)]
        with patch("qorqut_core.runtime.native_collector.discover", wraps=discover) as discovery:
            first = self.tick()
            self.assertEqual(discovery.call_count, 16)
            self.assertEqual(first.source_count, 16)
            self.assertEqual(str(discovery.call_args_list[0].args[3].run.run_id),
                             self.rows("session_owners")[-1]["owner_id"])
            self.tick()
            self.assertEqual(discovery.call_count, 18)
        self.assertEqual(len(self.entries()), 18)
        capture = self.collector._capture
        source = next(s for s in self.capture_states() if s.native_session_id == str(candidates[-1].native_session_id))
        # Hold only semantic consumption while the raw archive advances. The
        # retained fd must drain >1 MiB after the named path is replaced.
        tail = self.record(candidates[-1], "large " + "x" * (2 * 1048576 + 1000), assistant=True)
        with paths[-1].open("ab") as stream:
            stream.write(tail)
        consume = self.collector._projector.consume
        def delayed(context, binding, sid, epoch, **kwargs):
            if sid == source.source_id:
                raise CoreError("unavailable", "synthetic private diagnostic")
            return consume(context, binding, sid, epoch, **kwargs)
        with patch.object(self.collector._projector, "consume", side_effect=delayed):
            self.tick()
            old = paths[-1].read_bytes()
            paths[-1].rename(paths[-1].with_suffix(".retained"))
            replacement = self.header(candidates[-1]) + self.record(candidates[-1], "replacement") + b"bad-json\n"
            paths[-1].write_bytes(replacement)
            self.tick()
            self.tick()
            self.tick()
        self.assertGreater(len(self.capture_states()), 18)
        prior = next(s for s in self.capture_states() if s.source_epoch == source.source_epoch)
        self.assertEqual(self.archived(prior), old)
        self.assertEqual(prior.local_captured_offset, len(old))
        newest = capture.state(source.source_id)
        self.assertNotEqual(newest.source_epoch, prior.source_epoch)
        self.assertEqual(newest.previous_epoch, prior.source_epoch)
        # A failing old source must not prevent an independent active append.
        independent = self.record(candidates[0], "independent late", assistant=True)
        with paths[0].open("ab") as stream:
            stream.write(independent)
        poll = capture.poll
        def failing(sid, **kwargs):
            assert_external_io_allowed()
            self.assertEqual(kwargs, {"max_bytes": 1048576})
            if sid == source.source_id:
                raise NativeCaptureError("synthetic private path or secret")
            return poll(sid, **kwargs)
        with patch.object(capture, "poll", side_effect=failing), \
                patch.object(capture, "transfer", wraps=capture.transfer) as transfers, \
                patch.object(self.collector._projector, "consume", wraps=consume) as parses:
            for _ in range(3):
                view = self.tick()
                self.assertLessEqual(len(transfers.call_args_list), 16)
                self.assertTrue(all(c.kwargs == {"max_chunks": 16} for c in transfers.call_args_list))
                budgets = {}
                for call in parses.call_args_list:
                    budgets[call.args[2]] = budgets.get(call.args[2], 0) + call.kwargs["max_bytes"]
                self.assertTrue(all(b <= 1048576 for b in budgets.values()))
                transfers.reset_mock()
                parses.reset_mock()
        self.assertEqual(view.reason, "collection_blocked")
        self.assertNotIn("secret", view.model_dump_json())
        self.assertIn("independent late", [e["text"] for e in self.entries()])
        for _ in range(4):
            view = self.tick()
        self.assertEqual((view.source_count, view.pending_archive_bytes, view.pending_parse_bytes), (18, 0, 0))
        self.assertEqual((view.state, view.reason), ("gap", "capture_gap"))
        self.assertTrue(self.rows("native_parse_gaps"))
        self.assertIn("replacement", [e["text"] for e in self.entries()])
        self.reopen()
        self.assertEqual(self.tick().reason, "capture_gap")
        self.assertEqual(self.archived(prior), old)
        self.assertEqual(self.archived(newest), replacement)

    def test_disabled_and_changed_workspace_stop_effects(self):
        with patch.object(self.app, "settings", self.settings.model_copy(update={"local_terminal": None})):
            disabled = NativeCollector(self.app)
        with patch("qorqut_core.runtime.native_collector.NativeCapture", side_effect=AssertionError("disabled I/O")):
            disabled.tick()
        self.assertEqual(disabled.view(self.host["id"]).reason, "not_configured")
        disabled.close()
        candidate, _ = self.bind("claude")
        path = self.source(candidate, self.record(candidate, "before"))
        self.tick()
        before = self.capture_states()
        with path.open("ab") as stream:
            stream.write(self.record(candidate, "after"))
        marker = self.base / ".qorqut-owner.json"
        original = marker.read_bytes()
        marker.write_text("{}")
        self.assertEqual(self.tick().reason, "collection_blocked")
        self.assertEqual(self.capture_states(), before)
        marker.write_bytes(original)
        self.assertEqual(self.tick().pending_parse_bytes, 0)
        self.assertEqual([e["text"] for e in self.entries()], ["before", "after"])
        # Saved parser gaps remain visible even at equal capture/archive/parse offsets.
        with path.open("ab") as stream:
            stream.write(b"bad-json\n")
        self.assertEqual(self.tick().reason, "parser_gap")
        self.reopen()
        self.assertEqual(self.tick().reason, "parser_gap")
