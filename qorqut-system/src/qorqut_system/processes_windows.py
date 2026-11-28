"""Process observations and control on Windows: a first version, not shipped and not hardened.

A process is started detached, inside a job object that the layer keeps for it, so stopping the job stops every
process it started. Identity is the process's creation time joined with the boot time, as on the other systems.

This first version reads a process's name, program, parent and the process list. Windows keeps a command line, a
current folder, an environment and an owner for every process, but reading them for another process needs more of the
system than this version uses; those operations raise ``UnsupportedSystem`` with a clear message instead of returning
a guess. Nothing here runs in a delivered installation.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from collections import namedtuple
from functools import lru_cache
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import uuid

from .errors import UnsupportedSystem

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
TH32CS_SNAPPROCESS = 0x00000002
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _SYSTEM_TIMEOFDAY_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BootTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("TimeZoneBias", ctypes.c_longlong),
        ("TimeZoneId", wintypes.ULONG),
        ("Reserved", wintypes.ULONG),
        ("BootTimeBias", ctypes.c_ulonglong),
        ("SleepTimeBias", ctypes.c_ulonglong),
    ]


kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME))
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD))
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
kernel32.TerminateJobObject.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
ntdll.NtQuerySystemInformation.restype = ctypes.c_long
ntdll.NtQuerySystemInformation.argtypes = (wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                           ctypes.POINTER(wintypes.ULONG))


CLAUDE_VERSIONED = '/claude/versions/'


def _last_error(message):
    number = ctypes.get_last_error()
    return OSError(number, message + ": " + ctypes.FormatError(number))


def _open_process(pid, access=PROCESS_QUERY_LIMITED_INFORMATION):
    handle = kernel32.OpenProcess(access, False, int(pid))
    if handle:
        return handle
    number = ctypes.get_last_error()
    if number == ERROR_INVALID_PARAMETER:
        raise ProcessLookupError(int(pid))
    if number == ERROR_ACCESS_DENIED:
        raise PermissionError("Cannot inspect process " + str(int(pid)))
    raise OSError(number, ctypes.FormatError(number))


def _process_table():
    """pid -> (parent pid, executable name) for every process the snapshot holds."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot in (INVALID_HANDLE_VALUE, -1):
        raise _last_error("Cannot list the processes of this computer")
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    table = {}
    try:
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return table
    finally:
        kernel32.CloseHandle(snapshot)


def _create_job():
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise _last_error("A job object for the new process cannot be created")
    limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(limits), ctypes.sizeof(limits)):
        error = _last_error("The job object cannot be given its limits")
        kernel32.CloseHandle(job)
        raise error
    return job


def _assign_job(pid):
    job = _create_job()
    access = PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(access, False, int(pid))
    if not handle:
        error = _last_error("The new process cannot be opened for its job object")
        kernel32.CloseHandle(job)
        raise error
    try:
        if not kernel32.AssignProcessToJobObject(job, handle):
            raise _last_error("The new process cannot be placed in its job object")
    finally:
        kernel32.CloseHandle(handle)
    return job


def darwin():
    return False


def fields(pid, terminal=False):
    raise UnsupportedSystem("Windows has no process stat fields like the Linux and macOS versions")


def cmdline(pid):
    raise UnsupportedSystem("This first Windows version does not read another process's command line")


def cwd(pid):
    raise UnsupportedSystem("This first Windows version does not read another process's current folder")


def cmdline_contains(pid, needle):
    """The first Windows version does not read another process's command line."""
    raise UnsupportedSystem("Windows has no command line of another process in this first version")


def cmdline_parts(pid):
    """The first Windows version does not read another process's command line."""
    raise UnsupportedSystem("Windows has no command line of another process in this first version")


def environ_strict(pid):
    """The first Windows version does not read another process's environment."""
    raise UnsupportedSystem("Windows has no environment of another process in this first version")


def environ(pid):
    raise UnsupportedSystem("This first Windows version does not read another process's environment")


def user_id():
    """Windows has no ``os.getuid``; the first version reports no user id."""
    return None


def effective_user_id():
    """Windows has no ``os.geteuid``; the first version reports no user id."""
    return None


