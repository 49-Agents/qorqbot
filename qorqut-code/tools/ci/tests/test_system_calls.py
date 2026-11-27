"""The system layer check: matches, messages, the list and the base branch comparison."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("system_calls", Path(__file__).resolve().parents[1] / "system_calls.py")
system_calls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(system_calls)


class Tree:
    """A temporary repository root holding exactly the files a test names."""

    def __init__(self, files):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.folder.cleanup()

    def list(self, files):
        path = self.root / system_calls.LIST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": 1, "files": files}))


class Matches(unittest.TestCase):
    def test_a_lock_is_found_with_its_line_and_service(self):
        found = system_calls.findings("import fcntl\n\nfcntl.flock(handle, fcntl.LOCK_EX)\n")
        self.assertEqual([(kind, line) for kind, line, *_ in found],
                         [("system_module", 1), ("file_lock", 3)])
        self.assertEqual(found[1][3:], ("locks", "qorqut_system.locks"))

    def test_a_system_test_is_found(self):
        found = system_calls.findings("import os\nif os.name == 'nt':\n    pass\n")
        self.assertEqual([(kind, line) for kind, line, *_ in found], [("platform_check", 2)])

    def test_permission_bits_are_found(self):
        found = system_calls.findings("if info.st_mode & 0o077: pass\n")
        self.assertEqual([(kind, line) for kind, line, *_ in found], [("permission_bits", 1)])


class Rules(unittest.TestCase):
    def check(self, files, listed):
        with Tree(files) as tree:
            if listed is not None:
                tree.list(listed)
            records, scanned = system_calls.observe(tree.root)
            return system_calls.compare(listed or {}, records, scanned)

    def test_a_new_file_with_a_lock_fails_with_the_message(self):
        messages = self.check({"qorqut-code/app/qorqut/new.py": "import fcntl\nfcntl.flock(handle, 2)\n"}, {})
        self.assertEqual(len(messages), 3)
        self.assertIn("qorqut-code/app/qorqut/new.py: new system-specific code", messages[0])
        self.assertIn("qorqut-code/app/qorqut/new.py:1: an import of fcntl", messages[1])
        self.assertIn("qorqut-code/app/qorqut/new.py:2: a file lock outside the system layer", messages[2])
        self.assertIn("Use locks in the system layer (qorqut_system.locks)", messages[2])
        self.assertIn("a missing operation is added to the layer first", messages[2])

    def test_an_exact_list_holds(self):
        files = {"qorqut-code/app/qorqut/old.py": "import fcntl\nfcntl.flock(handle, 2)\n".replace("import fcntl\n", "")}
        self.assertEqual(self.check(files, {"qorqut-code/app/qorqut/old.py": {"file_lock": 1}}), [])

    def test_a_raised_number_is_refused(self):
        files = {"qorqut-code/app/qorqut/old.py": "fcntl.flock(a, 2)\nfcntl.flock(b, 2)\n"}
        messages = self.check(files, {"qorqut-code/app/qorqut/old.py": {"file_lock": 1}})
        self.assertEqual(len(messages), 1)
        self.assertIn("the list says", messages[0])
        self.assertIn("Lower the list in the same change", messages[0])

    def test_a_converted_file_leaves_the_list(self):
        files = {"qorqut-code/app/qorqut/old.py": "from qorqut_system import locks\n"}
        self.assertEqual(self.check(files, {}), [])

    def test_a_listed_file_with_no_match_must_lose_its_entry(self):
        messages = self.check({"qorqut-code/app/qorqut/old.py": "x = 1\n"},
                              {"qorqut-code/app/qorqut/old.py": {"file_lock": 1}})
        self.assertEqual(len(messages), 1)
        self.assertIn("no match", messages[0])

    def test_a_listed_file_that_is_gone_must_lose_its_entry(self):
        messages = self.check({"qorqut-code/app/qorqut/other.py": "x = 1\n"},
                              {"qorqut-code/app/qorqut/old.py": {"file_lock": 1}})
        self.assertEqual(len(messages), 1)
        self.assertIn("no longer read", messages[0])

    def test_tests_and_the_layer_are_not_read(self):
        files = {"qorqut-code/app/qorqut/tests/test_x.py": "fcntl.flock(a, 2)\n",
                 "qorqut-code/app/qorqut/conftest.py": "fcntl.flock(a, 2)\n",
                 "qorqut-system/src/qorqut_system/locks.py": "fcntl.flock(a, 2)\n"}
        self.assertEqual(self.check(files, {}), [])


class Base(unittest.TestCase):
    def test_a_change_may_only_lower_numbers_and_remove_files(self):
        base = {"a.py": {"file_lock": 3}, "gone.py": {"permission_bits": 2}}
        current = {"a.py": {"file_lock": 2}}
        self.assertEqual(system_calls.compare_base(current, base), [])

    def test_a_raised_number_is_refused_against_the_base_branch(self):
        messages = system_calls.compare_base({"a.py": {"file_lock": 4}}, {"a.py": {"file_lock": 3}})
        self.assertEqual(len(messages), 1)
        self.assertIn("rises from 3 to 4", messages[0])

    def test_a_new_file_is_refused_against_the_base_branch(self):
        messages = system_calls.compare_base({"new.py": {"file_lock": 1}}, {})
        self.assertEqual(len(messages), 1)
        self.assertIn("may not add a file", messages[0])

    def test_a_kind_that_did_not_exist_before_counts_as_a_raise(self):
        messages = system_calls.compare_base({"a.py": {"file_lock": 1, "platform_check": 1}}, {"a.py": {"file_lock": 1}})
        self.assertEqual(len(messages), 1)
        self.assertIn("platform_check", messages[0])


class Command(unittest.TestCase):
    def run_check(self, root):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = system_calls.main(["--root", str(root)])
        return status, output.getvalue()

    def test_the_check_passes_on_a_clean_tree(self):
        with Tree({"qorqut-code/app/qorqut/ok.py": "x = 1\n"}) as tree:
            tree.list({})
            status, output = self.run_check(tree.root)
        self.assertEqual(status, 0)
        self.assertIn("The system layer rule holds", output)

    def test_the_check_fails_on_a_trial_change(self):
        with Tree({"qorqut-code/app/qorqut/added.py": "import fcntl\n", "qorqut-code/app/qorqut/ok.py": "x = 1\n"}) as tree:
            tree.list({})
            status, output = self.run_check(tree.root)
        self.assertEqual(status, 1)
        self.assertIn("qorqut-code/app/qorqut/added.py:1: an import of fcntl", output)

    def test_a_missing_list_is_a_build_error(self):
        with Tree({"qorqut-code/app/qorqut/ok.py": "x = 1\n"}) as tree:
            status, _ = self.run_check(tree.root)
        self.assertEqual(status, 2)

    def test_the_recorded_list_is_a_format_one_record(self):
        path = Path(__file__).resolve().parents[1] / "system_calls_list.json"
        value = json.loads(path.read_text())
        self.assertEqual(value["format"], 1)
        self.assertTrue(value["files"])
        self.assertTrue(all(isinstance(counts, dict) and counts for counts in value["files"].values()))
        self.assertIn("qorqut-code/dashboard/pty_broker.py", value["files"])


if __name__ == "__main__":
    unittest.main()