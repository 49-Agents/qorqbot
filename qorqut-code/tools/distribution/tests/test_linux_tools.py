"""The Linux tools build refuses unpinned sources and any program that needs more than the C library.

    python3.12 -m unittest discover -s tools/distribution/tests -p 'test_linux_tools.py' -v

Nothing is downloaded or compiled: downloads and readelf are stand-ins, except one read of a real system program.
"""
import argparse
import contextlib
import hashlib
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import linux_tools  # noqa: E402
import macos_tools  # noqa: E402
import native_launch  # noqa: E402

DYNAMIC = """
Dynamic section at offset 0x1000 contains 6 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [{}]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
{}"""
VERSIONS = """
Version needs section '.gnu.version_r' contains 1 entry:
 000000: Version: 1  File: libc.so.6  Cnt: 3
  0x0010:   Name: GLIBC_2.2.5  Flags: none  Version: 4
  0x0020:   Name: {}  Flags: none  Version: 3
"""


def readelf(needed='libm.so.6', rpath='', glibc='GLIBC_2.34'):
    """Stand-in for the two readelf calls: the dynamic section and the version needs."""
    tag = f' 0x000000000000000f (RUNPATH)            Library runpath: [{rpath}]\n' if rpath else ''
    answers = iter([DYNAMIC.format(needed, tag), VERSIONS.format(glibc)])
    return mock.patch.object(linux_tools.subprocess, 'run',
                             side_effect=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=next(answers)))


class Pins(unittest.TestCase):
    def test_every_source_is_pinned_by_a_sha256_over_https(self):
        for name, source in linux_tools.SOURCES.items():
            self.assertRegex(source['sha256'], r'^[0-9a-f]{64}$', name)
            self.assertTrue(source['url'].startswith('https://'), name)
            self.assertTrue(source['version'] and source['license'], name)

    def test_what_both_systems_ship_has_one_pin(self):
        self.assertEqual(linux_tools.SOURCES['git'], macos_tools.SOURCES['git'])
        python = linux_tools.SOURCES['python-x86_64']
        self.assertEqual(python['version'], macos_tools.PYTHON_VERSION + '+' + macos_tools.PYTHON_BUILD)
        self.assertTrue(python['url'].endswith('-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'))

    def test_every_shipped_library_has_its_license_copied(self):
        for name in ('git', 'zlib', 'openssl', 'curl'):
            self.assertIn('license_file', linux_tools.SOURCES[name], name)

    def test_the_browser_tool_is_pinned_by_the_launchers_own_checksum(self):
        self.assertEqual(native_launch.agent_browser_binary_name('Linux', 'x86_64'), 'agent-browser-linux-x64')
        self.assertTrue(native_launch.AGENT_BROWSER_SHA512)


