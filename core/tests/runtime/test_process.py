from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from qorqut_core.runner.models import ProcessFingerprint
from qorqut_core.runtime import SessionOwners, observe_process
from runtime.support import RuntimeCase


class ProcessInspection(RuntimeCase):
    def test_exit_evidence_is_distinct_from_unreadable_or_malformed_proc(self):
        proc = self.root / "owned-proc-fixture"
        boot = proc / "sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text(str(uuid4()))
        slot = proc / "123"
        (slot / "ns").mkdir(parents=True)
        (slot / "ns/pid").touch()
        stat = slot / "stat"

        def write_stat(state="S", start=456):
            stat.write_text("123 (fixture with ) in name) " + " ".join([state, *(["0"] * 18), str(start)]))

        write_stat()
        process = ProcessFingerprint(boot_id=boot.read_text(), pid=123, start_ticks=456,
                                     uid=slot.stat().st_uid, pid_namespace=(slot / "ns/pid").stat().st_ino)
        observe = lambda p=process: observe_process(p, proc_root=proc, clock=self.clock)
        self.assertEqual(observe().state, "live")
        self.assertEqual(observe(process.model_copy(update={"uid": process.uid + 1})).state, "unknown")
        self.assertEqual(observe(process.model_copy(update={"pid_namespace": process.pid_namespace + 1})).state, "unknown")
        for malformed in ("broken", "999 (wrong pid) " + " ".join(["S", *(["0"] * 18), "456"]),
                          "123 (bad state) " + " ".join(["?", *(["0"] * 18), "456"])):
            stat.write_text(malformed)
            self.assertEqual(observe().state, "unknown")
        write_stat()
        read_text = Path.read_text

        def denied(path, *args, **kwargs):
            if path == stat:
                raise PermissionError("synthetic denial")
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", denied):
            self.assertEqual(observe().state, "unknown")
        with patch.object(Path, "read_text", side_effect=OSError("synthetic I/O failure")):
            self.assertEqual(observe().state, "unknown")
        original_boot = boot.read_text()
        boot.write_text("malformed")
        self.assertEqual(observe().state, "unknown")
        boot.unlink()
        self.assertEqual(observe().state, "unknown")
        boot.write_text(original_boot)
        (slot / "ns/pid").unlink()
        self.assertEqual(observe().state, "unknown")
        (slot / "ns/pid").touch()
        process = process.model_copy(update={"pid_namespace": (slot / "ns/pid").stat().st_ino})
        for state in ("Z", "X", "x"):
            write_stat(state)
            self.assertEqual(observe(process).state, "exited")
        write_stat()
        owners = SessionOwners(self.app, observe_process=lambda p: observe(p))
        connected = owners.connect(self.candidate(process))
        write_stat(start=457)
        self.assertEqual(observe(process).reason, "process_start_changed")
        self.assertTrue(owners.release(connected.owner_id, process))
        write_stat()
        boot.write_text(str(uuid4()))
        self.assertEqual(observe(process).reason, "boot_changed")
        boot.write_text(original_boot)
        stat.unlink()
        self.assertEqual(observe(process).reason, "process_missing")
