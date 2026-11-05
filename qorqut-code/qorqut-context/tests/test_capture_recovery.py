"""Authoritative capture, verification and relocated restore: synthetic Git stores and injected runners only."""
import hashlib
import json
from pathlib import Path
import shutil
import threading
import subprocess
from unittest.mock import patch

from qorqut_context import Context, Pipeline, recovery
from qorqut_context.git_store import Git
from qorqut_context.recovery import RecoveryError
from qorqut_context.runner import AppServerRunner
from .helpers import ContextCase
from .test_pipeline import ScriptRunner, finish, new_node
from .test_recovery import ProcessStopped
from .test_usage import snap


class Barrier:
    """A bounded synthetic controller: proven while ``active``."""

    def __init__(self):
        self.active, self.checks = True, 0

    def verify_active(self):
        self.checks += 1
        if not self.active:
            raise RuntimeError("the barrier was released")
        return {"controller": "synthetic", "check": self.checks}


class Stopped(BaseException):
    """Simulate losing the process during a restore phase."""


def interrupt(phase):
    class Interrupted(recovery._Restore):
        def checkpoint(self, name):
            if name == phase:
                raise Stopped(name)
    return Interrupted


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tree(base, *, skip=("search.sqlite3", "writer.lock", "propagation.lock", "context.json", "runtime.json")):
    """Every file beneath ``base`` as ``{relative path: sha256}``, without markers, locks and the derived index."""
    return {path.relative_to(base).as_posix(): sha(path.read_bytes()) for path in sorted(Path(base).rglob("*"))
            if path.is_file() and path.name not in skip}


class Recovered(ContextCase):
    """A Context holding published history, a finished model attempt and one pending, interrupted event."""

    def setUp(self):
        super().setUp()
        source = self.source()
        self.node(source)
        self.context.publish(source.event_id)

        def usage(stage, index, emit):
            emit(snap(index, 10 * index))
            return snap(index, 20 * index)

        self.published = Pipeline(self.context, runner=ScriptRunner(
            lambda tools, payload: (new_node(tools, "A new person"), finish(tools)), usage)).submit(
            "add", "foundation/two.md", content=b"Jesse appears in this note.",
            metadata={"kind": "note", "title": "two.md", "origin": "focused-test"}, submitted_by="test")

        def die(tools, payload):
            new_node(tools, "Only a draft")
            raise ProcessStopped()

        with self.assertRaises(ProcessStopped):
            Pipeline(self.context, runner=ScriptRunner(die, usage)).submit(
                "add", "foundation/three.md", content=b"Jesse is mentioned again.",
                metadata={"kind": "note", "title": "three.md", "origin": "focused-test"}, submitted_by="test")
        self.pending = self.context.pending()
        self.barrier = Barrier()
        self.capture_directory = self.directory / "generation" / "context"
        self.capture_directory.parent.mkdir()

    def capture(self, **kwargs):
        return recovery.capture(self.context.root, self.context.runtime, self.capture_directory,
                                barrier=kwargs.pop("barrier", self.barrier), **kwargs)

    def targets(self, name="restored"):
        base = self.directory / name
        return base / "context", base / "runtime"

    def restore(self, summary, name="restored", **kwargs):
        repository, runtime = self.targets(name)
        return recovery.restore(self.capture_directory, target_repository=repository, target_runtime=runtime,
                                expected_manifest_sha256=summary["manifest_sha256"], **kwargs)

    def authority(self, context):
        snapshot = context.snapshot()
        return {"refs": context.git.run("for-each-ref", "--format=%(refname) %(objectname)").decode(),
                "files": {path: sha(content) for path, content in snapshot.files.items()},
                "event_commits": dict(snapshot.event_commits),
                "runtime": tree(context.runtime)}


