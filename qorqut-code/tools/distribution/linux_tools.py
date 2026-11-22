#!/usr/bin/env python3
"""Build the programs the Linux download carries, so a Linux desktop needs nothing installed first.

Build tooling only; runs on Linux x86-64 with a C toolchain, or in a clean container with --docker. Output:

    OUT/python/     self-contained CPython 3.12 (python-build-standalone), unchanged
    OUT/native/     bin/git, bin/agent-browser, libexec/git-core/, share/git-core/, licenses/
    OUT/TOOLS.json  versions, sources and SHA-256 of everything that was fetched

The counterpart of macos_tools.py, with the same layout. Every source is pinned by SHA-256 (git is the pin
macos_tools.py uses). zlib, OpenSSL and curl are linked into git as static libraries, so a shipped program needs
only the C library every glibc distribution has; the result is refused if a program lists any other library, an
absolute library path, or needs a newer glibc than Ubuntu 24.04's.

Built on Ubuntu 24.04 the programs need glibc 2.38 (TOOLS.json records the exact floor): Ubuntu 24.04 and newer, Fedora 39
and newer, Debian 13. Debian 12 and Ubuntu 22.04 are older than that; python and agent-browser alone would run there.

    python3 tools/distribution/linux_tools.py --docker --output /abs/tools --work /abs/work
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request

import native_launch
from macos_tools import PYTHON_BUILD, PYTHON_VERSION, SOURCES as SHARED_SOURCES, run, sha256, unpack, unpack_python

BUILD_PATH = '/usr/bin:/bin'
DOCKER_IMAGE = 'ubuntu:24.04'
# What a clean Debian-family container needs on top of its base. Nothing else (no pkg-config, no development libraries)
# is installed, so no system library can leak into the programs.
BUILD_PACKAGES = ('build-essential', 'bison', 'perl', 'python3', 'ca-certificates')
GLIBC_CEILING = (2, 39)  # Ubuntu 24.04
# Libraries every glibc system has, as part of glibc itself. libpthread, libdl, librt and libutil are empty compatibility
# stubs since glibc 2.34.
SYSTEM_LIBRARIES = frozenset({'libc.so.6', 'libm.so.6', 'libpthread.so.0', 'libdl.so.2', 'librt.so.1', 'libutil.so.1',
                              'libresolv.so.2', 'ld-linux-x86-64.so.2'})
SOURCES = {'git': SHARED_SOURCES['git']}
SOURCES.update({
    'python-x86_64': {
        'version': PYTHON_VERSION + '+' + PYTHON_BUILD, 'license': 'PSF-2.0',
        'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/' + PYTHON_BUILD
               + '/cpython-' + PYTHON_VERSION + '+' + PYTHON_BUILD + '-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz',
        'sha256': '6a9e62dad033790aef722dbd370e0ce76ce17b73627ba3ae679f537049350b39'},
    'zlib': {
        'version': '1.3.2', 'license': 'Zlib', 'license_file': 'LICENSE',
        'url': 'https://github.com/madler/zlib/releases/download/v1.3.2/zlib-1.3.2.tar.gz',
        'sha256': 'bb329a0a2cd0274d05519d61c667c062e06990d72e125ee2dfa8de64f0119d16'},
    'openssl': {
        'version': '3.5.9', 'license': 'Apache-2.0', 'license_file': 'LICENSE.txt',
        'url': 'https://github.com/openssl/openssl/releases/download/openssl-3.5.9/openssl-3.5.9.tar.gz',
        'sha256': '603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a'},
    'curl': {
        'version': '8.23.0', 'license': 'curl', 'license_file': 'COPYING',
        'url': 'https://github.com/curl/curl/releases/download/curl-8_23_0/curl-8.23.0.tar.xz',
        'sha256': '8dadd7d2536a314f982ad05a651b713f426bf7d02cc2c76cffb9796f2218e6c5'},
})
LIBRARIES = ('zlib', 'openssl', 'curl')
# Where the system keeps its certificates; git looks there first.
CA_BUNDLE, CA_DIRECTORY = '/etc/ssl/certs/ca-certificates.crt', '/etc/ssl/certs'
# git parts Qorqbot and its agents never call: servers, mail and other version-control bridges.
GIT_UNUSED = ('bin/git-cvsserver', 'bin/git-shell', 'bin/scalar', 'libexec/git-core/git-cvsserver',
              'libexec/git-core/git-shell', 'libexec/git-core/scalar', 'libexec/git-core/git-daemon',
              'libexec/git-core/git-http-backend', 'libexec/git-core/git-imap-send',
              'libexec/git-core/git-instaweb', 'share/bash-completion', 'share/git-gui', 'share/gitk',
              'share/gitweb', 'share/locale', 'share/perl5', 'share/man')


def machine():
    name = platform.machine().lower()
    if name in ('x86_64', 'amd64'):
        return 'x86_64'
    raise RuntimeError('Unsupported Linux CPU: ' + name)


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


def fetch_agent_browser(downloads):
    """The agent-browser package, pinned by the npm integrity (SHA-512) native_launch pins."""
    target = downloads / ('agent-browser-' + native_launch.AGENT_BROWSER_VERSION + '.tgz')
    digest = lambda path: base64.b64encode(hashlib.sha512(path.read_bytes()).digest()).decode()
    if not target.is_file() or digest(target) != native_launch.AGENT_BROWSER_SHA512:
        partial = target.with_name(target.name + '.part')
        if native_launch._download(native_launch.AGENT_BROWSER_URL, partial, native_launch.AGENT_BROWSER_LIMIT) \
                != native_launch.AGENT_BROWSER_SHA512:
            partial.unlink()
            raise RuntimeError('Checksum mismatch for ' + native_launch.AGENT_BROWSER_URL)
        partial.replace(target)
    return target


def is_elf(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open('rb') as stream:
        return stream.read(4) == b'\x7fELF'


def elf_facts(binary):
    """(needed libraries, library search paths, newest glibc version it needs) of one program, read with readelf."""
    dynamic = subprocess.run(['/usr/bin/readelf', '-d', str(binary)], capture_output=True, text=True, check=True).stdout
    versions = subprocess.run(['/usr/bin/readelf', '-V', str(binary)], capture_output=True, text=True, check=True).stdout
    needed = re.findall(r'\(NEEDED\)\s+Shared library: \[(.+?)\]', dynamic)
    paths = [path for value in re.findall(r'\((?:RPATH|RUNPATH)\)\s+Library \w+path: \[(.+?)\]', dynamic) for path in value.split(':')]
    glibc = max((tuple(map(int, found.split('.'))) for found in re.findall(r'GLIBC_(\d+(?:\.\d+)+)', versions)), default=(0,))
    return needed, paths, glibc


def require_self_contained(binary, allowed=SYSTEM_LIBRARIES):
    """A shipped program may need only the C library, search for libraries only beside itself, and not need a newer
    glibc than Ubuntu 24.04's. Returns the newest glibc version it needs."""
    needed, paths, glibc = elf_facts(binary)
    for library in needed:
        if library not in allowed:
            raise RuntimeError(f'{binary.name} links against {library}, which a clean Linux desktop does not have')
    for path in paths:
        if not path.startswith('$ORIGIN'):
            raise RuntimeError(f'{binary.name} searches for libraries in {path}, which only exists on the build machine')
    if glibc > GLIBC_CEILING:
        raise RuntimeError(f'{binary.name} needs glibc {".".join(map(str, glibc))}; Ubuntu 24.04 has {".".join(map(str, GLIBC_CEILING))}')
    return glibc


