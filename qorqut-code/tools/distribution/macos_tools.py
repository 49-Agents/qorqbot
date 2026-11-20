#!/usr/bin/env python3
"""Build the programs the Mac download carries, so a Mac needs nothing installed first.

Build tooling only; runs on macOS with Apple's command line tools, for the chip
it runs on. Output:

    OUT/python/     self-contained CPython 3.12 (python-build-standalone), unchanged
    OUT/native/     bin/git, libexec/git-core/, share/git-core/, licenses/
    OUT/TOOLS.json  versions, sources and SHA-256 of everything that was fetched

Every source is pinned by SHA-256. Nothing from Homebrew, MacPorts or Fink is
used: the build PATH holds only macOS's own folders, and the result is refused
if a program links against a library outside macOS or the output itself.

    python3 tools/distribution/macos_tools.py --output /abs/tools --work /abs/work
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request

DEPLOYMENT_TARGET = '14.0'
BUILD_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
PYTHON_VERSION = '3.12.16'
PYTHON_BUILD = '20261110'
SOURCES = {
    'python-arm64': {
        'version': PYTHON_VERSION + '+' + PYTHON_BUILD, 'license': 'PSF-2.0',
        'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/' + PYTHON_BUILD
               + '/cpython-' + PYTHON_VERSION + '+' + PYTHON_BUILD + '-aarch64-apple-darwin-install_only_stripped.tar.gz',
        'sha256': '2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd'},
    'python-x86_64': {
        'version': PYTHON_VERSION + '+' + PYTHON_BUILD, 'license': 'PSF-2.0',
        'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/' + PYTHON_BUILD
               + '/cpython-' + PYTHON_VERSION + '+' + PYTHON_BUILD + '-x86_64-apple-darwin-install_only_stripped.tar.gz',
        'sha256': 'b78a4c479f1964ec484880bee567568266f68da102269fb93a05fe8716b45f91'},
    'git': {
        'version': '2.56.1', 'license': 'GPL-2.0-only', 'license_file': 'COPYING',
        'url': 'https://github.com/git/git/archive/refs/tags/v2.56.1.tar.gz',
        'sha256': '7352791502f1b9745e7414e15e4ae9b7bb4773428b9a1b4f7beb3fdd1073aba4'},
}
# git parts Qorqbot and its agents never call: servers, mail and other version-control bridges.
GIT_UNUSED = ('bin/git-cvsserver', 'bin/git-shell', 'bin/scalar', 'libexec/git-core/git-cvsserver',
              'libexec/git-core/git-shell', 'libexec/git-core/scalar', 'libexec/git-core/git-daemon',
              'libexec/git-core/git-http-backend', 'libexec/git-core/git-imap-send',
              'libexec/git-core/git-instaweb', 'share/bash-completion', 'share/git-gui', 'share/gitk',
              'share/gitweb', 'share/locale', 'share/perl5', 'share/man')
SYSTEM_LIBRARY_ROOTS = ('/usr/lib/', '/System/Library/')


def machine():
    name = platform.machine().lower()
    if name in ('arm64', 'aarch64'):
        return 'arm64'
    if name in ('x86_64', 'amd64'):
        return 'x86_64'
    raise RuntimeError('Unsupported Mac chip: ' + name)


def sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(name, downloads):
    """Download one pinned source, or reuse a verified earlier download."""
    source = SOURCES[name]
    target = downloads / (name + '-' + source['url'].rsplit('/', 1)[1])
    if not target.is_file() or sha256(target) != source['sha256']:
        partial = target.with_name(target.name + '.part')
        with urllib.request.urlopen(source['url'], timeout=120) as response, partial.open('wb') as output:
            shutil.copyfileobj(response, output)
        if sha256(partial) != source['sha256']:
            partial.unlink()
            raise RuntimeError('Checksum mismatch for ' + source['url'])
        partial.replace(target)
    return target


def unpack(archive, target):
    """Extract a source archive without its top-level folder."""
    target.mkdir(parents=True)
    with tarfile.open(archive) as package:
        members = package.getmembers()
        top = members[0].name.split('/', 1)[0]
        for member in members:
            if member.name != top and not member.name.startswith(top + '/'):
                raise RuntimeError('Unexpected archive layout: ' + archive.name)
        package.extractall(target, filter='data')
    for item in (target / top).iterdir():
        item.rename(target / item.name)
    (target / top).rmdir()


def run(argv, cwd, env, log):
    with log.open('ab') as output:
        done = subprocess.run([str(a) for a in argv], cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    if done.returncode:
        tail = log.read_text(errors='replace').splitlines()[-25:]
        raise RuntimeError('Build step failed: ' + ' '.join(str(a) for a in argv[:3]) + '\n' + '\n'.join(tail))


def linked_libraries(binary):
    lines = subprocess.run(['/usr/bin/otool', '-L', str(binary)], capture_output=True, text=True, check=True).stdout
    return [line.split(' (', 1)[0].strip() for line in lines.splitlines()[1:] if line.strip()]


def require_self_contained(binary, allowed=()):
    """A shipped program may link only against macOS itself and the libraries shipped beside it."""
    for library in linked_libraries(binary):
        if library.startswith(SYSTEM_LIBRARY_ROOTS) or library in allowed:
            continue
        raise RuntimeError(f'{binary.name} links against {library}, which a clean Mac does not have')


def build(output, work, jobs):
    chip = machine()
    downloads, sources, logs = work / 'downloads', work / 'src', work / 'logs'
    for folder in (downloads, logs):
        folder.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(sources, ignore_errors=True)
    env = {'PATH': BUILD_PATH, 'HOME': str(work), 'LANG': 'C', 'MACOSX_DEPLOYMENT_TARGET': DEPLOYMENT_TARGET}
    unpack(fetch('git', downloads), sources / 'git')
    make = ['/usr/bin/make', '-j' + str(jobs)]

    # git with https through macOS's own libcurl; RUNTIME_PREFIX lets it find its helpers wherever the folder sits.
    git_options = ['NO_DARWIN_PORTS=1', 'NO_FINK=1', 'NO_RUST=1', 'NO_EXPAT=1', 'NO_GETTEXT=1', 'NO_PERL=1',
                   'NO_PYTHON=1', 'NO_TCLTK=1', 'NO_OPENSSL=1', 'NO_INSTALL_HARDLINKS=1', 'INSTALL_SYMLINKS=1',
                   'RUNTIME_PREFIX=YesPlease', 'prefix=/usr/local']
    run(make + git_options, sources / 'git', env, logs / 'git.log')
    stage = work / 'git-stage'
    shutil.rmtree(stage, ignore_errors=True)
    run(['/usr/bin/make', *git_options, 'DESTDIR=' + str(stage), 'install'], sources / 'git', env, logs / 'git.log')

    native = output / 'native'
    (native / 'bin').mkdir(parents=True)
    git_root = stage / 'usr/local'
    for unused in GIT_UNUSED:
        target = git_root / unused
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    for name in ('bin', 'libexec', 'share'):
        shutil.copytree(git_root / name, native / name, symlinks=True, dirs_exist_ok=True)
    licenses = native / 'licenses'
    licenses.mkdir()
    shutil.copy2(sources / 'git' / SOURCES['git']['license_file'], licenses / 'git.txt')

    unpack_python(fetch('python-' + chip, downloads), output / 'python')
    shutil.copy2(output / 'python/lib/python3.12/LICENSE.txt', licenses / 'python.txt')

    for binary in (native / 'bin/git', native / 'libexec/git-core/git-remote-http', output / 'python/bin/python3.12'):
        require_self_contained(binary)
    clean = {'PATH': '/usr/bin:/bin', 'HOME': str(work), 'LANG': 'C'}
    versions = {}
    for label, argv in (('git', [native / 'bin/git', '--version']),
                        ('python', [output / 'python/bin/python3.12', '-I', '-c', 'import sys, ssl, sqlite3; print(sys.version.split()[0])'])):
        versions[label] = subprocess.run([str(a) for a in argv], env=clean, capture_output=True, text=True, check=True).stdout.strip()
    if SOURCES['git']['version'] not in versions['git'] or versions['python'] != PYTHON_VERSION:
        raise RuntimeError('A built program reports an unexpected version: ' + json.dumps(versions))
    used = {name: {key: SOURCES[name][key] for key in ('version', 'license', 'url', 'sha256')}
            for name in ('git', 'python-' + chip)}
    (output / 'TOOLS.json').write_text(json.dumps({
        'format': 1, 'chip': chip, 'deployment_target': DEPLOYMENT_TARGET, 'reported_versions': versions,
        'sources': used}, indent=2) + '\n')
    return versions


def unpack_python(archive, target):
    with tarfile.open(archive) as package:
        for member in package.getmembers():
            if member.name != 'python' and not member.name.startswith('python/'):
                raise RuntimeError('Unexpected Python archive layout')
        package.extractall(target.parent, filter='tar')
    if not (target / 'bin/python3.12').is_file():
        raise RuntimeError('The Python archive has no bin/python3.12')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--output', type=Path, required=True, help='new directory for the built tools')
    parser.add_argument('--work', type=Path, required=True, help='directory for downloads, sources and logs (reused)')
    parser.add_argument('--jobs', type=int, default=os.cpu_count() or 2)
    args = parser.parse_args(argv)
    if sys.platform != 'darwin':
        parser.error('Build the Mac tools on macOS')
    output, work = args.output.resolve(), args.work.resolve()
    if output.exists():
        parser.error('Output must be a new directory')
    if output.is_relative_to(work) or work.is_relative_to(output):
        parser.error('Output and work directories must be separate')
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True)
    print(json.dumps({'output': str(output), 'chip': machine(), 'versions': build(output, work, args.jobs)}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print('macos_tools: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
