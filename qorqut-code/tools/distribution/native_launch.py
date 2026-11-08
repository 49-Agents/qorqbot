#!/usr/bin/env python3
"""Consumer launcher, copied to runtime/launch.py by native_build.py.

Uses system Python and standard venv/pip with the release wheelhouse.
No installer, daemon, permission broker or host configuration is installed.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def read_json(path):
    return json.loads(path.read_text())


def save_json(path, value):
    temp = path.with_suffix('.new')
    with temp.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def link_user_keychains(home, user_home):
    """macOS finds the login keychain through $HOME/Library/Keychains. Agent CLIs keep their sign-in there, so the
    private home links that one folder to the user's own; without it macOS reports "A keychain cannot be found" and
    offers to reset the keychain. Nothing else of the user's Library is shared, and an existing entry is never replaced."""
    target = Path(user_home) / 'Library' / 'Keychains'
    link = Path(home) / 'Library' / 'Keychains'
    if Path(home).resolve() == Path(user_home).resolve() or not target.is_dir():
        return
    if link.is_symlink() or link.exists():
        return
    link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    link.symlink_to(target, target_is_directory=True)


def environment(root, home):
    runtime = root / 'runtime'
    env = dict(os.environ)
    user_home = Path(env.get('HOME', str(Path.home())))
    user_path = env.get('PATH', os.defpath)
    # Preserve the invoking user's CLI configuration separately from app storage.
    # None means unset, which is different from an empty or synthesized path.
    native_keys = ('HOME', 'PATH', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME',
                   'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME',
                   'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR',
                   'npm_config_cache', 'PIP_CACHE_DIR', 'GIT_CONFIG_NOSYSTEM',
                   'GIT_CONFIG_GLOBAL', 'PIP_CONFIG_FILE', 'PIP_NO_INDEX')
    native_env = {key: env.get(key) for key in native_keys}
    native_env['HOME'] = str(user_home)
    native_env['PATH'] = user_path
    env['QORQUT_NATIVE_USER_ENV'] = json.dumps(native_env)
    for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'QORQUT_INSTALLATION_CONFIG',
                'QORQUT_APP_MANIFEST', 'QORQUT_TELEGRAM_CONFIG_FILE', 'BIZ_DASH_TOKEN',
                'BAZARLYK_MCP_URL', 'QORQUT_TERMINAL_WORKSPACE', 'PIP_TARGET',
                'PIP_PREFIX', 'PIP_ROOT', 'PIP_USER', 'PYTHONUSERBASE',
                'QORQUT_NATIVE_ROOT'):
        env.pop(key, None)
    paths = {
        'HOME': home, 'XDG_CONFIG_HOME': home / 'config', 'XDG_CACHE_HOME': home / 'cache',
        'XDG_DATA_HOME': home / 'data', 'XDG_STATE_HOME': home / 'state',
        'XDG_RUNTIME_DIR': home / 'run', 'TMPDIR': home / 'tmp', 'TMUX_TMPDIR': root / '.run',
        'npm_config_cache': home / 'cache/npm', 'PIP_CACHE_DIR': home / 'cache/pip',
    }
    for key, path in paths.items():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[key] = str(path)
    if sys.platform == 'darwin':
        link_user_keychains(home, user_home)
    env.update({
        'PATH': str(runtime / 'venv/bin') + os.pathsep + user_path,
        'QORQUT_FOLDER_ROOT': str(root),
        'QORQUT_CALENDAR_CONFIG': str(root / 'instance/config/calendar.json'),
        'BIZ_DASH_HOST': '127.0.0.1', 'BIZ_TMUX_SOCKET': 'bazarlyk',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
        'PYTHONDONTWRITEBYTECODE': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
        'PIP_CONFIG_FILE': os.devnull, 'PIP_NO_INDEX': '1',
        'LANG': 'C.UTF-8',
    })
    # Vendor clients remain user-installed and use the user's existing account
    # configuration. Qorqbot's own state and generated workspace files stay here.
    env.setdefault('CODEX_HOME', str(user_home / '.codex'))
    browser_tool = agent_browser_path(root)
    if 'QORQUT_AGENT_BROWSER_EXECUTABLE' not in env and browser_tool.is_file():
        env['QORQUT_AGENT_BROWSER_EXECUTABLE'] = str(browser_tool)
    env.setdefault('CLAUDE_CONFIG_DIR', str(user_home / '.claude'))
    timezone_file = root / 'instance/config/operational-timezone'
    if timezone_file.exists():
        zone = timezone_file.read_text().strip()
    else:
        zone = env.get('QORQUT_TIMEZONE', '')
        if not zone:
            localtime = str(Path('/etc/localtime').resolve())
            zone = localtime.partition('/zoneinfo/')[2] or 'UTC'
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise RuntimeError('The installation timezone is not a valid IANA timezone: ' + zone) from None
    env['QORQUT_TIMEZONE'] = zone
    return env


