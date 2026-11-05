"""Shared, explicit acknowledgments of related context for any item (the v1 context reviews).

Reading a relationship or its source never acknowledges it. A review decision names the reviewed item's expected
revision and the relationship, review-state and source versions the reviewer observed, carries an explanation, and
is appended immutably by the engine's ``append_context_review`` prepared effect. An optional edit to the same item
commits in the same transaction under that edit's own operation policy: the nested operation is looked up, bound,
prepared and target-validated through the one application registry, and its changes keep their operation
attribution."""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.application.plans import (PAGE, UUID, PlanBuilder, apply_records, page, ref, require_revision,
                                                 rows, schema)
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.items import UNAVAILABLE, item_revision
from qorqut_operations.relationships.edges import endpoints, related_rows

REVISION = {"type": "integer", "minimum": 1}
TOKEN = {"type": "string", "minLength": 1, "maxLength": 100}
EXPLANATION = {"type": "string", "minLength": 1, "maxLength": 4000}
OBSERVED = {"relationship_id": UUID, "relationship_revision": REVISION,
            "review_revision": {"type": "integer", "minimum": 0}, "source_token": TOKEN}
DECISION = schema({**OBSERVED, "result": {"enum": ["incorporated", "no_change"]}, "explanation": EXPLANATION},
                  (*OBSERVED, "result", "explanation"))


