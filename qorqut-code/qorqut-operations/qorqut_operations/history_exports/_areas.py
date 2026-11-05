"""Typed authored-area selections, their validation and the representation of a resolved area.

A trusted area provider reads the current registrations inside the history export's own read unit and returns values of
exactly these four kinds. Values carry exact registered IDs and business slugs, never absolute paths; the host resolves
them to folders below the operations root through its own ``resolve_areas`` callable after the read unit has closed.
Nothing here scans folders, reads tags or guesses a registration, and a duplicate, unsafe or overlapping selection fails
the snapshot before any file is captured.

``excluded`` on a selection is a trusted list of subtrees, relative to that area, that a domain generates itself (for
example table-export projections). Each must be a safe normalized relative path; the capture then skips it and never
carries an earlier saved copy of it forward.

What counts as one safe registered ID or slug is the host's existing naming rule, handed in as ``safe_segment``; no
second rule is defined here.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Union

from qorqut_operations.contracts import DomainError

MAXIMUM_AREAS = 5_000
MAXIMUM_EXCLUDED = 64
MAXIMUM_SUBTREE = 512


def _component(value: Any) -> bool:
    return isinstance(value, str) and value not in ("", ".", "..") and not any(
        character in value for character in "/\\\0")


@dataclass(frozen=True)
class GlobalFiles:
    """The global authored working material (``operations/files``)."""

    excluded: tuple[str, ...] = ()
    kind = "global_files"
    identifiers = ()


@dataclass(frozen=True)
class GlobalAgentFiles:
    """One registered global agent's authored files (``operations/agents/{agent_id}/files``)."""

    agent_id: str
    excluded: tuple[str, ...] = ()
    kind = "global_agent_files"

    @property
    def identifiers(self):
        return (self.agent_id,)


@dataclass(frozen=True)
class BusinessAgentFiles:
    """One registered business agent's authored files (``operations/projects/{project}/agents/{agent_id}/files``)."""

    business: str
    agent_id: str
    excluded: tuple[str, ...] = ()
    kind = "business_agent_files"

    @property
    def identifiers(self):
        return (self.business, self.agent_id)


@dataclass(frozen=True)
class BusinessHeadquarters:
    """One registered business's headquarters notes and documents (``operations/projects/{project}/hq``)."""

    business: str
    excluded: tuple[str, ...] = ()
    kind = "business_hq"

    @property
    def identifiers(self):
        return (self.business,)


Area = Union[GlobalFiles, GlobalAgentFiles, BusinessAgentFiles, BusinessHeadquarters]
AREA_TYPES = (GlobalFiles, GlobalAgentFiles, BusinessAgentFiles, BusinessHeadquarters)
AreaProvider = Callable[[Any], Iterable[Area]]


def label_of(area: Area) -> str:
    return ":".join((area.kind, *area.identifiers)) if area.identifiers else area.kind


def invalid(message: str, **details: Any) -> DomainError:
    return DomainError("history_areas_invalid", message, details=details or None)


def normalize_subtree(value: Any) -> tuple[str, ...]:
    """A safe normalized relative POSIX subtree: no empty, ``.``/``..``, backslash, NUL or slash-edge components."""
    if not isinstance(value, str) or not value or len(value) > MAXIMUM_SUBTREE:
        raise invalid("An excluded subtree is a short relative path")
    parts = tuple(value.split("/"))
    if not all(_component(part) for part in parts):
        raise invalid("An excluded subtree must be a normalized relative path", subtree=value[:80])
    return parts


@dataclass(frozen=True)
class ResolvedArea:
    """One validated area and where the host placed it: ``parts`` below the operations root, or ``blocked``.

    Build it with ``located`` (the folder's path components relative to the operations root) or ``refused`` (an area whose
    existing path the host refused, for example one that crosses a symlink: it is reported as excluded and never read)."""

    area: Area
    label: str
    parts: tuple[str, ...] | None
    excluded: frozenset[str]
    blocked: str | None = None

    @classmethod
    def located(cls, area: Area, parts: Iterable[str]) -> ResolvedArea:
        parts = tuple(parts)
        excluded = frozenset("/".join((*parts, *normalize_subtree(item))) for item in area.excluded)
        return cls(area, label_of(area), parts, excluded)

    @classmethod
    def refused(cls, area: Area, reason: str = "symlink") -> ResolvedArea:
        return cls(area, label_of(area), None, frozenset(), reason)


def validate(raw: Iterable[Any], safe_segment: Callable[[str], object]) -> tuple[Area, ...]:
    """Check the provider's typed values: kinds, exact IDs, normalized exclusions and no duplicate or overlapping area.

    ``safe_segment`` is the host's validator for one registered ID or slug (truthy when it is one safe path segment)."""
    areas = tuple(raw)
    if len(areas) > MAXIMUM_AREAS:
        raise invalid("The area provider returned too many areas")
    seen: dict[str, str] = {}
    for area in areas:
        if type(area) not in AREA_TYPES:
            raise invalid("Areas are GlobalFiles, GlobalAgentFiles, BusinessAgentFiles or BusinessHeadquarters values")
        for identifier in area.identifiers:
            if not isinstance(identifier, str) or not safe_segment(identifier):
                raise invalid("An area names a registered ID or slug that is not one safe path segment", kind=area.kind)
        excluded = area.excluded
        if isinstance(excluded, str) or not isinstance(excluded, (tuple, list)):
            raise invalid("An area's excluded subtrees are a tuple of relative paths", kind=area.kind)
        if len(excluded) > MAXIMUM_EXCLUDED or len({normalize_subtree(item) for item in excluded}) != len(excluded):
            raise invalid("An area's excluded subtrees are unique and bounded", kind=area.kind)
        label = label_of(area)
        # Case-folded as well: a case-insensitive filesystem would merge two such selections into one folder.
        key = label.casefold()
        if key in seen:
            raise invalid("An area is selected more than once", area=label)
        seen[key] = label
    return tuple(sorted(areas, key=label_of))


def check_resolved(selection: tuple[Area, ...], resolved: Any) -> tuple[ResolvedArea, ...]:
    """The host's resolution of exactly ``selection``, in order; overlapping folders fail the whole selection."""
    try:
        resolved = tuple(resolved)
    except TypeError:
        raise invalid("The area resolver must return an iterable of resolved areas") from None
    if len(resolved) != len(selection) or any(
            type(item) is not ResolvedArea or item.area != area for item, area in zip(resolved, selection)):
        raise invalid("The area resolver must return one resolved area for each selected area, in order")
    located = [(tuple(part.casefold() for part in item.parts), item.label) for item in resolved if item.parts]
    for index, (first, first_label) in enumerate(located):
        for second, second_label in located[index + 1:]:
            if first[:len(second)] == second or second[:len(first)] == first:
                raise invalid("Areas overlap", first=first_label, second=second_label)
    return resolved
