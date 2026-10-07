"""Concrete workspace effects, crash evidence, projection races and recovery holds."""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import qorqut_core
from qorqut_core.app import Application
from qorqut_core.contracts import CoreError
from qorqut_core.agents.workspace import DIRECTORIES, MARKER, rename_noreplace
from test_foundation import InjectedCrash
from agents.support import AgentCase


def tree(path):
    return {str(p.relative_to(path)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
            p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in path.rglob("*")} if path.exists() else {}


class WorkspaceTests(AgentCase):
    def run_job(self, expected):
        self.assertEqual(self.app.jobs.run_next(worker_id=uuid4(), lease_seconds=1), expected)
        return self.app.jobs.get(expected)

    def test_real_workspace_preserves_seed_and_honest_dependency_no_launch(self):
        result = self.create(persona="\nExact\r\n persona\u2003é\n")
        agent_id, job_id, final, stage = self.coordinates(result)
        self.assertFalse(final.exists())
        with patch("subprocess.Popen", side_effect=AssertionError("provider/process launch")), \
             patch("socket.socket", side_effect=AssertionError("network launch")):
            job = self.run_job(job_id)
        self.assertEqual((job["state"], job["wait_reason"], job["progress"]["phase"]),
                         ("waiting", "dependency_unavailable", "runtime_integration_required"))
        self.assertEqual(job["last_error"]["code"], "unsupported_capability")
        self.assertIsNotNone(job["next_attempt_at"])
        self.assertFalse(stage.exists())
        self.assertEqual((final / "workspace/persona.md").read_bytes(), "\nExact\r\n persona\u2003é\n".encode())
        self.assertEqual((final / "workspace/memory.md").read_bytes(), b"")
        for path in DIRECTORIES:
            self.assertTrue((final / path).is_dir(), path)
        self.assertEqual(list((final / "runtime").iterdir()), [])
        self.assertFalse((final / "workspace/AGENTS.md").exists())
        self.assertFalse((final / "workspace/CLAUDE.md").exists())
        marker = json.loads((final / MARKER).read_text())
        self.assertEqual(marker, {"version": 1, "installation_id": str(self.settings.installation_id),
            "agent_id": agent_id, "operation_id": str(result.operation_id), "job_id": str(job_id), "phase": "complete"})
        current = self.get(agent_id)
        self.assertIsNotNone(current["workspace_initialized_at"])
        self.assertEqual(current["projection_revision"], 1)
        self.assertEqual((current["lifecycle"], current["lifecycle_revision"], current["start_requested"]), ("provisioning", 1, False))
        projection = json.loads((final / "agent.json").read_text())
        self.assertEqual((projection["id"], projection["revision"], projection["layout_version"]), (agent_id, 1, 1))
        self.assertNotIn("persona", projection)
        self.assertIs(projection["start_requested"], False)
        self.assertEqual(len(self.events(agent_id, "agent.workspace_initialized")), 1)
        self.clock.advance(1000)
        self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))
        self.assertEqual(self.app.jobs.get(job_id)["attempt_count"], 1)
        self.assertEqual(self.app.invoke("operations_get", self.context,
            {"operation_id": str(result.operation_id)}).data["original_result"], result.model_dump(mode="json"))
        self.restart()
        self.assertEqual(self.get(agent_id), current)
        self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))

    def test_crashes_marker_publication_projection_and_domain_commit(self):
        points = ("agent_after_stage_mkdir", "agent_after_preparing_marker", "agent_after_seeds_fsync",
            "agent_marker_after_stage_fsync", "agent_marker_after_complete", "agent_before_publish",
            "agent_after_publish", "agent_after_publish_fsync", "agent_projection_after_stage_fsync",
            "agent_projection_after_replace", "agent_before_workspace_commit", "agent_after_workspace_commit")
        for point in points:
            with self.subTest(point=point):
                result = self.create(point)
                agent_id, job_id, final, stage = self.coordinates(result)
                self.fault.point = point
                with self.assertRaises(InjectedCrash):
                    self.run_job(job_id)
                if point != "agent_after_workspace_commit":
                    self.assertIsNone(self.get(agent_id)["workspace_initialized_at"])
                self.restart()
                self.clock.advance(2)
                self.app.jobs.expire()
                self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))
                if point == "agent_after_stage_mkdir":
                    before = tree(stage)
                    self.assertEqual(self.app.jobs.reconcile(job_id).state, "uncertain")
                    self.assertEqual(self.app.jobs.reconcile(job_id).state, "uncertain")
                    self.assertFalse(final.exists())
                    self.assertEqual(tree(stage), before)
                    self.assertFalse((stage / MARKER).exists())
                    self.assertEqual(self.events(agent_id, "agent.workspace_initialized"), [])
                else:
                    resolution = self.app.jobs.reconcile(job_id)
                    self.assertEqual(resolution.reason, "dependency_unavailable", resolution)
                    self.assertTrue(final.is_dir())
                    self.assertEqual(self.get(agent_id)["projection_revision"], 1)
                    self.assertEqual(len(self.events(agent_id, "agent.workspace_initialized")), 1)
                    self.assertEqual(self.app.jobs.reconcile(job_id).reason, "dependency_unavailable")
                    self.assertEqual(len(self.events(agent_id, "agent.workspace_initialized")), 1)

    def test_published_authored_edits_deletions_and_missing_published_root_are_preserved(self):
        result = self.create(persona="initial")
        agent_id, job_id, final, stage = self.coordinates(result)
        self.fault.point = "agent_after_publish"
        with self.assertRaises(InjectedCrash):
            self.run_job(job_id)
        (final / "workspace/persona.md").write_bytes(b"authored edit")
        (final / "workspace/memory.md").unlink()
        (final / "workspace/scratch/keep").write_bytes(b"persistent scratch")
        (final / "workspace/data/keep").write_bytes(b"persistent data")
        authored = tree(final / "workspace")
        self.clock.advance(2)
        self.app.jobs.expire()
        self.assertEqual(self.app.jobs.reconcile(job_id).reason, "dependency_unavailable")
        self.assertEqual(tree(final / "workspace"), authored)
        self.assertEqual(self.app.jobs.reconcile(job_id).reason, "dependency_unavailable")
        self.assertEqual(tree(final / "workspace"), authored)
        retained = self.root / "retained-published"
        final.rename(retained)
        self.assertEqual(self.app.jobs.reconcile(job_id).state, "uncertain")
        update = self.app.invoke("agents_update", self.context, {"agent_id": agent_id, "expected_revision": 1,
            "display_name": "Changed", "idempotency_key": "missing-projection"})
        self.assertTrue(update.ok)
        project_job = self.app.jobs.run_next(worker_id=uuid4())
        self.assertEqual(self.app.jobs.get(project_job)["state"], "uncertain")
        self.assertFalse(final.exists())
        self.assertFalse(stage.exists())
        self.assertEqual(tree(retained / "workspace"), authored)

    def test_unknown_destinations_and_stages_never_adopted_or_deleted(self):
        for mode in ("empty-final", "unmarked-stage", "mismatched-stage", "mismatched-final", "preparing-final"):
            with self.subTest(mode=mode):
                result = self.create(mode)
                agent_id, job_id, final, stage = self.coordinates(result)
                target = stage if mode.endswith("stage") else final
                target.mkdir()
                if mode.startswith("mismatched") or mode == "preparing-final":
                    marker = {"version": 1, "installation_id": str(self.settings.installation_id), "agent_id": agent_id,
                        "operation_id": str(result.operation_id), "job_id": str(job_id), "phase": "complete"}
                    if mode.startswith("mismatched"):
                        marker["agent_id"] = str(uuid4())
                    else:
                        marker["phase"] = "preparing"
                    (target / MARKER).write_text(json.dumps(marker))
                before = tree(target)
                job = self.run_job(job_id)
                self.assertEqual(job["state"], "uncertain")
                self.assertEqual(self.app.jobs.reconcile(job_id).state, "uncertain")
                self.assertEqual(tree(target), before)
                self.assertIsNone(self.get(agent_id)["workspace_initialized_at"])
                self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))
        # A lease that expired before any file evidence cannot silently replay.
        result = self.create("absent-evidence")
        _, job_id, final, stage = self.coordinates(result)
        self.assertEqual(self.app.jobs.claim(worker_id=uuid4(), lease_seconds=1).job_id, job_id)
        self.clock.advance(2)
        self.app.jobs.expire()
        self.assertEqual(self.app.jobs.reconcile(job_id).state, "uncertain")
        self.assertFalse(final.exists())
        self.assertFalse(stage.exists())

    def test_owned_preparing_stage_keeps_regular_seed_bytes_and_rejects_specials(self):
        for mode in ("regular", "symlink", "fifo", "ancestor-link"):
            with self.subTest(mode=mode):
                result = self.create(mode)
                _, job_id, final, stage = self.coordinates(result)
                self.fault.point = "agent_after_preparing_marker"
                with self.assertRaises(InjectedCrash):
                    self.run_job(job_id)
                outside = self.root / ("outside-" + mode)
                outside.mkdir()
                workspace = stage / "workspace"
                if mode == "ancestor-link":
                    workspace.symlink_to(outside, target_is_directory=True)
                else:
                    workspace.mkdir()
                    if mode == "regular":
                        (workspace / "persona.md").write_bytes(b"preexisting bytes")
                    elif mode == "symlink":
                        (outside / "keep").write_bytes(b"untouched")
                        (workspace / "persona.md").symlink_to(outside / "keep")
                    else:
                        os.mkfifo(workspace / "persona.md")
                outside_before = tree(outside)
                self.clock.advance(2)
                self.app.jobs.expire()
                resolution = self.app.jobs.reconcile(job_id)
                if mode == "regular":
                    self.assertEqual(resolution.reason, "dependency_unavailable")
                    self.assertEqual((final / "workspace/persona.md").read_bytes(), b"preexisting bytes")
                else:
                    self.assertEqual(resolution.state, "uncertain")
                    self.assertFalse(final.exists())
                self.assertEqual(tree(outside), outside_before)

    def test_atomic_no_replace_race_symlink_substitution_and_unsupported_primitive(self):
        result = self.create("race")
        agent_id, job_id, final, stage = self.coordinates(result)
        outside = self.root / "outside"
        outside.mkdir()
        real_publish = rename_noreplace

        def raced(fd, source, target):
            final.symlink_to(outside, target_is_directory=True)
            return real_publish(fd, source, target)

        with patch("qorqut_core.agents.workspace.rename_noreplace", side_effect=raced):
            self.assertEqual(self.run_job(job_id)["state"], "uncertain")
        self.assertTrue(final.is_symlink())
        self.assertTrue(stage.is_dir())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIsNone(self.get(agent_id)["workspace_initialized_at"])
        result = self.create("unsupported")
        _, job_id, final, stage = self.coordinates(result)
        with patch("ctypes.CDLL", return_value=object()):
            job = self.run_job(job_id)
        self.assertEqual(job["last_error"]["code"], "unsupported_capability")
        self.assertTrue(stage.is_dir())
        self.assertFalse(final.exists())
        # Substitute the final ancestor after publication, before its projection.
        result = self.create("ancestor")
        _, job_id, final, stage = self.coordinates(result)
        renamed_parent = self.root / "detached-agents"

        def substitute(point):
            if point == "agent_after_publish_fsync":
                final.parent.rename(renamed_parent)
                final.parent.symlink_to(outside, target_is_directory=True)

        self.app.db.fault = substitute
        try:
            self.assertEqual(self.run_job(job_id)["state"], "uncertain")
            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((renamed_parent / final.name / "agent.json").exists())
        finally:
            self.app.db.fault = self.fault

    def test_projection_waits_then_reads_latest_and_records_stale_snapshot_honestly(self):
        result = self.create()
        agent_id, provision_id, final, _ = self.coordinates(result)
        provision = self.app.jobs.claim(worker_id=uuid4())
        args = {"agent_id": agent_id, "expected_revision": 1, "display_name": "Second", "idempotency_key": "second"}
        updated = self.app.invoke("agents_update", self.context, args)
        self.assertTrue(updated.ok)
        projection = self.app.jobs.claim(worker_id=uuid4())
        waiting = self.app.workspaces.execute(projection)
        self.assertEqual(waiting.reason, "retry_delay")
        self.app.jobs.finish(projection, waiting)
        self.assertFalse(final.exists())
        self.assertFalse(final.parent.joinpath(".qorqut-agent-stage-" + str(provision_id)).exists())
        changed = False

        def concurrent_update(point):
            nonlocal changed
            if point == "agent_projection_after_stage_fsync" and not changed:
                changed = True
                changed_result = self.app.invoke("agents_update", self.context,
                    {**args, "expected_revision": 2, "display_name": "Third", "idempotency_key": "third"})
                self.assertTrue(changed_result.ok)

        self.app.db.fault = concurrent_update
        try:
            done = self.app.workspaces.execute(provision)
            self.app.jobs.finish(provision, done)
        finally:
            self.app.db.fault = self.fault
        current = self.get(agent_id)
        self.assertEqual((current["revision"], current["projection_revision"]), (3, 2))
        self.assertEqual(json.loads((final / "agent.json").read_text())["revision"], 2)
        self.clock.advance(6)
        first, second = self.app.jobs.claim(worker_id=uuid4()), self.app.jobs.claim(worker_id=uuid4())
        self.assertEqual({first.kind, second.kind}, {"agent_project"})
        # Finish the newer queued job before the older one; both read revision 3.
        for claim in (second, first):
            self.app.jobs.finish(claim, self.app.workspaces.execute(claim))
            self.assertEqual(json.loads((final / "agent.json").read_text())["revision"], 3)
        self.assertEqual(self.get(agent_id)["projection_revision"], 3)
        self.assertEqual(len(self.events(agent_id, "agent.workspace_initialized")), 1)
        self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))

    def test_lifecycle_and_expired_claim_are_revalidated_before_further_effects(self):
        result = self.create("lifecycle")
        agent_id, job_id, final, stage = self.coordinates(result)
        claim = self.app.jobs.claim(worker_id=uuid4())
        with self.app.db.transaction() as tx:
            tx.connection.execute("UPDATE agents SET lifecycle_revision=2 WHERE id=?", (agent_id,))
        with self.assertRaises(CoreError) as stale:
            self.app.workspaces.execute(claim)
        self.assertEqual(stale.exception.code, "target_changed")
        self.assertFalse(final.exists())
        self.assertFalse(stage.exists())
        # Retire the intentionally stale fixture claim before another queue case.
        self.clock.advance(31)
        self.app.jobs.expire()
        for boundary in ("agent_after_preparing_marker", "agent_before_publish"):
            with self.subTest(boundary=boundary):
                result = self.create("expired-" + boundary)
                agent_id, job_id, final, stage = self.coordinates(result)

                def expire(point):
                    if point == boundary:
                        self.clock.advance(2)
                        self.app.jobs.expire()

                self.app.db.fault = expire
                try:
                    with self.assertRaises(CoreError) as expired:
                        self.run_job(job_id)
                    self.assertEqual(expired.exception.code, "target_changed")
                finally:
                    self.app.db.fault = self.fault
                self.assertTrue(stage.exists())
                if boundary == "agent_after_preparing_marker":
                    self.assertFalse((stage / "workspace").exists())
                self.assertFalse(final.exists())
                self.assertIsNone(self.get(agent_id)["workspace_initialized_at"])

    def test_recovery_inspection_is_read_only_and_keeps_every_hold(self):
        for mode, point in (("absent", None), ("staged", "agent_before_publish"),
                            ("published", "agent_projection_after_replace"), ("initialized", None)):
            with self.subTest(mode=mode):
                result = self.create(mode)
                agent_id, job_id, final, stage = self.coordinates(result)
                if point:
                    self.fault.point = point
                    with self.assertRaises(InjectedCrash):
                        self.run_job(job_id)
                elif mode == "initialized":
                    self.run_job(job_id)
                    (final / "agent.json").unlink()
                self.recovery("recover-" + mode)
                before_final, before_stage = tree(final), tree(stage)
                before_row, before_events = self.get(agent_id), self.events(agent_id)
                with patch("os.mkdir", side_effect=AssertionError("recovery mkdir")), \
                     patch("os.replace", side_effect=AssertionError("recovery replace")), \
                     patch("os.fsync", side_effect=AssertionError("recovery fsync")), \
                     patch("qorqut_core.agents.workspace.rename_noreplace", side_effect=AssertionError("recovery publish")):
                    resolution = self.app.jobs.reconcile(job_id)
                self.assertEqual((resolution.state, resolution.reason), ("waiting", "recovery_review"))
                self.assertEqual((tree(final), tree(stage)), (before_final, before_stage))
                self.assertEqual(self.get(agent_id), before_row)
                self.assertEqual(self.events(agent_id), before_events)
                self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))
                self.assertEqual(self.app.jobs.get(job_id)["wait_reason"], "recovery_review")

    def test_filesystem_work_releases_sql_and_effect_entry_rejects_write_transaction(self):
        result = self.create()
        _, job_id, _, _ = self.coordinates(result)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def pause(point):
            if point == "agent_after_seeds_fsync":
                entered.set()
                self.assertTrue(release.wait(5))

        self.app.db.fault = pause
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                work = pool.submit(self.app.jobs.run_next, worker_id=uuid4())
                self.assertTrue(entered.wait(3))
                self.assertTrue(self.app.invoke("config_update", self.context, self.update_defaults()).ok)
                self.assertTrue(self.app.invoke("agents_list", self.context).ok)
                release.set()
                self.assertEqual(work.result(timeout=5), job_id)
        finally:
            self.app.db.fault = self.fault
        with self.app.db.transaction():
            with self.assertRaises(CoreError):
                self.app.workspaces.execute(None)
            with self.assertRaises(CoreError):
                self.app.workspaces.inspect({})

    def test_abrupt_process_exit_after_projection_preserves_exact_import_and_recovers(self):
        result = self.create()
        agent_id, job_id, final, _ = self.coordinates(result)
        self.app.close()
        source = str(Path(qorqut_core.__file__).resolve().parent.parent)
        code = '''import os,sys
from pathlib import Path
from uuid import uuid4
import qorqut_core
from qorqut_core.app import Application,load_settings
assert Path(qorqut_core.__file__).resolve().parent.parent == Path(sys.argv[3])
def fault(point):
    if point == "agent_projection_after_replace":
        os._exit(76)
app=Application.open(load_settings(Path(sys.argv[1])),clock=lambda:sys.argv[2],fault=fault)
app.jobs.run_next(worker_id=uuid4(),lease_seconds=1)
raise SystemExit(2)
'''
        completed = subprocess.run([sys.executable, "-B", "-c", code,
            str(self.settings.data_root / "installation.json"), self.clock(), source],
            env={"PATH": os.defpath, "PYTHONPATH": source, "PYTHONNOUSERSITE": "1", "LC_ALL": "C.UTF-8"},
            cwd=self.root, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 76, completed.stderr)
        self.assertTrue((final / "agent.json").is_file())
        self.clock.advance(2)
        self.app = Application.open(self.settings, clock=self.clock, fault=self.fault)
        self.assertIsNone(self.get(agent_id)["workspace_initialized_at"])
        self.app.jobs.expire()
        self.assertEqual(self.app.jobs.reconcile(job_id).reason, "dependency_unavailable")
        self.assertEqual(len(self.events(agent_id, "agent.workspace_initialized")), 1)
