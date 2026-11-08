import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from build_version import build_version  # noqa: E402

# The regular expression semver.org publishes for a valid Semantic Version.
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
                    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$")
ELECTRON = Path(__file__).resolve().parents[1] / "macos_app" / "electron"


def semver_key(version):
    """SemVer precedence for these versions: numeric identifiers compare as numbers and rank below text, text as text."""
    core, _, pre = version.partition("-")
    parts = [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")]
    return tuple(int(n) for n in core.split(".")), parts


def semver_module():
    """The ``semver`` package the Mac app updates with: its own install, else the copy that ships inside npm. None if neither."""
    node = shutil.which("node")
    if node is None:
        return None
    for root in (ELECTRON / "node_modules", Path(node).resolve().parents[1] / "lib/node_modules/npm/node_modules"):
        if (root / "semver" / "package.json").is_file():
            return root / "semver"
    return None


class BuildVersionTest(unittest.TestCase):
    def test_the_version_names_the_time_then_the_commit(self):
        version = build_version("1.3.3", "local", "fad178051f1a" + "0" * 28, 1_793_000_000)
        self.assertEqual(version, "1.3.3-local.t20261026073320.gfad178051f1a")
        self.assertRegex(version, SEMVER)

    def test_later_builds_sort_later_whatever_their_hash(self):
        earlier = build_version("1.3.3", "local", "fad178051f1a" + "0" * 28, 1_793_000_000)
        later = build_version("1.3.3", "local", "0ea2f355b09e" + "0" * 28, 1_793_000_060)
        self.assertLess(semver_key(earlier), semver_key(later))

    def test_builds_sort_chronologically_across_every_field_width(self):
        seconds = [0, 1, 59, 60, 3600, 86_399, 86_400, 951_782_400, 1_793_000_000, 1_793_000_001, 1_793_635_199, 1_793_635_200,
                   4_102_444_799]
        hashes = ["f" * 12, "0" * 12, "9" * 12, "a1b2c3d4e5f6", "000000000001"]
        versions = [build_version("1.3.3", "check", hashes[index % len(hashes)] + "0" * 28, value) for index, value in enumerate(seconds)]
        for version in versions:
            self.assertRegex(version, SEMVER)
        self.assertEqual(sorted(versions, key=semver_key), versions)
        self.assertEqual(len({len(version) for version in versions}), 1)

    def test_a_build_is_greater_than_the_earlier_build_formats(self):
        # Earlier builds were BASE-CHANNEL.SHA12 (a hexadecimal identifier), then BASE-CHANNEL.YYYYMMDDHHMMSS.SHA12.
        current = build_version("1.3.3", "check", "0ea2f355b09e" + "0" * 28, 1_793_000_000)
        for sha in ("fad178051f1a", "ffffffffffff", "9fffffffffff", "0ea2f355b09e", "a" * 12, "123456789012", "999999999999"):
            self.assertLess(semver_key(f"1.3.3-check.{sha}"), semver_key(current), sha)
        self.assertLess(semver_key("1.3.3-check.20261026073320.fad178051f1a"), semver_key(current))
        self.assertLess(semver_key("1.3.3-check.99991231235959.ffffffffffff"), semver_key(current))

    def test_a_hash_made_only_of_digits_is_still_a_valid_version(self):
        for sha in ("012345678901", "000000000000", "123456789012", "100000000000"):
            version = build_version("1.3.3", "check", sha + "0" * 28, 1_793_000_000)
            self.assertRegex(version, SEMVER, version)
            self.assertEqual(version, f"1.3.3-check.t20261026073320.g{sha}")
            # Without a letter the hash would be a numeric identifier, which SemVer refuses with a leading zero.
            self.assertEqual(bool(SEMVER.match(f"1.3.3-check.t20261026073320.{sha}")), not sha.startswith("0"))
        earlier = build_version("1.3.3", "check", "012345678901" + "0" * 28, 1_793_000_000)
        later = build_version("1.3.3", "check", "0ea2f355b09e" + "0" * 28, 1_793_000_001)
        self.assertLess(semver_key(earlier), semver_key(later))

    def test_bad_inputs_are_refused(self):
        for args in (("1.3", "local", "a" * 12), ("1.3.3", "Local", "a" * 12), ("1.3.3", "local", "xyz"),
                     ("01.3.3", "local", "a" * 12), ("1.3.3", "1local", "a" * 12), ("1.3.3", "local", "a" * 11)):
            with self.assertRaises(ValueError):
                build_version(*args, 0)

    def test_the_mac_app_semver_orders_old_and_new_versions(self):
        module = semver_module()
        if module is None:
            self.skipTest("No semver package is installed to compare with")
        old = ["1.3.3-check.fad178051f1a", "1.3.3-check.ffffffffffff", "1.3.3-check.123456789012", "1.3.3-check.0ea2f355b09e",
               "1.3.3-check.20261026073320.fad178051f1a"]
        new = [build_version("1.3.3", "check", sha + "0" * 28, seconds)
               for sha, seconds in (("fad178051f1a", 1_793_000_000), ("0ea2f355b09e", 1_793_000_060), ("012345678901", 1_793_000_061),
                                    ("000000000000", 1_793_000_062), ("ffffffffffff", 1_800_000_000))]
        script = ("const semver = require(process.argv[1]); const [old, fresh] = JSON.parse(process.argv[2]);"
                  "const bad = fresh.filter(v => semver.valid(v) !== v);"
                  "const downgrades = []; for (const a of old) for (const b of fresh) if (!semver.gt(b, a)) downgrades.push([b, a]);"
                  "const order = fresh.slice(1).filter((v, i) => !semver.gt(v, fresh[i]));"
                  "console.log(JSON.stringify({bad, downgrades, order, sorted: semver.sort([...fresh].reverse())}));")
        home = tempfile.mkdtemp(prefix="semver-check ")
        self.addCleanup(shutil.rmtree, home, True)
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "XDG_CONFIG_HOME": home, "CLAUDE_CONFIG_DIR": home, "CODEX_HOME": home}
        done = subprocess.run(["node", "-e", script, str(module), json.dumps([old, new])], capture_output=True, text=True,
                              env=env, timeout=60, check=True)
        found = json.loads(done.stdout)
        self.assertEqual((found["bad"], found["downgrades"], found["order"]), ([], [], []))
        self.assertEqual(found["sorted"], new)


if __name__ == "__main__":
    unittest.main()
