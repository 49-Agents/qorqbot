"""Find a Signoff's review scope from its id alone, for an agent that selected none.

Every Signoff is reviewed in one scope: a project, or Global. Signoff ids are unique across scopes, so an agent calling a
Signoff tool over MCP may name the Signoff by ``signoff_id`` alone. ``SignoffLookup`` is the ``locate_scope`` hook of the
agent-facing operations that take one ``signoff_id`` (see ``Application._locate``). A Signoff counts as found only where
the caller could read it by selecting that scope itself: the service's own context, ``signoffs_get`` authority and
visibility rules, judged for that one scope (``SignoffSources.readable``).

* nothing selected (no ``project_id``, no ``scope``, no review selector): a Signoff reviewed in a project runs the
  operation in that project, exactly as if it had been selected. One reviewed in Global runs it as ``scope=global``:
  these operations read their Global selection from that argument, so the hook states the scope it found there and the
  call is the explicit one in every respect, its input hash and idempotency receipt included. A Signoff that does not
  exist, or that the caller may not read, is ``not_found``;
* a project selected: the selection stands. A Signoff reviewed in another scope the caller may read is refused with
  ``not_found`` saying where; any other keeps the operation's own answer;
* ``scope=global``, ``scope=project`` without a project, or a review selector (``review_project_id``): the call is left
  exactly as it was.

Nothing outside the caller's reach is disclosed. An owner session is bound to its project and is never looked up
elsewhere. A delegated worker's call is looked up only in the one scope its assignment binds, a project or Global.
"""

from __future__ import annotations

from dataclasses import replace

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.contracts import DomainError
from qorqut_operations.records.references import KINDS

# Appended to the description of every operation that takes this hook.
LOCATED = instruction_text('lookup-pass-signoff-id-alone-its-review-scope-is-found')


class SignoffLookup:
    """The ``locate_scope`` hook of the agent-facing operations that name one Signoff by ``signoff_id``."""

    def __init__(self, service):
        self.service = service

    def __call__(self, ctx, arguments, uow):
        """``(arguments, namespace or None)``: the namespace to run in when the caller selected none and may read the
        Signoff where it is reviewed, with ``scope=global`` stated in the arguments for a Global one."""
        signoff_id = arguments.get("signoff_id")
        if (not isinstance(signoff_id, str) or arguments.get("scope") == "global" or "review_project_id" in arguments
                or ctx.owner_session_project_id is not None):
            return arguments, None
        if ctx.delegation_assignment_id is not None:
            return self.delegated(ctx, arguments, signoff_id, uow)
        if ctx.project_id is not None:
            return self.selected(ctx, arguments, signoff_id, uow), None
        if "scope" in arguments or ctx.review_project_id is not None:
            return arguments, None  # scope=project without a project, or a review selector: the operation answers.
        found, project_id = self.place(signoff_id, uow)
        if not found or self.view(ctx, signoff_id, project_id, uow) is None:
            raise DomainError("not_found", "Proposal does not exist in any review scope you can reach")
        return self.located(arguments, project_id)

    @staticmethod
    def located(arguments, project_id):
        """The hook's answer for a Signoff found in this review scope (``None`` is Global)."""
        if project_id is None:
            return {**arguments, "scope": "global"}, "global"
        return arguments, "project:" + project_id

    @staticmethod
    def place(signoff_id, uow):
        """``(found, project_id)``: whether a Signoff has this id, and the project that reviews it (``None`` for Global)."""
        row = uow.connection.execute("SELECT review_project_id FROM signoffs WHERE id = ?", (signoff_id,)).fetchone()
        return row is not None, row["review_project_id"] if row else None

    def view(self, ctx, signoff_id, project_id, uow):
        """What the caller is told of a Signoff it may read in this, its own review scope; ``None`` when it may not read
        it there. A project outside the caller's reach is not judged at all."""
        if project_id is not None and project_id not in ctx.allowed_project_ids:
            return None
        candidates = [{"signoff_id": signoff_id, "review_project_id": project_id}]
        visible = self.service.sources.readable(ctx, candidates, uow)
        if signoff_id not in visible:
            return None
        project = uow.repository("projects").get(project_id) if project_id else None
        return {"id": signoff_id, "review_project_id": project_id,
                "project_name": (project["name"] or project["slug"]) if project else "Global", "title": visible[signoff_id]}

    def selected(self, ctx, arguments, signoff_id, uow):
        """The caller's own project stands; a Signoff reviewed in another scope it may read is refused saying where."""
        if ctx.review_project_id not in (None, ctx.project_id):
            return arguments  # A review selector naming another project: the operation answers.
        found, project_id = self.place(signoff_id, uow)
        if not found or project_id == ctx.project_id:
            return arguments
        view = self.view(ctx, signoff_id, project_id, uow)
        if view is None:
            return arguments
        raise DomainError("not_found", f"Proposal is not in the selected review scope; it is in {view['project_name']}. "
                          "Call again with signoff_id alone, without project_id or scope", details={"signoff": view})

    def delegated(self, ctx, arguments, signoff_id, uow):
        """A delegated worker that selects nothing works in the one scope its assignment binds when the Signoff is
        reviewed there; nothing else is looked at, so the worker learns nothing about other scopes and the operation and
        the assignment's own scope check still decide."""
        provider = self.service.app.delegation
        if ctx.project_id is not None or "scope" in arguments or ctx.review_project_id is not None or provider is None:
            return arguments, None
        assignment = provider.assignment(uow, ctx.delegation_assignment_id)
        found, project_id = self.place(signoff_id, uow)
        if (assignment is None or not found or project_id != assignment["project_id"]
                or self.view(ctx, signoff_id, project_id, uow) is None):
            return arguments, None
        return self.located(arguments, project_id)


def reference_kind(service):
    """The Signoff kind of ``records.references`` that completes a reference only for a Signoff the caller may read in
    its review scope, by the same rule as ``SignoffLookup.view``."""
    lookup = SignoffLookup(service)
    return replace(KINDS["signoff"],
                   visible=lambda ctx, uow, signoff_id, project_id: lookup.view(ctx, signoff_id, project_id, uow) is not None)


__all__ = ["LOCATED", "SignoffLookup", "reference_kind"]
