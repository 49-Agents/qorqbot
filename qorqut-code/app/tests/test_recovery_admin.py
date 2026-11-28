"""The recovery administration refuses a socket folder other users can reach."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from qorqut_operations.contracts import DomainError
from qorqut.storage import recovery_admin


class PrivateSocketParent(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        os.chmod(self.root, 0o700)

    def folder(self):
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        return descriptor

    def test_a_private_folder_is_accepted(self):
        recovery_admin._require_private_parent(self.folder())

    def test_a_folder_other_users_can_enter_is_refused(self):
        os.chmod(self.root, 0o755)
        with self.assertRaises(DomainError) as raised:
            recovery_admin._require_private_parent(self.folder())
        self.assertEqual(raised.exception.code, "invalid_configuration")

    @unittest.skipIf(os.name == "nt", "Windows has no permission bits")
    def test_a_path_that_is_not_a_folder_is_refused(self):
        file = self.root / "socket"
        file.write_text("")
        os.chmod(file, 0o700)                          # private, so only "not a folder" can refuse it
        descriptor = os.open(file, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        with self.assertRaises(DomainError):
            recovery_admin._require_private_parent(descriptor)


if __name__ == "__main__":
    unittest.main()
