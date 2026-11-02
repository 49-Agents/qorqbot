"""Bounded extraction from explicitly selected transcript snapshots."""

from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from qorqut_operations.contracts import DomainError, new_id, utc_now
from bazarlyk.domains.learnings import analysis, sources, verification
from bazarlyk.domains.learnings.scope import require_global
from qorqut_operations.application.plans import rows
from bazarlyk.composition.locations import business_hq
from qorqut_operations.background.programs import ProgramResult
from .providers import ProviderError

MAX_PASS_BYTES = 64 * 1024 * 1024    # The most saved-session content one pass over a business's transcript adapter selects.


@dataclass
class BusinessBrief:
    row: dict
    profiles: list
    root: str = ""  # the business's registered HQ, which transcript-relative paths are shown against

    @property
    def id(self): return self.row["id"]
    @property
    def slug(self): return self.row["slug"]

    def header(self):
        config = self.row["config_json"]
        return {"id": self.id, "name": self.row["name"], "stage": self.row["stage"],
                **{key: config[key] for key in ("one_liner", "owner") if key in config}}

    def agents(self):
        return [{"name": row["name"], "kind": row["profile_json"].get("role", {}).get("id", "")}
                for row in self.profiles]


class LearningProgram:
    def __init__(self, models, transcripts, engine=None):
        self.models, self.transcripts, self.engine = models, transcripts, engine

    def snapshots(self, control, agent_id=None, limit=100, seen=None, exclude=frozenset(), listed=None, only=None, limits=None):
        """Saved snapshots not already ``seen`` at their current hash, at most ``limit`` and ``MAX_PASS_BYTES`` of them.

        A caller that needs to know which sessions the adapter offered, whether or not they were skipped as seen, passes an
        empty ``listed`` dict: it is filled with each validated offered session's ``(hash, size in bytes)`` as the adapter is read.
        ``only`` (a set of session IDs) selects just those: every other offered session is validated and listed but never
        selected, and counts against neither bound. A caller that must tell a bound from the end of the adapter's list passes an
        empty ``limits`` set: it receives ``"count"`` or ``"bytes"`` when that bound stopped the pass with a selected snapshot unread.
        """
        if self.transcripts is None:
            raise DomainError("operation_unavailable", "Business transcript adapter is not configured")
        count, total, sessions = 0, 0, set()
        for source in self.transcripts(control.business_id, agent_id):
            control.check()
            if not isinstance(source, dict):
                raise DomainError("invalid_transcript", "Transcript adapter must return registered snapshots")
            value = dict(source)
            for key, maximum in (("session_id", 200), ("source_id", 2000), ("agent_id", 36), ("agent_kind", 200)):
                if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= maximum:
                    raise DomainError("invalid_transcript", "Transcript identity is missing or unbounded")
            try:
                if str(UUID(value["agent_id"])) != value["agent_id"]:
                    raise ValueError()
            except ValueError as exc:
                raise DomainError("invalid_transcript", "Transcript agent must use a registered UUID") from exc
            if agent_id and value["agent_id"] != agent_id:
                raise DomainError("forbidden", "Transcript adapter returned another agent's session")
            if value["agent_id"] in exclude:
                continue
            with control.service.database.read() as uow:
                profile = uow.repository("agent_profiles", business_id=control.business_id).get(value["agent_id"])
                if profile is None:
                    raise DomainError("forbidden", "Transcript agent is outside the selected workspace")
                role = profile['profile_json'].get('role', {}).get('id')
                if control.business_id is None and role == 'learning-applier':
                    if agent_id:
                        raise DomainError('forbidden', 'Learning Applier chats are excluded from automatic learning')
                    continue
                if control.business_id is None:
                    value['agent_kind'] = role
            content = value.get("content")
            if not isinstance(content, str):
                raise DomainError("invalid_transcript", "Transcript content must be UTF-8 JSONL text")
            raw = content.encode("utf-8")
            if len(raw) > 16 * 1024 * 1024:
                raise DomainError("invalid_transcript", "Selected transcript input exceeds the bounded run limit")
            digest = hashlib.sha256(raw).hexdigest()
            if value.get("transcript_hash", digest) != digest or value["session_id"] in sessions:
                raise DomainError("invalid_transcript", "Transcript hash or session identity is inconsistent")
            value["transcript_hash"] = digest
            sessions.add(value["session_id"])
            if listed is not None:
                listed[value["session_id"]] = (digest, len(raw))
            if (seen or {}).get(value['session_id']) == digest or (only is not None and value["session_id"] not in only):
                continue
            count += 1
            if count > limit:
                if limits is not None:
                    limits.add("count")
                break
            total += len(raw)
            if total > MAX_PASS_BYTES:
                if limits is not None:
                    limits.add("bytes")
                break
            yield value

    def run(self, control, args):
        # A directly invoked or old persisted business job stops here: no transcript read, model call, cursor, reconciliation
        # or supersession touches the business's historical learning.
        require_global(control.business_id)
        with control.service.database.read() as uow:
            business = (uow.repository("businesses").get(control.business_id, required=True) if control.business_id else
                {"id": None, "slug": "Global", "name": "Global", "stage": "", "config_json": {}})
            biz = BusinessBrief(business, rows(uow, "agent_profiles", control.business_id),
                                str(business_hq(control.service.paths, business)) if control.business_id else "")
            scan = {row['session_id']: row for row in rows(uow, 'learning_scan_state', control.business_id)}
            seen = {key: row['transcript_hash'] for key, row in scan.items() if sources.counted(row['state_json'], args.get('retry_failed'))}
            decisions = {row["candidate_id"]: row for row in
                rows(uow, "learning_decisions", control.business_id)}
            existing = []
            for row in rows(uow, "learning_candidates", control.business_id):
                decision = decisions.get(row["id"])
                if row["candidate_json"].get("engine"):
                    continue  # Engine review items carry typed evidence this program cannot compare or supersede.
                if row["candidate_json"].get("bucket") == "delta":
                    continue  # Retired procedural history is readable but never a Context extraction counterpart.
                if not row["candidate_json"].get("reconciled_into") and (decision is None or decision["state"] in {"open", "accepted"}):
                    existing.append({**row["candidate_json"], **(decision["changes_json"] if decision else {}),
                        "state": row.get("work_state") or (decision["state"] if decision else "open"),
                        "reviewed": decision is not None or bool(row.get("filing_json")), "revision": row["revision"]})
            # Owner contributions of Global and every active business, under one bound (v1.2 G023).
            packets = sources.snapshots(uow, seen, retry_failed=bool(args.get('retry_failed')), limit=50) if not args.get('agent_id') else []
        run_id, cores, processed, fresh, errors = new_id(), {}, [], [], []
        recorded, diagnostics = [], []
        # A migrated note whose v1 progress cannot be proven the same content: paused in Global scan state for the later
        # migration's reconciliation, never extracted. Its original rows, cursors and evidence stay as they are.
        for packet in [packet for packet in packets if packet.get('unresolved')]:
            origin = packet['unresolved']
            recorded.append({"session_id": packet['key'], "transcript_hash": packet['hash'], "source_id": packet['source']['resource_id'],
                             "agent_id": None, "source_kind": packet['source']['kind'], "status": "needs_attention",
                             "last_error": f"{sources.UNRESOLVED}: {origin['source_table']} {origin['source_id']}"})
            diagnostics.append({"source": packet['key'], "stage": "source_identity", "reason": sources.UNRESOLVED,
                                "business_id": packet['source']['business_id'], **origin})
        packets = [packet for packet in packets if not packet.get('unresolved')]
        budget = args.get("max_usd", 1.0)

        def chat(system, user, model):
            if control.spent_usd() >= budget:
                raise analysis.LearnError("Soft model budget reached; no further calls started")
            try:
                return self.models.ask(control, system, user, json_object=True, thinking=False,
                    max_tokens=16000, model=model, empty_retries=0)
            except DomainError as exc:
                if exc.code in {"cancelled", "timeout", "service_stopping"}:
                    raise
                raise analysis.LearnError(exc.message) from exc

        model = None
        def contributions():
            for packet in packets:
                try:
                    yield sources.core(packet, biz), {"session_id": packet['key'], "transcript_hash": packet['hash'],
                        "source_id": packet['source']['resource_id'], "agent_id": None,
                        "source_kind": packet['source']['kind']}
                except DomainError as exc:
                    errors.append({'source': packet['key'], 'error': exc.message})
        def transcripts():
            # The retired engine no longer owns any session: its enrollment rows are history and must not suppress
            # authorized Context input on the factual route.
            for source in self.snapshots(control, args.get("agent_id"), args.get("max_sessions", 100), seen):
                core = analysis.build_core(source, biz, source["agent_id"], source["agent_kind"], source.get("subagents", []))
                core["header"]["session"] = source["session_id"]
                core["source"] = {key: source[key] for key in ("session_id", "source_id", "transcript_hash", "agent_id")}
                yield core, core['source']

        def inputs():
            from itertools import zip_longest
            # Alternate families under the shared budget; neither always goes last.
            for pair in zip_longest(contributions(), transcripts()):
                for value in pair:
                    if value is not None:
                        yield value

        for core, record in inputs():
            control.check()
            if control.spent_usd() >= budget or len(recorded) >= 100:
                break
            identity = record['session_id']
            items = []
            guarded = control.business_id is None and record.get('agent_id') is not None
            prior = scan.get(identity)
            if guarded:
                record = {**record, 'expected_scan_revision': prior['revision'] if prior else 0}
            prior_state = prior['state_json'] if prior and prior['transcript_hash'] == record['transcript_hash'] else {}
            attempts = 1 + (prior_state.get('attempts', 0) if prior_state.get('status') == 'retry' else 0)
            def failed(message):
                errors.append({'source': identity, 'error': message})
                if guarded:
                    diagnostics.append({'source': identity, 'stage': 'source_failure',
                                        'reason': message, 'proposals': items})
                    recorded.append({**record, 'status': 'needs_attention' if attempts >= 3 else 'retry',
                                     'attempts': attempts, 'last_error': message[:2000]})
            if len(core["markdown"]) > 200000:
                failed('Pruned core exceeds the extraction limit')
                continue
            cores[identity] = core
            # Metadata-only/empty sessions have no evidence to extract.
            if core.get('stats', {}).get('owner_messages') == 0:
                processed.append(record)
                recorded.append(record)
                continue
            if model is None and self.models.factory:
                try:
                    model = self.models.factory(control.business_id).config.model
                except ProviderError as exc:
                    raise DomainError(exc.code, str(exc)) from exc
            try:
                rejected = []
                items, _, issues = analysis.extract(core, biz, run_id, model=model, passes=args.get("passes", 3), chat=chat,
                    diagnostics=rejected)
                diagnostics.extend({'source': identity, **entry} for entry in rejected)
                if rejected and not guarded:
                    issues = [*issues, *[f"pass {entry.get('pass')} item {entry.get('item')}: {entry.get('reason')}"
                                        for entry in rejected if entry.get('stage') == 'evidence']]
                if guarded and issues:
                    failed('; '.join(issues))
                    continue
                if guarded:
                    items, reviewed = verification.verify(items, core, model, chat=chat)
                    diagnostics.extend({'source': identity, **entry} for entry in reviewed)
            except analysis.LearnError as exc:
                failed(str(exc))
                continue
            # This route retains factual Context only; procedural deltas are Overseer's and are dropped before output.
            from bazarlyk.domains.learnings import legacy
            fresh.extend(legacy.context_only_items(items))
            errors.extend({"source": identity, "error": issue} for issue in issues)
            # Partial or invalid extraction must be retried, not silently consumed.
            if not issues:
                processed.append(record)
                recorded.append({**record, 'status': 'complete', 'attempts': attempts} if guarded else record)
            if len(fresh) >= 1800:
                break
        evidence, stats = [], {"merged": 0, "duplicates": 0, "contradictions": 0, "ignored": 0}
        if fresh or args.get('supersession_mode'):
            try:
                fresh, evidence, stats, _ = analysis.dedup(fresh, existing, biz, run_id, model=model, chat=chat,
                    supersession_mode=args.get('supersession_mode'))
            except analysis.LearnError as exc:
                errors.append({"error": "Dedup skipped: " + str(exc)})
        partial = bool(errors or any(entry.get("stage") in {"evidence", "source_failure"}
                                     for entry in diagnostics if isinstance(entry, dict)))
        run = {"id": run_id, "cores": cores, "started": control.job["created_at"], "finished": utc_now(),
            "supersession_mode": args.get('supersession_mode'),
            "processed": len(processed), "processed_by_source": {kind: sum(r.get("source_kind", "transcript") == kind for r in processed) for kind in ("transcript", *sources.KINDS)}, "items": len(fresh), "dedup": stats, "errors": errors,
            "diagnostics": diagnostics, "partial": partial,
            "status": ("completed_with_warnings" if processed or fresh else "failed") if partial else "complete",
            "source_states": recorded,
            "paused_sources": [state for state in
                ({key: row['state_json'] for key, row in scan.items()} |
                 {record['session_id']: record for record in recorded}).values()
                if state.get('status') == 'needs_attention'],
            "cost_usd": control.spent_usd(), "max_usd": budget, "job_id": control.job["id"]}
        return ProgramResult(outputs=({"operation": "learnings_record_batch", "arguments": {
            "items": fresh, "evidence": evidence, "processed": recorded, "run": run}},),
            data={key: value for key, value in run.items() if key != "cores"}, cost_usd=run["cost_usd"],
            completion_error=DomainError("learning_extraction_failed", "No changed source completed extraction").to_dict()
                if errors and not processed and not fresh else None)
