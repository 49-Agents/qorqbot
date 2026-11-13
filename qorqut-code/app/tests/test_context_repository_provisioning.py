"""The Context repository is created only by explicit provisioning, and an existing one is never touched."""
import tempfile
import unittest
from pathlib import Path

from qorqut.bootstrap.context_repository import main, provision_context_repository
from qorqut.instance_paths import InstancePaths


class ContextRepositoryProvisioningTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp()).resolve()
        self.component = base / "context-engine"
        self.component.mkdir()
        self.instance = base / "instance"
        (self.instance / "operations").mkdir(parents=True, mode=0o700)
        self.paths = InstancePaths(self.instance, {"context_engine": self.component})

    def test_creates_the_repository_and_runtime_once(self):
        result = provision_context_repository(self.paths)
        self.assertTrue(result.created)
        self.assertEqual(result.path, self.paths.context)
        self.assertEqual(len(result.revision), 40)
        self.assertTrue((self.paths.context / ".git").exists())
        self.assertTrue(self.paths.context_runtime.is_dir())
        again = provision_context_repository(self.paths)
        self.assertFalse(again.created)
        self.assertIsNone(again.revision)

    def test_existing_path_is_left_exactly_as_found(self):
        self.paths.context.mkdir()
        (self.paths.context / "keep.txt").write_text("mine")
        self.assertFalse(provision_context_repository(self.paths).created)
        self.assertEqual([p.name for p in self.paths.context.iterdir()], ["keep.txt"])
        self.assertFalse(self.paths.context_runtime.exists())

    def test_command_creates_then_leaves_alone(self):
        arguments = ["--instance-root", str(self.instance), "--component", f"context_engine={self.component}"]
        self.assertEqual(main(arguments), 0)
        self.assertTrue((self.paths.context / ".git").exists())
        self.assertEqual(main(arguments), 0)


if __name__ == "__main__":
    unittest.main()
