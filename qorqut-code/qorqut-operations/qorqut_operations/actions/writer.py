"""The one writer of Actions: a record, a correction or a withdrawal is a row, its immutable revision and its admitted input.

Every accepted change appends exactly one ``action_revisions`` row in the same prepared plan as the Action row it
describes. ``settle`` then runs in the applying transaction, after the records are written and before it commits: it
hands each new revision (the exact stored row, never a later read of the Action) to the required ``on_revision`` port
and announces the change to the Action's followers. Any operation that writes Actions through ``new_action`` must settle
its plan with the composition's one ``on_revision``; the Action operations do, and so must any host caller of ``ActionWriteback``.
"""

from __future__ import annotations

from qorqut_operations.actions import models
from qorqut_operations.actions.ports import require_on_revision
from qorqut_operations.actions.scope import links
from qorqut_operations.application.plans import UNSET
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.discussion.events import emit_event
from qorqut_operations.discussion.follows import ThreadItem, auto_follow, followers
from qorqut_operations.tags.catalog import distinct_labels, item_labels, plan_item_tags, tag_snapshots

SOURCE_TYPE = "actions"
EVENT_TYPES = {"add": "created", "edit": "updated", "remove": "withdrawn"}


def labels_of(values) -> list[str]:
    """Tags as the shared catalog stores them (each label once, by normalized key)."""
    return distinct_labels(values)


def _revision_row(builder, key, revision, operation, state, *, storage, performer, provenance):
    values = {"business_id": storage, "action_key": key, "revision": revision, "operation": operation,
              "snapshot_json": state, "recorded_by": builder.ctx.principal_id, "performed_by": performer,
              "provenance_json": provenance, "created_at": builder.at}
    builder.record("action_revisions", {"action_key": key, "revision": revision}, None, values, business_id=storage,
                   label=f"{key}:{revision}")


def new_action(builder, uow, *, text, category, businesses, occurrence, origin, refs=None, tags=(), related=(), performer=UNSET,
               identity=None, operation_name=None, provenance=None) -> dict:
    """Plan a new Action: its row, business links, tags and revision 1. Returns the new row's values."""
    storage = builder.ctx.business_id
    text = models.validate_text(text, origin)
    identity = identity or new_id()
    key = models.action_key(storage, identity)
    values = {"id": identity, "action_key": key, "category": category, "action_text": text, "origin": origin,
              "performed_by": builder.ctx.principal_id if performer is UNSET else performer,
              "recorded_by": builder.ctx.principal_id, "occurred_at": occurrence["occurred_at"],
              "occurred_date": occurrence["real_date"], "occurred_hour": occurrence["real_hour"],
              "time_precision": occurrence["time_precision"], "time_zone": occurrence["time_zone"],
              "refs_json": models.refs(refs or {}), "related_objects_json": models.related_objects(related),
              "withdrawn": 0, "created_at": builder.at, "updated_at": builder.at}
    builder.record("action_logs", identity, None, values, operation_name=operation_name, label=models.one_line(text))
    for business_id in businesses:
        link = {"action_key": key, "business_id": business_id}
        # A business-stored action links only its own business; Global-stored links are Global membership records.
        builder.record("action_businesses", link, None, {**link, "created_at": builder.at},
                       business_id=storage if storage == business_id else None, operation_name=operation_name, label=business_id)
    labels = labels_of(tags)
    planned = plan_item_tags(builder, uow, "action", identity, storage, labels, created=True)
    state = models.snapshot(values, businesses=businesses, tags=planned["tags"], operation="add")
    _revision_row(builder, key, 1, "add", state, storage=storage, performer=values["performed_by"], provenance=provenance or {})
    return {**values, "revision": 1}


def change_action(builder, uow, before: dict, *, operation, changes: dict, businesses=None, tags=None, provenance=None) -> dict:
    """Plan a correction (``edit``) or withdrawal (``remove``) of an existing Action as its next revision."""
    key, storage = before["action_key"], before["business_id"]
    if before["withdrawn"]:
        raise DomainError("invalid_transition", "Withdrawn Actions retain history; later activity requires a new Action")
    current_links = links(uow, key)
    current_labels = item_labels(uow, storage, "action", before["id"])
    after_links = current_links if businesses is None else list(businesses)
    after_labels = current_labels if tags is None else labels_of(tags)
    row = {**before, **changes}
    if operation == "edit":
        if "action_text" in changes:
            row["action_text"] = changes["action_text"] = models.validate_text(changes["action_text"], models.origin_for(builder.ctx))
        unchanged = (all(before[column] == value for column, value in changes.items())
                     and sorted(after_links) == sorted(current_links)
                     and sorted(label.casefold() for label in after_labels) == sorted(label.casefold() for label in current_labels))
        if unchanged:
            raise DomainError("validation_error", "The correction changes nothing; supply a different value")
    values = {**changes, "updated_at": builder.at}
    builder.record("action_logs", before["id"], before, values, business_id=storage, label=models.one_line(row["action_text"]))
    frozen = tag_snapshots(uow, storage, "action", before["id"]) if tags is None else plan_item_tags(
        builder, uow, "action", before["id"], storage, after_labels)["tags"]
    for business_id in sorted(set(current_links) - set(after_links)):
        link = {"action_key": key, "business_id": business_id}
        builder.record("action_businesses", link, uow.repository("action_businesses").get(link), {}, action="delete",
                       business_id=storage if storage == business_id else None, label=business_id)
    for business_id in [value for value in after_links if value not in current_links]:
        link = {"action_key": key, "business_id": business_id}
        builder.record("action_businesses", link, None, {**link, "created_at": builder.at},
                       business_id=storage if storage == business_id else None, label=business_id)
    state = models.snapshot(row, businesses=after_links, tags=frozen, operation=operation)
    _revision_row(builder, key, before["revision"] + 1, operation, state, storage=storage, performer=before["performed_by"],
                  provenance=provenance or {})
    return {**row, "revision": before["revision"] + 1}


def settle(ctx, plan, uow, *, on_revision) -> list[dict]:
    """Admit and announce every Action revision a just-applied plan wrote. Runs inside the applying transaction.

    ``on_revision(ctx, exact_revision, uow) -> input_id`` is the composition's required port; it receives the stored
    revision row and its result is reported as ``input_id``."""
    on_revision = require_on_revision(on_revision)
    settled = []
    for change in plan.changes:
        if change.resource.resource_type != "action_revisions" or change.action != "insert":
            continue
        values = plain(change.values)
        revision = uow.repository("action_revisions", business_id=values["business_id"]).get(
            {"action_key": values["action_key"], "revision": values["revision"]}, required=True)
        input_id = on_revision(ctx, revision, uow)
        action = uow.connection.execute("SELECT * FROM action_logs WHERE action_key=?", (revision["action_key"],)).fetchone()
        item = ThreadItem(action["business_id"], "action", action["id"])
        if revision["operation"] == "add":
            auto_follow(uow, item, [action["recorded_by"]], "creator")
        event = emit_event(uow, business_id=action["business_id"], source_type=SOURCE_TYPE, source_id=action["action_key"],
                           source_revision=revision["revision"], actor_id=ctx.principal_id,
                           event_type=EVENT_TYPES[revision["operation"]], summary=models.one_line(action["action_text"]),
                           optional=followers(uow, item))
        settled.append({"action_key": revision["action_key"], "revision": revision["revision"], "input_id": input_id,
                        "event_id": event})
    return settled


__all__ = ["EVENT_TYPES", "SOURCE_TYPE", "change_action", "labels_of", "new_action", "settle"]
