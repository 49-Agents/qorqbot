"""The Context home text asks Git again only when what Git would read has changed: an unchanged repository starts no
Git process on a repeated call, and any ref change made by another process is in the very next call's text."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from qorqut_context import git_store
from qorqut_context.engine import Context
from qorqut_context.git_store import published_ref_signature
from qorqut_context.integration.reading import PublishedContextText, SharedContextReader

IDENTITY = ("-c", "user.name=Another process", "-c", "user.email=other@example.invalid")


class Processes:
    """Count every process the Context Git layer starts."""

    def __init__(self, case):
        self.count = 0
        real = subprocess.run

        def run(*args, **options):
            self.count += 1
            return real(*args, **options)
        patch = mock.patch.object(git_store.subprocess, "run", run)
        patch.start()
        case.addCleanup(patch.stop)

    def take(self):
        count, self.count = self.count, 0
        return count


class HomeSignatureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository, self.runtime = self.root / "context", self.root / "runtime"
        Context.initialize(self.repository, self.runtime)
        self.reader = SharedContextReader(self.repository, self.runtime)
        self.addCleanup(self.reader.close)
        self.text = PublishedContextText(self.reader)

    def git(self, *args, cwd=None):
        """Git as a separate process, outside the reader."""
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run(["git", *IDENTITY, *args], cwd=cwd or self.repository, env=environment, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()

    def main(self):
        return self.git("rev-parse", "refs/heads/main")

    def seen(self):
        return self.reader.status()["revision"]

    def test_an_unchanged_repository_starts_no_git_process_on_the_second_call(self):
        processes = Processes(self)
        first = self.text.home_text()
        self.assertGreater(processes.take(), 0)
        self.assertEqual(first, self.text.home_text())
        self.assertEqual(0, processes.take())
        self.reader.status()
        self.assertEqual(0, processes.take())

    def test_a_commit_made_by_another_process_is_in_the_next_call(self):
        before = self.text.home_text()
        self.git("commit", "-q", "--allow-empty", "-m", "made elsewhere")
        after = self.text.home_text()
        self.assertNotEqual(before, after)
        self.assertIn(f"Revision {self.main()}:", after)

    def test_a_ref_moving_between_loose_and_packed_is_seen_each_time(self):
        first = self.main()
        self.git("pack-refs", "--all")
        self.assertFalse((self.repository / ".git/refs/heads/main").exists())
        self.assertEqual(first, self.seen())
        self.git("commit", "-q", "--allow-empty", "-m", "loose again")
        self.assertTrue((self.repository / ".git/refs/heads/main").exists())
        self.assertEqual(self.main(), self.seen())
        second = self.main()
        self.git("pack-refs", "--all")
        self.assertEqual(second, self.seen())
        # Back to the first commit, then packed: only packed-refs holds the branch, and it changed.
        self.git("update-ref", "refs/heads/main", first)
        self.git("pack-refs", "--all")
        self.assertFalse((self.repository / ".git/refs/heads/main").exists())
        self.assertEqual(first, self.seen())

    def test_a_replaced_repository_is_read_anew(self):
        first = self.seen()
        shutil.rmtree(self.repository)
        self.assertEqual("not_initialized", self.reader.status()["status"])
        shutil.rmtree(self.runtime)
        Context.initialize(self.repository, self.runtime)
        self.git("commit", "-q", "--allow-empty", "-m", "a different history")
        self.assertNotEqual(first, self.main())
        self.assertEqual(self.main(), self.seen())

    def test_a_branch_deleted_by_another_process_is_noticed(self):
        self.seen()
        self.git("checkout", "-q", "--detach")
        self.git("update-ref", "-d", "refs/heads/main")
        fresh = SharedContextReader(self.repository, self.runtime)
        self.addCleanup(fresh.close)
        self.assertEqual("not_initialized", fresh.status()["status"])
        self.assertEqual(fresh.status(), self.reader.status())


class SignatureCoverageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repository = self.root / "repository"
        Context.initialize(self.repository, self.root / "runtime")

    def git(self, *args):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", *IDENTITY, *args], cwd=self.repository, env=environment, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_each_thing_git_reads_changes_the_signature(self):
        signature = published_ref_signature(self.repository)
        self.assertIsNotNone(signature)
        self.assertEqual(signature, published_ref_signature(self.repository))
        self.git("config", "core.logAllRefUpdates", "false")
        configured = published_ref_signature(self.repository)
        self.assertTrue(signature != configured)              # (no assertNotEqual: it would print the configuration)
        self.git("symbolic-ref", "HEAD", "refs/heads/other")
        self.assertTrue(configured != published_ref_signature(self.repository))

    def test_layouts_it_cannot_vouch_for_are_never_kept(self):
        self.git("worktree", "add", "-q", "--detach", str(self.root / "linked"))
        self.assertIsNone(published_ref_signature(self.root / "linked"))      # a .git file
        self.assertIsNotNone(published_ref_signature(self.repository))
        self.git("config", "include.path", "elsewhere")
        self.assertIsNone(published_ref_signature(self.repository))
        self.git("config", "--unset", "include.path")
        main = self.repository / ".git/refs/heads/main"
        main.write_text("ref: refs/heads/other\n")
        self.assertIsNone(published_ref_signature(self.repository))
        self.assertIsNone(published_ref_signature(self.root / "absent"))


if __name__ == "__main__":
    unittest.main()
