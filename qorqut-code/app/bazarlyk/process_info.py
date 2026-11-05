"""Local process observations on Linux and macOS; unavailable evidence raises OSError.

Linux keeps its existing kernel start-tick identities. macOS uses the kernel boot
UUID and psutil's creation time in integer microseconds. Neither a process name
nor an open file substitutes for a verified process lifetime or a held lock.
"""
from functools import lru_cache
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import uuid


def darwin():
    return sys.platform == 'darwin'


def _psutil():
    import psutil
    return psutil


def _terminal(process):
    """The process's terminal device path. psutil maps devices to paths once and caches that map, while macOS creates
    a /dev/ttysNNN for each new pseudo-terminal; a terminal opened after the first lookup (a new agent pane) would read
    as none. A missing terminal therefore rebuilds the map once before it is reported as none."""
    tty = process.terminal()
    if tty is None:
        try:
            from psutil import _psposix
            _psposix.get_terminal_map.cache_clear()
        except (ImportError, AttributeError):
            return None
        tty = process.terminal()
    return tty


def _read(pid, operation):
    psutil = _psutil()
    try:
        process = psutil.Process(int(pid))
        born = process.create_time()
        result = operation(process)
        if psutil.Process(int(pid)).create_time() != born:
            raise ProcessLookupError('Process identity changed during inspection')
        return result
    except (psutil.NoSuchProcess, psutil.ZombieProcess) as exc:
        raise ProcessLookupError(int(pid)) from exc
    except psutil.AccessDenied as exc:
        raise PermissionError('Cannot inspect process ' + str(pid)) from exc


def fields(pid):
    """Existing stat field positions, populated from actual OS observations."""
    if not darwin():
        return Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
    def observe(process):
        tty = _terminal(process)
        foreground, device = -1, 0
        if tty:
            descriptor = os.open(tty, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
            try:
                try:
                    foreground = os.tcgetpgrp(descriptor)
                except OSError:
                    # A different terminal session can reject tcgetpgrp. Darwin ps
                    # reads the kernel's foreground group without acquiring that tty.
                    try:
                        output = subprocess.check_output(['/bin/ps', '-o', 'tpgid=', '-p', str(int(pid))], text=True, timeout=2)
                        foreground = int(output.strip())
                    except (subprocess.SubprocessError, ValueError) as exc:
                        raise OSError('Cannot inspect terminal foreground process group') from exc
                device = os.fstat(descriptor).st_rdev
            finally:
                os.close(descriptor)
        value = ['0'] * 20
        value[:6] = ['Z' if process.status() in {_psutil().STATUS_ZOMBIE, _psutil().STATUS_DEAD} else 'S',
                     str(process.ppid()), str(os.getpgid(int(pid))), str(os.getsid(int(pid))),
                     str(device), str(foreground)]
        value[19] = str(round(process.create_time() * 1_000_000))
        return value
    return _read(pid, observe)


def start_token(pid):
    if not darwin():
        return fields(pid)[19]
    return _read(pid, lambda p: str(round(p.create_time() * 1_000_000)))


def is_zombie(pid):
    if not darwin():
        return fields(pid)[0] in {'Z', 'X'}
    return _read(pid, lambda p: p.status() in {_psutil().STATUS_ZOMBIE, _psutil().STATUS_DEAD})


def pid_identity(pid):
    if is_zombie(pid):
        raise ProcessLookupError(int(pid))
    return f'{int(pid)}:{start_token(pid)}'


@lru_cache(maxsize=1)
def _darwin_boot_id():
    try:
        value = subprocess.check_output(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], text=True, timeout=3).strip()
        return str(uuid.UUID(value))
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise OSError('Cannot establish the kernel boot identity') from exc


def boot_id():
    if darwin():
        return _darwin_boot_id()
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError as exc:
        raise OSError('Cannot establish the kernel boot identity') from exc


def namespace_id():
    # macOS has one local PID namespace; do not pretend a Linux namespace was inspected.
    return 'darwin:' + boot_id() if darwin() else str(Path('/proc/self/ns/pid').readlink())


@lru_cache(maxsize=1)
def _darwin_machine_id():
    try:
        raw = subprocess.check_output(['/usr/sbin/ioreg', '-a', '-rd1', '-c', 'IOPlatformExpertDevice'], timeout=3)
        return str(uuid.UUID(plistlib.loads(raw)[0]['IOPlatformUUID']))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError, plistlib.InvalidFileException) as exc:
        raise OSError('Cannot establish the host platform identity') from exc


def machine_id():
    return _darwin_machine_id() if darwin() else Path('/etc/machine-id').read_text().strip()


def identity(pid):
    return boot_id() + ':' + start_token(pid)


def cmdline(pid):
    if darwin():
        return _read(pid, lambda p: p.cmdline())
    return [arg.decode('utf-8', 'replace') for arg in Path(f'/proc/{int(pid)}/cmdline').read_bytes().split(b'\0') if arg]


# Claude Code's native installer runs the client from .../claude/versions/<version>, so macOS names the process after
# the version ("2.1.317"); Linux reports the title the client sets ("claude"). Report the client name on both.
CLAUDE_VERSIONED = '/claude/versions/'


