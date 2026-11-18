"""Managed Claude Code and Codex: resolution order, the pinned download with its checks, progress, cancel and retry.

Downloads are faked (no network) and the "binaries" are small shell scripts that print the version the real ones print.
"""
import base64
import errno
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

from qorqut import agent_cli_install as cli

MACHINE = {"system": "Darwin", "machine": "arm64"}


def tarball(provider, extra=(), links=()):
    """A package shaped like the registry's for ``provider`` on darwin-arm64."""
    inner = cli._TARGETS[provider]["darwin-arm64"][4]
    version = cli.VERSIONS[provider]
    program = f'#!/bin/sh\necho "{cli.VERSION_OUTPUT[provider].format(version=version)}"\n'.encode()
    files = {"claude": [("claude", program, 0o755)],
             "codex": [("bin/codex", program, 0o755), ("bin/codex-code-mode-host", b"host", 0o755),
                       ("codex-path/rg", b"rg", 0o755), ("codex-resources/zsh/bin/zsh", b"zsh", 0o755),
                       ("codex-package.json", b"{}", 0o644)]}[provider]
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, body, mode in [*files, *extra]:
            info = tarfile.TarInfo(inner + name)
            info.size, info.mode = len(body), mode
            archive.addfile(info, io.BytesIO(body))
        for name, target in links:
            info = tarfile.TarInfo(inner + name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            archive.addfile(info)
        info = tarfile.TarInfo("package/package.json")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"{}"))
    return data.getvalue()