def verify(runtime):
    manifest = read_json(runtime / 'PAYLOAD.json')
    if manifest.get('format') != 1:
        raise RuntimeError('Unsupported package manifest')
    for name, expected in manifest['files'].items():
        parts = Path(name).parts
        if Path(name).is_absolute() or '..' in parts:
            raise RuntimeError('Invalid package path')
        path = runtime / name
        if not path.is_file() or not path.resolve().is_relative_to(runtime):
            raise RuntimeError('Missing package file: ' + name)
        hasher = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(chunk)
        if hasher.hexdigest() != expected:
            raise RuntimeError('Package checksum failed: ' + name)
    return manifest


def run(argv, *, env, cwd):
    subprocess.run(list(map(str, argv)), env=env, cwd=cwd, check=True)


def install_hint(*packages, system=None):
    """The command that installs ``packages`` here: Homebrew on macOS, apt on Ubuntu (including Ubuntu in WSL)."""
    if (system or platform.system()) == 'Darwin':
        names = {'python3.12': 'python@3.12', 'python3.12-venv': None}
        return 'brew install ' + ' '.join(n for n in (names.get(p, p) for p in packages) if n)
    return 'sudo apt install ' + ' '.join(packages)


def require_prerequisites(env):
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('This download requires Python 3.12 with venv support. Install it with: '
                           + install_hint('python3.12', 'python3.12-venv'))
    if not shutil.which('git', path=env.get('PATH')):
        raise RuntimeError('Git is required. Install it with: ' + install_hint('git'))
    try:
        import ensurepip
    except ImportError:
        raise RuntimeError('Python venv support is required. Install it with: '
                           + install_hint('python3.12-venv')) from None


# The browser tool the agents' own browser control drives (bazarlyk.browser), pinned to the version and npm integrity
# in qorqut-code/app/tools/browser/package-lock.json. Qorqbot installs it into the folder itself on first launch; only the
# native binary for this machine is kept, so no Node is needed to run it.
AGENT_BROWSER_VERSION = '0.38.1'
AGENT_BROWSER_URL = 'https://registry.npmjs.org/agent-browser/-/agent-browser-0.38.1.tgz'
AGENT_BROWSER_SHA512 = 'k58FCz0yUOCANoNkMiqJe+H2y6r6sUZazqXsWF+MYq1iRC42PjtLcBoag6SSTOD/FRQppvPDvE5HDYEhclvnhw=='
AGENT_BROWSER_LIMIT = 200 * 1024 * 1024
MAC_CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
CHROME_URL = 'https://www.google.com/chrome/'


def agent_browser_binary_name(system=None, machine=None):
    """The binary in the agent-browser package for this machine, or None where Qorqbot has no package."""
    system, machine = (system or platform.system()), (machine or platform.machine()).lower()
    if system == 'Darwin' and machine in ('arm64', 'aarch64'):
        return 'agent-browser-darwin-arm64'
    if system == 'Linux' and machine in ('x86_64', 'amd64'):
        return 'agent-browser-linux-x64'
    return None


