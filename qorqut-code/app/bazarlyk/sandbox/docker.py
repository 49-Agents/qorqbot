"""Trusted Docker CLI boundary; workload requests never supply its arguments."""

from dataclasses import dataclass
import json
import re
import subprocess

from qorqut_operations.contracts import DomainError
from .config import business_identity


@dataclass(frozen=True)
class Ownership:
    installation: str
    business_id: str

    def __post_init__(self):
        business_identity(self.business_id)
        if not re.fullmatch(r"[a-f0-9]{20}", self.installation):
            raise DomainError("invalid_configuration", "Invalid sandbox installation label")

    @property
    def labels(self):
        return {"qorqut.managed": "business-sandbox", "qorqut.installation": self.installation,
                "qorqut.business": self.business_id}

    def require(self, labels):
        if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in self.labels.items()):
            raise DomainError("environment_conflict", "Docker resource belongs to another environment")


class Docker:
    """A host administration port with no raw stderr in public exceptions.

    The executable prefix is installation configuration (e.g. `sudo -n docker`),
    never agent input. Socket credentials stay on the trusted host. A production
    daemon failure is never interpreted as an absent resource or a successful stop.
    """

    def __init__(self, *, command=("docker",), runner=subprocess.run):
        if not command or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command):
            raise ValueError("Invalid trusted Docker executable")
        self.command, self.runner = tuple(command), runner

    def call(self, *arguments, input=None, timeout=90):
        if not 0 < timeout <= 600:
            raise ValueError("Docker commands need a bounded timeout")
        try:
            result = self.runner((*self.command, *arguments), input=input, text=True,
                                 capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            raise DomainError("environment_unavailable", "Docker operation could not be completed", retryable=True) from None
        if result.returncode:
            raise DomainError("environment_unavailable", "Docker operation could not be completed", retryable=True)
        return result.stdout.strip()

    def inspect(self, kind, identity, *, timeout=30):
        if not 0 < timeout <= 600:
            raise ValueError("Docker inventory needs a bounded timeout")
        if kind not in {"container", "network", "image"}:
            raise ValueError("Unsupported Docker resource kind")
        # Identity is generated from registered state, not a command option.
        if (not isinstance(identity, str) or not identity or identity.startswith("-")
                or any(c.isspace() or ord(c) < 32 for c in identity)):
            raise DomainError("invalid_configuration", "Invalid Docker resource identity")
        try:
            result = self.runner((*self.command, kind, "inspect", identity), text=True,
                                 capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            raise DomainError("environment_unavailable", "Docker inventory is unavailable", retryable=True) from None
        if result.returncode:
            # A precise CLI not-found response is distinct from daemon/socket,
            # permissions, timeout and malformed response failures.
            missing = {f"Error: No such {kind}: {identity}", f"Error response from daemon: No such {kind}: {identity}",
                       f"Error: No such object: {identity}", f"Error response from daemon: No such image: {identity}"}
            if kind == "network":
                missing.add(f"Error response from daemon: network {identity} not found")
            if result.stderr.strip() in missing:
                return None
            raise DomainError("environment_unavailable", "Docker inventory is unavailable", retryable=True)
        try:
            values = json.loads(result.stdout)
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
                raise ValueError
            return values[0]
        except (TypeError, ValueError):
            raise DomainError("environment_unavailable", "Docker inventory returned an invalid record") from None

    def owned(self, kind, identity, ownership: Ownership, *, timeout=30):
        result = self.inspect(kind, identity, timeout=timeout)
        if result is not None:
            labels = result.get("Config", {}).get("Labels") if kind == "container" else result.get("Labels")
            ownership.require(labels)
        return result

    def exec(self, container_id, ownership: Ownership, *arguments, input=None, timeout=90):
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise DomainError("invalid_configuration", "Execution requires the exact observed container ID")
        import time
        deadline = time.monotonic() + timeout
        current = self.owned("container", container_id, ownership, timeout=min(timeout, 30))
        if current is None or not current.get("State", {}).get("Running"):
            raise DomainError("environment_unavailable", "Business environment is not running")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DomainError('environment_unavailable', 'Execution budget expired', retryable=True)
        return self.call("exec", "-i", "--user", "1000:1000", container_id, *arguments,
                         input=input, timeout=remaining)


def trusted_host_docker() -> Docker:
    """Return the reviewed host Docker administration command.

    This command is installation configuration, never business input. Docker
    credentials and its socket remain on the trusted host.
    """
    return Docker(command=("sudo", "-n", "docker"))
