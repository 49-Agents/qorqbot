"""Tools that run before any package is installed start with the standard library only."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[4]
TOOLS = ("qorqut-code/tools/export-source.py",
         "qorqut-code/tools/export-unified.py",
         "qorqut-code/tools/prepare-local.py",
         "qorqut-code/app/tools/check_installed_host.py",
         "qorqut-code/tools/distribution/runtime_source.py")


class ToolsStart(unittest.TestCase):
    def test_each_tool_prints_its_help_without_any_package(self):
        # No PYTHONPATH, no site-packages: the tools load their snapshot file by path and must not need the layer.
        environment = {name: value for name, value in os.environ.items() if name not in ("PYTHONPATH", "PYTHONHOME")}
        for tool in TOOLS:
            with self.subTest(tool=tool):
                done = subprocess.run([sys.executable, "-I", "-S", str(ROOT / tool), "--help"], cwd=ROOT,
                                      env=environment, capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr[-800:])
                self.assertIn("usage", done.stdout.lower())


if __name__ == "__main__":
    unittest.main()
