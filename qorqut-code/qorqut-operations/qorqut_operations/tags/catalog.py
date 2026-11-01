"""Label normalization, catalog lookups and the planned application of tags to an item."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import re
import unicodedata

from qorqut_operations.application.plans import ref
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.departments import ALIASES, department_id

LABEL_LIMIT = 64
TAG_LIST_LIMIT = 100
LABEL_SCHEMA = {"type": "string", "minLength": 1, "maxLength": LABEL_LIMIT}
LABELS_SCHEMA = {"type": "array", "items": LABEL_SCHEMA, "maxItems": TAG_LIST_LIMIT, "uniqueItems": True}
_SPACE = re.compile(r"\s+")


def display_label(value) -> str:
    """The stable display form of a label: trimmed, single-spaced, one line."""
    if not isinstance(value, str):
        raise DomainError("validation_error", "A tag is a text label")
    label = _SPACE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    if not label or len(label) > LABEL_LIMIT or any(unicodedata.category(char).startswith("C") for char in label):
        raise DomainError("validation_error", f"A tag is a visible label of 1 to {LABEL_LIMIT} characters")
    return label


def label_key(value) -> str:
    """The normalized identity of a label: the same words in any case or spacing are one catalog entry."""
    return display_label(value).casefold()


def distinct_labels(values: Iterable) -> list[str]:
    """The labels once each, in order of first use, by normalized key."""
    seen, labels = set(), []
    for value in values:
        label = display_label(value)
        if label.casefold() not in seen:
            seen.add(label.casefold())
            labels.append(label)
    if len(labels) > TAG_LIST_LIMIT:
        raise DomainError("validation_error", f"An item carries at most {TAG_LIST_LIMIT} tags")
    return labels


def canonical_labels(values: Iterable) -> list[str]:
    """The labels once each, with a department alias (any case) replaced by the department's id, as v1 normalized tags."""
    return list(dict.fromkeys(department_id(label.casefold()) if label.casefold() in ALIASES else label for label in distinct_labels(values)))


def catalog_row(uow, label):
    return uow.connection.execute("SELECT * FROM tags WHERE label_key=?", (label_key(label),)).fetchone()


def item_labels(uow, business_id, item_type: str, item_id: str) -> list[str]:
    """The display labels applied to one item, in catalog order."""
    return [row[0] for row in uow.connection.execute(
        "SELECT t.label FROM item_tags i JOIN tags t ON t.id=i.tag_id WHERE i.business_id IS ? AND i.item_type=? "
        "AND i.item_id=? ORDER BY t.label_key", (business_id, item_type, item_id))]


def snapshot_order(snapshots: Iterable[dict]) -> list[dict]:
    """Tag snapshots in their one deterministic order: display name (case-insensitive), then the stable catalog id."""
    return sorted(snapshots, key=lambda tag: (tag["name"].casefold(), tag["id"]))


def tag_snapshots(uow, business_id, item_type: str, item_id: str) -> list[dict]:
    """The item's tags as immutable revision snapshots ``{id, name, business_id}``.

    ``id`` is the stable catalog id and ``name`` the catalog's display label, which never changes after creation. The
    catalog is shared, so ``business_id`` is always None: a tag is not owned by a business and is never inferred to
    be one. The owning scope of the item is recorded separately by whatever freezes the snapshot."""
    return snapshot_order(
        {"id": row[0], "name": row[1], "business_id": None} for row in uow.connection.execute(
            "SELECT t.id, t.label FROM item_tags i JOIN tags t ON t.id=i.tag_id WHERE i.business_id IS ? AND i.item_type=? "
            "AND i.item_id=?", (business_id, item_type, item_id)))


def labels_by_item(uow, business_id, item_type: str) -> dict[str, list[str]]:
    """Every tagged item of one namespace and kind, for list filtering after the caller's access is resolved."""
    found: dict[str, list[str]] = {}
    for row in uow.connection.execute(
            "SELECT i.item_id, t.label FROM item_tags i JOIN tags t ON t.id=i.tag_id WHERE i.business_id IS ? "
            "AND i.item_type=? ORDER BY t.label_key", (business_id, item_type)):
        found.setdefault(row[0], []).append(row[1])
    return found


def matches(item_labels_: Sequence[str], wanted: Sequence[str]) -> bool:
    """Whether an item carries every wanted label (by normalized key)."""
    have = {label.casefold() for label in item_labels_}
    return all(label_key(label) in have for label in wanted)


def plan_item_tags(builder, uow, item_type: str, item_id: str, business_id, desired: Sequence[str], *, created=False) -> dict:
    """Record the catalog entries and item associations that make the item's tags exactly ``desired``.

    Applying a label nobody used before creates it in the shared catalog in the same plan. Nothing here reads or
    changes access: tags are labels. Returns the labels added and removed and ``tags``, the resulting immutable
    snapshots ``{id, name, business_id}`` (see ``tag_snapshots``) read from nothing but this plan and the catalog it
    consulted."""
    wanted = {label_key(label): display_label(label) for label in desired}
    current = {} if created else {row["label_key"]: row for row in uow.connection.execute(
        "SELECT i.*, t.label_key, t.label FROM item_tags i JOIN tags t ON t.id=i.tag_id WHERE i.business_id IS ? AND i.item_type=? "
        "AND i.item_id=?", (business_id, item_type, item_id))}
    added, removed, resulting = [], [], []
    for key, row in sorted(current.items()):
        if key not in wanted:
            removed.append(key)
            builder.record("item_tags", {"item_type": item_type, "item_id": item_id, "tag_id": row["tag_id"]},
                           dict(row), {}, action="delete", business_id=business_id, label=key)
    for key, label in wanted.items():
        if key in current:
            resulting.append({"id": current[key]["tag_id"], "name": current[key]["label"], "business_id": None})
            continue
        entry = uow.connection.execute("SELECT * FROM tags WHERE label_key=?", (key,)).fetchone()
        if entry is None:
            tag_id = new_id()
            builder.record("tags", tag_id, None, {"id": tag_id, "label_key": key, "label": label,
                                                  "created_by": builder.ctx.principal_id, "created_at": builder.at},
                           business_id=None, label=label)
        else:
            tag_id = entry["id"]
            builder.expect(ref("tags", tag_id, None), dict(entry))
        resulting.append({"id": tag_id, "name": label if entry is None else entry["label"], "business_id": None})
        added.append(label)
        builder.record("item_tags", {"item_type": item_type, "item_id": item_id, "tag_id": tag_id}, None,
                       {"item_type": item_type, "item_id": item_id, "tag_id": tag_id,
                        "applied_by": builder.ctx.principal_id, "applied_at": builder.at},
                       business_id=business_id, label=label)
    return {"added": added, "removed": removed, "tags": snapshot_order(resulting)}
