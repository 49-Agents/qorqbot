from pathlib import Path
from unittest.mock import patch

from qorqut_context import Conflict, Context, ContextError
from qorqut_context.git_store import Git
from .helpers import ContextCase


class ProcessStopped(BaseException):
    """Simulate losing the process without running normal error bookkeeping."""


class Recovery(ContextCase):
    def test_repository_binding_keeps_one_runtime_and_pending_event(self):
        event = self.source()
        self.node(event)
        # A Git file must still lead every handle to the repository's lock.
        self.context.git.run("init", "--separate-git-dir", str(self.directory / "git-data"))
        self.assertTrue((self.context.root / ".git").is_file())
        other = Context(self.context.root, self.directory / "alternate-runtime")
        with self.context._lock(), self.assertRaises(Conflict):
            other.pending()
        with self.assertRaises(Conflict):
            other.pending()
        self.assertFalse(other.runtime.exists())

        unrelated = self.directory / "unrelated"
        with self.assertRaises(Conflict):
            Context.initialize(unrelated, self.context.runtime)
        self.assertFalse((unrelated / ".git/qorqut-context/runtime.json").exists())
        self.assertEqual(self.context.pending()["id"], event.event_id)

        alias = self.directory / "runtime-alias"
        alias.symlink_to(self.context.runtime, target_is_directory=True)
        resumed = Context(self.context.root, alias)
        self.assertEqual(resumed.pending()["id"], event.event_id)
        self.assertEqual(resumed.recover()["status"], "needs_retry")
        commit = resumed.publish(event.event_id)
        self.assertEqual(resumed.snapshot().read("node", "jesse")["support"], [commit])
        self.assertFalse(any(path.endswith(("runtime.json", "context.json", "writer.lock"))
                             for path in resumed.snapshot().files))
        self.assertIsNone(resumed.pending())

    def test_initialization_preserves_files_appearing_before_repository_lock(self):
        root = self.directory / "initializing"
        original_run = Git.run

        def create_file_after_init(git, *args, **kwargs):
            result = original_run(git, *args, **kwargs)
            if args[0] == "init":
                (git.root / "owner.txt").write_text("Keep this work.")
            return result

        with patch.object(Git, "run", create_file_after_init), self.assertRaises(Conflict):
            Context.initialize(root)
        self.assertEqual((root / "owner.txt").read_text(), "Keep this work.")
        self.assertFalse((root / ".git/qorqut-context/runtime.json").exists())
        self.assertFalse(root.with_name(root.name + ".runtime").exists())

    def test_initialization_checkout_preserves_a_late_conflicting_file(self):
        root = self.directory / "initializing"
        original_checkout = Git.checkout

        def collide_at_checkout(git, base, commit):
            path = git.root / "graph/config.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"Owner's unfinished configuration.")
            return original_checkout(git, base, commit)

        with patch.object(Git, "checkout", collide_at_checkout), self.assertRaises(ContextError):
            Context.initialize(root)
        self.assertEqual((root / "graph/config.json").read_bytes(), b"Owner's unfinished configuration.")

    def test_partial_checkout_reconciles_only_known_target_entries(self):
        first = self.source()
        self.node(first)
        self.edge(first, "edge1", "jesse", "jesse")
        base = self.context.publish(first.event_id)
        event = self.context.begin("edit", "foundation/one.md", content=b"Updated source.", submitted_by="owner")
        self.context.stage_handoff_note(event.event_id, "")
        self.node(event, summary="Updated Jesse")
        self.context.remove_edge(event.event_id, "edge1", explanation="Relationship corrected.", citations=["self"])

        def partially_materialize(before, commit):
            target = self.context.git.files(commit)
            for path in ("foundation/one.md", f"graph/events/{event.event_id}.json"):
                (self.context.root / path).write_bytes(target[path])
            (self.context.root / "graph/edges/edge1.json").unlink()
            raise ProcessStopped()

        with patch.object(self.context.git, "checkout", side_effect=partially_materialize), self.assertRaises(ProcessStopped):
            self.context.publish(event.event_id)
        commit = self.context.git.head()
        self.assertEqual(self.context.git.run("write-tree").strip(), self.context.git.run("rev-parse", base + "^{tree}").strip())
        count = self.context.git.run("rev-list", "--count", "main")
        unrelated = self.context.root / "owner.txt"
        unrelated.write_bytes(b"Unrelated staged work.")
        self.context.git.run("add", "--", "owner.txt")
        staged = self.context.git.run("ls-files", "--stage", "--", "owner.txt")

        resumed = Context(self.context.root)
        index = Path(resumed.git.run("rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip())
        lock = Path(str(index) + ".lock")
        lock.write_bytes(b"Another Git user's lock.")
        try:
            with self.assertRaisesRegex(Conflict, "index is locked"):
                resumed.recover()
            self.assertEqual(lock.read_bytes(), b"Another Git user's lock.")
        finally:
            lock.unlink()  # Release only this test's simulated Git-user lock.

        owner_blob = resumed.git.run("hash-object", "-w", "--stdin", data=b"Owner's staged source.").decode().strip()
        resumed.git.run("update-index", "--cacheinfo", "100644", owner_blob, "foundation/one.md")
        owner_index = index.read_bytes()
        with self.assertRaisesRegex(Conflict, "unrelated staged changes"):
            resumed.recover()
        self.assertEqual(index.read_bytes(), owner_index)
        self.assertEqual(resumed.pending()["id"], event.event_id)
        base_blob = resumed.git.run("rev-parse", base + ":foundation/one.md").decode().strip()
        resumed.git.run("update-index", "--cacheinfo", "100644", base_blob, "foundation/one.md")

        self.assertEqual(resumed.recover(), {"status": "completed", "commit": commit, "event_id": event.event_id})
        self.assertEqual(resumed.git.head(), commit)
        self.assertEqual(resumed.git.run("rev-list", "--count", "main"), count)
        self.assertEqual(resumed.git.run("diff", "--cached", "--name-only", commit, "--", "foundation", "graph"), b"")
        self.assertEqual(resumed.git.run("diff", "--name-only", "--", "foundation", "graph"), b"")
        self.assertEqual(resumed.git.run("ls-files", "--stage", "--", "owner.txt"), staged)
        self.assertEqual(unrelated.read_bytes(), b"Unrelated staged work.")
        self.assertEqual(resumed.index.revision(), commit)
        self.assertEqual(resumed.snapshot().nodes["jesse"]["summary"], "Updated Jesse")
        self.assertNotIn("edge1", resumed.snapshot().edges)
        self.assertIsNone(resumed.pending())

    def test_interrupted_explicit_discard_can_finish_without_retrying_source(self):
        event = self.source()
        before = self.context.git.head()
        with patch.object(self.context, "_cleanup", side_effect=ProcessStopped()), self.assertRaises(ProcessStopped):
            self.context.discard(event.event_id)
        resumed = Context(self.context.root)
        self.assertEqual(resumed.recover(), {"status": "discarded", "event_id": event.event_id})
        self.assertEqual(resumed.git.head(), before)
        self.assertIsNone(resumed.pending())

    def test_stop_before_publication_retains_same_event_input_and_draft(self):
        event = self.source(content=b"Preserve these exact source bytes.\r\n")
        self.node(event)
        before = self.context.git.head()

        def stop(phase):
            if phase == "before_publication":
                raise ProcessStopped()

        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.context.publish(event.event_id)
        resumed = Context(self.context.root)
        self.assertEqual(resumed.git.head(), before)
        self.assertEqual(resumed.recover()["status"], "needs_retry")
        self.assertEqual(resumed.pending()["id"], event.event_id)
        self.assertEqual((resumed.runtime / "events" / event.event_id / "input.bin").read_bytes(),
                         b"Preserve these exact source bytes.\r\n")
        resumed.publish(event.event_id)
        self.assertEqual(set(resumed.snapshot().nodes), {"jesse"})
        self.assertEqual(len(resumed.snapshot().events), 1)
        self.assertIsNone(resumed.pending())

    def _crash_after(self, checkpoint):
        event = self.source()
        self.node(event)

        def stop(phase):
            if phase == checkpoint:
                raise ProcessStopped()

        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.context.publish(event.event_id)
        commit = self.context.git.head()
        self.assertEqual(self.context.snapshot().read("node", "jesse")["support"], [commit])
        commits_before = self.context.git.run("rev-list", "--count", "main")
        resumed = Context(self.context.root)
        self.assertEqual(resumed.recover(), {"status": "completed", "commit": commit, "event_id": event.event_id})
        self.assertEqual(resumed.git.run("rev-list", "--count", "main"), commits_before)
        self.assertEqual(resumed.index.revision(), commit)
        self.assertEqual(resumed.git.head(), commit)
        self.assertEqual(len(resumed.snapshot().history("jesse")), 1)
        self.assertIsNone(resumed.pending())
        self.assertFalse((resumed.runtime / "events" / event.event_id).exists())
        resumed.git.require_clean()

    def test_stop_after_publication_finishes_without_republishing(self):
        self._crash_after("after_publication")

    def test_stop_after_checkout_is_idempotent(self):
        self._crash_after("after_checkout")

    def test_stop_after_index_finishes_bookkeeping(self):
        self._crash_after("after_index")

    def test_index_failure_preserves_published_event_and_recovers(self):
        event = self.source()
        self.node(event)
        with patch.object(self.context.index, "apply", side_effect=RuntimeError("index unavailable")):
            with self.assertRaisesRegex(RuntimeError, "index unavailable"):
                self.context.publish(event.event_id)
        commit = self.context.git.head()
        self.assertEqual(self.context.pending()["phase"], "published")
        self.assertEqual(self.context.pending()["error"], "index unavailable")
        self.assertEqual(self.context.snapshot().read("node", "jesse")["support"], [commit])
        with self.assertRaises(Conflict):
            self.context.discard(event.event_id)
        resumed = Context(self.context.root)
        self.assertEqual(resumed.recover()["commit"], commit)
        self.assertEqual(resumed.index.revision(), commit)
        self.assertEqual(len(resumed.snapshot().events), 1)

    def test_changed_publication_base_and_pending_event_block_new_work(self):
        event = self.source()
        self.node(event)
        with self.assertRaises(Conflict):
            self.source("two.md")
        self.context.git.run("commit", "--allow-empty", "-m", "Other writer")
        intervening = self.context.git.head()
        with self.assertRaises(Conflict):
            self.context.publish(event.event_id)
        self.assertEqual(self.context.git.head(), intervening)
        self.assertIsNotNone(self.context.pending())
        self.assertEqual(self.context.recover()["status"], "needs_retry")
        self.context.discard(event.event_id)
        self.assertEqual(self.context.git.head(), intervening)
        self.assertIsNone(self.context.pending())

    def test_compare_and_swap_handles_a_race_after_tree_preparation(self):
        event = self.source()
        self.node(event)
        other_commit = []

        def advance(phase):
            if phase == "before_publication":
                self.context.git.run("commit", "--allow-empty", "-m", "Concurrent writer")
                other_commit.append(self.context.git.head())

        with patch.object(self.context, "_checkpoint", side_effect=advance), self.assertRaises(Conflict):
            self.context.publish(event.event_id)
        self.assertEqual(self.context.git.head(), other_commit[0])
        self.assertNotIn(event.event_id, self.context.snapshot().events)
        self.assertIsNotNone(self.context.pending())

    def test_dirty_checkout_and_changed_input_are_not_overwritten(self):
        event = self.source()
        self.node(event)
        foreign = self.context.root / "owner-note.txt"
        foreign.write_text("Keep this work.")
        with self.assertRaises(Conflict):
            self.context.publish(event.event_id)
        self.assertEqual(foreign.read_text(), "Keep this work.")
        foreign.unlink()
        input_path = self.context.runtime / "events" / event.event_id / "input.bin"
        input_path.write_bytes(b"Changed behind the writer")
        with self.assertRaisesRegex(ContextError, "input changed"):
            self.context.publish(event.event_id)
        self.assertNotIn(event.event_id, self.context.snapshot().events)
        self.assertTrue(input_path.exists())

    def test_writer_lock_excludes_a_second_context_handle(self):
        other = Context(self.context.root)
        with self.context._lock(), self.assertRaises(Conflict):
            other.pending()

    def test_checkout_recovery_preserves_external_file_changes(self):
        first = self.source()
        self.node(first)
        self.context.publish(first.event_id)
        event = self.context.begin("edit", "foundation/one.md", content=b"New source revision.", submitted_by="owner")
        self.context.stage_handoff_note(event.event_id, "")

        def stop(phase):
            if phase == "after_publication":
                raise ProcessStopped()

        with patch.object(self.context, "_checkpoint", side_effect=stop), self.assertRaises(ProcessStopped):
            self.context.publish(event.event_id)
        source_file = self.context.root / "foundation/one.md"
        source_file.write_bytes(b"Owner's unfinished edit.")
        with self.assertRaises(ContextError):
            Context(self.context.root).recover()
        self.assertEqual(source_file.read_bytes(), b"Owner's unfinished edit.")
        self.assertIsNotNone(self.context.pending())
