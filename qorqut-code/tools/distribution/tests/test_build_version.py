import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from build_version import build_version  # noqa: E402


def semver_key(version):
    """SemVer precedence for these versions: numeric identifiers compare as numbers, others as text."""
    core, _, pre = version.partition("-")
    parts = [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")]
    return tuple(int(n) for n in core.split(".")), parts


class BuildVersionTest(unittest.TestCase):
    def test_later_builds_sort_later_whatever_their_hash(self):
        earlier = build_version("1.3.3", "local", "fad178051f1a" + "0" * 28, 1_793_000_000)
        later = build_version("1.3.3", "local", "0ea2f355b09e" + "0" * 28, 1_793_000_060)
        self.assertEqual(earlier, "1.3.3-local.20261026073320.fad178051f1a")
        self.assertLess(semver_key(earlier), semver_key(later))

    def test_bad_inputs_are_refused(self):
        for args in (("1.3", "local", "a" * 12), ("1.3.3", "Local", "a" * 12), ("1.3.3", "local", "xyz")):
            with self.assertRaises(ValueError):
                build_version(*args, 0)


if __name__ == "__main__":
    unittest.main()