class CaptureAndRestore(Recovered):
    def test_published_and_pending_authority_restores_to_other_paths_with_identical_hashes(self):
        self.context.search("Jesse")                        # the derived index exists in the source runtime
        before = self.authority(self.context)
        summary = self.capture()
        self.assertEqual(self.authority(self.context), before)              # capture changes no authoritative byte
        self.assertEqual((summary["complete"], summary["pending_event"], summary["events"]),
                         (True, self.pending["id"], 2))
        self.assertGreaterEqual(self.barrier.checks, 2)                     # proven before and after the copy
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        self.assertEqual(manifest["barrier"]["before"]["controller"], "synthetic")
        self.assertEqual(manifest["source"]["repository"], str(self.context.root))
        self.assertEqual(recovery.verify(self.capture_directory, expected_manifest_sha256=summary["manifest_sha256"])["published_commit"],
                         self.published["published_commit"])

        report = self.restore(summary)
        repository, runtime = self.targets()
        self.assertEqual((report["status"], report["resumed"], report["repository"], report["runtime"]),
                         ("restored", False, str(repository), str(runtime)))
        restored = Context(repository, runtime)
        self.assertEqual(self.authority(restored), before)                  # identical refs, graph/event bytes, runtime evidence
        # Only the two location markers differ, and they name the new pair.
        self.assertEqual(json.loads((repository / ".git/qorqut-context/runtime.json").read_bytes()), {"runtime_root": str(runtime)})
        self.assertEqual(json.loads((runtime / "context.json").read_bytes()), {"context_root": str(repository)})
        self.assertEqual(json.loads((self.context.runtime / "context.json").read_bytes()), {"context_root": str(self.context.root)})
        # Events keep their exact provenance: nothing was relabelled for the new location.
        for event_id, commit in self.context.snapshot().event_commits.items():
            self.assertEqual(restored.snapshot().events[event_id], self.context.snapshot().events[event_id])
            self.assertEqual(restored.snapshot().event_commits[event_id], commit)
        self.assertEqual(restored.git.head(), self.context.git.head())
        self.assertFalse((runtime / "search.sqlite3").exists())             # derived: not captured, rebuilt on demand
        self.assertEqual(report["pending_event"], self.pending["id"])

    def test_the_restored_pending_state_is_intact_and_resumes_only_when_the_owner_asks(self):
        summary = self.capture()
        self.restore(summary)
        repository, runtime = self.targets()
        restored = Context(repository, runtime)
        self.assertEqual(restored.pending(), self.pending)
        self.assertEqual((runtime / "events" / self.pending["id"] / "input.bin").read_bytes(), b"Jesse is mentioned again.")
        attempt = next((runtime / "runs" / self.pending["id"]).glob("*/attempt.json"))
        self.assertEqual(json.loads(attempt.read_bytes())["stage"], "propagation")      # unresolved execution, not repaired
        receipt = json.loads((attempt.parent / "calls" / "propagation.json").read_bytes())
        self.assertEqual(receipt["state"], "started")                                  # unresolved usage, not settled
        self.assertEqual(receipt["usage"]["input_tokens"], 20)
        self.assertEqual(restored.recover()["status"], "needs_retry")
        self.assertEqual(restored.search("Jesse")[0].kind, "node")                     # the index rebuilds on demand
        restored.stage_handoff_note(self.pending["id"], "")
        restored.put_node(self.pending["id"], {"id": "dr4ft", "summary": "Draft", "attributes": {"basic_entity_type": ["person"], "name": ["Draft"]}},
                          explanation="x", citations=["self"], support_after=["self"])
        commit = restored.publish(self.pending["id"])
        self.assertEqual(restored.snapshot().event_commits[self.pending["id"]], commit)

    def test_the_capture_is_self_contained(self):
        summary = self.capture()
        for path in self.capture_directory.rglob("*"):
            self.assertFalse(path.is_symlink())
            if path.is_file():
                self.assertEqual(path.stat().st_nlink, 1)                               # no hardlink to the source
                self.assertEqual(path.stat().st_mode & 0o077, 0)                        # private
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        names = {path.relative_to(self.capture_directory).as_posix() for path in self.capture_directory.rglob("*") if path.is_file()}
        self.assertNotIn(".git/objects/info/alternates", names)
        self.assertFalse(any(name.startswith("runtime/search") or name.endswith(".lock") or name.endswith("context.json") for name in names))
        # Moving the source away does not matter: verify and restore need only the capture.
        shutil.move(self.context.root, self.directory / "moved-away")
        shutil.move(self.context.runtime, self.directory / "moved-away-runtime")
        self.assertEqual(recovery.verify(self.capture_directory, expected_manifest_sha256=summary["manifest_sha256"])["closure_sha256"],
                         manifest["git"]["closure"]["sha256"])
        self.restore(summary)

    def test_objects_held_only_by_retained_runtime_state_are_captured(self):
        base = self.context.git.head()
        orphan = self.context.git.commit({"graph/orphan.json": b"{}\n"}, self.context.runtime, base, "never published")
        self.assertNotEqual(self.context.git.run("rev-list", "-n", "1", "--all").decode().strip(), orphan)
        attempt = next((self.context.runtime / "runs" / self.pending["id"]).glob("*"))
        (attempt / "recorded.json").write_bytes(json.dumps({"published_commit": orphan, "base_revision": base}).encode())
        summary = self.capture()
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        self.assertEqual([root["oid"] for root in manifest["git"]["runtime_only_roots"]], [orphan])
        self.assertTrue(manifest["git"]["runtime_only_roots"][0]["referenced_by"][0].endswith("recorded.json"))
        self.restore(summary)
        repository, _ = self.targets()
        self.assertEqual(Git(repository).run("cat-file", "-t", orphan).strip(), b"commit")

    def test_the_derived_index_is_classified_and_never_a_silent_omission(self):
        self.context.search("Jesse")
        self.assertTrue((self.context.runtime / "search.sqlite3").exists())
        self.capture()
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        derived = {(item["area"], item["path"]): item for item in manifest["derived"]}
        self.assertEqual(derived[("runtime", "search.sqlite3")]["owner"], "qorqut_context.search.SQLiteIndex")
        self.assertIn(("git", "index"), derived)
        self.assertFalse(any(item["path"] == "pending.json" for item in manifest["derived"]))

    def test_a_publication_interrupted_after_the_ref_moved_is_preserved_and_not_finished(self):
        self.context.discard(self.pending["id"])
        event = self.source("four.md", b"Another note.")
        self.node(event, "other")

        class Lost(BaseException):
            pass

        def stop(phase):
            if phase == "after_publication":
                raise Lost()

        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(Lost):
            self.context.publish(event.event_id)
        job = self.context.pending()
        self.assertEqual(job["phase"], "ready")
        published = self.context.git.head()
        self.assertEqual(published, self.context.snapshot().event_commits[event.event_id])
        summary = self.capture()
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        self.assertEqual((manifest["git"]["published_commit"], manifest["git"]["checkout_revision"]),
                         (published, job["base_revision"]))                        # the checkout lags the ref, as in the source
        self.restore(summary)
        repository, runtime = self.targets()
        restored = Context(repository, runtime)
        self.assertEqual(restored.pending()["phase"], "ready")
        self.assertEqual(restored.recover()["status"], "completed")             # finished only by the owner's recovery
        self.assertEqual(restored.git.head(), published)


