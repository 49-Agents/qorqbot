"""Dependency wheels of a downloadable extension (``qorqut/bootstrap/extension_deps.py``): a real wheel built here, a
real bundle from ``build_folder``, a real staged install through ``apply_pending`` and a real start that imports it."""

import base64
import hashlib
import json
from pathlib import Path
import shutil
import sys
from unittest import mock
import zipfile

from qorqut import extension_index
from qorqut import extension_registry as registry
from qorqut.bootstrap import extension_change as change
from qorqut.bootstrap import extension_deps as deps
from qorqut.extensions import NO_EXTENSIONS

from test_extension_change import ChangeCase, builder, generator

DIST, VERSION = "qorqbot_tiny_dep", "1.0.0"

EXTENSION = '''"""Tinyuse: a test extension whose code imports a dependency that comes as a wheel in its bundle."""

import qorqbot_tiny_dep
import yaml

from qorqut.extensions import Extension


class Tinyuse(Extension):
    id = "tinyuse"

    def __init__(self):
        self.value = qorqbot_tiny_dep.VALUE
        self.dependency = qorqbot_tiny_dep.__file__
        self.yaml = yaml.__file__


def create(config=None):
    return Tinyuse()
'''

MANIFEST = """contract: 1
id: tinyuse
name: Tinyuse
summary: A test extension whose code needs a dependency wheel.
version: 1.0.0
delivery: downloadable
platforms: [linux, mac, windows]
requires:
  qorqbot: ">=1.7.0"
code:
  factory: qorqut_tinyuse.extension:create
setup:
  kind: none
agents:
  allowed: [ordubashy]
"""


def tiny_wheel(directory, requires=()):
    """A pure-Python wheel of one module, ``qorqbot_tiny_dep``, and of a module named ``yaml`` that must lose to the
    app's own: its RECORD covers its files with their checksums, as a wheel tool writes it."""
    info = f"{DIST}-{VERSION}.dist-info"
    files = {f"{DIST}/__init__.py": b"VALUE = 42\n",
             "yaml/__init__.py": b"SHADOW = True\n",
             f"{info}/METADATA": ("Metadata-Version: 2.1\nName: qorqbot-tiny-dep\nVersion: 1.0.0\n"
                                  + "".join(f"Requires-Dist: {item}\n" for item in requires)).encode(),
             f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    rows = [f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip('=')},{len(body)}"
            for name, body in files.items()] + [f"{info}/RECORD,,"]
    path = directory / f"{DIST}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        for name, body in files.items():
            wheel.writestr(name, body)
        wheel.writestr(f"{info}/RECORD", "\n".join(rows) + "\n")
    return path


class DepsCase(ChangeCase):
    def setUp(self):
        super().setUp()
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()

    def folder(self, requires=("qorqbot-tiny-dep==1.0.0",)):
        folder = self.release / "tinyuse-source" / "tinyuse"
        package = folder / "code" / "qorqut_tinyuse"
        package.mkdir(parents=True)
        (folder / "extension.yaml").write_text(MANIFEST)
        (folder / "README.md").write_text("# Tinyuse\n\nA test extension. It sends nothing anywhere and stores nothing.\n")
        (package / "__init__.py").write_text("")
        (package / "extension.py").write_text(EXTENSION)
        dependencies = ", ".join(json.dumps(item) for item in requires)
        (folder / "code" / "pyproject.toml").write_text(
            f'[project]\nname = "qorqut-tinyuse"\nversion = "1.0.0"\ndependencies = [{dependencies}]\n')
        (folder / "code" / "constraints.txt").write_text("qorqbot-tiny-dep==1.0.0\n")
        return folder

    def publish_tiny(self, wheel_requires=()):
        folder = self.folder()
        out = self.release / "tinyuse-out"
        out.mkdir()
        bundle = builder.build_folder(folder, "any", out / "tinyuse.tar.gz", [tiny_wheel(self.wheels, wheel_requires)])["path"]
        (out / extension_index.INDEX_FILE).write_text(json.dumps(generator.build_index([folder], [bundle], "1.7.0",
                                                                                       out.as_uri())))
        return extension_index.load(out / extension_index.INDEX_FILE)

    def stage_tiny(self, index):
        entry, bundle = index.entry_for("tinyuse", "linux-x86_64")
        return change.stage(self.paths, entry, bundle, requested_by="agent-ordubashy", restart=self.restart)

    def start(self):
        """What a start does with the code parts: find the folders, then load the fine ones."""
        found = self.discover(app_version="1.7.0")
        found, _selection, loaded = registry.load_code(found, NO_EXTENSIONS)
        for made in loaded.values():
            self.addCleanup(registry._forget, made.folder)
        return {item.id: item for item in found}, loaded


