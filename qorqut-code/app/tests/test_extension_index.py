"""The index of downloadable extensions: the generator (tools/distribution/extension_index.py) and the reader
(qorqut/extension_index.py)."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import yaml

from qorqut import extension_index
from qorqut.extension_index import ExtensionIndexError

TESTS = Path(__file__).resolve().parent
EXAMPLE = TESTS / "fixtures" / "extension-contract" / "network"
DISTRIBUTION = TESTS.parents[1] / "tools" / "distribution"
BASE = "https://example.invalid/releases/v1.7.0"


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"tool_{name}", DISTRIBUTION / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_tool("extension_bundle")
generator = load_tool("extension_index")


def entry(**changes):
    value = {"id": "network", "version": "1.0.0", "name": "Network", "summary": "Keeps your network.",
             "requires_qorqbot": ">=1.7.0", "platforms": ["linux", "mac"], "setup_kind": "none",
             "requires_extensions": [], "folder_sha256": "a" * 64,
             "bundles": [{"platform": "any", "url": BASE + "/network.tar.gz", "sha256": "b" * 64, "size": 10}]}
    value.update(changes)
    return value


def index(*entries, **changes):
    value = {"format": 1, "app_version": "1.7.0", "extensions": list(entries)}
    value.update(changes)
    return value


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def write(self, value, name="extensions-index.json"):
        path = self.root / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path


class ReaderTest(Case):
    def test_an_index_is_read(self):
        loaded = extension_index.load(self.write(index(entry())))
        self.assertEqual(loaded.app_version, "1.7.0")
        item = loaded.entry("network")
        self.assertEqual((item.version, item.platforms, item.setup_kind, item.requires_qorqbot),
                         ("1.0.0", ("linux", "mac"), "none", ">=1.7.0"))
        self.assertEqual(item.bundles[0].size, 10)
        self.assertIsNone(loaded.entry("other"))

    def test_entry_for_takes_the_platform_bundle_before_the_one_for_any(self):
        bundles = [{"platform": "any", "url": BASE + "/any.tar.gz", "sha256": "b" * 64, "size": 1},
                   {"platform": "mac-arm64", "url": BASE + "/mac.tar.gz", "sha256": "c" * 64, "size": 2}]
        loaded = extension_index.load(self.write(index(entry(bundles=bundles))))
        chosen, bundle = loaded.entry_for("network", "mac-arm64")
        self.assertEqual((chosen.id, bundle.url), ("network", BASE + "/mac.tar.gz"))
        self.assertEqual(loaded.entry_for("network", "linux-x86_64")[1].url, BASE + "/any.tar.gz")
        self.assertIsNone(loaded.entry_for("other", "mac-arm64"))
        only_mac = extension_index.load(self.write(index(entry(bundles=bundles[1:])), "mac.json"))
        self.assertIsNone(only_mac.entry_for("network", "linux-x86_64"))

    def test_an_empty_index_is_read(self):
        self.assertEqual(extension_index.load(self.write(index())).entries, ())

    def test_a_malformed_index_is_refused_with_its_reason(self):
        two = [{"platform": "any", "url": "u", "sha256": "b" * 64, "size": 1}] * 2
        cases = {
            "not json": ("{", "The extension index is not valid JSON"),
            "duplicate key": ('{"format": 1, "format": 1}', "Duplicate JSON key"),
            "not an object": ([], "The extension index must be a JSON object."),
            "missing field": ({"format": 1, "extensions": []}, "The extension index lacks 'app_version'."),
            "unknown field": (index(extra=1), "The extension index has the unknown field 'extra'."),
            "format": (index(format=2), "The extension index must have format 1."),
            "app version": (index(app_version="dev"), "app_version that is not three numbers"),
            "entry field": (index({**entry(), "more": 1}), "The index entry 1 has the unknown field 'more'."),
            "id": (index(entry(id="Network")), "The index entry 1 has an id that is not an extension id."),
            "version": (index(entry(version="1.0")), "The index entry 'network' has a version"),
            "specifier": (index(entry(requires_qorqbot="1.7")), "requires_qorqbot that is not comparisons"),
            "platforms": (index(entry(platforms=["linux", "linux"])), "must have platforms as a list"),
            "setup kind": (index(entry(setup_kind="password")), "has an unknown setup_kind."),
            "requires": (index(entry(requires_extensions=["Bad"])), "requires_extensions as a list"),
            "requires itself": (index(entry(requires_extensions=["network"])), "names itself in requires_extensions."),
            "requires twice": (index(entry(requires_extensions=["crm", "crm"])), "repeats an id in requires_extensions."),
            "folder digest": (index(entry(folder_sha256="x")), "folder_sha256 that is not"),
            "no bundles": (index(entry(bundles=[])), "must have bundles as a non-empty list."),
            "bundle sha": (index(entry(bundles=[{**entry()["bundles"][0], "sha256": "B" * 64}])),
                           "The index entry 'network', bundle 1 has a sha256"),
            "bundle size": (index(entry(bundles=[{**entry()["bundles"][0], "size": True}])), "has a size"),
            "bundle url": (index(entry(bundles=[{**entry()["bundles"][0], "url": ""}])), "url must be a non-empty"),
            "bundle platform": (index(entry(bundles=[{**entry()["bundles"][0], "platform": "ubuntu24-x86_64"}])),
                                "has a platform that is not any or a platform and processor"),
            "two per platform": (index(entry(bundles=two)), "has two bundles for one platform."),
            "repeated id": (index(entry(), entry()), "The extension index lists 'network' twice."),
        }
        for name, (value, reason) in cases.items():
            with self.subTest(name):
                with self.assertRaises(ExtensionIndexError) as caught:
                    extension_index.load(self.write(value))
                self.assertIn(reason, str(caught.exception))

    def test_a_missing_file_is_refused(self):
        with self.assertRaisesRegex(ExtensionIndexError, "cannot be read: FileNotFoundError"):
            extension_index.load(self.root / "absent.json")


class GeneratorTest(Case):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "extensions" / "network"
        shutil.copytree(EXAMPLE, self.folder)
        self.bundle = Path(builder.build_folder(self.folder, "any", self.root / "network-1.0.0.tar.gz")["path"])
        self.builtin = self.root / "extensions" / "kept"
        shutil.copytree(EXAMPLE, self.builtin)
        self.manifest(self.builtin, id="kept", delivery="built-in")

    def manifest(self, folder, **changes):
        path = folder / "extension.yaml"
        data = yaml.safe_load(path.read_text())
        data.update(changes)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def generate(self, folders=None, bundles=None):
        return generator.build_index(folders or [self.folder, self.builtin], bundles or [self.bundle], "1.7.0", BASE)

    def test_the_index_lists_downloadable_folders_with_their_bundles(self):
        value = self.generate()
        self.assertEqual([item["id"] for item in value["extensions"]], ["network"])
        item = value["extensions"][0]
        self.assertEqual((item["name"], item["version"], item["platforms"], item["setup_kind"]),
                         ("Network", "1.0.0", ["linux", "mac", "windows"], "none"))
        self.assertEqual(item["bundles"], [{"platform": "any", "url": BASE + "/network-1.0.0.tar.gz",
                                            "sha256": builder.contract.checksum(self.bundle),
                                            "size": self.bundle.stat().st_size}])
        output = self.root / "extensions-index.json"
        self.assertEqual(generator.main([str(self.folder), str(self.builtin), "--bundle", str(self.bundle),
                                         "--app-version", "1.7.0", "--base-url", BASE, "--output", str(output)]), 0)
        loaded = extension_index.load(output)
        self.assertEqual(loaded.entry_for("network", "linux-x86_64")[1].sha256, item["bundles"][0]["sha256"])
        self.assertEqual(json.loads(output.read_text()), self.generate())

    def test_the_folder_digest_ignores_tests(self):
        before = self.generate()["extensions"][0]["folder_sha256"]
        (self.folder / "tests").mkdir()
        (self.folder / "tests" / "test_network.py").write_text("")
        self.assertEqual(self.generate()["extensions"][0]["folder_sha256"], before)

    def test_two_ids_of_which_one_is_the_other_followed_by_more_are_refused(self):
        folders = []
        for ext_id in ("ab", "ab_c"):
            folders.append(self.root / "named" / ext_id)
            shutil.copytree(EXAMPLE, folders[-1])
            self.manifest(folders[-1], id=ext_id)
        with self.assertRaisesRegex(ValueError, "cannot be indexed together: the extension id ab_c is the id ab "
                                                "followed by _ and more"):
            generator.build_index(folders, [], "1.7.0", BASE)

    def test_a_downloadable_folder_without_a_bundle_is_refused(self):
        with self.assertRaisesRegex(ValueError, "network has no bundle"):
            generator.build_index([self.folder], [], "1.7.0", BASE)

    def test_a_bundle_of_a_changed_folder_is_refused(self):
        (self.folder / "README.md").write_text("Changed after the build.\n")
        with self.assertRaisesRegex(ValueError, "not built from the folder network as it is now"):
            self.generate()

    def test_a_bundle_of_another_version_is_refused(self):
        self.manifest(self.folder, version="1.0.1")
        with self.assertRaisesRegex(ValueError, "has the version 1.0.0, but the folder has 1.0.1"):
            self.generate()

    def test_a_bundle_without_its_folder_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not a given downloadable folder"):
            generator.build_index([self.builtin], [self.bundle], "1.7.0", BASE)

    def test_two_bundles_for_one_platform_are_refused(self):
        again = Path(builder.build_folder(self.folder, "any", self.root / "again.tar.gz")["path"])
        with self.assertRaisesRegex(ValueError, "Two bundles of network are for the platform any"):
            self.generate(bundles=[self.bundle, again])

    def test_a_broken_bundle_is_refused(self):
        broken = self.root / "broken.tar.gz"
        broken.write_bytes(b"not a bundle")
        with self.assertRaisesRegex(ValueError, "broken.tar.gz does not pass its check"):
            self.generate(bundles=[broken])


if __name__ == "__main__":
    unittest.main()
