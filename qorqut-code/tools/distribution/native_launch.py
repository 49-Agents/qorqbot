#!/usr/bin/env python3
"""Consumer launcher, copied to runtime/launch.py by native_build.py.

A Linux package uses system Python and standard venv/pip with the release wheelhouse.
A self-contained (Mac) package ships its Python environment and git ready to use.
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


# The variable that names each agent client's own folder (its sign-in, settings, own instructions, skills, plugins, memory
# and conversations), with the folder Qorqbot keeps for it and the one the client uses by default in the user's home.
CLIENT_HOMES = {'CLAUDE_CONFIG_DIR': ('instance/home/claude', '.claude'), 'CODEX_HOME': ('instance/home/codex', '.codex')}
OWN_CLIENT_HOMES = 'instance/config/own-client-homes'


def own_client_homes(root):
    """Whether this installation gives Claude Code and Codex folders of its own. Every installation initialized by this
    build or a later one does (``initialize`` records it). One initialized earlier keeps using the user's own folders, where
    its sign-in and its agents' conversations are, until it is switched."""
    return (root / OWN_CLIENT_HOMES).is_file()


def client_homes(root, user_home, env):
    """``{variable: (folder, own)}`` for both agent clients. A folder named in the environment Qorqbot was started from is
    kept as given. Otherwise the folder is Qorqbot's own, inside the installation: the clients then sign in separately from
    the user's own Claude Code and Codex and read none of the user's instructions, settings, skills, plugins or memory, and
    nothing Qorqbot's agents do is written into the user's. Without the record (``own_client_homes``) it is the user's."""
    own, homes = own_client_homes(root), {}
    for variable, (folder, default) in CLIENT_HOMES.items():
        if env.get(variable):
            homes[variable] = (env[variable], False)
        elif own:
            path = root / folder
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            homes[variable] = (str(path), True)
        else:
            homes[variable] = (str(user_home / default), False)
    return homes


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
    # The clients keep the user's HOME (their login keychain, git and SSH setup) and get their own folders by name. With
    # the user's folders the recorded value stays as the user had it, unset included: on macOS Claude Code names its
    # Keychain item after an explicitly named folder, so naming the default one would read a different sign-in.
    homes = client_homes(root, user_home, env)
    for variable, (folder, own) in homes.items():
        if own:
            native_env[variable] = folder
    env['QORQUT_NATIVE_USER_ENV'] = json.dumps(native_env)
    for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'QORQUT_INSTALLATION_CONFIG',
                'QORQUT_APP_MANIFEST', 'QORQUT_TELEGRAM_CONFIG_FILE', 'QORQUT_DASH_TOKEN',
                'QORQUT_MCP_URL', 'BAZARLYK_MCP_URL', 'QORQUT_TERMINAL_WORKSPACE', 'PIP_TARGET',
                'PIP_PREFIX', 'PIP_ROOT', 'PIP_USER', 'PYTHONUSERBASE',
                'QORQUT_NATIVE_ROOT'):
        env.pop(key, None)
    paths = {
        'HOME': home, 'XDG_CONFIG_HOME': home / 'config', 'XDG_CACHE_HOME': home / 'cache',
        'XDG_DATA_HOME': home / 'data', 'XDG_STATE_HOME': home / 'state',
        'XDG_RUNTIME_DIR': home / 'run', 'TMPDIR': home / 'tmp',
        'npm_config_cache': home / 'cache/npm', 'PIP_CACHE_DIR': home / 'cache/pip',
    }
    for key, path in paths.items():
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[key] = str(path)
    if sys.platform == 'darwin':
        link_user_keychains(home, user_home)
    # A self-contained package ships git; it comes before the computer's own so every Mac runs the same one.
    shipped = [str(runtime / 'native/bin')] if (runtime / 'native/bin').is_dir() else []
    env.update({
        'PATH': os.pathsep.join([str(runtime / 'venv/bin'), *shipped, user_path]),
        'QORQUT_FOLDER_ROOT': str(root),
        'QORQUT_CALENDAR_CONFIG': str(root / 'instance/config/calendar.json'),
        'QORQUT_DASH_HOST': '127.0.0.1',
        # One installation's agent terminals (legacy Terminals mode) stay apart from another's on the same computer.
        'TMUX_TMPDIR': str(root / '.run'),
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
        'PYTHONDONTWRITEBYTECODE': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
        'PIP_CONFIG_FILE': os.devnull, 'PIP_NO_INDEX': '1',
        'LANG': 'C.UTF-8',
    })
    # The service names both client folders explicitly (``client_homes``): Qorqbot's own, or the user's on an installation
    # initialized before it kept its own.
    for variable, (folder, _own) in homes.items():
        env[variable] = folder
    browser_tool = shipped_agent_browser(root) or agent_browser_path(root)
    if 'QORQUT_AGENT_BROWSER_EXECUTABLE' not in env and browser_tool.is_file():
        env['QORQUT_AGENT_BROWSER_EXECUTABLE'] = str(browser_tool)
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


