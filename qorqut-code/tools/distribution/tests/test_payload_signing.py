"""The programs and libraries of a Mac package are signed for Apple's notary service.

    python3.12 -m unittest discover -s tools/distribution/tests -v

codesign is a stand-in that records its command line and marks the file it "signed"; no Mac or certificate is used.
"""
import base64
import csv
import hashlib
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native_build  # noqa: E402
import payload_signing  # noqa: E402

IDENTITY = 'ACCFA2F8E794212F54EE03754F48798355F95B07'
MARK = b'\x00SIGNED'
HEADER_FIELDS = (0x0100000C, 0)  # arm64


def macho(filetype, little=True):
    """The first bytes of a 64-bit Mach-O file of the given type, padded like a real file."""
    if little:
        return b'\xcf\xfa\xed\xfe' + struct.pack('<III', *HEADER_FIELDS, filetype) + bytes(64)
    return b'\xfe\xed\xfa\xcf' + struct.pack('>III', *HEADER_FIELDS, filetype) + bytes(64)


def fat(slice_bytes, large=False):
    entry = (struct.pack('>IIQQII', *HEADER_FIELDS, 64, len(slice_bytes), 14, 0) if large
             else struct.pack('>IIIII', *HEADER_FIELDS, 64, len(slice_bytes), 14))
    magic = b'\xca\xfe\xba\xbf' if large else b'\xca\xfe\xba\xbe'
    return (magic + struct.pack('>I', 1) + entry).ljust(64, b'\0') + slice_bytes


PROGRAM, LIBRARY = macho(payload_signing.MH_EXECUTE), macho(6)


def put(path, data, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)
    return path