class ContextReviews:
    def __init__(self, relationships):
        self.relationships, self.items = relationships, relationships.items
        self.application = self.items.application

    @property
    def edits(self):
        return self.relationships.ports.review_edits

    def descriptors(self):
        item = self.items.item_schema
        reads = []
        for name, fields, required in (
            ("context_reviews_list", {**PAGE, "item": item, "include_reviewed": {"type": "boolean"}, "include_archived": {"type": "boolean"}}, ("item",)),
            ("context_reviews_history", {**PAGE, "item": item, "relationship_id": UUID}, ("item", "relationship_id")),
        ):
            reads.append(OperationDescriptor(name, schema(fields, required), effect_kind="read", supports_signoff=False,
                description="Read shared pending context or immutable review decisions for this item. Reading does not acknowledge context. Each endpoint reviews related evidence independently of the other endpoint.",
                read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow)))
        edit = schema({"operation": {"enum": list(self.edits)}, "arguments": {"type": "object"}}, ("operation", "arguments"))
        return (*reads,
            OperationDescriptor("context_review", schema({"item": item, "items": {"type": "array", "items": DECISION, "minItems": 1, "maxItems": 50}, "edit": edit}, ("item", "items")),
                description="Explicitly incorporate related context or explain why no change is needed. Supply the item's expected_revision and observed relationship/source/review versions. Optional edit to this same item commits atomically under its normal domain policy. Never acknowledge context merely read or filed.",
                prepare=self.prepare, apply=self.apply, validate_targets=self.validate_prepared),
            OperationDescriptor("context_review_reopen", schema({"item": item, **OBSERVED, "explanation": EXPLANATION}, ("item", *OBSERVED, "explanation")),
                description="Reopen this item's shared review of a relationship, retaining every earlier decision. Requires current item, relationship, review-state and source versions and an explanation.",
                prepare=lambda ctx, args, options, uow: self.prepare(ctx, args, options, uow, reopen=True),
                apply=self.apply, validate_targets=self.validate_prepared))

    @staticmethod
    def state_key(item, edge):
        return {"relationship_id": edge["id"], "item_type": item["type"], "item_id": item["id"]}

    @staticmethod
    def other(item, edge):
        source, target = endpoints(edge)
        if source == item:
            return target
        if target == item:
            return source
        raise DomainError("not_found", "Relationship does not involve this context item")

    @staticmethod
    def attribution(uow, review):
        if not review:
            return None
        actor = uow.repository("principals").get(review["actor_id"])
        return {**review, "actor": {"id": review["actor_id"], "name": actor["display_name"] if actor else "Unknown",
                                    "kind": actor["kind"] if actor else "unknown"}}

    def status(self, ctx, item, edge, uow):
        source = self.other(item, edge)
        state = uow.repository("context_review_states", business_id=ctx.business_id).get(self.state_key(item, edge))
        latest = uow.repository("context_reviews", business_id=ctx.business_id).get(state["review_id"], required=True) if state else None
        value = {"id": edge["id"], "relationship_id": edge["id"], "relationship_revision": edge["revision"],
                 "review_revision": state["revision"] if state else 0, "item": item, "source": source,
                 "source_token": None, "available": False, "pending": True, "latest_review": None,
                 "role": edge["role"], "context": edge["context"], "created_at": edge["created_at"], "updated_at": edge["updated_at"]}
        value["direction"] = "outgoing" if endpoints(edge)[0] == item else "incoming"
        try:
            value["source_item"] = self.items.resolve(ctx, source, uow)
            value["source_token"] = self.items.fingerprint(ctx, source, uow)
        except DomainError as error:
            if error.code not in UNAVAILABLE:
                raise
            return value
        value.update(available=True, latest_review=self.attribution(uow, latest))
        value["pending"] = (not latest or latest["result"] == "pending" or latest["source_token"] != value["source_token"]
                            or latest["relationship_revision"] != edge["revision"])
        return value

    def read(self, operation, ctx, args, uow):
        item = self.items.resolve(ctx, args["item"], uow, allow_busy=operation == "context_reviews_history")
        if operation == "context_reviews_history":
            history = rows(uow, "context_reviews", ctx.business_id, where="item_type=? AND item_id=? AND relationship_id=?",
                           parameters=(item["type"], item["id"], args["relationship_id"]))
            for review in history:
                self.items.resolve(ctx, {"type": review["source_type"], "id": review["source_id"]}, uow, allow_busy=True)
            selected, cursor = page(history, args, ctx.business_id, operation)
            return {"reviews": [self.attribution(uow, review) for review in selected], "next_cursor": cursor}
        edges = {edge["id"]: edge for edge in [*related_rows(uow, ctx.business_id, source=args["item"]),
                                               *related_rows(uow, ctx.business_id, target=args["item"])]}
        found = []
        for edge in edges.values():
            status = self.status(ctx, args["item"], edge, uow)
            if not args.get("include_archived") and status.get("source_item", {}).get("archived"):
                continue
            if status["pending"] or args.get("include_reviewed"):
                found.append(status)
        selected, cursor = page(found, args, ctx.business_id, operation)
        return {"items": selected, "next_cursor": cursor, "item_revision": item["revision"]}

    def nested_plan(self, ctx, args, options, uow):
        edit = args.get("edit")
        if not edit:
            return None
        operation, item = edit["operation"], args["item"]
        spec = self.edits[operation]
        table, row = self.items.resource(ctx, item, uow)
        if (edit["arguments"].get(spec.argument) != item["id"]
                or (spec.resource_type is not None and table != spec.resource_type)
                or (spec.item_kind is not None and item["type"] != spec.item_kind)):
            raise DomainError("validation_error", "An atomic review edit must change the item being reviewed")
        arguments = self.application.registry.bind_arguments(operation, edit["arguments"])
        descriptor = self.application.registry.get(operation)
        return descriptor.prepare(ctx, arguments, options, uow)

    def prepare(self, ctx, args, options, uow, reopen=False):
        operation = "context_review_reopen" if reopen else "context_review"
        builder = PlanBuilder(ctx, operation, args, "Review related context", clock=self.relationships.clock)
        self.items.resolve(ctx, args["item"], uow, builder)
        _, row = self.items.resource(ctx, args["item"], uow)
        require_revision({"revision": item_revision(row)}, options.expected_revision)
        nested = self.nested_plan(ctx, args, options, uow)
        if nested:
            for target in nested.expected_targets:
                if target.resource in builder.targets and target != builder.targets[target.resource]:
                    raise DomainError("invalid_plan", "Atomic review edits disagree on the current item")
                builder.targets[target.resource] = target
            builder.changes.extend(replace(change, operation_name=change.operation_name or nested.operation_name) for change in nested.changes)
            builder.resources.extend(nested.preview["resources"])
            builder.previews.extend(nested.preview["changes"])
            builder.effects.extend(nested.preview["effects"])
        decisions = ([{**{key: args[key] for key in OBSERVED}, "result": "pending", "explanation": args["explanation"]}]
                     if reopen else args["items"])
        if len({decision["relationship_id"] for decision in decisions}) != len(decisions):
            raise DomainError("validation_error", "Review each relationship once per batch")
        for decision in decisions:
            edge = uow.repository("relationships", business_id=ctx.business_id).get(decision["relationship_id"], required=True)
            require_revision(edge, decision["relationship_revision"])
            source = self.other(args["item"], edge)
            token = self.items.fingerprint(ctx, source, uow, builder)
            if token != decision["source_token"]:
                raise DomainError("revision_conflict", "Related content changed; review the current source first")
            current = self.status(ctx, args["item"], edge, uow)
            if current["review_revision"] != decision["review_revision"]:
                raise DomainError("revision_conflict", "Another participant changed this shared review")
            if reopen == current["pending"]:
                raise DomainError("conflict", "This context is already pending" if reopen else "This context was already reviewed; reopen it explicitly")
            if not decision["explanation"].strip():
                raise DomainError("validation_error", "Explain the review decision")
            builder.expect(ref("relationships", edge["id"], ctx.business_id), edge)
            identity = new_id()
            builder.record("context_reviews", identity, None, {"id": identity, "relationship_id": edge["id"],
                "item_type": args["item"]["type"], "item_id": args["item"]["id"], "source_type": source["type"], "source_id": source["id"],
                "source_token": token, "relationship_revision": edge["revision"], "result": decision["result"], "explanation": decision["explanation"],
                "actor_id": ctx.principal_id, "prepared_at": builder.at}, action="append_context_review")
            key = self.state_key(args["item"], edge)
            state = uow.repository("context_review_states", business_id=ctx.business_id).get(key)
            builder.record("context_review_states", key, state,
                           {"review_id": identity} if state else {**key, "review_id": identity, "created_at": builder.at, "updated_at": builder.at})
        return builder.build()

    def validate_prepared(self, ctx, plan, uow):
        for change in plan.changes:
            if change.resource.resource_type != "context_reviews":
                continue
            review = change.values
            source = {"type": review["source_type"], "id": review["source_id"]}
            if self.items.fingerprint(ctx, source, uow) != review["source_token"]:
                raise DomainError("revision_conflict", "Related source changed before review was applied")
        edit = plan.arguments.get("edit")
        if edit:
            descriptor = self.application.registry.get(edit["operation"])
            if descriptor.validate_targets:
                nested = replace(plan, operation_name=edit["operation"], arguments=edit["arguments"],
                    changes=tuple(change for change in plan.changes if change.resource.resource_type not in {"context_reviews", "context_review_states"}))
                descriptor.validate_targets(ctx, nested, uow)

    def apply(self, ctx, plan, uow):
        edit = plan.arguments.get("edit")
        if edit and self.edits[edit["operation"]].apply_through_domain:
            self.application.registry.get(edit["operation"]).apply(
                ctx, replace(plan, operation_name=edit["operation"], arguments=edit["arguments"]), uow)
        else:
            apply_records(ctx, plan, uow)
        return {"item": self.items.resolve(ctx, plain(plan.arguments["item"]), uow, allow_busy=True),
                "review_ids": [change.resource.resource_id for change in plan.changes if change.resource.resource_type == "context_reviews"]}
