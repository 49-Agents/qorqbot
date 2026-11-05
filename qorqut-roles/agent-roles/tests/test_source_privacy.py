"""The packaged role sources carry no installation-specific account, address or path."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = ("README.md", "docs", "roles", "skills", "evaluations")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SYNTHETIC_DOMAINS = {"example.com", "example.org", "example.net"}


def packaged_files():
    for name in PACKAGED:
        path = ROOT / name
        yield from ([path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file()))


class SourcePrivacyTest(unittest.TestCase):
    def test_no_real_address_or_absolute_home_path_is_packaged(self):
        files = list(packaged_files())
        self.assertGreater(len(files), 30)
        for path in files:
            text = path.read_text()
            with self.subTest(path=str(path.relative_to(ROOT))):
                real = [match for match in EMAIL.findall(text) if match.rsplit("@", 1)[1].lower() not in SYNTHETIC_DOMAINS]
                self.assertEqual(real, [])
                self.assertNotIn("/home/", text)


if __name__ == "__main__":
    unittest.main()
