"""Bounded, read-only frozen evidence and append-only history for owner-led learning review.

The standalone engine's issue packages are gone. Every retained candidate -- factual Context and a historical procedural
delta alike -- is read on its own frozen extraction cores and its own append-only history. Evidence and history are two
independent bounded collections: one page never truncates the record, and following each collection's continuation
cursor enumerates it completely. ``content_cursor`` stays a byte transport for one already-selected page and is never a
collection cursor.
"""

import base64
import json

from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, canonical_bytes, canonical_hash, plain
from qorqut_operations.application.plans import IDENTITY, schema
from qorqut_operations.storage.repositories import decode_row

from .analysis import BUCKETS, SCOPES, TAGS
from .scope import SCOPE
from .shared import STATES, agenda, content_cursor, reasoning_projection
from . import legacy

SOURCES_PER_PAGE = 20
HISTORY_PER_PAGE = 20
TRAVERSAL_STEPS = 100
DIRECT_RESPONSE_BYTES = 32000
CHUNK_CHARS = 6000
CURSOR_VERSION = 2
_SKIP, _GAP = object(), object()


def _stale(message="This review cursor is invalid, stale or belongs to another selection; restart the review page"):
    return DomainError("validation_error", message)


def _decode_cursor(value, *, kind, binding):
    """One opaque collection position bound to its scope, candidate and frozen boundary.

    The ``signature`` field is a public canonical hash used to detect a mis-bound or edited cursor; it is not a keyed
    authenticity or authorization mechanism.
    """
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        if (decoded["kind"] != kind or decoded["version"] != CURSOR_VERSION or decoded["binding"] != binding
                or decoded["signature"] != canonical_hash([decoded["binding"], decoded["position"], decoded.get("boundary")])):
            raise ValueError
        position = decoded["position"]
        after = position.get("after") if isinstance(position, dict) else None
        if after is not None and (not isinstance(after, list) or len(after) != 2
                                  or not all(isinstance(value, str) for value in after)):
            raise ValueError
        if (not isinstance(position, dict) or type(position.get("offset")) is not int or position["offset"] < 0
                or type(position.get("child")) is not int or position["child"] < 0
                or not isinstance(position.get("path"), list)
                or any(type(index) is not int or index < 0 for index in position["path"])
                or any(type(position.get(name)) is not int or position[name] < 0 for name in ("available", "missing", "gaps"))):
            raise ValueError
    except (ValueError, TypeError, KeyError) as exc:
        raise _stale() from exc
    return decoded


def _encode_cursor(*, kind, binding, position, boundary=None):
    return base64.urlsafe_b64encode(canonical_bytes({
        "kind": kind, "version": CURSOR_VERSION, "binding": binding, "position": plain(position),
        "boundary": plain(boundary), "signature": canonical_hash([binding, plain(position), plain(boundary)])})).decode("ascii")


def _reference_count(candidate):
    """The number of stored reference positions of one candidate, without materialising the entries."""
    return (0 if candidate.get("evidence") is None else 1) + len(candidate.get("evidence_more") or [])


def _reference_at(candidate, index):
    """One stored reference position: a ``(run, session)`` identity, ``_GAP`` for missing metadata, or ``_SKIP``."""
    evidence = candidate.get("evidence")
    more = candidate.get("evidence_more") or []
    if evidence is not None:
        entry = evidence if index == 0 else more[index - 1]
    else:
        entry = more[index]
    if entry is None:
        return _SKIP                                 # no stored reference metadata at all: nothing to resolve
    if not isinstance(entry, dict):
        return _GAP                                  # an entry that is not reference metadata at all
    if not entry:
        return _SKIP
    run, session = entry.get("run") or candidate.get("run"), entry.get("session")
    return (run, session) if run and session else _GAP


def _relation_at(relations, index):
    """The embedded earlier candidate of one reconciliation by position (the stored list is indexed lazily)."""
    relation = relations[index]
    earlier = relation.get("earlier") if isinstance(relation, dict) else None
    return earlier if isinstance(earlier, dict) else None