BUNDLED_TREES = ('native', 'venv')


def tree_digest(folder):
    """One SHA-256 for a folder: every file's path, owner-executable bit and content, and every link's path and target."""
    entries = []
    for base, directories, files in os.walk(folder):
        for name in directories + files:
            path = Path(base) / name
            relative = path.relative_to(folder).as_posix()
            if path.is_symlink():
                entries.append((relative, 'link', os.readlink(path)))
            elif path.is_file():
                hasher = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        hasher.update(chunk)
                entries.append((relative, 'program' if path.stat().st_mode & 0o100 else 'file', hasher.hexdigest()))
            elif not path.is_dir():
                raise RuntimeError('Unexpected special file in the package: ' + relative)
    digest = hashlib.sha256()
    for entry in sorted(entries):
        digest.update('\0'.join(entry).encode() + b'\n')
    return digest.hexdigest()


def bundled(runtime):
    """What a self-contained package ships ready to use, as {folder: digest}: its Python environment (``venv``) and its
    git (``native``). Empty for a package that uses the computer's own Python and git."""
    trees = read_json(runtime / 'PAYLOAD.json').get('bundled') or {}
    if not isinstance(trees, dict) or set(trees) - set(BUNDLED_TREES):
        raise RuntimeError('Unsupported package manifest')
    return trees


def verify(runtime):
    manifest = read_json(runtime / 'PAYLOAD.json')
    if manifest.get('format') != 1:
        raise RuntimeError('Unsupported package manifest')
    for name, expected in bundled(runtime).items():
        # Once the environment is in use, optional extensions are installed into it; it is checked as shipped only.
        if name == 'venv' and (runtime / 'ENVIRONMENT.json').exists():
            continue
        if not (runtime / name).is_dir() or tree_digest(runtime / name) != expected:
            raise RuntimeError('Package checksum failed: ' + name)
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


def run(argv, *, env, cwd, log=None):
    """Run one set-up step. With ``log`` its output goes to that file instead of the terminal, and a failure is raised
    with the step's last lines and the file's path, so the owner sees the real error and where the rest is."""
    if log is None:
        subprocess.run(list(map(str, argv)), env=env, cwd=cwd, check=True)
        return
    with Path(log).open('ab') as stream:
        stream.write(('\n$ ' + shlex.join(map(str, argv)) + '\n').encode())
        stream.flush()
        start = stream.tell()
        code = subprocess.run(list(map(str, argv)), env=env, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT).returncode
    if code:
        with Path(log).open('rb') as stream:
            stream.seek(max(start, Path(log).stat().st_size - 8192))
            lines = [line for line in stream.read().decode(errors='replace').splitlines() if line.strip()][-5:]
        raise RuntimeError('Setting up Qorqbot did not finish:\n  ' + '\n  '.join(lines or ['no details'])
                           + '\nThe full details are in ' + str(log))


INITIALIZATION_RECEIPT = 'instance/config/initialization-receipt.json'


def setup_log(root):
    """The file set-up output goes to: ``instance/logs/setup.log``, beside the application's log. The instance folder
    must stay empty until it is initialized, so until then the log waits in ``.run`` (``keep_setup_log`` moves it)."""
    root = Path(root)
    if not (root / INITIALIZATION_RECEIPT).exists():
        (root / '.run').mkdir(mode=0o700, exist_ok=True)
        return root / '.run/setup.log'
    logs = root / 'instance/logs'
    logs.mkdir(mode=0o700, exist_ok=True)
    return logs / 'setup.log'


