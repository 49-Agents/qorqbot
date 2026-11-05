"""The Application's own admission of a direct call to a read operation, for host code that reads what that operation would
return without invoking it (a linked record's discussion in the aggregate activity, a Table's discussion count on a Focus card).

``ItemResolver.authorize`` only asks the policy resolver for a decision. A direct call is admitted by more than that: the
operation must be exposed to the caller's source, a Global call needs the granted capability, and a delegated worker must
name an in-boundary record through the operation's own selector argument. This module performs exactly the admission
``Application.invoke`` performs for a read - the operation's namespace for the exact arguments, then ``Application.authorize``
with those arguments - and nothing else: it neither runs the read handler nor widens, caches or records any decision."""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError

# Refusals meaning "this caller may not read that now"; callers treat them as an absent (or undisclosed) source.
REFUSED = frozenset({"forbidden", "policy_denied", "not_found", "operation_unavailable", "validation_error", "scope_required",
                     "business_archived", "context_refresh_required", "invalid_principal"})


def admit_read(application, ctx, operation, arguments, uow):
    """The context the read ``operation`` would run under, or a ``DomainError`` when this caller may not call it with exactly
    ``arguments``. ``ctx`` is already the caller's current context for the record's own scope."""
    descriptor = application.registry.get(operation)
    bound = application.registry.bind_arguments(operation, arguments)
    namespace = application.namespace(ctx, descriptor, bound)
    if namespace == "global":
        ctx = replace(ctx, business_id=None, review_business_id=None)
    decision = application.authorize(ctx, descriptor, (), uow, namespace=namespace, arguments=bound)
    if decision.mode != "allow":
        raise DomainError("policy_denied", "The read operation is not permitted")
    return ctx


def may_read(application, ctx, operation, arguments, uow) -> bool:
    """``admit_read`` as a yes/no for presentation: a refusal is ``False`` and discloses nothing; any other failure propagates."""
    try:
        admit_read(application, ctx, operation, arguments, uow)
    except DomainError as error:
        if error.code in REFUSED:
            return False
        raise
    return True
