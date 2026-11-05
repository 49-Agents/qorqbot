"""``tags_list`` and ``tags_create``: the shared label catalog for Tasks and Actions.

Both operate on the installation-wide catalog, so they accept the caller's own namespace (a selected business, or
Global for a global agent) and authorize through the normal v1 policy for that namespace. The catalog reveals label
names only. It carries no usage counts and no item references, so a tag query can never disclose how many items an
unauthorized caller may not read, or what they contain; filtering items by tag happens after each caller's own
access is resolved, in the owning item operation."""

from __future__ import annotations

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.plans import PAGE, PlanBuilder, apply_records, page, ref, schema, utc_clock
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.tags.catalog import LABEL_SCHEMA, catalog_row, display_label, label_key

TAG_ENTRIES = (CatalogEntry("tags_list", False, "read"), CatalogEntry("tags_create", True, "records"))
SCOPE_KINDS = frozenset({"business", "global"})


def tag_namespace(ctx, arguments):
    """The caller's own namespace: its selected business, otherwise Global (which needs a global capability)."""
    return "business:" + ctx.business_id if ctx.business_id else "global"


def tag_view(row) -> dict:
    return {"id": row["id"], "label": row["label"], "created_at": row["created_at"]}


class TagOperations:
    def __init__(self, *, clock=utc_clock):
        self.clock = clock

    def contribution(self) -> CatalogContribution:
        return CatalogContribution("tags", TAG_ENTRIES, self.descriptors())

    def descriptors(self):
        common = dict(scope_kinds=SCOPE_KINDS, global_capability="read_global_context", resolve_namespace=tag_namespace)
        return (
            OperationDescriptor("tags_list", schema({**PAGE, "query": {"type": "string", "maxLength": 64}}),
                effect_kind="read", supports_signoff=False, read=self.list_tags, **common,
                description="List the shared tag catalog: every label ever used on a Task or Action, in the form it was "
                            "first created. Tags are labels only: they grant no access and the catalog shows no item "
                            "contents or counts."),
            OperationDescriptor("tags_create", schema({"label": LABEL_SCHEMA, "rationale": {"type": "string", "minLength": 1, "maxLength": 2000}},
                                                      ("label",)),
                prepare=self.prepare, apply=self.apply, **common,
                description="Add a label to the shared tag catalog (or find the existing one: labels equal by normalized "
                            "text are one entry). Labels cannot be renamed or merged. Applying a tag to a Task or "
                            "Action also creates it, so this is only needed to prepare a label in advance."))

    def list_tags(self, ctx, arguments, options, uow):
        query = label_key(arguments["query"]) if arguments.get("query") else None
        rows = [row for row in uow.connection.execute("SELECT * FROM tags ORDER BY label_key, id")
                if query is None or query in row["label_key"]]
        selected, cursor = page([{**dict(row), "sort": (row["label_key"], row["id"])} for row in rows], arguments, None,
                                "tags_list", key=lambda row: row["sort"], reverse=False)
        return {"tags": [tag_view(row) for row in selected], "next_cursor": cursor}

    def prepare(self, ctx, arguments, options, uow):
        label = display_label(arguments["label"])
        builder = PlanBuilder(ctx, "tags_create", arguments, "Add tag " + label, clock=self.clock)
        existing = catalog_row(uow, label)
        if existing is not None:
            builder.expect(ref("tags", existing["id"], None), dict(existing))
            builder.effects.append("The label already exists in the shared tag catalog; nothing changes.")
            return builder.build()
        tag_id = new_id()
        builder.record("tags", tag_id, None, {"id": tag_id, "label_key": label_key(label), "label": label,
                                              "created_by": ctx.principal_id, "created_at": builder.at},
                       business_id=None, label=label)
        builder.effects.append("Adds the label to the shared catalog. A label grants no access and owns nothing.")
        return builder.build()

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        row = catalog_row(uow, plan.arguments["label"])
        if row is None:
            raise DomainError("invalid_plan", "The prepared label is missing")
        return {"tag": tag_view(row)}
