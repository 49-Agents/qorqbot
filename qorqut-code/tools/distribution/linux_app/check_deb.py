#!/usr/bin/env python3
"""Check a built Qorqbot .deb, and optionally the installed app.

Package checks need only dpkg-deb. Installed checks start the real app under
a virtual display with Chromium's sandbox on, so they belong on a clean runner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT_LIMIT = 50  # the Unix socket path inside the install folder must stay short
SANDBOX_MODE = '-rwsr-xr-x'
REQUIRED_DEPENDS = ('python3.12', 'python3.12-venv', 'git', 'tmux')
PAYLOAD = './opt/Qorqbot/resources/qorqbot-payload/'
REQUIRED_PATHS = (
    './opt/Qorqbot/Qorqbot',
    './opt/Qorqbot/chrome-sandbox',
    './opt/Qorqbot/resources/app.asar',
    PAYLOAD + 'ARCHIVE_NAME',
    PAYLOAD + 'ARCHIVE_SHA256',
    PAYLOAD + 'VERSION',
    './opt/Qorqbot/resources/LICENSE.electron.txt',
    './opt/Qorqbot/resources/LICENSES.chromium.html',
    './opt/Qorqbot/resources/THIRD-PARTY-NOTICES.md',
    './usr/bin/qorqbot-desktop',
    './usr/share/applications/qorqbot.desktop',
    './usr/share/icons/hicolor/scalable/apps/qorqbot.svg',
)
LAUNCHER_TARGET = '/opt/Qorqbot/Qorqbot'
NOT_TESTED = [
    'a real desktop session with a person using it',
    'Wayland',
    'authenticated agent clients',
    'other distributions and Ubuntu versions',
]
LISTING_LINE = re.compile(r'^(\S{10})\s+(\S+)\s+(\d+)\s+\S+\s+\S+\s+(.*)$')


class CheckError(Exception):
    pass


class Entry:
    def __init__(self, mode: str, owner: str, name: str, target: str = ''):
        self.mode, self.owner, self.name, self.target = mode, owner, name, target

    @property
    def is_symlink(self) -> bool:
        return self.mode.startswith('l')


def parse_listing_line(line: str) -> Entry:
    """Parse one line of `dpkg-deb -c`."""
    match = LISTING_LINE.match(line.rstrip('\n'))
    if not match:
        raise CheckError(f'Cannot read package listing line: {line!r}')
    mode, owner, _size, rest = match.groups()
    target = ''
    if mode.startswith('l') and ' -> ' in rest:
        rest, target = rest.split(' -> ', 1)
    elif mode.startswith('h') and ' link to ' in rest:
        rest, target = rest.split(' link to ', 1)
    return Entry(mode, owner, rest, target)


def check_root_length(work: Path) -> None:
    root = work / 'e' / 'Qorqbot' / 'qorqbot'
    if len(str(root)) > ROOT_LIMIT:
        raise CheckError(
            f'The work folder is too long: {root} is {len(str(root))} characters, '
            f'the limit is {ROOT_LIMIT}. Use a shorter --work path.')


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def run_dpkg(*args: str) -> str:
    try:
        done = subprocess.run(['dpkg-deb', *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise CheckError('dpkg-deb is not installed') from None
    if done.returncode != 0:
        raise CheckError(f'dpkg-deb {" ".join(args)} failed: {done.stderr.strip()}')
    return done.stdout


# Part A: package checks

def check_control(deb: Path) -> str:
    def field(name: str) -> str:
        return run_dpkg('-f', str(deb), name).strip()
    if field('Package') != 'qorqbot':
        raise CheckError(f'Package field is {field("Package")!r}, expected qorqbot')
    if field('Architecture') != 'amd64':
        raise CheckError(f'Architecture is {field("Architecture")!r}, expected amd64')
    version = field('Version')
    if not version:
        raise CheckError('Version field is empty')
    depends = {part.strip().split(' ')[0].split('|')[0] for part in field('Depends').split(',')}
    missing = [name for name in REQUIRED_DEPENDS if name not in depends]
    if missing:
        raise CheckError(f'Depends is missing: {", ".join(missing)}')
    return version


def check_listing(deb: Path) -> list[Entry]:
    entries = [parse_listing_line(line) for line in run_dpkg('-c', str(deb)).splitlines() if line.strip()]
    for entry in entries:
        if entry.owner != 'root/root':
            raise CheckError(f'{entry.name} is owned by {entry.owner}, expected root/root')
        if not entry.is_symlink and (entry.mode[5] == 'w' or entry.mode[8] == 'w'):
            raise CheckError(f'{entry.name} is group- or world-writable ({entry.mode})')
        special = entry.mode[3] in 'sS' or entry.mode[6] in 'sS'
        if special and entry.name != './opt/Qorqbot/chrome-sandbox':
            raise CheckError(f'{entry.name} has a setuid or setgid bit ({entry.mode})')
    by_name = {entry.name.rstrip('/'): entry for entry in entries}
    for path in REQUIRED_PATHS:
        if path not in by_name:
            raise CheckError(f'The package is missing {path[1:]}')
    sandbox = by_name['./opt/Qorqbot/chrome-sandbox']
    if sandbox.mode != SANDBOX_MODE:
        raise CheckError(f'chrome-sandbox has mode {sandbox.mode}, expected {SANDBOX_MODE} (4755)')
    launcher = by_name['./usr/bin/qorqbot-desktop']
    if not launcher.is_symlink or launcher.target != LAUNCHER_TARGET:
        raise CheckError(f'/usr/bin/qorqbot-desktop must be a symlink to {LAUNCHER_TARGET}')
    return entries


def check_control_area(deb: Path, scratch: Path) -> None:
    control = scratch / 'control'
    run_dpkg('-e', str(deb), str(control))
    extra = sorted(set(os.listdir(control)) - {'control', 'md5sums'})
    if extra:
        raise CheckError(f'The package has maintainer scripts or extra control files: {", ".join(extra)}')


def read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as error:
        raise CheckError(f'Cannot read {path.name}: {error}') from None


def check_payload(tree: Path, archive: Path) -> str:
    payload = tree / PAYLOAD[2:]
    name = read_text(payload / 'ARCHIVE_NAME')
    packaged = payload / name
    if not packaged.is_file():
        raise CheckError(f'The payload archive {name} is not in the package')
    expected = sha256_of(archive)
    if sha256_of(packaged) != expected:
        raise CheckError('The packaged payload archive differs from --archive')
    if read_text(payload / 'ARCHIVE_SHA256') != expected:
        raise CheckError('ARCHIVE_SHA256 does not match the archive')
    match = re.fullmatch(r'qorqbot-(.+)-ubuntu24-x86_64\.tar\.gz', name)
    if not match:
        raise CheckError(f'Unexpected payload archive name: {name}')
    version = read_text(payload / 'VERSION')
    if version != match.group(1):
        raise CheckError(f'VERSION is {version!r} but the archive name says {match.group(1)!r}')
    if name != archive.name:
        raise CheckError(f'ARCHIVE_NAME is {name!r} but --archive is {archive.name!r}')
    return version


def check_desktop_file(tree: Path, entries: list[Entry]) -> None:
    desktop = tree / 'usr/share/applications/qorqbot.desktop'
    exec_line = next((line for line in desktop.read_text().splitlines() if line.startswith('Exec=')), '')
    words = exec_line[5:].split()
    if not words:
        raise CheckError('qorqbot.desktop has no Exec line')
    target = words[0] if words[0].startswith('/') else '/usr/bin/' + words[0]
    if '.' + target not in {entry.name.rstrip('/') for entry in entries}:
        raise CheckError(f'The desktop file runs {words[0]}, which is not in the package')
    if shutil.which('desktop-file-validate'):
        done = subprocess.run(['desktop-file-validate', str(desktop)], capture_output=True, text=True)
        if done.returncode != 0:
            raise CheckError(f'desktop-file-validate failed: {(done.stdout + done.stderr).strip()}')


def check_package(deb: Path, archive: Path) -> tuple[str, list[str]]:
    """Run every package check; return the payload version and what passed."""
    checks = []
    check_control(deb)
    checks.append('package name, architecture, version and dependencies')
    entries = check_listing(deb)
    checks.append('all files owned by root, none writable by group or others, '
                  'chrome-sandbox is setuid 4755 and nothing else is setuid or setgid, '
                  'expected files present, launcher link correct')
    with tempfile.TemporaryDirectory(prefix='qd-pkg-') as scratch:
        check_control_area(deb, Path(scratch))
        checks.append('no maintainer scripts')
        tree = Path(scratch) / 'tree'
        run_dpkg('-x', str(deb), str(tree))
        version = check_payload(tree, archive)
        checks.append('payload archive is identical to the release archive; version and checksum files agree')
        check_desktop_file(tree, entries)
        checks.append('desktop file points at an existing launcher and validates')
    return version, checks


# Part B: installed checks

def http_status(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except Exception:
        return None


def base_env(work: Path, home: str, port: int) -> dict[str, str]:
    env = {
        'HOME': str(work / home),
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        'TMUX_TMPDIR': str(work / 't'),
        'QORQBOT_ACCOUNT_API': 'http://127.0.0.1:9',
        'QORQBOT_PORT': str(port),
        'LANG': 'C.UTF-8',
    }
    if os.environ.get('GITHUB_SHA'):
        env['GITHUB_SHA'] = os.environ['GITHUB_SHA']
    return env


def check_installed_files(app_root: Path, archive: Path, unsandboxed: bool) -> list[str]:
    if unsandboxed:
        return []
    sandbox = app_root / 'chrome-sandbox'
    info = sandbox.stat()
    if info.st_uid != 0 or (info.st_mode & 0o7777) != 0o4755:
        raise CheckError(f'{sandbox} must be owned by root with mode 4755, '
                         f'found uid {info.st_uid} mode {oct(info.st_mode & 0o7777)}')
    name = read_text(app_root / 'resources/qorqbot-payload/ARCHIVE_NAME')
    if sha256_of(app_root / 'resources/qorqbot-payload' / name) != sha256_of(archive):
        raise CheckError('The installed payload archive differs from --archive')
    return ['installed chrome-sandbox is root-owned with mode 4755',
            'installed payload archive is identical to the release archive']


def prepare_work(work: Path) -> None:
    check_root_length(work)
    if work.exists() and any(work.iterdir()):
        raise CheckError(f'The work folder {work} is not empty')
    for name in ('e', 'w', 't'):
        (work / name).mkdir(parents=True, exist_ok=True)


def run_engine_check(app_root: Path, work: Path, version: str, archive_sha: str, port: int) -> list[str]:
    script = Path(__file__).resolve().with_name('check_engine.cjs')
    result = work / 'engine-result.json'
    env = base_env(work, 'e', port)
    env['ELECTRON_RUN_AS_NODE'] = '1'
    try:
        done = subprocess.run(
            [str(app_root / 'Qorqbot'), str(script), str(app_root / 'resources'), version, archive_sha, str(result)],
            env=env, cwd=work, timeout=900)
    except subprocess.TimeoutExpired:
        raise CheckError('The engine check took longer than 15 minutes') from None
    if done.returncode != 0:
        raise CheckError(f'The engine check failed (exit {done.returncode}); see its output above')
    return json.loads(result.read_text())


def split_command_line(raw: bytes) -> list[str]:
    """Arguments of a /proc/<pid>/cmdline. Chromium child processes rewrite theirs
    as one string with spaces, so split on whitespace as well as NUL."""
    return raw.decode(errors='replace').replace('\0', ' ').split()


def read_proc(pid: int) -> tuple[int, list[str], str] | None:
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        ppid = int(stat[stat.rindex(')') + 2:].split()[1])
        args = split_command_line(Path(f'/proc/{pid}/cmdline').read_bytes())
        exe = os.readlink(f'/proc/{pid}/exe')
    except (OSError, ValueError):
        return None
    return ppid, args, exe


def process_table() -> dict[int, tuple[int, list[str], str]]:
    table = {}
    for name in os.listdir('/proc'):
        if name.isdigit():
            info = read_proc(int(name))
            if info:
                table[int(name)] = info
    return table


def find_main(table: dict, app_root: Path) -> int | None:
    exe = str((app_root / 'Qorqbot').resolve())
    for pid, (_ppid, args, path) in table.items():
        if path == exe and not any(a.startswith('--type=') for a in args):
            return pid
    return None


def descendants(table: dict, root: int) -> dict[int, list[str]]:
    found, queue = {}, [root]
    while queue:
        parent = queue.pop()
        for pid, (ppid, args, _exe) in table.items():
            if ppid == parent and pid not in found:
                found[pid] = args
                queue.append(pid)
    return found


def wait_for_dashboard(home: Path, deadline: float, process: subprocess.Popen) -> int:
    port_file = home / 'Qorqbot' / 'server-port'
    while time.time() < deadline:
        if process.poll() is not None:
            raise CheckError(f'The app exited early with code {process.returncode}; see window.log in the work folder')
        if port_file.exists():
            port = int(port_file.read_text().strip() or 0)
            if port and http_status(f'http://127.0.0.1:{port}/') == 200:
                return port
        time.sleep(2)
    raise CheckError('The dashboard did not answer within 240 seconds; see window.log in the work folder')


def check_process_tree(app_root: Path, unsandboxed: bool) -> int:
    table = process_table()
    main = find_main(table, app_root)
    if main is None:
        raise CheckError('The Electron main process is not running')
    tree = descendants(table, main)
    types = {a for args in tree.values() for a in args if a.startswith('--type=')}
    for needed in ('--type=renderer', '--type=zygote'):
        if needed not in types:
            raise CheckError(f'No {needed} process found under the app; the window did not start properly')
    if not unsandboxed:
        mains_args = table[main][1]
        if '--no-sandbox' in mains_args or any('--no-sandbox' in args for args in tree.values()):
            raise CheckError('A process runs with --no-sandbox')
    return main


def run_window_check(app_root: Path, work: Path, port: int, unsandboxed: bool, holder: list) -> list[str]:
    if not shutil.which('xvfb-run'):
        raise CheckError('xvfb-run is not installed (package xvfb)')
    window_port = port + 30
    command = ['xvfb-run', '-a', str(app_root / 'Qorqbot')]
    if unsandboxed:
        command.append('--no-sandbox')
    with open(work / 'window.log', 'wb') as log:
        process = subprocess.Popen(command, env=base_env(work, 'w', window_port), cwd=work,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    holder.append(process)
    home = work / 'w'
    shown_port = wait_for_dashboard(home, time.time() + 240, process)
    main = check_process_tree(app_root, unsandboxed)
    os.kill(main, signal.SIGTERM)
    deadline = time.time() + 90
    while time.time() < deadline and read_proc(main):
        time.sleep(1)
    if read_proc(main):
        raise CheckError('The app did not quit within 90 seconds of SIGTERM')
    if http_status(f'http://127.0.0.1:{shown_port}/') is not None:
        raise CheckError('The dashboard still answers after the app quit')
    status = subprocess.run(['./qorqbot', 'status'], cwd=home / 'Qorqbot' / 'qorqbot',
                            env=base_env(work, 'w', window_port), capture_output=True, text=True, timeout=60)
    if 'Stopped' not in status.stdout + status.stderr:
        raise CheckError(f'qorqbot status did not say Stopped after quitting: {status.stdout.strip()}')
    return ['app window starts (renderer and zygote processes running)',
            'the server started by the app answers HTTP 200',
            'quitting the app stops the server']


def ancestors_of_self() -> set[int]:
    found, pid = {os.getpid()}, os.getpid()
    while pid > 1:
        info = read_proc(pid)
        if not info:
            break
        pid = info[0]
        found.add(pid)
    return found


def inside(path: str, folder: Path) -> bool:
    return path == str(folder) or path.startswith(str(folder) + '/')


def leftover_pids(work: Path) -> list[int]:
    keep, uid, found = ancestors_of_self(), os.getuid(), []
    for name in os.listdir('/proc'):
        pid = int(name) if name.isdigit() else 0
        if not pid or pid in keep:
            continue
        try:
            if os.stat(f'/proc/{pid}').st_uid != uid:
                continue
            places = [os.readlink(f'/proc/{pid}/exe'), os.readlink(f'/proc/{pid}/cwd')]
        except OSError:
            continue
        if any(inside(place, work) for place in places):
            found.append(pid)
    return found


def clean_up(work: Path, holder: list) -> None:
    for home in ('e', 'w'):
        root = work / home / 'Qorqbot' / 'qorqbot'
        if (root / 'qorqbot').exists():
            try:
                subprocess.run(['./qorqbot', 'stop'], cwd=root, env=base_env(work, home, 0),
                               capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass
    for process in holder:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(2)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in leftover_pids(work):
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        time.sleep(2)


def check_installed(app_root: Path, archive: Path, work: Path, port: int, version: str,
                    unsandboxed: bool, holder: list) -> list[str]:
    checks = check_installed_files(app_root, archive, unsandboxed)
    checks += ['installed engine: ' + item for item in
               run_engine_check(app_root, work, version, sha256_of(archive), port)]
    checks += run_window_check(app_root, work, port, unsandboxed, holder)
    if not unsandboxed:
        checks.append('Chromium sandbox on: no process runs with --no-sandbox')
    return checks


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Check a built Qorqbot .deb and the installed app.')
    parser.add_argument('--deb', required=True, type=Path)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--app-root', type=Path, default=Path('/opt/Qorqbot'))
    parser.add_argument('--skip-installed', action='store_true', help='package checks only')
    parser.add_argument('--work', type=Path, default=None,
                        help='fresh folder used as HOME for the installed checks (default /tmp/qd-<pid>)')
    parser.add_argument('--port', type=int, default=1949, help='first port for the app (default 1949)')
    parser.add_argument('--unsandboxed-extract', action='store_true',
                        help='developer machines only, for a package unpacked with dpkg-deb -x: runs the app '
                             'with --no-sandbox and does not test the sandbox. Never used for a release check.')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    work = (args.work or Path(f'/tmp/qd-{os.getpid()}')).absolute()
    holder: list = []
    started = False
    ok = False
    try:
        version, checks = check_package(args.deb, args.archive)
        not_tested = list(NOT_TESTED)
        if not args.skip_installed:
            prepare_work(work)  # refuses a used folder before anything can be cleaned up in it
            started = True
            checks += check_installed(args.app_root, args.archive, work, args.port, version,
                                      args.unsandboxed_extract, holder)
            if args.unsandboxed_extract:
                not_tested.append('Chromium sandbox (the app ran with --no-sandbox)')
        report = {
            'version': version,
            'source_commit': os.environ.get('GITHUB_SHA'),
            'deb_sha256': sha256_of(args.deb),
            'archive_sha256': sha256_of(args.archive),
            'checks': checks,
            'not_tested': not_tested,
        }
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(f'Desktop package checks passed: {args.report}')
        ok = True
        return 0
    except CheckError as error:
        print(f'Check failed: {error}', file=sys.stderr)
        return 1
    finally:
        if started:
            clean_up(work, holder)
            if ok:
                shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
