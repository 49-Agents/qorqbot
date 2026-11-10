"""One process owns a prepared application through file work and finalization.

The generic prepared file executor: the host supplies the trusted file-target resolver (inside the
``FileApplier``) and the explicit lock directories; this module owns the receipts, claims and sequencing."""

from contextlib import contextmanager, nullcontext
import fcntl
import os
from uuid import UUID, uuid5

from qorqut_operations.contracts import ChangeSet, DomainError, InvocationOptions, OperationResult, canonical_hash, plain
from .locks import LockPaths
from .receipts import ApplyReceipts, IdempotencyReceipts
from .queue_indexes import RECOVER_PENDING

APPLICATION_NAMESPACE = UUID("6a4d4a51-8fcb-4e49-82f1-e82d788db632")


@contextmanager
def application_claim(locks: LockPaths, application_id):
    """A non-blocking process-wide claim on one stable application ID, in the explicit lock directory."""
    if not isinstance(locks, LockPaths):
        raise TypeError("Application claims need explicit LockPaths")
    try:
        if str(UUID(application_id)) != application_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise DomainError("validation_error", "Application claim requires a stable UUID") from exc
    directory = locks.application
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / (application_id + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DomainError("busy", "Prepared application is already running", retryable=True) from exc
        yield
    finally:
        os.close(descriptor)


class PreparedFileExecutor:
    """Allowed file effects use the same exact receipts as owner-approved effects."""

    def __init__(self, database, file_applier, *, context_loader=None, writer_admission=None):
        self.database, self.file_applier = database, file_applier
        self.context_loader, self.writer_admission = context_loader, writer_admission

    @staticmethod
    def _id(context, namespace, operation_name, options):
        key = IdempotencyReceipts.key(context.principal_id, namespace, operation_name, options.idempotency_key)
        return str(uuid5(APPLICATION_NAMESPACE, canonical_hash(key)))

    @staticmethod
    def _plan(row, input_hash):
        binding = row["progress_json"].get("invocation", {})
        if binding.get("input_hash") != input_hash or row["signoff_id"] is not None:
            raise DomainError("idempotency_conflict", "Application key is bound to different input")
        return ChangeSet.from_bytes(row["change_set"], expected_hash=row["payload_hash"])

    def pending(self, context, descriptor, options, input_hash, uow, *, namespace=None):
        if namespace is None:
            raise DomainError("invalid_plan", "Pending file lookup requires the server-resolved namespace")
        row = uow.repository("apply_receipts").get(self._id(context, namespace, descriptor.name, options))
        return self._plan(row, input_hash) if row else None

    def execute(self, app, context, descriptor, plan, options, input_hash):
        admission = self.writer_admission.writer() if self.writer_admission is not None else nullcontext()
        with self.database.observe(plan.operation_name, context.request_id, job_id=context.job_id,
                receipt_id=self._id(context, plan.target_namespace, descriptor.name, options)), admission:
            return self._execute(app, context, descriptor, plan, options, input_hash)

    def _execute(self, app, context, descriptor, plan, options, input_hash):
        namespace = plan.target_namespace
        receipt_id = self._id(context, namespace, descriptor.name, options)
        with application_claim(self.file_applier.locks, receipt_id):
            try:
                with self.database.transaction() as uow:
                    replay = IdempotencyReceipts(uow).lookup(context.principal_id, namespace, descriptor.name,
                                                           options.idempotency_key, input_hash)
                    if replay is not None:
                        return replay
                    receipt = uow.repository("apply_receipts").get(receipt_id)
                    if receipt is None:
                        app.validate_prepared(context, plan, uow, require_approval=False)
                        if any(value["mode"] != "allow" for value in plan.policy_binding.get("operations", {}).values()):
                            raise DomainError("approval_required", "This change requires its owner's exact approval")
                        ApplyReceipts(uow).create(plan, receipt_id=receipt_id)
                        receipt = ApplyReceipts(uow).record_progress(receipt_id, "invocation", {
                            "input_hash": input_hash, "idempotency_key": options.idempotency_key,
                            "options": plain(options)})
                    else:
                        plan = self._plan(receipt, input_hash)
                    if receipt["state"] == "applied":
                        return OperationResult.from_dict(receipt["result_json"])
                    context, descriptor = app.validate_prepared(context, plan, uow, receipt_id=receipt_id,
                                                               allow_unverified_files=True)
                    ApplyReceipts(uow).claim_resources(receipt_id)
                self.file_applier.apply(receipt_id)
                with self.database.transaction() as uow:
                    result = app.apply_prepared(context, plan, uow, receipt_id=receipt_id)
                    IdempotencyReceipts(uow).save(context.principal_id, namespace, descriptor.name,
                                                options.idempotency_key, input_hash, result)
                    ApplyReceipts(uow).finish(receipt_id, result.to_dict())
                return result
            except DomainError as exc:
                self._failure(receipt_id, exc)
                raise
            except Exception as exc:
                error = DomainError("operation_failed", "Prepared file application failed", retryable=True)
                self._failure(receipt_id, error)
                raise error from exc

    def _failure(self, receipt_id, error):
        with self.database.read() as uow:
            row = uow.repository("apply_receipts").get(receipt_id)
        if row is None or row["state"] == "applied":
            return
        try:
            self.file_applier.release_unchanged(receipt_id, error)
        except (DomainError, OSError):
            with self.database.transaction() as uow:
                ApplyReceipts(uow).fail(receipt_id, error)

    def recover_pending(self, app, *, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Recovery batch must contain 1-100 applications")
        with self.database.read() as uow:
            ids = [row[0] for row in uow.connection.execute(
                RECOVER_PENDING, (limit,))]
        results = []
        for receipt_id in ids:
            try:
                results.append(self.resume(app, receipt_id))
            except DomainError as exc:
                try:
                    with application_claim(self.file_applier.locks, receipt_id):
                        self._failure(receipt_id, exc)
                except DomainError as claim_error:
                    if claim_error.code != "busy":
                        raise
                results.append(OperationResult.failure(exc))
        return results

    def resume(self, app, receipt_id):
        """Trusted recovery entrypoint; callers must authorize the requested receipt first.

        Reconstruct its original principal and invocation, then revalidate current
        authority and exact targets. Never rebind saved effects to the retry caller.
        """
        with self.database.read() as uow:
            row = ApplyReceipts(uow).get(receipt_id)
            invocation = row["progress_json"]["invocation"]
            plan = self._plan(row, invocation["input_hash"])
            business = plan.target_namespace.removeprefix("business:") if plan.target_namespace.startswith("business:") else None
            if self.context_loader:
                context = self.context_loader(plan, uow)
            else:
                context = app.policy.context_for(plan.principal_id, business_id=business,
                    review_business_id=plan.review_business_id, initiated_by_principal_id=plan.initiated_by_principal_id,
                    job_id=plan.policy_binding.get("job_id"), uow=uow)
            descriptor = app.registry.get(plan.operation_name)
            options = InvocationOptions.from_mapping(invocation["options"])
        return self.execute(app, context, descriptor, plan, options, invocation["input_hash"])