def _frame(candidate, *, offset=0, child=0, index=0):
    relations = candidate.get("reconciliations") or []
    return {"candidate": candidate, "references": _reference_count(candidate), "relations": relations,
            "offset": offset, "child": child, "index": index}


class LearningReview:
    def review_get(self, ctx, args, options, uow):
        row = uow.repository("learning_candidates", business_id=ctx.business_id).get(args["learning_id"], required=True)
        decision = uow.repository("learning_decisions", business_id=ctx.business_id).get(row["id"])
        item = self.item(row, decision)
        evidence_cursor, history_cursor = args.get("evidence_cursor"), args.get("history_cursor")
        bundle = self.page(uow, ctx, row, decision, item, evidence_cursor, history_cursor)
        serialized = canonical_bytes(bundle).decode()
        capture = canonical_hash([ctx.business_id, row["id"], row["revision"], serialized])
        if args.get("content_cursor"):
            offset = self._content_offset(args["content_cursor"], capture, serialized)
            content = serialized[offset:offset + CHUNK_CHARS]
            following = offset + len(content)
            return {"learning_id": row["id"], "revision": row["revision"], "content": content, "encoding": "json",
                    "complete": following == len(serialized),
                    "next_content_cursor": content_cursor(capture, following) if following < len(serialized) else None}
        if len(serialized.encode()) <= DIRECT_RESPONSE_BYTES:
            return {**bundle, "complete": True, "next_content_cursor": None}
        return {"learning_id": row["id"], "revision": row["revision"], "complete": False,
                "next_content_cursor": content_cursor(capture, 0)}

    @staticmethod
    def _content_offset(cursor, capture, serialized):
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
            offset = decoded["offset"]
            if (decoded.get("kind") != "content" or decoded.get("version") != CURSOR_VERSION
                    or decoded["binding"] != capture or type(offset) is not int or not 0 <= offset < len(serialized)):
                raise ValueError
        except (ValueError, TypeError, KeyError) as exc:
            raise _stale("Review content cursor is invalid, stale or from another page selection") from exc
        return offset

    def page(self, uow, ctx, row, decision, item, evidence_cursor, history_cursor):
        """One bounded page of the requested collections; the initial request selects both."""
        initial = evidence_cursor is None and history_cursor is None
        collections = [name for name, missing in (("evidence", evidence_cursor is None), ("history", history_cursor is None))
                       if initial or not missing]
        bundle = {
            "item": item, "source_kind": "frozen_extraction_cores", "original_transcripts_included": False,
            "read_only": legacy.is_procedural(item), "page_collections": list(collections),
            "sources": [], "history": [], "next_evidence_cursor": None, "next_history_cursor": None,
            "evidence_complete": False, "history_complete": False, "references_complete": False,
            "evidence_status": "partial",
        }
        if "evidence" in collections:
            page, cursor, complete, references, status = self.evidence_page(uow, ctx, row, decision, item, evidence_cursor)
            bundle.update(sources=page, next_evidence_cursor=cursor, evidence_complete=complete,
                          references_complete=references, evidence_status=status)
        if "history" in collections:
            events, cursor, complete = self.history_page(uow, ctx, row, decision, history_cursor)
            bundle.update(history=events, next_history_cursor=cursor, history_complete=complete)
        bundle.update(reasoning_projection(uow, ctx.business_id, item))
        return bundle

    @staticmethod
    def binding(ctx, row, decision, kind):
        """The frozen identity a collection cursor belongs to: scope, candidate, effective decision and graph."""
        decision = decision or {}
        return canonical_hash({"version": CURSOR_VERSION, "kind": kind, "business_id": ctx.business_id,
            "candidate_id": row["id"], "candidate_revision": row["revision"],
            "decision_revision": decision.get("revision"), "decision_state": decision.get("state"),
            "decision_changes": canonical_hash(plain(decision["changes_json"])) if decision else None,
            "graph": canonical_hash(plain(row["candidate_json"]))})

    # ------------------------------------------------------------------ frozen evidence collection
    def evidence_page(self, uow, ctx, row, decision, item, cursor):
        binding = self.binding(ctx, row, decision, "evidence")
        if cursor is None:
            position = {"path": [], "offset": 0, "child": 0, "available": 0, "missing": 0, "gaps": 0}
        else:
            position = _decode_cursor(cursor, kind="evidence", binding=binding)["position"]
        frames = self._frames(item, position)
        available, missing, gaps = position["available"], position["missing"], position["gaps"]
        sources, seen, steps, done = [], set(), 0, False
        cores = {}
        while steps < TRAVERSAL_STEPS:
            frame = frames[-1]
            if frame["offset"] < frame["references"]:
                reference = _reference_at(frame["candidate"], frame["offset"])
                frame["offset"] += 1
                steps += 1
                if reference is _SKIP:
                    continue
                if reference is _GAP:
                    gaps += 1
                    continue
                if reference in seen:
                    continue
                seen.add(reference)
                core = self.core(uow, ctx, reference[0], reference[1], cores)
                missing += core is None
                available += core is not None
                sources.append({"run": reference[0], "session": reference[1],
                                "status": "available" if core is not None else "missing", "core": core})
                if len(sources) >= SOURCES_PER_PAGE:
                    break
                continue
            if frame["child"] < len(frame["relations"]):
                child = _relation_at(frame["relations"], frame["child"])
                index = frame["child"]
                frame["child"] += 1
                steps += 1
                if child is None:
                    gaps += 1
                    continue
                frames.append(_frame(child, index=index))
                continue
            if len(frames) == 1:
                done = True
                break
            frames.pop()
            steps += 1
        if done:
            return sources, None, True, gaps == 0, self.status(available, missing, complete=True)
        terminal = frames[-1]
        next_position = {"path": [frame["index"] for frame in frames[1:]], "offset": terminal["offset"],
                         "child": terminal["child"], "available": available, "missing": missing, "gaps": gaps}
        return (sources, _encode_cursor(kind="evidence", binding=binding, position=next_position), False, False,
                self.status(available, missing, complete=False))

    @staticmethod
    def _frames(item, position):
        """Rebuild the deterministic depth-first traversal position from the root candidate and its path of children."""
        frames = [_frame(item)]
        for index in position["path"]:
            parent = frames[-1]
            if index >= len(parent["relations"]) or _relation_at(parent["relations"], index) is None:
                raise _stale()
            frames.append(_frame(_relation_at(parent["relations"], index), index=index))
            parent["offset"] = parent["references"]
            parent["child"] = index + 1
        terminal = frames[-1]
        if position["offset"] > terminal["references"] or position["child"] > len(terminal["relations"]):
            raise _stale()
        terminal["offset"], terminal["child"] = position["offset"], position["child"]
        return frames

    @staticmethod
    def status(available, missing, *, complete):
        """Truthful saved-core availability for what this traversal has enumerated so far."""
        if complete:
            return "available" if available and not missing else "missing" if not available else "partial"
        return "partial"

    def core(self, uow, ctx, run, session, cores):
        """The saved frozen core of one ``(run, session)`` reference, looked up in this caller's own scope only."""
        if run not in cores:
            stored = uow.repository("learning_runs", business_id=ctx.business_id).get(run)
            cores[run] = stored["result_json"].get("cores") if stored else None
        return (cores[run] or {}).get(session)

    # ------------------------------------------------------------------ append-only history collection
    def history_metadata(self, uow, ctx, candidate_id):
        """O(1) append-sensitive boundary metadata: the row count and the final ordered identity, never snapshots."""
        total = uow.connection.execute(
            "SELECT COUNT(*) AS total FROM learning_history WHERE business_id IS ? AND candidate_id = ?",
            (ctx.business_id, candidate_id)).fetchone()["total"]
        last = uow.connection.execute(
            "SELECT created_at, id FROM learning_history WHERE business_id IS ? AND candidate_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (ctx.business_id, candidate_id)).fetchone()
        return total, ([last["created_at"], last["id"]] if last else None)

    def history_page(self, uow, ctx, row, decision, cursor):
        """One page of the complete append-only history, selected with an ordered keyset and SQL LIMIT.

        Snapshots are returned exactly as stored; the byte transport, not compaction, carries an oversized page. The
        boundary is the row count plus the last ordered identity, so any append or removal invalidates continuation
        without decoding every event.
        """
        binding = self.binding(ctx, row, decision, "history")
        total, last = self.history_metadata(uow, ctx, row["id"])
        boundary = canonical_hash([total, last])
        after = None
        if cursor is not None:
            decoded = _decode_cursor(cursor, kind="history", binding=binding)
            if decoded.get("boundary") != boundary:
                raise _stale("The append-only history changed after this cursor was issued; restart the review page")
            after = decoded["position"].get("after")
        clause, parameters = "business_id IS ? AND candidate_id = ?", [ctx.business_id, row["id"]]
        if after is not None:
            clause += " AND (created_at, id) > (?, ?)"
            parameters.extend(after)
        raw = uow.connection.execute(
            "SELECT * FROM learning_history WHERE " + clause + " ORDER BY created_at, id LIMIT ?",
            [*parameters, HISTORY_PER_PAGE + 1]).fetchall()
        more = len(raw) > HISTORY_PER_PAGE
        selected = raw[:HISTORY_PER_PAGE]
        events = [decode_row(value) for value in selected]
        next_cursor = None
        if more:
            next_cursor = _encode_cursor(kind="history", binding=binding, boundary=boundary,
                                         position={"path": [], "offset": 0, "child": 0, "available": 0, "missing": 0,
                                                   "gaps": 0, "after": [selected[-1]["created_at"], selected[-1]["id"]]})
        return events, next_cursor, not more

    def register_review(self):
        self.app.registry.register(OperationDescriptor("learnings_review_list", schema({**{"limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "cursor": {"type": "string", "maxLength": 2000}},
            "states": {"type": "array", "minItems": 1, "maxItems": 6, "uniqueItems": True, "items": {"enum": list(STATES)}},
            "bucket": {"enum": list(BUCKETS)}, "tag": {"enum": list(TAGS)}, "scope": {"enum": list(SCOPES)}}),
            effect_kind="read", supports_signoff=False, scope_kinds={"business", "agent_workspace", "global"},
            global_capability="read_global_context",
            resolve_namespace=lambda c,a: "business:" + c.business_id if c.business_id else "agent:" + c.agent_id if c.agent_id else "global",
            read=agenda,
            description="Read the compact factual Context review agenda across authorized scopes; defaults to open. "
                        "Excludes filed and collapsed items. Does not approve anything and returns no issue projection."))
        self.app.registry.register(OperationDescriptor("learnings_review_get", schema({"learning_id": IDENTITY,
            "content_cursor": {"type": "string", "maxLength": 2000, "description": "Byte continuation of the exact selected page; repeat the same evidence_cursor/history_cursor arguments."},
            "evidence_cursor": {"type": "string", "maxLength": 4000, "description": "Continue the frozen evidence collection at this position."},
            "history_cursor": {"type": "string", "maxLength": 4000, "description": "Continue the append-only history collection at this position."}},
            ("learning_id",)),
            **SCOPE, effect_kind="read", supports_signoff=False, read=self.review_get,
            description="Read one retained learning candidate with its owner decision, reasoning and bounded frozen extraction cores "
                        "(quoted evidence and source hashes, never original transcripts). Evidence and history are independent collections: "
                        "follow next_evidence_cursor and next_history_cursor until both are null to enumerate the whole saved record, and "
                        "merge the page_collections this page carries. Sources are keyed by both run and session; missing saved cores are explicit. "
                        "An oversized page comes back in complete:false with next_content_cursor; reassemble it with the same collection "
                        "arguments before following either collection cursor. Historical procedural records are read_only."))