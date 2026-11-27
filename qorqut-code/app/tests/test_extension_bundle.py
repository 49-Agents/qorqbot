"""Format 2 extension bundles: the builder (tools/distribution/extension_bundle.py build-folder) and the reading side
(qorqut/bootstrap/extension_bundle.py), plus the format 1 installer's use of the checks it shares with them."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

import yaml

from qorqut.bootstrap import extension_bundle as reading

TESTS = Path(__file__).resolve().parent
EXAMPLE = TESTS / "fixtures" / "extension-contract" / "network"
DISTRIBUTION = TESTS.parents[1] / "tools" / "distribution"


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"tool_{name}", DISTRIBUTION / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load_tool("extension_bundle")
installer = load_tool("native_extension")


def make_wheel(directory, name="demo", version="1.0", tag="py3-none-any"):
    """A minimal wheel whose RECORD covers its files."""
    path = Path(directory) / f"{name}-{version}-{tag}.whl"
    info = f"{name}-{version}.dist-info"
    files = {f"{name}/__init__.py": b"", f"{info}/METADATA": f"Name: {name}\nVersion: {version}\n".encode(),
             f"{info}/WHEEL": b"Wheel-Version: 1.0\n"}
    rows = [f"{item},sha256={base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip('=')},{len(body)}"
            for item, body in files.items()] + [f"{info}/RECORD,,"]
    with zipfile.ZipFile(path, "w") as wheel:
        for item, body in files.items():
            wheel.writestr(item, body)
        wheel.writestr(f"{info}/RECORD", "\n".join(rows) + "\n")
    return path


def write_tar(path, members):
    """A hand-made bundle: ``members`` is a list of (TarInfo, bytes or None)."""
    with tarfile.open(path, "w:gz") as archive:
        for info, body in members:
            if body is not None:
                info.size = len(body)
            archive.addfile(info, io.BytesIO(body) if body is not None else None)
    return path


def file_member(name, body):
    return tarfile.TarInfo(name), body


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / "source" / "network"
        shutil.copytree(EXAMPLE, self.folder)

    def build(self, name="network.tar.gz", platform="any", wheels=(), folder=None):
        return builder.build_folder(folder or self.folder, platform, self.root / name, wheels)

    def members(self, path):
        with tarfile.open(path, "r:gz") as tar:
            return [member.name for member in tar.getmembers()]

    def header(self, path):
        with tarfile.open(path, "r:gz") as tar:
            return json.loads(tar.extractfile(reading.HEADER).read())

    def hand_made(self, extra=(), drop=(), change=None):
        """A copy of the built network bundle with members added, left out or changed, under the same BUNDLE.json."""
        built = self.build("base.tar.gz")["path"]
        members = []
        with tarfile.open(built, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name in drop:
                    continue
                body = tar.extractfile(member).read()
                if change and member.name == change:
                    body += b"changed"
                members.append((tarfile.TarInfo(member.name), body))
        return write_tar(self.root / "hand.tar.gz", members + list(extra))


class BuildTest(Case):
    def test_the_same_folder_gives_the_same_bytes(self):
        first = self.build("first.tar.gz")
        second = self.build("second.tar.gz")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(Path(first["path"]).read_bytes(), Path(second["path"]).read_bytes())

    def test_the_bundle_holds_the_folder_and_its_checksums(self):
        result = self.build()
        header = self.header(result["path"])
        self.assertEqual((header["format"], header["id"], header["version"], header["platform"]),
                         (2, "network", "1.0.0", "any"))
        on_disk = {f"network/{path.relative_to(self.folder).as_posix()}"
                   for path in self.folder.rglob("*") if path.is_file()}
        self.assertEqual(set(header["files"]), on_disk)
        self.assertEqual(self.members(result["path"]), [reading.HEADER, *sorted(on_disk)])
        self.assertEqual(header["files"]["network/extension.yaml"],
                         hashlib.sha256((self.folder / "extension.yaml").read_bytes()).hexdigest())
        self.assertEqual(reading.check_bundle(result["path"]), [])

    def test_a_tests_folder_is_left_out(self):
        (self.folder / "tests").mkdir()
        (self.folder / "tests" / "test_network.py").write_text("")
        (self.folder / "plugin" / "skills" / "network" / "tests").mkdir()
        (self.folder / "plugin" / "skills" / "network" / "tests" / "case.md").write_text("")
        result = self.build()
        self.assertFalse([name for name in self.members(result["path"]) if "tests" in name.split("/")])
        self.assertEqual(result["sha256"], self.build("again.tar.gz", folder=self.folder)["sha256"])

    def test_caches_and_compiled_files_are_left_out(self):
        before = self.build("before.tar.gz")["sha256"]
        cache = self.folder / "plugin" / "__pycache__"
        cache.mkdir()
        (cache / "x.cpython-312.pyc").write_bytes(b"\0")
        (self.folder / "plugin" / "stray.pyc").write_bytes(b"\0")
        self.assertEqual(self.build("after.tar.gz")["sha256"], before)

    def test_a_folder_that_breaks_the_contract_is_refused(self):
        (self.folder / "README.md").unlink()
        with self.assertRaisesRegex(ValueError, "README.md is missing"):
            self.build()
        self.assertFalse((self.root / "network.tar.gz").exists())

    def test_a_link_in_the_folder_is_refused(self):
        os.symlink(self.folder / "README.md", self.folder / "plugin" / "linked.md")
        with self.assertRaisesRegex(ValueError, "is a symbolic link"):
            self.build()

    def test_a_deps_folder_in_the_source_is_refused(self):
        (self.folder / "deps").mkdir()
        (self.folder / "deps" / "x.txt").write_text("")
        with self.assertRaisesRegex(ValueError, "deps/ folder"):
            self.build()

    def test_wheels_without_code_constraints_are_refused(self):
        with self.assertRaisesRegex(ValueError, "constraints.txt"):
            self.build(wheels=[make_wheel(self.root)])


class WheelTest(Case):
    def setUp(self):
        super().setUp()
        code = self.folder / "code"
        (code / "qorqut_network").mkdir(parents=True)
        (code / "pyproject.toml").write_text('[project]\nname = "qorqut-network"\ndependencies = ["demo==1.0"]\n')
        (code / "qorqut_network" / "extension.py").write_text("def create():\n    pass\n")
        (code / "constraints.txt").write_text("# Pins of the dependency wheels.\ndemo==1.0\nplat==2.0\n")
        path = self.folder / "extension.yaml"
        data = yaml.safe_load(path.read_text())
        data["code"] = {"factory": "qorqut_network.extension:create"}
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()

    def test_a_pinned_wheel_goes_to_deps(self):
        wheel = make_wheel(self.wheels)
        result = self.build(wheels=[wheel])
        header = self.header(result["path"])
        self.assertEqual(header["files"]["network/deps/demo-1.0-py3-none-any.whl"],
                         hashlib.sha256(wheel.read_bytes()).hexdigest())
        self.assertIn("network/code/constraints.txt", header["files"])
        self.assertEqual(reading.check_bundle(result["path"]), [])

    def test_an_unpinned_wheel_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not pinned"):
            self.build(wheels=[make_wheel(self.wheels, version="1.1")])

    def test_the_platform_follows_the_wheels(self):
        specific = make_wheel(self.wheels, name="plat", version="2.0", tag="cp312-cp312-manylinux_2_17_x86_64")
        # The code needs demo, so every bundle of it carries the demo wheel.
        demo = make_wheel(self.wheels)
        with self.assertRaisesRegex(ValueError, "built for one platform"):
            self.build(wheels=[specific, demo])
        with self.assertRaisesRegex(ValueError, "any platform"):
            self.build(platform="linux-x86_64", wheels=[make_wheel(self.wheels)])
        result = self.build(platform="linux-x86_64", wheels=[specific, demo])
        self.assertEqual(self.header(result["path"])["platform"], "linux-x86_64")
        self.assertEqual(reading.check_bundle(result["path"]), [])

    def test_a_broken_wheel_is_refused(self):
        wheel = make_wheel(self.wheels)
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr("demo/extra.py", b"")
        with self.assertRaisesRegex(ValueError, "RECORD"):
            self.build(wheels=[wheel])


class CheckTest(Case):
    def test_a_tests_member_is_refused(self):
        found = reading.check_bundle(self.hand_made(extra=[file_member("network/tests/test_x.py", b"")]))
        self.assertEqual(found, ["The bundle holds network/tests/test_x.py, which is inside a tests folder."])

    def test_a_member_not_in_the_header_is_refused(self):
        found = reading.check_bundle(self.hand_made(extra=[file_member("network/plugin/extra.md", b"x")]))
        self.assertEqual(found, ["The bundle holds network/plugin/extra.md, which BUNDLE.json does not list."])

    def test_a_missing_member_is_refused(self):
        found = reading.check_bundle(self.hand_made(drop={"network/README.md"}))
        self.assertEqual(found, ["The bundle lacks network/README.md, which BUNDLE.json lists."])

    def test_a_changed_member_is_refused(self):
        found = reading.check_bundle(self.hand_made(change="network/README.md"))
        self.assertEqual(found, ["The checksum of network/README.md differs from BUNDLE.json."])

    def test_links_parent_paths_and_duplicates_are_refused(self):
        link = tarfile.TarInfo("network/plugin/link.md")
        link.type, link.linkname = tarfile.SYMTYPE, "../../README.md"
        hard = tarfile.TarInfo("network/plugin/hard.md")
        hard.type, hard.linkname = tarfile.LNKTYPE, "network/README.md"
        unsafe = "The bundle holds a link, a special file, a duplicate or an unsafe path."
        for extra in ([(link, None)], [(hard, None)], [file_member("network/../escape.md", b"")],
                      [file_member("/network/absolute.md", b"")], [file_member("network/README.md", b"again")]):
            with self.subTest(extra=extra[0][0].name):
                self.assertEqual(reading.check_bundle(self.hand_made(extra=extra)), [unsafe])
                os.unlink(self.root / "hand.tar.gz")
                os.unlink(self.root / "base.tar.gz")

    def test_the_folder_inside_must_pass_the_checker(self):
        built = self.build("base.tar.gz")["path"]
        members = []
        with tarfile.open(built, "r:gz") as tar:
            header = json.loads(tar.extractfile(reading.HEADER).read())
            for member in tar.getmembers():
                if member.name not in (reading.HEADER, "network/README.md"):
                    members.append((tarfile.TarInfo(member.name), tar.extractfile(member).read()))
        del header["files"]["network/README.md"]
        members.append(file_member(reading.HEADER, json.dumps(header).encode()))
        found = reading.check_bundle(write_tar(self.root / "hand.tar.gz", members))
        self.assertEqual(found, ["README.md is missing at the top of the folder."])

    def test_the_header_version_must_equal_the_manifest(self):
        built = self.build("base.tar.gz")["path"]
        with tarfile.open(built, "r:gz") as tar:
            members = [(tarfile.TarInfo(m.name), tar.extractfile(m).read()) for m in tar.getmembers()]
        header = json.loads(members[0][1])
        header["version"] = "1.0.1"
        members[0] = file_member(reading.HEADER, json.dumps(header).encode())
        found = reading.check_bundle(write_tar(self.root / "hand.tar.gz", members))
        self.assertEqual(found, ["BUNDLE.json has the version 1.0.1 but the manifest has 1.0.0."])

    def test_a_malformed_header_is_refused(self):
        found = reading.check_bundle(write_tar(self.root / "hand.tar.gz", [file_member(reading.HEADER, b"{}")]))
        self.assertEqual(len(found), 1)
        self.assertIn("must have exactly the fields", found[0])
        found = reading.check_bundle(write_tar(self.root / "plain.tar.gz", [file_member("network/README.md", b"")]))
        self.assertEqual(found, ["The bundle has no BUNDLE.json."])

    def test_the_platform_must_follow_the_wheels_also_without_deps(self):
        built = self.build("base.tar.gz")["path"]
        with tarfile.open(built, "r:gz") as tar:
            members = [(tarfile.TarInfo(m.name), tar.extractfile(m).read()) for m in tar.getmembers()]
        header = json.loads(members[0][1])
        header["platform"] = "linux-x86_64"
        members[0] = file_member(reading.HEADER, json.dumps(header).encode())
        found = reading.check_bundle(write_tar(self.root / "hand.tar.gz", members))
        self.assertEqual(found, ["The bundle is for linux-x86_64 but every wheel in it works on any platform."])

    def test_the_header_platform_has_the_bundle_form(self):
        header = self.header(self.build()["path"])
        with self.assertRaisesRegex(ValueError, "must name the platform any, or a platform and processor"):
            reading.bundle_header({**header, "platform": "windows"})
        for good in ("any", "linux-x86_64", "mac-arm64", "mac-x86_64"):
            self.assertTrue(reading.BUNDLE_PLATFORM.fullmatch(good))
        for bad in ("windows", "Linux-x86_64", "linux", "ubuntu24-x86_64", "mac-arm64 "):
            self.assertFalse(reading.BUNDLE_PLATFORM.fullmatch(bad))

    def test_a_file_that_is_also_a_folder_is_refused_before_anything_is_written(self):
        hand = self.hand_made(extra=[file_member("network/README.md/inside.md", b"")])
        self.assertEqual(reading.check_bundle(hand), [
            "The bundle holds network/README.md as a file and also network/README.md/inside.md inside it."])
        target = self.root / "target"
        with self.assertRaisesRegex(ValueError, "as a file and also"):
            reading.unpack(hand, target)
        self.assertFalse(target.exists())

    def test_names_equal_but_for_case_are_refused(self):
        hand = self.hand_made(extra=[file_member("network/readme.md", b"")])
        self.assertEqual(reading.check_bundle(hand), [
            "The bundle holds both network/README.md and network/readme.md, which differ only by case."])

    def test_unpack_takes_only_a_new_or_empty_folder(self):
        built = self.build()["path"]
        target = self.root / "target"
        target.mkdir()
        os.symlink(self.root, target / "network")
        with self.assertRaisesRegex(ValueError, "^A bundle is unpacked only into a new or empty folder.$"):
            reading.unpack(built, target)
        (target / "network").unlink()
        reading.unpack(built, target)
        self.assertTrue((target / "network" / "README.md").is_file())
        with self.assertRaisesRegex(ValueError, "new or empty folder"):
            reading.unpack(built, target)

    def test_an_executable_file_stays_executable(self):
        helper = self.folder / "plugin" / "instructions" / "weekly-review.md"
        helper.chmod(0o755)
        first = self.build("first.tar.gz")["path"]
        self.assertEqual(Path(first).read_bytes(), Path(self.build("second.tar.gz")["path"]).read_bytes())
        with tarfile.open(first, "r:gz") as tar:
            modes = {member.name: member.mode for member in tar.getmembers()}
        self.assertEqual(modes.pop("network/plugin/instructions/weekly-review.md"), 0o755)
        self.assertEqual(set(modes.values()), {0o644})
        target = self.root / "target"
        reading.unpack(first, target)
        self.assertTrue(os.access(target / "network/plugin/instructions/weekly-review.md", os.X_OK))
        self.assertFalse(os.access(target / "network/README.md", os.X_OK))

    def test_unpack_writes_only_the_listed_files(self):
        built = self.build()["path"]
        target = self.root / "unpacked"
        header = reading.unpack(built, target)
        written = {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()}
        self.assertEqual(written, {reading.HEADER, *header["files"]})
        self.assertEqual((target / "network" / "README.md").read_bytes(), (self.folder / "README.md").read_bytes())


class FormatOneTest(unittest.TestCase):
    """The format 1 installer takes the moved checks from the app module and behaves as before."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def bundle(self, extra=(), name="telegram.tar.gz"):
        wheel = self.root / "qorqut_telegram-1.0-py3-none-any.whl"
        if not wheel.exists():
            make_wheel(self.root, name="qorqut_telegram", version="1.0")
        value = {"format": 1, "id": "telegram", "version": "1.0", "factory": installer.FACTORIES["telegram"],
                 "wheels": [{"filename": wheel.name, "sha256": installer.checksum(wheel)}]}
        directory = tarfile.TarInfo("wheels")
        directory.type = tarfile.DIRTYPE
        path = write_tar(self.root / name, [
            file_member("EXTENSION.json", json.dumps(value).encode()), (directory, None),
            file_member("wheels/" + wheel.name, wheel.read_bytes()), *extra])
        return path, installer.checksum(path), wheel

    def test_the_installer_runs_alone_in_the_layout_of_a_download(self):
        """``native_extension.py`` is copied to ``runtime/extension_install.py`` and run with ``python -I``; it finds the
        shared checks in the app source of the download and nowhere else."""
        root = self.root / "qorqbot"
        bootstrap = root / "runtime" / "src" / "qorqut-code" / "app" / "qorqut" / "bootstrap"
        bootstrap.mkdir(parents=True)
        shutil.copy2(DISTRIBUTION / "native_extension.py", root / "runtime" / "extension_install.py")
        shutil.copy2(Path(reading.__file__), bootstrap / "extension_bundle.py")

        def run(bundle, sha):
            return subprocess.run([sys.executable, "-I", "-B", root / "runtime" / "extension_install.py", bundle,
                                   "--sha256", sha], cwd=self.root, env={"PATH": os.environ["PATH"]},
                                  capture_output=True, text=True, timeout=30)
        path, sha, _ = self.bundle()
        refused = run(path, "0" * 64)
        self.assertEqual((refused.returncode, refused.stderr.strip()),
                         (1, "Qorqbot extension install: Extension bundle checksum mismatch"))
        # A good bundle passes the shared checks and stops only where this layout has no launcher.
        passed = run(path, sha)
        self.assertEqual(passed.returncode, 1)
        self.assertIn("launch.py", passed.stderr)
        link = tarfile.TarInfo("wheels/link.whl")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        linked, linked_sha, _ = self.bundle(extra=[(link, None)], name="linked.tar.gz")
        self.assertEqual(run(linked, linked_sha).stderr.strip(),
                         "Qorqbot extension install: Duplicate, linked or special archive member")
        (bootstrap / "extension_bundle.py").unlink()
        missing = run(path, sha)
        self.assertIn("The app source with the extension bundle checks is missing", missing.stderr)

    def test_a_format_one_bundle_is_inspected_and_extracted(self):
        path, sha, wheel = self.bundle()
        stage = self.root / "stage"
        value = installer.inspect_bundle(path, sha, stage)
        self.assertEqual(value["id"], "telegram")
        self.assertEqual((stage / "wheels" / wheel.name).read_bytes(), wheel.read_bytes())
        self.assertEqual(installer.inspect_wheel(stage / "wheels" / wheel.name)["name"], "qorqut-telegram")

    def test_a_format_one_link_is_refused_as_before(self):
        link = tarfile.TarInfo("wheels/link.whl")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        path, sha, _ = self.bundle(extra=[(link, None)])
        with self.assertRaisesRegex(ValueError, "^Duplicate, linked or special archive member$"):
            installer.inspect_bundle(path, sha)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            installer.inspect_bundle(path, "0" * 64)


if __name__ == "__main__":
    unittest.main()