class InstallTest(DepsCase):
    def test_a_staged_install_makes_the_wheel_importable_at_the_next_start(self):
        self.assertStaged(self.stage_tiny(self.publish_tiny()))
        staged = change._staged_folder(self.paths, change.pending(self.paths)["sha256"], "tinyuse")
        self.assertTrue((staged / "deps" / "site" / DIST / "__init__.py").is_file())
        change.apply_pending(self.paths)
        self.assertEqual(change.pending(self.paths)["result"], {"state": change.DONE, "note": None})
        folder = self.downloaded / "tinyuse"
        site = folder / "deps" / "site"
        self.assertTrue((site / f"{DIST}-{VERSION}.dist-info" / "METADATA").is_file())

        app_entry = next(index for index, entry in enumerate(sys.path)
                         if (Path(entry) / "yaml" / "__init__.py").is_file())
        found, loaded = self.start()
        self.assertTrue(found["tinyuse"].fine, found["tinyuse"])
        made = loaded["tinyuse"].extension
        self.assertEqual(made.value, 42)
        self.assertTrue(Path(made.dependency).resolve().is_relative_to(site.resolve()))
        # Appended after code/ and after the app's own packages: the app's yaml wins over the wheel's.
        self.assertFalse(Path(made.yaml).resolve().is_relative_to(site.resolve()))
        self.assertGreater(sys.path.index(str(site.resolve())), sys.path.index(str((folder / "code").resolve())))
        self.assertGreater(sys.path.index(str((folder / "code").resolve())), app_entry)

        registry._forget(loaded["tinyuse"].folder)
        self.assertNotIn(str(site.resolve()), sys.path)
        self.assertNotIn(DIST, sys.modules)
        self.assertIn("yaml", sys.modules)

    def test_a_wheel_that_would_replace_an_app_distribution_fails_the_change_and_stages_nothing(self):
        index = self.publish_tiny()
        app = {**deps.app_distributions(), "qorqbot-tiny-dep": "1.0.0"}
        with mock.patch.object(deps, "app_distributions", lambda: app):
            outcome = self.stage_tiny(index)
        self.assertEqual(outcome.note, "Its dependency wheels do not fit this Qorqbot: the wheel "
                                       f"{DIST}-{VERSION}-py3-none-any.whl would replace qorqbot-tiny-dep, which comes "
                                       "with Qorqbot.")
        self.assertEqual((outcome.staged, outcome.kind, self.restarts, change.pending(self.paths)),
                         (False, "dependencies", [], None))
        self.assertEqual(list(change.staging(self.paths).iterdir()), [])

    def test_the_app_that_starts_judges_the_staged_wheels_again_and_keeps_the_old_folder(self):
        self.assertStaged(self.stage_tiny(self.publish_tiny(wheel_requires=["PyYAML>=6"])))
        with mock.patch.object(deps, "app_distributions", lambda: {}):
            change.apply_pending(self.paths)
        self.assertEqual(change.pending(self.paths)["result"], {
            "state": change.FAILED, "note": "Its dependency wheels do not fit this Qorqbot: it needs PyYAML>=6, which "
                                            "neither Qorqbot nor its wheels give."})
        self.assertFalse((self.downloaded / "tinyuse").exists())


class StartTest(DepsCase):
    def installed(self, **arguments):
        self.assertStaged(self.stage_tiny(self.publish_tiny(**arguments)))
        change.apply_pending(self.paths)
        return self.downloaded / "tinyuse"

    def test_after_an_app_update_wheels_that_no_longer_fit_make_it_unsupported(self):
        self.installed(wheel_requires=["PyYAML>=6"])
        with mock.patch.object(deps, "app_distributions", lambda: {}):
            found, loaded = self.start()
        self.assertEqual(found["tinyuse"].unsupported, "Its dependency wheels do not fit this Qorqbot: it needs PyYAML>=6, "
                                                       "which neither Qorqbot nor its wheels give.")
        self.assertNotIn("tinyuse", loaded)
        found, loaded = self.start()
        self.assertIsNone(found["tinyuse"].unsupported)
        self.assertIn("tinyuse", loaded)

    def test_a_folder_with_wheels_put_in_place_by_hand_is_refused(self):
        folder = self.installed()
        shutil.rmtree(folder / "deps" / "site")
        path_before = list(sys.path)
        found, loaded = self.start()
        self.assertEqual(found["tinyuse"].findings[-1], "Its dependency wheels are not installed. Qorqbot installs them "
                                                         "only when it installs the extension from the index.")
        self.assertEqual((loaded, sys.path), ({}, path_before))


class BuildTest(DepsCase):
    def test_the_builder_refuses_wheels_whose_requirements_nothing_gives(self):
        with self.assertRaisesRegex(ValueError, "it needs qorqbot-missing-dist>=1, which neither Qorqbot nor its wheels "
                                                "give"):
            builder.build_folder(self.folder(), "any", self.root / "tinyuse.tar.gz",
                                 [tiny_wheel(self.wheels, ["qorqbot-missing-dist>=1"])])

    def test_the_builder_refuses_code_requirements_nothing_gives(self):
        with self.assertRaisesRegex(ValueError, "it needs qorqbot-missing-dist==2, which"):
            builder.build_folder(self.folder(requires=("qorqbot-missing-dist==2",)), "any", self.root / "tinyuse.tar.gz",
                                 [tiny_wheel(self.wheels)])
