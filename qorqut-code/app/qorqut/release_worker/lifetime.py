"""Shared launch/build/switch/prune exclusion and PID/start lifetime references."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import socket
import sys


def atomic_json(path, value):
    path = Path(path)
    missing=[];directory=path.parent
    while not directory.exists():
        missing.append(directory);directory=directory.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Persist both the renamed file and any newly created directory entries.
        for directory in dict.fromkeys([path.parent,*[item.parent for item in missing]]):
            descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def exclusion(root, *, shared=False, blocking=True):
    path = Path(root) / "state/deployment.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(stream, mode if blocking else mode | fcntl.LOCK_NB)
        yield


def identity(pid):
    try:
        if sys.platform == 'darwin':
            from qorqut import process_info
            return None if process_info.is_zombie(pid) else process_info.start_token(pid)
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def exited_process(pid):
    try:
        if sys.platform == 'darwin':
            from qorqut import process_info
            return process_info.is_zombie(pid)
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0] in {'Z','X'}
    except (FileNotFoundError, ProcessLookupError):return True
    except (OSError,IndexError):return False


def local_instance():
    """Target identity, boot and PID namespace are separate observations."""
    if sys.platform == 'darwin':
        from qorqut import process_info
        return {'host': socket.gethostname(), 'target': 'local',
                'target_instance': process_info.machine_id(), 'boot_id': process_info.boot_id(),
                'pid_namespace': process_info.namespace_id()}
    return {'host': socket.gethostname(), 'target': 'local',
            'target_instance': Path('/etc/machine-id').read_text().strip(),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'pid_namespace': str(Path('/proc/self/ns/pid').readlink())}


@contextmanager
def reference_lock(root):
    """Serialize reference union/attestation; shared deployment locks do not."""
    path = Path(root) / 'state/reference-records.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def process_state(value):
    """Never compare a foreign or unknown process lifetime to local /proc."""
    current = local_instance()
    if value.get('host', current['host']) != current['host']:
        return 'unknown', 'foreign_execution_target'
    if value.get('target', 'local') != 'local':
        return 'unknown', 'target_inspection_required'
    if not value.get('target_instance') or not value.get('boot_id') or not value.get('pid_namespace'):
        return 'unknown', 'legacy_target_identity_unproved'
    if value['target_instance'] != current['target_instance']:
        return 'unknown', 'target_instance_changed'
    if value['boot_id'] != current['boot_id']:
        return 'exited', 'verified_host_reboot'
    if value['pid_namespace'] != current['pid_namespace']:
        return 'unknown', 'pid_namespace_changed'
    observed = identity(value['pid'])
    if observed == value['started']:
        return 'alive', 'exact_process_lifetime'
    if observed is not None or exited_process(value['pid']):
        return 'exited', 'verified_process_exit_or_pid_reuse'
    return 'unknown', 'unreadable_process_identity'


def retire_process_pin(root, path):
    """Retire one proven-exited ordinary pin during background maintenance.

    Never wait behind a launch/release, follow a symlink, or retire a cached
    client's references. Read and classify again under both writer exclusions.
    The caller scans the directory without holding either lock.
    """
    root, path = Path(root), Path(path)
    directory = root / 'state/process-pins'
    if path.parent != directory or path.suffix != '.json':
        return False
    try:
        with exclusion(root, blocking=False), reference_lock(root):
            if any(parent.is_symlink() for parent in (path, *path.parents)) or path.stat().st_size > 1024 * 1024:
                return False
            value = json.loads(path.read_text())
            # Stable launchers predate the optional cached_client flag. Their
            # ordinary pins still carry full target/lifetime identity.
            if (not isinstance(value, dict) or value.get('cached_client', False) is not False
                    or type(value.get('pid')) is not int or value['pid'] <= 0
                    or not isinstance(value.get('started'), str) or not value['started']
                    or not isinstance(value.get('references'), list)
                    or not all(isinstance(ref, str) for ref in value['references'])):
                return False
            if process_state(value)[0] != 'exited':
                return False
            path.unlink()
            return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def pin(root, references, *, pid=None, cached=False):
    pid = pid or os.getpid()
    started = identity(pid)
    if started is None:
        raise RuntimeError("Cannot pin an unverified process")
    path = Path(root) / "state/process-pins" / f"{socket.gethostname()}-{pid}-{started}{'-cached' if cached else ''}.json"
    with reference_lock(root):
        existing = json.loads(path.read_text()) if path.exists() else {}
        atomic_json(path, {**local_instance(), "pid": pid, "started": started,
                           "references": sorted(set(map(str, references)) | set(existing.get('references', []))),
                           "cached_client": cached})
    return path