class CaptureRefusals(Recovered):
    def refused(self, code, **kwargs):
        with self.assertRaises(RecoveryError) as raised:
            self.capture(**kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertFalse(self.capture_directory.exists())                    # nothing incomplete is ever published
        self.assertEqual([path.name for path in self.capture_directory.parent.iterdir()], [])
        return raised.exception

    def test_a_missing_or_unproven_barrier_refuses(self):
        self.refused("barrier_unproven", barrier=None)
        self.refused("barrier_unproven", barrier=object())
        self.barrier.active = False
        self.refused("barrier_unproven")

    def test_a_barrier_lost_during_capture_refuses(self):
        original = recovery._copy_stable

        def release(*args, **kwargs):
            self.barrier.active = False
            return original(*args, **kwargs)

        with patch.object(recovery, "_copy_stable", release):
            self.refused("barrier_unproven")

    def test_a_still_running_writer_refuses(self):
        for hold in (lambda: self.context._lock(),
                     lambda: recovery.file_lock(self.context.git.common_directory() / "qorqut-context" / "propagation.lock")):
            with hold():
                self.assertTrue(self.refused("writer_active").retryable)
        self.capture()                                                       # released: the same capture now succeeds

    def test_a_source_that_changes_during_capture_refuses(self):
        original = Git.stream

        def alter(git, *args, **kwargs):
            result = original(git, *args, **kwargs)
            pending = self.context.runtime / "pending.json"
            pending.write_bytes(pending.read_bytes() + b" ")
            return result

        with patch.object(Git, "stream", alter):
            self.assertTrue(self.refused("source_changed").retryable)

    def test_an_unclassified_file_is_never_dropped_as_derived(self):
        stray = self.context.runtime / "operator-note.txt"
        stray.write_text("keep me")
        self.assertEqual(self.refused("unclassified_authority").details["paths"], ["runtime/operator-note.txt"])
        stray.unlink()
        (self.context.root / ".git" / "ORIG_HEAD").write_text("x")
        self.assertEqual(self.refused("unclassified_authority").details["paths"], [".git/ORIG_HEAD"])
        (self.context.root / ".git" / "ORIG_HEAD").unlink()
        (self.context.runtime / "runs" / "link").symlink_to(self.directory)
        self.refused("unclassified_authority")
        (self.context.runtime / "runs" / "link").unlink()
        config = self.context.root / ".git" / "config"
        original = config.read_bytes()
        config.write_bytes(original + b'[remote "origin"]\n\turl = /somewhere/else\n')
        self.refused("unclassified_authority")
        config.write_bytes(original)
        self.capture()

    def test_a_declared_host_lock_is_held_recorded_and_never_captured(self):
        name = "host-driver.lock"
        lock = self.context.runtime / name
        lock.touch()
        self.refused("unclassified_authority")                                # undeclared: an unknown file is never dropped
        self.assertEqual(self.refused("invalid_request", runtime_locks=("writer.lock",)).code, "invalid_request")
        for bad in ("../x.lock", "a/b.lock", "plain", ".hidden-no-lock"):
            self.refused("invalid_request", runtime_locks=(bad,))
        with recovery.file_lock(lock):                                         # a running host writer is a refusal
            self.assertTrue(self.refused("writer_active", runtime_locks=(name,)).retryable)
        lock.write_bytes(b"data")                                              # bytes make it more than an advisory lock
        self.refused("unclassified_authority", runtime_locks=(name,))
        lock.write_bytes(b"")
        summary = self.capture(runtime_locks=(name,))
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        self.assertEqual(manifest["runtime"]["locks"], [name])
        self.assertFalse((self.capture_directory / "runtime" / name).exists())
        report = self.restore(summary)                                         # verified by the recorded lock name
        self.assertFalse((Path(report["runtime"]) / name).exists())

    def test_storage_outside_the_git_directory_refuses(self):
        alternates = self.context.root / ".git" / "objects" / "info" / "alternates"
        alternates.write_text(str(self.directory / "elsewhere") + "\n")
        self.refused("not_self_contained")
        alternates.unlink()
        shutil.rmtree(self.directory / "gitfile", ignore_errors=True)
        moved = self.directory / "git-data"
        shutil.move(self.context.root / ".git", moved)
        (self.context.root / ".git").write_text(f"gitdir: {moved}\n")
        self.refused("not_self_contained")

    def test_external_edits_and_an_unrecognized_checkout_refuse(self):
        (self.context.root / "foundation" / "one.md").write_bytes(b"edited outside")
        self.refused("source_dirty")
        (self.context.root / "foundation" / "one.md").write_bytes(b"Jesse is a person.")
        (self.context.root / "untracked.txt").write_text("x")
        self.refused("source_dirty")

    def test_an_invalid_ownership_pair_refuses(self):
        marker = self.context.runtime / "context.json"
        original = marker.read_bytes()
        marker.write_bytes(json.dumps({"context_root": str(self.directory / "other")}).encode())
        self.refused("invalid_ownership")
        marker.unlink()
        self.refused("invalid_ownership")
        marker.write_bytes(original)
        binding = self.context.root / ".git/qorqut-context/runtime.json"
        binding.write_bytes(json.dumps({"runtime_root": str(self.directory / "other-runtime")}).encode())
        self.refused("invalid_ownership")

    def test_missing_git_objects_and_missing_runtime_objects_refuse(self):
        blob = next(line.split()[0] for line in self.context.git.run("rev-list", "--objects", "--all").decode().splitlines()
                    if line.endswith("graph/events/%s.json" % self.published["event_id"]))
        loose = self.context.root / ".git" / "objects" / blob[:2] / blob[2:]
        saved = loose.read_bytes()
        loose.chmod(0o644)
        loose.unlink()
        self.assertEqual(self.refused("object_missing").code, "object_missing")
        loose.write_bytes(saved)
        attempt = next((self.context.runtime / "runs" / self.pending["id"]).glob("*"))
        (attempt / "recorded.json").write_bytes(json.dumps({"published_commit": "1" * 40}).encode())
        self.refused("object_missing")

    def test_a_missing_runtime_or_an_invalid_pending_job_refuses(self):
        pending = self.context.runtime / "pending.json"
        job = json.loads(pending.read_bytes())
        pending.write_bytes(json.dumps({**job, "format_version": 1}).encode())
        self.refused("source_invalid")
        pending.write_bytes(json.dumps(job).encode())
        (self.context.runtime / "events" / self.pending["id"] / "input.bin").write_bytes(b"tampered input")
        self.refused("source_invalid")
        with self.assertRaises(RecoveryError) as raised:
            recovery.capture(self.context.root, self.directory / "no-runtime", self.capture_directory, barrier=self.barrier)
        self.assertEqual(raised.exception.code, "invalid_request")


class Verification(Recovered):
    def setUp(self):
        super().setUp()
        self.summary = self.capture()

    def verify(self, summary=None):
        return recovery.verify(self.capture_directory, expected_manifest_sha256=(summary or self.summary)["manifest_sha256"])

    def invalid(self, code="invalid_capture"):
        with self.assertRaises(RecoveryError) as raised:
            self.verify()
        self.assertEqual(raised.exception.code, code)

    def test_a_modified_missing_or_extra_capture_file_fails(self):
        for relative in ("runtime/pending.json", "git/objects.pack", "git/logs/HEAD", "manifest.json"):
            path = self.capture_directory / relative
            original = path.read_bytes()
            path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            self.invalid()
            path.write_bytes(original)
        self.verify()
        victim = self.capture_directory / "runtime" / "events" / self.pending["id"] / "input.bin"
        original = victim.read_bytes()
        victim.unlink()
        self.invalid()
        victim.write_bytes(original)
        (self.capture_directory / "runtime" / "extra.json").write_text("{}")
        self.invalid()
        (self.capture_directory / "runtime" / "extra.json").unlink()
        self.verify()

    def test_a_removed_component_artifact_or_an_unrecorded_digest_fails(self):
        with self.assertRaises(RecoveryError):
            recovery.verify(self.capture_directory, expected_manifest_sha256="0" * 64)
        with self.assertRaises(RecoveryError):
            recovery.verify(self.capture_directory, expected_manifest_sha256=None)
        shutil.rmtree(self.capture_directory / "git")
        self.invalid()

    def test_an_incomplete_or_foreign_manifest_fails_even_with_a_matching_digest(self):
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        for change in ({"complete": False}, {"format_version": 2}, {"format": "other"},
                       {"fingerprint": {"before": "a", "after": "b"}},
                       {"source": {**manifest["source"], "runtime": "/somewhere/else"}}):
            data = recovery.encode({**manifest, **change})
            (self.capture_directory / "manifest.json").write_bytes(data)
            with self.assertRaises(RecoveryError) as raised:
                recovery.verify(self.capture_directory, expected_manifest_sha256=sha(data))
            self.assertIn(raised.exception.code, ("invalid_capture",))

    def test_a_pack_missing_a_required_runtime_object_fails(self):
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        manifest["git"]["runtime_only_roots"] = [{"oid": "2" * 40, "type": "commit", "referenced_by": ["pending.json"]}]
        data = recovery.encode(manifest)
        (self.capture_directory / "manifest.json").write_bytes(data)
        with self.assertRaises(RecoveryError) as raised:
            recovery.verify(self.capture_directory, expected_manifest_sha256=sha(data))
        self.assertEqual(raised.exception.code, "object_missing")

    def test_verification_changes_nothing(self):
        before = tree(self.capture_directory)
        source = tree(self.context.root / ".git"), tree(self.context.runtime)
        self.verify()
        self.assertEqual((tree(self.capture_directory), (tree(self.context.root / ".git"), tree(self.context.runtime))), (before, source))


class RestoreSafety(Recovered):
    def setUp(self):
        super().setUp()
        self.summary = self.capture()

    def refused(self, code, name="restored", **kwargs):
        with self.assertRaises(RecoveryError) as raised:
            self.restore(self.summary, name, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_targets_must_be_new_or_empty_explicit_and_separate(self):
        repository, runtime = self.targets()
        runtime.mkdir(parents=True)
        (runtime / "occupied").write_text("x")
        self.refused("invalid_target")
        self.assertFalse(repository.exists())
        self.assertEqual([path.name for path in runtime.parent.iterdir() if path.name.startswith(".")], [])   # nothing written
        shutil.rmtree(runtime)
        for repo, run in (("relative/context", str(runtime)), (str(repository), "relative/runtime"), (str(repository), str(repository / "inside"))):
            with self.assertRaises(RecoveryError):
                recovery.restore(self.capture_directory, target_repository=repo, target_runtime=run,
                                 expected_manifest_sha256=self.summary["manifest_sha256"])
        for repo, run in ((self.capture_directory / "context", self.directory / "elsewhere"), (self.directory / "e1", self.capture_directory / "runtime")):
            with self.assertRaises(RecoveryError) as raised:
                recovery.restore(self.capture_directory, target_repository=repo, target_runtime=run,
                                 expected_manifest_sha256=self.summary["manifest_sha256"])
            self.assertEqual(raised.exception.code, "invalid_target")
        link = self.directory / "linked"
        link.symlink_to(self.directory / "real", target_is_directory=True)
        (self.directory / "real").mkdir()
        with self.assertRaises(RecoveryError):
            recovery.restore(self.capture_directory, target_repository=link / "context", target_runtime=self.directory / "r2",
                             expected_manifest_sha256=self.summary["manifest_sha256"])
        (self.directory / "occupied").mkdir()
        (self.directory / "occupied" / "x").write_text("x")
        with self.assertRaises(RecoveryError) as raised:
            recovery.restore(self.capture_directory, target_repository=self.directory / "occupied", target_runtime=self.directory / "r3",
                             expected_manifest_sha256=self.summary["manifest_sha256"])
        self.assertEqual(raised.exception.code, "invalid_target")

    def test_the_source_locations_are_not_the_destination_and_are_not_touched(self):
        before = (tree(self.context.root / ".git"), tree(self.context.runtime))
        with self.assertRaises(RecoveryError) as raised:
            recovery.restore(self.capture_directory, target_repository=self.context.root, target_runtime=self.context.runtime,
                             expected_manifest_sha256=self.summary["manifest_sha256"])
        self.assertEqual(raised.exception.code, "invalid_target")
        self.assertEqual((tree(self.context.root / ".git"), tree(self.context.runtime)), before)

    def test_a_modified_capture_is_refused_before_any_target_exists(self):
        victim = self.capture_directory / "runtime" / "pending.json"
        victim.write_bytes(victim.read_bytes() + b" ")
        self.refused("invalid_capture")
        repository, runtime = self.targets()
        self.assertEqual((repository.exists(), runtime.exists(), list(repository.parent.glob(".*"))), (False, False, []))

    def test_destination_paths_never_come_from_the_imported_manifest(self):
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        for key in ("target", "destination", "target_repository", "target_runtime"):
            manifest[key] = {"repository": str(self.directory / "attacker" / "context"), "runtime": str(self.directory / "attacker" / "runtime")}
        data = recovery.encode(manifest)
        (self.capture_directory / "manifest.json").write_bytes(data)
        summary = {"manifest_sha256": sha(data)}
        repository, runtime = self.targets()
        report = recovery.restore(self.capture_directory, target_repository=repository, target_runtime=runtime,
                                  expected_manifest_sha256=summary["manifest_sha256"])
        self.assertEqual((report["repository"], report["runtime"]), (str(repository), str(runtime)))
        self.assertFalse((self.directory / "attacker").exists())

    def test_a_restore_is_repeatable_only_for_the_same_capture_and_targets(self):
        first = self.restore(self.summary)
        again = self.restore(self.summary)
        self.assertEqual((again["resumed"], again["published_commit"]), (True, first["published_commit"]))
        repository, runtime = self.targets()
        before = tree(repository / ".git"), tree(runtime)
        # Another target pair sharing the journal location is a different restore and is refused untouched.
        with self.assertRaises(RecoveryError) as raised:
            recovery.restore(self.capture_directory, target_repository=repository, target_runtime=self.directory / "restored" / "runtime2",
                             expected_manifest_sha256=self.summary["manifest_sha256"])
        self.assertEqual(raised.exception.code, "restore_conflict")
        self.assertEqual((tree(repository / ".git"), tree(runtime)), before)
        # A different capture of the same Context cannot overwrite the finished target either.
        second_directory = self.capture_directory.with_name("second")
        second = recovery.capture(self.context.root, self.context.runtime, second_directory, barrier=self.barrier)
        with self.assertRaises(RecoveryError) as raised:
            recovery.restore(second_directory, target_repository=repository, target_runtime=runtime,
                             expected_manifest_sha256=second["manifest_sha256"])
        self.assertIn(raised.exception.code, ("restore_conflict", "invalid_target"))
        self.assertEqual((tree(repository / ".git"), tree(runtime)), before)

    def test_every_interruption_leaves_an_incomplete_nonactive_target_that_resumes_deterministically(self):
        phases = ("before_staging", "staged", "bound", "runtime_renamed", "runtime_installed", "repository_renamed", "repository_installed")
        for number, phase in enumerate(phases):
            name = f"interrupted-{number}"
            repository, runtime = self.targets(name)
            with self.assertRaises(Stopped):
                self.restore(self.summary, name, _restore=interrupt(phase))
            journal = json.loads((repository.parent / ".context.context-restore.json").read_bytes())
            self.assertNotEqual(journal["phase"], "complete")
            if phase in ("before_staging", "staged", "bound"):
                self.assertFalse(repository.exists() or runtime.exists())          # nothing installed yet
            if phase != "repository_installed" and phase != "repository_renamed":
                # A half-installed pair can never be opened as a Context: the repository arrives last.
                self.assertFalse(repository.exists())
                with self.assertRaises(Exception):
                    Context(repository, runtime).pending()
                with self.assertRaises(RecoveryError):
                    recovery.check_pair(repository, runtime, json.loads((self.capture_directory / "manifest.json").read_bytes()))
            report = self.restore(self.summary, name)                             # the same restore completes it
            self.assertEqual(report["status"], "restored")
            self.assertEqual(Context(repository, runtime).pending(), self.pending)
            self.assertEqual(tree(runtime), tree(self.targets()[1]) if (self.targets()[1]).exists() else tree(runtime))
            self.assertEqual(json.loads((repository.parent / ".context.context-restore.json").read_bytes())["phase"], "complete")
            self.assertEqual([path.name for path in repository.parent.iterdir() if path.name.endswith(".restoring")], [])

    def test_changed_staged_or_installed_evidence_is_refused_not_replayed(self):
        repository, runtime = self.targets("evidence")
        with self.assertRaises(Stopped):
            self.restore(self.summary, "evidence", _restore=interrupt("runtime_installed"))
        installed = runtime / "pending.json"
        installed.write_bytes(installed.read_bytes() + b" ")
        with self.assertRaises(RecoveryError) as raised:
            self.restore(self.summary, "evidence")
        self.assertEqual(raised.exception.code, "restore_incomplete")
        self.assertFalse(repository.exists())                                       # the partial target stays incomplete
        self.assertTrue(runtime.exists())
        journal = json.loads((repository.parent / ".context.context-restore.json").read_bytes())
        self.assertEqual(journal["phase"], "runtime_installed")

    def test_a_different_capture_cannot_replace_a_partial_restore(self):
        repository, runtime = self.targets("partial")
        with self.assertRaises(Stopped):
            self.restore(self.summary, "partial", _restore=interrupt("bound"))
        other = self.capture_directory.with_name("other-capture")
        summary = recovery.capture(self.context.root, self.context.runtime, other, barrier=self.barrier)
        self.assertNotEqual(summary["manifest_sha256"], self.summary["manifest_sha256"])
        with self.assertRaises(RecoveryError) as raised:
            recovery.restore(other, target_repository=repository, target_runtime=runtime, expected_manifest_sha256=summary["manifest_sha256"])
        self.assertEqual(raised.exception.code, "restore_conflict")

    def test_a_concurrent_restore_of_the_same_location_is_refused(self):
        repository, runtime = self.targets("concurrent")
        entered, release = threading.Event(), threading.Event()
        outcome = {}

        class Held(recovery._Restore):
            def checkpoint(self, name):
                if name == "staged":
                    entered.set()
                    release.wait(30)

        def first():
            outcome["first"] = self.restore(self.summary, "concurrent", _restore=Held)

        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(entered.wait(30))
            with self.assertRaises(RecoveryError) as raised:
                self.restore(self.summary, "concurrent")
            self.assertEqual(raised.exception.code, "restore_conflict")
        finally:
            release.set()
            thread.join(60)
        self.assertEqual(outcome["first"]["status"], "restored")

    def entries(self, base):
        """Every entry beneath ``base`` (links not followed) except lock files, as ``{path: sha256 | kind}``."""
        found = {}
        for path in sorted(Path(base).rglob("*")):
            if path.name.endswith(".lock"):
                continue
            key = path.relative_to(base).as_posix()
            found[key] = "link" if path.is_symlink() else sha(path.read_bytes()) if path.is_file() else "dir"
        return found

    def test_an_unowned_staging_location_is_refused_and_never_removed(self):
        for number, side in enumerate(("repository", "runtime")):
            for kind in ("directory", "file", "link"):
                name = f"unowned-{number}-{kind}"
                repository, runtime = self.targets(name)
                base = repository.parent
                base.mkdir()
                staging = base / f".{(repository if side == 'repository' else runtime).name}.restoring"
                if kind == "directory":
                    staging.mkdir()
                    (staging / "owner-file.txt").write_text("not ours")
                elif kind == "file":
                    staging.write_text("not ours")
                else:
                    (base / "elsewhere").mkdir()
                    (base / "elsewhere" / "owner-file.txt").write_text("not ours")
                    staging.symlink_to(base / "elsewhere", target_is_directory=True)
                before = self.entries(base)
                error = self.refused("restore_conflict", name)
                self.assertEqual(error.details["paths"], [str(staging)])
                self.assertEqual(self.entries(base), before)                     # not one byte of it was removed or replaced
                self.assertFalse(repository.exists() or runtime.exists())
                self.assertFalse((base / ".context.context-restore.json").exists())   # no journal claimed it

    def test_both_derived_staging_locations_are_checked_before_a_journal_is_written(self):
        repository, runtime = self.targets("both")
        repository.parent.mkdir()
        for side in (repository, runtime):
            (side.parent / f".{side.name}.restoring").mkdir()
        before = self.entries(repository.parent)
        error = self.refused("restore_conflict", "both")
        self.assertEqual(len(error.details["paths"]), 2)
        self.assertEqual(self.entries(repository.parent), before)

    def test_a_journal_owns_only_its_exact_recorded_staging_and_never_a_foreign_one(self):
        repository, runtime = self.targets("owned")
        with self.assertRaises(Stopped):
            self.restore(self.summary, "owned", _restore=interrupt("bound"))
        journal_path = repository.parent / ".context.context-restore.json"
        journal = json.loads(journal_path.read_bytes())
        self.assertEqual(journal["staging"], {"repository": str(repository.parent / ".context.restoring"),
                                              "runtime": str(runtime.parent / ".runtime.restoring")})
        (runtime.parent / ".runtime.restoring" / "stale.tmp").write_text("left by the interrupted restore")
        report = self.restore(self.summary, "owned")                                  # the same restore may discard and rebuild it
        self.assertEqual((report["status"], report["resumed"]), ("restored", False))
        self.assertEqual(Context(repository, runtime).pending(), self.pending)
        # Without its journal the same staging is no one's: it is refused untouched, however it came to exist.
        repository, runtime = self.targets("orphaned")
        with self.assertRaises(Stopped):
            self.restore(self.summary, "orphaned", _restore=interrupt("bound"))
        (repository.parent / ".context.context-restore.json").unlink()
        before = self.entries(repository.parent)
        self.refused("restore_conflict", "orphaned")
        self.assertEqual(self.entries(repository.parent), before)
        # A journal that records other staging (a different location layout) does not own this one either.
        repository, runtime = self.targets("foreign")
        with self.assertRaises(Stopped):
            self.restore(self.summary, "foreign", _restore=interrupt("bound"))
        journal_path = repository.parent / ".context.context-restore.json"
        journal = json.loads(journal_path.read_bytes())
        journal["staging"]["runtime"] = str(self.directory / "somewhere" / ".runtime.restoring")
        journal_path.write_bytes(recovery.encode(journal))
        before = self.entries(repository.parent)
        self.refused("restore_conflict", "foreign")
        self.assertEqual(self.entries(repository.parent), before)

    def test_restores_that_share_either_target_contend_and_the_loser_leaves_every_byte_alone(self):
        repository, runtime = self.targets("shared")
        entered, release = threading.Event(), threading.Event()
        outcome = {}

        class Held(recovery._Restore):
            def checkpoint(self, name):
                if name == "staged":
                    entered.set()
                    release.wait(30)

        def first():
            outcome["first"] = self.restore(self.summary, "shared", _restore=Held)

        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(entered.wait(30))
            before = self.entries(repository.parent)
            expected = self.summary["manifest_sha256"]
            rivals = (("same runtime", self.directory / "rival-a" / "context", runtime),
                      ("same repository", repository, self.directory / "rival-b" / "runtime"),
                      ("repository path is the other's runtime", runtime, self.directory / "rival-c" / "runtime"),
                      ("runtime path is the other's repository", self.directory / "rival-d" / "context", repository))
            for label, other_repository, other_runtime in rivals:
                with self.subTest(label):
                    with self.assertRaises(RecoveryError) as raised:
                        recovery.restore(self.capture_directory, target_repository=other_repository, target_runtime=other_runtime,
                                         expected_manifest_sha256=expected)
                    self.assertEqual((raised.exception.code, raised.exception.retryable), ("restore_conflict", True))
                    self.assertEqual(self.entries(repository.parent), before)         # targets and staging untouched
                    for side in (other_repository, other_runtime):
                        if side.parent != repository.parent:
                            self.assertEqual([path.name for path in side.parent.iterdir() if not path.name.endswith(".lock")], [])
        finally:
            release.set()
            thread.join(60)
        self.assertEqual(outcome["first"]["status"], "restored")
        self.assertEqual(Context(repository, runtime).pending(), self.pending)

    def test_locks_are_taken_in_one_stable_order_whichever_role_a_path_has(self):
        orders = []
        original = recovery.fcntl.flock

        def record(descriptor, operation):
            orders[-1].append(Path(f"/proc/self/fd/{descriptor}").resolve().name)
            return original(descriptor, operation)

        base = self.directory / "ordered"
        for repository, runtime in ((base / "zeta", base / "alpha"), (base / "alpha2", base / "zeta2")):
            orders.append([])
            with patch.object(recovery.fcntl, "flock", record):
                recovery.restore(self.capture_directory, target_repository=repository, target_runtime=runtime,
                                 expected_manifest_sha256=self.summary["manifest_sha256"])
        self.assertEqual(orders[0], [".alpha.context-restore.lock", ".zeta.context-restore.lock"])    # the runtime's first
        self.assertEqual(orders[1], [".alpha2.context-restore.lock", ".zeta2.context-restore.lock"])  # the repository's first

    def test_overlapping_capture_target_and_control_paths_are_refused_before_anything_is_written(self):
        expected = self.summary["manifest_sha256"]
        base = self.directory / "overlap"
        cases = (("runtime is the repository staging", base / ".runtime.restoring", base / "runtime"),
                 ("runtime is the repository journal", base / "a", base / ".a.context-restore.json"),
                 ("runtime is the repository lock", base / "a", base / ".a.context-restore.lock"),
                 ("runtime is the repository staging of its sibling", base / "a", base / ".a.restoring"),
                 ("repository is the runtime lock", base / ".b.context-restore.lock", base / "b"))
        for label, repository, runtime in cases:
            with self.subTest(label):
                with self.assertRaises(RecoveryError) as raised:
                    recovery.restore(self.capture_directory, target_repository=repository, target_runtime=runtime,
                                     expected_manifest_sha256=expected)
                self.assertEqual(raised.exception.code, "invalid_target")
                self.assertFalse(base.exists())
        # The capture itself may not sit where a restore would stage, journal or lock.
        base.mkdir()
        for name in (".context.restoring", ".runtime.restoring", ".context.context-restore.json", ".context.context-restore.lock"):
            with self.subTest(name):
                capture = base / name
                summary = recovery.capture(self.context.root, self.context.runtime, capture, barrier=self.barrier)
                with self.assertRaises(RecoveryError) as raised:
                    recovery.restore(capture, target_repository=base / "context", target_runtime=base / "runtime",
                                     expected_manifest_sha256=summary["manifest_sha256"])
                self.assertEqual(raised.exception.code, "invalid_target")
                self.assertEqual([path.name for path in base.iterdir()], [name])             # nothing else was written
                self.assertTrue(recovery.verify(capture, expected_manifest_sha256=summary["manifest_sha256"])["complete"])
                shutil.rmtree(capture)

    def test_an_inconsistent_ownership_pair_in_the_capture_is_refused(self):
        manifest = json.loads((self.capture_directory / "manifest.json").read_bytes())
        manifest["source"]["ownership"]["runtime"]["value"] = {"context_root": "/not/the/source"}
        data = recovery.encode(manifest)
        (self.capture_directory / "manifest.json").write_bytes(data)
        with self.assertRaises(RecoveryError) as raised:
            recovery.restore(self.capture_directory, target_repository=self.targets()[0], target_runtime=self.targets()[1],
                             expected_manifest_sha256=sha(data))
        self.assertEqual(raised.exception.code, "invalid_capture")
        self.assertFalse(self.targets()[0].exists())


class NoSideEffects(Recovered):
    def test_capture_verify_and_restore_start_nothing_and_finish_nothing(self):
        before = (self.context.pending(), tree(self.context.runtime), tree(self.context.root / ".git"))
        launched = []
        real_popen = subprocess.Popen

        def only_git(command, *args, **kwargs):
            launched.append(command[0])
            if command[0] != "git":
                raise AssertionError(f"a process other than git was started: {command[0]}")
            return real_popen(command, *args, **kwargs)

        with patch.object(AppServerRunner, "run", side_effect=AssertionError("model call")), \
                patch("subprocess.Popen", only_git), \
                patch.object(Context, "recover", side_effect=AssertionError("recover")), \
                patch.object(Context, "publish", side_effect=AssertionError("publish")), \
                patch.object(Context, "discard", side_effect=AssertionError("discard")), \
                patch.object(Pipeline, "_run", side_effect=AssertionError("pipeline")), \
                patch.object(Pipeline, "retry", side_effect=AssertionError("retry")):
            summary = self.capture()
            recovery.verify(self.capture_directory, expected_manifest_sha256=summary["manifest_sha256"])
            self.restore(summary)
        self.assertEqual((self.context.pending(), tree(self.context.runtime), tree(self.context.root / ".git")), before)
        self.assertEqual(set(launched), {"git"})
        repository, runtime = self.targets()
        after = tree(runtime)
        self.assertEqual(sorted(path for path in after if path.startswith("runs/")),
                         sorted(path for path in tree(self.context.runtime) if path.startswith("runs/")))   # no new execution or usage
        self.assertEqual(sorted(path.name for path in runtime.iterdir()), ["context.json", "events", "pending.json", "runs"])


class ExactBytes(Recovered):
    def test_event_and_provenance_bytes_are_exact_when_the_pair_moves(self):
        self.context.discard(self.pending["id"])
        event = self.context.begin("add", "foundation/ctx.md", content=b"Attributed text", submitted_by="owner",
                                   metadata={"kind": "note", "title": "ctx", "origin": "test", "created_at": "2026"},
                                   record_context={"scope": {"kind": "global"}, "provenance": {"actor": "agent", "path": str(self.context.root)}})
        self.context.stage_handoff_note(event.event_id, "")
        self.node(event, "ctxn1")
        self.context.publish(event.event_id)
        summary = self.capture()
        self.restore(summary)
        repository, _ = self.targets()
        source_event = (self.context.root / "graph" / "events" / f"{event.event_id}.json").read_bytes()
        # The recorded provenance names the source location; it is evidence and must not be rewritten.
        self.assertIn(str(self.context.root).encode(), source_event)
        self.assertEqual((repository / "graph" / "events" / f"{event.event_id}.json").read_bytes(), source_event)
