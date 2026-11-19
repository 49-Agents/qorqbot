#!/usr/bin/env python3
"""Assemble a native folder download using system tools and standard Python wheels.

Build tooling only. Input is a prepare-local output of the filtered export. Never point this at an initialized installation.
No venv, configuration, HOME, build log or instance is copied from either input.

With --bundled-tools (the output of macos_tools.py, built on the same Mac chip) the package is self-contained:
it ships a ready-made Python environment in runtime/venv and git and the agents' browser tool in
runtime/native, so nothing is installed or built on the user's Mac.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
from email.parser import BytesParser
import re
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


DEV = {'tests', 'test', '__tests__', '__pycache__', '.github', '.git', '.gitlab', '.pytest_cache'}
TARGETS = {'ubuntu24-x86_64': ('Ubuntu 24.04 x86_64', None), 'macos14-arm64': ('macOS 14+ Apple silicon', 'macosx_14_0_arm64'),
           'macos14-x86_64': ('macOS 14+ Intel', 'macosx_14_0_x86_64')}
# Parts of the shipped Python that Qorqbot never loads: its test suite, the Tk user interface, the IDLE editor,
# C headers and the shared library (the python program has the interpreter linked in).
PYTHON_UNUSED = ('include', 'share', 'lib/pkgconfig', 'lib/libpython3.12.dylib', 'lib/python3.12/test',
                 'lib/python3.12/idlelib', 'lib/python3.12/tkinter', 'lib/python3.12/turtledemo',
                 'lib/python3.12/turtle.py', 'lib/python3.12/lib2to3', 'lib/python3.12/ensurepip',
                 'lib/python3.12/pydoc_data', 'lib/python3.12/config-3.12-darwin')
PYTHON_UNUSED_PATTERNS = ('lib/tcl*', 'lib/tk*', 'lib/itcl*', 'lib/thread*', 'lib/libtcl*', 'lib/libtk*',
                          'lib/python3.12/lib-dynload/_tkinter*', 'bin/idle*', 'bin/2to3*', 'bin/pydoc*')
RELOCATABLE_SCRIPT = b'#!/bin/sh\n\'\'\'exec\' "$(dirname -- "$0")/python3.12" "$0" "$@"\n\' \'\'\'\n'

# The ./qorqbot command of every package.
LAUNCHER = """#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
python=${QORQUT_PYTHON:-}
if [ -z "$python" ]; then
    # A self-contained package runs on the Python it ships; other packages use this computer's Python 3.12.
    for own in "$root/runtime" "$root/.previous-runtime"; do
        if [ -d "$own/native" ] && [ -x "$own/venv/bin/python3.12" ]; then python="$own/venv/bin/python3.12"; break; fi
    done
fi
python=${python:-python3.12}
if ! command -v "$python" >/dev/null 2>&1; then
    echo "Qorqbot requires Python 3.12 with venv support." >&2
    if [ "$(uname -s)" = Darwin ]; then
        echo "Install it with: brew install python@3.12" >&2
    else
        echo "Install it with: sudo apt install python3.12 python3.12-venv" >&2
    fi
    exit 1
fi
unset PYTHONHOME PYTHONPATH
if [ "${1-}" = update ]; then
    shift
    updater="$root/runtime"
    if [ ! -d "$updater" ]; then updater="$root/.previous-runtime"; fi
    exec "$python" -I -B "$updater/update.py" "$@"
fi
if [ "${1-}" = extension-install ]; then
    shift
    exec "$python" -I -B "$root/runtime/extension_install.py" "$@"
