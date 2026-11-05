import unittest
from unittest import mock

from bazarlyk import process_info


class Process:
    """A psutil.Process stand-in whose terminal() reads the (possibly stale) device map."""

    def __init__(self, device_map):
        self.device_map = device_map

    def terminal(self):
        return self.device_map.get("ttys035")


class TerminalTest(unittest.TestCase):
    def test_a_terminal_opened_after_the_map_was_cached_is_found(self):
        devices = {}
        clear = mock.Mock(side_effect=lambda: devices.update({"ttys035": "/dev/ttys035"}))
        with mock.patch("psutil._psposix.get_terminal_map") as terminal_map:
            terminal_map.cache_clear = clear
            self.assertEqual(process_info._terminal(Process(devices)), "/dev/ttys035")
        clear.assert_called_once()

    def test_a_known_terminal_does_not_rebuild_the_map(self):
        with mock.patch("psutil._psposix.get_terminal_map") as terminal_map:
            self.assertEqual(process_info._terminal(Process({"ttys035": "/dev/ttys035"})), "/dev/ttys035")
        terminal_map.cache_clear.assert_not_called()

    def test_no_terminal_stays_none(self):
        with mock.patch("psutil._psposix.get_terminal_map"):
            self.assertIsNone(process_info._terminal(Process({})))


if __name__ == "__main__":
    unittest.main()
