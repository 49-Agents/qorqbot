"""The Action module's required outward port: how an accepted immutable revision reaches its consumer.

``settle`` does not know what consumes a revision. The composition supplies one callable, ``on_revision(ctx, exact_revision,
uow) -> input_id``, that receives the exact stored ``action_revisions`` row (decoded, never a later read of the Action) inside
the applying transaction and returns the ID of whatever it admitted. It is required wherever Actions are settled; there is
no default and no no-op, so an assembly that forgets it fails at construction instead of silently losing revisions."""

from __future__ import annotations

from typing import Any, Protocol

from qorqut_operations.storage.assembly import AssemblyError


class RevisionAdmission(Protocol):
    """``on_revision(ctx, exact_revision, uow) -> input_id``: admit one stored Action revision in the open transaction."""

    def __call__(self, ctx: Any, exact_revision: dict, uow: Any) -> str: ...


def require_on_revision(on_revision: Any) -> RevisionAdmission:
    """The supplied revision-admission callable, or an assembly error if it is missing or not callable."""
    if not callable(on_revision):
        raise AssemblyError("Actions need a revision admission callable on_revision(ctx, exact_revision, uow) -> input_id")
    return on_revision


__all__ = ["RevisionAdmission", "require_on_revision"]