def build(output, work, jobs):
    downloads, sources, logs = work / 'downloads', work / 'src', work / 'logs'
    for folder in (downloads, logs):
        folder.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(sources, ignore_errors=True)
    static, stage = work / 'static', work / 'git-stage'
    for folder in (static, stage):
        shutil.rmtree(folder, ignore_errors=True)
    env = {'PATH': BUILD_PATH, 'HOME': str(work), 'LANG': 'C', 'CFLAGS': '-O2', 'CPPFLAGS': '-I' + str(static / 'include'),
           'LDFLAGS': '-L' + str(static / 'lib') + ' -Wl,--as-needed'}
    for name in LIBRARIES + ('git',):
        unpack(fetch(name, downloads), sources / name)
    make, install = ['/usr/bin/make', '-j' + str(jobs)], ['/usr/bin/make', 'install']

    def step(name, *commands):
        for command in commands:
            run(command, sources / name, env, logs / (name + '.log'))

    # The libraries are static and live only in the work folder; nothing from them is shipped except what is linked in.
    step('zlib', ['./configure', '--prefix=' + str(static), '--static'], make, install)
    step('openssl', ['./Configure', 'linux-x86_64', '--prefix=' + str(static), '--libdir=lib', '--openssldir=/etc/ssl',
                     'no-shared', 'no-tests', 'no-docs', 'no-engine', 'no-dso', 'no-comp', 'no-ssl3', 'no-afalgeng'],
         make, ['/usr/bin/make', 'install_sw'])
    # https for git. Certificates come from the computer's own store (see native_launch.system_ca_bundle).
    step('curl', ['./configure', '--prefix=' + str(static), '--disable-shared', '--enable-static', '--with-openssl=' + str(static),
                  '--with-zlib=' + str(static), '--with-ca-bundle=' + CA_BUNDLE, '--with-ca-path=' + CA_DIRECTORY,
                  '--without-libpsl', '--without-brotli', '--without-zstd', '--without-libidn2', '--without-nghttp2',
                  '--without-libssh2', '--without-librtmp', '--disable-ldap', '--disable-ldaps', '--disable-rtsp',
                  '--disable-dict', '--disable-telnet', '--disable-tftp', '--disable-pop3', '--disable-imap',
                  '--disable-smb', '--disable-smtp', '--disable-gopher', '--disable-mqtt', '--disable-manual',
                  '--disable-docs'], make, install)

    # git; RUNTIME_PREFIX lets it find its helpers wherever the folder sits. Only the https helpers link libcurl.
    curl_config = static / 'bin/curl-config'
    static_libs = subprocess.run([str(curl_config), '--static-libs'], capture_output=True, text=True, check=True).stdout.strip()
    git_options = ['NO_RUST=1', 'NO_EXPAT=1', 'NO_GETTEXT=1', 'NO_PERL=1', 'NO_PYTHON=1', 'NO_TCLTK=1', 'NO_OPENSSL=1',
                   'NO_INSTALL_HARDLINKS=1', 'INSTALL_SYMLINKS=1', 'RUNTIME_PREFIX=YesPlease', 'prefix=/usr/local',
                   'CURL_CONFIG=' + str(curl_config), 'CURL_CFLAGS=-I' + str(static / 'include'), 'CURL_LDFLAGS=' + static_libs,
                   'CFLAGS=' + env['CFLAGS'] + ' ' + env['CPPFLAGS'], 'LDFLAGS=' + env['LDFLAGS']]
    step('git', make + git_options, ['/usr/bin/make', *git_options, 'DESTDIR=' + str(stage), 'install'])

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
    for name in ('git', 'zlib', 'openssl', 'curl'):
        shutil.copy2(sources / name / SOURCES[name]['license_file'], licenses / (name + '.txt'))
    for path in sorted(native.rglob('*')):
        if is_elf(path):
            subprocess.run(['/usr/bin/strip', '--strip-unneeded', str(path)], check=True)
    browser = ship_agent_browser(downloads, native)

    unpack_python(fetch('python-x86_64', downloads), output / 'python')
    shutil.copy2(output / 'python/lib/python3.12/LICENSE.txt', licenses / 'python.txt')

    glibc = max(require_self_contained(path) for path in sorted(native.rglob('*')) + [output / 'python/bin/python3.12']
                if is_elf(path))
    clean = {'PATH': '/usr/bin:/bin', 'HOME': str(work), 'LANG': 'C'}
    versions = {}
    for label, argv in (('git', [native / 'bin/git', '--version']),
                        ('python', [output / 'python/bin/python3.12', '-I', '-c', 'import sys, ssl, sqlite3; print(sys.version.split()[0])'])):
        versions[label] = subprocess.run([str(a) for a in argv], env=clean, capture_output=True, text=True, check=True).stdout.strip()
    if SOURCES['git']['version'] not in versions['git'] or versions['python'] != PYTHON_VERSION:
        raise RuntimeError('A built program reports an unexpected version: ' + json.dumps(versions))
    used = {name: {key: SOURCES[name][key] for key in ('version', 'license', 'url', 'sha256')}
            for name in ('zlib', 'openssl', 'curl', 'git', 'python-x86_64')}
    used['agent-browser'] = browser
    (output / 'TOOLS.json').write_text(json.dumps({
        'format': 1, 'chip': machine(), 'glibc_required': '.'.join(map(str, glibc)), 'reported_versions': versions,
        'sources': used}, indent=2) + '\n')
    return versions