def _darwin_name(process):
    value = process.name()
    try:
        executable = process.exe()
    except _psutil().AccessDenied:
        return value
    return 'claude' if CLAUDE_VERSIONED in executable and Path(executable).name == value else value


def name(pid):
    return _read(pid, _darwin_name) if darwin() else Path(f'/proc/{int(pid)}/comm').read_text().strip()


def exe(pid):
    value = _read(pid, lambda p: p.exe()) if darwin() else str(Path(f'/proc/{int(pid)}/exe').readlink())
    return Path(value.removesuffix(' (deleted)'))


def cwd(pid):
    return Path(_read(pid, lambda p: p.cwd())) if darwin() else Path(f'/proc/{int(pid)}/cwd').readlink()


def environ(pid):
    if darwin():
        value = _read(pid, lambda p: p.environ())
        # macOS can hide restricted processes' environments as {}, without an
        # AccessDenied error. An empty result cannot prove a marker is absent.
        if not value and int(pid) != os.getpid():
            raise PermissionError('macOS did not expose the process environment')
        return value
    return dict(part.decode('utf-8', 'replace').split('=', 1) for part in
                Path(f'/proc/{int(pid)}/environ').read_bytes().split(b'\0') if b'=' in part)


def uids(pid):
    if darwin():
        return tuple(_read(pid, lambda p: p.uids()))
    for line in Path(f'/proc/{int(pid)}/status').read_text().splitlines():
        if line.startswith('Uid:'):
            return tuple(map(int, line.split()[1:4]))
    raise OSError('Cannot establish process ownership')


def parent_pid(pid):
    return _read(pid, lambda p: p.ppid()) if darwin() else int(fields(pid)[1])


def pids():
    return _psutil().pids() if darwin() else [int(p.name) for p in Path('/proc').iterdir() if p.name.isdecimal()]


def owns_listener(pid, port, address=None):
    """Inspect this exact process, never a system-wide socket guess or a wildcard match."""
    if not darwin():
        from bazarlyk.browser.control import owns_listener as linux_listener
        return linux_listener(pid, port, address)
    def inspect(process):
        for connection in process.net_connections(kind='inet'):
            if connection.status != _psutil().CONN_LISTEN or not connection.laddr:
                continue
            host, number = connection.laddr[:2]
            if number == port and (address is None or host == address):
                return True
        return False
    return _read(pid, inspect)


def _darwin_signals(pid, root):
    """Direct ownership evidence for one same-user process (the Linux rule).

    macOS hides the environment (and sometimes the executable) of Apple platform
    binaries such as Finder, Dock or /bin/sleep, even for the same user. Each
    unreadable signal is treated as absent, matching the Linux scan, which skips
    unreadable /proc entries. User-installed tools that Qorqbot launches (venv
    Python, Homebrew tmux, Node clients) expose both signals. The working
    directory is deliberately not a signal: an owner's Terminal or the app
    launcher sitting in the folder must not block updates.
    """
    try:
        if exe(pid).is_relative_to(root):
            return True
    except PermissionError:
        pass
    try:
        return environ(pid).get('QORQUT_FOLDER_ROOT') == str(root)
    except PermissionError:
        return False


def owned(root, *, exclude=()):
    """Folder executable/marker owners.

    Linux: executable under the folder or the folder marker in the environment.
    macOS: the same signals, plus an owned ancestor, because platform binaries
    started by an owned process hide their environment.
    """
    root = Path(root).resolve()
    excluded = {os.getpid(), *map(int, exclude)}
    candidates = {}
    for pid in pids():
        if pid <= 0 or pid in excluded:
            continue
        try:
            if os.geteuid() not in uids(pid) or is_zombie(pid):
                continue
            before = identity(pid)
            if darwin():
                selected = _darwin_signals(pid, root)
                parent = parent_pid(pid)
            else:
                selected = exe(pid).is_relative_to(root) or environ(pid).get('QORQUT_FOLDER_ROOT') == str(root)
                parent = None
            if identity(pid) != before:
                raise OSError('Process identity changed during ownership inspection')
            candidates[pid] = {'identity': before, 'selected': selected, 'parent': parent}
        except (ProcessLookupError, FileNotFoundError):
            continue
        except PermissionError:
            if not darwin():
                raise
            continue
    found = []
    for pid, item in candidates.items():
        selected, seen, parent = item['selected'], {pid}, item['parent']
        while not selected and parent in candidates and parent not in seen:
            seen.add(parent)
            selected = candidates[parent]['selected']
            parent = candidates[parent]['parent']
        if selected:
            found.append({'pid': pid, 'identity': item['identity']})
    return found


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    one = sub.add_parser('identity'); one.add_argument('pid', type=int)
    group = sub.add_parser('owned'); group.add_argument('root'); group.add_argument('--exclude-pid', type=int, action='append', default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == 'identity':
            if is_zombie(args.pid):
                return 3
            print(identity(args.pid))
        else:
            print(json.dumps(owned(args.root, exclude=args.exclude_pid)))
        return 0
    except (ProcessLookupError, FileNotFoundError):
        return 3 if args.command == 'identity' else 1
    except (OSError, ValueError, IndexError) as exc:
        print('Process inspection unavailable: ' + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
