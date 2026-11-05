"""Routing between the two controlled deterministic edit formats; neither is selected by text or a filename."""
from __future__ import annotations

from dataclasses import dataclass

from . import graph_edits, supervised_edits
from .schema import ContextError

MODULES = (supervised_edits, graph_edits)


@dataclass(frozen=True)
class Edit:
    """One validated controlled edit: its format module, normalized plan and execution marker."""
    module: object
    plan: dict
    marker: dict


def route(record):
    """The module owning a pending job or published event, ``None`` for ordinary input.

    Each module refuses the other's reserved prefix or marker, so a record is never claimed by both.
    """
    for module in MODULES:
        if module.binding(record):
            return module
    return None


def binding(record):
    return route(record) is not None


def is_supervised(record):
    return route(record) is supervised_edits


def bound_plan(record, content):
    module = route(record)
    return None if module is None else module.bound_plan(record, content)


def check_event(event, files):
    module = route(event)
    if module is not None:
        module.check_event(event, files)


def check_commit(record, content, read):
    """For a supervised edit, check its plan against the records of the commit that published it; others are untouched."""
    if is_supervised(record):
        supervised_edits.check_commit(supervised_edits.bound_plan(record, content), read)


def module_for(plan):
    """The module whose format a normalized plan declares."""
    for module in MODULES:
        if plan.get("format") == module.FORMAT:
            return module
    raise ContextError("Unknown controlled edit format")


def graph_edit(plan):
    return Edit(graph_edits, plan, graph_edits.marker(plan))


def supervised_edit(plan, content):
    return Edit(supervised_edits, plan, supervised_edits.marker(plan, content))
