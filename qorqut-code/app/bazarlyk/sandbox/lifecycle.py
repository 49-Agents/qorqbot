"""One trusted post-commit reconciliation entrypoint for every business path."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from typing import Protocol

from qorqut_operations.contracts import DomainError
from qorqut_operations.application.engine import Application
from .config import BusinessPaths, HostConfig, business_identity
from .disk import DiskBudgetAdmission
from .state import SandboxStateStore
from qorqut_operations.storage.admission import InstallationRecoveryLock
from bazarlyk.composition.files import lock_paths


@dataclass(frozen=True)
class EnvironmentResult:
    environment_id: str
    state: str = "ready"


class EnvironmentDriver(Protocol):
    def stop(self, business: dict, paths: BusinessPaths, *, heartbeat) -> None:
        """Verify all owned writers stopped, retaining persistent files."""
        ...

    def reconcile(self, business: dict, paths: BusinessPaths, config: HostConfig,
                  generation: int, *, heartbeat) -> EnvironmentResult:
        """Inspect labeled resources, drain replacements, reconcile, validate.

        This call owns no SQLite transaction. Call heartbeat between bounded
        external operations. A ready result must mean native tools, networking,
        browser and registered preparation checks passed, not just Docker start.
        Never create a second writable environment while an old one is running.
        """
        ...


class SandboxLifecycle:
    def __init__(self, database, paths, driver: EnvironmentDriver, *, config_loader=HostConfig.read, state=None,
                 adoption_inventory=None, disk_admission=None):
        self.database, self.paths, self.driver = database, paths, driver
        self.root = paths.operations
        self.recovery_lock = InstallationRecoveryLock(lock_paths(paths).recovery)
        self.config_loader = config_loader
        self.state = state or SandboxStateStore()
        self.disk_admission = disk_admission or DiskBudgetAdmission(self.root)
        self._adoption_inventory = adoption_inventory
        self._rollback_boundary = None

    def set_adoption_inventory(self, inventory) -> None:
        """Install the composed, host-writer inventory used only for adoption.

        Normal creation commits its first intent with the business aggregate and
        never calls this.  The service composition installs a concrete callback
        after it has assembled the fixed tmux and legacy-browser inventories.
        """
        if not callable(inventory):
            raise TypeError("Sandbox adoption inventory must be callable")
        self._adoption_inventory = inventory

    def set_rollback_boundary(self, *, stop_listener, profile_lock_unheld, require_authority=lambda: None) -> None:
        if not callable(stop_listener) or not callable(profile_lock_unheld) or not callable(require_authority):
            raise TypeError("Sandbox rollback boundary must be callable")
        self._rollback_boundary = stop_listener, profile_lock_unheld, require_authority

    def _intent_exists(self, business_id: str) -> bool:
        with self.database.read() as uow:
            return self.state.get(uow, business_id) is not None

    def _adopt_existing_business(self, business_id: str, business: dict, paths: BusinessPaths) -> tuple[dict, BusinessPaths]:
        """Atomically establish a first intent after proving this host is idle.

        The exclusive installation lock excludes terminal opens, which take its
        shared side through write admission.  It is deliberately released before
        the ordinary shared reconciliation lock and before all external sandbox
        effects.  Inventory itself performs only bounded read-only observations.
        """
        if self._intent_exists(business_id):
            return business, paths
        if not callable(self._adoption_inventory):
            raise DomainError("environment_conflict", "Existing-business sandbox adoption inventory is unavailable",
                              retryable=True)
        try:
            with self.recovery_lock.exclusive(timeout=60):
                # A concurrent creator may have committed its aggregate while
                # this caller waited for the terminal admission barrier.
                with self.database.transaction() as uow:
                    Application.ensure_writes_open(uow)
                    current = self.state.get(uow, business_id)
                    selected = uow.repository("businesses").get(business_id, required=True)
                    selected_revision = selected["revision"]
                selected_paths = BusinessPaths.registered(self.paths, selected, require_workspace=False)
                if current is not None:
                    return selected, selected_paths
                self._adoption_inventory(selected, selected_paths, timeout=60)
                # Keep SQLite transactions out of inventory.  Recheck under the
                # same EX lock, then make intent insertion the sole mutation.
                with self.database.transaction() as uow:
                    Application.ensure_writes_open(uow)
                    if self.state.get(uow, business_id) is None:
                        latest = uow.repository("businesses").get(business_id, required=True)
                        if latest["revision"] != selected_revision:
                            raise DomainError("environment_conflict", "Business registration changed during sandbox adoption",
                                              retryable=True)
                        self.state.insert_intent(uow, business_id)
                return selected, selected_paths
        except TimeoutError as exc:
            raise DomainError("maintenance", "Existing-business sandbox adoption is waiting for terminal drain",
                              retryable=True) from exc

    def status(self, business_id):
        business_identity(business_id)
        with self.database.read() as uow:
            return self.state.get(uow, business_id)

    @contextmanager
    def _lock(self, paths):
        # The shared installation lock stays held across external reconciliation,
        # so full recovery's exclusive lock cannot race a second service process.
        with self.recovery_lock.writer():
            directory = paths.private_directory()
            descriptor = os.open(directory / "reconcile.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            acquired = False
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    pass
                yield acquired
            finally:
                if acquired:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def ensure_business_environment(self, business_id):
        """Create/adopt/reconcile one registered business, safely repeatable.

        Creation paths insert durable intent with their business records. The
        post-commit worker calls this method; explicit existing-business adoption
        can call the same method to establish that first intent. It never launches
        a model session, changes roles, or silently falls back to host terminals.
        """
        business_identity(business_id)
        with self.database.read() as uow:
            business = uow.repository("businesses").get(business_id, required=True)
        paths = BusinessPaths.registered(self.paths, business, require_workspace=False)
        business, paths = self._adopt_existing_business(business_id, business, paths)
        with self._lock(paths) as acquired:
            if not acquired:
                return self.status(business_id)
            with self.database.transaction() as uow:
                # First intent was either committed atomically by business
                # creation or by the guarded adoption step above.
                row = self.state.get(uow, business_id, required=True)
                lease = self.state.claim(uow, business_id, lease_seconds=600)
            if lease is None:
                return self.status(business_id)

            def heartbeat():
                with self.database.transaction() as uow:
                    self.state.renew(uow, lease, lease_seconds=600)

            try:
                if row["desired_state"] == "stopped":
                    self.driver.stop(business, paths, heartbeat=heartbeat)
                    with self.database.transaction() as uow:
                        return self.state.mark_stopped(uow, lease)
                config = self.config_loader(self.paths)
                if config is None or not config.enabled:
                    with self.database.transaction() as uow:
                        return self.state.mark_pending(uow, lease)
                if not (paths.workspace / "hq").is_dir():
                    with self.database.transaction() as uow:
                        return self.state.mark_pending(uow, lease)
                disk = self.disk_admission.check(self.database, paths, config)
                with self.database.transaction() as uow:
                    self.state.record_disk(uow, lease, disk.to_dict())
                if not disk.admitted:
                    raise DomainError(disk.error_code, "Sandbox persistent storage budget is unavailable")
                # External work deliberately starts after the intent/claim commit.
                result = self.driver.reconcile(business, paths, config, lease.desired_generation,
                                               heartbeat=heartbeat)
                if not isinstance(result, EnvironmentResult) or result.state != "ready":
                    raise DomainError("environment_unhealthy", "Sandbox readiness was not verified")
                with self.database.transaction() as uow:
                    return self.state.mark_ready(uow, lease, environment_id=result.environment_id)
            except DomainError as error:
                if error.code == "reconciliation_lease_lost":
                    # Another desired generation or process owns publication now.
                    # It will discover the already labeled resources under the lock.
                    raise
                with self.database.transaction() as uow:
                    if error.code == "configuration_missing":
                        return self.state.mark_pending(uow, lease)
                    code = error.code if error.code in {
                        "environment_conflict", "environment_unavailable", "environment_unhealthy"
                    } else "provisioning_failed"
                    return self.state.mark_failed(uow, lease, error_code=code)
            except (OSError, ValueError, RuntimeError):
                # Never put Docker stderr, request URLs or credentials in SQLite.
                with self.database.transaction() as uow:
                    return self.state.mark_failed(uow, lease, error_code="provisioning_failed")

    def rollback_to_host(self, business_id, *, expected_revision: int):
        """Explicitly return one verified stopped sandbox to its host writers.

        This is intentionally not an automatic recovery path.  It keeps all
        registered files and credentials, and deletes the durable intent only
        after the trusted driver has proved exact owned Docker absence.
        """
        business_identity(business_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise DomainError("validation_error", "expected_revision is required")
        if self._rollback_boundary is None or not callable(getattr(self.driver, "rollback_to_host", None)):
            raise DomainError("environment_conflict", "Sandbox host rollback is unavailable")
        stop_listener, profile_lock_unheld, require_authority = self._rollback_boundary
        with self.recovery_lock.exclusive(timeout=60):
            require_authority()
            with self.database.transaction() as uow:
                Application.ensure_writes_open(uow)
                business = uow.repository("businesses").get(business_id, required=True)
                row = self.state.get(uow, business_id, required=True)
                if (row["revision"] != expected_revision or row["desired_state"] != "stopped"
                        or row["state"] != "stopped" or row["lease_until"] is not None):
                    raise DomainError("environment_conflict", "Sandbox must be unchanged and stopped before host rollback")
            paths = BusinessPaths.registered(self.paths, business, require_workspace=False)
            self.driver.rollback_to_host(business, paths, stop_listener=stop_listener,
                                         profile_lock_unheld=profile_lock_unheld)
            with self.database.transaction() as uow:
                require_authority()
                Application.ensure_writes_open(uow)
                row = self.state.get(uow, business_id, required=True)
                if (row["revision"] != expected_revision or row["desired_state"] != "stopped"
                        or row["state"] != "stopped" or row["lease_until"] is not None):
                    raise DomainError("revision_conflict", "Sandbox intent changed during host rollback")
                return uow.repository("business_sandboxes").delete(business_id, expected_revision=expected_revision)
