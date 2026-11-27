"""Dependency wheels of a downloadable extension (``qorqut/bootstrap/extension_deps.py``): real wheels built here, real
bundles from ``build_folder``, real staged installs through ``apply_pending`` and real starts that import them."""

import base64
import hashlib
import io
import json
from pathlib import Path, PureWindowsPath
import shutil
import sys
import tarfile
from unittest import mock
import zipfile

from qorqut import extension_index
from qorqut import extension_registry as registry
from qorqut.bootstrap import extension_bundle as bundles
from qorqut.bootstrap import extension_change as change
from qorqut.bootstrap import extension_deps as deps
from qorqut.extensions import NO_EXTENSIONS

from test_extension_change import ChangeCase, builder, generator

DIST, VERSION = "qorqbot_tiny_dep", "1.0.0"
UNFIT = "Its dependency wheels do not fit this Qorqbot: "

EXTENSION = '''"""{name}: a test extension whose code imports a dependency that comes as a wheel in its bundle."""

import {imports}
import yaml

from qorqut.extensions import Extension


class Made(Extension):
    id = "{id}"

    def __init__(self):
        self.value = {imports}.VALUE
        self.dependency = {imports}.__file__
        self.yaml = yaml.__file__


def create(config=None):
    return Made()
'''

MANIFEST = """contract: 1
id: {id}
name: {name}
summary: A test extension whose code needs a dependency wheel.
version: 1.0.0
delivery: downloadable
platforms: [linux, mac, windows]
requires:
  qorqbot: ">=1.7.0"
code:
  factory: qorqut_{id}.extension:create
setup:
  kind: none
agents:
  allowed: [ordubashy]
"""


def record(name, body):
    return f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip('=')},{len(body)}"


