"""Host controllers for the engine's installation-recovery writer barrier: Docker sandbox containers.

The barrier itself (``RecoveryQuiescer``, ``QuiescenceProof``, ``BoundedWriter``, ``KnownZeroWriters`` and the
maintenance gate) is the Operations engine's (``qorqut_operations.storage.quiescence``). What this module owns is the
Docker-specific inventory and pause/unpause of the exact labeled sandbox containers, passed to that barrier as its
``container_writers``."""

from __future__ import annotations

import hashlib
from time import monotonic

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.quiescence import QuiescenceProof
from .docker import Docker, Ownership


class DockerSandboxWriters:
    """Inventory and freeze every registered sandbox container generation."""

    def __init__(self, database, root, *, freezer=None):
        self.database, self.root = database, str(root)
        self.freezer = freezer or DockerFreezer()
        self._frozen = ()

    @property
    def _installation(self):
        return hashlib.sha256(self.root.encode()).hexdigest()[:20]

    def _ownerships(self):
        with self.database.read() as uow:
            rows = uow.connection.execute("SELECT business_id FROM business_sandboxes ORDER BY business_id").fetchall()
        return tuple(Ownership(self._installation, row[0]) for row in rows)

    def freeze(self, *, timeout):
        deadline = monotonic() + timeout
        tracked = []
        self._frozen = tuple(tracked)
        error = None
        for ownership in self._ownerships():
            identities = []
            tracked.append((ownership, identities))
            def candidate(identity):
                identities.append(identity)
                self._frozen = tuple((item_ownership, tuple(item_ids)) for item_ownership, item_ids in tracked)
            self._frozen = tuple((item_ownership, tuple(item_ids)) for item_ownership, item_ids in tracked)
            try:
                self.freezer.freeze(ownership, deadline=deadline, on_candidate=candidate)
                self._frozen = tuple((item_ownership, tuple(item_ids)) for item_ownership, item_ids in tracked)
            except (DomainError, TimeoutError) as exc:
                error = exc
                break
        if error is not None:
            try:
                self.thaw(deadline=deadline)
            except DomainError:
                pass
            raise error
        return QuiescenceProof("sandbox_containers", "frozen")

    def thaw(self, *, deadline=None):
        deadline = deadline or monotonic() + 60
        remaining, failure = [], None
        for ownership, identities in reversed(self._frozen):
            try:
                self.freezer.thaw(ownership, identities, deadline=deadline)
            except DomainError as exc:
                remaining.append((ownership, identities))
                failure = failure or exc
        self._frozen = tuple(reversed(remaining))
        if failure is not None:
            raise failure




class DockerFreezer:
    """Pause only exact, labeled containers and prove their paused state."""

    def __init__(self, docker=None):
        self.docker = docker or Docker()

    @staticmethod
    def _ids(docker, ownership, *, timeout=60):
        arguments = ["container", "ls", "--quiet", "--no-trunc", "--all"]
        for key, value in ownership.labels.items():
            arguments.extend(("--filter", f"label={key}={value}"))
        return tuple(identity for identity in docker.call(*arguments, timeout=timeout).splitlines() if identity)

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("recovery Docker deadline elapsed")
        return min(60, remaining)

    def freeze(self, ownership: Ownership, *, deadline, on_candidate):
        candidates = []
        try:
            for identity in self._ids(self.docker, ownership, timeout=self._remaining(deadline)):
                current = self.docker.owned("container", identity, ownership, timeout=self._remaining(deadline))
                if current is None or current.get("State", {}).get("Paused"):
                    continue
                if current.get("State", {}).get("Running"):
                    candidates.append(identity)
                    on_candidate(identity)
                    self.docker.call("pause", identity, timeout=self._remaining(deadline))
                    observed = self.docker.owned("container", identity, ownership, timeout=self._remaining(deadline))
                    if not observed or not observed.get("State", {}).get("Paused"):
                        raise DomainError("environment_unavailable", "Sandbox writer did not enter paused state", retryable=True)
        except Exception:
            # Candidates are recorded before pause.  A timed-out pause can have
            # taken effect, so inspect every candidate during rollback.
            self.thaw(ownership, reversed(candidates), deadline=deadline)
            raise

    def thaw(self, ownership: Ownership, identities, *, deadline):
        failures = []
        for identity in identities:
            try:
                current = self.docker.owned("container", identity, ownership, timeout=self._remaining(deadline))
                if current and current.get("State", {}).get("Paused"):
                    self.docker.call("unpause", identity, timeout=self._remaining(deadline))
                    observed = self.docker.owned("container", identity, ownership, timeout=self._remaining(deadline))
                    if observed and observed.get("State", {}).get("Paused"):
                        raise DomainError("environment_unavailable", "Sandbox writer did not leave paused state", retryable=True)
            except (DomainError, TimeoutError):
                failures.append(identity)
        if failures:
            raise DomainError("environment_unavailable", "Sandbox writer could not be resumed", retryable=True)