fi
exec "$python" -I -B "$root/runtime/launch.py" "$@"
"""


def remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def relocate_scripts(venv):
    """pip writes the build machine's Python path into each command in venv/bin. Replace it with a start line
    that runs the python3.12 beside the command, so the commands work wherever the folder is unpacked."""
    marker = str(venv / 'bin/python').encode()
    changed = []
    for script in sorted((venv / 'bin').iterdir()):
        if script.is_symlink() or not script.is_file():
            continue
        with script.open('rb') as stream:
            head = stream.read(4096)
        if not head.startswith(b'#!') or marker not in head:
            continue
        data = script.read_bytes()
        first, _, rest = data.partition(b'\n')
        if first.rstrip() == b'#!/bin/sh':
            # pip's own form for a long path: a shell line that starts Python, closed by a line holding ' \'\'\'.
            _, closing, rest = rest.partition(b"' '''\n")
            if not closing:
                raise ValueError('Unrecognized command start in ' + script.name)
        elif marker not in first:
            raise ValueError('Unrecognized command start in ' + script.name)
        script.write_bytes(RELOCATABLE_SCRIPT + rest)
        changed.append(script.name)
    return changed


def ready_made_environment(tools, runtime, platform_tag):
    """Install the package's libraries into the shipped Python at build time and return what was done."""
    venv, package_root = runtime / 'venv', runtime.parent
    shutil.copytree(tools / 'python', venv, symlinks=True)
    for name in PYTHON_UNUSED:
        remove(venv / name)
    for pattern in PYTHON_UNUSED_PATTERNS:
        for path in venv.glob(pattern):
            remove(path)
    for cache in list(venv.rglob('__pycache__')):
        shutil.rmtree(cache)
    python = venv / 'bin/python3.12'
    with tempfile.TemporaryDirectory(prefix='qorqbot-build-home-') as home:
        env = {'PATH': '/usr/bin:/bin', 'HOME': home, 'LANG': 'C.UTF-8', 'PIP_CONFIG_FILE': os.devnull, 'PIP_NO_INDEX': '1',
               'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_CACHE_DIR': str(Path(home) / 'pip-cache'), 'PYTHONDONTWRITEBYTECODE': '1'}
        # The same command a Linux package runs on the user's computer, run here once for everyone.
        subprocess.run([python, '-I', '-B', '-m', 'pip', 'install', '--quiet', '--no-index', '--no-compile',
                        '--find-links', runtime / 'wheelhouse', '--find-links', runtime / 'wheels',
                        '--require-hashes', '-r', runtime / 'requirements.lock'], env=env, cwd=package_root, check=True)
        subprocess.run([python, '-I', '-B', '-m', 'pip', 'check'], env=env, cwd=package_root, check=True)
        scripts = relocate_scripts(venv)
        # Compiled files that stay valid whatever the unpacked files' dates are; the launcher never writes its own.
        subprocess.run([python, '-I', '-m', 'compileall', '-q', '-j', '0', '--invalidation-mode', 'unchecked-hash',
                        venv / 'lib/python3.12'], env={key: value for key, value in env.items() if key != 'PYTHONDONTWRITEBYTECODE'},
                       cwd=package_root, check=True)
        machine = subprocess.run([python, '-I', '-B', '-c', 'import platform, cryptography, pydantic_core, psutil, yaml; print(platform.machine())'],
                                 env=env, cwd=package_root, check=True, capture_output=True, text=True).stdout.strip()
    expected = 'arm64' if platform_tag.endswith('arm64') else 'x86_64'
    if machine != expected:
        raise ValueError(f'The bundled tools are built for {machine}; this package targets {expected}')
    return {'relocated_commands': scripts}