def fields_as_given(pid):
    raise UnsupportedSystem("Windows has no process stat fields like the Linux and macOS versions")


def is_zombie_as_given(pid):
    return is_zombie(pid)


def open_links(pid):
    raise FileNotFoundError(2, "Windows has no /proc descriptor table", f'/proc/{int(pid)}/fd')


def uids(pid):
    raise UnsupportedSystem("Windows names a process owner by security identifier, not by user identifiers; "
                            "this first version does not read it")


def namespace_id(pid=None):
    raise UnsupportedSystem("Windows has no process identifier namespace")


def namespace_pids():
    raise UnsupportedSystem("Windows has no process identifier namespace")


def owns_listener(pid, port, address=None):
    raise UnsupportedSystem("This first Windows version does not read the listening sockets of another process")


def owned(root, *, exclude=()):
    raise UnsupportedSystem("This first Windows version does not scan for the processes of a folder")


def start_token(pid):
    handle = _open_process(pid)
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            raise _last_error("Cannot establish the process start time")
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return str(ticks // 10)  # microseconds, as the macOS version reports them
    finally:
        kernel32.CloseHandle(handle)


def is_zombie(pid):
    handle = _open_process(pid)
    kernel32.CloseHandle(handle)
    return False


def pid_identity(pid):
    if is_zombie(pid):
        raise ProcessLookupError(int(pid))
    return f'{int(pid)}:{start_token(pid)}'


@lru_cache(maxsize=1)
def _boot_time():
    information = _SYSTEM_TIMEOFDAY_INFORMATION()
    returned = wintypes.ULONG(0)
    status = ntdll.NtQuerySystemInformation(3, ctypes.byref(information), ctypes.sizeof(information),
                                            ctypes.byref(returned))
    if status != 0:
        raise OSError("Cannot establish the kernel boot identity")
    return information.BootTime


def boot_id_plain():
    """This system's boot identity; the first Windows version has one form of it."""
    return boot_id()


def boot_id():
    return str(_boot_time())


@lru_cache(maxsize=1)
def _windows_machine_id():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
        return str(uuid.UUID(value))
    except (OSError, ValueError) as exc:
        raise OSError("Cannot establish the host platform identity") from exc


def machine_id():
    return _windows_machine_id()


def identity(pid):
    return boot_id() + ':' + start_token(pid)


def name(pid):
    try:
        return _process_table()[int(pid)][1]
    except KeyError:
        raise ProcessLookupError(int(pid)) from None


def exe(pid):
    handle = _open_process(pid)
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(capacity)):
            raise _last_error("Cannot inspect the program of the process")
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def parent_pid(pid):
    try:
        return _process_table()[int(pid)][0]
    except KeyError:
        raise ProcessLookupError(int(pid)) from None


def pids():
    return list(_process_table())


def process_group(pid):
    """A process group in the sense a Unix signal reaches does not exist on Windows; the job object is what a stop
    reaches, so this first version says so instead of guessing."""
    raise UnsupportedSystem("This first Windows version cannot read the process group of a process")


def start_in_background(argv, **options):
    """Start a program detached inside a job object, so stopping the job stops everything it starts.

    The job ends every process it holds when it is closed, so the handle travels with the process object.
    """
    options.pop('start_new_session', None)  # the job object carries the detachment on Windows
    options['creationflags'] = options.get('creationflags', 0) | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    process = subprocess.Popen(argv, **options)
    try:
        process._qorqut_job = _assign_job(process.pid)
    except BaseException:
        process.kill()
        process.wait()
        raise
    return process


class OpenFile(namedtuple('OpenFile', 'name path device inode')):
    """One file a process holds open: its handle's name, what it is open on and that file's identity."""


def open_files(pid):
    raise UnsupportedSystem("This first Windows version does not list the files another process holds open")






def process_token():
    """This process's identity as plain text: the computer's name, the process id and its start time."""
    try:
        start = start_token(os.getpid())
    except OSError:
        start = ''
    return f'{socket.gethostname()}:{os.getpid()}:{start}'


