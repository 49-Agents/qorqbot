#!/usr/bin/env python3
"""Consumer launcher, copied to runtime/launch.py by native_build.py.

Uses system Python and standard venv/pip with the release wheelhouse.
No installer, daemon, permission broker or host configuration is installed.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import shutil
import socket
import subprocess
import sys
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


def require_prerequisites(env):
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('This download requires Python 3.12 with venv support')
    if not shutil.which('git', path=env.get('PATH')):
        raise RuntimeError('Git is required. Install git using your system package manager, then retry.')
    try:
        import ensurepip
    except ImportError:
        raise RuntimeError('Python venv support is required. Install Python 3.12 with venv support, then retry.') from None


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
    parser.add_argument('command', nargs='?', choices=('start', 'stop', 'status', 'verify', 'setup', 'calendar-connect'), default='start')
    parser.add_argument('--port', type=int, default=1049)
    args, remaining = parser.parse_known_args(argv)
    if remaining and args.command != 'calendar-connect':
        parser.error('Unrecognized arguments: ' + ' '.join(remaining))
    root = Path(__file__).resolve().parent.parent
    runtime = root / 'runtime'
    require_platform(runtime)
    os.umask(0o077)
    run_dir = root / '.run'
    run_dir.mkdir(mode=0o700, exist_ok=True)
    if max(len(os.fsencode(run_dir / f'tmux-{os.getuid()}/bazarlyk')),
           len(os.fsencode(run_dir / 'c/org.chromium.Chromium.XXXXXX/SingletonSocket'))) >= 104:
        raise RuntimeError('Choose a shorter installation path so local terminal sockets fit (roughly 50 characters or fewer)')
    state = run_dir / 'app.json'
    existing = active(state)
    if args.command == 'status':
        print('Running at ' + existing['url'] if existing else 'Stopped')
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
            probe.bind(('127.0.0.1', args.port))
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
            save_json(state, {'pid': os.getpid(), 'identity': identity(os.getpid()), 'url': url})
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