def keep_setup_log(root):
    waiting = Path(root) / '.run/setup.log'
    if waiting.is_file():
        with setup_log(root).open('ab') as stream:
            stream.write(waiting.read_bytes())
        waiting.unlink()


def install_hint(*packages, system=None):
    """The command that installs ``packages`` here: Homebrew on macOS, apt on Ubuntu (including Ubuntu in WSL)."""
    if (system or platform.system()) == 'Darwin':
        names = {'python3.12': 'python@3.12', 'python3.12-venv': None}
        return 'brew install ' + ' '.join(dict.fromkeys(n for n in (names.get(p, p) for p in packages) if n))
    return 'sudo apt install ' + ' '.join(dict.fromkeys(packages))


def missing_prerequisites(env):
    """``(names, packages)`` of what this computer lacks to build Qorqbot's environment, all of it at once."""
    names, packages = [], []
    if sys.version_info[:2] != (3, 12):
        names.append('Python 3.12 with venv support')
        packages += ['python3.12', 'python3.12-venv']
    else:
        try:
            import ensurepip  # noqa: F401
        except ImportError:
            names.append('Python 3.12 venv support')
            packages.append('python3.12-venv')
    if not shutil.which('git', path=env.get('PATH')):
        names.append('Git')
        packages.append('git')
    return names, packages


def require_prerequisites(env, self_contained=False, system=None):
    if self_contained:
        # The package runs on the Python and git it ships; nothing has to be installed first.
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError('Start Qorqbot with its own ./qorqbot command, which uses the Python shipped in this folder.')
        return
    names, packages = missing_prerequisites(env)
    if names:
        listed = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1]
        raise RuntimeError(f"{listed} {'is' if len(names) == 1 else 'are'} required. Install "
                           f"{'it' if len(names) == 1 else 'them'} with this command, then run ./qorqbot again:\n  "
                           + install_hint(*packages, system=system))


# The browser tool the agents' own browser control drives (qorqut.browser), pinned to the version and npm integrity
# in qorqut-code/app/tools/browser/package-lock.json. A package ships it; a folder without it (an older download) installs it
# on first launch. Only the native binary for this machine is kept, so no Node is needed to run it.
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
    if system == 'Darwin' and machine in ('x86_64', 'amd64'):
        return 'agent-browser-darwin-x64'
    if system == 'Linux' and machine in ('x86_64', 'amd64'):
        return 'agent-browser-linux-x64'
    return None


def agent_browser_path(root):
    return Path(root) / 'tools' / ('agent-browser-' + AGENT_BROWSER_VERSION) / 'agent-browser'


def shipped_agent_browser(root):
    """The browser tool the package ships, or None. A self-contained package has it beside its git; a package that
    uses the computer's own tools has it in ``runtime/tools``, so that the folder of bundled tools stays absent there."""
    for place in ('runtime/native/bin/agent-browser', 'runtime/tools/agent-browser'):
        path = Path(root) / place
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


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


def extract_agent_browser(archive, name, target):
    """Write the one native binary ``name`` from the downloaded agent-browser package to ``target``."""
    with tarfile.open(archive, 'r:gz') as package:
        member = package.getmember('package/bin/' + name)
        if not member.isfile():
            raise RuntimeError('The agent-browser package has an unexpected layout')
        with package.extractfile(member) as source, Path(target).open('wb') as output:
            shutil.copyfileobj(source, output)
    Path(target).chmod(0o755)


