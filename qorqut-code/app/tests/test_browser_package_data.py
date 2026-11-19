"""The browser package's scripts reach an installed copy.

The tests run against the source tree, where every file is present, so a script missing from ``package-data`` passes
here and fails only once installed: the captcha command crashed on an installed release for want of ``captcha.js``."""

import tomllib
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]


class PackageDataTest(unittest.TestCase):
    def test_every_script_the_browser_package_reads_is_installed_with_it(self):
        declared = tomllib.loads((APP / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["qorqut.browser"]
        scripts = sorted(path.name for path in (APP / "qorqut" / "browser").glob("*.js"))
        self.assertTrue(scripts)
        self.assertEqual(sorted(declared), scripts)


if __name__ == "__main__":
    unittest.main()
