"""Follow state of an item discussion: who has chosen, or been given, an optional subscription.

Creators, assignees and commenters (and a relayed comment's represented speaker) follow by default. That default is
only ever *inserted*; it never touches an existing row, so an explicit unfollow stays a tombstone until the same
principal explicitly follows again. A follow is an optional-delivery preference. It is neither a read grant nor a
responsibility: required deliveries are computed elsewhere and ignore this table."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.discussion.events import eligible_agents
from qorqut_operations.discussion.schema import ITEM_TYPES

REASONS = ("creator", "assignee", "commenter", "represented", "explicit")


@dataclass(frozen=True)
class ThreadItem:
    """The record a discussion belongs to: its owning namespace (``business_id`` None = Global), kind and identity."""

    business_id: str | None
    item_type: str
    item_id: str

    def __post_init__(self):
        if self.item_type not in ITEM_TYPES or not isinstance(self.item_id, str) or not self.item_id:
            raise DomainError("validation_error", "A discussion belongs to a Task or an Action")

    @property
    def namespace(self) -> str:
        return "global" if self.business_id is None else "business:" + self.business_id

    @property
    def thread_id(self) -> str:
        """A stable identity derived from the item, so a thread cannot exist apart from it."""
        return str(uuid5(NAMESPACE_URL, f"qorqut-thread:{self.namespace}:{self.item_type}:{self.item_id}"))


def can_follow(uow, item: ThreadItem, principal_id: str) -> bool:
    """Whether the principal is a registered, active participant of the item's namespace."""
    principal = uow.connection.execute("SELECT kind, active FROM principals WHERE id=?", (principal_id,)).fetchone()
    if principal is None or not principal["active"]:
        return False
    if principal["kind"] == "agent":
        return bool(eligible_agents(uow, item.business_id, [principal_id]))
    if principal["kind"] != "human":
        return False
    if item.business_id is None:
        owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
        return bool(owner and owner["owner_principal_id"] == principal_id)
    member = uow.connection.execute("SELECT active FROM business_memberships WHERE business_id=? AND principal_id=?",
                                    (item.business_id, principal_id)).fetchone()
    return bool(member and member["active"])


def auto_follow(uow, item: ThreadItem, principal_ids, reason: str) -> list[str]:
    """Give each eligible principal its default follow. Existing rows, tombstones included, are left exactly as they are."""
    uow.require_write()
    if reason not in REASONS or reason == "explicit":
        raise DomainError("validation_error", "Unknown automatic follow reason")
    now, added = utc_now(), []
    for principal_id in sorted({p for p in principal_ids if p}):
        if not can_follow(uow, item, principal_id):
            continue
        cursor = uow.connection.execute(
            "INSERT OR IGNORE INTO thread_follows(business_id,item_type,item_id,principal_id,following,explicit,reason,"
            "created_at,updated_at) VALUES(?,?,?,?,1,0,?,?,?)",
            (item.business_id, item.item_type, item.item_id, principal_id, reason, now, now))
        if cursor.rowcount:
            added.append(principal_id)
    return added


def set_follow(uow, item: ThreadItem, principal_id: str, following: bool) -> dict:
    """The principal's own choice. Unfollowing leaves a sticky tombstone; following replaces it."""
    uow.require_write()
    if not can_follow(uow, item, principal_id):
        raise DomainError("forbidden", "Only an active participant of this namespace can follow its items")
    now = utc_now()
    where = "business_id IS ? AND item_type=? AND item_id=? AND principal_id=?"
    key = (item.business_id, item.item_type, item.item_id, principal_id)
    if uow.connection.execute(f"SELECT 1 FROM thread_follows WHERE {where}", key).fetchone():
        uow.connection.execute(f"UPDATE thread_follows SET following=?,explicit=1,reason='explicit',updated_at=? WHERE {where}",
                               (int(following), now, *key))
    else:
        uow.connection.execute(
            "INSERT INTO thread_follows(business_id,item_type,item_id,principal_id,following,explicit,reason,created_at,"
            "updated_at) VALUES(?,?,?,?,?,1,'explicit',?,?)", (*key[:3], principal_id, int(following), now, now))
    return follow_state(uow, item, principal_id)


def follow_state(uow, item: ThreadItem, principal_id: str) -> dict:
    row = uow.connection.execute(
        "SELECT following, explicit, reason FROM thread_follows WHERE business_id IS ? AND item_type=? AND item_id=? "
        "AND principal_id=?", (item.business_id, item.item_type, item.item_id, principal_id)).fetchone()
    return {"following": bool(row and row["following"]), "explicit": bool(row and row["explicit"]),
            "reason": row["reason"] if row else None}


def followers(uow, item: ThreadItem) -> list[str]:
    return [row[0] for row in uow.connection.execute(
        "SELECT principal_id FROM thread_follows WHERE business_id IS ? AND item_type=? AND item_id=? AND following=1 "
        "ORDER BY principal_id", (item.business_id, item.item_type, item.item_id))]
