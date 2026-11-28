"""The layer imports without touching the system and reports its version."""
import unittest

import qorqut_system


class PackageTest(unittest.TestCase):
    def test_version(self):
        self.assertEqual(qorqut_system.__version__, "0.1.0")

    def test_import_has_no_side_effects(self):
        self.assertEqual(sorted(qorqut_system.__all__), ["__version__"])