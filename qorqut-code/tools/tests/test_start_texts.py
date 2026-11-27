"""The start text tool resolves the default agents and records the same text twice."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("start_texts", Path(__file__).resolve().parents[1] / "start_texts.py")
start_texts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start_texts)

REPOSITORY = Path(__file__).resolve().parents[3]


def commit():
    try:
        return subprocess.run(["git", "-C", str(REPOSITORY), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


class Comparison(unittest.TestCase):
    def record(self, hash_value, count=10):
        return {"format": 1, "agents": {"ordubashy": {"text_hash": hash_value, "text_characters": count}}}

    def test_the_same_text_is_accepted(self):
        self.assertEqual(start_texts.compare(self.record("sha256:a"), self.record("sha256:a")), [])

    def test_a_changed_text_is_reported(self):
        differences = start_texts.compare(self.record("sha256:a"), self.record("sha256:b"))
        self.assertEqual(len(differences), 1)
        self.assertIn("ordubashy: the start text changed", differences[0])

    def test_a_changed_size_is_reported(self):
        differences = start_texts.compare(self.record("sha256:a"), self.record("sha256:a", count=11))
        self.assertEqual(len(differences), 1)
        self.assertIn("size changed from 10 to 11", differences[0])

    def test_an_agent_present_on_one_side_is_reported(self):
        head = {"format": 1, "agents": {}}
        self.assertEqual(len(start_texts.compare(self.record("sha256:a"), head)), 1)


class Recording(unittest.TestCase):
    def setUp(self):
        if commit() is None:
            self.skipTest("needs a Git checkout of the repository")

    def test_two_installations_of_one_commit_have_the_same_start_text(self):
        with tempfile.TemporaryDirectory() as work:
            first = start_texts.record(REPOSITORY, commit(), Path(work) / "first.json", work)
            second = start_texts.record(REPOSITORY, commit(), Path(work) / "second.json", work)
        self.assertEqual(sorted(first["agents"]), ["learning-applier", "ordubashy"])
        self.assertEqual(start_texts.compare(first, second), [])


if __name__ == "__main__":
    unittest.main()
