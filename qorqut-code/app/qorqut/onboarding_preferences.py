"""Installation-local walkthrough progress, separate from operational records."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from qorqut_operations.contracts import DomainError

# Saved step ids, in walkthrough order. Removing an id makes saved progress unreadable, so ids are only added.
STEPS = ("assistant", "terminal", "start", "chat", "focus", "attention", "activity", "calendar", "journal",
         "projects", "signoffs", "learning", "context", "settings", "privacy", "connectors", "connect-more")
STATUSES = {"new", "in_progress", "skipped", "completed"}


def progress(runtime_directory, owner_id, change=None):
    default = {"version": 1, "status": "new", "step": STEPS[0]}
    directory = Path(runtime_directory) / "dashboard-preferences"
    path = directory / (hashlib.sha256(owner_id.encode()).hexdigest() + "-onboarding.json")
    if change is None:
        if not path.exists():
            return default
        try:
            saved = json.loads(path.read_text())
            if (saved.get("version") == 1 and saved.get("status") in STATUSES
                    and saved.get("step") in STEPS):
                return saved
        except (ValueError, AttributeError, TypeError):
            pass
        raise DomainError("configuration_unavailable", "Walkthrough progress could not be read")
    if (not isinstance(change, dict) or set(change) != {"status", "step"}
            or not isinstance(change["status"], str) or change["status"] not in STATUSES
            or not isinstance(change["step"], str) or change["step"] not in STEPS):
        raise DomainError("validation_error", "Supply a valid walkthrough status and step")
    saved = {"version": 1, **change}
    directory.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".onboarding-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(saved, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return saved
