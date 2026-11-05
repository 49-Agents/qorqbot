"""Synthetic exercise of export-source.py: exact committed content, nesting rules and unsafe-input refusal.

    python3 tools/test_export_source.py

Uses throwaway repositories under the system temporary directory (set TMPDIR to choose another); nothing else is read."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).with_name("export-source.py")
IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def git(repository, *args):
    return subprocess.run(["git", "-C", str(repository), *IDENTITY, *args], check=True, capture_output=True, text=True).stdout.strip()


def commit_all(repository, message):
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", message)
    return git(repository, "rev-parse", "HEAD")


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.child = self.repository("child", {"lib.py": "x = 1\n"})
        self.child_commit = commit_all(self.child, "child")
        self.parent = self.repository("parent", {"README.md": "one\n", "bin/run.sh": "#!/bin/sh\n"})
        os.chmod(self.parent / "bin/run.sh", 0o755)
        os.symlink("README.md", self.parent / "link.txt")
        (self.parent / "sub").mkdir()                                  # an uninitialized submodule location
        git(self.parent, "add", "-A")
        git(self.parent, "update-index", "--add", "--cacheinfo", f"160000,{self.child_commit},sub")
        git(self.parent, "commit", "-q", "-m", "one")
        self.c1 = git(self.parent, "rev-parse", "HEAD")
        (self.parent / "README.md").write_text("two, committed later\n")
        commit_all(self.parent, "two")
        (self.parent / "README.md").write_text("dirty\n")          # uncommitted edit and an untracked file must never be exported
        (self.parent / "untracked.txt").write_text("secret\n")

    def repository(self, name, files):
        path = self.root / name
        path.mkdir()
        git(path, "init", "-q")
        for relative, text in files.items():
            (path / relative).parent.mkdir(parents=True, exist_ok=True)
            (path / relative).write_text(text)
        return path

    def run_export(self, components, output=None, expect=0):
        manifest = self.root / "input.json"
        manifest.write_text(json.dumps({"format": 1, "components": components}))
        output = output or self.root / "out"
        result = subprocess.run(["python3", str(TOOL), "--manifest", str(manifest), "--output", str(output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def component(self, name, repository, commit, destination):
        return {"name": name, "repository": str(repository), "commit": commit, "destination": destination}

    def parent_and_child(self):
        return [self.component("host", self.parent, self.c1, "qorqut-code"),
                self.component("engine", self.child, self.child_commit, "qorqut-code/sub")]

    def test_exact_committed_content_with_nested_gitlink_component(self):
        self.run_export(self.parent_and_child())
        out = self.root / "out"
        self.assertEqual((out / "qorqut-code/README.md").read_text(), "one\n")
        self.assertFalse((out / "qorqut-code/untracked.txt").exists())
        self.assertEqual((out / "qorqut-code/sub/lib.py").read_text(), "x = 1\n")
        self.assertTrue(os.access(out / "qorqut-code/bin/run.sh", os.X_OK))
        self.assertEqual(os.readlink(out / "qorqut-code/link.txt"), "README.md")
        names = [name for _, directories, files in os.walk(out) for name in (*directories, *files)]
        self.assertFalse([name for name in names if name.startswith(".git")], "no Git metadata")
        manifest_text = (out / "SOURCE-MANIFEST.json").read_text()
        manifest = json.loads(manifest_text)
        self.assertEqual({k: v for k, v in manifest["components"][0].items() if k != "snapshot_sha256"}, {"name": "host", "destination": "qorqut-code", "commit": self.c1,
                                                     "tree": git(self.parent, "rev-parse", f"{self.c1}^{{tree}}")})
        self.assertEqual({key for component in manifest["components"] for key in component}, {"name", "destination", "commit", "tree", "snapshot_sha256"})
        for path in (self.parent, self.child, self.root):
            self.assertNotIn(str(path), manifest_text)
        for file in out.rglob("*"):
            if file.is_file() and not file.is_symlink():
                self.assertNotIn(str(self.root).encode(), file.read_bytes())
        self.assertEqual(len(git(self.parent, "status", "--porcelain").splitlines()), 2)     # source untouched: dirty and untracked remain

    def test_replacement_refs_do_not_change_the_exported_content(self):
        blob = git(self.parent, "rev-parse", f"{self.c1}:README.md")
        other = subprocess.run(["git", "-C", str(self.parent), "hash-object", "-w", "--stdin"], input="replaced\n",
                               capture_output=True, text=True, check=True).stdout.strip()
        git(self.parent, "replace", blob, other)
        self.assertEqual(git(self.parent, "show", f"{self.c1}:README.md"), "replaced")       # active for ordinary Git commands
        self.run_export(self.parent_and_child()[:1])
        self.assertEqual((self.root / "out/qorqut-code/README.md").read_text(), "one\n")

    def test_empty_existing_output_is_accepted_and_nonempty_is_refused(self):
        (self.root / "out").mkdir()
        self.run_export(self.parent_and_child()[:1])
        self.assertTrue((self.root / "out/qorqut-code/README.md").is_file())
        (self.root / "taken").mkdir()
        (self.root / "taken/file").write_text("x")
        self.run_export(self.parent_and_child()[:1], output=self.root / "taken", expect=2)
        self.assertEqual([p.name for p in (self.root / "taken").iterdir()], ["file"])

    def test_output_inside_input_repository_or_git_directory_is_refused(self):
        for output in (self.parent / "exported", self.parent / ".git" / "exported"):
            with self.subTest(output=output):
                self.run_export(self.parent_and_child()[:1], output=output, expect=2)
                self.assertFalse(output.exists())

    def test_non_commit_short_and_unsafe_inputs_are_refused(self):
        tree = git(self.parent, "rev-parse", f"{self.c1}^{{tree}}")
        base = self.parent_and_child()[0]
        for change in ({"commit": tree}, {"commit": self.c1[:12]}, {"commit": "0" * 40}, {"destination": "../escape"},
                       {"destination": "/abs"}, {"destination": "a/.git"}, {"repository": "relative/path"},
                       {"repository": str(self.parent / "bin")}):
            with self.subTest(change=change):
                self.run_export([{**base, **change}], expect=2)
                self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(("out", "."))), [])

    def test_nested_component_must_fill_an_empty_gitlink_location(self):
        base, nested = self.parent_and_child()
        for destination in ("qorqut-code/bin", "qorqut-code/README.md", "qorqut-code/missing"):
            with self.subTest(destination=destination):
                self.run_export([base, {**nested, "destination": destination}], expect=2)
                self.assertFalse((self.root / "out").exists())

    def test_escaping_and_absolute_symlinks_are_refused_but_in_tree_links_are_allowed(self):
        cases = [{"bad": "../outside"}, {"bad": "/etc/passwd"}, {"bad": "a/../../x"},
                 {"dir/up": "..", "dir/L2": "up/.."}]         # the last is lexically inside but resolves outside the component
        for number, links in enumerate(cases):
            with self.subTest(links=links):
                evil = self.repository(f"evil{number}", {"ok.txt": "fine\n", "dir/f": "x\n"})
                for name, target in links.items():
                    os.symlink(target, evil / name)
                self.run_export([self.component("evil", evil, commit_all(evil, "link"), "evil")], expect=2)
                self.assertFalse((self.root / "out").exists())
        good = self.repository("good", {"d/real.txt": "ok\n"})
        os.symlink("d/real.txt", good / "alias")
        self.run_export([self.component("good", good, commit_all(good, "link"), "good")])
        self.assertEqual((self.root / "out/good/alias").read_text(), "ok\n")


if __name__ == "__main__":
    unittest.main()
