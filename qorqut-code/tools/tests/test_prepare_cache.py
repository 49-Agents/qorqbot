"""Wheel reuse must follow source bytes and reject damaged artifacts."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location("prepare_local", Path(__file__).resolve().parents[1] / "prepare-local.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class WheelReuse(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cache = self.root / "cache"
        self.source = self.root / "export"
        (self.source / "pkg").mkdir(parents=True)
        (self.source / "pkg/resource.md").write_text("first")
        self.builds = 0
        self.runs = 0

    def prepare(self, cache=True):
        self.runs += 1
        output = self.root / f"prepared-{self.runs}"
        preparer = prepare.Preparer(self.source, output, False, False, wheel_cache=self.cache if cache else None)
        preparer.src = self.source
        preparer.wheels.mkdir(parents=True)

        def build(_python, _key, relative):
            self.builds += 1
            wheel = preparer.wheels / "qorqut_example-1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("qorqut_example/resource.md", (self.source / relative / "resource.md").read_bytes())
                archive.writestr("qorqut_example-1.0.dist-info/METADATA", "Name: qorqut-example\nVersion: 1.0\n")
            return wheel

        with patch.object(preparer, "build_wheel", side_effect=build):
            preparer.build("unused-python", "example", "pkg")
        return preparer

    def test_unchanged_content_reuses_wheel_and_reads_metadata(self):
        first = self.prepare()
        second = self.prepare()
        self.assertEqual(self.builds, 1)
        self.assertEqual(second.reused, {"example"})
        self.assertEqual(second.meta["example"]["Name"], "qorqut-example")
        self.assertEqual(first.built["example"].read_bytes(), second.built["example"].read_bytes())
        second.plan_first_party()
        second.check_first_party("venv", ["example"])

    def test_changed_resource_invalidates_even_with_same_size_and_timestamp(self):
        self.prepare()
        resource = self.source / "pkg/resource.md"
        previous = resource.stat()
        resource.write_text("other")
        prepare.os.utime(resource, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        changed = self.prepare()
        self.assertEqual(self.builds, 2)
        self.assertFalse(changed.reused)
        with zipfile.ZipFile(changed.built["example"]) as archive:
            self.assertEqual(archive.read("qorqut_example/resource.md"), b"other")

    def test_mode_link_and_interpreter_changes_invalidate(self):
        self.prepare()
        (self.source / "pkg/resource.md").chmod(0o755)
        self.prepare()
        (self.source / "pkg/alias.md").symlink_to("resource.md")
        self.prepare()
        with patch.object(prepare.sys, "version", "different Python build"):
            self.prepare()
        self.assertEqual(self.builds, 4)

    def test_damaged_wheel_or_unsafe_receipt_is_a_miss(self):
        self.prepare()
        wheel = next(self.cache.glob("*/*.whl"))
        wheel.write_bytes(b"broken")
        self.assertFalse(self.prepare().reused)
        receipt = next(self.cache.glob("*/wheel.json"))
        record = json.loads(receipt.read_text())
        record["file"] = "../escape-py3-none-any.whl"
        receipt.write_text(json.dumps(record))
        self.assertFalse(self.prepare().reused)
        self.assertEqual(self.builds, 3)

    def test_disabled_or_unwritable_cache_still_builds(self):
        self.prepare(cache=False)
        self.assertFalse(self.cache.exists())
        self.cache.write_text("not a directory")
        self.assertFalse(self.prepare().reused)
        self.assertEqual(self.builds, 2)


if __name__ == "__main__":
    unittest.main()