def make_wheel(directory, name="qorqbot-tiny-dep", version=VERSION, *, requires=(), tag="py3-none-any",
               requires_python=None, files=None, extra=None, value=42):
    """A pure-Python wheel: its module (``qorqbot_tiny_dep`` by default, with ``VALUE``), a module named ``yaml`` that
    must lose to the app's own, and ``extra`` files; its RECORD covers every file with its checksum, as a wheel tool
    writes it."""
    stem = name.replace("-", "_")
    info = f"{stem}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += f"Requires-Python: {requires_python}\n" if requires_python else ""
    metadata += "".join(f"Requires-Dist: {item}\n" for item in requires)
    files = files if files is not None else {f"{stem}/__init__.py": f"VALUE = {value}\n".encode(),
                                             "yaml/__init__.py": b"SHADOW = True\n"}
    files = {**files, **(extra or {}), f"{info}/METADATA": metadata.encode(),
             f"{info}/WHEEL": f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: {tag}\n".encode()}
    rows = [record(member, body) for member, body in files.items()] + [f"{info}/RECORD,,"]
    path = directory / f"{stem}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as wheel:
        for member, body in files.items():
            wheel.writestr(member, body)
        wheel.writestr(f"{info}/RECORD", "\n".join(rows) + "\n")
    return path


class DepsCase(ChangeCase):
    def setUp(self):
        super().setUp()
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        self.made = 0

    def folder(self, ext_id="tinyuse", requires=("qorqbot-tiny-dep==1.0.0",), pins=("qorqbot-tiny-dep==1.0.0",),
               imports=DIST):
        folder = self.release / f"{ext_id}-source" / ext_id
        package = folder / "code" / f"qorqut_{ext_id}"
        package.mkdir(parents=True)
        name = ext_id.capitalize()
        (folder / "extension.yaml").write_text(MANIFEST.format(id=ext_id, name=name))
        (folder / "README.md").write_text(f"# {name}\n\nA test extension. It sends nothing anywhere and stores nothing.\n")
        (package / "__init__.py").write_text("")
        (package / "extension.py").write_text(EXTENSION.format(id=ext_id, name=name, imports=imports))
        dependencies = ", ".join(json.dumps(item) for item in requires)
        (folder / "code" / "pyproject.toml").write_text(
            f'[project]\nname = "qorqut-{ext_id}"\nversion = "1.0.0"\ndependencies = [{dependencies}]\n')
        (folder / "code" / "constraints.txt").write_text("".join(f"{pin}\n" for pin in pins))
        return folder

    def publish_tiny(self, wheel_requires=(), *, ext_id="tinyuse", wheels=None, **folder):
        self.made += 1
        wheel_folder = self.wheels / str(self.made)
        wheel_folder.mkdir()
        folder = self.folder(ext_id, **folder)
        out = self.release / f"{ext_id}-out"
        out.mkdir()
        given = wheels(wheel_folder) if wheels else [make_wheel(wheel_folder, requires=wheel_requires)]
        bundle = builder.build_folder(folder, "any", out / f"{ext_id}.tar.gz", given)["path"]
        (out / extension_index.INDEX_FILE).write_text(json.dumps(generator.build_index([folder], [bundle], "1.7.0",
                                                                                       out.as_uri())))
        return extension_index.load(out / extension_index.INDEX_FILE)

    def stage_tiny(self, index, ext_id="tinyuse"):
        entry, bundle = index.entry_for(ext_id, "linux-x86_64")
        return change.stage(self.paths, entry, bundle, requested_by="agent-ordubashy", restart=self.restart)

    def installed(self, ext_id="tinyuse", **arguments):
        self.assertStaged(self.stage_tiny(self.publish_tiny(ext_id=ext_id, **arguments), ext_id))
        change.apply_pending(self.paths)
        self.assertEqual(change.pending(self.paths)["result"]["state"], change.DONE)
        change.change_path(self.paths).unlink()
        return self.downloaded / ext_id

    def start(self):
        """What a start does with the code parts: find the folders, then load the fine ones."""
        found = self.discover(app_version="1.7.0")
        found, _selection, loaded = registry.load_code(found, NO_EXTENSIONS)
        for made in loaded.values():
            self.addCleanup(registry._forget, made.folder)
        return {item.id: item for item in found}, loaded

    def refused_at_staging(self, index, ext_id="tinyuse"):
        restarts = list(self.restarts)
        outcome = self.stage_tiny(index, ext_id)
        self.assertEqual((outcome.staged, outcome.kind, self.restarts, change.pending(self.paths)),
                         (False, "dependencies", restarts, None), outcome)
        self.assertEqual(list(change.staging(self.paths).iterdir()), [])
        return outcome.note


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
        # What an extension's wheels bring is not the app's.
        self.assertNotIn("qorqbot-tiny-dep", deps.app_distributions())

        registry._forget(loaded["tinyuse"].folder)
        self.assertNotIn(str(site.resolve()), sys.path)
        self.assertNotIn(DIST, sys.modules)
        self.assertIn("yaml", sys.modules)

    def test_a_wheel_that_would_replace_an_app_distribution_fails_the_change_and_stages_nothing(self):
        index = self.publish_tiny()
        app = {**deps.app_distributions(), "qorqbot-tiny-dep": "1.0.0"}
        with mock.patch.object(deps, "app_distributions", lambda: app):
            note = self.refused_at_staging(index)
        self.assertEqual(note, f"{UNFIT}the wheel {DIST}-{VERSION}-py3-none-any.whl would replace qorqbot-tiny-dep, which "
                               "comes with Qorqbot.")

    def test_a_wheel_for_another_python_or_needing_another_python_is_refused_at_staging(self):
        self.assertEqual(self.refused_at_staging(self.publish_tiny(wheels=lambda folder: [
            make_wheel(folder, tag="py2-none-any")])),
            f"{UNFIT}the wheel {DIST}-{VERSION}-py2-none-any.whl is for another Python or another computer.")

    def test_a_wheel_needing_another_python_version_is_refused_at_staging(self):
        self.assertEqual(self.refused_at_staging(self.publish_tiny(wheels=lambda folder: [
            make_wheel(folder, requires_python=">=99")])),
            f"{UNFIT}the wheel {DIST}-{VERSION}-py3-none-any.whl needs Python >=99.")

    def test_a_requirement_for_another_computer_is_not_asked_for(self):
        self.assertStaged(self.stage_tiny(self.publish_tiny(
            wheel_requires=["qorqbot-missing-dist; python_version < '3'"])))

    def test_what_a_start_would_judge_is_judged_at_staging(self):
        # A second metadata folder without METADATA: the wheel's own RECORD covers it, so the wheel passes its check.
        index = self.publish_tiny(wheels=lambda folder: [
            make_wheel(folder, extra={"other-1.0.dist-info/RECORD": b""})])
        self.assertEqual(self.refused_at_staging(index), "Its dependency wheels cannot be read: FileNotFoundError.")

    def test_a_requirement_with_an_extra_needs_what_the_extra_needs(self):
        def wheels(folder):
            return [make_wheel(folder, requires=["qorqbot-missing-dist; extra == 'socks'"])]
        with self.assertRaisesRegex(ValueError, "it needs qorqbot-missing-dist; extra == 'socks', which"):
            self.publish_tiny(wheels=wheels, requires=("qorqbot-tiny-dep[socks]==1.0.0",))
        # Without the extra, nothing of it is needed.
        self.assertStaged(self.stage_tiny(self.publish_tiny(ext_id="other", wheels=wheels), "other"))

    def test_an_interrupted_staging_is_unpacked_again_by_the_start(self):
        self.assertStaged(self.stage_tiny(self.publish_tiny()))
        staged = change._staged_folder(self.paths, change.pending(self.paths)["sha256"], "tinyuse")
        shutil.rmtree(staged / "deps" / "site")
        change.apply_pending(self.paths)
        self.assertEqual(change.pending(self.paths)["result"], {"state": change.DONE, "note": None})
        self.assertTrue((self.downloaded / "tinyuse" / "deps" / "site" / DIST / "__init__.py").is_file())

    def test_the_app_that_starts_judges_the_staged_wheels_again_and_keeps_the_old_folder(self):
        self.assertStaged(self.stage_tiny(self.publish_tiny(wheel_requires=["PyYAML>=6"])))
        with mock.patch.object(deps, "app_distributions", lambda: {}):
            change.apply_pending(self.paths)
        self.assertEqual(change.pending(self.paths)["result"], {
            "state": change.FAILED, "note": f"{UNFIT}it needs PyYAML>=6, which neither Qorqbot nor its wheels give."})
        self.assertFalse((self.downloaded / "tinyuse").exists())

    def test_a_judgement_that_fails_at_the_start_fails_the_change_and_the_start_goes_on(self):
        self.assertStaged(self.stage_tiny(self.publish_tiny()))
        with mock.patch.object(deps, "_first_problem", side_effect=TypeError("broken")):
            change.apply_pending(self.paths)
        self.assertEqual(change.pending(self.paths)["result"], {
            "state": change.FAILED, "note": "Its dependency wheels cannot be judged: TypeError."})
        self.assertFalse((self.downloaded / "tinyuse").exists())


class OtherExtensionsTest(DepsCase):
    def test_another_version_of_a_distribution_another_extension_brings_is_refused(self):
        self.installed()
        newer = self.publish_tiny(ext_id="other", pins=("qorqbot-tiny-dep==2.0.0",), requires=("qorqbot-tiny-dep",),
                                  wheels=lambda folder: [make_wheel(folder, version="2.0.0")])
        sentence = (f"{UNFIT}it brings qorqbot-tiny-dep 2.0.0, and the extension tinyuse brings qorqbot-tiny-dep 1.0.0; "
                    "one Qorqbot can hold only one version of it.")
        self.assertEqual(self.refused_at_staging(newer, "other"), sentence)
        # Staged before tinyuse came: the start that applies it refuses it the same way.
        shutil.move(self.downloaded / "tinyuse", self.root / "aside")
        self.assertStaged(self.stage_tiny(newer, "other"))
        shutil.move(self.root / "aside", self.downloaded / "tinyuse")
        change.apply_pending(self.paths)
        self.assertEqual(change.pending(self.paths)["result"], {"state": change.FAILED, "note": sentence})

    def test_at_a_start_the_later_one_is_unsupported_and_the_same_version_is_fine(self):
        self.installed()
        self.installed("same", requires=("qorqbot-tiny-dep",))
        found, loaded = self.start()
        self.assertEqual((found["same"].unsupported, sorted(loaded)), (None, ["same", "tinyuse"]))
        registry._forget(loaded["same"].folder)
        registry._forget(loaded["tinyuse"].folder)
        # A deps/site of another version, as no staging would leave it: the later folder cannot run.
        shutil.rmtree(self.downloaded / "same" / "deps")
        wheel = make_wheel(self.root, version="2.0.0")
        (self.downloaded / "same" / "deps").mkdir()
        shutil.copy(wheel, self.downloaded / "same" / "deps")
        deps.unpack([wheel], deps.site(self.downloaded / "same"))
        (self.downloaded / "same" / "code" / "constraints.txt").write_text("qorqbot-tiny-dep==2.0.0\n")
        found, loaded = self.start()
        self.assertEqual(found["tinyuse"].unsupported, f"{UNFIT}it brings qorqbot-tiny-dep 1.0.0, and the extension same "
                                                       "brings qorqbot-tiny-dep 2.0.0; one Qorqbot can hold only one "
                                                       "version of it.")
        self.assertEqual(sorted(loaded), ["same"])

    def test_every_code_folder_comes_before_every_wheel_folder(self):
        # tinyuse's wheel holds a package with the name of the other extension's code.
        self.installed(wheels=lambda folder: [make_wheel(folder, extra={
            "qorqut_later/__init__.py": b"", "qorqut_later/extension.py": b"raise ImportError('the wrong one')\n"})])
        self.installed("later", wheels=lambda folder: [make_wheel(folder)])
        found, loaded = self.start()
        self.assertEqual(sorted(loaded), ["later", "tinyuse"], found)
        places = [entry for entry in sys.path if str(self.downloaded) in entry]
        self.assertEqual([Path(entry).name for entry in places], ["code", "code", "site", "site"])


class StartTest(DepsCase):
    def test_after_an_app_update_wheels_that_no_longer_fit_make_it_unsupported(self):
        self.installed(wheel_requires=["PyYAML>=6"])
        with mock.patch.object(deps, "app_distributions", lambda: {}):
            found, loaded = self.start()
        self.assertEqual(found["tinyuse"].unsupported, f"{UNFIT}it needs PyYAML>=6, which neither Qorqbot nor its wheels "
                                                       "give.")
        self.assertNotIn("tinyuse", loaded)
        found, loaded = self.start()
        self.assertIsNone(found["tinyuse"].unsupported)
        self.assertIn("tinyuse", loaded)

    def test_a_judgement_that_fails_makes_it_unsupported_and_the_start_goes_on(self):
        self.installed()
        with mock.patch.object(deps, "_first_problem", side_effect=ValueError("broken")):
            found, loaded = self.start()
        self.assertEqual((found["tinyuse"].unsupported, loaded), ("Its dependency wheels cannot be judged: ValueError.", {}))

    def test_a_folder_with_wheels_put_in_place_by_hand_is_refused(self):
        folder = self.installed()
        shutil.rmtree(folder / "deps" / "site")
        path_before = list(sys.path)
        found, loaded = self.start()
        self.assertEqual(found["tinyuse"].findings[-1], "Its dependency wheels are not installed. Qorqbot installs them "
                                                         "only when it installs the extension from the index.")
        self.assertEqual((loaded, sys.path), ({}, path_before))


class JudgementTest(DepsCase):
    def test_a_version_that_is_there_counts_a_pre_release_too(self):
        with mock.patch.object(deps, "app_distributions", lambda: {"qorqbot-core-like": "1.3.0.dev0"}):
            self.assertIsNone(deps.judge([], ["qorqbot-core-like>=1.0"]))
        self.assertIsNone(deps.judge([make_wheel(self.wheels, version="2.0rc1")], ["qorqbot-tiny-dep>=1"]))

    def test_markers_are_judged_for_the_computers_the_bundle_is_for(self):
        windows = ["qorqbot-missing-dist; sys_platform == 'win32'"]
        self.assertIsNone(deps.judge([], windows, platform="linux-x86_64"))
        self.assertIsNone(deps.judge([], windows, platform="any", platforms=("linux", "mac")))
        self.assertIn("it needs qorqbot-missing-dist", deps.judge([], windows, platform="any",
                                                                  platforms=("linux", "windows")))
        posix = ["qorqbot-missing-dist; sys_platform != 'win32'"]
        self.assertIsNone(deps.judge([], posix, platform="any", platforms=("windows",)))
        self.assertIn("it needs qorqbot-missing-dist", deps.judge([], posix, platform="mac-arm64"))
        arm = ["qorqbot-missing-dist; platform_machine == 'aarch64'"]
        self.assertIsNone(deps.judge([], arm, platform="linux-x86_64"))
        self.assertIn("it needs", deps.judge([], arm, platform="linux-arm64"))

    def test_the_builder_judges_the_markers_of_the_bundle_not_of_this_computer(self):
        folder = self.folder(requires=("qorqbot-tiny-dep==1.0.0", "qorqbot-missing-dist; sys_platform == 'win32'"))
        with self.assertRaisesRegex(ValueError, "it needs qorqbot-missing-dist; sys_platform == 'win32'"):
            builder.build_folder(folder, "any", self.root / "tinyuse.tar.gz", [make_wheel(self.wheels)])

    def test_the_builder_refuses_wheels_whose_requirements_nothing_gives(self):
        with self.assertRaisesRegex(ValueError, "it needs qorqbot-missing-dist>=1, which neither Qorqbot nor its wheels "
                                                "give"):
            builder.build_folder(self.folder(), "any", self.root / "tinyuse.tar.gz",
                                 [make_wheel(self.wheels, requires=["qorqbot-missing-dist>=1"])])

    def test_the_builder_refuses_code_requirements_nothing_gives(self):
        with self.assertRaisesRegex(ValueError, "it needs qorqbot-missing-dist==2, which"):
            builder.build_folder(self.folder(requires=("qorqbot-missing-dist==2",)), "any", self.root / "tinyuse.tar.gz",
                                 [make_wheel(self.wheels)])


HOSTILE = ("C:/Windows/evil.py", "D:evil.py", "mod.py:stream", "//server/share/evil.py")


class NamesTest(DepsCase):
    def test_names_that_leave_the_folder_on_windows_are_refused(self):
        place = PureWindowsPath("C:/inst/deps/site")
        for name in HOSTILE:
            joined = place / name
            # Read by the Windows rules, each of these names leaves the folder or names a data stream.
            self.assertTrue(not str(joined).startswith(str(place) + "\\") or ":" in joined.name, name)
            with self.assertRaises(ValueError, msg=name):
                bundles.relative(name)
        self.assertEqual(str(bundles.relative("pkg/mod.py")), "pkg/mod.py")

    def test_a_wheel_with_such_a_name_is_refused_by_its_check_and_by_the_writer(self):
        for number, name in enumerate(HOSTILE):
            folder = self.wheels / str(number)
            folder.mkdir()
            wheel = make_wheel(folder, extra={name: b"evil\n"})
            with self.assertRaises(ValueError, msg=name):
                bundles.inspect_wheel(wheel)
            # The writer judges again, whatever judged the wheel before.
            target = self.root / f"site-{number}"
            with self.assertRaisesRegex(deps.DepsError, "holds an unsafe name", msg=name):
                deps.unpack([wheel], target)
            self.assertFalse(target.exists())

    def test_a_bundle_with_such_a_name_is_refused_by_its_check_and_by_the_writer(self):
        for number, name in enumerate(HOSTILE):
            archive = self.root / f"hostile-{number}.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for member, body in ((bundles.HEADER, b"{}"), (f"network/{name}", b"evil\n")):
                    info = tarfile.TarInfo(member)
                    info.size = len(body)
                    tar.addfile(info, io.BytesIO(body))
            with self.assertRaisesRegex(ValueError, "unsafe path", msg=name):
                bundles.unpack(archive, self.root / f"out-{number}")
            with tarfile.open(archive) as tar:
                members = {member.name: member for member in tar.getmembers()}
                with self.assertRaisesRegex(ValueError, "Unsafe archive path", msg=name):
                    bundles.extract(tar, members, self.root / f"written-{number}")
