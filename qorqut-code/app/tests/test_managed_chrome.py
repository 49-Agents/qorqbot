"""Qorqbot's own Chrome: pinned download, hash check, safe extraction, atomic install, start check and preference."""

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from qorqut_operations.contracts import DomainError
from qorqut.browser import managed_chrome as mc
from tests.test_browser_shared_profile import ATLAS, SCOUT, SharedProfileTest

TOP = "chrome-linux64"
PAGE = "#!/bin/sh\necho '<html><head></head><body></body></html>'\n"


def archive(members=None):
    """A tiny stand-in for the Chrome archive. ``members`` maps a name to text, or to ``(text, mode)``; a mode with the
    link type makes the text the link's target."""
    members = {TOP + "/": "", TOP + "/chrome": (PAGE, 0o100755), TOP + "/resources.pak": "data"} if members is None else members
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name, value in members.items():
            text, mode = value if isinstance(value, tuple) else (value, 0o40755 if name.endswith("/") else 0o100644)
            info = zipfile.ZipInfo(name)
            info.create_system, info.external_attr = 3, mode << 16
            package.writestr(info, text)
    return buffer.getvalue()


class Download:
    """Stands in for the network: writes the given bytes, or fails like a dropped connection."""

    def __init__(self, data=None, error=None):
        self.data, self.error, self.urls = archive() if data is None else data, error, []

    def __call__(self, url, destination):
        self.urls.append(url)
        if self.error:
            Path(destination).write_bytes(self.data[:10])      # a partial file is on disk when the connection drops
            raise self.error
        Path(destination).write_bytes(self.data)
        return hashlib.sha256(self.data).hexdigest()


class ManagedChromeTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runtime = Path(folder.name).resolve() / "runtime"  # the temporary folder is behind a symbolic link on macOS
        self.runtime.mkdir(mode=0o700)
        self.root = mc.chrome_root(self.runtime)
        self.download = Download()
        self.checked = []
        for target, name, value in ((mc, "platform_key", lambda *_a: "linux64"),
                                    (mc, "ARCHIVES", {"linux64": hashlib.sha256(self.download.data).hexdigest()}),
                                    (mc, "_running_programs", lambda: [])):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("QORQUT_CHROME_EXECUTABLE", "QORQUT_NATIVE_ROOT"):
            os.environ.pop(name, None)
        mc._RUNS.clear()
        self.addCleanup(mc._RUNS.clear)

    def check(self, binary, work):
        self.checked.append(str(binary))

    def converge(self, **changes):
        return mc.converge(self.runtime, **{"download": self.download, "check": self.check, **changes})

    def entries(self):
        return sorted(entry.name for entry in self.root.iterdir()) if self.root.exists() else []

    def pin(self, version, data=None):
        """Move the pin to another version, as a release would."""
        self.download = Download(archive({TOP + "/chrome": (PAGE + "# " + version, 0o100755)}) if data is None else data)
        for name, value in (("VERSION", version), ("ARCHIVES", {"linux64": hashlib.sha256(self.download.data).hexdigest()})):
            patcher = mock.patch.object(mc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class InstallTest(ManagedChromeTest):
    def test_the_pinned_build_is_downloaded_checked_and_becomes_the_one_new_browsers_use(self):
        selected = self.converge()
        build = f"{mc.VERSION}-linux64"
        self.assertEqual(selected, self.root / "current" / TOP / "chrome")
        self.assertEqual(self.entries(), [build, "current", "install.lock"])
        self.assertEqual(os.readlink(self.root / "current"), build)
        self.assertEqual(self.download.urls, [f"https://storage.googleapis.com/chrome-for-testing-public/{mc.VERSION}"
                                              "/linux64/chrome-linux64.zip"])
        self.assertEqual(self.checked, [str(self.root / build / TOP / "chrome")])
        self.assertTrue(os.access(selected, os.X_OK))
        self.assertFalse(os.access(self.root / build / TOP / "resources.pak", os.X_OK))
        self.assertEqual(mc.preferred(self.runtime), str(selected))
        self.assertTrue(mc.settled(self.runtime))
        self.assertEqual(mc.view(self.runtime)["state"], "ready")

    def test_running_again_downloads_nothing_and_changes_nothing(self):
        self.converge()
        before = (self.root / "current" / TOP / "chrome").stat().st_ino
        self.converge()
        self.assertEqual(len(self.download.urls), 1)
        self.assertEqual((self.root / "current" / TOP / "chrome").stat().st_ino, before)

    def test_a_wrong_hash_is_refused_before_anything_is_extracted(self):
        with mock.patch.object(mc, "ARCHIVES", {"linux64": "0" * 64}), mock.patch.object(mc, "extract") as extract, \
                self.assertRaises(DomainError) as raised:
            self.converge()
        self.assertEqual(raised.exception.code, "chrome_archive_mismatch")
        extract.assert_not_called()
        self.assertEqual((self.entries(), self.checked), (["install.lock"], []))
        self.assertIsNone(mc.preferred(self.runtime))

    def test_a_failed_download_leaves_everything_as_it_was(self):
        self.converge()
        before = self.entries()
        self.pin("200.0.0.1")
        self.download.error = DomainError("chrome_download_failed", "Could not download Chrome: offline")
        with self.assertRaises(DomainError) as raised:
            self.converge()
        self.assertEqual(raised.exception.code, "chrome_download_failed")
        self.assertEqual(self.entries(), before)
        self.assertEqual(mc.preferred(self.runtime), str(self.root / "current" / TOP / "chrome"))

    def test_a_half_finished_install_is_never_taken_for_a_chrome_and_is_cleared_by_the_next_one(self):
        """What a crash during extraction leaves behind: a staging folder holding a Chrome, no published build."""
        interrupted = self.root / ".staging-crashed" / "build" / TOP
        interrupted.mkdir(parents=True)
        (interrupted / "chrome").write_text(PAGE)
        (interrupted / "chrome").chmod(0o755)
        self.assertIsNone(mc.current_program(self.runtime))
        self.assertFalse(mc.settled(self.runtime))
        self.converge()
        self.assertEqual(self.entries(), [f"{mc.VERSION}-linux64", "current", "install.lock"])

    def test_a_published_folder_without_its_record_is_not_a_chrome(self):
        """A build is complete only with the record written last; a folder that merely looks like one is installed again."""
        fake = self.root / f"{mc.VERSION}-linux64" / TOP
        fake.mkdir(parents=True)
        (fake / "chrome").write_text("not the pinned program")
        (fake / "chrome").chmod(0o755)
        os.symlink(f"{mc.VERSION}-linux64", self.root / "current")
        self.assertIsNone(mc.current_program(self.runtime))
        self.converge()
        self.assertEqual((self.root / "current" / TOP / "chrome").read_text(), PAGE)

    def test_a_build_whose_program_was_altered_or_removed_is_installed_again(self):
        self.converge()
        program = self.root / f"{mc.VERSION}-linux64" / TOP / "chrome"
        for damage in (lambda: program.write_text("truncated"), program.unlink):
            damage()
            with self.subTest(damage=damage):
                self.assertIsNone(mc.current_program(self.runtime))
                self.assertIsNone(mc.preferred(self.runtime))
                self.converge()
                self.assertEqual(program.read_text(), PAGE)
        self.assertEqual(len(self.download.urls), 3)

    def test_current_must_name_a_build_beside_it(self):
        self.converge()
        outside = self.runtime / "elsewhere"
        os.rename(self.root / f"{mc.VERSION}-linux64", outside)
        for target in (str(outside), "../elsewhere", ".hidden"):
            (self.root / "current").unlink()
            os.symlink(target, self.root / "current")
            with self.subTest(target=target):
                self.assertIsNone(mc.current_program(self.runtime))

    def test_only_one_install_runs_at_a_time(self):
        self.root.mkdir()
        with mc._install_lock(self.root), self.assertRaises(DomainError) as raised:
            self.converge()
        self.assertEqual(raised.exception.code, "chrome_install_busy")
        self.assertEqual(self.download.urls, [])

    def test_a_computer_without_a_pinned_build_is_refused(self):
        with mock.patch.object(mc, "platform_key", lambda *_a: None), self.assertRaises(DomainError) as raised:
            self.converge()
        self.assertEqual(raised.exception.code, "chrome_unsupported_platform")
        self.assertFalse(self.root.exists())


class StartsHereTest(ManagedChromeTest):
    def refuse(self, binary, work):
        raise DomainError("chrome_sandbox_unavailable", "Chrome has no usable sandbox here")

    def test_a_build_that_does_not_start_here_stays_on_disk_but_is_never_preferred(self):
        with self.assertRaises(DomainError) as raised:
            self.converge(check=self.refuse)
        self.assertEqual(raised.exception.code, "chrome_sandbox_unavailable")
        self.assertEqual(self.entries(), [f"{mc.VERSION}-linux64", "install.lock", mc.START_FAILED])   # and the failure is noted
        self.assertIsNone(mc.current_program(self.runtime))
        self.assertIsNone(mc.preferred(self.runtime))
        self.assertFalse(mc.settled(self.runtime))

    def test_a_retry_after_the_computer_is_fixed_needs_no_second_download(self):
        with self.assertRaises(DomainError):
            self.converge(check=self.refuse)
        self.converge()
        self.assertEqual(len(self.download.urls), 1)
        self.assertIsNotNone(mc.preferred(self.runtime))

    def test_a_new_pin_takes_over_once_it_starts_and_the_unused_old_build_is_removed(self):
        self.converge()
        old = mc.VERSION
        self.pin("200.0.0.1")
        self.assertFalse(mc.settled(self.runtime))
        selected = self.converge()
        self.assertEqual(self.entries(), ["200.0.0.1-linux64", "current", "install.lock"])
        self.assertIn("200.0.0.1", selected.read_text())
        self.assertNotIn(old, self.entries())

    def test_an_old_build_a_browser_still_runs_from_is_kept(self):
        self.converge()
        old = self.root / f"{mc.VERSION}-linux64"
        self.pin("200.0.0.1")
        with mock.patch.object(mc, "_running_programs", lambda: ["/usr/bin/python3", str(old / TOP / "chrome")]):
            self.converge()
        self.assertTrue((old / TOP / "chrome").is_file())
        self.assertEqual(os.readlink(self.root / "current"), "200.0.0.1-linux64")
        self.pin("201.0.0.1")
        self.converge()                                  # that browser has stopped since
        self.assertEqual(self.entries(), ["201.0.0.1-linux64", "current", "install.lock"])

    def test_old_builds_are_kept_when_running_programs_cannot_be_told_apart(self):
        self.converge()
        old = f"{mc.VERSION}-linux64"
        for listing in (None, [str(self.root / "current" / TOP / "chrome")]):
            with self.subTest(listing=listing), mock.patch.object(mc, "_running_programs", lambda: listing):
                mc._prune(self.root, "another-build")
                self.assertIn(old, self.entries())

    def test_a_new_pin_that_does_not_start_leaves_the_old_build_in_use(self):
        self.converge()
        old = mc.VERSION
        self.pin("200.0.0.1")
        with self.assertRaises(DomainError):
            self.converge(check=self.refuse)
        self.assertEqual(os.readlink(self.root / "current"), f"{old}-linux64")
        self.assertEqual(mc.preferred(self.runtime), str(self.root / "current" / TOP / "chrome"))
        self.assertEqual(mc.view(self.runtime)["state"], "ready")


class BackOffTest(ManagedChromeTest):
    """A computer where the build can never start (Ubuntu's denied sandbox) is not asked again every hour."""

    def refuse(self, binary, work):
        self.checked.append("refused")
        raise DomainError("chrome_sandbox_unavailable", "Chrome has no usable sandbox here")

    def test_the_automatic_loop_does_not_start_a_build_that_already_failed_for_a_reason_only_the_owner_can_fix(self):
        with self.assertRaises(DomainError):
            self.converge(check=self.refuse, automatic=True)                      # the first attempt looks
        for _ in range(3):
            with self.assertRaises(DomainError) as again:
                self.converge(check=self.refuse, automatic=True)
            self.assertEqual(again.exception.code, "chrome_sandbox_unavailable")
            self.assertIn("no usable sandbox", str(again.exception))              # the same explanation as the first time
        self.assertEqual(self.checked, ["refused"])
        self.assertEqual(len(self.download.urls), 1)

    def test_the_owners_retry_looks_again_and_a_fixed_computer_forgets_the_failure(self):
        with self.assertRaises(DomainError):
            self.converge(check=self.refuse, automatic=True)
        self.converge()                                                           # Retry setup: not automatic, and it starts now
        self.assertEqual(self.checked[-1], str(self.root / f"{mc.VERSION}-linux64" / TOP / "chrome"))
        self.assertFalse((self.root / mc.START_FAILED).exists())
        self.assertIsNotNone(mc.preferred(self.runtime))

    def test_a_new_pin_is_tried_even_where_the_old_one_failed(self):
        with self.assertRaises(DomainError):
            self.converge(check=self.refuse, automatic=True)
        self.pin("200.0.0.1")
        self.converge(automatic=True)
        self.assertIn("200.0.0.1", mc.current_program(self.runtime).read_text())

    def test_a_failure_that_may_pass_by_itself_is_not_remembered(self):
        def hangs(binary, work):
            self.checked.append("hung")
            raise DomainError("chrome_not_starting", "Chrome did not start: no page within 60 seconds")
        for _ in range(2):
            with self.assertRaises(DomainError):
                self.converge(check=hangs, automatic=True)
        self.assertEqual(self.checked, ["hung", "hung"])

    def test_the_background_loop_asks_for_the_automatic_run_and_the_owners_retry_does_not(self):
        runs = []
        def run(runtime, **options):
            runs.append(options)
        for automatic in (True, False):
            mc._RUNS.clear()
            self.assertTrue(mc.start(self.runtime, run=run, automatic=automatic))
            mc._RUNS[str(self.root)]["thread"].join(10)
        self.assertEqual(runs, [{"automatic": True}, {}])

    def test_setup_passes_it_on(self):
        from types import SimpleNamespace
        from qorqut.browser import setup
        with mock.patch.object(mc, "start") as start:
            setup.prepare_chrome(SimpleNamespace(paths=SimpleNamespace(runtime=self.runtime)), backoff={})
            setup.prepare_chrome(SimpleNamespace(paths=SimpleNamespace(runtime=self.runtime)))
        self.assertEqual([call.kwargs["automatic"] for call in start.call_args_list], [True, False])


class RecordedBuildTest(ManagedChromeTest):
    """A build that a browser's ownership record points into is never removed."""

    def record(self, name, chrome, mode="managed"):
        folder = self.runtime / "agent-browsers" / name
        folder.mkdir(parents=True)
        meta = {"mode": mode, "managed": {"chrome": str(chrome)}} if mode == "managed" else {"mode": mode, "legacy": {}}
        (folder / "meta.json").write_text(json.dumps(meta))

    def test_a_build_replaced_because_it_was_altered_is_refused_while_a_browser_is_recorded_on_it(self):
        self.converge()
        program = self.root / f"{mc.VERSION}-linux64" / TOP / "chrome"
        self.record("atlas", self.root / "current" / TOP / "chrome")
        program.write_text("altered")                                             # not the pinned build any more
        with self.assertRaises(DomainError) as refused:
            self.converge()
        self.assertEqual(refused.exception.code, "chrome_install_failed")
        self.assertEqual(program.read_text(), "altered", "the files a browser would start from were not touched")

    def test_the_same_build_is_replaced_when_no_browser_is_recorded_on_it(self):
        self.converge()
        program = self.root / f"{mc.VERSION}-linux64" / TOP / "chrome"
        self.record("atlas", "/usr/bin/google-chrome-stable")
        self.record("operator", "/unused", mode="legacy")
        program.write_text("altered")
        self.converge()
        self.assertEqual(program.read_text(), PAGE)

    def test_an_old_build_a_stopped_browser_is_recorded_on_is_kept(self):
        self.converge()
        old = self.root / f"{mc.VERSION}-linux64"
        self.record("atlas", old / TOP / "chrome")                                # recorded by its versioned path
        self.pin("200.0.0.1")
        self.converge()                                                           # nothing runs from the old build
        self.assertTrue((old / TOP / "chrome").is_file())
        self.assertEqual(os.readlink(self.root / "current"), "200.0.0.1-linux64")

    def test_nothing_is_removed_when_a_record_cannot_be_read(self):
        self.converge()
        old = f"{mc.VERSION}-linux64"
        self.record("atlas", "/usr/bin/google-chrome-stable")
        (self.runtime / "agent-browsers/atlas/meta.json").write_text("{damaged")
        self.pin("200.0.0.1")
        self.converge()
        self.assertIn(old, self.entries())


class StartCheckTest(unittest.TestCase):
    """The real check against stand-in programs that behave like a Chrome that starts, lacks a library, or has no sandbox."""

    def setUp(self):
        # Chrome's singleton socket path must fit a Unix socket address (104 bytes on macOS), and macOS's own temporary
        # directory is too long for that, so the stand-in programs run from a short real path.
        folder = tempfile.TemporaryDirectory(dir=os.path.realpath("/tmp"))
        self.addCleanup(folder.cleanup)
        self.folder = Path(os.path.realpath(folder.name))

    def run_check(self, script):
        program = self.folder / "chrome"
        program.write_text("#!/bin/sh\n" + script + "\n")
        program.chmod(0o755)
        mc.start_check(program, self.folder / ".check")

    def code(self, script):
        with self.assertRaises(DomainError) as raised:
            self.run_check(script)
        self.assertFalse((self.folder / ".check").exists())
        return raised.exception

    def test_a_chrome_that_prints_the_blank_page_passes_and_leaves_no_profile_behind(self):
        self.run_check('case "$*" in *--headless=new*--user-data-dir=*--dump-dom*) echo "<html><head></head></html>";; *) exit 3;; esac')
        self.assertFalse((self.folder / ".check").exists())

    def test_a_missing_system_library_is_named(self):
        error = self.code('echo "chrome: error while loading shared libraries: libnss3.so: cannot open shared object file" >&2; exit 127')
        self.assertEqual((error.code, error.message), ("chrome_libraries_missing", "Chrome needs the system library libnss3.so"))

    def test_a_denied_sandbox_is_reported_as_one(self):
        error = self.code('echo "[1:1:FATAL:zygote_host_impl_linux.cc:144] No usable sandbox! If you are running on Ubuntu 23.10+" >&2; kill -ABRT $$')
        self.assertEqual(error.code, "chrome_sandbox_unavailable")

    def test_any_other_failure_carries_its_first_line(self):
        error = self.code('echo "something else broke" >&2; exit 1')
        self.assertEqual(error.code, "chrome_not_starting")
        self.assertIn("something else broke", error.message)

    def test_a_page_without_success_is_not_a_start(self):
        self.assertEqual(self.code('echo "<html></html>"; exit 1').code, "chrome_not_starting")

    def test_a_chrome_that_hangs_is_stopped_and_reported(self):
        with mock.patch.object(mc, "CHECK_TIMEOUT", 1):
            error = self.code('echo $$ > "$PWD/../pid"; sleep 60')
        self.assertEqual(error.code, "chrome_not_starting")
        self.assertIn("within 1 seconds", error.message)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.folder / "pid").read_text()), 0)

    def test_a_program_that_cannot_be_run_is_reported(self):
        with self.assertRaises(DomainError) as raised:
            mc.start_check(self.folder / "absent", self.folder / ".check")
        self.assertEqual(raised.exception.code, "chrome_not_starting")


class ExtractTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name).resolve()

    def extract(self, members):
        (self.folder / "chrome.zip").write_bytes(archive(members))
        mc.extract(self.folder / "chrome.zip", self.folder / "build", TOP)
        return self.folder / "build" / TOP

    def test_files_folders_and_inside_links_are_written_with_plain_permissions(self):
        top = self.extract({TOP + "/Framework/Versions/158/Resources/": "", TOP + "/Framework/Versions/158/lib": ("code", 0o100755),
                            TOP + "/Framework/Versions/Current": ("158", 0o120755),
                            TOP + "/Framework/Resources": ("Versions/Current/Resources", 0o120755),
                            TOP + "/chrome_sandbox": ("helper", 0o104755), TOP + "/deb.deps": ("deps", 0o100600)})
        self.assertEqual(os.readlink(top / "Framework/Versions/Current"), "158")
        self.assertTrue((top / "Framework/Resources").is_dir())
        self.assertEqual(stat.S_IMODE((top / "Framework/Versions/158/lib").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((top / "chrome_sandbox").stat().st_mode), 0o755, "no set-user-ID bit survives")
        self.assertEqual(stat.S_IMODE((top / "deb.deps").stat().st_mode), 0o644)

    def test_anything_that_would_leave_the_build_is_refused(self):
        outside = self.folder / "outside"
        outside.mkdir()
        attempts = {
            "a parent step": {TOP + "/../evil": "x"},
            "a parent step at the start": {"../evil": "x"},
            "an absolute name": {"/tmp/evil": "x"},
            "a backslash name": {TOP + "\\..\\evil": "x"},
            "another top folder": {"other/chrome": "x"},
            "a link to an absolute path": {TOP + "/link": (str(outside), 0o120777)},
            "a link out of the build": {TOP + "/link": ("../outside", 0o120777)},
            "a link that climbs back through itself": {TOP + "/a/": "", TOP + "/a/link": ("../../" + TOP + "/../outside", 0o120777)},
            "a file written through a link": {TOP + "/real/": "", TOP + "/link": ("real", 0o120777), TOP + "/link/evil": "x"},
            "a folder made through a link": {TOP + "/real/": "", TOP + "/link": ("real", 0o120777), TOP + "/link/sub/": ""},
            "a device": {TOP + "/device": ("", 0o020644)},
            "a named pipe": {TOP + "/pipe": ("", 0o010644)},
        }
        for label, members in attempts.items():
            with self.subTest(label), tempfile.TemporaryDirectory(dir=self.folder) as work:
                Path(work, "chrome.zip").write_bytes(archive(members))
                with self.assertRaises(DomainError) as raised:
                    mc.extract(Path(work, "chrome.zip"), Path(work, "build"), TOP)
                self.assertEqual(raised.exception.code, "chrome_archive_unsafe")
                self.assertEqual(list(outside.iterdir()), [])
                self.assertFalse((self.folder / "evil").exists())

    def test_the_same_name_twice_is_refused_instead_of_overwritten(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as package, self.assertWarns(UserWarning):
            for text in ("first", "second"):
                info = zipfile.ZipInfo(TOP + "/chrome")
                info.create_system, info.external_attr = 3, 0o100755 << 16
                package.writestr(info, text)
        (self.folder / "chrome.zip").write_bytes(buffer.getvalue())
        with self.assertRaises(DomainError) as raised:
            mc.extract(self.folder / "chrome.zip", self.folder / "build", TOP)
        self.assertEqual(raised.exception.code, "chrome_archive_unsafe")

    def test_size_and_entry_limits_hold(self):
        for limits in ({"unpacked": 10}, {"members": 1}, {"link": 2}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory(dir=self.folder) as work, \
                    mock.patch.dict(mc.LIMITS, limits), self.assertRaises(DomainError) as raised:
                Path(work, "chrome.zip").write_bytes(archive({TOP + "/chrome": "x" * 64, TOP + "/link": ("chrome", 0o120777)}))
                mc.extract(Path(work, "chrome.zip"), Path(work, "build"), TOP)
            self.assertEqual(raised.exception.code, "chrome_archive_unsafe")

    def test_a_file_that_is_not_an_archive_is_refused(self):
        (self.folder / "chrome.zip").write_bytes(b"<html>proxy sign-in page</html>")
        with self.assertRaises(DomainError) as raised:
            mc.extract(self.folder / "chrome.zip", self.folder / "build", TOP)
        self.assertEqual(raised.exception.code, "chrome_archive_unsafe")

    def test_an_archive_without_the_chrome_program_is_not_installed(self):
        root = self.folder / "chrome"
        root.mkdir()
        data = archive({TOP + "/readme": "no program here"})
        with mock.patch.object(mc, "ARCHIVES", {"linux64": hashlib.sha256(data).hexdigest()}), \
                self.assertRaises(DomainError) as raised:
            mc.install(root, "linux64", Download(data))
        self.assertEqual(raised.exception.code, "chrome_archive_unsafe")
        self.assertEqual(list(root.iterdir()), [])


class DownloadTest(unittest.TestCase):
    def test_only_https_is_downloaded(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(DomainError) as raised:
            mc.fetch("http://storage.googleapis.com/chrome.zip", Path(folder) / "chrome.zip")
        self.assertEqual(raised.exception.code, "chrome_download_failed")

    def response(self, body, length=None):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read = io.BytesIO(body).read
        response.headers = {"Content-Length": str(len(body) if length is None else length)}
        return mock.patch.object(mc.urllib.request, "build_opener", return_value=mock.Mock(open=mock.Mock(return_value=response)))

    def test_the_hash_of_what_was_written_is_returned(self):
        with tempfile.TemporaryDirectory() as folder, self.response(b"archive bytes"):
            digest = mc.fetch("https://example.invalid/chrome.zip", Path(folder) / "chrome.zip")
            self.assertEqual(digest, hashlib.sha256(b"archive bytes").hexdigest())
            self.assertEqual((Path(folder) / "chrome.zip").read_bytes(), b"archive bytes")

    def test_a_connection_that_drops_is_a_failed_download_not_a_wrong_file(self):
        with tempfile.TemporaryDirectory() as folder, self.response(b"half", length=8), self.assertRaises(DomainError) as raised:
            mc.fetch("https://example.invalid/chrome.zip", Path(folder) / "chrome.zip")
        self.assertEqual(raised.exception.code, "chrome_download_failed")

    def test_an_oversized_download_is_stopped(self):
        with tempfile.TemporaryDirectory() as folder, self.response(b"x" * 100), self.assertRaises(DomainError) as raised:
            mc.fetch("https://example.invalid/chrome.zip", Path(folder) / "chrome.zip", limit=10)
        self.assertEqual(raised.exception.code, "chrome_archive_mismatch")

    def test_network_errors_become_a_failed_download(self):
        opener = mock.Mock(open=mock.Mock(side_effect=OSError("Temporary failure in name resolution")))
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(mc.urllib.request, "build_opener", return_value=opener), \
                self.assertRaises(DomainError) as raised:
            mc.fetch("https://example.invalid/chrome.zip", Path(folder) / "chrome.zip")
        self.assertEqual(raised.exception.code, "chrome_download_failed")


class BackgroundTest(ManagedChromeTest):
    def test_start_returns_at_once_and_settings_follow_the_download(self):
        release, results = threading.Event(), []

        def run(runtime):
            release.wait(10)
            self.converge()

        self.assertEqual(mc.view(self.runtime)["state"], "waiting")
        self.assertTrue(mc.start(self.runtime, results.append, run=run))
        self.assertEqual(mc.view(self.runtime)["state"], "downloading")
        self.assertIn("downloading its own browser", mc.view(self.runtime)["explanation"])
        self.assertFalse(mc.start(self.runtime, results.append, run=run), "one download at a time")
        release.set()
        mc._RUNS[str(self.root)]["thread"].join(10)
        self.assertEqual(results, [None])
        self.assertEqual(mc.view(self.runtime), {"state": "ready", "version": mc.VERSION, "explanation": "", "diagnostic": ""})
        self.assertFalse(mc.start(self.runtime, results.append, run=run), "nothing to do once the pin is in use")

    def outcome(self, error):
        results = []

        def run(runtime):
            raise error

        self.assertTrue(mc.start(self.runtime, results.append, run=run))
        mc._RUNS[str(self.root)]["thread"].join(10)
        return results, mc.view(self.runtime)

    def test_a_failed_download_is_reported_and_explained_to_the_owner(self):
        results, view = self.outcome(DomainError("chrome_download_failed", "Could not download Chrome: timed out"))
        self.assertEqual([error.code for error in results], ["chrome_download_failed"])
        self.assertEqual(view["state"], "failed")
        self.assertIn("internet connection", view["explanation"])
        self.assertEqual(view["diagnostic"], "Could not download Chrome: timed out")

    def test_a_chrome_that_does_not_start_sends_the_owner_to_google_chrome(self):
        for code in mc.NOT_STARTING:
            with self.subTest(code=code):
                _, view = self.outcome(DomainError(code, "detail"))
                self.assertEqual(view["state"], "unusable")
                self.assertIn("google.com/chrome", view["explanation"])

    def test_an_unexpected_error_is_a_failure_not_a_crash(self):
        with self.assertLogs(mc.logger, "ERROR"):
            results, view = self.outcome(RuntimeError("bug"))
        self.assertEqual(([error.code for error in results], view["state"]), (["chrome_install_failed"], "failed"))

    def test_nothing_is_downloaded_when_the_installation_names_its_own_chrome(self):
        self.converge()
        for name in ("QORQUT_CHROME_EXECUTABLE", "QORQUT_NATIVE_ROOT"):
            with self.subTest(name=name), mock.patch.dict(os.environ, {name: "/opt/chosen"}):
                self.assertFalse(mc.start(self.runtime, run=self.fail))
                self.assertIsNone(mc.preferred(self.runtime))
                self.assertEqual(mc.view(self.runtime)["state"], "not_used")

    def test_a_computer_without_a_pinned_build_is_told_to_install_chrome(self):
        with mock.patch.object(mc, "platform_key", lambda *_a: None):
            self.assertFalse(mc.start(self.runtime, run=self.fail))
            view = mc.view(self.runtime)
        self.assertEqual(view["state"], "unsupported")
        self.assertIn("google.com/chrome", view["explanation"])

    def test_every_explanation_is_written_for_the_owner(self):
        for text in [*mc.FAILURE_TEXT.values(), mc.DOWNLOADING]:
            with self.subTest(text=text):
                self.assertNotRegex(text, r"action=|[a-z]+_[a-z]+|docs/|QORQUT|sandbox|namespace|SHA|hash")


class PinTest(unittest.TestCase):
    def test_every_packaged_platform_has_a_pinned_archive(self):
        keys = {mc.platform_key("Linux", "x86_64"), mc.platform_key("Darwin", "arm64"), mc.platform_key("Darwin", "x86_64")}
        self.assertEqual(keys, set(mc.ARCHIVES))
        for digest in mc.ARCHIVES.values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertRegex(mc.VERSION, r"^\d+\.\d+\.\d+\.\d+$")

    def test_other_computers_have_no_build(self):
        for system, machine in (("Linux", "aarch64"), ("Windows", "AMD64"), ("FreeBSD", "amd64")):
            self.assertIsNone(mc.platform_key(system, machine))

    def test_the_program_is_where_each_archive_puts_it(self):
        self.assertEqual(mc.program("linux64"), "chrome-linux64/chrome")
        self.assertEqual(mc.program("mac-arm64"), "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/"
                                                  "Google Chrome for Testing")


class NewAndExistingBrowsersTest(SharedProfileTest):
    """Which Chrome a browser's ownership record names. ``sys.executable`` stands in for the installed Chrome."""

    def setUp(self):
        super().setUp()
        data = archive()
        for target, name, value in ((mc, "platform_key", lambda *_a: "linux64"), (mc, "_running_programs", lambda: []),
                                    (mc, "ARCHIVES", {"linux64": hashlib.sha256(data).hexdigest()})):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("QORQUT_CHROME_EXECUTABLE", "QORQUT_NATIVE_ROOT"):
            os.environ.pop(name, None)
        self.install = lambda: mc.converge(self.paths.runtime, download=Download(data), check=lambda *_a: None)

    def chrome(self, agent):
        import json
        return json.loads((self.state(agent) / "meta.json").read_text())["managed"]["chrome"]

    def test_without_qorqbots_own_chrome_a_new_browser_runs_on_the_installed_one(self):
        self.manage(ATLAS, "ensure")
        self.assertEqual(self.chrome(ATLAS), self.manager.chrome)

    def test_a_new_browser_runs_on_qorqbots_own_chrome_once_it_is_installed(self):
        managed = str(self.install())
        self.assertEqual(self.manage(ATLAS, "ensure")["state"], "healthy")
        self.assertEqual(self.chrome(ATLAS), managed)
        self.assertIn("/chrome/current/", managed)

    def test_an_existing_browser_keeps_its_chrome_and_profile_when_qorqbots_own_appears(self):
        self.manage(ATLAS, "ensure")
        (self.state(ATLAS) / "profile/Cookies").write_text("signed in")
        managed = str(self.install())
        for action in ("ensure", "restart", "repair", "stop", "ensure"):
            self.manage(ATLAS, action)
        self.assertEqual(self.chrome(ATLAS), self.manager.chrome)
        self.assertEqual((self.state(ATLAS) / "profile/Cookies").read_text(), "signed in")
        self.manage(SCOUT, "ensure")
        self.assertEqual(self.chrome(SCOUT), managed)

    def test_qorqbots_own_chrome_is_enough_where_no_chrome_is_installed(self):
        self.manager.chrome = "/usr/bin/google-chrome-stable-is-not-installed"
        self.assertEqual(self.manage(ATLAS, "status")["state"], "unsupported")
        managed = str(self.install())
        self.assertEqual(self.manage(ATLAS, "status")["state"], "missing")
        self.assertEqual(self.manage(ATLAS, "ensure")["state"], "healthy")
        self.assertEqual(self.chrome(ATLAS), managed)

    def refused(self, agent, action="ensure"):
        with self.assertRaises(DomainError) as raised:
            self.manage(agent, action)
        self.assertEqual(raised.exception.code, "browser_capability_missing")
        return str(raised.exception)

    def test_a_browser_that_has_no_chrome_yet_is_told_that_qorqbots_own_is_on_its_way(self):
        self.manager.chrome = "/usr/bin/google-chrome-stable-is-not-installed"
        with mock.patch.object(self.manager, "_capable", return_value=True):      # as when Chrome goes after the check
            message = self.refused(ATLAS)
            self.assertIn("downloaded", message)
            self.assertIn(self.manager.chrome, message)
            self.assertNotRegex(message, "(?i)install chrome|nothing is downloaded")
            with mock.patch.dict(os.environ, {"QORQUT_CHROME_EXECUTABLE": self.manager.chrome}):
                message = self.refused(ATLAS)                # an explicit choice: nothing is downloaded, and it says so
            self.assertNotIn("downloaded", message)
            self.assertIn(self.manager.chrome, message)

    def test_a_browser_whose_own_chrome_is_gone_is_told_what_brings_it_back(self):
        import shutil
        managed = str(self.install())
        self.manage(ATLAS, "ensure")
        self.manage(ATLAS, "stop")
        shutil.rmtree(mc.chrome_root(self.paths.runtime))
        message = self.refused(ATLAS)
        self.assertIn(managed, message)
        self.assertIn("downloaded again", message)
        self.assertNotIn("nothing is downloaded", message)

    def test_an_explicitly_chosen_chrome_is_never_replaced(self):
        self.install()
        with mock.patch.dict(os.environ, {"QORQUT_CHROME_EXECUTABLE": self.manager.chrome}):
            self.manage(ATLAS, "ensure")
        self.assertEqual(self.chrome(ATLAS), self.manager.chrome)


del SharedProfileTest          # its own tests run from their module, not again from here


if __name__ == "__main__":
    unittest.main()
