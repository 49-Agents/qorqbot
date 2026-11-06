"""First launch installs the pinned agent-browser; a missing Chrome is reported, never installed.

    python3.12 -m unittest discover -s tools/distribution/tests -v

Downloads are faked: nothing here contacts the network.
"""
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_launch  # noqa: E402

LOCK = Path(__file__).resolve().parents[3] / 'app/tools/browser/package-lock.json'
FAKE_BINARY = b'#!/bin/sh\necho "agent-browser 0.38.1"\n'


def package(binary_name, content=FAKE_BINARY):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        for name, body in (('package/package.json', b'{"name":"agent-browser"}'), ('package/bin/' + binary_name, content)):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(body))
    return data.getvalue()


class FakeDownload:
    def __init__(self, data=None, integrity=native_launch.AGENT_BROWSER_SHA512, error=None):
        self.data, self.integrity, self.error, self.calls = data, integrity, error, []

    def __call__(self, url, destination, limit):
        self.calls.append(url)
        if self.error:
            raise self.error
        destination.write_bytes(self.data)
        return self.integrity


class AgentBrowserInstallTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def install(self, download, **machine):
        machine = machine or {'system': 'Darwin', 'machine': 'arm64'}
        return native_launch.ensure_agent_browser(self.root, download=download, **machine)

    def test_the_pin_matches_the_repository_lockfile(self):
        locked = json.loads(LOCK.read_text())['packages']['node_modules/agent-browser']
        self.assertEqual(locked['version'], native_launch.AGENT_BROWSER_VERSION)
        self.assertEqual(locked['resolved'], native_launch.AGENT_BROWSER_URL)
        self.assertEqual(locked['integrity'], 'sha512-' + native_launch.AGENT_BROWSER_SHA512)

    def test_installs_only_this_machines_binary(self):
        download = FakeDownload(package('agent-browser-darwin-arm64'))
        path, problem = self.install(download)
        self.assertIsNone(problem)
        self.assertEqual(path, native_launch.agent_browser_path(self.root))
        self.assertEqual(path.read_bytes(), FAKE_BINARY)
        self.assertTrue(os.access(path, os.X_OK))
        self.assertEqual([entry.name for entry in path.parent.parent.iterdir()], [path.parent.name])

    def test_an_installed_copy_is_not_downloaded_again(self):
        self.install(FakeDownload(package('agent-browser-darwin-arm64')))
        again = FakeDownload(error=AssertionError('downloaded twice'))
        path, problem = self.install(again)
        self.assertTrue(path.is_file())
        self.assertEqual(again.calls, [])

    def test_offline_is_reported_and_retried_later(self):
        path, problem = self.install(FakeDownload(error=OSError('offline')))
        self.assertIsNone(path)
        self.assertIn('next start', problem)
        self.assertFalse(native_launch.agent_browser_path(self.root).exists())
        self.assertEqual(list((self.root / 'tools').iterdir()), [])

    def test_a_checksum_mismatch_installs_nothing(self):
        with self.assertRaises(RuntimeError):
            self.install(FakeDownload(package('agent-browser-darwin-arm64'), integrity='wrong'))
        self.assertFalse(native_launch.agent_browser_path(self.root).exists())

    def test_a_binary_that_does_not_run_installs_nothing(self):
        with self.assertRaises(RuntimeError):
            self.install(FakeDownload(package('agent-browser-darwin-arm64', b'#!/bin/sh\nexit 3\n')))
        self.assertFalse(native_launch.agent_browser_path(self.root).exists())

    def test_machines(self):
        name = native_launch.agent_browser_binary_name
        self.assertEqual(name('Darwin', 'arm64'), 'agent-browser-darwin-arm64')
        self.assertEqual(name('Linux', 'x86_64'), 'agent-browser-linux-x64')
        self.assertIsNone(name('Darwin', 'x86_64'))
        path, problem = self.install(FakeDownload(error=AssertionError('no download')), system='Linux', machine='ppc64le')
        self.assertIsNone(path)
        self.assertIn('not available', problem)

    def test_the_service_uses_the_installed_copy_unless_the_owner_chose_one(self):
        self.install(FakeDownload(package('agent-browser-darwin-arm64')))
        home = self.root / 'home'
        with mock.patch.dict(os.environ, {'HOME': str(self.root), 'PATH': '/usr/bin:/bin', 'QORQUT_TIMEZONE': 'UTC'}, clear=True):
            env = native_launch.environment(self.root, home)
            self.assertEqual(env['QORQUT_AGENT_BROWSER_EXECUTABLE'], str(native_launch.agent_browser_path(self.root)))
            os.environ['QORQUT_AGENT_BROWSER_EXECUTABLE'] = '/opt/mine/agent-browser'
            env = native_launch.environment(self.root, home)
            self.assertEqual(env['QORQUT_AGENT_BROWSER_EXECUTABLE'], '/opt/mine/agent-browser')


class ChromeTests(unittest.TestCase):
    def test_missing_chrome_is_a_readiness_note(self):
        with mock.patch.object(native_launch, 'chrome_installed', return_value=False):
            notes = native_launch.readiness_notes('/nonexistent', system='Linux')
        self.assertTrue(any('Google Chrome' in note and native_launch.CHROME_URL in note for note in notes))
        with mock.patch.object(native_launch, 'chrome_installed', return_value=True):
            self.assertFalse(any('Chrome' in note for note in native_launch.readiness_notes('/nonexistent', system='Linux')))

    def test_chrome_is_found_on_path(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        self.assertFalse(native_launch.chrome_installed(str(directory), system='Linux'))
        chrome = directory / 'google-chrome'
        chrome.write_text('#!/bin/sh\n')
        chrome.chmod(0o755)
        self.assertTrue(native_launch.chrome_installed(str(directory), system='Linux'))


if __name__ == '__main__':
    unittest.main()
