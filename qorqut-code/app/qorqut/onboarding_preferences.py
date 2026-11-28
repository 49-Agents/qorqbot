"""Installation-local walkthrough progress, separate from operational records."""
import hashlib
import json
from pathlib import Path

from qorqut_operations.contracts import DomainError
from qorqut_system import locks, private_files

# Saved step ids, in walkthrough order. Removing an id makes saved progress unreadable, so ids are not removed. "terminal" and
# "start" belong to steps the walkthrough no longer has; they stay so progress saved earlier still loads (the dashboard resumes
# such progress at the next existing step), and nothing new is added for them.
STEPS = ("assistant", "terminal", "start", "focus", "attention", "activity", "calendar", "journal",
         "projects", "signoffs", "learning", "context", "settings", "privacy", "connect-overview", "browser-setup", "chat",
         "connectors", "connect-more")
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
        locks.acquire(lock)
        private_files.replace_entry(directory, path.name, json.dumps(saved).encode(), prefix=".onboarding-", chmod=False)
        private_files.sync_folder(directory)
    return saved
