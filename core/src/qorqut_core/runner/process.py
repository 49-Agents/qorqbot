"""Linux start identities; inspection never searches by command or process name."""

from pathlib import Path
from uuid import UUID

from .models import ProcessFingerprint, RunnerError


def fingerprint(pid: int, *, proc_root: Path = Path("/proc")) -> ProcessFingerprint:
    if type(pid) is not int or pid <= 0:
        raise RunnerError("invalid_argument", "pid must be positive")
    try:
        root = proc_root / str(pid)
        # comm can itself contain whitespace and ')'. Field 22 is starttime.
        raw = (root / "stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()
        if int(raw.split("(", 1)[0]) != pid or fields[0] in ("Z", "X", "x"):
            raise ValueError("process is no longer live")
        observed = ProcessFingerprint(
            boot_id=UUID((proc_root / "sys/kernel/random/boot_id").read_text().strip()),
            pid=pid, start_ticks=int(fields[19]), uid=root.stat().st_uid,
            pid_namespace=(root / "ns/pid").stat().st_ino,
        )
        # Refuse a process slot that changed while its other facts were read.
        again = (root / "stat").read_text()
        other = again[again.rindex(")") + 2:].split()
        if other[19] != fields[19] or other[0] in ("Z", "X", "x"):
            raise ValueError("process changed during inspection")
        return observed
    except (OSError, ValueError, IndexError) as exc:
        raise RunnerError("target_changed", "cannot verify the exact live process") from exc


def verify_process(expected: ProcessFingerprint, *, proc_root: Path = Path("/proc")) -> None:
    if fingerprint(expected.pid, proc_root=proc_root) != expected:
        raise RunnerError("target_changed", "process start fingerprint changed")