class FakeCodesign:
    """Stands in for subprocess.run: codesign marks files, reads back what it marked; other commands succeed."""

    def __init__(self, unsigned=(), fail=()):
        self.signed, self.runtime, self.calls, self.others = {}, {}, [], []
        self.unsigned, self.fail = set(unsigned), set(fail)

    def __call__(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        if argv[0] != payload_signing.CODESIGN:
            self.others.append(argv)
            code = 1 if Path(argv[0]).name in self.fail else 0
            return subprocess.CompletedProcess(argv, code, '', 'it crashed' if code else '')
        self.calls.append(argv)
        target = Path(argv[-1])
        if '--sign' in argv:
            target.write_bytes(target.read_bytes() + MARK)
            self.runtime[target.name] = '--options' in argv
            return subprocess.CompletedProcess(argv, 0, '', '')
        if '--verify' in argv:
            return subprocess.CompletedProcess(argv, 0, '', '')
        good = target.read_bytes().endswith(MARK) and target.name not in self.unsigned
        shown = ('Executable=x\nSignature=adhoc\nTeamIdentifier=not set\n' if not good else
                 'Authority=Developer ID Application: Someone (TEAM)\nAuthority=Apple Root CA\n'
                 'Timestamp=22 Nov 2026 at 15:03:20\n' + ('CodeDirectory v=20500 flags=0x10000(runtime) hashes=1\n'
                                                           if self.runtime.get(target.name) else 'CodeDirectory v=20500 flags=0x0(none) hashes=1\n'))
        return subprocess.CompletedProcess(argv, 0, '', shown)

    def signing(self, name):
        return next(argv for argv in self.calls if '--sign' in argv and Path(argv[-1]).name == name)


class Detect(unittest.TestCase):
    def kind(self, data):
        return payload_signing.macho_kind(io.BytesIO(data))

    def test_programs_and_libraries_are_told_apart(self):
        self.assertEqual(self.kind(PROGRAM), 'program')
        self.assertEqual(self.kind(LIBRARY), 'library')
        self.assertEqual(self.kind(macho(8)), 'library')  # a bundle, such as a Python extension module
        self.assertEqual(self.kind(macho(payload_signing.MH_EXECUTE, little=False)), 'program')

    def test_a_universal_file_is_read_by_its_first_slice(self):
        self.assertEqual(self.kind(fat(PROGRAM)), 'program')
        self.assertEqual(self.kind(fat(LIBRARY, large=True)), 'library')

    def test_other_files_are_not_signed(self):
        self.assertIsNone(self.kind(b'#!/bin/sh\necho hi\n'))
        self.assertIsNone(self.kind(b''))
        self.assertIsNone(self.kind(b'\xca\xfe\xba\xbe' + struct.pack('>I', 52) + bytes(32)))  # a Java class file


class SignTree(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name).resolve() / 'runtime'
        put(self.runtime / 'native/bin/git', PROGRAM)
        put(self.runtime / 'native/bin/git-upload-pack', PROGRAM, 0o555)  # read-only until signed
        put(self.runtime / 'native/lib/libjemalloc.2.dylib', LIBRARY)
        put(self.runtime / 'native/bin/helper.sh', b'#!/bin/sh\n')
        (self.runtime / 'native/bin/git-add').symlink_to('git')
        put(self.runtime / 'venv/bin/python3.12', PROGRAM)
        put(self.runtime / 'venv/lib/python3.12/lib-dynload/_dbm.cpython-312-darwin.so', macho(8))
        put(self.runtime / 'venv/lib/python3.12/site-packages/yaml/_yaml.cpython-312-darwin.so', macho(8))
        self.fake = FakeCodesign()
        patch = mock.patch('subprocess.run', self.fake)
        patch.start()
        self.addCleanup(patch.stop)

    def test_every_compiled_file_is_signed_with_a_timestamp_and_programs_with_the_hardened_runtime(self):
        signed = payload_signing.sign_tree(self.runtime / 'native', IDENTITY)
        self.assertEqual({(p.name, k) for p, k in signed},
                         {('git', 'program'), ('git-upload-pack', 'program'), ('libjemalloc.2.dylib', 'library')})
        for name in ('git', 'git-upload-pack', 'libjemalloc.2.dylib'):
            self.assertEqual(self.fake.signing(name)[:5], [payload_signing.CODESIGN, '--force', '--timestamp', '--sign', IDENTITY])
        self.assertEqual(self.fake.signing('git')[5:7], ['--options', 'runtime'])
        self.assertEqual(self.fake.signing('git-upload-pack')[5:7], ['--options', 'runtime'])
        self.assertNotIn('--entitlements', self.fake.signing('git'))
        self.assertNotIn('--options', self.fake.signing('libjemalloc.2.dylib'))

    def test_only_the_shipped_python_gets_entitlements(self):
        payload_signing.sign_tree(self.runtime / 'venv', IDENTITY, skip=(payload_signing.INSTALLED_PACKAGES,))
        argv = self.fake.signing('python3.12')
        self.assertEqual(argv[argv.index('--entitlements') + 1], str(payload_signing.ENTITLEMENTS))
        self.assertTrue(payload_signing.ENTITLEMENTS.is_file())
        self.assertNotIn('--entitlements', self.fake.signing('_dbm.cpython-312-darwin.so'))

    def test_installed_libraries_are_left_to_their_wheels(self):
        signed = payload_signing.sign_tree(self.runtime / 'venv', IDENTITY, skip=(payload_signing.INSTALLED_PACKAGES,))
        self.assertEqual(sorted(p.name for p, _ in signed), ['_dbm.cpython-312-darwin.so', 'python3.12'])

    def test_a_clean_tree_has_no_problems(self):
        payload_signing.sign_tree(self.runtime / 'native', IDENTITY)
        self.assertEqual(payload_signing.check_tree(self.runtime / 'native'), [])

    def test_unsigned_and_ad_hoc_files_are_named(self):
        payload_signing.sign_tree(self.runtime / 'native', IDENTITY)
        put(self.runtime / 'native/bin/late', PROGRAM)  # never signed: an ad-hoc file slipped in
        problems = payload_signing.check_tree(self.runtime / 'native')
        self.assertEqual(len(problems), 3)
        self.assertTrue(all(line.startswith('bin/late: ') for line in problems))
        self.assertTrue(any('hardened runtime' in line for line in problems))
        self.assertTrue(any('secure timestamp' in line for line in problems))

    def test_a_program_signed_without_the_hardened_runtime_is_a_problem(self):
        payload_signing.sign_tree(self.runtime / 'native', IDENTITY)
        self.fake.runtime['git'] = False
        self.assertEqual(payload_signing.check_tree(self.runtime / 'native'),
                         ['bin/git: hardened runtime is off'])


class SignedWheels(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.fake = FakeCodesign()
        patch = mock.patch('subprocess.run', self.fake)
        patch.start()
        self.addCleanup(patch.stop)
        self.source = self.folder / 'demo-1.0-cp312-cp312-macosx_11_0_arm64.whl'
        with zipfile.ZipFile(self.source, 'w') as wheel:
            wheel.writestr('demo/__init__.py', b'x = 1\n')
            wheel.writestr('demo/_speed.cpython-312-darwin.so', macho(8))
            wheel.writestr('demo/tests/test_x.py', b'pass\n')
            wheel.writestr('demo-1.0.dist-info/METADATA', b'Name: demo\nVersion: 1.0\n')
            wheel.writestr('demo-1.0.dist-info/RECORD', b'')

    def stripped(self, identity=None):
        target = self.folder / 'out.whl'
        record = native_build.strip_wheel(self.source, target, identity)
        with zipfile.ZipFile(target) as wheel:
            return record, target, {name: wheel.read(name) for name in wheel.namelist()}

    def test_a_signed_wheel_holds_signed_files_and_a_matching_record(self):
        record, target, members = self.stripped(IDENTITY)
        signed = members['demo/_speed.cpython-312-darwin.so']
        self.assertEqual(signed, macho(8) + MARK)
        self.assertEqual(members['demo/__init__.py'], b'x = 1\n')
        rows = {row[0]: row for row in csv.reader(io.StringIO(members['demo-1.0.dist-info/RECORD'].decode()))}
        digest = base64.urlsafe_b64encode(hashlib.sha256(signed).digest()).decode().rstrip('=')
        self.assertEqual(rows['demo/_speed.cpython-312-darwin.so'][1:], ['sha256=' + digest, str(len(signed))])
        self.assertEqual(record['runtime_sha256'], hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(len(self.fake.calls), 1)  # only the compiled file is signed

    def test_without_an_identity_a_wheel_is_only_stripped(self):
        _record, _target, members = self.stripped()
        self.assertEqual(members['demo/_speed.cpython-312-darwin.so'], macho(8))
        self.assertEqual(self.fake.calls, [])


class SignShippedPrograms(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name).resolve() / 'qorqbot/runtime'
        for name in ('git', 'agent-browser'):
            put(self.runtime / 'native/bin' / name, PROGRAM)
        put(self.runtime / 'native/lib/libjemalloc.2.dylib', LIBRARY)
        put(self.runtime / 'venv/bin/python3.12', PROGRAM)
        put(self.runtime / 'venv/lib/python3.12/lib-dynload/_dbm.cpython-312-darwin.so', macho(8))
        # an installed library: signed inside its wheel, so here it is already marked and only checked
        put(self.runtime / 'venv/lib/python3.12/site-packages/yaml/_yaml.cpython-312-darwin.so', macho(8) + MARK)

    def run_with(self, fake):
        with mock.patch('subprocess.run', fake):
            return native_build.sign_shipped_programs(self.runtime, IDENTITY)

    def test_everything_is_signed_checked_and_started(self):
        fake = FakeCodesign()
        record = self.run_with(fake)
        self.assertEqual((record['programs'], record['libraries']), (3, 2))
        self.assertIn('native/bin/agent-browser', record['files'])
        self.assertNotIn('venv/lib/python3.12/site-packages/yaml/_yaml.cpython-312-darwin.so', record['files'])
        self.assertEqual([Path(argv[0]).name for argv in fake.others], ['git', 'agent-browser', 'python3.12'])

    def test_a_file_apple_would_refuse_stops_the_build(self):
        with self.assertRaisesRegex(ValueError, 'Signing check failed'):
            self.run_with(FakeCodesign(unsigned={'_yaml.cpython-312-darwin.so'}))

    def test_a_signed_program_that_no_longer_starts_stops_the_build(self):
        with self.assertRaisesRegex(ValueError, 'signed agent-browser does not start'):
            self.run_with(FakeCodesign(fail={'agent-browser'}))

    def test_signing_needs_the_bundled_tools(self):
        with self.assertRaisesRegex(ValueError, 'add --bundled-tools'):
            native_build.main(['--prepared', '/x', '--output', '/y', '--report', '/z', '--version', '1.0.0',
                               '--target', 'macos14-arm64', '--sign-identity', IDENTITY])


if __name__ == '__main__':
    unittest.main()
