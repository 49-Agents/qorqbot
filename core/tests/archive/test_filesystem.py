import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from qorqut_core.agents.workspace import MARKER, rename_noreplace
from qorqut_core.contracts import CoreError
from test_foundation import InjectedCrash
from .support import ArchiveCase


class FilesystemTests(ArchiveCase):
    def test_complete_exact_marker_is_required_before_descendants_are_created(self):
        self.register()
        marker = self.settings.data_root / "agents" / self.agent["id"] / MARKER
        original = marker.read_bytes()
        data = json.loads(original)
        for name, value in (("agent_id", str(uuid4())), ("installation_id", str(uuid4())),
                            ("operation_id", str(uuid4())), ("job_id", str(uuid4())), ("phase", "preparing")):
            with self.subTest(name=name):
                marker.write_text(json.dumps({**data, name: value}))
                try:
                    with self.assertRaises(CoreError) as unowned:
                        self.receive()
                    self.assertEqual(unowned.exception.code, "uncertain_effect")
                    self.assertFalse(self.object_path().parent.parent.exists())
                finally:
                    marker.write_bytes(original)
        marker.unlink()
        with self.assertRaises(CoreError) as absent:
            self.receive()
        self.assertEqual(absent.exception.code, "uncertain_effect")
        marker.write_bytes(original)

    def test_symlink_ancestors_and_occupied_final_objects_cannot_escape_or_replace(self):
        self.register()
        outside = self.root / "owned-outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"keep")
        chunks = self.settings.data_root / "agents" / self.agent["id"] / "history" / "chunks"
        saved = chunks.with_name("saved-chunks")
        chunks.rename(saved)
        chunks.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaises(CoreError):
                self.receive()
            self.assertEqual(list(outside.iterdir()), [sentinel])
        finally:
            chunks.unlink()
            saved.rename(chunks)
        self.fault.point = "archive_before_stage_fsync"
        with self.assertRaises(InjectedCrash):
            self.receive()
        target = self.object_path()
        for kind in ("symlink", "directory", "fifo", "hardlink", "wrong-bytes"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    target.symlink_to(sentinel)
                elif kind == "directory":
                    target.mkdir()
                elif kind == "fifo":
                    os.mkfifo(target)
                elif kind == "hardlink":
                    os.link(sentinel, target)
                else:
                    target.write_bytes(b"occupied")
                before = self.inventory()
                original_inode = target.lstat().st_ino
                try:
                    with self.assertRaises(CoreError):
                        self.receive()
                    self.assertEqual(self.inventory(), before)
                    self.assertEqual(sentinel.read_bytes(), b"keep")
                    self.assertEqual(target.lstat().st_ino, original_inode)
                finally:
                    target.rmdir() if kind == "directory" else target.unlink()
        self.assertEqual(self.state().host_archived_offset, 0)

    def test_parent_substitution_at_publication_is_detected_before_sql_commit(self):
        for point in ("archive_before_publish", "archive_before_finalize"):
            with self.subTest(point=point):
                self.fresh_source()
                target = self.object_path()
                detached = target.parent.with_name("detached-" + self.epoch)
                def substitute(at):
                    if at == point:
                        target.parent.rename(detached)
                        target.parent.mkdir()
                self.app.db.fault = substitute
                try:
                    with self.assertRaises(CoreError) as changed:
                        self.receive()
                    self.assertEqual(changed.exception.code, "target_changed")
                    self.assertEqual(self.state().host_archived_offset, 0)
                    self.assertFalse(target.exists())
                    self.assertEqual(list(target.parent.iterdir()), [])
                    if point == "archive_before_publish":
                        self.assertFalse((detached / target.name).exists())
                finally:
                    self.app.db.fault = self.fault

    def test_data_root_substitution_and_racing_destination_are_never_adopted(self):
        self.register()
        root = self.settings.data_root
        detached = root.with_name("detached-data")
        def substitute(point):
            if point == "archive_before_finalize":
                root.rename(detached)
                root.mkdir()
        self.app.db.fault = substitute
        try:
            with self.assertRaises(CoreError) as changed:
                self.receive()
            self.assertEqual(changed.exception.code, "target_changed")
            self.assertEqual(list(root.iterdir()), [])
        finally:
            self.app.db.fault = self.fault
            root.rmdir()
            detached.rename(root)
        self.assertEqual(self.state().host_archived_offset, 0)
        self.fresh_source()
        target = self.object_path()
        def occupy(fd, source, destination):
            target.write_bytes(b"racing occupant")
            return rename_noreplace(fd, source, destination)
        with patch("qorqut_core.transcripts.archive.rename_noreplace", side_effect=occupy), self.assertRaises(CoreError):
            self.receive()
        self.assertEqual(target.read_bytes(), b"racing occupant")
        self.assertEqual(self.state().host_archived_offset, 0)
