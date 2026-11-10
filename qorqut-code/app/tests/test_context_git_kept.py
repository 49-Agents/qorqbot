"""What a full commit ID names is read from Git once; a published view is built without starting Git per event."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from qorqut_context.engine import Context
from qorqut_context.git_store import KEPT, Git, _Kept
from qorqut_context.schema import ContextError


class Counted:
    """Count the Git processes started, by subcommand."""

    def __init__(self, case):
        self.calls = []
        real = Git.run

        def run(git, *args, **options):
            self.calls.append(args[0])
            return real(git, *args, **options)
        patch = mock.patch.object(Git, "run", run)
        patch.start()
        case.addCleanup(patch.stop)

    def take(self):
        calls, self.calls = self.calls, []
        return calls


class KeptReadsTests(unittest.TestCase):
    def setUp(self):
        KEPT.clear()
        self.addCleanup(KEPT.clear)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "repository"
        self.root.mkdir()
        self.git = Git(self.root)
        self.git.run("init", "-b", "main")
        self.commits = []
        for index in range(4):
            self.write("graph/config.json", json.dumps({"format_version": 3, "step": index}))
            self.write(f"graph/events/event{index}.json", json.dumps({"id": f"event{index}"}))
            self.write("notes/shared.txt", "the same in every commit")
            self.commits.append(self.commit(f"step {index}"))
        self.counted = Counted(self)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        self.git.run("add", "-A")
        self.git.run("commit", "-q", "-m", message)
        return self.git.run("rev-parse", "HEAD").decode().strip()

    def test_files_of_a_commit_are_read_once_and_equal_a_fresh_read(self):
        first = self.git.files(self.commits[-1])
        self.assertEqual(["ls-tree", "cat-file"], self.counted.take())
        second = self.git.files(self.commits[-1])
        self.assertEqual([], self.counted.take())
        self.assertEqual(first, second)
        self.assertIsNot(first, second)                       # a caller's dictionary is its own
        KEPT.clear()
        self.assertEqual(first, self.git.files(self.commits[-1]))
        self.assertEqual({"graph/config.json", "notes/shared.txt", *(f"graph/events/event{i}.json" for i in range(4))},
                         set(first))

    def test_an_earlier_commit_reuses_the_blobs_it_shares(self):
        self.git.files(self.commits[-1])
        self.counted.take()
        earlier = self.git.files(self.commits[1])
        self.assertEqual(["ls-tree", "cat-file"], self.counted.take())      # only its own config is new
        self.assertEqual(json.dumps({"format_version": 3, "step": 1}).encode(), earlier["graph/config.json"])
        self.assertEqual(b"the same in every commit", earlier["notes/shared.txt"])

    def test_a_ref_is_never_kept(self):
        before = self.git.files("refs/heads/main")
        self.write("notes/new.txt", "added later")
        self.commit("later")
        self.counted.take()
        after = self.git.files("refs/heads/main")
        self.assertEqual("ls-tree", self.counted.take()[0])
        self.assertNotIn("notes/new.txt", before)
        self.assertEqual(b"added later", after["notes/new.txt"])
        self.assertEqual(b"added later", self.git.read("HEAD", ["notes/new.txt"])["notes/new.txt"])
        self.assertEqual("ls-tree", self.counted.take()[0])

    def test_event_commits_are_kept_per_commit(self):
        expected = {f"event{index}": commit for index, commit in enumerate(self.commits)}
        self.assertEqual(expected, self.git.event_commits(self.commits[-1]))
        self.assertEqual(["log"], self.counted.take())
        self.assertEqual(expected, self.git.event_commits(self.commits[-1]))
        self.assertEqual(list(expected), list(self.git.event_commits(self.commits[-1])))    # publication order
        self.assertEqual([], self.counted.take())
        self.assertEqual({"event0": self.commits[0], "event1": self.commits[1]}, self.git.event_commits(self.commits[1]))

    def test_many_commit_paths_are_one_call_and_then_none(self):
        pairs = [(commit, f"graph/events/event{index}.json") for index, commit in enumerate(self.commits)]
        first = self.git.at(pairs)
        self.assertEqual(["cat-file"], self.counted.take())
        self.assertEqual({pair: json.dumps({"id": f"event{index}"}).encode() for index, pair in enumerate(pairs)}, first)
        self.assertEqual(first, self.git.at(pairs))
        self.assertEqual([], self.counted.take())
        configs = self.git.blobs_at(self.commits, "graph/config.json")
        self.assertEqual(["cat-file"], self.counted.take())
        self.assertEqual([json.dumps({"format_version": 3, "step": index}).encode() for index in range(4)],
                         [configs[commit] for commit in self.commits])
        self.assertEqual(configs, self.git.blobs_at(self.commits, "graph/config.json"))
        self.assertEqual([], self.counted.take())

    def test_nothing_at_a_name_is_reported_and_asked_again(self):
        absent = (self.commits[0], "graph/events/event3.json")
        unknown = ("0" * 40, "graph/config.json")
        self.assertEqual({absent: None, unknown: None}, self.git.at([absent, unknown]))
        self.counted.take()
        self.assertEqual({absent: None, unknown: None}, self.git.at([absent, unknown]))
        self.assertEqual(["cat-file"], self.counted.take())
        self.assertEqual({self.commits[0]: None}, self.git.blobs_at([self.commits[0]], "graph/events/event3.json"))

    def test_read_gives_exact_bytes_and_none_and_is_kept(self):
        wanted = ["graph/config.json", "graph/events/event3.json", "graph"]
        expected = {"graph/config.json": json.dumps({"format_version": 3, "step": 0}).encode(),
                    "graph/events/event3.json": None, "graph": None}
        self.assertEqual(expected, self.git.read(self.commits[0], wanted))
        self.counted.take()
        self.assertEqual(expected, self.git.read(self.commits[0], wanted))
        self.assertEqual([], self.counted.take())
        self.assertEqual({}, self.git.read(self.commits[0], []))
        with self.assertRaises(ContextError):
            self.git.read("0" * 40, ["graph/config.json"])
        with self.assertRaises(ContextError):
            self.git.files("0" * 40)

    def test_an_executable_file_is_still_refused(self):
        (self.root / "notes/shared.txt").chmod(0o755)
        commit = self.commit("executable")
        with self.assertRaises(ContextError):
            self.git.files(commit)
        with self.assertRaises(ContextError):
            self.git.read(commit, ["notes/shared.txt"])

    def test_what_is_kept_is_bounded(self):
        kept = _Kept(limit=1000)
        kept.put("a", b"x" * 300, 300)
        kept.put("b", b"y" * 300, 300)
        self.assertEqual(b"x" * 300, kept.get("a"))            # now the most recently used
        kept.put("c", b"z" * 300, 300)
        self.assertIsNone(kept.get("b"))
        self.assertEqual((b"x" * 300, b"z" * 300), (kept.get("a"), kept.get("c")))
        kept.put("huge", b"h" * 5000, 5000)
        self.assertIsNone(kept.get("huge"))
        self.assertEqual("fallback", kept.get("missing", "fallback"))


class PublishedViewTests(unittest.TestCase):
    def setUp(self):
        KEPT.clear()
        self.addCleanup(KEPT.clear)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.context = Context.initialize(root / "context", root / "runtime")
        self.counted = Counted(self)

    @staticmethod
    def content(snapshot):
        return {name: getattr(snapshot, name) for name in (
            "revision", "files", "format_version", "personal_context_node", "nodes", "edges", "notes", "catalogs",
            "event_commits", "events", "by_commit", "sources", "support", "used_ids", "actions", "known_objects")}

    def test_a_view_of_the_same_commit_is_built_without_reading_git_again(self):
        KEPT.clear()
        first = self.context.snapshot()
        cold = self.counted.take()
        second = self.context.snapshot()
        warm = self.counted.take()
        self.assertTrue(first.events)                            # the personal context event
        # Resolving the published ref is all that still asks Git: it can move.
        self.assertEqual(["rev-parse", "rev-parse", "merge-base"], warm)
        self.assertEqual(["rev-parse", "rev-parse", "merge-base", "ls-tree", "cat-file", "log", "cat-file", "cat-file"],
                         cold)
        self.assertEqual(self.content(first), self.content(second))
        self.assertIsNot(first.nodes, second.nodes)              # each view owns its records
        node = next(iter(first.nodes))
        self.assertIsNot(first.nodes[node], second.nodes[node])
        KEPT.clear()
        self.assertEqual(self.content(first), self.content(self.context.snapshot()))

    def test_a_modified_event_is_still_refused(self):
        snapshot = self.context.snapshot()
        event_id = next(iter(snapshot.events))
        path = f"graph/events/{event_id}.json"
        changed = json.loads(snapshot.files[path])
        changed["submitted_by"] = "someone-else"
        commit = self.context.git.commit({path: json.dumps(changed).encode()}, self.context.runtime, snapshot.revision,
                                         "Rewrite an immutable event")
        subprocess.run(["git", "-C", str(self.context.root), "update-ref", "refs/heads/main", commit], check=True)
        with self.assertRaisesRegex(ContextError, "immutable event was modified"):
            self.context.snapshot()


if __name__ == "__main__":
    unittest.main()
