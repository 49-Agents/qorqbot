"""Bounded, read-only evidence access for owner-led learning review."""

import base64
import json

from bazarlyk.application.registry import OperationDescriptor
from bazarlyk.contracts import DomainError, canonical_bytes, canonical_hash
from bazarlyk.domains.common import IDENTITY, PAGE, schema

from .analysis import BUCKETS, SCOPES, TAGS
from .issue_reads import VIEW
from .scope import SCOPE
from .work import STATES


class LearningReview:
    def review_get(self, ctx, args, options, uow):
        if ("learning_id" in args) == ("issue_id" in args):
            raise DomainError("validation_error", "Give exactly one of learning_id or issue_id")
        if "issue_id" in args:
            issue = uow.repository("learning_issues", business_id=ctx.business_id).get(args["issue_id"], required=True)
            if not issue["current_candidate_id"]:
                # Nothing was proposed for this issue (for example a held lineage): its policy, lineage and history only.
                return {"issue": self.issue_view_of(uow, ctx.business_id, issue), "source_kind": "engine_issue_without_review_item",
                        "evidence_status": "not_packaged", "references_complete": False, "complete": True, "next_content_cursor": None}
            args = {**{key: value for key, value in args.items() if key != "issue_id"}, "learning_id": issue["current_candidate_id"]}
        row = uow.repository("learning_candidates", business_id=ctx.business_id).get(args["learning_id"], required=True)
        decision = uow.repository("learning_decisions", business_id=ctx.business_id).get(row["id"])
        item = self.item(row, decision)
        if item.get("engine"):
            bundle = self.engine_bundle(uow, ctx, row, decision, item, args)
            projection = self.reasoning_projection(uow, ctx.business_id, item, self.issue_of_candidate(uow, ctx.business_id, row["id"]))
        else:
            bundle = self.frozen_bundle(uow, ctx, row, item)
            projection = self.reasoning_projection(uow, ctx.business_id, item, None)
        bundle.update(projection)
        serialized = canonical_bytes(bundle).decode()
        binding = canonical_hash([ctx.business_id, row["id"], row["revision"], serialized])
        if args.get("content_cursor"):
            try:
                cursor = json.loads(base64.urlsafe_b64decode(args["content_cursor"].encode()))
                offset = cursor["offset"]
                if cursor["binding"] != binding or type(offset) is not int or not 0 <= offset < len(serialized):
                    raise ValueError
            except (ValueError, TypeError, KeyError) as exc:
                raise DomainError("validation_error", "Review cursor is invalid or the evidence changed") from exc
            content = serialized[offset:offset + 6000]
            following = offset + len(content)
            return {"learning_id": row["id"], "revision": row["revision"], "content": content, "encoding": "json",
                    "complete": following == len(serialized),
                    "next_content_cursor": self.content_cursor(binding, following) if following < len(serialized) else None}
        if len(serialized.encode()) <= 32000:
            return {**bundle, "complete": True, "next_content_cursor": None}
        return {"learning_id": row["id"], "revision": row["revision"], "complete": False,
                "next_content_cursor": self.content_cursor(binding, 0)}

    def frozen_bundle(self, uow, ctx, row, item):
        pending, references, missing_references = [item], set(), 0
        # Reconciliation embeds earlier candidates. Keep each run/session pair:
        # the same session can have different frozen cores in different runs.
        for _ in range(100):
            if not pending:
                break
            candidate = pending.pop()
            for evidence in [candidate.get("evidence", {}), *candidate.get("evidence_more", [])]:
                run, session = evidence.get("run") or candidate.get("run"), evidence.get("session")
                if run and session:
                    references.add((run, session))
                elif evidence:
                    missing_references += 1
            pending.extend(relation.get("earlier", {}) for relation in candidate.get("reconciliations", []))
        references_complete = not pending and not missing_references and len(references) <= 200
        sources, current_run, stored = [], None, None
        for run, session in sorted(references)[:200]:
            if run != current_run:
                stored = uow.repository("learning_runs", business_id=ctx.business_id).get(run)
                current_run = run
            core = stored["result_json"].get("cores", {}).get(session) if stored else None
            sources.append({"run": run, "session": session, "status": "available" if core is not None else "missing",
                            "core": core})
        available = sum(source["status"] == "available" for source in sources)
        evidence_status = ("missing" if not available else "available"
                           if references_complete and available == len(sources) else "partial")
        return {"item": item, "sources": sources, "references_complete": references_complete,
                "evidence_status": evidence_status,
                "source_kind": "frozen_extraction_cores",
                "original_transcripts_included": False}

    def register_review(self):
        self.app.registry.register(OperationDescriptor("learnings_review_list", schema({**PAGE,
            "states": {"type": "array", "minItems": 1, "maxItems": 6, "uniqueItems": True, "items": {"enum": list(STATES)}},
            "bucket": {"enum": list(BUCKETS)}, "tag": {"enum": list(TAGS)}, "scope": {"enum": list(SCOPES)}, "issue_view": VIEW}),
            effect_kind="read", supports_signoff=False, scope_kinds={"business", "agent_workspace", "global"},
            global_capability="read_global_context",
            resolve_namespace=lambda c,a: "business:" + c.business_id if c.business_id else "agent:" + c.agent_id if c.agent_id else "global",
            read=lambda c,a,o,u: self.agenda(c,a,o,u,review=True),
            description="Read compact context and delta review agenda across authorized scopes; defaults to open. Excludes filed and collapsed items. "
                        "Items from a recurring engine issue carry its issue ID, independent-occurrence count and policy; muted (ignored or snoozed) issues "
                        "are left out unless issue_view asks for them, and suppressed counts them. Does not approve anything."))
        self.app.registry.register(OperationDescriptor("learnings_review_get", schema({"learning_id": IDENTITY, "issue_id": IDENTITY,
            "package_revision": {"type": "integer", "minimum": 1, "description": "Read an earlier bound evidence package of an engine review item."},
            "content_cursor": {"type": "string", "maxLength": 2000}}, ()),
            **SCOPE, effect_kind="read", supports_signoff=False, read=self.review_get,
            description="Read one candidate (learning_id) or one recurring issue (issue_id), with the owner decision, reasoning and either linked frozen "
                        "extraction cores or the engine's typed evidence package (quotes, event IDs, hashes, independent versus raw counts, completeness), "
                        "not original transcripts. Sources that are missing or no longer authorized are explicit. When incomplete, follow "
                        "next_content_cursor as content_cursor and concatenate JSON chunks before judging."))