def token_alive(token):
    """False only when the process a token names is known to be gone on this computer; anything unreadable counts as
    alive."""
    if not token:
        return True
    try:
        host, pid_text, start = token.rsplit(':', 2)
        pid = int(pid_text)
    except ValueError:
        return True
    if host != socket.gethostname():
        return True
    try:
        current = start_token(pid)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return not start or current == start


def terminate_process(process, *, group=True):
    """Ask ``process`` to stop. Windows has no gentle signal, so a client is expected to be asked through its own
    channel first; this version can only end the process and its job."""
    if process.poll() is not None:
        return
    job = getattr(process, '_qorqut_job', None)
    if job is not None:
        stop_process_group(process)
        return
    try:
        process.terminate()
    except OSError:
        pass


def kill_process(process, *, group=True):
    """Stop ``process`` and, with ``group``, everything the job object holds."""
    if process.poll() is not None:
        return
    if getattr(process, '_qorqut_job', None) is not None:
        stop_process_group(process)
        return
    try:
        process.kill()
    except OSError:
        pass


def terminate_process_group(pid):
    """End the process ``pid`` stands for. Windows has no gentle signal and no group signal, so this first version
    ends that one process; a process this layer started is stopped through its job object instead."""
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        pass


def kill_process_group(pid):
    """End the process ``pid`` stands for; see ``terminate_process_group`` for what this first version can reach."""
    terminate_process_group(pid)


def terminate_pid(pid):
    """Windows has no gentle signal to send to one process, so this first version refuses instead of guessing."""
    raise UnsupportedSystem("Windows has no gentle signal for one process")


def kill_pid(pid):
    """End exactly this process. Windows has no signal of its own here, so this first version ends it."""
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        pass


def wait_for_exit_without_reaping(pid):
    """This first Windows version cannot watch a child's exit without reaping it, so the caller waits as usual."""
    return False


def _identified_alive(pid, expected_identity):
    """The exact process (boot identity and start time) still exists and is not a zombie."""
    try:
        return identity(pid) == expected_identity and not is_zombie(pid)
    except (OSError, ValueError, IndexError, TypeError):
        return False


def stop_identified_process(pid, expected_identity, *, term_wait=3.0, kill_wait=2.0) -> bool:
    """Stop exactly the process ``pid`` whose identity is ``expected_identity``; a first version, not hardened.

    Windows has no gentle signal, so ``terminate_process_group`` ends that one process; the waits only watch the
    identity. The identity is checked before the call, so a reused process id is never touched. Returns True when it
    was running and is gone, False when it was not (or not that process)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or not expected_identity or \
            not _identified_alive(pid, expected_identity):
        return False
    terminate_process_group(pid)
    deadline = time.monotonic() + term_wait
    while time.monotonic() < deadline:
        if not _identified_alive(pid, expected_identity):
            return True
        time.sleep(0.05)
    return not _identified_alive(pid, expected_identity)


def ignore_hangup():
    raise UnsupportedSystem("Windows has no hang-up signal to ignore")


def stop_process_group(process, *, grace=2.0, even_when_ended=False):
    """Stop a process started by this layer and every process in its job.

    Windows has no gentle signal, so a client is expected to be asked to close through its own channel first. The job
    then ends every process it holds at once, and nothing it started survives it.

    ``even_when_ended`` asks the job to be ended although the process itself has already exited, for the same reason
    as on Unix: a caller's loop signals the group whether or not its leader is still there.
    """
    if process.poll() is not None and not even_when_ended:
        return
    job = getattr(process, '_qorqut_job', None)
    if job is None:
        raise UnsupportedSystem("Windows can stop only a process that this layer started in a job object")
    try:
        if not kernel32.TerminateJobObject(job, 1):
            raise _last_error("The job object of the process cannot be ended")
        try:
            process.wait(timeout=grace)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        process._qorqut_job = None
        kernel32.CloseHandle(job)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    one = sub.add_parser('identity'); one.add_argument('pid', type=int)
    group = sub.add_parser('owned')
    group.add_argument('root')
    group.add_argument('--exclude-pid', type=int, action='append', default=[])
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
    except (OSError, ValueError, IndexError, UnsupportedSystem) as exc:
        print('Process inspection unavailable: ' + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())