"""Related-work subscriptions: following a group Task together with the work directly related to it.

``inbox_subscribe`` on a Task with ``include_related`` follows the Task itself (the discussion's sticky follow) and also
keeps an ``inbox_subscriptions`` row, which makes the subscriber an optional recipient of events about the items directly
related to that group Task: its child Tasks (``parent_id``) and the items it shares a relationship with, in either
direction. It is one hop and never recursive (a child's children, or a related item's links, are not followed), it
includes links added later because the relationship is read when the event is made, and it never makes a delivery
required or grants access: reads recheck the subscriber's current authorization. The Task's follow stays authoritative, so
unfollowing the Task also stops its related-work events until it is followed again. Dependency and assignment edges are
not group membership and are not related work."""

from __future__ import annotations

# Edges that are dependency or responsibility, not membership of a group.
EXCLUDED_ROLES = ("assigned_to", "blocked_by")


def related(uow, business_id, group_id: str, item_type: str, identity: str) -> bool:
    """Whether ``identity`` is directly related work of the group Task ``group_id`` (child, or linked either way)."""
    if group_id == identity and item_type == "task":
        return False
    if item_type == "task" and uow.connection.execute(
            "SELECT 1 FROM tasks WHERE business_id IS ? AND id=? AND parent_id=?", (business_id, identity, group_id)).fetchone():
        return True
    return uow.connection.execute(
        "SELECT 1 FROM relationships WHERE business_id IS ? AND role NOT IN (" + ",".join("?" for _ in EXCLUDED_ROLES) + ") AND "
        "((from_type='task' AND from_id=? AND to_type=? AND to_id=?) OR (to_type='task' AND to_id=? AND from_type=? AND from_id=?)) LIMIT 1",
        (business_id, *EXCLUDED_ROLES, group_id, item_type, identity, group_id, item_type, identity)).fetchone() is not None


def following(uow, business_id, agent_id: str, group_id: str) -> bool:
    return uow.connection.execute(
        "SELECT 1 FROM thread_follows WHERE business_id IS ? AND item_type='task' AND item_id=? AND principal_id=? AND following=1",
        (business_id, group_id, agent_id)).fetchone() is not None


def routed(uow, subscription, item_type: str, identity: str) -> bool:
    """Whether the related-work ``subscription`` of a group Task covers the event about ``identity``."""
    business_id, group_id = subscription["business_id"], subscription["source_id"]
    return (bool(subscription["include_related"]) and following(uow, business_id, subscription["agent_id"], group_id)
            and related(uow, business_id, group_id, item_type, identity))


def subscribers(uow, business_id, item_type: str, identity: str) -> set[str]:
    """The agents whose related-work subscription to a group Task covers ``identity``."""
    return {row["agent_id"] for row in uow.connection.execute(
        "SELECT * FROM inbox_subscriptions WHERE business_id IS ? AND source_type='tasks' AND include_related=1", (business_id,))
        if routed(uow, row, item_type, identity)}


__all__ = ["EXCLUDED_ROLES", "following", "related", "routed", "subscribers"]