def agent_browser_path(root):
    return Path(root) / 'tools' / ('agent-browser-' + AGENT_BROWSER_VERSION) / 'agent-browser'


def _download(url, destination, limit, timeout=120, opener=urllib.request.urlopen):
    digest = hashlib.sha512()
    size = 0
    with opener(url, timeout=timeout) as response, destination.open('wb') as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise RuntimeError('The agent-browser download is larger than expected')
            digest.update(chunk)
            output.write(chunk)
    return base64.b64encode(digest.digest()).decode()


def ensure_agent_browser(root, *, system=None, machine=None, download=_download):
    """Install the pinned agent-browser into ``root/tools`` if it is not there yet. Returns its path, or None with the
    reason as a second value when it could not be installed (offline, unsupported machine). Never raises for those."""
    target = agent_browser_path(root)
    if target.is_file() and os.access(target, os.X_OK):
        return target, None
    name = agent_browser_binary_name(system, machine)
    if name is None:
        return None, 'Web browsing for agents is not available on this machine type.'
    work = target.parent.parent / ('.agent-browser-download-' + str(os.getpid()))
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, mode=0o700)
    try:
        archive = work / 'package.tgz'
        try:
            integrity = download(AGENT_BROWSER_URL, archive, AGENT_BROWSER_LIMIT)
        except OSError:
            return None, 'Could not download the browser tool for agents. Qorqbot tries again at the next start.'
        if integrity != AGENT_BROWSER_SHA512:
            raise RuntimeError('The agent-browser download does not match its pinned checksum')
        with tarfile.open(archive, 'r:gz') as package:
            member = package.getmember('package/bin/' + name)
            if not member.isfile():
                raise RuntimeError('The agent-browser package has an unexpected layout')
            source = package.extractfile(member)
            staged = work / 'agent-browser'
            with staged.open('wb') as output:
                shutil.copyfileobj(source, output)
        staged.chmod(0o755)
        check = subprocess.run([str(staged), '--version'], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
        if check.returncode != 0 or AGENT_BROWSER_VERSION not in check.stdout:
            raise RuntimeError('The downloaded agent-browser does not run on this machine')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(staged, target)
        return target, None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def chrome_installed(path=None, system=None):
    """Google Chrome, which the agents' browsers run on. Qorqbot cannot install it; the owner is told when it is missing."""
    if (system or platform.system()) == 'Darwin' and os.access(MAC_CHROME, os.X_OK):
        return True
    return any(shutil.which(name, path=path) for name in ('google-chrome-stable', 'google-chrome', 'chromium', 'chromium-browser'))


AGENT_CLIS = (('claude', 'Claude Code', 'https://docs.anthropic.com/en/docs/claude-code'),
              ('codex', 'Codex', 'https://github.com/openai/codex'))


def readiness_notes(path, system=None):
    """Optional tools the dashboard works without but agents need. Returned as lines to print, never raised."""
    notes = []
    if not chrome_installed(path, system):
        notes.append('Your agents need Google Chrome to work on the web. Install it from ' + CHROME_URL)
    if not shutil.which('tmux', path=path):
        notes.append('Agent terminals need tmux. Install it with: ' + install_hint('tmux', system=system))
    if not any(shutil.which(command, path=path) for command, _, _ in AGENT_CLIS):
        notes.append('No agent CLI found. Install one and sign in once in a terminal: '
                     + '; '.join(f'{name} ({command}) {url}' for command, name, url in AGENT_CLIS))
    return notes


def browser_command(url, system=None, release=None):
    """How to show ``url`` in the user's browser, or None when no browser opener is known.
    Under WSL the Windows browser is used, because the dashboard is forwarded to Windows localhost."""
    system = system or platform.system()
    if system == 'Darwin':
        return ['open', url]
    if release is None:
        try:
            release = Path('/proc/sys/kernel/osrelease').read_text()
        except OSError:
            release = ''
    if 'microsoft' in release.lower():
        for opener in ('wslview', 'explorer.exe'):
            if shutil.which(opener):
                return [opener, url]
        return None
    return ['xdg-open', url] if shutil.which('xdg-open') else None


def open_browser(url):
    command = browser_command(url)
    if command is None:
        return False
    try:
        # explorer.exe reports a non-zero status even when it opened the page, so the status is not checked.
        subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


COMMAND_MARKER = '# Qorqbot terminal command'


def command_shim(root):
    return f"#!/bin/sh\n{COMMAND_MARKER}. Runs the installation in the folder below.\nexec {shlex.quote(str(root / 'qorqbot'))} \"$@\"\n"


def command_directory(path_value, home, system=None):
    """A directory on PATH the user can write without a password: Homebrew's bin on macOS (Homebrew is already a
    prerequisite), otherwise ~/.local/bin. Returns (directory, on_path)."""
    entries = [Path(entry) for entry in path_value.split(os.pathsep) if entry]
    candidates = [Path('/opt/homebrew/bin'), Path('/usr/local/bin')] if (system or platform.system()) == 'Darwin' else []
    for candidate in candidates:
        if candidate in entries and candidate.is_dir() and os.access(candidate, os.W_OK):
            return candidate, True
    local = Path(home) / '.local/bin'
    return local, local in entries


def install_command(root, directory=None, remove=False, path_value=None, home=None):
    """Write or remove the ``qorqbot`` command. A file that Qorqbot did not write is never replaced or removed."""
    path_value = os.environ.get('PATH', os.defpath) if path_value is None else path_value
    home = Path(home or os.environ.get('HOME') or Path.home())
    if directory is None:
        directory, on_path = command_directory(path_value, home)
    else:
        directory = Path(directory)
        on_path = directory in [Path(entry) for entry in path_value.split(os.pathsep) if entry]
    target = directory / 'qorqbot'
    owned = target.is_file() and not target.is_symlink() and COMMAND_MARKER in target.read_text(errors='replace')
    if (target.exists() or target.is_symlink()) and not owned:
        raise RuntimeError(f'{target} already exists and was not created by Qorqbot; it was left unchanged')
    if remove:
        if owned:
            target.unlink()
            return f'Removed {target}'
        return f'There is no qorqbot command in {directory}'
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / '.qorqbot.new'
    temporary.write_text(command_shim(root))
    temporary.chmod(0o755)
    temporary.replace(target)
    message = f'Installed {target}. Run qorqbot from any folder to start Qorqbot and open it in your browser.'
    if not on_path:
        message += f'\n{directory} is not on your PATH yet. Add this line to your shell profile, then open a new terminal:\n  export PATH="{directory}:$PATH"'
    return message


def default_port(run_dir):
    """The port this folder last served on. Agents keep the dashboard address they were started with, so a later
    start without --port reuses it; 1049 is the first default."""
    try:
        value = (run_dir / 'port').read_text().strip()
    except OSError:
        return 1049
    return int(value) if value.isdigit() and 1024 <= int(value) <= 65535 else 1049


def package_platform(runtime):
    return read_json(runtime / 'PAYLOAD.json').get('platform', 'ubuntu24-x86_64')


def require_platform(runtime):
    target = package_platform(runtime)
    if target == 'ubuntu24-x86_64' and platform.system() == 'Linux' and platform.machine() == 'x86_64':
        return
    if target == 'macos14-arm64' and platform.system() == 'Darwin' and platform.machine() == 'arm64':
        version = platform.mac_ver()[0].split('.')[0]
        if version.isdigit() and int(version) >= 14:
            return
    raise RuntimeError('This archive targets ' + target + '; use the download for this operating system and CPU.')


def inspect_darwin(root, *arguments):
    # psutil is an application library, not a prerequisite in system Python.
    # Existing installation inspection uses its installed environment, including
    # the prior environment during interrupted-update recovery.
    python = next((root / name / 'venv/bin/python' for name in ('runtime', '.previous-runtime')
                   if (root / name / 'ENVIRONMENT.json').is_file()
                   and (root / name / 'venv/bin/python').is_file()), None)
    if python is None:
        raise RuntimeError('A completed Python environment is needed to inspect macOS processes safely. For a fresh download, run ./qorqbot setup first.')
    try:
        result = subprocess.run([str(python), '-I', '-B', '-m', 'bazarlyk.process_info', *map(str, arguments)],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('Unable to run macOS process inspection safely') from error
    if result.returncode == 3 and arguments[0] == 'identity':
        return None
    if result.returncode:
        raise RuntimeError('Unable to verify macOS process ownership: ' + result.stderr.strip())
    return result.stdout.strip()


def require_stopped_terminals(root, env):
    executable = shutil.which('tmux', path=env.get('PATH'))
    if not executable:
        if any((root / '.run').glob('tmux-*/*')):
            raise ValueError('tmux is unavailable and terminal sockets remain; restore tmux and close this installation’s terminals.')
        return
    result = subprocess.run([executable, '-L', 'bazarlyk', 'list-sessions'],
                            env=env, capture_output=True, text=True)
    if result.returncode == 0:
        raise ValueError('Stop this installation’s agent terminals before changing its runtime')
    if not any(text in result.stderr.lower() for text in ('no server running', 'no such file or directory')):
        raise ValueError('Could not prove that this installation’s agent terminals are stopped')


def installation_processes(root):
    """Find detached tools owned by this installation, including system executables."""
    if platform.system() == 'Darwin':
        return [item['pid'] for item in json.loads(inspect_darwin(root, 'owned', root,
                                                                 '--exclude-pid', os.getpid()))]
    found = []
    marker = ('QORQUT_FOLDER_ROOT=' + str(root)).encode()
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            owned = (process / 'exe').resolve().is_relative_to(root)
            owned = owned or marker in (process / 'environ').read_bytes().split(b'\0')
            if owned:
                found.append(int(process.name))
        except (OSError, ValueError):
            continue
    return found


def prepare(root, env):
    runtime = root / 'runtime'
    marker = runtime / 'ENVIRONMENT.json'
    if marker.exists():
        if read_json(marker)['root'] != str(root):
            raise RuntimeError('This installation has moved. Restore its original folder before starting it.')
        return
    print('Preparing Qorqbot from the bundled files…', flush=True)
    verify(runtime)
    venv = runtime / 'venv'
    if venv.exists():
        raise RuntimeError('An incomplete Python environment remains in runtime/venv. Keep it for diagnosis; setup was not retried.')
    require_prerequisites(env)
    run([sys.executable, '-I', '-B', '-m', 'venv', '--copies', venv], env=env, cwd=root)
    python = venv / 'bin/python'
    # Standard pip resolves the declared library dependencies from the verified
    # local wheelhouse. External programs are never installed by this command.
    run([python, '-I', '-B', '-m', 'pip', 'install', '--no-index', '--no-compile',
         '--find-links', runtime / 'wheelhouse', '--find-links', runtime / 'wheels',
         '--require-hashes', '-r', runtime / 'requirements.lock'], env=env, cwd=root)
    run([python, '-I', '-B', '-m', 'pip', 'check'], env=env, cwd=root)
    save_json(marker, {'format': 1, 'root': str(root)})


def initialize(root, env):
    runtime, instance = root / 'runtime', root / 'instance'
    receipt = instance / 'config/initialization-receipt.json'
    if receipt.exists():
        record = read_json(receipt)
        if record.get('state') != 'initialized' or record.get('instance_root') != str(instance):
            raise RuntimeError('The existing instance is incomplete or belongs to a different folder')
        return record
    if instance.exists() and any(instance.iterdir()):
        raise RuntimeError('The instance folder is not empty and has no successful initialization receipt; nothing was replaced')
    run([runtime / 'venv/bin/python', '-I', '-B', '-m', 'bazarlyk.bootstrap.initialize',
         '--prepared-root', runtime, '--instance-root', instance], env=env, cwd=root)
    (instance / 'config/operational-timezone').write_text(env['QORQUT_TIMEZONE'] + '\n')
    return read_json(receipt)


def identity(pid):
    if platform.system() == 'Darwin':
        value = inspect_darwin(Path(__file__).resolve().parent.parent, 'identity', pid)
        if value is None:
            return None
        boot, separator, start = value.rpartition(':')
        if not separator or not boot or not start:
            raise RuntimeError('macOS process inspection returned an invalid identity')
        return {'boot': boot, 'start': start}
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'start': fields[19]}
    except (OSError, IndexError):
        return None


def active(path):
    try:
        saved = read_json(path)
        return saved if identity(saved['pid']) == saved['identity'] else None
    except (OSError, ValueError, KeyError):
        return None


def require_completed_extension_installs(runtime):
    for journal in sorted((runtime / 'optional-install').glob('*/install.json')):
        if read_json(journal).get('phase') not in {'complete', 'refused'}:
            raise RuntimeError('An optional extension installation is incomplete. Resume the same extension-install command with --resume before starting Qorqbot.')


def require_completed_update(root):
    journal = root / '.run/update.json'
    if journal.exists() and read_json(journal).get('phase') not in {'complete', 'restored-before-start'}:
        raise RuntimeError('An update was interrupted. Run ./qorqbot update --recover before starting Qorqbot.')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run Qorqbot in this folder.')
    parser.add_argument('command', nargs='?', default='start',
                        choices=('start', 'stop', 'status', 'verify', 'setup', 'calendar-connect', 'install-command'))
    parser.add_argument('--port', type=int, help='Local port. Defaults to the port this folder last used, first 1049.')
    parser.add_argument('--no-open', action='store_true', help='Do not open the dashboard in your browser.')
    parser.add_argument('--dir', type=Path, help='install-command: directory for the qorqbot command.')
    parser.add_argument('--remove', action='store_true', help='install-command: remove the qorqbot command.')
    args, remaining = parser.parse_known_args(argv)
    if remaining and args.command != 'calendar-connect':
        parser.error('Unrecognized arguments: ' + ' '.join(remaining))
    root = Path(__file__).resolve().parent.parent
    runtime = root / 'runtime'
    if args.command == 'install-command':
        print(install_command(root, args.dir, args.remove))
        return 0
    # The browser opens only for a person at a terminal, never for the desktop app or a service manager.
    show = args.command == 'start' and not args.no_open and sys.stdout.isatty()
    require_platform(runtime)
    os.umask(0o077)
    run_dir = root / '.run'
    run_dir.mkdir(mode=0o700, exist_ok=True)
    if max(len(os.fsencode(run_dir / f'tmux-{os.getuid()}/bazarlyk')),
           len(os.fsencode(run_dir / 'c/org.chromium.Chromium.XXXXXX/SingletonSocket'))) >= 104:
        raise RuntimeError('Choose a shorter installation path so local terminal sockets fit (roughly 50 characters or fewer)')
    state = run_dir / 'app.json'
    existing = active(state)
    if args.port is None:
        args.port = default_port(run_dir)
    if args.command == 'status':
        # The desktop app reads "from Terminal" to leave a Terminal-started server running when it quits.
        print(('Running at ' + existing['url'] + (' from Terminal' if existing.get('terminal') else '')) if existing else 'Stopped')
        return 0 if existing else 1
    if args.command == 'stop':
        if existing:
            os.kill(existing['pid'], signal.SIGTERM)
            for _ in range(300):
                if not active(state):
                    print('Stopped')
                    return 0
                time.sleep(.1)
            raise RuntimeError('Qorqbot is still shutting down; no process was forcibly terminated')
        print('Stopped')
        return 0
    if args.command == 'verify':
        verify(runtime)
        print('Package checksums verified')
        return 0
    if args.command == 'start' and existing:
        print('Qorqbot is already running at ' + existing['url'], flush=True)
        if show:
            open_browser(existing['url'])
        return 0
    with (run_dir / 'app.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Qorqbot is already starting or running from this folder') from None
        require_completed_extension_installs(runtime)
        require_completed_update(root)
        env = environment(root, runtime / 'setup-state')
        require_prerequisites(env)
        prepare(root, env)
        record = initialize(root, env)
        # First launch installs the agents' browser tool; later launches find it in place.
        try:
            _, browser_problem = ensure_agent_browser(root)
        except (RuntimeError, OSError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
            browser_problem = 'The browser tool for agents could not be installed: ' + str(error)
        if browser_problem:
            print(browser_problem, flush=True)
        if args.command == 'calendar-connect':
            env = environment(root, root / 'instance/home')
            run([runtime / 'venv/bin/python', '-I', '-B', '-m', 'qorqut_google_calendar.connect',
                 *remaining, '--config', root / 'instance/config/calendar.json'], env=env, cwd=root)
            config = root / 'instance/config/extensions.json'
            selected = read_json(config)
            factory = 'qorqut_google_calendar.extension:create'
            if not any(item.get('factory') == factory for item in selected['extensions']):
                selected['extensions'].append({'factory': factory, 'config': {}})
                save_json(config, selected)
            print('Google Calendar is connected. Start Qorqbot normally.')
            return 0
        if args.command == 'setup':
            print('Qorqbot is ready. Run ./qorqbot to start.')
            return 0
        if not 1024 <= args.port <= 65535:
            raise RuntimeError('Choose a port from 1024 to 65535')
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(('127.0.0.1', args.port))
            except OSError:
                raise RuntimeError(f'Port {args.port} is in use by another program. Close it, or start with --port and a free port.') from None
        (run_dir / 'port').write_text(f'{args.port}\n')
        env = environment(root, root / 'instance/home')
        argv = [runtime / 'venv/bin/python', '-I', '-B', '-m', 'bazarlyk.service',
                '--workspace', runtime / 'src', '--instance-root', root / 'instance',
                '--extensions-config', root / 'instance/config/extensions.json',
                '--token-file', root / 'instance/config/owner-token', '--host', '127.0.0.1', '--port', str(args.port)]
        for name, location in record['components'].items():
            argv.extend(['--component', f'{name}={location}'])
        logdir = root / 'instance/logs'
        logdir.mkdir(mode=0o700, exist_ok=True)
        with (logdir / 'application.log').open('ab') as log:
            child = subprocess.Popen(list(map(str, argv)), env=env, cwd=root, stdout=log, stderr=log)
            stop_requested = False
            def stop(signum, frame):
                nonlocal stop_requested
                stop_requested = True
                if child.poll() is None:
                    child.send_signal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            url = f'http://127.0.0.1:{args.port}'
            save_json(state, {'pid': os.getpid(), 'identity': identity(os.getpid()), 'url': url,
                              'terminal': sys.stdin.isatty()})
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(300):
                    if child.poll() is not None:
                        if stop_requested and child.returncode in (0, -signal.SIGTERM, -signal.SIGINT):
                            return 0
                        raise RuntimeError('Qorqbot exited during startup. Details are in instance/logs/application.log')
                    try:
                        with opener.open(url, timeout=1) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(.2)
                else:
                    child.terminate()
                    child.wait(timeout=30)
                    raise RuntimeError('Qorqbot did not become ready. Details are in instance/logs/application.log')
                print(f'Qorqbot is running at {url}\nPress Ctrl+C to stop.', flush=True)
                if sys.stdout.isatty():
                    for note in readiness_notes(env.get('PATH')):
                        print(note, flush=True)
                if show and not open_browser(url):
                    print('Open the address above in your browser.', flush=True)
                code = child.wait()
                # Uvicorn can finish graceful shutdown by re-raising the received
                # signal. An owner-requested stop is a successful launcher exit.
                if stop_requested and code in (0, -signal.SIGTERM, -signal.SIGINT):
                    return 0
                return code if code >= 0 else 128 - code
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=30)
                state.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('Qorqbot: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
