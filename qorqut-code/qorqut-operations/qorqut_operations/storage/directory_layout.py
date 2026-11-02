"""The frozen values of a directory-tree file effect: an empty directory layout and its deterministic typed hash.

A ``create_directory_tree`` effect belongs to the prepared file-effect family (``PreparedChange`` of kind ``file``). It
makes one new directory tree that holds only empty directories. Its frozen values are the relative layout, the
before state (absence, ``before_hash`` is ``None``) and the typed layout hash as ``after_hash``. The hash describes a
layout only: an identical empty tree that someone else made has the same hash, so it is never proof of ownership.
Ownership is the separate inode identity proof the applier records in the receipt (see ``directories``).
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import re

from qorqut_operations.contracts import DomainError

ACTION = "create_directory_tree"
LAYOUT_TYPE = "empty-directory-tree"
LAYOUT_VERSION = 1
MAX_DIRECTORIES = 64
MAX_DEPTH = 8
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
VALUE_KEYS = frozenset({"layout", "version", "directories", "before_hash", "after_hash"})


def layout_hash(directories: Iterable[str], unexpected: Iterable[str] = ()) -> str:
    """The typed hash of an empty-directory layout; ``unexpected`` names anything else found, so it never equals a clean tree."""
    value = {"type": LAYOUT_TYPE, "version": LAYOUT_VERSION, "directories": sorted(directories)}
    unexpected = sorted(unexpected)
    if unexpected:
        value["unexpected"] = unexpected
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def normalized(directories: Iterable[str]) -> tuple[str, ...]:
    """Validate a relative layout: safe segments, every parent listed, bounded size and depth; returned sorted."""
    if isinstance(directories, (str, bytes)):
        raise DomainError("invalid_plan", "A directory layout is a list of relative paths")
    found = sorted(directories)
    if len(found) > MAX_DIRECTORIES or len(set(found)) != len(found):
        raise DomainError("invalid_plan", "A directory layout holds a bounded set of distinct paths")
    listed = set(found)
    for path in found:
        parts = path.split("/") if isinstance(path, str) else None
        if (not parts or len(parts) > MAX_DEPTH or any(SEGMENT.fullmatch(part) is None or part in (".", "..") for part in parts)):
            raise DomainError("invalid_plan", "A directory layout path is not a safe relative path")
        if len(parts) > 1 and "/".join(parts[:-1]) not in listed:
            raise DomainError("invalid_plan", "A directory layout lists every parent directory")
    return tuple(found)


def directory_values(directories: Iterable[str]) -> dict:
    """The complete frozen values of a ``create_directory_tree`` effect."""
    layout = normalized(directories)
    return {"layout": LAYOUT_TYPE, "version": LAYOUT_VERSION, "directories": list(layout),
            "before_hash": None, "after_hash": layout_hash(layout)}


def checked_values(values) -> tuple[str, ...]:
    """The layout of prepared directory values, refusing any value that is not exactly what ``directory_values`` builds."""
    if (not hasattr(values, "keys") or set(values) != VALUE_KEYS or values["layout"] != LAYOUT_TYPE
            or values["version"] != LAYOUT_VERSION or values["before_hash"] is not None
            or not isinstance(values["directories"], (list, tuple))):
        raise DomainError("invalid_plan", "Prepared directory effect has invalid fields")
    layout = normalized(values["directories"])
    if values["after_hash"] != layout_hash(layout):
        raise DomainError("invalid_plan", "Prepared directory hash does not match its layout")
    return layout
