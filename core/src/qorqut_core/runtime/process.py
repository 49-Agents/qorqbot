"""Conservative local /proc observations; no discovery or process effects."""

from pathlib import Path
from uuid import UUID

from ..contracts import utc_now
from ..runner.models import ProcessFingerprint, RunnerError
from ..runner.process import fingerprint
from ..storage.database import assert_external_io_allowed
from .models import ProcessObservation


def observe_process(expected: ProcessFingerprint, *, proc_root: Path = Path("/proc"),
                    clock=utc_now) -> ProcessObservation:
    assert_external_io_allowed()
    state, reason = "unknown", "process_unverified"
    try:
        # Validate the stat shape before using the existing fingerprint helper,
        # whose live-only contract is deliberately less discriminating.
        raw = (proc_root / str(expected.pid) / "stat").read_text()
        end = raw.rindex(")")
        fields = raw[end + 2:].split()
        if (int(raw.split("(", 1)[0]) != expected.pid or int(fields[19]) <= 0
                or raw[end + 1:end + 2] != " "
                or fields[0] not in {"R", "S", "D", "Z", "T", "t", "X", "x", "K", "W", "P", "I"}):
            raise ValueError("Malformed process identity")
        actual = fingerprint(expected.pid, proc_root=proc_root)
    except (RunnerError, OSError, ValueError, IndexError):
        # The existing live-only helper intentionally collapses failure causes.
        # Obtain explicit exit evidence; permission, malformed data, and general
        # I/O failures must never be mistaken for a dead process.
        try:
            boot = UUID((proc_root / "sys/kernel/random/boot_id").read_text().strip())
            if boot != expected.boot_id:
                state, reason = "exited", "boot_changed"
            else:
                try:
                    raw = (proc_root / str(expected.pid) / "stat").read_text()
                except (FileNotFoundError, ProcessLookupError):
                    state, reason = "exited", "process_missing"
                else:
                    end = raw.rindex(")")
                    fields = raw[end + 2:].split()
                    start = int(fields[19])
                    if (int(raw.split("(", 1)[0]) != expected.pid or start <= 0
                            or raw[end + 1:end + 2] != " "
                            or fields[0] not in {"R", "S", "D", "Z", "T", "t", "X", "x", "K", "W", "P", "I"}):
                        raise ValueError("Malformed process identity")
                    if fields[0] in {"Z", "X", "x"}:
                        state, reason = "exited", "process_dead"
                    elif start != expected.start_ticks:
                        state, reason = "exited", "process_start_changed"
        except (OSError, ValueError, IndexError):
            pass
    else:
        if actual == expected:
            state, reason = "live", "process_verified"
        elif actual.boot_id != expected.boot_id:
            state, reason = "exited", "boot_changed"
        elif actual.start_ticks != expected.start_ticks:
            state, reason = "exited", "process_start_changed"
        # A uid/namespace mismatch alone is not conclusive exit evidence.
    return ProcessObservation(state=state, reason=reason, observed_at=clock())