def ensure_agent_browser(root, *, system=None, machine=None, download=_download):
    """Install the pinned agent-browser into ``root/tools`` if it is not there yet. Returns its path, or None with the
    reason as a second value when it could not be installed (offline, unsupported machine). Never raises for those."""
    shipped = shipped_agent_browser(root)
    if shipped is not None:
        return shipped, None
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
        staged = work / 'agent-browser'
        extract_agent_browser(archive, name, staged)
        check = subprocess.run([str(staged), '--version'], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
        if check.returncode != 0 or AGENT_BROWSER_VERSION not in check.stdout:
            raise RuntimeError('The downloaded agent-browser does not run on this machine')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.replace(staged, target)
        return target, None
    finally:
        shutil.rmtree(work, ignore_errors=True)


# Claude Code and Codex that Qorqbot downloaded (from Settings, by app/qorqut/agent_cli_install.py, which owns the pinned URLs,
# checksums and the install itself) live in <folder>/tools/<provider>-<version>/. The launcher does not put them on PATH or name
# them in the service environment (that environment reaches every process Qorqbot starts, including the owner's own CLIs, and would pin a
# version a later release removes): the service finds its copy at each launch (app/qorqut/agent_cli_launch.py). The launcher
# only knows where an installed copy is, for tools and tests; a test keeps these equal to that module's.
AGENT_CLI_VERSIONS = {'claude': '2.1.327', 'codex': '0.170.0'}
AGENT_CLI_EXECUTABLE = {'claude': 'QORQUT_CLAUDE_EXECUTABLE', 'codex': 'QORQUT_CODEX_EXECUTABLE'}
AGENT_CLI_ENTRY = {'claude': 'claude', 'codex': 'bin/codex'}


def agent_cli_path(root, provider):
    return Path(root) / 'tools' / f'{provider}-{AGENT_CLI_VERSIONS[provider]}' / AGENT_CLI_ENTRY[provider]


def chrome_installed(path=None, system=None):
    """Google Chrome, which the agents' browsers run on (Chromium does not count, as in ``qorqut.native_paths``). Qorqbot
    cannot install it; the owner is told when it is missing."""
    if (system or platform.system()) == 'Darwin' and os.access(MAC_CHROME, os.X_OK):
        return True
    return any(shutil.which(name, path=path) for name in ('google-chrome-stable', 'google-chrome'))


def readiness_notes(path, system=None):
    """Optional tools the dashboard works without but agents need. Claude Code and Codex are not listed: Settings in the
    dashboard downloads them and signs them in. Returned as lines to print, never raised."""
    notes = []
    if not chrome_installed(path, system):
        notes.append('Your agents need Google Chrome to work on websites. Get it from ' + CHROME_URL)
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
    chip = {'macos14-arm64': 'arm64', 'macos14-x86_64': 'x86_64'}.get(target)
    if chip and platform.system() == 'Darwin' and platform.machine() == chip:
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
        result = subprocess.run([str(python), '-I', '-B', '-m', 'qorqut.process_info', *map(str, arguments)],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('Unable to run macOS process inspection safely') from error
    if result.returncode == 3 and arguments[0] == 'identity':
        return None
    if result.returncode:
        raise RuntimeError('Unable to verify macOS process ownership: ' + result.stderr.strip())
    return result.stdout.strip()


def listening_terminal_sockets(root):
    """Sockets under ``.run/tmux-*/`` (left by an older version that ran agents in tmux) that something still answers on.
    A socket nobody listens on is a leftover file and is ignored; nothing is signalled or removed here."""
    live = []
    for path in sorted((root / '.run').glob('tmux-*/*')):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(2)
            probe.connect(str(path))
            live.append(path)
        except OSError:
            continue
        finally:
            probe.close()
    return live


MODE_FILE = 'instance/operations/.runtime/managed-agents/runtime.json'
MODE_LIMIT = 4096  # as managed_runtime/mode.py: the file is a few hundred bytes, a larger one is not ours


def read_mode(root):
    """``managed`` or ``terminal``, as the installation's setting says. The launcher runs before the product's own packages can
    be imported, so this reads the file itself with the rule of ``qorqut.managed_runtime.mode`` (schema version 1, a known mode,
    a small file) and answers ``managed`` for anything else."""
    try:
        path = Path(root) / MODE_FILE
        if path.stat().st_size > MODE_LIMIT:
            return 'managed'
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return 'managed'
    if isinstance(value, dict) and value.get('schema_version') == 1 and value.get('mode') == 'terminal':
        return 'terminal'
    return 'managed'


def require_terminal_socket_room(root, run_dir):
    """A Unix socket path must stay under 104 bytes. Only the Terminals mode makes tmux sockets, so only it is checked."""
    if read_mode(root) == 'terminal' and len(os.fsencode(run_dir / f'tmux-{os.getuid()}/qorqut')) >= 104:
        raise RuntimeError('Choose a shorter installation path so local terminal sockets fit (roughly 50 characters or fewer)')


def running_terminal_sessions(root, env=None):
    """Names of this installation's agent terminals that are running, found only by listing sessions on this installation's own
    socket folder (``.run``). Nothing is ended or signalled. Raises when that cannot be told."""
    env = dict(os.environ if env is None else env)
    env['TMUX_TMPDIR'] = str(Path(root) / '.run')
    env.pop('TMUX', None)
    executable = shutil.which('tmux', path=env.get('PATH'))
    if not executable:
        if listening_terminal_sockets(Path(root)):
            raise ValueError('tmux is unavailable and terminal sockets remain; restore tmux and close this installation’s terminals.')
        return []
    found = []
    # A pre-rename installation can still have live native clients on the old socket name.
    for name in ('qorqut', 'bazarlyk'):
        try:
            result = subprocess.run([executable, '-L', name, 'list-sessions', '-F', '#{session_name}'],
                                    env=env, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError('Could not prove that this installation’s agent terminals are stopped') from error
        if result.returncode == 0:
            found.extend(line for line in result.stdout.splitlines() if line.strip())
        elif not any(text in result.stderr.lower() for text in ('no server running', 'no such file or directory')):
            raise ValueError('Could not prove that this installation’s agent terminals are stopped')
    return found


def require_no_terminal_agents(root, env=None):
    """Refuse an update or extension install while agents may still be running in terminals.

    Managed (also no setting file): an older version's tmux server that still answers means agents may live in it, and this
    package cannot move them; the cutover ends them cleanly and writes down what each agent was doing.
    Terminals: the owner chose terminals, so the cutover is the wrong advice; the update waits until this installation's agent
    terminals are closed or the owner switches back to managed sessions. Terminals are only listed, never ended here."""
    if read_mode(root) == 'terminal':
        if running_terminal_sessions(root, env):
            raise ValueError('Agents of this installation run in terminals, and some are running now. Under Settings > Model/Provider, '
                             'switch to Managed sessions, or close the agents’ terminals yourself. Then update again.')
        return
    live = listening_terminal_sockets(root)
    if live:
        raise ValueError('Some agents of this installation still run in the old terminal mode (' + ', '.join(map(str, live)) + '). '
                         'Run the managed cutover first (python -m qorqut.migration.managed_cutover), then update. '
                         'If only your own Terminal view is left there, close it, or end that server with: tmux -S <socket> kill-server')


def stop_managed_hosts(root, env, timeout=30):
    """Close every managed agent session of this installation before its runtime changes. Closed sessions open again by
    themselves at the next input. The work is done by ``qorqut.managed_runtime.shutdown`` of the installed runtime; this
    raises when a host is still there afterwards. Returns that module's report (``{}`` when no runtime environment exists
    yet, and so no agent ever ran)."""
    python = root / 'runtime/venv/bin/python'
    if not python.is_file():
        return {}
    try:
        done = subprocess.run([str(python), '-I', '-B', '-m', 'qorqut.managed_runtime.shutdown',
                               '--instance-root', str(root / 'instance'), '--timeout', str(timeout)],
                              env=env, cwd=root, capture_output=True, text=True, timeout=timeout + 60)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('Could not close this installation’s running agents; close them from the dashboard first') from error
    try:
        report = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        report = None
    if done.returncode != 0 or not isinstance(report, dict) or not report.get('ok'):
        left = ', '.join(report.get('remaining', ())) if isinstance(report, dict) else ''
        raise ValueError('Running agents could not be closed' + (' (' + left + ')' if left else '')
                         + '. Stop Qorqbot, wait a minute and try again.')
    return report


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


def stop_agent_browsers(root, timeout=30, grace=5):
    """Close this installation's detached agent browsers before its runtime changes; the application starts them again.

    Only a browser supervisor is signalled, and only while it is one of this installation's processes with the exact
    identity it recorded for itself. It then closes the one Chrome it launched. No stop request is recorded, and
    anything that remains is left for the caller to refuse."""
    records = sorted((root / 'instance/operations/.runtime/agent-browsers').glob('*/ready.json'))
    if not records:
        return
    owned = set(installation_processes(root))

    def running(pid, recorded):
        current = identity(pid)
        return bool(current) and current['boot'] + ':' + current['start'] == recorded

    signalled = []
    for record in records:
        try:
            saved = read_json(record)
            pid, recorded = saved['supervisor_pid'], saved['supervisor_identity']
            if isinstance(pid, int) and pid in owned and running(pid, recorded):
                os.kill(pid, signal.SIGTERM)
                signalled.append((pid, recorded))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            continue
    if not signalled:
        return
    # Each supervisor exits only after its Chrome has closed; Chrome's own helpers follow within moments.
    deadline = time.monotonic() + timeout
    while signalled and time.monotonic() < deadline:
        try:
            signalled = [item for item in signalled if running(*item)]
        except RuntimeError:
            break
        if signalled:
            time.sleep(.2)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and installation_processes(root):
        time.sleep(.5)


def prepare(root, env):
    runtime = root / 'runtime'
    marker = runtime / 'ENVIRONMENT.json'
    if marker.exists():
        if read_json(marker)['root'] != str(root):
            raise RuntimeError('This installation has moved. Restore its original folder before starting it.')
        return
    if (root / INITIALIZATION_RECEIPT).exists():
        print('Preparing Qorqbot from the bundled files…', flush=True)  # a first start has said so already (main)
    verify(runtime)
    venv = runtime / 'venv'
    if 'venv' in bundled(runtime):
        # The environment came ready-made and was checked against the manifest above; nothing is built on this computer.
        if not (venv / 'bin/python').is_file():
            raise RuntimeError('This package is incomplete: its Python environment is missing. Download Qorqbot again.')
        save_json(marker, {'format': 1, 'root': str(root)})
        return
    if venv.exists():
        raise RuntimeError('An incomplete Python environment remains in runtime/venv. Keep it for diagnosis; setup was not retried.')
    require_prerequisites(env)
    log = setup_log(root)
    run([sys.executable, '-I', '-B', '-m', 'venv', '--copies', venv], env=env, cwd=root, log=log)
    python = venv / 'bin/python'
    # Standard pip resolves the declared library dependencies from the verified
    # local wheelhouse. External programs are never installed by this command.
    run([python, '-I', '-B', '-m', 'pip', 'install', '--no-index', '--no-compile',
         '--find-links', runtime / 'wheelhouse', '--find-links', runtime / 'wheels',
         '--require-hashes', '-r', runtime / 'requirements.lock'], env=env, cwd=root, log=log)
    run([python, '-I', '-B', '-m', 'pip', 'check'], env=env, cwd=root, log=log)
    save_json(marker, {'format': 1, 'root': str(root)})


def initialize(root, env):
    runtime, instance = root / 'runtime', root / 'instance'
    receipt = root / INITIALIZATION_RECEIPT
    if receipt.exists():
        record = read_json(receipt)
        if record.get('state') != 'initialized' or record.get('instance_root') != str(instance):
            raise RuntimeError('The existing instance is incomplete or belongs to a different folder')
        ensure_context_repository(root, env, record)
        return record
    if instance.exists() and any(instance.iterdir()):
        raise RuntimeError('The instance folder is not empty and has no successful initialization receipt; nothing was replaced')
    selection = []
    if shipped_whatsapp(runtime):
        # A new installation selects the bundled WhatsApp connector from the start, so its guidance is imported with
        # everything else (``select_whatsapp`` adopts it for an installation initialized without it).
        initial = root / '.run/initial-extensions.json'
        save_json(initial, {'extensions': [{'factory': WHATSAPP_FACTORY, 'config': {}}]})
        selection = ['--extensions-config', initial]
    run([runtime / 'venv/bin/python', '-I', '-B', '-m', 'qorqut.bootstrap.initialize',
         '--prepared-root', runtime, '--instance-root', instance, *selection], env=env, cwd=root, log=setup_log(root))
    keep_setup_log(root)
    (instance / 'config/operational-timezone').write_text(env['QORQUT_TIMEZONE'] + '\n')
    # A new installation keeps Claude Code and Codex in folders of its own from its first start (``client_homes``).
    (root / OWN_CLIENT_HOMES).write_text('1\n')
    return read_json(receipt)


def ensure_context_repository(root, env, record):
    """An installation initialized before the Context store was provisioned gets it created once; an existing store is never touched."""
    instance = root / 'instance'
    if (instance / 'operations/context-graph').exists():
        return
    command = [root / 'runtime/venv/bin/python', '-I', '-B', '-m', 'qorqut.bootstrap.context_repository', '--instance-root', instance]
    for name, location in record['components'].items():
        command.extend(['--component', f'{name}={location}'])
    run(command, env=env, cwd=root, log=setup_log(root))


TREG_FACTORY = 'qorqut_treg.extension:create'
OPENROUTER_FACTORY = 'qorqut_openrouter.extension:create'
WHATSAPP_FACTORY = 'qorqut_whatsapp.extension:create'
GOOGLE_CALENDAR_FACTORY = 'qorqut_google_calendar.extension:create'
# The extensions this app's own bundle carries, which an update keeps and the release's name rules check against.
BUNDLED_FACTORIES = frozenset({GOOGLE_CALENDAR_FACTORY, TREG_FACTORY, OPENROUTER_FACTORY, WHATSAPP_FACTORY})


def select_bundled(root, name, factory):
    """Select an extension that ships in the runtime, once, when the runtime carries its wheel. An entry the owner already
    has for it, with its own configuration, is left alone."""
    receipt = root / 'runtime/PREPARATION-RECEIPT.json'
    if not receipt.exists() or not (read_json(receipt).get('selection') or {}).get(name):
        return False
    config = root / 'instance/config/extensions.json'
    selected = read_json(config)
    if any(item.get('factory') == factory for item in selected['extensions']):
        return False
    selected['extensions'].append({'factory': factory, 'config': {}})
    save_json(config, selected)
    return True


def select_treg(root):
    """The treg toolkit ships in the runtime and belongs to every installation, new or updated. Selecting it adds its
    tools only; it contacts nothing and stays idle until the owner adds a treg key in the dashboard."""
    return select_bundled(root, 'treg', TREG_FACTORY)


def select_openrouter(root):
    """The OpenRouter extension ships in the runtime and belongs to every installation, new or updated. Selecting it adds
    the OpenRouter card and the Chat microphone only; it contacts nothing and stays idle until the owner connects their own
    OpenRouter account in the dashboard."""
    return select_bundled(root, 'openrouter', OPENROUTER_FACTORY)


def shipped_whatsapp(runtime):
    """The receipt row of the WhatsApp wheel this runtime carries, or None: a runtime with no helper for its platform has
    none, and its installations start without WhatsApp."""
    receipt = runtime / 'PREPARATION-RECEIPT.json'
    if not receipt.exists():
        return None
    data = read_json(receipt)
    if not (data.get('selection') or {}).get('whatsapp'):
        return None
    return next((row for row in data.get('wheels') or () if row.get('name') == 'qorqut-whatsapp'), None)


def select_whatsapp(root, env):
    """The WhatsApp connector ships in the runtime and belongs to every installation, new or updated, once. It stays
    dormant: nothing starts its helper or contacts WhatsApp until the owner asks Ordubashy to set it up.

    A new installation selected it when it was initialized. One initialized without it has no WhatsApp guidance in its
    instructions, so selecting the factory alone would stop it from starting: the offline extension installer adopts
    the guidance, then selects it, as an optional connector bundle would. An existing entry is left alone. A refusal
    changes nothing and Qorqbot starts without WhatsApp; an interrupted adoption is resumed at the next start."""
    runtime, config = root / 'runtime', root / 'instance/config'
    row = shipped_whatsapp(runtime)
    if row is None or any(item.get('factory') == WHATSAPP_FACTORY for item in read_json(config / 'extensions.json')['extensions']):
        return False
    journal = config / 'extension-install.json'
    state = read_json(journal) if journal.exists() else {'phase': 'complete'}
    if state.get('phase') != 'complete' and ((state.get('request') or {}).get('entry') or {}).get('factory') != WHATSAPP_FACTORY:
        return False  # another connector's installation is unfinished; the service start reports it
    command = [runtime / 'venv/bin/python', '-I', '-B', '-m', 'qorqut.bootstrap.extension_install', '--prepared-root', runtime,
               '--instance-root', root / 'instance', '--factory', WHATSAPP_FACTORY, '--bundle-sha256', row['sha256']]
    if state.get('phase') != 'complete':
        command.append('--resume')
    print('Adding WhatsApp to this installation…', flush=True)
    done = subprocess.run(list(map(str, command)), env=env, cwd=root, capture_output=True, text=True)
    if done.returncode == 0:
        return True
    reason = (done.stderr or done.stdout).strip().splitlines()[-1:] or ['no details']
    if journal.exists() and read_json(journal).get('phase') != 'complete':
        raise RuntimeError('Adding WhatsApp was interrupted; start Qorqbot again to finish it. ' + reason[0])
    print('WhatsApp was not added to this installation; Qorqbot starts without it. ' + reason[0], flush=True)
    return False


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


# The service's exit code asking to be started again (qorqut/service/restart.py EXIT_RESTART): sysexits' EX_TEMPFAIL.
RESTART = os.EX_TEMPFAIL


def run_service(argv, *, env, cwd, log, state, url, show):
    """Run the service child until it stops, starting it again whenever it exits with ``RESTART``.

    The caller holds the app lock for the whole run; the record file names this launcher and stays while the child
    restarts. Every restart uses the same arguments and the same readiness wait. Any other exit ends the run as before.
    """
    def start():
        return subprocess.Popen(list(map(str, argv)), env=env, cwd=cwd, stdout=log, stderr=log)
    child = start()
    stop_requested = False
    def stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    save_json(state, {'pid': os.getpid(), 'identity': identity(os.getpid()), 'url': url,
                      'terminal': sys.stdin.isatty()})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        restarted = False
        while True:
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
            if restarted:
                print(f'Qorqbot restarted and is running at {url}', flush=True)
            else:
                print(f'Qorqbot is running at {url}\nKeep this window open while you use Qorqbot. Press Ctrl+C to stop it.',
                      flush=True)
                if sys.stdout.isatty():
                    for note in readiness_notes(env.get('PATH')):
                        print(note, flush=True)
                if show and not open_browser(url):
                    print('Open the address above in your browser.', flush=True)
            code = child.wait()
            if code == RESTART and not stop_requested:
                child, restarted = start(), True
                # A stop that arrived while the old child was being replaced still reaches the new one.
                if stop_requested:
                    child.send_signal(signal.SIGTERM)
                continue
            # Uvicorn can finish graceful shutdown by re-raising the received
            # signal. An owner-requested stop is a successful launcher exit,
            # also when the service had just asked to be started again.
            if stop_requested and code in (0, -signal.SIGTERM, -signal.SIGINT, RESTART):
                return 0
            return code if code >= 0 else 128 - code
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=30)
        state.unlink(missing_ok=True)


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
    if len(os.fsencode(run_dir / 'c/org.chromium.Chromium.XXXXXX/SingletonSocket')) >= 104:
        raise RuntimeError('Choose a shorter installation path so the browser sockets fit (roughly 50 characters or fewer)')
    require_terminal_socket_room(root, run_dir)
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
        require_prerequisites(env, self_contained=bool(bundled(runtime)))
        if not (root / INITIALIZATION_RECEIPT).exists():
            # Set-up output goes to its log (``setup_log``); the owner sees this line, then the address.
            print('Setting up Qorqbot for the first time. This takes about half a minute.', flush=True)
        prepare(root, env)
        record = initialize(root, env)
        select_treg(root)
        select_openrouter(root)
        select_whatsapp(root, env)
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
            factory = GOOGLE_CALENDAR_FACTORY
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
        argv = [runtime / 'venv/bin/python', '-I', '-B', '-m', 'qorqut.service',
                '--workspace', runtime / 'src', '--instance-root', root / 'instance',
                '--extensions-config', root / 'instance/config/extensions.json',
                '--token-file', root / 'instance/config/owner-token', '--host', '127.0.0.1', '--port', str(args.port)]
        for name, location in record['components'].items():
            argv.extend(['--component', f'{name}={location}'])
        logdir = root / 'instance/logs'
        logdir.mkdir(mode=0o700, exist_ok=True)
        with (logdir / 'application.log').open('ab') as log:
            return run_service(argv, env=env, cwd=root, log=log, state=state, url=f'http://127.0.0.1:{args.port}', show=show)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('Qorqbot: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
