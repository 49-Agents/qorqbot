"""Local process observations and process-group control on Linux and macOS; unavailable evidence raises OSError.

Linux keeps its existing kernel start-tick identities. macOS uses the kernel boot
UUID and psutil's creation time in integer microseconds. Neither a process name
nor an open file substitutes for a verified process lifetime or a held lock.

A process started here leads its own session, so stopping it reaches every process it started.
"""
from collections import namedtuple
from functools import lru_cache
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import time
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


def _tty_group(process, pid):
    """``(device, foreground group)`` of the process's controlling terminal, read through the tty device itself."""
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
    return device, foreground


def fields(pid, terminal=False):
    """Existing stat field positions, populated from actual OS observations. On macOS the terminal device and foreground group
    (fields 4 and 5) read 0 and -1 unless ``terminal`` is true: only the terminal backend asks for them, because reading them
    opens the process's tty device, which a managed agent (it has no terminal) never needs."""
    if not darwin():
        return Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
    def observe(process):
        device, foreground = _tty_group(process, pid) if terminal else (0, -1)
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


def boot_id_plain():
    """The kernel boot identity as the file reads it, with the file's own error when it cannot be read."""
    if darwin():
        return _darwin_boot_id()
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def boot_id():
    try:
        return boot_id_plain()
    except OSError as exc:
        raise OSError('Cannot establish the kernel boot identity') from exc


def namespace_id(pid=None):
    # macOS has one local PID namespace; do not pretend a Linux namespace was inspected.
    if darwin():
        return 'darwin:' + boot_id()
    return str(Path(f'/proc/{int(os.getpid() if pid is None else pid)}/ns/pid').readlink())


def namespace_pids():
    """``(namespace, the pid it shows inside) -> the pid this machine shows`` for every process, so a pid read inside
    another namespace can be found here. macOS has one local namespace, so it finds none."""
    if darwin():
        return {}
    found = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            namespace = str((entry / 'ns/pid').readlink())
            for line in (entry / 'status').read_text().splitlines():
                if line.startswith('NSpid:'):
                    found[(namespace, line.split()[-1])] = entry.name
                    break
        except OSError:
            continue
    return found


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


def cmdline_contains(pid, needle):
    """Does this process's command line contain ``needle``, read the way the supervisor did: the raw bytes on Linux, the
    argument list on macOS."""
    if darwin():
        return needle in _read(pid, lambda p: p.cmdline())
    return needle.encode() in Path(f'/proc/{int(pid)}/cmdline').read_bytes()


def cmdline_parts(pid):
    """The command line as the release worker read it: ``/proc`` bytes decoded strictly and split on NUL, empty items kept."""
    if darwin():
        return _read(pid, lambda p: p.cmdline())
    return Path(f'/proc/{int(pid)}/cmdline').read_bytes().decode().split('\0')


def environ_strict(pid):
    """The environment as the release worker read it: ``/proc`` bytes decoded strictly, only the entries with ``=``."""
    if darwin():
        value = _read(pid, lambda p: p.environ())
        if not value and int(pid) != os.getpid():
            raise PermissionError('macOS did not expose the process environment')
        return value
    return dict(v.split('=', 1) for v in Path(f'/proc/{int(pid)}/environ').read_bytes().decode().split('\0') if '=' in v)


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


def user_id():
    """The user this process runs as, exactly as ``os.getuid()`` gives it."""
    return os.getuid()


def effective_user_id():
    """The user this process acts as, exactly as ``os.geteuid()`` gives it."""
    return os.geteuid()


def fields_as_given(pid):
    """The stat fields of the process named the way the old line wrote ``pid`` into the path, before any conversion."""
    if darwin():
        return fields(pid)
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()


def is_zombie_as_given(pid):
    """Whether the process named by ``pid`` as given is a zombie, read as the release worker always read it."""
    if darwin():
        return is_zombie(pid)
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] in {'Z', 'X'}


def open_links(pid):
    """The raw target of every open descriptor of ``pid``, as the migration tool has always read them: no stat, no
    conversion, and a missing descriptor table raises ``OSError``. macOS shows no such table."""
    if darwin():
        return []
    found = []
    for entry in Path(f'/proc/{int(pid)}/fd').iterdir():
        try:
            found.append(os.readlink(entry))
        except OSError:
            continue
    return found


def uids(pid):
    if darwin():
        return tuple(_read(pid, lambda p: p.uids()))
    for line in Path(f'/proc/{int(pid)}/status').read_text().splitlines():
        if line.startswith('Uid:'):
            return tuple(map(int, line.split()[1:4]))
    raise OSError('Cannot establish process ownership')