class Fetch(unittest.TestCase):
    DATA = b'a pinned source'

    def source(self, sha256=None):
        return {'fake': {'version': '1', 'license': 'MIT', 'url': 'https://example.invalid/fake-1.tar.gz',
                         'sha256': sha256 or hashlib.sha256(self.DATA).hexdigest()}}

    def test_a_download_that_does_not_match_its_pin_is_deleted_and_refused(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(linux_tools.SOURCES, self.source('0' * 64)), \
                mock.patch.object(linux_tools.urllib.request, 'urlopen', return_value=io.BytesIO(self.DATA)):
            with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
                linux_tools.fetch('fake', Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_a_verified_earlier_download_is_reused_and_a_damaged_one_replaced(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(linux_tools.SOURCES, self.source()):
            downloads = Path(temporary)
            with mock.patch.object(linux_tools.urllib.request, 'urlopen', return_value=io.BytesIO(self.DATA)) as opened:
                path = linux_tools.fetch('fake', downloads)
                self.assertEqual(path.read_bytes(), self.DATA)
                linux_tools.fetch('fake', downloads)
                self.assertEqual(opened.call_count, 1)
                path.write_bytes(b'damaged')
                opened.return_value = io.BytesIO(self.DATA)
                self.assertEqual(linux_tools.fetch('fake', downloads).read_bytes(), self.DATA)
                self.assertEqual(opened.call_count, 2)


class ElfCheck(unittest.TestCase):
    def test_the_libraries_glibc_paths_and_version_are_read_from_readelf(self):
        with readelf(needed='libm.so.6', rpath='$ORIGIN/../lib:/build/lib', glibc='GLIBC_2.38'):
            needed, paths, glibc = linux_tools.elf_facts(Path('git'))
        self.assertEqual((needed, paths, glibc), (['libm.so.6', 'libc.so.6'], ['$ORIGIN/../lib', '/build/lib'], (2, 38)))

    @unittest.skipUnless(sys.platform == 'linux' and os.path.exists('/usr/bin/readelf') and os.path.exists('/bin/sh'), 'needs Linux binutils')
    def test_a_real_program_reports_the_c_library(self):
        needed, _, glibc = linux_tools.elf_facts(Path('/bin/sh').resolve())
        self.assertIn('libc.so.6', needed)
        self.assertEqual(glibc[0], 2)

    def test_a_program_that_needs_only_the_c_library_is_accepted(self):
        with readelf(needed='libm.so.6', rpath='$ORIGIN/../lib', glibc='GLIBC_2.34'):
            self.assertEqual(linux_tools.require_self_contained(Path('git')), (2, 34))

    def test_every_other_library_is_refused(self):
        for library in ('libz.so.1', 'libssl.so.3', 'libncursesw.so.6', 'libtinfo.so.6', 'libevent_core-2.1.so.7', 'libcurl.so.4'):
            with readelf(needed=library), self.assertRaisesRegex(RuntimeError, re.escape(library) + '.*clean Linux desktop'):
                linux_tools.require_self_contained(Path('git'))

    def test_a_search_path_outside_the_program_is_refused(self):
        with readelf(rpath='/tmp/work/static/lib'), self.assertRaisesRegex(RuntimeError, 'only exists on the build machine'):
            linux_tools.require_self_contained(Path('git'))

    def test_a_program_that_needs_a_newer_glibc_than_ubuntu_24_04_is_refused(self):
        with readelf(glibc='GLIBC_2.40'), self.assertRaisesRegex(RuntimeError, 'needs glibc 2.40; Ubuntu 24.04 has 2.39'):
            linux_tools.require_self_contained(Path('git'))
        with readelf(glibc='GLIBC_2.39'):
            self.assertEqual(linux_tools.require_self_contained(Path('git')), (2, 39))

    def test_the_allowed_libraries_are_the_glibc_ones(self):
        self.assertEqual(linux_tools.SYSTEM_LIBRARIES, {'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1',
                                                        'libutil.so.1', 'libresolv.so.2', 'ld-linux-x86-64.so.2'})

    def test_only_real_elf_files_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'program').write_bytes(b'\x7fELF\x02\x01')
            (folder / 'script').write_text('#!/bin/sh\n')
            (folder / 'link').symlink_to('program')
            self.assertEqual([linux_tools.is_elf(folder / name) for name in ('program', 'script', 'link', 'missing')],
                             [True, False, False, False])


class Container(unittest.TestCase):
    def test_the_build_runs_as_the_calling_user_in_a_container_that_installs_only_build_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            output, work = base / 'tools/out', base / 'work'
            arguments = argparse.Namespace(jobs=3, image='ubuntu:24.04', apt_mirror=None)
            with mock.patch.object(linux_tools.shutil, 'which', return_value='/usr/bin/docker'), \
                    mock.patch.object(linux_tools.subprocess, 'run') as run:
                linux_tools.in_container(arguments, output, work)
            argv = run.call_args.args[0]
            self.assertEqual(argv[:3], ['/usr/bin/docker', 'run', '--rm'])
            self.assertIn(f'{work}:{work}', argv)
            self.assertIn(f'{output.parent}:{output.parent}', argv)
            self.assertIn(str(Path(linux_tools.__file__).resolve().parent) + ':/tools:ro', argv)
            self.assertIn('ubuntu:24.04', argv)
            script = argv[-1]
            for package in linux_tools.BUILD_PACKAGES:
                self.assertIn(package, script)
            self.assertNotIn('pkg-config', script)
            self.assertIn('--no-install-recommends', script)
            self.assertIn(f'setpriv --reuid={os.getuid()} --regid={os.getgid()}', script)
            self.assertIn(f'--output {output} --work {work} --jobs 3', script)
            self.assertNotIn('sed', script)

    def test_an_ubuntu_mirror_can_replace_the_default_servers(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(linux_tools.shutil, 'which', return_value='/usr/bin/docker'), \
                mock.patch.object(linux_tools.subprocess, 'run') as run:
            arguments = argparse.Namespace(jobs=1, image='ubuntu:24.04', apt_mirror='http://mirror.example/ubuntu/')
            linux_tools.in_container(arguments, Path(temporary) / 'out', Path(temporary) / 'work')
        script = run.call_args.args[0][-1]
        self.assertTrue(script.startswith("mirror=http://mirror.example/ubuntu; sed -i"))
        self.assertLess(script.index('sed -i'), script.index('apt-get update'))

    def test_docker_must_be_installed(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(linux_tools.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'needs docker'):
                linux_tools.in_container(argparse.Namespace(jobs=1, image='x', apt_mirror=None), Path(temporary) / 'out', Path(temporary) / 'work')


class Command(unittest.TestCase):
    def refused(self, *arguments, system='linux', machine='x86_64'):
        with mock.patch.object(linux_tools.sys, 'platform', system), \
                mock.patch.object(linux_tools.platform, 'machine', return_value=machine), \
                contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
            linux_tools.main(list(arguments))
        return error.getvalue()

    def test_it_builds_only_on_linux_x86_64(self):
        self.assertIn('Linux x86-64', self.refused('--output', '/nowhere/out', '--work', '/nowhere/work', system='darwin'))
        self.assertIn('Linux x86-64', self.refused('--output', '/nowhere/out', '--work', '/nowhere/work', machine='aarch64'))

    def test_the_output_must_be_new_and_apart_from_the_work_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIn('new directory', self.refused('--output', temporary, '--work', '/nowhere/work'))
            self.assertIn('separate', self.refused('--output', temporary + '/out', '--work', temporary))


if __name__ == '__main__':
    unittest.main()
