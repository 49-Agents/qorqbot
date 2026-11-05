from pathlib import Path

from qorqut_operations.contracts import (ChangePreview, ChangeSet, ExpectedTarget, FieldChange,
    InvocationContext, PolicyDecision, PreparedChange, PreviewResource, Principal, ResourceRef, StructuredPreview)
from qorqut_operations.storage import Database
from schema_seed import initialized

HUMAN = "00000000-0000-4000-8000-000000000001"
BUSINESS = "00000000-0000-4000-8000-000000000002"
OTHER = "00000000-0000-4000-8000-000000000003"
AGENT = "00000000-0000-4000-8000-000000000004"


def database_at(tmp_path, *, real_initialization=False):
    """A fresh current-schema database; a test of initialization itself passes ``real_initialization=True``."""
    database = initialized(Database(Path(tmp_path) / "state.sqlite3"), real=real_initialization)
    with database.transaction() as uow:
        uow.repository("principals").insert({"id": HUMAN, "kind": "human", "display_name": "Owner"})
        for identity, name in ((BUSINESS, "first"), (OTHER, "second")):
            uow.repository("businesses").insert({"id": identity, "slug": name, "name": name,
                "workspace": str(tmp_path / name), "hq_path": str(tmp_path / name / "hq")})
            uow.repository("business_memberships", business_id=identity).insert({"principal_id": HUMAN, "is_owner": True})
    return database


def context(**kwargs):
    return InvocationContext(Principal(HUMAN, "human"), business_id=BUSINESS,
                             allowed_business_ids=frozenset({BUSINESS}), source="dashboard", **kwargs)


class FixedPolicy:
    def __init__(self, mode="allow", overrides=None):
        self.mode, self.overrides = mode, overrides or {}
        self.calls = []

    def resolve(self, ctx, operation, targets, uow):
        self.calls.append(operation.name)
        return PolicyDecision(self.overrides.get(operation.name, self.mode), "test")


def task_plan(ctx, arguments, *, exists=False, revision=None, operation="tasks_create", extra=()):
    resource = ResourceRef("tasks", arguments["id"], ctx.business_id)
    label = PreviewResource("tasks", arguments["id"], arguments.get("title", "Task"))
    return ChangeSet(operation, 1, ctx.principal_id, "business:" + ctx.business_id, arguments,
        {"kind": "business", "business_id": ctx.business_id},
        (ExpectedTarget(resource, revision=revision, exists=exists),),
        (PreparedChange("record", resource, "insert", {"id": arguments["id"], "title": arguments.get("title", "Task"),
             "status": "todo", "created_by": ctx.principal_id}), *extra),
        ChangePreview("Create task", "", {"kind": "business", "business_id": ctx.business_id}, (label,),
            (StructuredPreview(label, (FieldChange("title", None, arguments.get("title", "Task")),)),)))


def insert_action(uow, business_id, identity, *, summary, actor_id, occurred_at, tag="general", created_at=None,
                  origin="legacy", refs=None):
    """A business-stored, business-operational action row and its one membership, as legacy writers left them."""
    from bazarlyk import local_time
    created_at = created_at or occurred_at
    key = f"business/{business_id}/{identity}"
    uow.connection.execute(
        "INSERT INTO action_logs(business_id,id,tag,summary,refs_json,actor_id,occurred_at,created_at,action_key,category,"
        "action_text,performed_by,recorded_by,origin,occurred_date,occurred_hour,time_precision,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,'business-operational',?,?,?,?,?,?,'instant',?)",
        (business_id, identity, tag, summary, __import__("json").dumps(refs or {}), actor_id, occurred_at, created_at, key,
         summary, None if origin == "legacy" else actor_id, actor_id, origin, local_time.local_date(occurred_at),
         local_time.local_hour(occurred_at), created_at))
    uow.connection.execute("INSERT INTO action_businesses(action_key,business_id,created_at) VALUES (?,?,?)",
                           (key, business_id, created_at))
    return key
