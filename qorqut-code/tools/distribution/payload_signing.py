"""Sign every program and library inside a Mac package with a Developer ID certificate.

Build tooling only; runs on macOS. Apple's notary service opens the disk image, the package tar.gz inside the
app and the wheels inside that package, and rejects the whole upload when any Mach-O file in them lacks the
Developer ID signature, a secure timestamp or (for a program) the hardened runtime. electron-builder signs only
the Electron app, so the files of the shipped Python, git, tmux and agent-browser are signed here, before the
package's checksums are recorded.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile

CODESIGN = '/usr/bin/codesign'
# The same hardened-runtime entitlements the desktop app is signed with; only the shipped Python gets them.
ENTITLEMENTS = Path(__file__).resolve().parent / 'macos_app' / 'entitlements.plist'
PYTHON_PROGRAM = re.compile(r'python3(\.\d+)?')
# Files of installed packages were signed inside their wheels (see native_build.strip_wheel).
INSTALLED_PACKAGES = 'site-packages'

MH_EXECUTE = 2
_THIN = {b'\xcf\xfa\xed\xfe': '<', b'\xce\xfa\xed\xfe': '<', b'\xfe\xed\xfa\xcf': '>', b'\xfe\xed\xfa\xce': '>'}
_FAT = {b'\xca\xfe\xba\xbe': 20, b'\xca\xfe\xba\xbf': 32}  # bytes per architecture entry


def macho_kind(stream):
    """'program', 'library' or None (not a Mach-O file) for a file opened in binary mode."""
    magic = stream.read(4)
    offset = 0
    if magic in _FAT:
        count = struct.unpack('>I', stream.read(4))[0]
        if not 0 < count < 16:  # a Java class file starts with the same four bytes
            return None
        entry = stream.read(_FAT[magic])
        # cputype, cpusubtype, then the slice offset (32 bits, or 64 bits in the large form)
        offset = struct.unpack('>Q', entry[8:16])[0] if _FAT[magic] == 32 else struct.unpack('>I', entry[8:12])[0]
        stream.seek(offset)
        magic = stream.read(4)
    order = _THIN.get(magic)
    if order is None:
        return None
    header = stream.read(12)
    if len(header) < 12:
        return None
    filetype = struct.unpack(order + 'I', header[8:12])[0]
    return 'program' if filetype == MH_EXECUTE else 'library'


def kind_of_file(path):
    with open(path, 'rb') as stream:
        return macho_kind(stream)


def sign_command(path, identity, kind):
    argv = [CODESIGN, '--force', '--timestamp', '--sign', identity]
    if kind == 'program':
        argv += ['--options', 'runtime']
        if PYTHON_PROGRAM.fullmatch(Path(path).name):
            argv += ['--entitlements', str(ENTITLEMENTS)]
    return argv + [str(path)]


def sign_file(path, identity, kind):
    path = Path(path)
    mode = path.stat().st_mode
    if not mode & stat.S_IWUSR:
        path.chmod(mode | stat.S_IWUSR)
    subprocess.run(sign_command(path, identity, kind), check=True, capture_output=True, text=True)


def sign_bytes(name, data, identity):
    """The signed content of one file taken from a wheel; anything that is not a Mach-O file comes back unchanged."""
    kind = macho_kind(io.BytesIO(data))
    if kind is None:
        return data
    with tempfile.TemporaryDirectory(prefix='qorqbot-sign-') as folder:
        path = Path(folder) / Path(name).name
        path.write_bytes(data)
        sign_file(path, identity, kind)
        return path.read_bytes()


def mach_o_files(root, skip=()):
    """(path, kind) of every Mach-O file under root, in a fixed order; links are never followed."""
    found = []
    for folder, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in skip)
        for name in sorted(names):
            path = Path(folder) / name
            if path.is_symlink() or not path.is_file():
                continue
            kind = kind_of_file(path)
            if kind:
                found.append((path, kind))
    return found


def sign_tree(root, identity, skip=()):
    """Sign every Mach-O file under root; return [(path, kind)] of what was signed."""
    signed = mach_o_files(root, skip)
    for path, kind in signed:
        sign_file(path, identity, kind)
    return signed


def problems_with(path, kind):
    """What Apple would refuse about one signed file, read back from the file itself."""
    shown = subprocess.run([CODESIGN, '-dvv', str(path)], capture_output=True, text=True).stderr
    problems = []
    if 'Authority=Developer ID Application' not in shown:
        problems.append('not signed with a Developer ID Application certificate')
    if not re.search(r'^Timestamp=', shown, re.M):
        problems.append('no secure timestamp')
    if kind == 'program' and not re.search(r'flags=0x[0-9a-f]+\([^)]*\bruntime\b', shown):
        problems.append('hardened runtime is off')
    checked = subprocess.run([CODESIGN, '--verify', '--strict', str(path)], capture_output=True, text=True)
    if checked.returncode != 0:
        problems.append('signature does not verify: ' + checked.stderr.strip())
    return problems


def check_tree(root):
    """['path: problem', ...] for every Mach-O file under root that Apple would refuse; empty when all are fine."""
    return [f'{path.relative_to(root)}: {problem}'
            for path, kind in mach_o_files(root) for problem in problems_with(path, kind)]
