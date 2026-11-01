"""Injected authority and delivery boundaries; domain handlers stay synchronous.

Every port below is a typed seam the trusted host composes: the engine imports nothing from the host, and an
absent port fails closed (a delegated call without a ``DelegationProvider`` is forbidden, a protected mutation
without a ``SignoffStager`` is unavailable)."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from qorqut_operations.contracts import ArtifactRef, ChangeSet, InvocationContext, InvocationOptions, OperationResult, PolicyDecision
from .registry import OperationDescriptor


class PolicyResolver(Protocol):
    def resolve(self, context: InvocationContext, operation: OperationDescriptor,
                targets: tuple, uow) -> PolicyDecision: ...


class SignoffStager(Protocol):
    """Stages one exact prepared change for owner review. ``authorize_mail_control`` is called with the
    staged proposal result and the current per-operation decisions in the same transaction; it may only
    refuse or return the result unchanged."""

    def stage(self, context: InvocationContext, operation: OperationDescriptor,
              change_set: ChangeSet, options: InvocationOptions, uow) -> OperationResult: ...

    def authorize_mail_control(self, context: InvocationContext, operation: OperationDescriptor,
                               change_set: ChangeSet, result: OperationResult, decisions: Mapping[str, PolicyDecision],
                               uow) -> OperationResult: ...


class FileExecutor(Protocol):
    def pending(self, context: InvocationContext, operation: OperationDescriptor,
                options: InvocationOptions, input_hash: str, uow, *, namespace: str | None = None) -> ChangeSet | None: ...

    def execute(self, application, context: InvocationContext, operation: OperationDescriptor,
                change_set: ChangeSet, options: InvocationOptions, input_hash: str) -> OperationResult: ...

    def recover_pending(self, application, *, limit: int = 50) -> Sequence: ...


class DelegationProvider(Protocol):
    """Stored-row access, role support and committed-action recording for delegated workers.

    The engine keeps the authorization logic itself (binding, generation, live authentication, assignment
    operations and business/record boundaries, the explicit-selector rule for reads); this port only supplies
    the host's delegation rows and vocabulary. ``None`` from any lookup means "not found" and is refused."""

    worker_operations: frozenset[str]
    context_scoped_reads: frozenset[str]

    def binding(self, uow, binding_id: str) -> Mapping[str, Any] | None: ...

    def authenticate_binding(self, uow, credential_hash: str) -> Mapping[str, Any] | None: ...

    def assignment(self, uow, assignment_id: str) -> Mapping[str, Any] | None: ...

    def supports_role(self, role: ArtifactRef) -> bool: ...

    def required_capabilities(self, role: ArtifactRef) -> frozenset[str]: ...

    def selector_kinds(self, operation_name: str) -> Mapping[str, str]: ...

    def thread_has_assignment_message(self, uow, thread_id: str, assignment_id: str) -> bool: ...

    def record_committed_action(self, uow, assignment_id: str, **action: Any) -> None: ...
