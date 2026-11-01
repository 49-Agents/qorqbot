"""Learning candidates, owner triage and frozen extraction outputs in SQLite."""

from dataclasses import replace

from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.application.plans import IDENTITY, PAGE, TEXT, UUID, apply_records, page, prepare_job, require_revision, rows, schema
from .analysis import BUCKETS, SCOPES, TAGS, LearnError, validate_edit, validate_item
from .scope import PlanBuilder, SCOPE, namespace
from .work import LearningWork, STATES, record_history
from .review import LearningReview
from .issue_reads import VIEW, LearningIssueReads, in_view
from .issues import LearningIssues, summary as issue_summary
from .intake import snapshot as intake_snapshot

EXTRACT_ARGUMENTS = schema({"max_usd": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
    "supersession_mode": {"enum": ["preview", "apply"], "description": "Preview cross-day unresolved replacements, or automatically reject evidence-backed superseded findings. Omission preserves legacy reconciliation."},
    "passes": {"type": "integer", "minimum": 1, "maximum": 5},
    "max_sessions": {"type": "integer", "minimum": 1, "maximum": 100}, "agent_id": UUID,
    "retry_failed": {"type": "boolean", "description": "Explicitly retry paused Global transcript snapshots; normal routines leave this false."}})


class LearningOperations(LearningIssueReads, LearningIssues, LearningWork, LearningReview):
    def __init__(self, application, programs, authority=None, *, documents=None):
        self.app, self.programs, self.authority = application, programs, authority
        self.documents = documents

    @staticmethod
    def item(row, decision=None):
        value = {**plain(row["candidate_json"]), "id": row["id"], "revision": row["revision"]}
        if decision:
            value.update(plain(decision["changes_json"]))
            value.update(state=decision["state"], decision={"state": decision["state"], "note": decision["feedback"],
                "actor": decision["decided_by"], "created": decision["updated_at"]})
        else:
            value["state"] = "open"
        value.update(business_id=row["business_id"], origin_scope="business" if row["business_id"] else "global", agent_note=row.get("agent_note", ""),
                     targets=plain(row.get("work_targets_json", [])), outcome=plain(row.get("work_outcome_json", {})),
                     filing=plain(row.get("filing_json", {})))
        if row.get("work_state"):
            value["state"] = row["work_state"]
        return value

    def list(self, ctx, args, options, uow):
        decisions = {x["candidate_id"]: x for x in rows(uow, "learning_decisions", ctx.business_id)}
        values = [self.item(x, decisions.get(x["id"])) for x in rows(uow, "learning_candidates", ctx.business_id)]
        pending = {row["resource_id"]: {"state": row["state"]} for row in uow.connection.execute(
            "SELECT c.resource_id, r.state FROM resource_claims c JOIN apply_receipts r ON r.id=c.apply_id "
            "WHERE c.target_namespace=? AND c.resource_type='learning_candidates' AND r.operation_name IN ('learnings_review','learnings_edit','learnings_decide')",
            (namespace(ctx),))}
        for value in values:
            if value["id"] in pending:
                value["intake_pending"] = pending[value["id"]]
        briefs = self.issue_briefs(uow, ctx.business_id)
        for value in values:
            if value["id"] in briefs:
                value["issue"] = briefs[value["id"]]
        values = [x for x in values if not x["filing"] and not x.get("reconciled_into")]
        view = args.get("issue_view", "active")
        suppressed = self.issue_counts([x.get("issue") for x in values])
        values = [x for x in values if in_view(x.get("issue"), view)]
        counts = {state: sum(x["state"] == state for x in values) for state in STATES}
        if "state" not in args:
            values = [x for x in values if x["state"] != "implemented"]
        for name in ("state", "scope", "bucket", "tag"):
            if name in args:
                values = [x for x in values if x.get(name) == args[name]]
        items, cursor = page(values, args, ctx.business_id, "learnings_list", key=lambda x: (x.get("created", ""), x["id"]))
        return {"items": items, "counts": counts, "suppressed": suppressed, "next_cursor": cursor}

    def get(self, ctx, args, options, uow):
        row = uow.repository("learning_candidates", business_id=ctx.business_id).get(args["learning_id"], required=True)
        decision = uow.repository("learning_decisions", business_id=ctx.business_id).get(row["id"])
        item = self.item(row, decision)
        cores = {}
        evidence = [item.get('evidence', {}), *item.get('evidence_more', [])]
        pending_relations = list(item.get('reconciliations', []))
        for _ in range(100):
            if not pending_relations:
                break
            old = pending_relations.pop().get('earlier', {})
            evidence.extend([old.get('evidence', {}), *old.get('evidence_more', [])])
            pending_relations.extend(old.get('reconciliations', []))
        run_ids = {e.get('run') or item.get('run') for e in evidence}
        sessions = {e.get('session') for e in evidence}
        for run_id in sorted(r for r in run_ids if r)[:50]:
            stored = uow.repository('learning_runs', business_id=ctx.business_id).get(run_id)
            if stored:
                cores.update({key: value for key, value in stored['result_json'].get('cores', {}).items() if key in sessions})
        result = {"item": item, "cores": cores}
        issue = self.issue_of_candidate(uow, ctx.business_id, row["id"])
        if issue:
            result["issue"] = self.issue_view_of(uow, ctx.business_id, issue)
        result.update(self.reasoning_projection(uow, ctx.business_id, item, issue))
        return result

    def prepare_keep_both(self, ctx, args, options, uow):
        row = uow.repository('learning_candidates', business_id=ctx.business_id).get(args['learning_id'], required=True)
        require_revision(row, options.expected_revision)
        value = plain(row['candidate_json'])
        builder = PlanBuilder(ctx, 'learnings_keep_both', args, 'Keep related learning candidates separately')
        changed = False
        for relation in value.get('reconciliations', []):
            if not relation.get('automatic') or relation.get('kept_both'):
                continue
            old = relation['earlier']
            existing = uow.repository('learning_candidates', business_id=ctx.business_id).get(old['id'])
            if existing:
                restored = plain(existing['candidate_json'])
                if relation.get('supersession'):
                    decision = uow.repository('learning_decisions', business_id=ctx.business_id).get(old['id'])
                    if (restored.get('superseded_by') != row['id'] or not decision
                            or decision['state'] != 'rejected' or decision['revision'] != 1):
                        raise DomainError('revision_conflict', 'Superseded learning has since been reviewed')
                    restored.pop('superseded_by')
                    builder.record('learning_decisions', old['id'], decision,
                        {'state': 'open', 'feedback': None, 'decided_by': ctx.principal_id})
                elif restored.get('reconciled_into') != row['id']:
                    raise DomainError('revision_conflict', 'Earlier learning reconciliation has changed')
                restored.pop('reconciled_into', None)
            else:
                restored = {k:v for k,v in old.items() if k not in {'reviewed','revision','state'}}
                stored = uow.repository('learning_runs', business_id=ctx.business_id).get(old['run'], required=True)
                core = stored['result_json']['cores'][old['evidence']['session']]
                validate_item(restored, core.get('markdown'), source_core=core)
            restored['reconciliation_reviewed'] = True
            builder.record('learning_candidates', old['id'], existing, {**({'id':old['id']} if existing is None else {}), 'candidate_json':restored})
            if relation.get('supersession') and existing:
                record_history(builder, old['id'], self.item(existing, decision),
                    self.item({**existing, 'candidate_json': restored}, {**decision, 'state': 'open',
                        'feedback': None, 'decided_by': ctx.principal_id, 'updated_at': builder.at}))
            else:
                record_history(builder, old['id'], existing['candidate_json'] if existing else {}, restored)
            relation['kept_both'] = True
            changed = True
        if not changed:
            raise DomainError('invalid_transition', 'No collapsed learning remains to restore')
        builder.record('learning_candidates', row['id'], row, {'candidate_json':value})
        record_history(builder, row['id'], row['candidate_json'], value)
        return builder.build()

    def prepare_extract(self, ctx, args, options, uow):
        if args.get('supersession_mode') == 'apply' and ctx.principal_kind != 'human':
            raise DomainError('forbidden', 'Only the owner may enable automatic supersession for an extraction')
        if args.get("agent_id"):
            if ctx.business_id is not None:
                member = uow.repository('business_memberships', business_id=ctx.business_id).get(args['agent_id'])
                if not member or not member['active']:
                    raise DomainError('forbidden', 'Selected transcript agent is outside this business')
            profile = uow.repository('agent_profiles', business_id=ctx.business_id).get(args['agent_id'])
            if not profile or (ctx.business_id is None and profile['profile_json'].get('role', {}).get('id') == 'learning-applier'):
                raise DomainError('forbidden', 'Selected transcript agent is outside this learning workspace or excluded')
        builder = PlanBuilder(ctx, "learnings_extract", args, "Extract learning candidates")
        prepare_job(builder, uow, "learnings.extract", args, programs=self.programs)
        return builder.build()

    def prepare_decision(self, name, ctx, args, options, uow):
        delegated = name == "learnings_decide"
        if delegated and (ctx.principal_kind != "agent" or not ctx.role_artifact_ref
                          or ctx.role_artifact_ref.id != "learning-applier"):
            raise DomainError("forbidden", "Only Learning Applier can record an instructed learning decision")
        row = uow.repository("learning_candidates", business_id=ctx.business_id).get(args["learning_id"], required=True)
        if row['candidate_json'].get('reconciled_into'):
            raise DomainError('invalid_transition', 'This learning was combined with its answer; use Keep both separately before reviewing it')
        previous = uow.repository("learning_decisions", business_id=ctx.business_id).get(row["id"])
        changes = plain(previous["changes_json"]) if previous else {}
        current = self.item(row, previous)
        if delegated:
            require_revision(row, options.expected_revision)
            if current["state"] != "open" or current["filing"]:
                raise DomainError("invalid_transition", "Instructed decisions require an open, unfiled learning")
        state = previous_state = previous["state"] if previous else "open"
        feedback = previous["feedback"] if previous else None
        if "feedback" in args and "reasoning" in args and (args["feedback"] or None) != (args["reasoning"] or None):
            raise DomainError("validation_error", "feedback is the legacy name of reasoning; give one of them, or make them equal")
        # The owner's own explanation. It is stored in the existing decision note; agent rationale is kept apart.
        supplied = "feedback" in args or "reasoning" in args
        note = (args["reasoning"] if "reasoning" in args else args.get("feedback")) or None
        candidate = plain(row["candidate_json"])
        if current["filing"]:
            if (name == "learnings_review" and args["state"] == "accepted"
                    and (not supplied or note == feedback)):
                return PlanBuilder(ctx, name, args, "Context intake already committed").build()
            raise DomainError("validation_error", "This learning has been handed to Context; make further corrections there")
        claim = uow.repository("resource_claims").get({"target_namespace": namespace(ctx),
            "resource_type": "learning_candidates", "resource_id": row["id"]})
        if claim:
            raise DomainError("busy", "This learning has an unfinished context intake; resume the existing intake", retryable=True,
                              details={"intake_receipt_id": claim["apply_id"]})
        require_revision(row, options.expected_revision)
        if name == "learnings_edit":
            if row.get("work_state"):
                raise DomainError("validation_error", "Learning content cannot change during or after work")
            try:
                validate_edit(current, args["changes"])
            except LearnError as exc:
                raise DomainError("validation_error", str(exc)) from exc
            changes.update(args["changes"])
            candidate["edits"] = list(candidate.get("edits", [])) + [{"set": args["changes"], "actor": ctx.principal_id}]
        else:
            state = args["state"]
            if state == 'open' and candidate.get('superseded_by'):
                candidate.pop('superseded_by')
                candidate['reconciliation_reviewed'] = True
                if not supplied:
                    feedback = None
            if supplied:
                feedback = note
            if state != (previous["state"] if previous else "open"):
                if row.get("work_state") or (state == "open" and current["state"] not in {"accepted", "rejected"}):
                    raise DomainError("validation_error", "Invalid owner review transition")
                if state != "open" and current["state"] != "open":
                    raise DomainError("validation_error", "Revert the decision before reviewing it again")
            elif not supplied and not (state == "accepted" and current["bucket"] == "context"):
                raise DomainError("validation_error", "Learning already has this decision")
        builder = PlanBuilder(ctx, name, args, "Edit learning" if name == "learnings_edit" else "Review learning")
        updates = {"candidate_json": candidate, "updated_at": builder.at}
        values = {"state": state, "feedback": feedback, "changes_json": changes, "decided_by": ctx.principal_id}
        if previous is None:
            values["candidate_id"] = row["id"]
        after_decision = {**(previous or {}), **values, "updated_at": builder.at}
        after = self.item({**row, **updates, "revision": row["revision"] + 1}, after_decision)
        if state == "accepted" and after["bucket"] == "context":
            if self.documents is None:
                raise DomainError("operation_unavailable", "Context intake is not installed")
            from bazarlyk.domains.documents.operations import append_plan
            body, digest = intake_snapshot(after)
            upload, upload_id, job_id = self.documents.prepare_learning_intake(ctx,
                {"body": body, "title": after["tag"].title() + ": " + after["text"][:120], "source": "accepted-learning"}, uow,
                metadata={"format": "learning-v1", "learning_tag": after["tag"], "source_snapshot_hash": digest},
                source_refs=[{"resource_type": "learning_candidates", "resource_id": row["id"], "business_id": ctx.business_id}])
            if delegated or ctx.business_id is None:
                # Filing this frozen candidate is intrinsic to acceptance, not
                # a grant to upload arbitrary agent-authored context.
                upload = replace(upload, changes=tuple(replace(change, operation_name=name)
                    if change.operation_name == "context_upload" else change for change in upload.changes))
            append_plan(builder, upload, name if delegated or ctx.business_id is None else "context_upload")
            updates["filing_json"] = {"upload_id": upload_id, "job_id": job_id, "snapshot_hash": digest}
            after["filing"] = updates["filing_json"]
        builder.record("learning_candidates", row["id"], row, updates)
        builder.record("learning_decisions", row["id"], previous, values)
        if delegated:
            after["decision"]["owner_instruction"] = args["owner_instruction"]
            after["decision"]["instruction_source"] = "agent_reported_owner_conversation"
            if args.get("agent_rationale"):
                after["decision"]["agent_rationale"] = args["agent_rationale"]
        if supplied:
            after["decision"]["reasoning_source"] = "owner_conversation_via_agent" if delegated else "owner_direct"
        if name != "learnings_edit":
            # An engine-linked review item moves its issue's policy in this same transaction; legacy items are unaffected.
            self.decision_effects(builder, ctx, uow, options, row, args, before=previous_state, after_state=state,
                                  reasoning=note if supplied else None, supplied=supplied, delegated=delegated)
        record_history(builder, row["id"], current, after)
        return builder.build()

    def intake_status(self, ctx, args, options, uow):
        row = uow.repository("learning_candidates", business_id=ctx.business_id).get(args["learning_id"], required=True)
        claim = uow.repository("resource_claims").get({"target_namespace": namespace(ctx),
            "resource_type": "learning_candidates", "resource_id": row["id"]})
        receipt = uow.repository("apply_receipts").get(claim["apply_id"]) if claim else None
        if receipt and receipt["operation_name"] not in {"learnings_review", "learnings_edit", "learnings_decide"}:
            raise DomainError("busy", "Learning has a different unfinished operation")
        return {"learning_id": row["id"], "filing": row["filing_json"], "pending": {
            "receipt_id": receipt["id"], "state": receipt["state"], "error": receipt["error_json"]} if receipt else None}

    def resume_intake(self, ctx, learning_id):
        """Owner transport action outside transactions, replaying the original frozen operation."""
        status = self.app.invoke_or_raise(ctx, "learnings_intake_status", {"learning_id": learning_id}).data
        if status["filing"]:
            return {"filing": status["filing"], "pending": None}
        if not status["pending"]:
            raise DomainError("not_found", "No unfinished intake; review the learning to accept it")
        if self.app.file_executor is None:
            raise DomainError("operation_unavailable", "Durable file execution is not installed")
        return self.app.file_executor.resume(self.app, status["pending"]["receipt_id"])

    def prepare_batch(self, ctx, args, options, uow):
        builder = PlanBuilder(ctx, "learnings_record_batch", args, "Record verified learning extraction")
        run = args["run"]
        cores = run.get("cores", {})
        for item in args["items"]:
            core = cores.get((item.get("evidence") or {}).get("session"))
            if not isinstance(core, dict):
                raise DomainError("validation_error", "Learning output requires its source core")
            try:
                validate_item(item, core.get("markdown"), source_core=core)
            except ValueError as exc:
                raise DomainError("validation_error", str(exc)) from exc
            if uow.repository("learning_candidates", business_id=ctx.business_id).get(item["id"]):
                raise DomainError("revision_conflict", "Learning candidate already exists")
            builder.record("learning_candidates", item["id"], None, {"id": item["id"], "candidate_json": item})
        evidence = {}
        for line in args.get("evidence", []):
            identity = line["of"]
            if identity not in evidence:
                row = uow.repository("learning_candidates", business_id=ctx.business_id).get(identity, required=True)
                evidence[identity] = row, plain(row["candidate_json"])
            old_row, value = evidence[identity]
            if line.get('kind') == 'related':
                require_revision(old_row, line.get('revision'))
                value['reconciliations'] = list(value.get('reconciliations', [])) + line['reconciliations']
                continue
            if line.get('kind') in {'reconciled', 'superseded'}:
                decision = uow.repository('learning_decisions', business_id=ctx.business_id).get(identity)
                claim = uow.repository('resource_claims').get({'target_namespace':namespace(ctx),
                    'resource_type':'learning_candidates','resource_id':identity})
                if (claim or old_row['revision'] != line.get('revision') or decision is not None
                        or old_row.get('work_state') or old_row.get('filing_json') or value.get('reconciled_into')):
                    raise DomainError('revision_conflict', 'Earlier learning was reviewed or changed during reconciliation')
                survivor = next((i for i in args['items'] if i['id']==line['into']), None)
                relations = survivor.get('reconciliations', []) if survivor else [r for e in args.get('evidence', [])
                    if e.get('kind')=='related' and e['of']==line['into'] for r in e['reconciliations']]
                if not any(r.get('automatic') and r['earlier']['id']==identity for r in relations):
                    raise DomainError('validation_error', 'Reconciliation requires its surviving candidate and evidence')
                if line['kind'] == 'superseded':
                    from .reconciliation import supersession_supported
                    relation = next(r for r in relations if r.get('automatic') and r['earlier']['id'] == identity)
                    if survivor is None:
                        replacement = uow.repository('learning_candidates', business_id=ctx.business_id).get(line['into'], required=True)
                        related = next((e for e in args.get('evidence', []) if e.get('kind') == 'related' and e['of'] == line['into']), {})
                        require_revision(replacement, related.get('revision'))
                        replacement_decision = uow.repository('learning_decisions', business_id=ctx.business_id).get(line['into'])
                        replacement_claim = uow.repository('resource_claims').get({'target_namespace': namespace(ctx),
                            'resource_type': 'learning_candidates', 'resource_id': line['into']})
                        if replacement_decision or replacement_claim or replacement.get('work_state') or replacement.get('filing_json'):
                            raise DomainError('revision_conflict', 'Replacement learning was reviewed during reconciliation')
                        survivor = plain(replacement['candidate_json'])
                    if (run.get('supersession_mode') != 'apply' or not survivor
                            or not relation.get('supersession') or value.get('reconciliation_reviewed')
                            or relation['earlier'].get('evidence') != value.get('evidence')
                            or not supersession_supported(value, survivor, relation.get('assessment', {}), relation['kind'])):
                        raise DomainError('validation_error', 'Supersession requires a newer fully supported replacement')
                    value['superseded_by'] = line['into']
                    value['supersession'] = {'reason': relation['reason'], 'kind': relation['kind'],
                        'run': run['id'], 'replacement_id': line['into'], 'assessment': relation['assessment']}
                    rejection = {'candidate_id': identity, 'state': 'rejected',
                        'feedback': 'Superseded by ' + line['into'] + ': ' + relation['reason'],
                        'changes_json': {}, 'decided_by': ctx.principal_id}
                    builder.record('learning_decisions', identity, None, rejection)
                    record_history(builder, identity, self.item(old_row),
                        self.item({**old_row, 'candidate_json': value}, {**rejection, 'updated_at': builder.at}))
                    continue
                value['reconciled_into'] = line['into']
                record_history(builder, identity, old_row['candidate_json'], value)
                continue
            value["evidence_more"] = list(value.get("evidence_more", [])) + [line["evidence"], *line.get("evidence_more", [])]
        for identity, (row, value) in evidence.items():
            builder.record("learning_candidates", identity, row, {"candidate_json": value})
        for record in args.get("processed", []):
            prior = uow.repository("learning_scan_state", business_id=ctx.business_id).get(record["session_id"])
            if 'expected_scan_revision' in record and record['expected_scan_revision'] != (prior['revision'] if prior else 0):
                raise DomainError('revision_conflict', 'Transcript scan changed during extraction; late results were not published')
            values = {"transcript_hash": record["transcript_hash"], "state_json": {**record, "run_id": run["id"]}}
            if prior is None:
                values["session_id"] = record["session_id"]
            builder.record("learning_scan_state", record["session_id"], prior, values)
        builder.record("learning_runs", run["id"], None, {"id": run["id"], "result_json": run})
        return builder.build()

    def apply(self, ctx, plan, uow):
        changed = apply_records(ctx, plan, uow)
        if self.documents:
            self.documents.library.refresh_managed({change.resource.resource_id for change in plan.changes
                if change.resource.resource_type == "documents" and change.action != "policy_effect"}, uow)
        if plan.operation_name == "learnings_extract":
            return {"job_id": changed[0]["id"], "state": changed[0]["state"]}
        if plan.operation_name == "learnings_record_batch":
            return {"run_id": plan.arguments["run"]["id"], "items": len(plan.arguments["items"])}
        row = uow.repository("learning_candidates", business_id=ctx.business_id).get(plan.arguments["learning_id"])
        decision = uow.repository("learning_decisions", business_id=ctx.business_id).get(row["id"])
        if plan.operation_name.startswith("learnings_work_"):
            return self.work_projection(row, decision)
        result = {"item": LearningOperations.item(row, decision)}
        linked = self.issue_of_candidate(uow, ctx.business_id, row["id"])
        if linked:
            result["issue"] = issue_summary(linked)
        return result

    def register(self):
        registry = self.app.registry
        self.register_work()
        self.register_review()
        self.register_issues()
        registry.register(OperationDescriptor("learnings_decide", schema({
            "learning_id": IDENTITY, "state": {"enum": ["accepted", "rejected"]},
            "owner_instruction": {"type": "string", "minLength": 1, "maxLength": 8000, "pattern": r"\S",
                "description": "Exact owner instruction from the current conversation authorizing this decision for this proposal. Not source evidence, a recommendation, or standing permission."},
            "reasoning": {"type": "string", "maxLength": 8000,
                "description": "The owner's own explanation for this decision, exactly as they gave it in the current conversation. Omit it when the owner gave none: never ask for one and never write your own here."},
            "agent_rationale": {"type": "string", "maxLength": 8000,
                "description": "Optional rationale of the recording agent. It is stored separately and never as the owner's reasoning."},
            "feedback": {"type": "string", "maxLength": 8000, "description": "Legacy name of reasoning."}}, ["learning_id", "state", "owner_instruction"]),
            **SCOPE, supports_signoff=False, exposure={"mcp"},
            prepare=lambda c,a,o,u: self.prepare_decision("learnings_decide",c,a,o,u), apply=self.apply,
            validate_targets=self.documents.validate_targets if self.documents else None,
            description="Learning Applier only: record the owner's explicit accept/reject instruction for one open learning. Requires current expected_revision and idempotency_key. The instruction is agent-reported, not independently authenticated. Never decide autonomously."))
        registry.register(OperationDescriptor("learnings_intake_status", schema({"learning_id": IDENTITY}, ("learning_id",)),
            **SCOPE, effect_kind="read", supports_signoff=False, owner_only=True, exposure={"dashboard", "worker"}, read=self.intake_status))
        registry.register(OperationDescriptor("learnings_list", schema({**PAGE, "state": {"enum": list(STATES)},
            "scope": {"enum": list(SCOPES)}, "bucket": {"enum": list(BUCKETS)}, "tag": {"enum": list(TAGS)}, "issue_view": VIEW}),
            **SCOPE, effect_kind="read", supports_signoff=False, read=self.list))
        registry.register(OperationDescriptor("learnings_get", schema({"learning_id": IDENTITY}, ["learning_id"]),
            **SCOPE, effect_kind="read", supports_signoff=False, read=self.get))
        registry.register(OperationDescriptor('learnings_keep_both', schema({'learning_id':IDENTITY}, ['learning_id']),
            **SCOPE, owner_only=True, supports_signoff=False, exposure={'dashboard'},
            prepare=self.prepare_keep_both, apply=self.apply))
        registry.register(OperationDescriptor("learnings_extract", EXTRACT_ARGUMENTS, **SCOPE, prepare=self.prepare_extract, apply=self.apply))
        for name, properties, required in (
            ("learnings_review", {"state": {"enum": ["open", "accepted", "rejected"]}, "feedback": {"type": "string", "maxLength": 8000}}, ["state"]),
            ("learnings_edit", {"changes": schema({"scope": {"enum": list(SCOPES)}, "bucket": {"enum": list(BUCKETS)},
                "tag": {"enum": list(TAGS)}, "text": {"type": "string", "minLength": 1, "maxLength": 16000}})}, ["changes"])):
            registry.register(OperationDescriptor(name, schema({"learning_id": IDENTITY, **properties}, ["learning_id", *required]),
                **SCOPE, owner_only=True, supports_signoff=False, exposure=frozenset({"dashboard", "worker"}),
                prepare=lambda c,a,o,u,n=name: self.prepare_decision(n,c,a,o,u), apply=self.apply,
                validate_targets=self.documents.validate_targets if self.documents else None))
        item_schema = {"type": "object", "maxProperties": 40}
        registry.register(OperationDescriptor("learnings_record_batch", schema({
            "items": {"type": "array", "maxItems": 2000, "items": item_schema},
            "evidence": {"type": "array", "maxItems": 2000, "items": item_schema},
            "processed": {"type": "array", "maxItems": 100, "items": schema({"session_id": IDENTITY,
                "transcript_hash": {"type": "string", "pattern": "^[a-f0-9]{64}$"}, "source_id": TEXT, "agent_id": {"anyOf": [UUID, {"type": "null"}]}, "source_kind": {"type": "string"},
                "status": {"enum": ["complete", "retry", "needs_attention"]},
                "attempts": {"type": "integer", "minimum": 1, "maximum": 3}, "last_error": TEXT,
                "expected_scan_revision": {"type": "integer", "minimum": 0}},
                ["session_id", "transcript_hash", "source_id", "agent_id"])},
            "run": {"type": "object", "required": ["id", "cores"], "properties": {"id": UUID, "cores": {"type": "object"}}}},
            ["items", "run"]), **SCOPE, exposure=frozenset({"worker"}), prepare=self.prepare_batch, apply=self.apply))
        return self


def register_operations(application, programs, authority=None):
    return LearningOperations(application, programs, authority).register()