class FakeResponse:
    def __init__(self, data, gate=None):
        self.stream, self.gate, self.reads, self.step = io.BytesIO(data), gate, 0, max(1, len(data) // 2)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, amount):
        # Half the data arrives at once; the rest waits for the gate so a test can look at the download in between.
        if self.gate is not None and self.reads:
            self.gate.wait(10)
        self.reads += 1
        return self.stream.read(min(amount, self.step))


class FakeOpener:
    def __init__(self, data=b"", error=None, gate=None):
        self.data, self.error, self.gate, self.urls = data, error, gate, []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if self.error:
            raise self.error
        return FakeResponse(self.data, self.gate)


class Case(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.environ = {"QORQUT_FOLDER_ROOT": str(self.root), "PATH": str(self.root / "bin")}
        cli._jobs.clear()
        cli._system_versions.clear()
        self.addCleanup(cli._jobs.clear)
        self.addCleanup(cli._system_versions.clear)

    def pin(self, provider, data):
        """Point the darwin-arm64 row at ``data`` so a small fake passes the checksum and size checks."""
        row = (cli._TARGETS[provider]["darwin-arm64"][0], base64.b64encode(hashlib.sha512(data).digest()).decode(),
               len(data), 10, cli._TARGETS[provider]["darwin-arm64"][4])
        patcher = mock.patch.dict(cli._TARGETS[provider], {"darwin-arm64": row})
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait(self, provider):
        job = cli._jobs[provider]
        job["thread"].join(10)
        self.assertFalse(job["thread"].is_alive())

    def executable(self, path, body="#!/bin/sh\n"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)
        return str(path)

    def own_cli(self, provider, version):
        """The owner's own CLI on PATH, printing ``version`` the way the real one does."""
        said = cli.VERSION_OUTPUT[provider].format(version=version)
        return self.executable(self.root / "bin" / provider, f'#!/bin/sh\necho "{said}"\n')


class ResolveTests(Case):
    def test_an_explicit_path_wins_over_everything(self):
        managed = self.executable(cli.managed_path("claude", self.root))
        self.own_cli("claude", "2.1.300")
        mine = self.executable(self.root / "mine/claude")
        self.environ["QORQUT_CLAUDE_EXECUTABLE"] = mine
        self.assertEqual(cli.resolve("claude", self.environ), {"path": mine, "source": "override", "version": None})
        self.assertTrue(Path(managed).is_file())

    def test_an_explicit_path_that_is_gone_or_cannot_run_is_skipped(self):
        # The launcher points the variable at our own copy; deleting tools/claude-<version> must not leave the row "installed".
        gone = str(cli.managed_path("claude", self.root))
        self.environ["QORQUT_CLAUDE_EXECUTABLE"] = gone
        self.assertIsNone(cli.resolve("claude", self.environ))
        self.assertEqual(cli.status("claude", environ=self.environ, **MACHINE)["phase"], "not_downloaded")
        plain = self.root / "mine/claude"
        self.executable(plain)
        plain.chmod(0o644)
        self.environ["QORQUT_CLAUDE_EXECUTABLE"] = str(plain)
        self.assertIsNone(cli.resolve("claude", self.environ))
        (self.root / "mine/folder").mkdir()
        self.environ["QORQUT_CLAUDE_EXECUTABLE"] = str(self.root / "mine/folder")
        self.assertIsNone(cli.resolve("claude", self.environ))
        # ... and falls through to our copy, then the owner's.
        own = self.own_cli("claude", "2.1.300")
        self.assertEqual(cli.resolve("claude", self.environ)["path"], own)
        managed = self.executable(cli.managed_path("claude", self.root))
        self.assertEqual(cli.resolve("claude", self.environ), {"path": managed, "source": "managed", "version": "2.1.269"})

    def test_our_copy_comes_before_the_one_on_path(self):
        managed = self.executable(cli.managed_path("codex", self.root))
        self.own_cli("codex", "0.200.0")
        self.assertEqual(cli.resolve("codex", self.environ), {"path": managed, "source": "managed", "version": "0.154.0"})

    def test_the_variable_the_launcher_sets_for_our_copy_still_reads_as_managed(self):
        managed = self.executable(cli.managed_path("claude", self.root))
        self.environ["QORQUT_CLAUDE_EXECUTABLE"] = managed
        self.assertEqual(cli.resolve("claude", self.environ)["source"], "managed")

    def test_the_owners_own_cli_is_used_when_we_have_none_and_it_is_new_enough(self):
        found = self.own_cli("claude", "2.1.269")
        self.assertEqual(cli.resolve("claude", self.environ), {"path": found, "source": "system", "version": "2.1.269"})
        found = self.own_cli("codex", "0.200.1")
        self.assertEqual(cli.resolve("codex", self.environ), {"path": found, "source": "system", "version": "0.200.1"})

    def test_the_owners_own_cli_below_the_minimum_does_not_count(self):
        for provider, old in (("claude", "2.0.99"), ("codex", "0.153.9")):
            self.own_cli(provider, old)
            self.assertIsNone(cli.resolve(provider, self.environ), provider)
            current = cli.status(provider, environ=self.environ, **MACHINE)
            self.assertEqual(current["phase"], "not_downloaded")
            self.assertIn("too old", current["message"])
            self.assertIn("download its own copy", current["message"])
            self.assertIsNone(cli.managed_launch(provider, self.environ))

    def test_a_system_cli_that_will_not_say_its_version_does_not_count(self):
        self.executable(self.root / "bin/claude", "#!/bin/sh\necho hello\n")
        self.assertIsNone(cli.resolve("claude", self.environ))
        self.assertIn("did not report its version", cli.status("claude", environ=self.environ, **MACHINE)["message"])
        self.executable(self.root / "bin/codex", "#!/bin/sh\nexit 4\n")
        self.assertIsNone(cli.resolve("codex", self.environ))

    def test_the_minimum_is_the_pinned_major_and_minor_at_most(self):
        for provider in cli.PROVIDERS:
            pinned = tuple(int(part) for part in cli.VERSIONS[provider].split("."))
            self.assertLessEqual(cli.MINIMUM_VERSIONS[provider], pinned[:2])

    def test_the_owners_version_is_read_once_per_file_and_failures_are_remembered_too(self):
        calls = []

        def run(command, **options):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "2.1.300 (Claude Code)\n", "")

        own = self.own_cli("claude", "2.1.300")
        for _ in range(3):
            self.assertEqual(cli.resolve("claude", self.environ, run=run)["version"], "2.1.300")
        self.assertEqual(len(calls), 1)
        # A changed file is asked again.
        stat = os.stat(own)
        os.utime(own, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        cli.resolve("claude", self.environ, run=run)
        self.assertEqual(len(calls), 2)

        hung = []

        def hangs(command, **options):
            hung.append(options["timeout"])
            raise subprocess.TimeoutExpired(command, options["timeout"])

        self.own_cli("codex", "0.200.0")
        for _ in range(3):
            self.assertIsNone(cli.resolve("codex", self.environ, run=hangs))
            cli.status("codex", environ=self.environ, **MACHINE)
        self.assertEqual(hung, [cli.VERSION_TIMEOUT])
        # ... but not for ever: a CLI that was busy once is asked again later.
        with mock.patch.object(cli.time, "monotonic", return_value=time.monotonic() + cli.FAILED_VERSION_RETRY + 1):
            self.assertEqual(cli.resolve("codex", self.environ)["version"], "0.200.0")

    def test_nothing_found_is_none(self):
        self.assertIsNone(cli.resolve("claude", self.environ))
        self.assertIsNone(cli.resolve("codex", {"PATH": "/nonexistent"}))

    def test_a_folder_without_our_copy_ignores_the_folder(self):
        self.assertIsNone(cli.resolve("claude", {"PATH": "/nonexistent"}))

    def test_an_unknown_provider_is_refused(self):
        for call in (cli.resolve, cli.status, cli.start_download, cli.cancel_download, cli.launch_environment,
                     cli.launch_arguments, cli.managed_launch):
            with self.assertRaises(ValueError):
                call("gemini")


class ManagedLaunchTests(Case):
    def test_it_gives_what_a_terminal_needs_for_our_copy(self):
        for name in ("claude", "codex"):
            self.executable(cli.managed_path(name, self.root))
        # A session holds the stable path (through tools/<provider>-current), which a later pin does not remove.
        claude = str(self.root / "tools/claude-current/claude")
        self.assertEqual(cli.managed_launch("claude", self.environ),
                         {"path": claude, "bin_dir": str(Path(claude).parent), "env": cli.launch_environment("claude"),
                          "args": [], "source": "managed", "version": "2.1.269"})
        launch = cli.managed_launch("codex", self.environ)
        self.assertEqual((launch["path"], launch["bin_dir"]),
                         (str(self.root / "tools/codex-current/bin/codex"), str(self.root / "tools/codex-current/bin")))
        self.assertEqual(cli.resolve("codex", self.environ)["path"], str(cli.managed_path("codex", self.root)))
        self.assertEqual((launch["env"], launch["args"]), ({}, ["-c", "check_for_update_on_startup=false"]))

    def test_nothing_is_started_to_answer(self):
        self.executable(self.root / "bin/claude", "#!/bin/sh\ntouch %s\necho '2.1.300 (Claude Code)'\n" % (self.root / "ran"))
        # Not asked about its version yet: usable, version unknown, and it was not run.
        launch = cli.managed_launch("claude", self.environ)
        self.assertEqual((launch["source"], launch["version"]), ("system", None))
        self.assertFalse((self.root / "ran").exists())
        # Once resolve has asked (and remembered), the answer is used.
        self.assertEqual(cli.resolve("claude", self.environ)["version"], "2.1.300")
        self.assertEqual(cli.managed_launch("claude", self.environ)["version"], "2.1.300")

    def test_nothing_to_launch_is_none(self):
        self.assertIsNone(cli.managed_launch("claude", self.environ))


class StablePathTests(Case):
    """A terminal session outlives the pin it was created under, so it holds tools/<provider>-current, never a versioned folder."""

    def install(self, provider):
        data = tarball(provider)
        self.pin(provider, data)
        return cli.install(provider, self.root, opener=FakeOpener(data), run=passes(provider), **MACHINE)

    def test_an_install_points_the_current_link_at_the_version_folder_with_a_relative_target(self):
        self.install("codex")
        link = self.root / "tools/codex-current"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "codex-0.154.0")
        self.assertTrue(os.access(cli.stable_path("codex", self.root), os.X_OK))
        # Codex finds its sibling folders beside bin/codex; through a link to the folder they are all there.
        for name in ("codex-path/rg", "codex-resources/zsh/bin/zsh", "bin/codex-code-mode-host"):
            self.assertTrue((link / name).is_file(), name)
        self.assertEqual([p.name for p in (self.root / "tools").iterdir() if p.name.startswith(".")], [])

    def test_the_stable_path_keeps_working_when_the_pin_moves_and_the_old_folder_goes(self):
        self.install("claude")
        session_path = str(cli.managed_launch("claude", self.environ)["path"])
        self.assertEqual(session_path, str(self.root / "tools/claude-current/claude"))
        before = os.stat(session_path).st_ino
        with mock.patch.dict(cli.VERSIONS, {"claude": "2.1.270"}):
            self.assertIsNone(cli.resolve("claude", self.environ))  # not installed yet: the open session still has its path
            self.assertTrue(os.access(session_path, os.X_OK))
            self.install("claude")
            self.assertFalse((self.root / "tools/claude-2.1.269").exists())
            self.assertEqual(os.readlink(self.root / "tools/claude-current"), "claude-2.1.270")
            self.assertTrue(os.access(session_path, os.X_OK))
            self.assertNotEqual(os.stat(session_path).st_ino, before)
            self.assertEqual(cli.managed_launch("claude", self.environ)["path"], session_path)

    def test_the_old_folder_stays_while_the_link_cannot_be_moved(self):
        self.install("claude")
        (self.root / "tools/claude-current").unlink()
        (self.root / "tools/claude-current").mkdir()  # not a link: left alone
        with mock.patch.dict(cli.VERSIONS, {"claude": "2.1.270"}):
            self.install("claude")
        self.assertTrue((self.root / "tools/claude-2.1.269").exists())
        self.assertTrue((self.root / "tools/claude-current").is_dir())

    def test_a_copy_installed_before_the_link_existed_gets_its_link_when_asked(self):
        self.executable(cli.managed_path("claude", self.root))
        self.assertFalse((self.root / "tools/claude-current").exists())
        self.assertEqual(cli.managed_launch("claude", self.environ)["path"], str(self.root / "tools/claude-current/claude"))
        self.assertEqual(os.readlink(self.root / "tools/claude-current"), "claude-2.1.269")
        # A link left by an older pin is moved to the installed one.
        (self.root / "tools/claude-current").unlink()
        (self.root / "tools/claude-current").symlink_to("claude-2.0.0")
        self.assertEqual(cli.managed_launch("claude", self.environ)["path"], str(self.root / "tools/claude-current/claude"))
        self.assertEqual(os.readlink(self.root / "tools/claude-current"), "claude-2.1.269")

    def test_a_variable_naming_the_stable_path_is_still_our_copy_and_a_broken_link_is_not(self):
        self.install("claude")
        self.environ["QORQUT_CLAUDE_EXECUTABLE"] = str(self.root / "tools/claude-current/claude")
        self.assertEqual(cli.resolve("claude", self.environ)["source"], "managed")
        shutil.rmtree(self.root / "tools/claude-2.1.269")
        self.assertIsNone(cli.resolve("claude", self.environ))
        self.assertIsNone(cli.managed_launch("claude", self.environ))

    def test_an_unreadable_tools_folder_means_not_installed_and_raises_nothing(self):
        self.executable(cli.managed_path("claude", self.root))
        tools = self.root / "tools"
        tools.chmod(0)
        self.addCleanup(tools.chmod, 0o755)
        if os.access(tools, os.R_OK | os.X_OK):
            self.skipTest("this user can read a folder with no permissions (root)")
        self.assertFalse(cli.installed("claude", self.root))
        self.assertIsNone(cli.managed_launch("claude", self.environ))
        self.assertIsNone(cli.resolve("claude", {**self.environ, "PATH": ""}))
        self.assertEqual(cli.status("claude", environ=self.environ, **MACHINE)["phase"], "not_downloaded")
        self.assertFalse(cli._point_current("claude", self.root))


class UpdaterTests(unittest.TestCase):
    def test_claude_is_told_not_to_update_and_codex_by_argument(self):
        self.assertEqual(cli.launch_environment("claude"), {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1"})
        self.assertEqual(cli.launch_environment("codex"), {})
        self.assertEqual(cli.launch_arguments("claude"), [])
        self.assertEqual(cli.launch_arguments("codex"), ["-c", "check_for_update_on_startup=false"])


class InstallTests(Case):
    def install(self, provider, opener, **options):
        return cli.install(provider, self.root, opener=opener, **MACHINE, **options)

    def test_claude_installs_only_its_binary_where_the_launcher_expects_it(self):
        data = tarball("claude", extra=[("README.md", b"x", 0o644)])
        self.pin("claude", data)
        path = self.install("claude", FakeOpener(data))
        self.assertEqual(path, self.root / "tools/claude-2.1.269/claude")
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual([p.name for p in path.parent.iterdir()], ["claude"])
        self.assertEqual(sorted(p.name for p in (self.root / "tools").iterdir()), ["claude-2.1.269", "claude-current"])

    def test_codex_keeps_the_folders_it_finds_beside_its_program(self):
        data = tarball("codex")
        self.pin("codex", data)
        path = self.install("codex", FakeOpener(data))
        self.assertEqual(path, self.root / "tools/codex-0.154.0/bin/codex")
        folder = path.parent.parent
        for name in ("bin/codex-code-mode-host", "codex-path/rg", "codex-resources/zsh/bin/zsh", "codex-package.json"):
            self.assertTrue((folder / name).is_file(), name)
        self.assertTrue(os.access(folder / "codex-path/rg", os.X_OK))
        self.assertFalse(os.access(folder / "codex-package.json", os.X_OK))
        self.assertFalse((folder / "package.json").exists())

    def test_an_installed_copy_is_not_downloaded_again(self):
        data = tarball("claude")
        self.pin("claude", data)
        self.install("claude", FakeOpener(data))
        self.install("claude", FakeOpener(error=AssertionError("downloaded twice")))

    def test_a_checksum_mismatch_installs_nothing(self):
        data = tarball("claude")
        self.pin("claude", data)
        with self.assertRaisesRegex(cli.InstallProblem, "checksum"):
            self.install("claude", FakeOpener(data[:-1] + b"X"))
        self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_a_download_larger_than_pinned_stops(self):
        data = tarball("claude")
        self.pin("claude", data)
        with self.assertRaisesRegex(cli.InstallProblem, "larger"):
            self.install("claude", FakeOpener(data + b"extra"))
        self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_offline_gives_a_plain_reason(self):
        with self.assertRaisesRegex(cli.InstallProblem, "internet connection"):
            self.install("claude", FakeOpener(error=OSError("offline")))
        self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_a_program_that_does_not_run_or_is_another_version_installs_nothing(self):
        for body in (b"#!/bin/sh\nexit 3\n", b'#!/bin/sh\necho "1.0.0 (Claude Code)"\n'):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz") as archive:
                info = tarfile.TarInfo("package/claude")
                info.size, info.mode = len(body), 0o755
                archive.addfile(info, io.BytesIO(body))
            self.pin("claude", data.getvalue())
            with self.assertRaisesRegex(cli.InstallProblem, "does not run"):
                self.install("claude", FakeOpener(data.getvalue()))
            self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_links_and_paths_that_leave_the_folder_are_refused(self):
        for kind in ("link", "parent"):
            data = tarball("codex", extra=[("../escape", b"x", 0o644)] if kind == "parent" else [],
                           links=[("bin/link", "/etc/passwd")] if kind == "link" else [])
            self.pin("codex", data)
            with self.assertRaisesRegex(cli.InstallProblem, "unexpected layout"):
                self.install("codex", FakeOpener(data))
            self.assertEqual(list((self.root / "tools").iterdir()), [])
        self.assertFalse((self.root / "escape").exists())

    def test_a_package_without_the_program_installs_nothing(self):
        data = tarball("codex")
        self.pin("codex", data)
        with mock.patch.dict(cli._ENTRY, {"codex": "bin/missing"}):
            with self.assertRaisesRegex(cli.InstallProblem, "unexpected layout"):
                self.install("codex", FakeOpener(data))

    def test_an_unsupported_machine_is_reported(self):
        with self.assertRaisesRegex(cli.InstallProblem, "this type of computer"):
            cli.install("claude", self.root, system="Linux", machine="ppc64le", opener=FakeOpener(error=AssertionError("no")))

    def test_too_little_disk_space_is_reported_before_downloading(self):
        with mock.patch.object(cli.shutil, "disk_usage", return_value=mock.Mock(free=1)):
            with self.assertRaisesRegex(cli.InstallProblem, "disk space"):
                self.install("claude", FakeOpener(error=AssertionError("no download")))

    def test_machines(self):
        self.assertEqual(cli.target_name("Darwin", "arm64"), "darwin-arm64")
        self.assertEqual(cli.target_name("Darwin", "x86_64"), "darwin-x64")
        self.assertEqual(cli.target_name("Linux", "x86_64"), "linux-x64")
        self.assertIsNone(cli.target_name("Linux", "aarch64"))
        self.assertIsNone(cli.target_name("Windows", "AMD64"))


def passes(provider):
    """What ``run`` returns for a program that prints the right version."""
    said = cli.VERSION_OUTPUT[provider].format(version=cli.VERSIONS[provider])
    return lambda command, **options: subprocess.CompletedProcess(command, 0, said + "\n", "")


class InterruptedInstallTests(Case):
    def install(self, provider, data, **options):
        self.pin(provider, data)
        return cli.install(provider, self.root, opener=FakeOpener(data), **MACHINE, **options)

    def leftover(self, name, age):
        folder = self.root / "tools" / name
        (folder / "installed/sub").mkdir(parents=True)
        (folder / "package.tgz").write_bytes(b"x" * 100)
        past = time.time() - age
        for path in (folder / "installed/sub", folder / "installed", folder / "package.tgz", folder):
            os.utime(path, (past, past))
        return folder

    def test_old_leftover_download_folders_are_swept_before_a_new_install(self):
        old = self.leftover(f".claude-download-{os.getpid() + 7}-1", cli.STALE_WORK + 60)
        old_codex = self.leftover(".codex-download-1-2", cli.STALE_WORK + 60)
        fresh = self.leftover(".claude-download-99999-3", 5)  # another process may be working in it
        mine = self.leftover(".claude-download-4-5", cli.STALE_WORK + 60)  # a job in this process, however quiet
        unrelated = self.leftover(".other-download-1-1", cli.STALE_WORK + 60)
        cli._working.add(mine)
        self.addCleanup(cli._working.discard, mine)
        self.install("claude", tarball("claude"), run=passes("claude"))
        self.assertFalse(old.exists() or old_codex.exists())
        self.assertTrue(fresh.exists() and mine.exists() and unrelated.exists())

    def test_the_space_check_does_not_count_what_a_sweep_frees(self):
        self.leftover(".claude-download-1-1", cli.STALE_WORK + 60)
        seen = []

        def usage(path):
            seen.append(sorted(entry.name for entry in Path(path).iterdir()))
            return mock.Mock(free=10**12)

        with mock.patch.object(cli.shutil, "disk_usage", usage):
            self.install("claude", tarball("claude"), run=passes("claude"))
        self.assertEqual(seen, [[]])

    def test_folders_of_older_versions_go_after_a_successful_install_only(self):
        old = self.root / "tools/claude-2.0.1"
        self.executable(old / "claude")
        other = self.root / "tools/codex-0.100.0"
        self.executable(other / "bin/codex")
        notes = self.root / "tools/claude-notes"
        notes.mkdir()
        data = tarball("claude")
        self.pin("claude", data)
        with self.assertRaises(cli.InstallProblem):
            cli.install("claude", self.root, opener=FakeOpener(data[:-1] + b"X"), **MACHINE)
        self.assertTrue(old.exists())
        cli.install("claude", self.root, opener=FakeOpener(data), run=passes("claude"), **MACHINE)
        self.assertFalse(old.exists())
        self.assertTrue(other.exists() and notes.exists() and (self.root / "tools/claude-2.1.269/claude").exists())

    def test_cancel_between_files_leaves_nothing(self):
        cancelled = threading.Event()
        original = shutil.copyfileobj

        def copy(source, output, *args):
            original(source, output, *args)
            cancelled.set()

        data = tarball("codex")
        self.pin("codex", data)
        with mock.patch.object(cli.shutil, "copyfileobj", copy):
            with self.assertRaises(cli.Cancelled):
                cli.install("codex", self.root, opener=FakeOpener(data), cancelled=cancelled, **MACHINE)
        self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_cancel_during_the_version_check_leaves_nothing(self):
        cancelled = threading.Event()
        said = passes("claude")

        def run(command, **options):
            cancelled.set()
            return said(command, **options)

        data = tarball("claude")
        self.pin("claude", data)
        with self.assertRaises(cli.Cancelled):
            cli.install("claude", self.root, opener=FakeOpener(data), cancelled=cancelled, run=run, **MACHINE)
        self.assertEqual(list((self.root / "tools").iterdir()), [])
        self.assertFalse(cli.installed("claude", self.root))

    def test_cancel_during_the_final_step_leaves_nothing(self):
        cancelled = threading.Event()
        replace = os.replace

        def late(source, destination):
            replace(source, destination)
            cancelled.set()

        data = tarball("claude")
        self.pin("claude", data)
        with mock.patch.object(cli.os, "replace", late):
            with self.assertRaises(cli.Cancelled):
                cli.install("claude", self.root, opener=FakeOpener(data), cancelled=cancelled, run=passes("claude"), **MACHINE)
        self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_a_folder_that_is_not_a_working_install_does_not_block_the_install(self):
        target = self.root / "tools/claude-2.1.269"
        (target / "junk/deeper").mkdir(parents=True)
        (target / "junk/deeper/file").write_text("partial")
        path = self.install("claude", tarball("claude"), run=passes("claude"))
        self.assertEqual([entry.name for entry in path.parent.iterdir()], ["claude"])
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual(sorted(entry.name for entry in (self.root / "tools").iterdir()), ["claude-2.1.269", "claude-current"])
        shutil.rmtree(target)
        target.write_text("a stray file")
        path = self.install("claude", tarball("claude"), run=passes("claude"))
        self.assertTrue(path.is_file())

    def test_a_working_install_that_appears_meanwhile_is_kept(self):
        data = tarball("claude")
        self.pin("claude", data)
        target = self.root / "tools/claude-2.1.269"

        def other_process(command, **options):
            self.executable(target / "claude", "#!/bin/sh\necho theirs\n")
            return passes("claude")(command, **options)

        path = cli.install("claude", self.root, opener=FakeOpener(data), run=other_process, **MACHINE)
        self.assertIn("theirs", path.read_text())
        self.assertEqual(sorted(entry.name for entry in (self.root / "tools").iterdir()), ["claude-2.1.269", "claude-current"])

    def test_running_out_of_space_while_unpacking_gives_a_plain_message(self):
        def full(source, output, *args):
            raise OSError(errno.ENOSPC, "No space left on device")

        data = tarball("claude")
        self.pin("claude", data)
        with mock.patch.object(cli.shutil, "copyfileobj", full):
            with self.assertRaisesRegex(cli.InstallProblem, "^There is not enough free disk space for Claude Code") as raised:
                cli.install("claude", self.root, opener=FakeOpener(data), **MACHINE)
        self.assertNotIn("Errno", str(raised.exception))
        self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_another_write_error_while_unpacking_is_not_shown_raw(self):
        data = tarball("claude")
        self.pin("claude", data)
        with mock.patch.object(cli.shutil, "copyfileobj", side_effect=PermissionError(13, "denied")):
            with self.assertRaisesRegex(cli.InstallProblem, "could not be written"):
                cli.install("claude", self.root, opener=FakeOpener(data), **MACHINE)

    def test_a_link_that_is_not_needed_is_ignored_and_one_that_is_needed_is_refused(self):
        data = tarball("claude", links=[("link-to-nothing", "/etc/passwd")])
        self.pin("claude", data)
        self.assertTrue(cli.install("claude", self.root, opener=FakeOpener(data), run=passes("claude"), **MACHINE).is_file())
        shutil.rmtree(self.root / "tools/claude-2.1.269")
        data = tarball("claude", links=[("claude", "/bin/sh")])
        self.pin("claude", data)
        with self.assertRaisesRegex(cli.InstallProblem, "unexpected layout"):
            cli.install("claude", self.root, opener=FakeOpener(data), **MACHINE)


class BackgroundDownloadTests(Case):
    def start(self, provider, opener):
        return cli.start_download(provider, environ=self.environ, opener=opener, **MACHINE)

    def status(self, provider):
        return cli.status(provider, environ=self.environ, **MACHINE)

    def test_not_downloaded_then_ready_with_progress_reported_in_between(self):
        data = tarball("claude")
        self.pin("claude", data)
        self.assertEqual(self.status("claude"), {"phase": "not_downloaded", "progress": None, "message": None, "version": "2.1.269"})
        gate = threading.Event()
        started = self.start("claude", FakeOpener(data, gate=gate))
        self.assertEqual((started["phase"], started["version"]), ("downloading", "2.1.269"))
        deadline = threading.Event()
        for _ in range(200):
            if (self.status("claude")["progress"] or 0) > 0:
                break
            deadline.wait(0.02)
        middle = self.status("claude")
        self.assertEqual(middle["phase"], "downloading")
        self.assertTrue(0 < middle["progress"] < 100, middle)
        gate.set()
        self.wait("claude")
        self.assertEqual(self.status("claude"), {"phase": "ready", "progress": 100, "message": None, "version": "2.1.269"})
        self.assertEqual(cli.resolve("claude", self.environ)["source"], "managed")

    def test_starting_twice_downloads_once(self):
        data = tarball("claude")
        self.pin("claude", data)
        gate = threading.Event()
        opener = FakeOpener(data, gate=gate)
        self.start("claude", opener)
        self.assertEqual(self.start("claude", opener)["phase"], "downloading")
        gate.set()
        self.wait("claude")
        self.assertEqual(len(opener.urls), 1)
        self.assertEqual(self.start("claude", FakeOpener(error=AssertionError("again")))["phase"], "ready")

    def test_cancel_stops_the_download_and_leaves_nothing_behind(self):
        data = tarball("claude")
        self.pin("claude", data)
        gate = threading.Event()
        self.start("claude", FakeOpener(data, gate=gate))
        self.addCleanup(gate.set)
        threading.Timer(0.3, gate.set).start()
        result = cli.cancel_download("claude", environ=self.environ, **MACHINE)
        self.assertEqual(result["phase"], "not_downloaded")
        self.assertIn("cancelled", result["message"])
        self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_a_failure_is_an_error_with_a_reason_and_a_retry_works(self):
        data = tarball("claude")
        self.pin("claude", data)
        self.start("claude", FakeOpener(error=OSError("offline")))
        self.wait("claude")
        failed = self.status("claude")
        self.assertEqual(failed["phase"], "error")
        self.assertIn("internet connection", failed["message"])
        self.start("claude", FakeOpener(data))
        self.wait("claude")
        self.assertEqual(self.status("claude")["phase"], "ready")

    def test_two_starts_at_the_same_moment_start_one_download_and_it_can_be_cancelled(self):
        data = tarball("claude")
        self.pin("claude", data)
        gate = threading.Event()
        self.addCleanup(gate.set)
        opener = FakeOpener(data, gate=gate)
        real = cli.package

        def slow(provider, target):
            time.sleep(0.05)  # widens the window between "is it running?" and "register it"
            return real(provider, target)

        barrier = threading.Barrier(6)
        results = []

        def call():
            barrier.wait()
            results.append(self.start("claude", opener)["phase"])

        with mock.patch.object(cli, "package", slow):
            callers = [threading.Thread(target=call) for _ in range(6)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join(10)
        self.assertEqual(results, ["downloading"] * 6)
        self.assertEqual(len([t for t in threading.enumerate() if t.name == "claude-download"]), 1)
        job = cli._jobs["claude"]
        self.assertTrue(job["thread"].is_alive())
        threading.Timer(0.2, gate.set).start()
        cancelled = cli.cancel_download("claude", environ=self.environ, **MACHINE)
        self.assertEqual(cancelled["phase"], "not_downloaded")
        self.assertFalse(job["thread"].is_alive())
        self.assertEqual(len(opener.urls), 1)
        self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_after_a_cancel_the_row_never_turns_ready_later(self):
        data = tarball("claude")
        self.pin("claude", data)
        release = threading.Event()
        self.addCleanup(release.set)
        said = passes("claude")

        def slow_check(command, **options):
            release.wait(10)  # the version check is still running when Cancel is pressed
            return said(command, **options)

        cli.start_download("claude", environ=self.environ, opener=FakeOpener(data), run=slow_check, **MACHINE)
        job = cli._jobs["claude"]
        for _ in range(200):
            if job["progress"] >= 99 or not job["thread"].is_alive():
                break
            time.sleep(0.02)
        time.sleep(0.2)
        result = cli.cancel_download("claude", environ=self.environ, wait=0.3, **MACHINE)
        self.assertEqual((result["phase"], result["message"]), ("not_downloaded", "The download was cancelled."))
        release.set()
        job["thread"].join(10)
        self.assertFalse(job["thread"].is_alive())
        self.assertEqual(self.status("claude")["phase"], "not_downloaded")
        self.assertEqual(list((self.root / "tools").iterdir()), [])

    def test_a_cancel_with_nothing_running_changes_nothing(self):
        self.assertEqual(cli.cancel_download("codex", environ=self.environ, **MACHINE)["phase"], "not_downloaded")

    def test_unsupported_machine_or_a_service_outside_a_folder(self):
        self.assertEqual(cli.status("claude", environ=self.environ, system="Linux", machine="ppc64le")["phase"], "unsupported")
        outside = cli.start_download("claude", environ={}, opener=FakeOpener(error=AssertionError("no")), **MACHINE)
        self.assertEqual(outside["phase"], "unsupported")
        self.assertIn("own folder", outside["message"])

    def test_ready_is_judged_from_the_files_so_it_survives_a_restart(self):
        self.executable(cli.managed_path("codex", self.root))
        self.assertEqual(self.status("codex")["phase"], "ready")


if __name__ == "__main__":
    unittest.main()
