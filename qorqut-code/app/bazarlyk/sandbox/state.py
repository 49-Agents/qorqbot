"""Small SQLite store for BusinessSandbox lifecycle reconciliation.

Every method accepts a caller-owned writable :class:`~bazarlyk.storage.UnitOfWork`.
This lets business registration, default-agent creation, and ``insert_intent`` commit
atomically, while keeping SQLite transactions strictly outside Docker, network, and
other external effects.

``lease_fence`` prevents an expired reconciler from publishing an observation after a
new reconciler claims the row.  It does *not* stop an expired process from changing
Docker.  The trusted reconciler must also hold a host-local, per-business exclusive
lock through external effects and inspect its labeled resources before creating or
replacing them.  Desired configuration, credentials, and secret references belong in
that trusted host-side reconciler, never in this table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from qorqut_operations.contracts import DomainError, utc_now


ERROR_CODES = frozenset({
    "configuration_missing",
    "environment_conflict",
    "environment_unavailable",
    "environment_unhealthy",
    "provisioning_failed",
})

_MAX_LEASE_SECONDS = 600


def environment_status(uow, business_id):
    """Business-visible state, excluding host paths, credentials and Docker IDs."""
    row = uow.repository("business_sandboxes").get(business_id)
    if row is None:
        return {"mode": "host"}
    previews = [{"port": saved["workload_port"], "url": saved["origin"]} for saved in uow.connection.execute(
        "SELECT workload_port, origin FROM business_previews WHERE business_id=? AND active=1 ORDER BY public_port",
        (business_id,))]
    result = {"mode": "sandbox", **{key: row[key] for key in (
        "state", "desired_state", "desired_generation", "observed_generation", "error_code")},
        **({"previews": previews} if previews else {})}
    if row.get("disk_checked_at"):
        result["disk"] = {
            "used_bytes": row["disk_used_bytes"],
            "budget_bytes": row["disk_budget_bytes"],
            "checked_at": row["disk_checked_at"],
            "status": row["disk_error_code"] or "ok",
        }
    return result


@dataclass(frozen=True)
class SandboxLease:
    """A database fence held by one reconciliation attempt."""

    business_id: str
    fence: int
    desired_generation: int


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("clock must return an ISO-8601 timestamp with a timezone") from exc


class SandboxStateStore:
    """State-only persistence for a trusted, post-commit provisioner."""

    def __init__(self, *, clock: Callable[[], str] = utc_now):
        self._clock = clock

    @staticmethod
    def _repository(uow):
        uow.require_write()
        return uow.repository("business_sandboxes")

    def _now(self) -> tuple[str, datetime]:
        value = self._clock()
        return value, _parse_timestamp(value)

    @staticmethod
    def _require_environment_id(environment_id: str) -> None:
        if (not isinstance(environment_id, str) or not 1 <= len(environment_id) <= 256
                or any(ord(character) < 32 or ord(character) > 126 for character in environment_id)):
            raise DomainError("validation_error", "Environment identity must be a bounded printable identifier")

    @staticmethod
    def _require_lease(row: dict, lease: SandboxLease, now: datetime) -> None:
        if row["business_id"] != lease.business_id or row["lease_fence"] != lease.fence:
            raise DomainError("reconciliation_lease_lost", "Reconciliation lease is no longer current", retryable=True)
        lease_until = row["lease_until"]
        if lease_until is None or _parse_timestamp(lease_until) <= now:
            raise DomainError("reconciliation_lease_lost", "Reconciliation lease expired", retryable=True)

    def get(self, uow, business_id: str, *, required: bool = False):
        """Read a lifecycle row inside the caller's existing unit of work."""
        return uow.repository("business_sandboxes").get(business_id, required=required)

    def insert_intent(self, uow, business_id: str):
        """Insert the first durable provisioning intent, idempotently.

        Call this in the same transaction that registers the business.  It deliberately
        carries no requested image, path, proxy, credential, or other desired config.
        """
        repository = self._repository(uow)
        existing = repository.get(business_id)
        if existing is not None:
            return existing
        if uow.repository("businesses").get(business_id) is None:
            raise DomainError("not_found", "Business must be registered before sandbox intent")
        return repository.insert({
            "business_id": business_id,
            "desired_generation": 1,
            "state": "pending",
        })

    def request_replacement(self, uow, business_id: str, *, expected_revision: int):
        """Request a new desired generation and invalidate every outstanding lease.

        The previous observed environment remains recorded so a reconciler can drain
        it safely before reuse of writable state.
        """
        repository = self._repository(uow)
        current = repository.get(business_id, required=True)
        return repository.update(business_id, {
            "desired_generation": current["desired_generation"] + 1,
            "state": "pending",
            "error_code": None,
            "lease_fence": current["lease_fence"] + 1,
            "lease_until": None,
        }, expected_revision=expected_revision)

    def request_state(self, uow, business_id: str, *, desired_state: str, expected_revision: int):
        """Persist operator intent before stopping or restarting external writers."""
        if desired_state not in {"running", "stopped"}:
            raise DomainError("validation_error", "Unsupported desired sandbox state")
        repository = self._repository(uow)
        current = repository.get(business_id, required=True)
        return repository.update(business_id, {
            "desired_state": desired_state, "state": "pending", "error_code": None,
            "lease_fence": current["lease_fence"] + 1, "lease_until": None,
        }, expected_revision=expected_revision)

    def claim(self, uow, business_id: str, *, lease_seconds: int = 120) -> SandboxLease | None:
        """Acquire a bounded reconciliation fence, or return ``None`` while leased."""
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise DomainError("validation_error", "Reconciliation lease must be between 1 and 600 seconds")
        repository = self._repository(uow)
        current = repository.get(business_id, required=True)
        _, now = self._now()
        lease_until = current["lease_until"]
        if lease_until is not None and _parse_timestamp(lease_until) > now:
            return None
        fence = current["lease_fence"] + 1
        expires = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        repository.update(business_id, {
            "lease_fence": fence,
            "lease_until": expires,
        }, expected_revision=current["revision"])
        return SandboxLease(business_id=business_id, fence=fence, desired_generation=current["desired_generation"])

    def mark_ready(self, uow, lease: SandboxLease, *, environment_id: str):
        """Publish a validated environment for the lease's desired generation."""
        self._require_environment_id(environment_id)
        repository = self._repository(uow)
        current = repository.get(lease.business_id, required=True)
        _, now = self._now()
        self._require_lease(current, lease, now)
        if current["desired_generation"] != lease.desired_generation:
            raise DomainError("reconciliation_lease_lost", "Desired generation changed", retryable=True)
        return repository.update(lease.business_id, {
            "observed_generation": lease.desired_generation,
            "environment_id": environment_id,
            "state": "ready",
            "error_code": None,
            "lease_until": None,
        }, expected_revision=current["revision"])

    def mark_stopped(self, uow, lease: SandboxLease, *, environment_id: str | None = None):
        """Publish that the lease's desired environment is deliberately stopped."""
        if environment_id is not None:
            self._require_environment_id(environment_id)
        repository = self._repository(uow)
        current = repository.get(lease.business_id, required=True)
        _, now = self._now()
        self._require_lease(current, lease, now)
        if current["desired_generation"] != lease.desired_generation:
            raise DomainError("reconciliation_lease_lost", "Desired generation changed", retryable=True)
        return repository.update(lease.business_id, {
            "observed_generation": lease.desired_generation if environment_id else current["observed_generation"],
            "environment_id": environment_id or current["environment_id"],
            "desired_state": "stopped",
            "state": "stopped",
            "error_code": None,
            "lease_until": None,
        }, expected_revision=current["revision"])

    def mark_pending(self, uow, lease: SandboxLease, *, error_code: str = "configuration_missing"):
        """Publish a visible, retryable configuration wait without losing observation.

        ``configuration_missing`` is deliberately the only pending reason.  Other
        bounded errors describe an attempted environment and belong in ``failed``.
        """
        if error_code != "configuration_missing":
            raise DomainError("validation_error", "Only missing configuration can leave a sandbox pending")
        repository = self._repository(uow)
        current = repository.get(lease.business_id, required=True)
        _, now = self._now()
        self._require_lease(current, lease, now)
        if current["desired_generation"] != lease.desired_generation:
            raise DomainError("reconciliation_lease_lost", "Desired generation changed", retryable=True)
        return repository.update(lease.business_id, {
            "state": "pending",
            "error_code": error_code,
            "lease_until": None,
        }, expected_revision=current["revision"])

    def record_disk(self, uow, lease: SandboxLease, report: dict):
        """Persist one sanitized disk observation after its external scan."""
        repository = self._repository(uow)
        current = repository.get(lease.business_id, required=True)
        _, now = self._now()
        self._require_lease(current, lease, now)
        fields = ("used_bytes", "budget_bytes", "reserved_bytes", "available_bytes", "host_floor_bytes")
        if any(type(report.get(name)) is not int or report[name] < 0 for name in fields):
            raise DomainError("validation_error", "Disk observation is invalid")
        checked_at = report.get("checked_at")
        if not isinstance(checked_at, str) or len(checked_at) > 40:
            raise DomainError("validation_error", "Disk observation timestamp is invalid")
        error = report.get("error_code")
        if error not in {None, "environment_conflict", "environment_unavailable"}:
            raise DomainError("validation_error", "Disk observation error is invalid")
        return repository.update(lease.business_id, {
            "disk_used_bytes": report["used_bytes"],
            "disk_budget_bytes": report["budget_bytes"],
            "disk_reserved_bytes": report["reserved_bytes"],
            "disk_available_bytes": report["available_bytes"],
            "disk_host_floor_bytes": report["host_floor_bytes"],
            "disk_checked_at": checked_at,
            "disk_error_code": error,
        }, expected_revision=current["revision"])

    def mark_failed(self, uow, lease: SandboxLease, *, error_code: str):
        """Publish a fixed, sanitized error code while retaining observed identity."""
        if error_code not in ERROR_CODES:
            raise DomainError("validation_error", "Sandbox failure must use a supported sanitized error code")
        repository = self._repository(uow)
        current = repository.get(lease.business_id, required=True)
        _, now = self._now()
        self._require_lease(current, lease, now)
        if current["desired_generation"] != lease.desired_generation:
            raise DomainError("reconciliation_lease_lost", "Desired generation changed", retryable=True)
        return repository.update(lease.business_id, {
            "state": "failed",
            "error_code": error_code,
            "lease_until": None,
        }, expected_revision=current["revision"])

    def renew(self, uow, lease: SandboxLease, *, lease_seconds: int = 120):
        """Extend an unexpired current fence while external checks are still running."""
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise DomainError("validation_error", "Reconciliation lease must be between 1 and 600 seconds")
        repository = self._repository(uow)
        current = repository.get(lease.business_id, required=True)
        _, now = self._now()
        self._require_lease(current, lease, now)
        if current["desired_generation"] != lease.desired_generation:
            raise DomainError("reconciliation_lease_lost", "Desired generation changed", retryable=True)
        expires = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return repository.update(lease.business_id, {"lease_until": expires}, expected_revision=current["revision"])

    def release(self, uow, lease: SandboxLease):
        """Release a current fence without publishing an external observation."""
        repository = self._repository(uow)
        current = repository.get(lease.business_id, required=True)
        _, now = self._now()
        self._require_lease(current, lease, now)
        return repository.update(lease.business_id, {"lease_until": None}, expected_revision=current["revision"])