def ship_agent_browser(downloads, native):
    """The agent-browser program for Linux x86-64, from the pinned npm package; returns its TOOLS.json record."""
    archive = fetch_agent_browser(downloads)
    native_launch.extract_agent_browser(archive, native_launch.agent_browser_binary_name('Linux', 'x86_64'), native / 'bin/agent-browser')
    with tarfile.open(archive, 'r:gz') as package:
        for member in ('package/LICENSE', 'package/LICENSE.md', 'package/LICENSE.txt'):
            if member in package.getnames():
                (native / 'licenses/agent-browser.txt').write_bytes(package.extractfile(member).read())
                break
    check = subprocess.run([native / 'bin/agent-browser', '--version'], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    if check.returncode != 0 or native_launch.AGENT_BROWSER_VERSION not in check.stdout:
        raise RuntimeError('The agent-browser program does not run on this machine')
    return {'version': native_launch.AGENT_BROWSER_VERSION, 'license': 'Apache-2.0', 'url': native_launch.AGENT_BROWSER_URL,
            'sha512': native_launch.AGENT_BROWSER_SHA512}


def in_container(args, output, work):
    """Run this script in a clean container that installs only the build tools, as the calling user."""
    docker = shutil.which('docker')
    if not docker:
        raise RuntimeError('--docker needs docker on PATH')
    for folder in (work, output.parent):
        folder.mkdir(parents=True, exist_ok=True)
    mirror = ''
    if args.apt_mirror:
        # Ubuntu images only; apt still verifies every package against Ubuntu's signing keys.
        mirror = (f'mirror={shlex.quote(args.apt_mirror.rstrip("/"))}; sed -i "s|http://archive.ubuntu.com/ubuntu/|$mirror/|;'
                  's|http://security.ubuntu.com/ubuntu/|$mirror/|" /etc/apt/sources.list.d/ubuntu.sources && ')
    inner = (mirror + 'apt-get update -qq && apt-get install -y -qq --no-install-recommends ' + ' '.join(BUILD_PACKAGES)
             + f' >/dev/null && exec setpriv --reuid={os.getuid()} --regid={os.getgid()} --clear-groups'
             + f' python3 -B /tools/linux_tools.py --output {output} --work {work} --jobs {args.jobs}')
    subprocess.run([docker, 'run', '--rm', '-v', str(Path(__file__).resolve().parent) + ':/tools:ro', '-v', f'{work}:{work}',
                    '-v', f'{output.parent}:{output.parent}', args.image, 'sh', '-c', inner], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--output', type=Path, required=True, help='new directory for the built tools')
    parser.add_argument('--work', type=Path, required=True, help='directory for downloads, sources and logs (reused)')
    parser.add_argument('--jobs', type=int, default=os.cpu_count() or 2)
    parser.add_argument('--docker', action='store_true', help='build inside a clean container that installs the build tools')
    parser.add_argument('--image', default=DOCKER_IMAGE, help='container image for --docker; its python3 must be 3.12 (default: %(default)s)')
    parser.add_argument('--apt-mirror', help='--docker with an Ubuntu image: use this Ubuntu mirror, e.g. http://azure.archive.ubuntu.com/ubuntu')
    args = parser.parse_args(argv)
    if sys.platform != 'linux' or platform.machine().lower() not in ('x86_64', 'amd64'):
        parser.error('Build the Linux tools on Linux x86-64')
    output, work = args.output.resolve(), args.work.resolve()
    if output.exists():
        parser.error('Output must be a new directory')
    if output.is_relative_to(work) or work.is_relative_to(output):
        parser.error('Output and work directories must be separate')
    if args.docker:
        in_container(args, output, work)
        print(json.dumps({'output': str(output), 'tools': json.loads((output / 'TOOLS.json').read_text())}, indent=2))
        return
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True)
    print(json.dumps({'output': str(output), 'chip': machine(), 'versions': build(output, work, args.jobs)}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print('linux_tools: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
