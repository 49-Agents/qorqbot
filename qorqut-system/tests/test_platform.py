"""The layer's system question: the family of the machine, its own name, its machine name and the kind of host."""

import platform as _system
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_system import platform


class FamilyTest(unittest.TestCase):
    def test_the_answer_is_one_of_the_four_families(self):
        self.assertIn(platform.family(), platform.FAMILIES)

    def test_each_system_platform_maps_to_its_family(self):
        cases = {
            "linux": "linux",
            "linux2": "linux",
            "darwin": "mac",
            "win32": "windows",
            "cygwin": "windows",
            "aix": "other",
        }
        for system, family in cases.items():
            with self.subTest(system=system), mock.patch.object(sys, "platform", system):
                self.assertEqual(platform.family(), family)


class MachineTest(unittest.TestCase):
    def test_the_answer_is_the_platform_machine_value(self):
        self.assertEqual(platform.machine(), _system.machine())


class SystemNameTest(unittest.TestCase):
    def test_the_answer_is_the_name_the_system_gives_itself(self):
        self.assertEqual(platform.system_name(), _system.system())


class HostKindTest(unittest.TestCase):
    def test_the_answer_is_one_of_the_names_the_views_know(self):
        expected = ("windows",) if sys.platform == "win32" else ("darwin", "wsl", "linux")
        self.assertIn(platform.host_kind(), expected)

    @unittest.skipIf(sys.platform == "win32", "Windows answers windows before the kernel text is read")
    def test_a_wsl_kernel_is_reported_from_the_file_the_version_is_read_from(self):
        with tempfile.TemporaryDirectory() as directory:
            version = Path(directory) / "version"
            version.write_text("Linux version 5.15.153.1-microsoft-standard-WSL2")
            with mock.patch.object(platform.os, "uname", return_value=SimpleNamespace(sysname="Linux")):
                self.assertEqual(platform.host_kind(str(version)), "wsl")
            version.write_text("Linux version 6.8.0-generic")
            with mock.patch.object(platform.os, "uname", return_value=SimpleNamespace(sysname="Linux")):
                self.assertEqual(platform.host_kind(str(version)), "linux")
            with mock.patch.object(platform.os, "uname", return_value=SimpleNamespace(sysname="Linux")):
                self.assertEqual(platform.host_kind(str(Path(directory) / "missing")), "linux")

    @unittest.skipIf(sys.platform == "win32", "Windows answers windows before the kernel text is read")
    def test_a_darwin_kernel_is_named_darwin(self):
        with mock.patch.object(platform.os, "uname", return_value=SimpleNamespace(sysname="Darwin")):
            self.assertEqual(platform.host_kind(), "darwin")


if __name__ == "__main__":
    unittest.main()