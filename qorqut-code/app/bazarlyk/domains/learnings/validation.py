"""Public semantic guard for saved factual learning plans.

``validate_factual_plan`` is the shared entry point that both the retained learning mutators and the offline migration
use. It accepts an ordinary operation context, a decoded prepared ``ChangeSet`` and a UnitOfWork on the caller's existing
snapshot/transaction; it performs reads only and returns ``None`` for a supported factual learning plan. Procedural,
engine-linked, unsupported or unprovable learning effects raise ``DomainError``.

It deliberately needs no running service, provider, document executor or new transaction. Full Application authority,
version, revision, scope, receipt and approval checks are the caller's additional responsibility: this function does not
authorize executing a plan and does not classify arbitrary non-learning operations as compatible.

The implementation is the one guard already registered through ``OperationDescriptor.validate_targets`` on every
retained learning mutator (composed with ``documents.validate_targets`` where the Context intake effect is carried), so
the offline caller and the live executor cannot disagree.
"""

from .legacy import validate_saved_plan as _validate_saved_plan


def validate_factual_plan(ctx, plan, uow, *, source_bytes=None):
    """Return ``None`` for one supported factual learning plan, or raise ``DomainError`` describing the refusal.

    ``source_bytes`` is the trusted positive configured Context source limit. A plan carrying the supported Context
    filing composition must receive it explicitly; there is no library-default fallback. Plans without a filing keep
    working through the original three-argument call.
    """
    return _validate_saved_plan(ctx, plan, uow, source_bytes=source_bytes)