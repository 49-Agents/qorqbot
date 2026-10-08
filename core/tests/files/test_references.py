"""Metadata observations use only disposable workspaces and synthetic sources."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import json
import os
import socket
from unittest.mock import patch
from uuid import uuid4

from messaging.support import ChatCase
from qorqut_core.contracts import CoreError, Invocation
from qorqut_core.files.models import FileReference
from qorqut_core.storage.database import assert_external_io_allowed
from test_foundation import InjectedCrash


class References(ChatCase):
    def workspace(self, agent=None):
        return self.settings.data_root / "agents" / (agent or self.a)["id"] / "workspace"

    def reference(self, path="files/report.txt", *, key="reference", caller=None, agent=None, **kwargs):
        return self.app.invoke("files_reference", caller or self.context, {
            "idempotency_key": key, "agent_id": (agent or self.a)["id"], "path": path, **kwargs})

    def snapshot(self):
        return {table: self.rows(table) for table in ("file_references", "conversation_entries", "conversation_cursors",
            "conversation_publications", "operations", "events", "jobs")}

    def test_unicode_large_sparse_metadata_without_content_reads_or_sql_io(self):
        source = self.workspace() / "files" / "  文 résumé 🙂.bin "
        size = 2**40 + 17
        with source.open("wb") as stream:
            stream.truncate(size)
        os.utime(source, ns=(-1_234_567_891, -1_234_567_891))
        info = source.stat()
        real_open, real_fdopen, real_read, real_pread = os.open, os.fdopen, os.read, os.pread
        observed_fds = []

        def open_checked(path, flags, *args, **kwargs):
            assert_external_io_allowed()
            fd = real_open(path, flags, *args, **kwargs)
            if os.fstat(fd).st_ino == info.st_ino:
                self.assertEqual(flags & os.O_ACCMODE, os.O_RDONLY)
                self.assertTrue(flags & os.O_NONBLOCK and flags & os.O_NOFOLLOW)
                observed_fds.append(fd)
            return fd

        def no_source_read(function):
            def checked(fd, *args, **kwargs):
                self.assertNotEqual(os.fstat(fd).st_ino, info.st_ino, "Source content must never be read")
                return function(fd, *args, **kwargs)
            return checked

        def fault(point):
            if point == "file_reference_after_observation":
                for fd in observed_fds:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
                self.clock.advance(3)
            self.fault(point)

        requested = "./files//./" + source.name
        with patch("os.open", side_effect=open_checked), patch("os.fdopen", side_effect=no_source_read(real_fdopen)), \
                patch("os.read", side_effect=no_source_read(real_read)), patch("os.pread", side_effect=no_source_read(real_pread)), \
                patch("hashlib.file_digest", side_effect=AssertionError("No content digest")), patch.object(self.app.db, "fault", fault):
            result = self.reference(requested)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.outcome, "completed")
        self.assertTrue(observed_fds)
        reference = result.data["reference"]
        FileReference.model_validate(reference)
        self.assertEqual((reference["path"], reference["requested_path"], reference["label"]),
            ("files/" + source.name, requested, source.name))
        self.assertEqual((reference["size_bytes"], reference["mtime_ns"], reference["sha256"]),
            (size, str(info.st_mtime_ns), None))
        self.assertTrue(reference["mtime_ns"].startswith("-"))
        self.assertLess(reference["observed_at"], reference["recorded_at"])
        self.assertEqual(reference["installation_id"], str(self.settings.installation_id))
        self.assertEqual(reference["recovery_epoch"], self.config()["recovery_epoch"])
        self.assertEqual(reference["state"], "referenced")
        self.assertNotIn("environment_generation", reference)
        self.assertIsNone(reference["note"])

    def test_owner_agent_cross_target_attribution_and_host_absolute_locations(self):
        for agent in (self.a, self.b, self.onbash):
            (self.workspace(agent) / "files/report.txt").write_text("artifact")
        outside = self.root / "host 文.txt"
        outside.write_text("outside authored workspace")
        args = {"idempotency_key": "default", "path": "files/report.txt"}
        missing_target = self.app.invoke("files_reference", self.context, args)
        self.assertEqual(missing_target.error.code, "invalid_argument")
        own = self.app.invoke("files_reference", self.actor(self.a), args)
        self.assertTrue(own.ok, own)
        cases = (
            (self.reference(key="owner", label="  Owner label  ", note="line\n\tstill exact "), self.a, self.context, "user"),
            (own, self.a, self.actor(self.a), "agent"),
            (self.reference(key="cross", caller=self.actor(self.a), agent=self.b), self.b, self.actor(self.a), "agent"),
            (self.reference(str(outside), key="host", agent=self.onbash), self.onbash, self.context, "user"),
            (self.reference(str(self.workspace(self.onbash)) + "//files/./report.txt", key="absolute-workspace", agent=self.onbash),
                self.onbash, self.context, "user"),
        )
        for result, agent, caller, role in cases:
            self.assertTrue(result.ok, result)
            ref = result.data["reference"]
            self.assertEqual((ref["agent_id"], ref["conversation_id"], ref["actor_id"]),
                (agent["id"], agent["conversation_id"], str(caller.actor_id)))
            entry = next(e for e in self.conversation(agent)["items"] if e["entry_id"] == result.data["entry"]["entry_id"])
            self.assertEqual((entry["kind"], entry["role"], entry["direction"], entry["sender_actor_id"]),
                ("file_reference", role, "outgoing", str(caller.actor_id)))
            self.assertEqual((entry["text"], entry["occurred_at"], entry["recorded_at"]),
                (ref["label"], ref["observed_at"], ref["recorded_at"]))
            self.assertEqual(entry["source"], {"type": "file_reference", "reference": ref})
            self.assertIsNone(entry["delivery"])
            self.assertIsNone(entry["handling"])
            event = next(e for e in self.rows("events") if e["event_seq"] == result.event_seq)
            self.assertEqual((event["kind"], event["actor_id"], event["operation_id"]),
                ("file.referenced", str(caller.actor_id), str(result.operation_id)))
            self.assertEqual(json.loads(event["payload_json"]), {key: ref[key] for key in
                ("reference_id", "agent_id", "conversation_id", "location", "path")})
        self.assertEqual(cases[0][0].data["reference"]["label"], "  Owner label  ")
        self.assertEqual(cases[0][0].data["reference"]["note"], "line\n\tstill exact ")
        self.assertEqual((cases[-2][0].data["reference"]["location"], cases[-2][0].data["reference"]["path"]), ("host", str(outside)))
        self.assertEqual((cases[-1][0].data["reference"]["location"], cases[-1][0].data["reference"]["path"]), ("workspace", "files/report.txt"))
        with patch.object(self.app.file_references, "_observe", side_effect=AssertionError("No guest mapping")):
            for path in ("/workspace/files/report.txt", str(outside), str(self.workspace() / "files/report.txt")):
                self.assertEqual(self.reference(path, key=path).error.code, "unsupported_capability")
        self.assertEqual(self.rows("session_owners"), [])

    def test_schema_validation_preserves_literal_text_and_refuses_controls(self):
        before = self.snapshot()
        invalid = [{"path": value} for value in ("", "x" * 4097, "a/../b", "../b", "a/..", "\ud800", 123)]
        invalid += [{"label": value} for value in ("", " \t", "  ", "x" * 201, "\ud800")]
        invalid += [{"note": value} for value in ("x" * 4001, "\ud800", 123)]
        for control in [chr(c) for c in (*range(32), *range(127, 160))]:
            invalid += [{"path": "a" + control}, {"label": "label" + control}]
            if control not in "\n\t":
                invalid.append({"note": "note" + control})
        invalid += [{"actor_id": str(uuid4())}, {"conversation_id": str(uuid4())}]
        with patch.object(self.app.file_references, "_observe", side_effect=AssertionError("Invalid input reached I/O")):
            for arguments in invalid:
                with self.subTest(arguments=repr(arguments)):
                    result = self.reference(**arguments)
                    self.assertEqual(result.error.code, "invalid_argument")
            self.assertEqual(self.reference(caller=Invocation(actor_id=uuid4())).error.code, "not_found")
        self.assertEqual(self.snapshot(), before)
        for index, filename in enumerate(("  spaced  ", "~", "$VALUE", "   ", "x" * 201, "文" + "x" * 200)):
            (self.workspace() / filename).write_bytes(b"")
            saved = self.reference(filename, key=f"literal-{index}")
            self.assertTrue(saved.ok, saved)
            self.assertEqual(saved.data["reference"]["path"], filename)
            self.assertEqual(saved.data["reference"]["requested_path"], filename)
            expected_label = "File" if not filename.strip() else filename[:199] + "…" if len(filename) > 200 else filename
            self.assertEqual(saved.data["reference"]["label"], expected_label)
            if expected_label != filename:
                self.assertEqual(self.reference(filename, key=f"literal-{index}", label=expected_label).error.code,
                    "idempotency_conflict")
                explicit = self.reference(filename, key=f"explicit-{index}", label=" Exact override ")
                self.assertTrue(explicit.ok, explicit)
                self.assertEqual((explicit.data["reference"]["path"], explicit.data["reference"]["requested_path"],
                    explicit.data["reference"]["label"]), (filename, filename, " Exact override "))
        (self.workspace() / "url:").mkdir()
        (self.workspace() / "url:" / "literal").write_bytes(b"")
        self.assertEqual(self.reference("url://literal").data["reference"]["path"], "url:/literal")

    def test_missing_symlink_directory_fifo_socket_and_workspace_marker_refusal(self):
        workspace = self.workspace()
        (workspace / "files/report.txt").write_text("regular")
        (workspace / "link").symlink_to("files/report.txt")
        (workspace / "parent-link").symlink_to("files", target_is_directory=True)
        os.mkfifo(workspace / "fifo")
        with socket.socket(socket.AF_UNIX) as special:
            special.bind(str(workspace / "socket"))
            before = self.snapshot()
            for path, code in (("missing", "not_found"), ("missing/child", "not_found"), ("link", "invalid_argument"),
                    ("parent-link/report.txt", "invalid_argument"), ("files", "invalid_argument"), ("fifo", "invalid_argument"),
                    ("socket", "invalid_argument"), (".", "invalid_argument"), ("files/", "invalid_argument"),
                    ("files/report.txt/.", "invalid_argument"), ("/", "unsupported_capability")):
                with self.subTest(path=path):
                    self.assertEqual(self.reference(path).error.code, code)
            self.assertEqual(self.snapshot(), before)
        marker = workspace.parent / ".qorqut-owner.json"
        marker.unlink()
        self.assertEqual(self.reference().error.code, "uncertain_effect")
        self.assertEqual(self.snapshot(), before)
        for error, code in ((errno.EACCES, "unavailable"), (errno.EIO, "storage_blocked")):
            with patch.object(self.app.file_references, "_observe", side_effect=OSError(error, "PRIVATE_DIAGNOSTIC")):
                failure = self.reference()
                self.assertEqual(failure.error.code, code)
                self.assertNotIn("PRIVATE_DIAGNOSTIC", failure.model_dump_json())

    def test_exact_replay_without_io_and_distinct_validated_inputs_conflict(self):
        source = self.workspace() / "files/report.txt"
        source.write_text("first")
        args = {"idempotency_key": "unchanged", "path": "./files//report.txt"}
        actor = self.actor(self.a)
        with ThreadPoolExecutor(max_workers=4) as pool, patch.object(self.app.file_references, "_observe",
                wraps=self.app.file_references._observe) as observe:
            results = list(pool.map(lambda _: self.app.invoke("files_reference", actor, args), range(8)))
        self.assertTrue(results[0].ok, results[0])
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(len(self.rows("file_references")), 1)
        self.assertEqual(len(self.rows("conversation_entries")), 1)
        self.assertEqual(len(self.events(self.a["id"], "file.referenced")), 1)
        self.assertEqual(sum(j["kind"] == "conversation_publish" for j in self.rows("jobs")), 1)
        source.unlink()
        self.restart()
        before = self.snapshot()
        with patch.object(self.app.file_references, "_observe", side_effect=AssertionError("Replay must not reopen")):
            self.assertEqual(self.app.invoke("files_reference", actor, args), results[0])
            for change in ({"path": "files/report.txt"}, {"agent_id": self.a["id"]}, {"label": "report.txt"}, {"note": ""}):
                result = self.app.invoke("files_reference", actor, {**args, **change})
                self.assertEqual(result.error.code, "idempotency_conflict")
        self.assertEqual(self.snapshot(), before)

    def test_prepare_rechecks_receipt_and_rollback_is_atomic(self):
        (self.workspace() / "files/report.txt").write_text("saved")
        operation = self.app.registry["files_reference"]
        prepare = operation.prepare
        prior = []

        def completed_during_prepare(args, context):
            handler = prepare(args, context)
            prior.append(self.app.execute(replace(operation, prepare=None, handler=handler), context, args.model_dump(mode="json")))
            return handler

        args = {"idempotency_key": "race", "path": "files/report.txt", "agent_id": self.a["id"]}
        result = self.app.execute(replace(operation, prepare=completed_during_prepare), self.context, args)
        self.assertEqual(result, prior[0])
        self.assertEqual(len(self.rows("file_references")), 1)
        for point in ("file_reference_after_record", "conversation_after_entry", "operation_before_result",
                "operation_after_result", "transaction_before_commit"):
            before = self.snapshot()
            self.fault.point = point
            with self.assertRaises(InjectedCrash):
                self.reference(key=point)
            self.assertEqual(self.snapshot(), before)
        self.fault.point = "transaction_after_commit"
        with self.assertRaises(InjectedCrash):
            self.reference(key="committed")
        before = self.snapshot()
        (self.workspace() / "files/report.txt").unlink()
        self.restart()
        self.assertTrue(self.reference(key="committed").ok)
        self.assertEqual(self.snapshot(), before)

    def test_observation_detects_file_and_parent_changes_but_does_not_claim_atomic_fs_commit(self):
        for change in ("contents", "replace", "parent"):
            with self.subTest(change=change):
                parent = self.workspace() / change
                parent.mkdir()
                source = parent / "source"
                source.write_text("initial")
                before = self.snapshot()

                def changed(point):
                    if point == "file_reference_observing":
                        if change == "contents":
                            source.write_text("changed content")
                        elif change == "replace":
                            source.rename(parent / "old")
                            source.write_text("replacement")
                        else:
                            parent.rename(parent.with_name(change + "-old"))
                            parent.mkdir()
                            source.write_text("replacement")

                with patch.object(self.app.db, "fault", changed):
                    self.assertEqual(self.reference(change + "/source").error.code, "target_changed")
                self.assertEqual(self.snapshot(), before)
        source = self.workspace() / "files/report.txt"
        source.write_text("before")
        original = source.stat()

        def later(point):
            if point == "file_reference_after_observation":
                source.unlink()
                self.clock.advance(10)

        with patch.object(self.app.db, "fault", later):
            saved = self.reference()
        self.assertTrue(saved.ok, saved)
        self.assertEqual(saved.data["reference"]["size_bytes"], original.st_size)
        self.assertLess(saved.data["reference"]["observed_at"], saved.data["reference"]["recorded_at"])

    def test_target_lifecycle_initialization_and_epoch_are_rechecked_before_commit(self):
        (self.workspace() / "files/report.txt").write_text("regular")
        # The current agents schema only persists provisioning. Exercise the
        # admission rule for future lifecycle rows without extending that schema.
        row = next(row for row in self.rows("agents") if row["id"] == self.a["id"])
        for lifecycle in ("archived", "archiving"):
            with patch("qorqut_core.files.references.get_row", return_value={**row, "lifecycle": lifecycle}):
                self.assertEqual(self.reference().error.code, "invalid_transition")
        with self.app.db.transaction() as tx:
            tx.connection.execute("UPDATE agents SET workspace_initialized_at=NULL WHERE id=?", (self.a["id"],))
        self.assertEqual(self.reference().error.code, "not_ready")
        with self.app.db.transaction() as tx:
            tx.connection.execute("UPDATE agents SET workspace_initialized_at=? WHERE id=?", (row["workspace_initialized_at"], self.a["id"]))
        old_epoch = self.config()["recovery_epoch"]
        for sql, values in (
                ("UPDATE agents SET lifecycle_revision=lifecycle_revision+1 WHERE id=?", (self.a["id"],)),
                ("UPDATE installation SET recovery_epoch=?", (str(uuid4()),)),
                ("UPDATE actors SET kind='agent',agent_id=? WHERE actor_id=?", (str(uuid4()), str(self.owner.actor_id)))):
            def changed(point):
                if point == "file_reference_after_observation":
                    with self.app.db.transaction() as tx:
                        tx.connection.execute(sql, values)
            with patch.object(self.app.db, "fault", changed):
                self.assertEqual(self.reference().error.code, "target_changed")
            self.assertEqual(self.rows("file_references"), [])
            self.assertEqual(self.rows("conversation_entries"), [])
        self.assertNotEqual(self.config()["recovery_epoch"], old_epoch)

    def test_saved_entry_publishes_after_restart_and_never_reopens_source(self):
        source = self.workspace() / "files/report.txt"
        source.write_text("before")
        saved = self.reference(label="Artifact", note="Recorded metadata")
        self.assertTrue(saved.ok, saved)
        entry = self.conversation()["items"][0]
        source.unlink()
        self.restart()
        with patch.object(self.app.file_references, "_observe", side_effect=AssertionError("No reopen")):
            self.pump()
            self.assertEqual(self.conversation()["items"][0], entry)
            self.assertEqual(self.conversation()["publication"]["state"], "current")
        published = self.path().read_bytes()
        self.assertEqual(json.loads(published), {"format_version": 1,
            **{key: value for key, value in entry.items() if key not in ("delivery", "handling")}})
        self.restart()
        self.pump()
        self.assertEqual(self.path().read_bytes(), published)
        self.assertEqual(self.reference(label="Artifact", note="Recorded metadata"), saved)
        self.send(body="files/report.txt")
        self.assertEqual([e["kind"] for e in self.conversation()["items"]], ["file_reference", "message"])
        self.assertEqual(len(self.rows("file_references")), 1)

    def test_immutable_storage_and_source_discrimination(self):
        (self.workspace() / "files/report.txt").write_text("regular")
        saved = self.reference()
        self.assertTrue(saved.ok, saved)
        for table in ("file_references", "conversation_entries"):
            row = self.rows(table)[0]
            for sql, values in ((f"DELETE FROM {table}", ()), (f"UPDATE {table} SET agent_id=agent_id", ()),
                    (f"INSERT OR REPLACE INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))):
                with self.assertRaises(CoreError), self.app.db.transaction() as tx:
                    tx.connection.execute(sql, values)
        original = self.rows("conversation_entries")[0]
        reference = self.rows("file_references")[0]
        for changes in ({"file_reference_id": None}, {"sender_actor_id": None}, {"kind": "message"},
                {"message_id": str(uuid4())}, {"run_id": str(uuid4())}, {"provider": "codex"},
                {"source_id": str(uuid4())}, {"source_epoch": str(uuid4())}, {"offset": 0}, {"length": 0}):
            fresh_reference = {**reference, "reference_id": str(uuid4())}
            row = {**original, "entry_id": str(uuid4()), "conversation_seq": 2,
                "file_reference_id": fresh_reference["reference_id"], **changes}
            with self.assertRaises(CoreError), self.app.db.transaction() as tx:
                tx.connection.execute(f"INSERT INTO file_references ({','.join(fresh_reference)}) VALUES ({','.join('?' for _ in fresh_reference)})",
                    tuple(fresh_reference.values()))
                tx.connection.execute(f"INSERT INTO conversation_entries ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                    tuple(row.values()))
        with self.app.db.read() as tx:
            self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