def bundle_tools(tools, runtime, target):
    """Copy git and the agents' browser tool into runtime/native."""
    import native_launch
    native = runtime / 'native'
    shutil.copytree(tools / 'native', native, symlinks=True)
    shutil.copy2(tools / 'TOOLS.json', native / 'TOOLS.json')
    machine = 'arm64' if target.endswith('arm64') else 'x86_64'
    name = native_launch.agent_browser_binary_name('Darwin', machine)
    with tempfile.TemporaryDirectory(prefix='qorqbot-agent-browser-') as work:
        archive = Path(work) / 'package.tgz'
        if native_launch._download(native_launch.AGENT_BROWSER_URL, archive, native_launch.AGENT_BROWSER_LIMIT) != native_launch.AGENT_BROWSER_SHA512:
            raise ValueError('The agent-browser download does not match its pinned checksum')
        native_launch.extract_agent_browser(archive, name, native / 'bin/agent-browser')
        with tarfile.open(archive, 'r:gz') as package:
            for member in ('package/LICENSE', 'package/LICENSE.md', 'package/LICENSE.txt'):
                if member in package.getnames():
                    (native / 'licenses/agent-browser.txt').write_bytes(package.extractfile(member).read())
                    break
    check = subprocess.run([native / 'bin/agent-browser', '--version'], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    if check.returncode != 0 or native_launch.AGENT_BROWSER_VERSION not in check.stdout:
        raise ValueError('The bundled agent-browser does not run on this machine')
    return json.loads((tools / 'TOOLS.json').read_text())


def strip_wheel(source, target):
    """Strip third-party test/development trees and regenerate the wheel RECORD.

    The original wheel hash stays in the private build report. License files and
    metadata are retained, and pip verifies the new RECORD at installation.
    """
    removed, files = [], {}
    with zipfile.ZipFile(source) as wheel:
        for item in wheel.infolist():
            path = Path(item.filename)
            if path.is_absolute() or '..' in path.parts or item.filename in files:
                raise ValueError('Unsafe or duplicate wheel member')
            if item.is_dir():
                continue
            if (set(path.parts) & DEV or path.suffix in {'.pyc', '.pyo'}
                    or path.name in {'RECORD', 'RECORD.jws', 'RECORD.p7s'}):
                removed.append(item.filename)
                continue
            files[item.filename] = (item, wheel.read(item))
        records = [name for name in wheel.namelist() if name.endswith('.dist-info/RECORD')]
        if len(records) != 1:
            raise ValueError('Wheel needs exactly one RECORD')
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as wheel:
        for name, (item, data) in files.items():
            wheel.writestr(item, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
            writer.writerow((name, 'sha256=' + digest, len(data)))
        writer.writerow((records[0], '', ''))
        wheel.writestr(records[0], output.getvalue())
    return {'file': source.name, 'original_sha256': sha(source), 'runtime_sha256': sha(target), 'removed': removed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--target', choices=tuple(TARGETS), default='ubuntu24-x86_64')
    parser.add_argument('--bundled-tools', type=Path, help='macos_tools.py output for this Mac chip; makes the package self-contained')
    args = parser.parse_args(argv)
    platform_name, platform_tag = TARGETS[args.target]
    if args.bundled_tools and (platform_tag is None or sys.platform != 'darwin'):
        raise ValueError('A self-contained package is built for a Mac target, on a Mac')
    if not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z._-]{0,79}', args.version):
        raise ValueError('Invalid package version')
    prepared, output, report = (p.resolve() for p in (args.prepared, args.output, args.report))
    if output.exists() or report.exists():
        raise ValueError('Output and report must be new paths')
    for a in (prepared,):
        if output.is_relative_to(a) or a.is_relative_to(output):
            raise ValueError('Build inputs and output must be disjoint')
    if report.is_relative_to(output):
        raise ValueError('The private report must be outside the package')
    import runtime_source
    source = prepared / 'src'
    runtime_source.load_snapshot().validate_export(source)
    receipt = json.loads((prepared / 'PREPARATION-RECEIPT.json').read_text())
    if receipt.get('state') != 'prepared-not-configured' or set(receipt['environments']) != {'venv'}:
        raise ValueError('Expected a core-only prepared environment')
    # One authoritative audit, including the prepared console entry points.
    audit = runtime_source.audit_prepared(prepared)
    # Qualification is handled by the release gate; this assembler never publishes.
    blockers = audit['findings']
    if blockers:
        raise ValueError('Runtime source audit is not clean: ' + json.dumps(blockers[:5]))
    runtime = output / 'qorqbot/runtime'
    runtime.mkdir(parents=True)
    shutil.copytree(source, runtime / 'src', symlinks=True)
    shutil.copytree(prepared / 'wheels', runtime / 'wheels')
    shutil.copy2(prepared / 'PREPARATION-RECEIPT.json', runtime / 'PREPARATION-RECEIPT.json')
    for row in receipt['wheels']:
        if sha(runtime / row['file']) != row['sha256']:
            raise ValueError('First-party wheel checksum mismatch: ' + row['file'])
    python = prepared / 'venv/bin/python'
    packages = receipt['environments']['venv']['packages']
    normalize = lambda value: value.lower().replace('_', '-').replace('.', '-')
    own = {normalize(row['name']) for row in receipt['wheels']}
    requirements = sorted(f'{name}=={version}' for name, version in packages.items()
                          if normalize(name) not in own and normalize(name) not in {'pytest', 'pip'})
    download_options = []
    if platform_tag:
        download_options = ['--platform', platform_tag, '--python-version', '3.12', '--implementation', 'cp', '--abi', 'cp312']
        if not any(normalize(name) == 'psutil' for name in packages):
            requirements.append('psutil>=7.0,<8')
    downloads = report.parent / (report.stem + '-downloads')
    downloads.mkdir(parents=True, exist_ok=False)
    subprocess.run([python, '-I', '-m', 'pip', 'download', '--only-binary=:all:', '--no-deps', '--dest', downloads,
                    *download_options, *requirements], check=True)
    (runtime / 'wheelhouse').mkdir()
    wheel_records = [strip_wheel(w, runtime / 'wheelhouse' / w.name) for w in sorted(downloads.glob('*.whl'))]
    shutil.copy2(Path(__file__).with_name('native_launch.py'), runtime / 'launch.py')
    shutil.copy2(Path(__file__).with_name('native_update.py'), runtime / 'update.py')
    shutil.copy2(Path(__file__).with_name('native_extension.py'), runtime / 'extension_install.py')
    launcher = output / 'qorqbot/qorqbot'
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)
    guide = 'MACOS.md' if platform_tag else 'README.md'
    shutil.copy2(Path(__file__).resolve().parents[2] / ('docs/distribution/' + guide), runtime / 'README.txt')
    (output / 'qorqbot/README.txt').symlink_to('runtime/README.txt')
    for name in ('LICENSE', 'LICENSE-SCOPE.md', 'LICENSES/Apache-2.0.txt', 'RELEASE-LICENSES.json'):
        target = output / 'qorqbot' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # These links always follow the active runtime, including after an
        # update or recovery, so notices cannot silently lag behind its code.
        target.symlink_to(os.path.relpath(runtime / 'src/qorqut-code' / name, target.parent))
    locked = []
    for wheel_path in sorted((runtime / 'wheels').glob('*.whl')) + sorted((runtime / 'wheelhouse').glob('*.whl')):
        with zipfile.ZipFile(wheel_path) as wheel:
            paths = [n for n in wheel.namelist() if n.endswith('.dist-info/METADATA')]
            if len(paths) != 1:
                raise ValueError('Wheel needs one METADATA: ' + wheel_path.name)
            metadata = BytesParser().parsebytes(wheel.read(paths[0]))
        # First-party initialization verifies pip's direct-URL provenance against
        # the receipt wheel. A named find-links requirement does not produce it.
        requirement = ('./runtime/wheels/' + wheel_path.name if wheel_path.parent.name == 'wheels'
                       else f"{metadata['Name']}=={metadata['Version']}")
        locked.append(f"{requirement} --hash=sha256:{sha(wheel_path)}")
    (runtime / 'requirements.lock').write_text('\n'.join(sorted(locked)) + '\n')
    bundled, shipped_tools, environment_record = {}, None, None
    if args.bundled_tools:
        import native_launch
        tools = args.bundled_tools.resolve()
        shipped_tools = bundle_tools(tools, runtime, args.target)
        environment_record = ready_made_environment(tools, runtime, platform_tag)
    dependencies = {
        'platform': platform_name,
        'required': {'python': '3.12.x with venv/pip support', 'git': '2.43 or newer'},
        'optional': {'agent_client': 'Claude Code and Codex, downloaded by Qorqbot from its Settings',
                     'browser': 'user-installed compatible Chrome and agent-browser',
                     'node': 'as required by the selected external client/browser tool',
                     'desktop': 'Linux only: TigerVNC, Openbox, X11, D-Bus and keyring if a desktop is configured'},
        'python_libraries': 'requirements.lock', 'external_tools_bundled': False,
    }
    if args.bundled_tools:
        dependencies.update({
            'required': {}, 'external_tools_bundled': True,
            'bundled': {'python': shipped_tools['reported_versions']['python'],
                        'git': shipped_tools['reported_versions']['git'], 'agent-browser': native_launch.AGENT_BROWSER_VERSION,
                        'details': 'native/TOOLS.json, native/licenses'},
            'optional': {'agent_client': 'Claude Code and Codex, downloaded by Qorqbot from its Settings', 'browser': 'user-installed Google Chrome'}})
    (runtime / 'DEPENDENCIES.json').write_text(json.dumps(dependencies, indent=2) + '\n')
    if args.bundled_tools:
        bundled = {name: native_launch.tree_digest(runtime / name) for name in native_launch.BUNDLED_TREES}
    files = {}
    for path in sorted(runtime.rglob('*')):
        # The shipped environment and tools are covered by one digest each (PAYLOAD.json "bundled"), not listed file by file.
        if bundled and path.relative_to(runtime).parts[0] in bundled:
            continue
        if path.is_file():
            if not path.resolve().is_relative_to(runtime):
                raise ValueError('Runtime symlink escapes package: ' + str(path))
            files[str(path.relative_to(runtime))] = sha(path)
    schema_code = ('import json;from qorqut.composition.assembly import host_assembly;'
                   'from qorqut.service.changes import ChangeFeed;'
                   't=host_assembly(ChangeFeed()).target;print(json.dumps([t.name,t.version,t.digest]))')
    schema_target = json.loads(subprocess.check_output([python, '-I', '-c', schema_code], text=True))
    (runtime / 'PAYLOAD.json').write_text(json.dumps({'format': 1, 'version': args.version,
         'platform': args.target, 'schema_target': schema_target, 'files': files,
         **({'bundled': bundled} if bundled else {})}, indent=2) + '\n')
    archive = output / ('qorqbot-' + args.version + '-' + args.target + '.tar.gz')
    def public_header(member):
        member.uid = member.gid = 0
        member.uname = member.gname = ''
        member.mtime = 0
        member.pax_headers = {}
        return member
    with tarfile.open(archive, 'w:gz', compresslevel=6) as tar:
        tar.add(output / 'qorqbot', arcname='qorqbot', filter=public_header)
    record = {'format': 1, 'version': args.version, 'platform': args.target, 'qualified': False,
              'external_tools_bundled': bool(args.bundled_tools), 'archive': str(archive),
              'sha256': sha(archive), 'third_party_wheels': wheel_records, 'source_audit': audit}
    if args.bundled_tools:
        record.update({'bundled_tools': shipped_tools, 'bundled_digests': bundled, 'ready_made_environment': environment_record})
    report.write_text(json.dumps(record, indent=2) + '\n')
    print(archive)


if __name__ == '__main__':
    main()
