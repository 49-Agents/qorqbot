"""Host: this machine's mount table and the addresses it answers on."""
import ipaddress
import sys
import unittest

from qorqut_system import host
from qorqut_system.errors import UnsupportedSystem

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
# Another Unix system, a Mac for one, has no /proc to read them from, and reading it raises what it raised before.
ELSEWHERE = not WINDOWS and not LINUX
FIELDS = {"mount_id", "root", "mountpoint", "options", "filesystem", "source"}


class Mounts(unittest.TestCase):
    @unittest.skipUnless(LINUX, "the mount table is read on Linux only")
    def test_every_mount_carries_its_six_fields(self):
        found = host.mounts()
        self.assertTrue(found)
        for item in found:
            self.assertEqual(set(item), FIELDS)

    @unittest.skipUnless(LINUX, "the mount table is read on Linux only")
    def test_the_root_is_a_mountpoint_of_this_machine(self):
        self.assertIn("/", [item["mountpoint"] for item in host.mounts()])

    @unittest.skipUnless(LINUX, "the mount table is read on Linux only")
    def test_the_options_of_a_mount_are_a_list(self):
        for item in host.mounts():
            self.assertIsInstance(item["options"], list)
            self.assertTrue(item["options"])

    @unittest.skipUnless(WINDOWS, "the mount table is read there")
    def test_windows_refuses_the_mount_table(self):
        with self.assertRaises(UnsupportedSystem):
            host.mounts()

    @unittest.skipUnless(ELSEWHERE, "this system reads the mount table or refuses it")
    def test_another_unix_system_cannot_read_the_mount_table(self):
        with self.assertRaises(OSError):
            host.mounts()


class Addresses(unittest.TestCase):
    @unittest.skipUnless(LINUX, "the address list is read on Linux only")
    def test_the_loopback_address_is_one_of_this_machine_s_own(self):
        self.assertIn("127.0.0.1", host.local_addresses())

    @unittest.skipUnless(LINUX, "the address list is read on Linux only")
    def test_every_address_is_an_ip_address(self):
        for address in host.local_addresses():
            self.assertTrue(str(ipaddress.ip_address(address)))

    @unittest.skipUnless(WINDOWS, "the address list is read there")
    def test_windows_refuses_the_address_list(self):
        with self.assertRaises(UnsupportedSystem):
            host.local_addresses()

    @unittest.skipUnless(ELSEWHERE, "this system reads the address list or refuses it")
    def test_another_unix_system_cannot_read_the_address_list(self):
        with self.assertRaises(OSError):
            host.local_addresses()