def parent_pid(pid):
    return _read(pid, lambda p: p.ppid()) if darwin() else int(fields(pid)[1])


def process_group(pid):
    """The process group ``pid`` belongs to, read the way each system shows it: the kernel's own field on Linux, and
    macOS's ``os.getpgid``, which answers for a process whose stat fields are hidden."""
    if darwin():
        return os.getpgid(int(pid))
    return int(fields(pid)[2])


def pids():
    return _psutil().pids() if darwin() else [int(p.name) for p in Path('/proc').iterdir() if p.name.isdecimal()]


def owns_listener(pid, port, address=None):
    """Inspect this exact process, never a system-wide socket guess or a wildcard match.

    The process holds a listening TCP socket on ``port`` (optionally only on ``address``)."""
    if darwin():
        def inspect(process):
            for connection in process.net_connections(kind='inet'):
                if connection.status != _psutil().CONN_LISTEN or not connection.laddr:
                    continue
                host, number = connection.laddr[:2]
                if number == port and (address is None or host == address):
                    return True
            return False
        return _read(pid, inspect)
    wanted = None
    if address is not None:
        wanted = "".join(f"{byte:02X}" for byte in reversed(socket.inet_aton(address)))
    sockets = set()
    for path in Path(f"/proc/{int(pid)}/fd").iterdir():
        try:
            sockets.add(path.readlink().as_posix())
        except FileNotFoundError:
            continue  # Unrelated Chrome descriptors can close while inspecting.
    for protocol in ("tcp", "tcp6"):
        for line in Path(f"/proc/{int(pid)}/net/{protocol}").read_text().splitlines()[1:]:
            columns = line.split()
            host, _, number = columns[1].rpartition(":")
            if (columns[3] == "0A" and int(number, 16) == port and (wanted is None or host == wanted)
                and "socket:[" + columns[9] + "]" in sockets):
                return True
    return False


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


def start_in_background(argv, **options):
    """Start a program in a session of its own, so a later stop reaches everything it starts."""
    options['start_new_session'] = True
    return subprocess.Popen(argv, **options)


class OpenFile(namedtuple('OpenFile', 'name path device inode')):
    """One file a process holds open: its descriptor's name, what it is open on and that file's identity."""


def open_files(pid):
    """The files ``pid`` holds open, or only the ones still readable while it works: a descriptor can close as it is
    read. A process that cannot be read at all raises ``OSError``; macOS shows no descriptor table, so it finds none
    there."""
    if darwin():
        return []
    found = []
    for entry in Path(f'/proc/{int(pid)}/fd').iterdir():
        try:
            path = entry.readlink()
            info = entry.stat()
        except OSError:
            continue
        found.append(OpenFile(entry.name, path, info.st_dev, info.st_ino))
    return found






def process_token():
    """This process's identity as plain text: the computer's name, the process id and its start time, so a token read
    again can tell the same process from a later one that reused the id."""
    if darwin():
        start = _read(os.getpid(), lambda process: str(int(process.create_time() * 1_000_000)))
    else:
        # The stat file is read as bytes and only the start time's field is decoded, so a command name that is
        # not valid UTF-8 does not raise here.
        start = ''
        try:
            with open('/proc/self/stat', 'rb') as handle:
                start = handle.read().rsplit(b')', 1)[1].split()[19].decode()
        except (OSError, IndexError):
            start = ''
    return f'{socket.gethostname()}:{os.getpid()}:{start}'


def token_alive(token):
    """False only when the process a token names is known to be gone: the same computer and no such process. A token
    that cannot be read at all counts as alive, so an unreadable process is never reported as ended."""
    if not token or (not darwin() and not os.path.isdir('/proc/self')):
        return True
    try:
        host, pid_text, start = token.rsplit(':', 2)
        pid = int(pid_text)
    except ValueError:
        return True
    if host != socket.gethostname():
        return True
    if darwin():
        psutil = _psutil()
        try:
            current = str(int(psutil.Process(pid).create_time() * 1_000_000))
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
        return not start or current == start
    try:
        # Bytes, like the old line: a command name that is not valid UTF-8 is not this token's business.
        with open(f'/proc/{int(pid)}/stat', 'rb') as handle:
            current = handle.read().rsplit(b')', 1)[1].split()[19].decode()
    except (OSError, IndexError):
        return False
    return not start or current == start


