"""Find the owning scope of an Action from its id alone, for the operations that change one.

Every Action lives in one owning scope: a project, or Global (``action_logs.project_id``). Action ids are unique across
all scopes, so ``actions_correct``, ``actions_withdraw``, ``actions_comment`` and ``actions_follow`` take ``action_id``
alone. ``ActionLookup`` is their ``locate_scope`` hook (see ``Application._locate``):

* no project selected: an Action the caller may read, by the rules ``actions_get`` applies, runs the operation in the
  scope that stores it, exactly as if it had been selected; an Action that does not exist or that the caller may not read
  is ``not_found`` with no detail;
* a project selected: the selection stands.
"""

from __future__ import annotations

from qorqut_operations.actions.scope import links, reader_scope, visible
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.repositories import decode_row


class ActionLookup:
    """The ``locate_scope`` hook of the operations that name one Action by ``action_id``."""

    def __call__(self, ctx, arguments, uow):
        """``(arguments, namespace or None)``: the namespace of the scope that stores the Action when the caller selected none."""
        action_id = arguments.get("action_id")
        if ctx.project_id is not None or not isinstance(action_id, str):
            return arguments, None
        row = decode_row(uow.connection.execute("SELECT * FROM action_logs WHERE id=?", (action_id,)).fetchone())
        if row is None or not visible(reader_scope(ctx, uow), row, links(uow, row["id"])):
            raise DomainError("not_found", "Action does not exist")
        return arguments, "global" if row["project_id"] is None else "project:" + row["project_id"]