def _signal_process(process, number, group):
    """Signal ``process``, and with ``group`` the process group it leads; otherwise the process alone."""
    if group:
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, number)
                return
        except OSError:
            return
    try:
        process.terminate() if number != signal.SIGKILL else process.kill()
    except OSError:
        pass


def terminate_process(process, *, group=True):
    """Ask ``process``, and with ``group`` everything it started, to stop.

    Only a group this layer made is signalled as a group: the process must lead it, which a process started by
    ``start_in_background`` does. A process that has already gone is not an error."""
    _signal_process(process, signal.SIGTERM, group)


def kill_process(process, *, group=True):
    """Stop ``process`` by force, and with ``group`` everything it started."""
    _signal_process(process, signal.SIGKILL, group)


def ignore_hangup():
    """Ignore a hang-up in this process, so one started from a terminal is not ended when the terminal closes."""
    signal.signal(signal.SIGHUP, signal.SIG_IGN)


def _signal_group(pid, number):
    """Send ``number`` to the process group led by ``pid``, with the system call's own result and errors."""
    os.killpg(pid, number)


def terminate_pid(pid):
    """Ask exactly this process, and not its group, to stop.

    A process that has already gone raises what the system call raises (``ProcessLookupError``), because the caller
    checked its identity first and decides itself what a process that vanished under it means."""
    os.kill(pid, signal.SIGTERM)


def kill_pid(pid):
    """Stop exactly this process, and not its group, by force; a process that has already gone raises ``ProcessLookupError``."""
    os.kill(pid, signal.SIGKILL)


def terminate_process_group(pid):
    """Ask the process group led by ``pid`` to stop. A group that is already gone raises what the system call
    raises (``ProcessLookupError``), so each caller decides what that case means for it."""
    _signal_group(pid, signal.SIGTERM)


def kill_process_group(pid):
    """Stop the process group led by ``pid`` by force. A group that is already gone raises what the system call
    raises (``ProcessLookupError``), so each caller decides what that case means for it."""
    _signal_group(pid, signal.SIGKILL)








def wait_for_exit_without_reaping(pid):
    """Block until the child ``pid`` has exited but leave it unreaped (a zombie still owns its pid, so the pid and its
    process group number cannot be reused). ``False`` when this system cannot do that here."""
    pid = int(pid)
    if hasattr(os, "waitid"):
        try:
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
            return True
        except OSError:
            return False
    try:                                    # macOS: kqueue reports the exit without reaping (not run on a Mac here)
        import select
        queue = select.kqueue()
        try:
            event = select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT, select.KQ_NOTE_EXIT)
            queue.control([event], 1, None)
            return True
        finally:
            queue.close()
    except (AttributeError, OSError, ValueError):
        return False


def stop_process_group(process, *, grace=2.0, even_when_ended=False):
    """Stop ``process`` and everything it started: SIGTERM to its process group, a bounded wait, then SIGKILL.

    ``process`` is a handle from ``start_in_background``, or a process that made its own group the same way, so its
    process id is also the group id. A group that has already ended is not an error.

    ``even_when_ended`` asks the group to stop although the process itself has already exited, because the caller's
    loop signals the group whether or not its leader is still there: the processes the leader started outlive it.
    """
    if process.poll() is not None and not even_when_ended:
        return
    try:
        _signal_group(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        _signal_group(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _identified_alive(pid, expected_identity):
    """The exact process (boot identity and start time) still exists and is not a zombie."""
    try:
        return identity(pid) == expected_identity and not is_zombie(pid)
    except (OSError, ValueError, IndexError, TypeError):
        return False


def stop_identified_process(pid, expected_identity, *, term_wait=3.0, kill_wait=2.0) -> bool:
    """Stop exactly the process ``pid`` whose boot identity and start time are ``expected_identity``: SIGTERM, then SIGKILL,
    to its process group when it leads one other than ours, otherwise to the process. The identity is checked again before
    every signal, so a reused pid is never touched. Returns True when it was running and is gone, False when it was not (or
    not that process)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or not expected_identity or \
            not _identified_alive(pid, expected_identity):
        return False
    for number, wait in ((signal.SIGTERM, term_wait), (signal.SIGKILL, kill_wait)):
        try:
            if not _identified_alive(pid, expected_identity):
                return True
            group = os.getpgid(pid)
            if group == pid and group != os.getpgrp():
                os.killpg(group, number)
            else:
                os.kill(pid, number)
        except (ProcessLookupError, PermissionError, OSError):
            return not _identified_alive(pid, expected_identity)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _identified_alive(pid, expected_identity):
                return True
            time.sleep(0.05)
    return not _identified_alive(pid, expected_identity)


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