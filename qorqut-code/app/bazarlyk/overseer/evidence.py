"""Host evidence readers for explicitly admitted Overseer experience.

Nothing here creates the experience grant. P04 supplies an adapter bound to
its current authenticated invocation; each source read checks it again.
"""
from __future__ import annotations

from typing import Mapping, Protocol

from qorqut_operations.contracts import DomainError
from qorqut_business_docs_versioning import HistoryStore, NotFound

MAX_THREAD_PAGES = 1
MAX_THREAD_PAGE_NOTES = 20
SCAN_SECONDS = 3.0
MAX_HISTORY_REVISIONS = 100
MAX_TRANSCRIPT_ENTRIES = 1000
MAX_TRANSCRIPT_RECORDS = 2000
MAX_TRANSCRIPT_PAGE_BYTES = 8 * 1024 * 1024
EPISODE_WORK_SECONDS = 2.0
EPISODE_WORK_RESERVE_SECONDS = 0.25
EPISODE_MAX_CANDIDATES = 20
EPISODE_CURSOR_SCOPE = "focused_episode"
EPISODE_CURSOR_VERSION = 2

_EPISODE_KIND_ALIASES = {
    "parent_task": "task", "task": "task", "tasks": "task",
    "signoff": "signoff", "signoffs": "signoff",
    "action": "action", "action_logs": "action",
    "document": "document", "documents": "document",
    "supersedes_id": "signoff", "superseded_by_id": "signoff",
}


def _episode_source_key(ref):
    """Return the namespace-qualified episode identity for one captured member."""
    if not isinstance(ref, Mapping):
        return None
    namespace = ref.get("namespace")
    locator = ref.get("locator")
    if not isinstance(namespace, Mapping) or not isinstance(locator, Mapping):
        return None
    kind = ref.get("kind")
    transcript_lineage = None
    if kind in {"task", "task_message"}:
        base_kind, identity = "task", locator.get("task_id") or ref.get("record_id")
    elif kind in {"action", "action_revision", "action_message"}:
        base_kind, identity = "action", locator.get("action_id") or ref.get("record_id")
    elif kind in {"signoff", "signoff_note", "signoff_response", "execution_outcome"}:
        base_kind, identity = "signoff", locator.get("signoff_id") or ref.get("record_id")
    elif kind in {"document", "document_revision"}:
        base_kind, identity = "document", locator.get("document_id") or ref.get("record_id")
    elif kind == "transcript_entry":
        lineage = [locator.get("agent_id"), locator.get("provider"), locator.get("conversation_id")]
        if (ref.get("provider") != "operations"
                or not all(isinstance(part, str) and part for part in lineage)):
            return None
        base_kind, identity, transcript_lineage = "transcript", _key(lineage), lineage
    else:
        return None
    if not isinstance(identity, str) or not identity:
        return None
    if not ((namespace.get("kind") == "global" and namespace.get("business_id") is None) or
            (namespace.get("kind") == "business" and isinstance(namespace.get("business_id"), str)
             and namespace.get("business_id"))):
        return None
    if (base_kind == "transcript"
            and ref.get("episode_id") not in {
                _episode(dict(namespace), base_kind, transcript_lineage),
                _episode(dict(namespace), base_kind, identity)}):
        return None
    return (_key(dict(namespace)), base_kind, identity)


def _episode_relation(relation, source_ref):
    """Normalize frozen host relation metadata without changing namespace meaning."""
    if not isinstance(relation, Mapping):
        return None
    raw_kind = relation.get("kind") or relation.get("type") or relation.get("resource_type")
    kind = _EPISODE_KIND_ALIASES.get(raw_kind)
    identity = relation.get("record_id") or relation.get("id") or relation.get("resource_id")
    if kind is None or not isinstance(identity, str) or not identity:
        return None
    if "business_id" in relation:
        business_id = relation["business_id"]
        if business_id is None:
            namespace = {"kind": "global", "business_id": None}
        elif isinstance(business_id, str) and business_id:
            namespace = {"kind": "business", "business_id": business_id}
        else:
            return None
    else:
        namespace = source_ref.get("namespace")
        if not isinstance(namespace, Mapping):
            return None
        namespace = dict(namespace)
    if not ((namespace.get("kind") == "global" and namespace.get("business_id") is None) or
            (namespace.get("kind") == "business" and isinstance(namespace.get("business_id"), str)
             and namespace.get("business_id"))):
        return None
    label = raw_kind if isinstance(raw_kind, str) and raw_kind else kind
    return (_key(namespace), kind, identity), label


def _episode_link_ref(key):
    namespace_key, kind, identity = key
    import json
    namespace = json.loads(namespace_key)
    locator_key = {"task": "task_id", "action": "action_id", "signoff": "signoff_id",
                   "document": "document_id"}[kind]
    return {"provider": "operations", "kind": kind, "record_id": identity,
            "namespace": namespace, "episode_id": _episode(namespace, kind, identity),
            "locator": {locator_key: identity}}


class EvidenceAccess(Protocol):
    def resolve_selection(self, selection: Mapping) -> Mapping: ...
    def authorize_source(self, source_ref: Mapping) -> None: ...
    def authorize_retention(self, run_ref: Mapping, *, action: str) -> None: ...


def _access(access):
    if access is None or not callable(getattr(access, "resolve_selection", None)) or not callable(getattr(access, "authorize_source", None)):
        raise DomainError("forbidden", "Current experience-read admission is required")
    return access


def resolve_selection(access, selection):
    _access(access)
    if not isinstance(selection, Mapping) or selection.get("mode") not in {"all_authorized", "explicit"}:
        raise DomainError("validation_error", "Select all_authorized or explicit evidence namespaces")
    if set(selection) - {"mode", "namespaces", "agent_ids", "task_ids", "work_types", "workflow_refs"}:
        raise DomainError("validation_error", "Unknown evidence selection field")
    for key in ("agent_ids", "task_ids", "work_types", "workflow_refs"):
        if key in selection and (not isinstance(selection[key], list) or not all(isinstance(x, str) and x for x in selection[key]) or len(selection[key]) > 100):
            raise DomainError("validation_error", "Invalid evidence subject filter")
    if selection.get("work_types") or selection.get("workflow_refs"):
        raise DomainError("unsupported_filter", "Experience sources do not provide a complete work-type or workflow mapping")
    if selection["mode"] == "explicit":
        namespaces = selection.get("namespaces")
        if not isinstance(namespaces, list) or len(namespaces) > 100 or not all(isinstance(ns, Mapping) and set(ns) == {"kind", "business_id"} for ns in namespaces):
            raise DomainError("validation_error", "Explicit evidence selection requires namespaces")
    result = access.resolve_selection(dict(selection))
    if not isinstance(result, Mapping) or not isinstance(result.get("namespaces"), list):
        raise DomainError("forbidden", "Experience admission did not resolve namespaces")
    for namespace in result["namespaces"]:
        if not isinstance(namespace, Mapping) or not (
            namespace.get("kind") == "global" and namespace.get("business_id") is None or
            namespace.get("kind") == "business" and isinstance(namespace.get("business_id"), str) and namespace.get("business_id")):
            raise DomainError("forbidden", "Experience admission returned an invalid namespace")
    if selection["mode"] == "explicit":
        requested = {tuple(sorted(ns.items())) for ns in selection["namespaces"]}
        admitted = {tuple(sorted(ns.items())) for ns in result["namespaces"]}
        if requested != admitted:
            raise DomainError("forbidden", "Every requested evidence namespace must be admitted")
    for key in ("agent_ids", "task_ids", "work_types", "workflow_refs"):
        if result.get(key, []) != selection.get(key, []):
            raise DomainError("forbidden", "Experience admission changed a requested filter")
    return dict(result)


def _admit_inventory_scope(access, selection):
    """Recheck a frozen inventory scope without expanding it to newly granted projects."""
    current = resolve_selection(access, selection)
    allowed = {tuple(sorted(ns.items())) for ns in current["namespaces"]}
    if any(tuple(sorted(ns.items())) not in allowed for ns in selection["namespaces"]):
        raise DomainError("forbidden", "Saved inventory scope includes a namespace that is no longer authorized")
    return dict(selection)


class AdmittedReaders:
    """Read specific registered captures and history revisions under current admission.

    References are constructed by the host's source enumeration, never from a
    path or an untrusted document-history namespace string.
    """
    def __init__(self, runtime, access):
        self.runtime = runtime
        self.access = _access(access)

    def transcript(self, ref):
        self.access.authorize_source(ref)
        locator = ref.get("locator", {})
        agent, provider, conversation = locator.get("agent_id"), locator.get("provider"), locator.get("conversation_id")
        captures = self.runtime.native_chat.transcripts.listing(agent)
        capture = next((c for c in captures if c["provider"] == provider and c["conversation_id"] == conversation
                        and c["receipt_id"] == locator.get("receipt_id")), None)
        if capture is None:
            raise DomainError("evidence_unavailable", "Captured transcript receipt is unavailable")
        # The existing archive verifies the committed record boundary and hash.
        return self.runtime.native_chat.transcripts.content(agent, capture)

    def document_revision(self, ref, uow):
        self.access.authorize_source(ref)
        locator = ref.get("locator", {})
        document_id, version = locator.get("document_id"), locator.get("version")
        namespace = ref.get("namespace", {})
        business_id = namespace.get("business_id")
        if (namespace.get("kind") == "global" and business_id is not None or
            namespace.get("kind") == "business" and not business_id or
            namespace.get("kind") not in {"global", "business"} or
            not isinstance(version, int) or version < 1 or not isinstance(document_id, str)):
            raise DomainError("validation_error", "Invalid document history reference")
        # Ensure the registered document really belongs to the selected namespace.
        # Working-memory history retains its separate private source visibility.
        row = uow.connection.execute("SELECT kind FROM documents WHERE id=? AND business_id IS ?", (document_id, business_id)).fetchone()
        if row is None:
            raise DomainError("evidence_unavailable", "Registered document is unavailable")
        if row["kind"] == "working_memory":
            raise DomainError("evidence_unavailable", "Working-memory history requires its private reader")
        space = "business:" + business_id if business_id else "global"
        try:
            versioned = HistoryStore(uow.connection).get_document(space, document_id, version=version)
        except NotFound as exc:
            raise DomainError("evidence_unavailable", "Document history revision is unavailable") from exc
        return {"version": versioned.version, "content": versioned.content, "content_hash": versioned.content_sha256,
                "actor_id": versioned.author_id, "occurred_at": versioned.created_at, "reason": versioned.reason,
                "restored_from_version": versioned.restored_from_version}


class OperationsReaders:
    """Invoke existing read descriptors inside the caller's one read transaction.

    The context is supplied by the authenticated host, never by source JSON.
    Both the experience admission and the ordinary operation policy run.
    """
    OPERATIONS = frozenset({"tasks_list", "tasks_get", "tasks_thread_get", "actions_list", "actions_get",
                            "actions_thread_get", "signoffs_list", "signoffs_get", "signoffs_notes_list"})

    def __init__(self, app, context, access):
        self.app, self.context, self.access = app, context, _access(access)

    def read(self, operation, arguments, source_ref, uow):
        from dataclasses import replace
        from qorqut_operations.contracts import InvocationOptions, plain
        if operation not in self.OPERATIONS:
            raise DomainError("validation_error", "Unsupported experience source operation")
        self.access.authorize_source(source_ref)
        descriptor = self.app.registry.get(operation)
        if descriptor.effect_kind != "read" or descriptor.transport_only:
            raise DomainError("operation_unavailable", "Experience source is not an ordinary read")
        arguments = self.app.registry.bind_arguments(operation, dict(arguments))
        options = InvocationOptions()
        ctx = self.app._options_context(self.app.current_context(self.context, uow), options)
        plugin = self.app.registry.required_plugin_for(operation)
        if plugin and ctx.principal_kind == "agent" and plugin not in {ref.id for ref in ctx.enabled_plugin_refs}:
            raise DomainError("forbidden", "Required source plugin is not enabled")
        namespace = self.app.namespace(ctx, descriptor, arguments)
        if namespace == "global":
            ctx = replace(ctx, business_id=None, review_business_id=None)
        elif namespace.startswith("business:") and ctx.business_id is None:
            options = replace(options, business_id=namespace.removeprefix("business:"))
            ctx = self.app._options_context(ctx, options)
        self.app.authorize(ctx, descriptor, (), uow, namespace=namespace,
                           arguments=arguments if ctx.delegation_assignment_id is not None else None)
        result = descriptor.validate_output(self.app.result(ctx, descriptor.read(ctx, arguments, options, uow)))
        return plain(result.data)


class AnalysisCoverage(Protocol):
    def completed_sources(self, source_refs: list[Mapping]) -> list[Mapping]: ...


def _identity(ref):
    import json
    return json.dumps([ref.get("provider"), ref.get("kind"), ref.get("namespace"), ref.get("record_id"),
                       ref.get("revision"), ref.get("content_hash")], sort_keys=True, separators=(",", ":"))


class CaptureJournal:
    """Private inventory and pending observations; capture is never analysis.

    An exact immutable source is stored before its inventory hash is advanced.
    Positive committed coverage is queried externally and never deletes the
    captured bytes. A missing or failed coverage provider suppresses nothing.
    """
    def __init__(self, root, access, coverage=None, *, overseer_ids):
        import os
        import sqlite3
        from .archive import PrivateArchive
        self.access, self.coverage = _access(access), coverage
        self.origin_check = None
        self.overseer_ids = frozenset(overseer_ids)
        if not self.overseer_ids or not all(isinstance(identity, str) and identity for identity in self.overseer_ids):
            raise DomainError("invalid_configuration", "Current Overseer identities are required for source exclusion")
        self.archive = PrivateArchive(root, self._authorize, getattr(self.access, "authorize_retention", None))
        self.path = self.archive.root / "capture-index.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS captures (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, identity TEXT NOT NULL UNIQUE,
                    ref TEXT NOT NULL, material_ref TEXT NOT NULL, episode_id TEXT NOT NULL,
                    relations TEXT NOT NULL, gaps TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS selection_captures (
                    selection_key TEXT NOT NULL, identity TEXT NOT NULL,
                    PRIMARY KEY(selection_key, identity), FOREIGN KEY(identity) REFERENCES captures(identity)
                );
                CREATE TABLE IF NOT EXISTS source_continuations (
                    selection_key TEXT NOT NULL, source_key TEXT NOT NULL, value TEXT,
                    PRIMARY KEY(selection_key, source_key)
                );
                CREATE TABLE IF NOT EXISTS frontiers (
                    snapshot_id TEXT PRIMARY KEY, selection TEXT NOT NULL, selection_key TEXT NOT NULL,
                    captured_at TEXT NOT NULL, max_sequence INTEGER NOT NULL, gaps TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS frontier_members (
                    snapshot_id TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY(snapshot_id, identity)
                );
                CREATE TABLE IF NOT EXISTS inventory (
                    selection_key TEXT NOT NULL, source_key TEXT NOT NULL, identity TEXT NOT NULL,
                    PRIMARY KEY(selection_key, source_key)
                );
            """)

    def _authorize(self, reference):
        pending, seen = [reference], set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            self.access.authorize_source(current)
            provenance = current.get("provenance", {}) if isinstance(current, Mapping) else {}
            originals = list(provenance.get("original_refs", []))
            if isinstance(current, Mapping):
                originals.extend(current.get(key) for key in ("before_ref", "after_ref") if current.get(key) is not None)
            for original in originals:
                if isinstance(original, Mapping) and {"provider", "kind", "namespace", "record_id", "locator"} <= set(original):
                    pending.append(original)

    def observe(self, selection_key, source_key, reference, content, *, relations=(), gaps=()):
        import json
        import sqlite3
        from .provenance import content_hash
        if content_hash(content) != reference.get("content_hash"):
            raise DomainError("validation_error", "Captured source does not match its content hash")
        self._authorize(reference)
        identity = _identity(reference)
        # The operation key is deterministic for the exact source revision and
        # immutable metadata; a crash before the journal insert is retryable.
        from .provenance import content_hash as digest
        operation = "captured-source:" + digest({"identity": identity, "ref": reference})
        material_ref = self.archive.capture(reference, [content], [reference], operation)
        with sqlite3.connect(self.path) as db:
            old = db.execute("SELECT ref, material_ref FROM captures WHERE identity=?", (identity,)).fetchone()
            serialized = json.dumps(reference, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            material = json.dumps(material_ref, sort_keys=True, separators=(",", ":"))
            if old and (old[0] != serialized or old[1] != material):
                raise DomainError("operation_conflict", "Exact source identity has conflicting captured metadata")
            db.execute("INSERT OR IGNORE INTO captures(identity,ref,material_ref,episode_id,relations,gaps) VALUES(?,?,?,?,?,?)",
                       (identity, serialized, material, reference["episode_id"], json.dumps(list(relations)), json.dumps(list(gaps))))
            db.execute("INSERT OR IGNORE INTO selection_captures(selection_key,identity) VALUES(?,?)", (selection_key, identity))
            db.execute("INSERT INTO inventory(selection_key,source_key,identity) VALUES(?,?,?) "
                       "ON CONFLICT(selection_key,source_key) DO UPDATE SET identity=excluded.identity",
                       (selection_key, source_key, identity))
        return {**reference, "material_ref": material_ref}

    @staticmethod
    def _entry_signature(ref):
        """Receipt growth does not revise an unchanged entry or its provenance."""
        result = {key: value for key, value in ref.items() if key not in {"revision", "material_ref"}}
        result["locator"] = {key: value for key, value in ref["locator"].items() if key != "receipt_id"}
        return result

    def reusable_entry(self, candidate):
        """Find the first immutable reference with exactly the same entry/provenance."""
        import json
        import sqlite3
        signature = self._entry_signature(candidate)
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT ref FROM captures WHERE json_extract(ref,'$.provider')=? "
                "AND json_extract(ref,'$.kind')=? AND json_extract(ref,'$.record_id')=? "
                "ORDER BY sequence LIMIT 1001", (candidate["provider"], candidate["kind"], candidate["record_id"])).fetchall()
        for row in rows[:1000]:
            saved = json.loads(row[0])
            if self._entry_signature(saved) == signature:
                self._authorize(saved)
                return saved
        if len(rows) > 1000:
            raise DomainError("coverage_incomplete", "Entry provenance history exceeds the bounded lookup")
        return None

    def _excluded(self, ref):
        import json
        import sqlite3
        from .provenance import excluded
        def lookup(original):
            if not isinstance(original, Mapping):
                return None
            if original.get("provenance"):
                return original
            with sqlite3.connect(self.path) as db:
                rows = db.execute("SELECT ref FROM captures WHERE json_extract(ref,'$.provider')=? "
                    "AND json_extract(ref,'$.kind')=? AND json_extract(ref,'$.record_id')=?",
                    (original.get("provider"), original.get("kind"), original.get("record_id"))).fetchall()
            for row in rows:
                found = json.loads(row[0])
                if original.get("revision") is None or found.get("revision") == original["revision"]:
                    return found
            return None
        return excluded(ref, overseer_ids=self.overseer_ids, lookup=lookup, origin_check=self.origin_check)

    def pending(self, selection_key, *, after=0, limit=50):
        import json
        import sqlite3
        if type(limit) is not int or not 1 <= limit <= 100 or type(after) is not int or after < 0:
            raise DomainError("validation_error", "Invalid pending source page")
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT c.sequence,c.ref,c.material_ref FROM captures c JOIN selection_captures s ON s.identity=c.identity "
                              "WHERE s.selection_key=? AND c.sequence>? ORDER BY c.sequence LIMIT ?",
                              (selection_key, after, limit)).fetchall()
        available, gaps = [], []
        for sequence, raw, material in rows:
            ref = json.loads(raw)
            try:
                self._authorize(ref)
                if self._excluded(ref):
                    continue
            except DomainError as error:
                gaps.append({"code": error.code, "sequence": sequence})
                continue
            available.append((sequence, {**ref, "material_ref": json.loads(material)}))
        analyzed = set()
        if available:
            try:
                if self.coverage is None:
                    raise LookupError("coverage unavailable")
                requested = [ref for _, ref in available]
                result = self.coverage.completed_sources(requested)
                if not isinstance(result, (list, tuple)):
                    raise ValueError("invalid coverage result")
                requested_ids = {_identity(ref) for ref in requested}
                analyzed = {_identity(ref) for ref in result if isinstance(ref, Mapping) and _identity(ref) in requested_ids}
            except Exception:
                gaps.append({"code": "coverage_unavailable"})
        items = [ref for _, ref in available if _identity(ref) not in analyzed]
        return {"items": items, "next_after": rows[-1][0] if len(rows) == limit else None, "gaps": gaps}

    def get_source(self, reference):
        import json
        import sqlite3
        self._authorize(reference)
        if self._excluded(reference):
            raise DomainError("forbidden", "Overseer-origin material is excluded from extraction")
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT ref,material_ref FROM captures WHERE identity=?", (_identity(reference),)).fetchone()
        if row is None:
            raise DomainError("evidence_unavailable", "Captured source revision is unavailable")
        saved, material = json.loads(row[0]), json.loads(row[1])
        candidate = {key: value for key, value in reference.items() if key != "material_ref"}
        if candidate != saved or reference.get("material_ref") != material:
            raise DomainError("evidence_unavailable", "Captured source reference does not match")
        value = self.archive.get(material)
        return value["records"][0]

    def captured_revision(self, provider, kind, namespace, record_id, revision):
        import json
        import sqlite3
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT ref,material_ref FROM captures WHERE json_extract(ref,'$.provider')=? "
                "AND json_extract(ref,'$.kind')=? AND json_extract(ref,'$.record_id')=? "
                "AND json_extract(ref,'$.revision')=? ORDER BY sequence DESC LIMIT 100",
                (provider, kind, record_id, str(revision))).fetchall()
        for raw, material in rows:
            ref = json.loads(raw)
            if ref.get("namespace") == namespace:
                self._authorize(ref)
                return {**ref, "material_ref": json.loads(material)}
        return None

    def continuation(self, selection_key, source_key):
        import json
        import sqlite3
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT value FROM source_continuations WHERE selection_key=? AND source_key=?",
                             (selection_key, source_key)).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    def set_continuation(self, selection_key, source_key, value):
        import json
        import sqlite3
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO source_continuations(selection_key,source_key,value) VALUES(?,?,?) "
                       "ON CONFLICT(selection_key,source_key) DO UPDATE SET value=excluded.value",
                       (selection_key, source_key, json.dumps(value) if value is not None else None))

    def frontier(self, selection, selection_key, *, gaps=(), episode_ids=(), source_refs=(), frozen_inventory=False):
        """Freeze the observed journal prefix; no cross-provider atomicity is claimed."""
        import json
        import sqlite3
        from uuid import uuid4
        from qorqut_operations.contracts import utc_now
        resolved = (_admit_inventory_scope(self.access, selection) if frozen_inventory
                    else resolve_selection(self.access, selection))
        snapshot_id, at = str(uuid4()), utc_now()
        gaps = list(gaps)
        with sqlite3.connect(self.path) as db:
            maximum = db.execute("SELECT COALESCE(MAX(sequence),0) FROM captures").fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM selection_captures WHERE selection_key=?", (selection_key,)).fetchone()[0]
            if total > 1000:
                gaps.append({"code": "frontier_members_truncated"})
            db.execute("INSERT INTO frontiers VALUES(?,?,?,?,?,?)", (snapshot_id, json.dumps(resolved, sort_keys=True),
                selection_key, at, maximum, json.dumps(gaps)))
            episodes = list(dict.fromkeys(episode_ids))[:100]
            identities = list(dict.fromkeys(_identity(ref) for ref in source_refs))[:100]
            terms, ordering_args = [], []
            if identities:
                terms.append("WHEN c.identity IN (" + ",".join("?" for _ in identities) + ") THEN 0")
                ordering_args.extend(identities)
            if episodes:
                terms.append("WHEN c.episode_id IN (" + ",".join("?" for _ in episodes) + ") THEN 1")
                ordering_args.extend(episodes)
            priority = "CASE " + " ".join(terms) + " ELSE 2 END, " if terms else ""
            db.execute("INSERT INTO frontier_members(snapshot_id,identity) "
                       "SELECT ?,c.identity FROM captures c JOIN selection_captures s ON s.identity=c.identity "
                       "WHERE s.selection_key=? ORDER BY " + priority + "c.sequence DESC LIMIT 1000",
                       (snapshot_id, selection_key, *ordering_args))
            providers = [row[0] for row in db.execute("SELECT DISTINCT json_extract(c.ref,'$.provider') FROM captures c "
                "JOIN frontier_members m ON m.identity=c.identity WHERE m.snapshot_id=? ORDER BY 1", (snapshot_id,))]
        return {"snapshot_id": snapshot_id, "captured_at": at,
                "providers": [{"provider": provider, "anchor": str(maximum)} for provider in providers], "gaps": gaps}

    def episode(self, episode_ref, frontier, *, cursor=None, max_chars=160000):
        import base64
        import json
        import sqlite3
        import time

        if not isinstance(episode_ref, Mapping) or not isinstance(episode_ref.get("id"), str):
            raise DomainError("validation_error", "Episode reference requires an ID")
        if not isinstance(frontier, Mapping) or not isinstance(frontier.get("snapshot_id"), str):
            raise DomainError("validation_error", "A captured frontier is required")
        if type(max_chars) is not int or not 1 <= max_chars <= 160000:
            raise DomainError("validation_error", "Invalid episode page size")
        started = time.monotonic()
        work_deadline = started + EPISODE_WORK_SECONDS - EPISODE_WORK_RESERVE_SECONDS
        snapshot, root_id = frontier["snapshot_id"], episode_ref["id"]
        with sqlite3.connect(self.path) as db:
            found = db.execute("SELECT selection,selection_key,captured_at,max_sequence,gaps FROM frontiers WHERE snapshot_id=?",
                               (snapshot,)).fetchone()
            if found is None or found[2] != frontier.get("captured_at"):
                raise DomainError("evidence_unavailable", "Captured frontier is unavailable")
            if cursor is not None:
                try:
                    if not isinstance(cursor, str) or len(cursor) > 4096:
                        raise ValueError("cursor size")
                    binding = json.loads(base64.b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4),
                                                         altchars=b"-_", validate=True))
                    if not isinstance(binding, dict):
                        raise ValueError("cursor object")
                    legacy = set(binding) == {"snapshot", "episode", "sequence", "offset"}
                    if legacy:
                        valid_binding = binding["snapshot"] == snapshot and binding["episode"] == root_id
                    else:
                        valid_binding = (binding.get("scope") == EPISODE_CURSOR_SCOPE
                            and binding.get("version") == EPISODE_CURSOR_VERSION
                            and binding.get("snapshot") == snapshot and binding.get("captured_at") == found[2]
                            and binding.get("episode") == root_id)
                    if not valid_binding:
                        raise ValueError("cursor binding")
                except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                    raise DomainError("validation_error", "Invalid episode cursor") from exc
            selection = json.loads(found[0])
            resolve_selection(self.access, selection)
            candidates = db.execute("SELECT c.sequence,c.ref,c.material_ref,c.relations,c.gaps,c.episode_id FROM captures c "
                "JOIN frontier_members m ON m.identity=c.identity WHERE m.snapshot_id=? AND c.sequence<=? "
                "ORDER BY c.sequence LIMIT 1001", (snapshot, found[3])).fetchall()
        truncated_rows = len(candidates) > 1000
        rows = []
        for sequence, raw_ref, raw_material, raw_relations, raw_gaps, episode_id in candidates[:1000]:
            ref = json.loads(raw_ref)
            rows.append({"sequence": sequence, "raw_ref": raw_ref, "raw_material": raw_material,
                         "episode_id": episode_id, "ref": ref, "relations": json.loads(raw_relations),
                         "gaps": json.loads(raw_gaps), "key": _episode_source_key(ref)})
        frontier_view = {"snapshot_id": snapshot, "captured_at": found[2],
            "providers": [{"provider": provider, "anchor": str(found[3])}
                          for provider in sorted({row["ref"].get("provider") for row in rows
                                                  if isinstance(row["ref"].get("provider"), str)})],
            "gaps": json.loads(found[4])}
        root_rows = [row for row in rows if row["episode_id"] == root_id]
        if not root_rows:
            raise DomainError("evidence_unavailable", "Episode is absent from this captured frontier")
        root_keys = {row["key"] for row in root_rows if row["key"] is not None}
        if not root_keys:
            raise DomainError("evidence_unavailable", "Episode has no usable captured source identity")

        rows_by_key = {}
        for row in rows:
            if row["key"] is not None:
                rows_by_key.setdefault(row["key"], []).append(row)

        # Select exactly one relation hop from the requested episode. Incoming
        # relations are checked against the same namespace-qualified identity.
        attachment_kinds = {"signoff", "action", "document"}
        attachment_episode_proofs = {}
        def add_attachment_proof(episode_id, proof_row):
            if episode_id == root_id:
                return
            proofs = attachment_episode_proofs.setdefault(episode_id, [])
            if all(existing["sequence"] != proof_row["sequence"] for existing in proofs):
                proofs.append(proof_row)

        for row in rows:
            if row["key"] is None:
                continue
            if row["episode_id"] == root_id:
                for relation in row["relations"]:
                    edge = _episode_relation(relation, row["ref"])
                    if edge and edge[0][1] in attachment_kinds:
                        for member in rows_by_key.get(edge[0], ()):
                            add_attachment_proof(member["episode_id"], row)
            elif row["key"][1] in attachment_kinds:
                for relation in row["relations"]:
                    edge = _episode_relation(relation, row["ref"])
                    if edge and edge[0] in root_keys:
                        # For an incoming edge, the attached source itself is
                        # the frozen proof. Its read path checks admission and
                        # exclusion before yielding either body or navigation.
                        add_attachment_proof(row["episode_id"], row)
                        break
        attachment_episode_ids = set(attachment_episode_proofs)
        attachment_episode_ids.discard(root_id)
        attachment_rows = [row for row in rows if row["episode_id"] in attachment_episode_ids]
        attachment_proofs = {row["sequence"]: attachment_episode_proofs[row["episode_id"]]
                             for row in attachment_rows}
        attachment_by_key = {}
        for row in attachment_rows:
            if row["key"] is None:
                continue
            old = attachment_by_key.get(row["key"])
            if old is None or row["ref"].get("kind") == row["key"][1]:
                attachment_by_key[row["key"]] = row
        selected_episode_ids = {root_id, *attachment_episode_ids}
        selected_keys = root_keys | {row["key"] for row in attachment_rows if row["key"] is not None}

        # Navigation only inspects frozen relation metadata. Each candidate is
        # admitted later, when its page is built; no target body is read here.
        link_proofs = {}
        def add_link(target_key, relation_name, origin_row, target_row=None, attachment_endpoint=None):
            if target_key is None or target_key in selected_keys or target_key[1] not in {"task", "signoff", "action", "document"}:
                return
            entry = link_proofs.get(target_key)
            if entry is None:
                captured_row = target_row or next(iter(rows_by_key.get(target_key, ())), None)
                entry = {"target_ref": captured_row["ref"] if captured_row else _episode_link_ref(target_key),
                         "captured": captured_row is not None, "proofs": []}
                link_proofs[target_key] = entry
            if attachment_endpoint is None and origin_row["episode_id"] in attachment_episode_ids:
                attachment_endpoint = origin_row
            gate_proofs = (attachment_proofs.get(attachment_endpoint["sequence"], ())
                           if attachment_endpoint is not None else ())
            endpoint_identity = (_identity(attachment_endpoint["ref"])
                                 if attachment_endpoint is not None else None)
            proof = {"sequence": origin_row["sequence"], "origin_ref": origin_row["ref"],
                     "relation": relation_name,
                     "endpoint_ref": attachment_endpoint["ref"] if attachment_endpoint is not None else None,
                     "endpoint_identity": endpoint_identity,
                     "gate_proofs": [item["ref"] for item in gate_proofs]}
            if all(existing["sequence"] != proof["sequence"] or existing["relation"] != proof["relation"]
                   or existing["endpoint_identity"] != proof["endpoint_identity"]
                   for existing in entry["proofs"]):
                entry["proofs"].append(proof)

        for row in rows:
            if row["key"] is None:
                continue
            if row["episode_id"] in selected_episode_ids:
                for relation in row["relations"]:
                    edge = _episode_relation(relation, row["ref"])
                    if edge:
                        add_link(edge[0], edge[1], row, next(iter(rows_by_key.get(edge[0], ())), None))
            for relation in row["relations"]:
                edge = _episode_relation(relation, row["ref"])
                if edge and edge[0] in selected_keys and row["key"] not in selected_keys:
                    add_link(row["key"], edge[1], row, row,
                             attachment_endpoint=attachment_by_key.get(edge[0]))
        link_groups = []
        for target_key in sorted(link_proofs, key=lambda item: (item[0], item[1], item[2])):
            entry = link_proofs[target_key]
            proofs = sorted(entry["proofs"], key=lambda proof: (proof["sequence"],
                _key(proof["origin_ref"]), proof["relation"], proof["endpoint_identity"] or ""))
            link_groups.append({"key": target_key, "target_ref": entry["target_ref"],
                                "captured": entry["captured"], "proofs": proofs})

        phase, position, offset, proof_position, gate_position, gap_seen = "root", 0, 0, 0, 0, False
        cursor_reset = False
        value = None
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 4096:
                    raise ValueError("cursor size")
                value = json.loads(base64.b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4),
                                                    altchars=b"-_", validate=True))
                if not isinstance(value, dict):
                    raise ValueError("cursor object")
                if set(value) == {"snapshot", "episode", "sequence", "offset"}:
                    sequence, old_offset = value["sequence"], value["offset"]
                    if (value["snapshot"] != snapshot or value["episode"] != root_id or type(sequence) is not int
                            or type(old_offset) is not int or not 0 <= sequence <= found[3] + 1
                            or not 0 <= old_offset <= 2**31 - 1):
                        raise ValueError("legacy cursor binding or bounds")
                    # The former cursor encoded the transitive graph position.
                    # It is valid for this root/frontier, but must restart the
                    # smaller scope without touching saved coverage.
                    cursor_reset = True
                else:
                    required = {"scope", "version", "snapshot", "captured_at", "episode", "phase",
                                "position", "offset", "gap_seen"}
                    allowed = required | {"proof", "gate"}
                    if not required <= set(value) or set(value) - allowed:
                        raise ValueError("cursor fields")
                    if (value["scope"] != EPISODE_CURSOR_SCOPE
                            or type(value["version"]) is not int
                            or value["version"] != EPISODE_CURSOR_VERSION):
                        raise ValueError("cursor version")
                    if value["snapshot"] != snapshot or value["captured_at"] != found[2] or value["episode"] != root_id:
                        raise ValueError("cursor binding")
                    phase, position = value["phase"], value["position"]
                    offset, gap_seen = value["offset"], value["gap_seen"]
                    proof_position = value.get("proof", 0)
                    gate_position = value.get("gate", 0)
                    if phase not in {"root", "attachments", "links"} or type(position) is not int or position < 0:
                        raise ValueError("cursor phase or position")
                    if (type(offset) is not int or not 0 <= offset <= 2**31 - 1
                            or type(gap_seen) is not bool or type(proof_position) is not int or proof_position < 0
                            or type(gate_position) is not int or gate_position < 0):
                        raise ValueError("cursor offset or state")
                    phase_items = {"root": root_rows, "attachments": attachment_rows, "links": link_groups}[phase]
                    if position > len(phase_items):
                        raise ValueError("cursor position bounds")
                    if phase == "root" and (proof_position != 0 or gate_position != 0
                                             or "proof" in value or "gate" in value):
                        raise ValueError("root cursor proof position")
                    if phase == "attachments":
                        if "proof" not in value or "gate" in value:
                            raise ValueError("attachment proof position")
                        if position < len(attachment_rows) and proof_position >= len(attachment_proofs[attachment_rows[position]["sequence"]]):
                            raise ValueError("attachment proof bounds")
                        if position == len(phase_items) and (offset != 0 or proof_position != 0):
                            raise ValueError("attachment terminal position")
                    if phase == "root" and position == len(phase_items) and offset != 0:
                        raise ValueError("cursor terminal offset")
                    if phase == "links":
                        if "proof" not in value or "gate" not in value:
                            raise ValueError("missing link proof position")
                        if offset != 0 or (position == len(link_groups) and (proof_position != 0 or gate_position != 0)):
                            raise ValueError("link cursor offset")
                        if position < len(link_groups) and proof_position >= len(link_groups[position]["proofs"]):
                            raise ValueError("link proof bounds")
                        if position < len(link_groups):
                            gate_proofs = link_groups[position]["proofs"][proof_position]["gate_proofs"]
                            if gate_position >= max(1, len(gate_proofs)):
                                raise ValueError("link gate bounds")
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise DomainError("validation_error", "Invalid episode cursor") from exc
        else:
            phase, position, offset, proof_position, gate_position, gap_seen = "root", 0, 0, 0, 0, False

        frontier_truncated = truncated_rows or any(
            isinstance(gap, dict) and gap.get("code") == "frontier_members_truncated"
            for gap in json.loads(found[4]))
        if frontier_truncated:
            gap_seen = True

        # Check the requested episode's current admission on every continuation.
        anchor_row = next((row for row in root_rows
                           if row["key"] and row["ref"].get("kind") == row["key"][1]), root_rows[0])
        anchor_ref = anchor_row["ref"]
        anchor_sequence = anchor_row["sequence"]
        if anchor_row["key"] and anchor_row["key"][1] == "transcript":
            import json
            agent, provider, conversation = json.loads(anchor_row["key"][2])
            anchor_ref = {"provider": "native_archive", "kind": "transcript", "record_id": anchor_row["key"][2],
                "namespace": anchor_row["ref"]["namespace"], "episode_id": root_id,
                "locator": {"agent_id": agent, "provider": provider, "conversation_id": conversation}}
            anchor_sequence = None
        anchor = {"ref": anchor_ref, "sequence": anchor_sequence, "row": anchor_row}
        records, refs, relations, related_episodes, gaps = [], [], [], [], []
        try:
            self._authorize(anchor["ref"])
            if self._excluded(anchor["ref"]):
                raise DomainError("forbidden", "Episode is excluded")
        except DomainError:
            return {"id": root_id, "scope": None, "records": [], "source_refs": [],
                    "relations": [], "related_episodes": [], "frontier": frontier_view,
                    "availability": "forbidden", "complete": False,
                    "gaps": [{"code": "forbidden"}], "next_cursor": None,
                    **({"cursor_reset": True} if cursor_reset else {})}
        if time.monotonic() >= work_deadline:
            payload = {"scope": EPISODE_CURSOR_SCOPE, "version": EPISODE_CURSOR_VERSION,
                "snapshot": snapshot, "captured_at": found[2], "episode": root_id,
                "phase": phase, "position": position, "offset": offset, "gap_seen": gap_seen}
            if phase in {"attachments", "links"}:
                payload["proof"] = proof_position
            if phase == "links":
                payload["gate"] = gate_position
            next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
            result = {"id": root_id, "scope": anchor["ref"].get("scope", anchor_row["ref"].get("scope")), "records": records,
                "source_refs": refs, "relations": relations, "related_episodes": related_episodes,
                "frontier": frontier_view, "availability": "partial", "complete": False,
                "gaps": [{"code": "frontier_members_truncated"}] if frontier_truncated else [],
                "next_cursor": next_cursor}
            if cursor_reset:
                result["cursor_reset"] = True
            return result

        phases = {"root": (root_rows, "attachments"), "attachments": (attachment_rows, "links"),
                  "links": (link_groups, None)}
        remaining = max_chars
        candidate_count = 1
        current_admissions = {_identity(anchor["ref"])}
        counted_sources = {anchor["sequence"]} if anchor["sequence"] is not None else set()
        while phase is not None:
            phase_items, following = phases[phase]
            if position >= len(phase_items):
                phase, position, offset, proof_position, gate_position = following, 0, 0, 0, 0
                continue
            if (candidate_count >= EPISODE_MAX_CANDIDATES or remaining <= 0
                    or time.monotonic() >= work_deadline):
                break
            item = phase_items[position]
            if phase == "attachments":
                proofs = attachment_proofs[item["sequence"]]
                if proof_position >= len(proofs):
                    gaps.append({"code": "related_source_forbidden"})
                    gap_seen = True
                    position, offset, proof_position = position + 1, 0, 0
                    continue
                proof_row = proofs[proof_position]
                proof_ref = proof_row["ref"]
                proof_identity = _identity(proof_ref)
                target_identity = _identity(item["ref"])
                proof_is_target = proof_identity == target_identity
                proof_new = proof_identity not in current_admissions and not proof_is_target
                source_new = item["sequence"] not in counted_sources
                if candidate_count + (1 if proof_new else 0) + (1 if source_new else 0) > EPISODE_MAX_CANDIDATES:
                    break
                if proof_new:
                    if time.monotonic() >= work_deadline:
                        break
                    candidate_count += 1
                    try:
                        self._authorize(proof_ref)
                        if self._excluded(proof_ref):
                            raise DomainError("forbidden", "Attachment relation proof is excluded")
                        current_admissions.add(proof_identity)
                    except DomainError:
                        gaps.append({"code": "related_source_forbidden"})
                        gap_seen = True
                        proof_position += 1
                        if proof_position >= len(proofs):
                            position, offset, proof_position = position + 1, 0, 0
                        continue
                if time.monotonic() >= work_deadline:
                    break
                source_ref = {**item["ref"], "material_ref": json.loads(item["raw_material"])}
                try:
                    content = self.get_source(source_ref)
                    current_admissions.add(target_identity)
                except DomainError as error:
                    gaps.append({"code": error.code, "source": "unavailable"})
                    gap_seen = True
                    if source_new:
                        candidate_count += 1
                        counted_sources.add(item["sequence"])
                    position, offset, proof_position = position + 1, 0, 0
                    continue
            elif phase == "root":
                source_ref = {**item["ref"], "material_ref": json.loads(item["raw_material"])}
                try:
                    content = self.get_source(source_ref)
                    current_admissions.add(_identity(item["ref"]))
                except DomainError as error:
                    gaps.append({"code": error.code, "source": "unavailable"})
                    gap_seen = True
                    if item["sequence"] not in counted_sources:
                        candidate_count += 1
                        counted_sources.add(item["sequence"])
                    position, offset = position + 1, 0
                    continue
            else:
                group = item
                proofs = group["proofs"]
                if proof_position >= len(proofs):
                    position, proof_position, gate_position = position + 1, 0, 0
                    continue
                proof = proofs[proof_position]
                origin_ref = proof["origin_ref"]
                origin_identity = _identity(origin_ref)
                origin_new = origin_identity not in current_admissions
                endpoint_ref = proof.get("endpoint_ref")
                endpoint_identity = _identity(endpoint_ref) if endpoint_ref is not None else None
                endpoint_new = (endpoint_identity is not None and endpoint_identity != origin_identity
                                and endpoint_identity not in current_admissions)
                gate_proofs = proof["gate_proofs"]
                if gate_position >= len(gate_proofs) and gate_proofs:
                    position, proof_position, gate_position = position + 1, 0, 0
                    continue
                gate_ref = gate_proofs[gate_position] if gate_proofs else None
                gate_identity = _identity(gate_ref) if gate_ref else None
                gate_new = (gate_identity is not None and gate_identity not in current_admissions
                            and gate_identity not in {origin_identity, endpoint_identity})
                # Reserve target admission while checking the relation origin
                # plus any selected attachment endpoint and its direct-edge proof.
                needed = (1 if origin_new else 0) + (1 if endpoint_new else 0) + (1 if gate_new else 0) + 1
                if candidate_count + needed > EPISODE_MAX_CANDIDATES:
                    break
                if origin_new:
                    if time.monotonic() >= work_deadline:
                        break
                    candidate_count += 1
                    try:
                        self._authorize(origin_ref)
                        if self._excluded(origin_ref):
                            raise DomainError("forbidden", "Related source is excluded")
                        current_admissions.add(origin_identity)
                    except DomainError:
                        gaps.append({"code": "related_source_forbidden"})
                        gap_seen = True
                        proof_position += 1
                        if proof_position >= len(proofs):
                            position, proof_position = position + 1, 0
                        gate_position = 0
                        continue
                if endpoint_new:
                    if time.monotonic() >= work_deadline:
                        break
                    candidate_count += 1
                    try:
                        self._authorize(endpoint_ref)
                        if self._excluded(endpoint_ref):
                            raise DomainError("forbidden", "Selected attachment is excluded")
                        current_admissions.add(endpoint_identity)
                    except DomainError:
                        gaps.append({"code": "related_source_forbidden"})
                        gap_seen = True
                        proof_position += 1
                        if proof_position >= len(proofs):
                            position, proof_position = position + 1, 0
                        gate_position = 0
                        continue
                if gate_new:
                    if time.monotonic() >= work_deadline:
                        break
                    candidate_count += 1
                    try:
                        self._authorize(gate_ref)
                        if self._excluded(gate_ref):
                            raise DomainError("forbidden", "Attachment relation proof is excluded")
                        current_admissions.add(gate_identity)
                    except DomainError:
                        gaps.append({"code": "related_source_forbidden"})
                        gap_seen = True
                        gate_position += 1
                        if gate_position >= len(gate_proofs):
                            proof_position += 1
                            if proof_position >= len(proofs):
                                position, proof_position = position + 1, 0
                            gate_position = 0
                        continue
                if time.monotonic() >= work_deadline:
                    break
                target_ref = group["target_ref"]
                candidate_count += 1
                if _identity(target_ref) != origin_identity:
                    try:
                        self._authorize(target_ref)
                        if self._excluded(target_ref):
                            raise DomainError("forbidden", "Related target is excluded")
                    except DomainError:
                        gaps.append({"code": "related_source_forbidden"})
                        gap_seen = True
                        position, proof_position, gate_position = position + 1, 0, 0
                        continue
                namespace_key, kind, identity = group["key"]
                namespace = json.loads(namespace_key)
                related_episodes.append({"episode_ref": {"id": _episode(namespace, kind, identity)},
                    "kind": kind, "record_id": identity, "namespace": namespace,
                    "relation": proof["relation"], "captured": group["captured"]})
                position, proof_position, gate_position = position + 1, 0, 0
                if len(related_episodes) >= EPISODE_MAX_CANDIDATES:
                    break
                continue

            content_text = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if offset > len(content_text):
                raise DomainError("validation_error", "Episode cursor exceeds source size")
            fragment = content_text[offset:offset + remaining]
            records.append({"source_ref": source_ref, "content_fragment": fragment,
                            "encoding": "json", "offset": offset})
            refs.append(source_ref)
            if offset == 0:
                gaps.extend(item["gaps"])
                if item["gaps"]:
                    gap_seen = True
            for relation in item["relations"]:
                if _episode_relation(relation, item["ref"]) is None:
                    relations.append(relation)
            remaining -= len(fragment)
            if item["sequence"] not in counted_sources:
                candidate_count += 1
                counted_sources.add(item["sequence"])
            end = offset + len(fragment)
            if end < len(content_text):
                offset = end
                break
            position, offset, proof_position, gate_position = position + 1, 0, 0, 0
            if remaining <= 0:
                break

        if phase is not None and position >= len(phases[phase][0]):
            phase, position, offset, proof_position, gate_position = phases[phase][1], 0, 0, 0, 0
        unfinished = phase is not None
        next_cursor = None
        if unfinished:
            payload = {"scope": EPISODE_CURSOR_SCOPE, "version": EPISODE_CURSOR_VERSION,
                "snapshot": snapshot, "captured_at": found[2], "episode": root_id,
                "phase": phase, "position": position, "offset": offset, "gap_seen": gap_seen}
            if phase in {"attachments", "links"}:
                payload["proof"] = proof_position
            if phase == "links":
                payload["gate"] = gate_position
            next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")

        unique_relations = list({_key(relation): relation for relation in relations}.values())
        display_gaps = list(gaps)
        if frontier_truncated and not any(gap.get("code") == "frontier_members_truncated" for gap in display_gaps):
            display_gaps.append({"code": "frontier_members_truncated"})
        scope = refs[0].get("scope") if refs else anchor["ref"].get("scope", anchor_row["ref"].get("scope"))
        partial = bool(gap_seen or next_cursor)
        result = {"id": root_id, "scope": scope, "records": records, "source_refs": refs,
            "relations": unique_relations, "related_episodes": related_episodes,
            "frontier": frontier_view, "availability": "partial" if partial else "available",
            "complete": not partial, "gaps": display_gaps, "next_cursor": next_cursor}
        if cursor_reset:
            result["cursor_reset"] = True
        return result

def _episode(namespace, kind, identity):
    from uuid import NAMESPACE_URL, uuid5
    return str(uuid5(NAMESPACE_URL, "qorqut:experience:" + _key([namespace, kind, identity])))


def _key(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scope(namespace):
    return {"description": "Global" if namespace["kind"] == "global" else "Project " + namespace["business_id"],
            "project_ids": [] if namespace["kind"] == "global" else [namespace["business_id"]],
            "work_types": [], "workflow_refs": [], "conditions": [], "exceptions": []}


class HostScanner:
    """Enumerate real registered Operations and native archive sources.

    The host binds the authenticated context and the experience gateway. SQL is
    used for routing keys only; contents are read through the existing scoped
    Operations readers or registered archive/history readers.
    """
    def __init__(self, runtime, context, access, journal):
        self.runtime, self.context, self.access, self.journal = runtime, context, _access(access), journal
        self.readers = AdmittedReaders(runtime, access)
        self.deadline = None

    def expired(self):
        import time
        return self.deadline is not None and time.monotonic() >= self.deadline

    def keys(self, selection, uow, *, after=None, limit=20):
        """A bounded stable page of registered routing identities, not bodies."""
        rows = []
        tasks, agents = set(selection.get("task_ids", [])), set(selection.get("agent_ids", []))
        old = after or ["", "", ""]
        for namespace in sorted(selection["namespaces"], key=_key):
            nskey, business_id = _key(namespace), namespace["business_id"]
            statements = [("action", "action_key", "action_logs", "business_id"),
                          ("document", "id", "documents", "business_id"),
                          ("signoff", "id", "signoffs", "review_business_id"),
                          ("task", "id", "tasks", "business_id")]
            for kind, column, table, column_scope in statements:
                if (tasks and kind == "document") or (nskey, kind) < (old[0], old[1]):
                    continue
                bound = old[2] if (nskey, kind) == (old[0], old[1]) else ""
                clause, parameters = "", []
                if tasks:
                    marks = ",".join("?" for _ in tasks)
                    if kind == "task":
                        clause += " AND id IN (" + marks + ")"
                    elif kind == "action":
                        clause += " AND json_extract(refs_json,'$.task') IN (" + marks + ")"
                    elif kind == "signoff":
                        clause += " AND EXISTS (SELECT 1 FROM json_each(expected_targets_json) t WHERE "                                   "json_extract(t.value,'$.resource.resource_type')='tasks' AND "                                   "json_extract(t.value,'$.resource.resource_id') IN (" + marks + "))"
                    parameters.extend(sorted(tasks))
                if agents:
                    marks = ",".join("?" for _ in agents)
                    if kind == "task":
                        clause += " AND (created_by IN (" + marks + ") OR EXISTS (SELECT 1 FROM thread_messages m "                                   "WHERE m.business_id IS tasks.business_id AND m.item_type='task' AND m.item_id=tasks.id "                                   "AND (m.author_id IN (" + marks + ") OR m.recorder_id IN (" + marks + "))))"
                        parameters.extend(sorted(agents) * 3)
                    elif kind == "signoff":
                        clause += " AND (proposer_agent_id IN (" + marks + ") OR proposer_principal_id IN (" + marks + "))"
                        parameters.extend(sorted(agents) * 2)
                    elif kind == "action":
                        clause += " AND (performed_by IN (" + marks + ") OR recorded_by IN (" + marks + "))"
                        parameters.extend(sorted(agents) * 2)
                    else:
                        clause += " AND owning_agent_id IN (" + marks + ")"
                        parameters.extend(sorted(agents))
                sql = f"SELECT {column} FROM {table} WHERE {column_scope} IS ? AND {column}>?{clause} ORDER BY {column} LIMIT ?"
                for row in uow.connection.execute(sql, (business_id, bound, *parameters, limit + 1)):
                    rows.append(([nskey, kind, row[0]], namespace, kind, row[0]))
            if not tasks and (nskey, "transcript") >= (old[0], old[1]):
                bound = old[2] if (nskey, "transcript") == (old[0], old[1]) else ["", "", ""]
                if not isinstance(bound, list) or len(bound) != 3:
                    raise DomainError("validation_error", "Invalid transcript enumeration cursor")
                agent_filter = " AND c.agent_id IN (" + ",".join("?" for _ in agents) + ")" if agents else ""
                captures = uow.connection.execute("SELECT c.agent_id,c.provider,c.conversation_id FROM native_captures c "
                    "JOIN agent_profiles p ON p.agent_id=c.agent_id WHERE p.home_business_id IS ? "
                    "AND c.receipt_id IS NOT NULL AND c.captured_length>0 "
                    "AND (c.agent_id,c.provider,c.conversation_id)>(?,?,?)" + agent_filter +
                    " ORDER BY c.agent_id,c.provider,c.conversation_id LIMIT ?",
                    (business_id, *bound, *sorted(agents), limit + 1))
                for row in captures:
                    identity = [row[0], row[1], row[2]]
                    rows.append(([nskey, "transcript", identity], namespace, "transcript", identity))
        rows.sort(key=lambda item: item[0])
        return rows[:limit + 1]

    def _current_origin_excluded(self, ref):
        """Recheck an Action's linked Task ancestry using current source access."""
        if isinstance(ref, Mapping) and ref.get('provider') == 'operations' and ref.get('kind') in (
                'signoff', 'signoff_note', 'signoff_response', 'execution_outcome'):
            with self.runtime.database.read() as uow:
                return self._review_signoff(ref.get('locator', {}).get('signoff_id') or ref.get('record_id'), uow)
        if not isinstance(ref, Mapping) or ref.get("provider") != "operations" or ref.get("kind") != "task":
            return False
        namespace = ref.get("namespace") or {}
        task_id = ref.get("record_id")
        if namespace.get("kind") not in {"global", "business"} or not task_id:
            raise DomainError("evidence_unavailable", "Task ancestry is unavailable")
        self.access.authorize_source(ref)
        with self.runtime.database.read() as uow:
            row = uow.connection.execute("SELECT created_by FROM tasks WHERE business_id IS ? AND id=?",
                                         (namespace.get("business_id"), task_id)).fetchone()
        if row is None:
            raise DomainError("evidence_unavailable", "Task ancestry is unavailable")
        return row[0] in self.journal.overseer_ids

    def _ops(self, namespace):
        from dataclasses import replace
        ctx = replace(self.context, business_id=namespace["business_id"], review_business_id=namespace["business_id"])
        return OperationsReaders(self.runtime.application, ctx, self.access)

    def _locator(self, namespace, kind, identity, **locator):
        return {"provider": "native_archive" if kind == "transcript" else "operations", "kind": kind,
                "namespace": dict(namespace), "record_id": identity, "locator": locator}

    def _save(self, selection_key, pre, content, *, revision, episode_id, actor_id=None, actor_kind=None,
              occurred_at=None, producer_id=None, original_refs=(), origin_event_id=None, relations=(), gaps=(),
              before_ref=None, after_ref=None, provenance_extra=None, native_archive=None, overseer_origin=False):
        from .provenance import source_ref
        exact = source_ref(provider=pre["provider"], kind=pre["kind"], record_id=pre["record_id"], revision=revision,
            content=content, episode_id=episode_id, actor_id=actor_id, actor_kind=actor_kind, occurred_at=occurred_at,
            locator=pre["locator"], namespace=pre["namespace"], scope=_scope(pre["namespace"]), producer_id=producer_id,
            original_refs=original_refs, original_event_id=origin_event_id,
            overseer_origin=overseer_origin or producer_id in self.journal.overseer_ids, before_ref=before_ref, after_ref=after_ref)
        # Observe rechecks the completed exact reference before persisting.
        if provenance_extra:
            exact["provenance"].update(provenance_extra)
        if after_ref is not None:
            exact["after_ref"] = {key: value for key, value in exact.items() if key not in {"before_ref", "after_ref", "material_ref"}}
        if native_archive is not None:
            from .provenance import content_hash
            saved = self.journal.reusable_entry(exact)
            if saved is not None:
                locator = saved["locator"]
                try:
                    receipt = native_archive.load_receipt(locator["receipt_id"])
                    if ((receipt.agent_id, receipt.provider, receipt.conversation_id, receipt.archive_id) !=
                            (locator["agent_id"], locator["provider"], locator["conversation_id"], locator["archive_id"])
                            or locator["position"] + locator["length"] > receipt.record_boundary):
                        raise ValueError("saved entry is outside its original receipt")
                    part = native_archive.read_raw(receipt, offset=locator["position"], limit=locator["length"])
                    if len(part.data) != locator["length"]:
                        raise ValueError("saved entry range is unavailable")
                except Exception as exc:
                    raise DomainError("evidence_unavailable", "Earlier transcript evidence cannot be verified") from exc
                exact = saved
            else:
                exact["revision"] = "entry-sha256:" + content_hash(self.journal._entry_signature(exact))
        return self.journal.observe(selection_key, _key([pre["namespace"], pre["kind"], pre["record_id"]]), exact,
                                    content, relations=relations, gaps=gaps)

    def capture(self, selection_key, namespace, kind, identity, uow):
        """Capture a bounded episode in the caller's Operations read transaction."""
        if kind == "task":
            return self._task(selection_key, namespace, identity, uow)
        if kind == "signoff":
            return self._signoff(selection_key, namespace, identity, uow)
        if kind == "action":
            return self._action(selection_key, namespace, identity, uow)
        if kind == "document":
            return self._document(selection_key, namespace, identity, uow)
        return self._transcript(selection_key, namespace, identity, uow)

    def _admit_notes(self, namespace, identity, kind, uow, *, cursor=None, limit=MAX_THREAD_PAGE_NOTES):
        from qorqut_operations.application.plans import page_position
        if kind == "signoff":
            ids = uow.connection.execute("SELECT id FROM signoff_notes WHERE signoff_id=? AND sequence<? "
                                         "ORDER BY sequence DESC LIMIT ?", (identity, cursor or 9223372036854775807, limit + 1))
            for row in ids:
                self.access.authorize_source(self._locator(namespace, "signoff_note", row[0], signoff_id=identity, note_id=row[0]))
        else:
            if kind == "action":
                action = uow.connection.execute("SELECT id,business_id FROM action_logs WHERE action_key=?", (identity,)).fetchone()
                if action is None:
                    raise DomainError("evidence_unavailable", "Action is unavailable")
                item_id, business_id = action[0], action[1]
            else:
                item_id, business_id = identity, namespace["business_id"]
            operation = "tasks_thread_get" if kind == "task" else "actions_thread_get"
            normalized = {"item_id": item_id, "principal_id": self.context.principal_id, "limit": limit,
                          "kinds": ["comment", "system"], **({"cursor": cursor} if cursor else {})}
            _, after = page_position(normalized, business_id, operation)
            bounds = " AND (created_at,id)<(?,?)" if after else ""
            ids = uow.connection.execute("SELECT id FROM thread_messages WHERE business_id IS ? AND item_type=? "
                "AND item_id=?" + bounds + " ORDER BY created_at DESC,id DESC LIMIT ?",
                (business_id, kind, item_id, *(after or ()), limit + 1))
            for row in ids:
                field = "task_id" if kind == "task" else "action_id"
                self.access.authorize_source(self._locator(namespace, kind + "_message", row[0], **{field: identity, "message_id": row[0]}))

    def _continuation_key(self, namespace, kind, identity):
        return _key([self.context.principal_id, namespace, kind, identity])

    def _mirror_status(self, note, uow, namespace):
        originals = []
        attribution = note.get("attribution") or {}
        source = attribution.get("source")
        if isinstance(source, dict) and source.get("provider") and source.get("session_id"):
            rows = uow.connection.execute("SELECT c.agent_id,p.home_business_id FROM native_captures c "
                "JOIN agent_profiles p ON p.agent_id=c.agent_id WHERE c.provider=? AND c.conversation_id=?",
                (source["provider"], source["session_id"])).fetchall()
            if any(row[0] in self.journal.overseer_ids for row in rows):
                return "excluded", []
            if len(rows) != 1:
                return "unknown", []
            source_ns = ({"kind": "business", "business_id": rows[0][1]} if rows[0][1]
                         else {"kind": "global", "business_id": None})
            pre = self._locator(source_ns, "transcript", _key([rows[0][0], source["provider"], source["session_id"]]),
                                agent_id=rows[0][0], provider=source["provider"], conversation_id=source["session_id"])
            try:
                self.access.authorize_source(pre)
            except DomainError:
                return "unknown", []
            originals.append(pre)
        for ref in note.get("refs", []):
            if not isinstance(ref, dict) or ref.get("unavailable"):
                return "unknown", []
            if ref.get("type") == "agent":
                if ref.get("id") in self.journal.overseer_ids:
                    return "excluded", []
                pre = self._locator({"kind": "global", "business_id": None}, "agent", ref.get("id"), agent_id=ref.get("id"))
                try:
                    self.access.authorize_source(pre)
                except DomainError:
                    return "unknown", []
                originals.append(pre)
            if ref.get("type") == "task":
                row = uow.connection.execute("SELECT created_by FROM tasks WHERE business_id IS ? AND id=?", (namespace["business_id"], ref.get("id"))).fetchone()
                if row is None:
                    return "unknown", []
                if row[0] in self.journal.overseer_ids:
                    return "excluded", []
                pre = self._locator(namespace, "task", ref["id"], task_id=ref["id"])
                try:
                    self.access.authorize_source(pre)
                except DomainError:
                    return "unknown", []
                originals.append(pre)
            if ref.get("type") == "action":
                row = uow.connection.execute("SELECT recorded_by,performed_by,action_key FROM action_logs WHERE business_id IS ? AND (id=? OR action_key=?)",
                    (namespace["business_id"], ref.get("id"), ref.get("id"))).fetchone()
                if row is None:
                    return "unknown", []
                if row[0] in self.journal.overseer_ids or row[1] in self.journal.overseer_ids:
                    return "excluded", []
                pre = self._locator(namespace, "action", row[2], action_id=row[2])
                try:
                    self.access.authorize_source(pre)
                except DomainError:
                    return "unknown", []
                originals.append(pre)
            if ref.get("type") == "signoff":
                row = uow.connection.execute("SELECT proposer_agent_id,proposer_principal_id,review_business_id FROM signoffs WHERE id=?", (ref.get("id"),)).fetchone()
                if row is None:
                    return "unknown", []
                if row[0] in self.journal.overseer_ids or row[1] in self.journal.overseer_ids:
                    return "excluded", []
                source_ns = ({"kind": "business", "business_id": row[2]} if row[2]
                             else {"kind": "global", "business_id": None})
                if self._review_signoff(ref['id'], uow):
                    return 'excluded', []
                pre = self._locator(source_ns, "signoff", ref["id"], signoff_id=ref["id"])
                try:
                    self.access.authorize_source(pre)
                except DomainError:
                    return "unknown", []
                originals.append(pre)
        return "clear", originals

    def _task(self, selection_key, namespace, identity, uow):
        pre = self._locator(namespace, "task", identity, task_id=identity)
        ops = self._ops(namespace)
        self.access.authorize_source(pre)
        self._admit_notes(namespace, identity, "task", uow)
        item = ops.read("tasks_get", {"task_id": identity}, pre, uow)["task"]
        episode = _episode(namespace, "task", identity)
        note_cursor = item.pop("notes_next_cursor", None)
        item.pop("notes", None)
        refs = self._save(selection_key, pre, item, revision=str(item["revision"]), episode_id=episode,
                          actor_id=None, actor_kind=None,
                          occurred_at=item.get("updated_at"), producer_id=item.get("created_by"),
                          relations=[{"kind": "parent_task", "record_id": item["parent_id"]}] if item.get("parent_id") else [],
                          gaps=[{"code": "task_edit_history_unavailable"}])
        position_key = self._continuation_key(namespace, "task_thread", identity)
        cursor = self.journal.continuation(selection_key, position_key)
        result, unresolved = [refs], False
        if item.get("created_by") in self.journal.overseer_ids:
            return result, []
        for _ in range(MAX_THREAD_PAGES):
            args = {"task_id": identity, "limit": MAX_THREAD_PAGE_NOTES}
            if cursor:
                args["cursor"] = cursor
            self._admit_notes(namespace, identity, "task", uow, cursor=cursor)
            thread = ops.read("tasks_thread_get", args, pre, uow)
            for note in thread["notes"]:
                mirror, linked_sources = self._mirror_status(note, uow, namespace)
                if mirror != "clear":
                    unresolved |= mirror == "unknown"
                    continue
                note_pre = self._locator(namespace, "task_message", note["id"], task_id=identity, message_id=note["id"])
                self.access.authorize_source(note_pre)
                attribution = note.get("attribution") or {}
                source = attribution.get("source")
                origins = ([source] if isinstance(source, dict) else []) + linked_sources
                from .provenance import native_event_id
                origin = (native_event_id(source["provider"], source["session_id"], source["message_id"])
                          if isinstance(source, dict) and all(source.get(k) for k in ("provider", "session_id", "message_id"))
                          else note.get("id") if attribution.get("kind") in ("self", "system") else None)
                result.append(self._save(selection_key, note_pre, note, revision="1", episode_id=episode,
                    actor_id=note.get("author_id"), actor_kind=note.get("author_kind"), occurred_at=note.get("created_at"),
                    producer_id=note.get("recorder_id"), original_refs=origins,
                    origin_event_id=origin,
                    relations=note.get("refs", [])))
            cursor = thread.get("next_cursor")
            if not cursor:
                break
        self.journal.set_continuation(selection_key, position_key, cursor)
        gaps = [{"code": "task_thread_continuation_pending"}] if cursor else []
        if unresolved:
            gaps.append({"code": "unresolved_provenance"})
        if item.get("related_tasks_next_cursor") or item.get("children_has_more"):
            gaps.append({"code": "task_relations_truncated"})
        return result, gaps

    @staticmethod
    def _review_signoff(identity, uow):
        # Review feedback (including Ordubashy's shared submissions and later
        # amendments) is retained separately, never fresh learning evidence.
        return uow.connection.execute('''WITH RECURSIVE ancestry(id) AS (
            SELECT ? UNION SELECT s.supersedes_id FROM signoffs s JOIN ancestry a ON s.id=a.id
            WHERE s.supersedes_id IS NOT NULL)
            SELECT 1 FROM ancestry a JOIN overseer_signoffs x ON x.signoff_id=a.id LIMIT 1''', (identity,)).fetchone() is not None

    def _signoff(self, selection_key, namespace, identity, uow, *, depth=0):
        if self._review_signoff(identity, uow):
            return [], []
        pre = self._locator(namespace, "signoff", identity, signoff_id=identity)
        ops = self._ops(namespace)
        args = {"signoff_id": identity, **({"scope": "global"} if namespace["kind"] == "global" else {})}
        self.access.authorize_source(pre)
        self._admit_notes(namespace, identity, "signoff", uow)
        metadata = uow.connection.execute("SELECT decision_json IS NOT NULL, apply_result_json IS NOT NULL, "
            "json_extract(decision_json,'$.message_id') FROM signoffs WHERE id=?", (identity,)).fetchone()
        if metadata is None:
            raise DomainError("evidence_unavailable", "Signoff is unavailable")
        response_pre = self._locator(namespace, "signoff_response", identity, signoff_id=identity)
        outcome_pre = self._locator(namespace, "execution_outcome", identity, signoff_id=identity)
        if metadata[0]:
            self.access.authorize_source(response_pre)
        if metadata[2]:
            self.access.authorize_source(self._locator(namespace, "signoff_note", metadata[2], signoff_id=identity, note_id=metadata[2]))
        if metadata[1]:
            self.access.authorize_source(outcome_pre)
        item = ops.read("signoffs_get", args, pre, uow)["signoff"]
        episode = _episode(namespace, "signoff", identity)
        producer = item.get("proposer_agent_id") or item.get("proposer_principal_id")
        decision, outcome = item.pop("decision", None), item.pop("apply_result", None)
        relations = [{"kind": name, "record_id": item[name]} for name in ("supersedes_id", "superseded_by_id") if item.get(name)]
        types = {"tasks": "task", "documents": "document", "action_logs": "action"}
        for target in item.get("expected_targets", []):
            resource = target.get("resource", {})
            if resource.get("resource_type") in types and resource.get("resource_id"):
                relations.append({"kind": types[resource["resource_type"]], "record_id": resource["resource_id"],
                                  "business_id": resource.get("business_id")})
        previous, edit_gaps = None, []
        if item.get("supersedes_id"):
            previous_pre = self._locator(namespace, "signoff", item["supersedes_id"], signoff_id=item["supersedes_id"])
            try:
                self.access.authorize_source(previous_pre)
                if depth >= 8:
                    edit_gaps.append({"code": "signoff_chain_truncated"})
                else:
                    earlier, earlier_gaps = self._signoff(selection_key, namespace, item["supersedes_id"], uow, depth=depth + 1)
                    previous = next((ref for ref in earlier if ref["kind"] == "signoff" and ref["record_id"] == item["supersedes_id"]), None)
                    edit_gaps.extend(earlier_gaps)
                    if previous is None:
                        edit_gaps.append({"code": "signoff_before_unavailable"})
            except DomainError:
                edit_gaps.append({"code": "signoff_before_forbidden"})
        edit = item.get("preview", {}).get("owner_edit") or {}
        author_id = edit.get("by") or item.get("proposer_principal_id")
        author = uow.connection.execute("SELECT kind FROM principals WHERE id=?", (author_id,)).fetchone() if author_id else None
        result = [self._save(selection_key, pre, item, revision=str(item["status_revision"]), episode_id=episode,
                  actor_id=author_id, actor_kind=author[0] if author else None, occurred_at=item.get("created_at"),
                  producer_id=producer, origin_event_id=identity, relations=relations, gaps=edit_gaps,
                  before_ref=previous, after_ref=pre if previous else None)]
        if producer in self.journal.overseer_ids:
            return result, []
        if decision:
            by = decision.get("by")
            actor = uow.connection.execute("SELECT kind FROM principals WHERE id=?", (by,)).fetchone() if by else None
            result.append(self._save(selection_key, response_pre, decision, revision=str(item["status_revision"]),
                episode_id=episode, actor_id=by, actor_kind=actor[0] if actor else None, occurred_at=decision.get("at"),
                producer_id=by, origin_event_id=decision.get("message_id") or (identity + ":decision")))
        if outcome is not None:
            result.append(self._save(selection_key, outcome_pre, outcome, revision=str(item["status_revision"]),
                episode_id=episode, actor_id=None, actor_kind=None, occurred_at=item.get("applied_at"), producer_id=producer,
                origin_event_id=identity + ":outcome"))
        position_key = self._continuation_key(namespace, "signoff_notes", identity)
        before, unresolved = self.journal.continuation(selection_key, position_key), False
        for _ in range(MAX_THREAD_PAGES):
            params = {**args, "limit": MAX_THREAD_PAGE_NOTES}
            if before:
                params["before"] = before
            self._admit_notes(namespace, identity, "signoff", uow, cursor=before)
            page = ops.read("signoffs_notes_list", params, pre, uow)
            for note in page["notes"]:
                mirror, linked_sources = self._mirror_status(note, uow, namespace)
                if mirror != "clear":
                    unresolved |= mirror == "unknown"
                    continue
                note_pre = self._locator(namespace, "signoff_note", note["id"], signoff_id=identity, note_id=note["id"])
                self.access.authorize_source(note_pre)
                result.append(self._save(selection_key, note_pre, note, revision="1", episode_id=episode,
                    actor_id=note.get("created_by"), actor_kind=note.get("author_kind"), occurred_at=note.get("created_at"),
                    producer_id=note.get("created_by"), origin_event_id=note["id"], original_refs=linked_sources, relations=note.get("refs", [])))
            before = page.get("next_before")
            if not before:
                break
        self.journal.set_continuation(selection_key, position_key, before)
        return result, edit_gaps + ([{"code": "signoff_notes_continuation_pending"}] if before else []) + ([{"code": "unresolved_provenance"}] if unresolved else [])

    def _action(self, selection_key, namespace, identity, uow):
        pre = self._locator(namespace, "action", identity, action_id=identity)
        ops = self._ops(namespace)
        self.access.authorize_source(pre)
        self._admit_notes(namespace, identity, "action", uow)
        item = ops.read("actions_get", {"action_id": identity}, pre, uow)["action"]
        episode = _episode(namespace, "action", identity)
        actor = item.get("performed_by") or item.get("recorded_by") or {}
        if actor.get("id") in self.journal.overseer_ids:
            return [], []
        result, link_gaps = [], []
        maximum = item["revision"]
        position_key = self._continuation_key(namespace, "action_history", identity)
        last = self.journal.continuation(selection_key, position_key) or 0
        if type(last) is not int or last < 0 or last > maximum:
            raise DomainError("evidence_unavailable", "Action history continuation is invalid")
        previous = self.journal.captured_revision("operations", "action_revision", namespace, identity, last) if last else None
        if last and previous is None:
            raise DomainError("evidence_unavailable", "Prior Action history observation is unavailable")
        end, completed = min(maximum, last + MAX_HISTORY_REVISIONS), last
        excluded_history = bool(previous and self.journal._excluded(previous))
        for number in range(last + 1, end + 1):
            if number > last + 1 and self.expired():
                break
            revision_pre = self._locator(namespace, "action_revision", identity, action_id=identity, revision=number)
            value = ops.read("actions_get", {"action_id": identity, "revision": number}, revision_pre, uow)["action"]
            # Existing Action reads may intentionally redact references. Consult
            # only the immutable revision's routing metadata to detect a hidden
            # parent; never expose that metadata when the reader redacted it.
            route = uow.connection.execute("SELECT json_extract(snapshot_json,'$.refs.task') FROM action_revisions "
                                           "WHERE action_key=? AND revision=?", (identity, number)).fetchone()
            if route is None:
                link_gaps.append({"code": "action_revision_unavailable"})
                break
            task_id = (value.get("refs") or {}).get("task")
            if route[0] and task_id != route[0]:
                link_gaps.append({"code": "related_source_forbidden"})
                break
            linked_task, excluded_parent = None, False
            if task_id:
                task_pre = self._locator(namespace, "task", task_id, task_id=task_id)
                try:
                    self.access.authorize_source(task_pre)
                    parent = uow.connection.execute("SELECT created_by FROM tasks WHERE business_id IS ? AND id=?",
                        (namespace["business_id"], task_id)).fetchone()
                except DomainError:
                    link_gaps.append({"code": "related_source_forbidden"})
                    break
                if parent is None:
                    link_gaps.append({"code": "related_source_unavailable"})
                    break
                excluded_parent = parent[0] in self.journal.overseer_ids
                linked_task = task_pre
            actor = value.get("recorded_by") or {}
            if excluded_parent:
                from .provenance import content_hash
                existing = self.journal.captured_revision("operations", "action_revision", namespace, identity, number)
                if existing is not None and existing["content_hash"] == content_hash(value):
                    # An immutable earlier observation may predate knowledge of
                    # its excluded Task ancestry. Keep its exact metadata and
                    # recheck that ancestry and preserve the saved record.
                    saved = {key: data for key, data in existing.items() if key != "material_ref"}
                    previous = self.journal.observe(selection_key, _key([namespace, "action_revision", identity]), saved, value)
                    completed, excluded_history = number, True
                    continue
            captured = self._save(selection_key, revision_pre, value, revision=str(number), episode_id=episode,
                actor_id=actor.get("id"), actor_kind=actor.get("kind"), occurred_at=value.get("recorded_at"),
                producer_id=actor.get("id"), origin_event_id=f"{identity}:r{number}",
                relations=([{"kind": "task", "record_id": task_id}] if task_id else []),
                original_refs=[linked_task] if linked_task else [], before_ref=previous,
                after_ref={**revision_pre, "revision": str(number)} if number > 1 else None,
                overseer_origin=excluded_parent)
            previous, completed = captured, number
            if self.journal._excluded(captured):
                excluded_history = True
            else:
                result.append(captured)
        self.journal.set_continuation(selection_key, position_key, completed if completed < maximum else None)
        gaps = link_gaps + ([{"code": "action_history_continuation_pending"}] if completed < maximum else [])
        if excluded_history or link_gaps:
            # A discussion bound to excluded or unresolved Action history cannot
            # safely be presented as an independent experience record.
            return result, gaps
        position_key = self._continuation_key(namespace, "action_thread", identity)
        cursor, unresolved = self.journal.continuation(selection_key, position_key), False
        for _ in range(MAX_THREAD_PAGES):
            args = {"action_id": identity, "limit": MAX_THREAD_PAGE_NOTES}
            if cursor:
                args["cursor"] = cursor
            self._admit_notes(namespace, identity, "action", uow, cursor=cursor)
            page = ops.read("actions_thread_get", args, pre, uow)
            for note in page["messages"]:
                mirror, linked_sources = self._mirror_status(note, uow, namespace)
                if mirror != "clear":
                    unresolved |= mirror == "unknown"
                    continue
                note_pre = self._locator(namespace, "action_message", note["id"], action_id=identity, message_id=note["id"])
                self.access.authorize_source(note_pre)
                attribution = note.get("attribution") or {}
                source = attribution.get("source")
                from .provenance import native_event_id
                origin = (native_event_id(source["provider"], source["session_id"], source["message_id"])
                          if isinstance(source, dict) and all(source.get(k) for k in ("provider", "session_id", "message_id"))
                          else note.get("id") if attribution.get("kind") in ("self", "system") else None)
                result.append(self._save(selection_key, note_pre, note, revision="1", episode_id=episode,
                    actor_id=note.get("author_id"), actor_kind=note.get("author_kind"), occurred_at=note.get("created_at"),
                    producer_id=note.get("recorder_id"), origin_event_id=origin,
                    original_refs=([source] if isinstance(source, dict) else []) + linked_sources, relations=note.get("refs", [])))
            cursor = page.get("next_cursor")
            if not cursor:
                break
        self.journal.set_continuation(selection_key, position_key, cursor)
        if cursor:
            gaps.append({"code": "action_thread_continuation_pending"})
        if unresolved:
            gaps.append({"code": "unresolved_provenance"})
        return result, gaps

    def _document_origin(self, namespace, identity, uow, seen):
        import json
        key = (_key(namespace), identity)
        if key in seen:
            return "clear", []
        if len(seen) >= 100:
            return "unknown", []
        seen.add(key)
        pre = self._locator(namespace, "document_revision", identity, document_id=identity)
        try:
            self.access.authorize_source(pre)
        except DomainError:
            return "unknown", []
        row = uow.connection.execute("SELECT kind,owning_agent_id,source_refs_json FROM documents WHERE id=? AND business_id IS ?",
                                     (identity, namespace["business_id"])).fetchone()
        if row is None or row["kind"] == "working_memory":
            return "unknown", []
        if row["owning_agent_id"] in self.journal.overseer_ids:
            return "excluded", []
        space = "business:" + namespace["business_id"] if namespace["business_id"] else "global"
        first = uow.connection.execute("SELECT author_id FROM qbdv_document_versions WHERE namespace=? AND document_id=? ORDER BY version LIMIT 1",
                                       (space, identity)).fetchone()
        if first and first[0] in self.journal.overseer_ids:
            return "excluded", []
        originals = [pre]
        types = {"tasks": "task", "signoffs": "signoff", "action_logs": "action", "agent_profiles": "agent"}
        for source in json.loads(row["source_refs_json"] or "[]"):
            if not isinstance(source, dict):
                return "unknown", []
            kind, source_id = source.get("resource_type"), source.get("resource_id")
            business = source.get("business_id")
            source_ns = {"kind": "business", "business_id": business} if business else {"kind": "global", "business_id": None}
            if kind == "documents":
                status, related = self._document_origin(source_ns, source_id, uow, seen)
            elif kind in types:
                status, related = self._mirror_status({"refs": [{"type": types[kind], "id": source_id}]}, uow, source_ns)
            else:
                return "unknown", []
            if status != "clear":
                return status, []
            originals.extend(related)
        return "clear", originals

    def _document(self, selection_key, namespace, identity, uow):
        pre = self._locator(namespace, "document_revision", identity, document_id=identity)
        self.access.authorize_source(pre)
        import json
        row = uow.connection.execute("SELECT kind,owning_agent_id,source_refs_json FROM documents WHERE id=? AND business_id IS ?", (identity, namespace["business_id"])).fetchone()
        if row is None:
            raise DomainError("evidence_unavailable", "Registered document is unavailable")
        if row["kind"] == "working_memory":
            return [], [{"code": "private_working_memory_not_eligible"}]
        if row["owning_agent_id"] in self.journal.overseer_ids:
            return [], []
        space = "business:" + namespace["business_id"] if namespace["business_id"] else "global"
        first = uow.connection.execute("SELECT author_id FROM qbdv_document_versions WHERE namespace=? AND document_id=? ORDER BY version LIMIT 1", (space, identity)).fetchone()
        if first and first[0] in self.journal.overseer_ids:
            return [], []
        status, linked_sources = self._document_origin(namespace, identity, uow, set())
        if status == "excluded":
            return [], []
        if status == "unknown":
            return [], [{"code": "unresolved_provenance"}]
        linked_sources = linked_sources[1:]  # The first locator is this document itself, not its origin.
        position_key = self._continuation_key(namespace, "document_history", identity)
        last = self.journal.continuation(selection_key, position_key) or 0
        if type(last) is not int or last < 0:
            raise DomainError("evidence_unavailable", "Document history continuation is invalid")
        versions = uow.connection.execute("SELECT version FROM qbdv_document_versions WHERE namespace=? AND document_id=? "
            "AND version>? ORDER BY version LIMIT ?", (space, identity, last, MAX_HISTORY_REVISIONS + 1)).fetchall()
        more = len(versions) > MAX_HISTORY_REVISIONS
        versions = versions[:MAX_HISTORY_REVISIONS]
        episode = _episode(namespace, "document", identity)
        result = []
        previous = self.journal.captured_revision("operations", "document_revision", namespace, identity, last) if last else None
        gaps = []
        for row_version in versions:
            if result and self.expired():
                more = True
                break
            number = row_version[0]
            if number > 1 and not result and previous is None:
                gaps.append({"code": "document_before_unavailable"})
            version_pre = self._locator(namespace, "document_revision", identity, document_id=identity, version=number)
            value = self.readers.document_revision(version_pre, uow)
            result.append(self._save(selection_key, version_pre, value, revision=str(number), episode_id=episode,
                actor_id=value["actor_id"], occurred_at=value["occurred_at"], producer_id=value["actor_id"],
                origin_event_id=f"document:{identity}:v{number}", original_refs=linked_sources,
                before_ref=result[-1] if result else previous,
                after_ref={**version_pre, "revision": str(number)} if number > 1 else None))
        self.journal.set_continuation(selection_key, position_key, int(result[-1]["revision"]) if more and result else None)
        if more:
            gaps.append({"code": "document_history_continuation_pending"})
        if not result and not last:
            gaps.append({"code": "document_history_unavailable"})
        return result, gaps

    def _transcript_material(self, agent, provider, conversation, receipt_id, uow):
        from bazarlyk.native_chat import store
        profile = store.active_profile(uow.connection, agent, self.runtime.paths, any_status=True)
        if profile is None:
            raise DomainError("evidence_unavailable", "Registered transcript agent is unavailable")
        archive = self.runtime.native_chat.indexer.archive(profile)
        try:
            receipt = archive.load_receipt(receipt_id)
        except Exception as exc:
            raise DomainError("evidence_unavailable", "Transcript receipt is unavailable") from exc
        if (receipt.agent_id, receipt.provider, receipt.conversation_id) != (agent, provider, conversation):
            raise DomainError("evidence_unavailable", "Transcript receipt does not match its registered conversation")
        return archive, receipt

    def _transcript(self, selection_key, namespace, identity, uow):
        from bazarlyk.native_chat import store
        from bazarlyk.native_chat.projection import (Lineage, Projector, ProjectorState, SPEAKER_AGENT, SPEAKER_NOTICE,
                                                      SPEAKER_USER, MAX_ENTRY_CHARS)
        from bazarlyk.native_chat.reader import proven_type, bound_completion
        from .provenance import native_event_id
        agent, provider, conversation = identity
        pre = self._locator(namespace, "transcript", _key(identity), agent_id=agent, provider=provider,
                            conversation_id=conversation)
        self.access.authorize_source(pre)
        if agent in self.journal.overseer_ids:
            return [], []
        row = uow.connection.execute("SELECT receipt_id,updated_at,complete,error_code,transcript_path FROM native_captures "
            "WHERE agent_id=? AND provider=? AND conversation_id=?", (agent, provider, conversation)).fetchone()
        if row is None or not row["receipt_id"]:
            return [], [{"code": "transcript_unavailable"}]
        pre["locator"]["receipt_id"] = row["receipt_id"]
        self.access.authorize_source(pre)
        archive, receipt = self._transcript_material(agent, provider, conversation, row["receipt_id"], uow)
        lineage = Lineage(agent, provider, conversation, receipt.archive_id)
        position_key = self._continuation_key(namespace, "transcript", identity)
        saved = self.journal.continuation(selection_key, position_key)
        start, blocked, saved_state = 0, False, None
        if saved:
            try:
                earlier = archive.load_receipt(saved["receipt_id"])
                if ((earlier.agent_id, earlier.provider, earlier.conversation_id, earlier.archive_id)
                        != (agent, provider, conversation, receipt.archive_id)
                        or type(saved["offset"]) is not int or saved["offset"] < 0
                        or saved["offset"] > earlier.record_boundary or saved["offset"] > receipt.record_boundary):
                    raise ValueError("invalid transcript continuation")
                if saved["offset"]:
                    archive.read_raw(earlier, offset=0, limit=1)
                start, blocked, saved_state = saved["offset"], bool(saved["blocked"]), saved["state"]
            except Exception as exc:
                raise DomainError("evidence_unavailable", "Transcript continuation is unavailable") from exc
        projector = Projector(provider, conversation, state=ProjectorState.load(saved_state) if saved_state else None)
        episode = _episode(namespace, "transcript", identity)
        result, gaps, count, record_count, continuation = [], [], 0, 0, None
        owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
        owner_id = owner[0] if owner else None
        try:
            with archive.iter_records(receipt, offset=start) as records:
                for record in records:
                    if record.end > receipt.record_boundary:
                        break
                    if ((record_count and self.expired()) or count >= MAX_TRANSCRIPT_ENTRIES or record_count >= MAX_TRANSCRIPT_RECORDS
                            or (record_count and record.end - start > MAX_TRANSCRIPT_PAGE_BYTES)):
                        continuation = {"receipt_id": receipt.receipt_id, "offset": record.offset,
                                        "state": projector.state.dump(), "blocked": blocked}
                        break
                    record_count += 1
                    if record.length > MAX_TRANSCRIPT_PAGE_BYTES:
                        gaps.append({"code": "unresolved_provenance", "reason": "oversized_record", "offset": record.offset})
                        blocked = True
                        continue
                    if record.kind != "json":
                        if record.kind not in ("blank",):
                            gaps.append({"code": "unresolved_provenance", "reason": record.kind, "offset": record.offset})
                        continue
                    value = record.value
                    payload = value.get("payload", {}) if isinstance(value, dict) else {}
                    inner = payload.get("type") if isinstance(payload, dict) else None
                    if inner in {"task_started", "turn_started"}:
                        blocked = False
                    projected = projector.feed(record)
                    if ((isinstance(value, dict) and value.get("type") == "user" and
                        any(isinstance(part, dict) and part.get("type") == "tool_result" for part in
                            ((value.get("message") or {}).get("content") or []) if isinstance((value.get("message") or {}).get("content"), list)))
                        or (inner == "item_completed" and isinstance(payload.get("item"), dict)
                            and payload["item"].get("type") == "McpToolCall")):
                        gaps.append({"code": "tool_result_unresolved", "offset": record.offset})
                    for entry in projected.entries:
                        count += 1
                        locator = {"agent_id": agent, "provider": provider, "conversation_id": conversation,
                                   "archive_id": receipt.archive_id, "receipt_id": receipt.receipt_id,
                                   "position": entry.offset, "length": entry.length,
                                   "identity_kind": entry.identity_kind, "identity_key": entry.key}
                        entry_id = entry.public_id(lineage)
                        entry_pre = self._locator(namespace, "transcript_entry", entry_id, **locator)
                        self.access.authorize_source(entry_pre)
                        if entry.turn_end:
                            replies = {value["raw_offset"]: value for value in store.replies_at(
                                uow.connection, agent, provider, conversation, [entry.offset])}
                            if bound_completion(entry, replies) is None:
                                gaps.append({"code": "unresolved_provenance", "reason": "unbound_completion", "offset": entry.offset})
                                continue
                        if entry.speaker == SPEAKER_NOTICE:
                            if entry.receipts:
                                gaps.append({"code": "tool_result_unresolved", "offset": entry.offset})
                                continue
                            marker_rows = store.conversation_markers(uow.connection, agent, provider, conversation,
                                                                     [entry.marker], row["transcript_path"])
                            marker = marker_rows.get(entry.marker)
                            if entry.marker and proven_type(marker, entry.marker_type) is None:
                                gaps.append({"code": "unresolved_provenance", "reason": "unverified_marker", "offset": entry.offset})
                            blocked = True
                            continue
                        actor_id, actor_kind, event_id = agent, "agent", entry_id
                        original_sources, association_info, eligible_text = [], None, entry.text
                        if entry.speaker == SPEAKER_USER:
                            # Only an existing receipt-to-exact-record association establishes ownership.
                            association = uow.connection.execute(
                                "SELECT i.principal_id,i.input_type,i.origin,i.state,i.id,i.source_key,i.reason,"
                                "json_extract(i.evidence_json,'$.receipt_id'),json_type(i.evidence_json,'$.processing_confirmed'),i.sensitive "
                                "FROM native_inputs i JOIN native_sessions s ON s.id=i.session_id "
                                "WHERE i.agent_id=? AND s.agent_id=? AND s.provider=? AND s.conversation_id=? "
                                "AND (s.transcript_path IS NULL OR s.transcript_path=?) "
                                "AND json_type(i.evidence_json,'$.native_input_observed')='true' "
                                "AND json_extract(i.evidence_json,'$.raw_offset')=? LIMIT 2",
                                (agent, agent, provider, conversation, row["transcript_path"], entry.offset)).fetchall()
                            if (len(association) != 1 or association[0][0] not in (None, owner_id) or association[0][1] != "telegram"
                                    or association[0][2] != "owner" or association[0][3] != "uncertain"
                                    or association[0][6] != "transcript_confirmed" or association[0][8] != "false" or association[0][9]
                                    or not association[0][7]):
                                gaps.append({"code": "unresolved_provenance", "reason": "unbound_native_input", "offset": entry.offset})
                                blocked = True
                                continue
                            try:
                                observed = archive.load_receipt(association[0][7])
                                if ((observed.agent_id, observed.provider, observed.conversation_id, observed.archive_id)
                                        != (agent, provider, conversation, receipt.archive_id)
                                        or observed.record_boundary < entry.offset + entry.length):
                                    raise ValueError("wrong receipt lineage or range")
                                raw = archive.read_raw(observed, offset=entry.offset, limit=entry.length)
                                if len(raw.data) != entry.length:
                                    raise ValueError("incomplete observed range")
                            except Exception:
                                gaps.append({"code": "unresolved_provenance", "reason": "association_receipt_unavailable", "offset": entry.offset})
                                blocked = True
                                continue
                            input_pre = self._locator(namespace, "native_input", association[0][4],
                                agent_id=agent, provider=provider, conversation_id=conversation, input_id=association[0][4])
                            self.access.authorize_source(input_pre)
                            original_sources.append(input_pre)
                            source_key = association[0][5] or ""
                            if not source_key.startswith("telegram:"):
                                gaps.append({"code": "unresolved_provenance", "reason": "telegram_source_missing", "offset": entry.offset})
                                blocked = True
                                continue
                            telegram_id = source_key.removeprefix("telegram:")
                            telegram_pre = self._locator(namespace, "telegram_owner_input", telegram_id, owner_input_id=telegram_id, agent_id=agent)
                            self.access.authorize_source(telegram_pre)
                            telegram = uow.connection.execute("SELECT purpose,correlation_id,signoff_id,owner_principal_id,agent_id,sensitive,message_id "
                                "FROM telegram_owner_inputs WHERE id=?", (telegram_id,)).fetchone()
                            if (telegram is None or telegram[3] != owner_id or telegram[4] != agent
                                    or telegram[5] or not telegram[1]):
                                gaps.append({"code": "unresolved_provenance", "reason": "telegram_source_missing", "offset": entry.offset})
                                blocked = True
                                continue
                            original_sources.append(telegram_pre)
                            message_pre = self._locator(namespace, "telegram_message", telegram[6], message_id=telegram[6])
                            self.access.authorize_source(message_pre)
                            message = uow.connection.execute("SELECT body,direction,sensitive,body_redacted,reply_to_id FROM telegram_messages WHERE id=?",
                                (telegram[6],)).fetchone()
                            if (message is None or message[1] != "inbound" or message[2] or message[3]
                                    or not isinstance(message[0], str)):
                                gaps.append({"code": "unresolved_provenance", "reason": "telegram_message_unavailable", "offset": entry.offset})
                                blocked = True
                                continue
                            if message[4] and telegram[0] != "signoff_reply":
                                parent = uow.connection.execute("SELECT agent_id FROM telegram_messages WHERE id=?", (message[4],)).fetchone()
                                if parent is None or parent[0] is None:
                                    gaps.append({"code": "unresolved_provenance", "reason": "telegram_reply_origin_unknown", "offset": entry.offset})
                                    blocked = True
                                    continue
                                if parent[0] in self.journal.overseer_ids:
                                    blocked = True
                                    continue
                            original_sources.append(message_pre)
                            eligible_text = message[0]
                            actor_id, actor_kind, event_id = owner_id, "human", telegram[1]
                            association_info = {"association_kind": "host_reconciled_observation",
                                "association_method": "exact_text_and_session_time_window", "processing_confirmed": False,
                                "owner_principal_source": "telegram_owner_input", "owner_principal_ref": telegram_pre,
                                "native_principal_id": association[0][0],
                                "native_input_ref": input_pre, "owner_input_ref": telegram_pre,
                                "receipt_ref": {"archive_id": observed.archive_id, "receipt_id": observed.receipt_id,
                                                "offset": entry.offset, "length": entry.length}}
                            if telegram[0] == "signoff_reply":
                                signoff = uow.connection.execute("SELECT proposer_agent_id,proposer_principal_id,review_business_id FROM signoffs WHERE id=?",
                                    (telegram[2],)).fetchone()
                                if signoff is None:
                                    gaps.append({"code": "unresolved_provenance", "reason": "signoff_source_missing", "offset": entry.offset})
                                    blocked = True
                                    continue
                                if signoff[0] in self.journal.overseer_ids or signoff[1] in self.journal.overseer_ids:
                                    blocked = True
                                    continue
                                source_namespace = ({"kind": "business", "business_id": signoff[2]} if signoff[2]
                                                    else {"kind": "global", "business_id": None})
                                source_pre = self._locator(source_namespace, "signoff_note", telegram[1], signoff_id=telegram[2], note_id=telegram[1])
                                try:
                                    self.access.authorize_source(source_pre)
                                except DomainError:
                                    gaps.append({"code": "unresolved_provenance", "reason": "signoff_source_forbidden", "offset": entry.offset})
                                    blocked = True
                                    continue
                                original_sources.append(source_pre)
                                note = uow.connection.execute("SELECT body FROM signoff_notes WHERE id=? AND signoff_id=?", (telegram[1], telegram[2])).fetchone()
                                if note is None:
                                    gaps.append({"code": "unresolved_provenance", "reason": "signoff_source_missing", "offset": entry.offset})
                                    blocked = True
                                    continue
                                eligible_text = note[0]
                        elif entry.speaker != SPEAKER_AGENT or blocked:
                            continue
                        if entry.references is not None:
                            # Presentations can carry reference identities, but a referenced Overseer
                            # source is not eligible and unknown reference types are withheld.
                            safe = True
                            for kind, ref_id, business_id in entry.references:
                                if kind == "delta" or (kind == "agent" and ref_id in self.journal.overseer_ids):
                                    safe = False
                                    break
                                if kind not in {"task", "action", "signoff", "agent", "document"}:
                                    safe = False
                                    gaps.append({"code": "unresolved_provenance", "reason": "presentation_reference", "offset": entry.offset})
                                    break
                                target_namespace = ({"kind": "business", "business_id": business_id} if business_id
                                                    else {"kind": "global", "business_id": None} if kind == "agent" else namespace)
                                linked_pre = self._locator(target_namespace, kind, ref_id, **{kind + "_id": ref_id})
                                try:
                                    self.access.authorize_source(linked_pre)
                                    original_sources.append(linked_pre)
                                except DomainError:
                                    safe = False
                                    gaps.append({"code": "unresolved_provenance", "reason": "presentation_reference_forbidden", "offset": entry.offset})
                                    break
                                if kind == "signoff":
                                    other = uow.connection.execute("SELECT proposer_agent_id,proposer_principal_id FROM signoffs WHERE id=?", (ref_id,)).fetchone()
                                    if not other:
                                        safe = False
                                        gaps.append({"code": "unresolved_provenance", "reason": "presentation_reference_missing", "offset": entry.offset})
                                        break
                                    if other[0] in self.journal.overseer_ids or other[1] in self.journal.overseer_ids:
                                        safe = False
                                        break
                            if not safe:
                                continue
                        if entry.identity_kind == "native_id" and event_id == entry_id:
                            event_id = native_event_id(provider, conversation, entry.key)
                        content = {"speaker": entry.speaker, "text": eligible_text, "timestamp": entry.timestamp,
                                   "references": list(entry.references) if entry.references is not None else None}
                        if len(eligible_text) >= MAX_ENTRY_CHARS:
                            gaps.append({"code": "transcript_entry_truncated", "offset": entry.offset})
                            continue
                        result.append(self._save(selection_key, entry_pre, content, revision=receipt.receipt_id,
                            episode_id=episode, actor_id=actor_id, actor_kind=actor_kind, occurred_at=entry.timestamp,
                            producer_id=actor_id, origin_event_id=event_id, original_refs=original_sources,
                            provenance_extra=association_info, native_archive=archive))
                    if projected.completion is not None or inner in {"task_complete", "turn_complete", "turn_aborted"}:
                        blocked = False
        except DomainError:
            raise
        except Exception as exc:
            raise DomainError("evidence_unavailable", "Transcript projection is unavailable") from exc
        self.journal.set_continuation(selection_key, position_key, continuation)
        if continuation:
            gaps.append({"code": "transcript_continuation_pending", "offset": continuation["offset"]})
        if not row["complete"]:
            gaps.append({"code": "transcript_capture_incomplete"})
        if row["error_code"]:
            gaps.append({"code": "transcript_capture_error"})
        return result, gaps


class EvidenceProvider:
    """The four-method host boundary, bound to a current authenticated actor."""
    def __init__(self, runtime, context, archive_root, access, *, overseer_ids, coverage=None):
        import sqlite3
        self.access = _access(access)
        self.journal = CaptureJournal(archive_root, access, coverage, overseer_ids=overseer_ids)
        self.scanner = HostScanner(runtime, context, access, self.journal)
        self.journal.origin_check = self.scanner._current_origin_excluded
        with sqlite3.connect(self.journal.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS scan_progress (selection_key TEXT PRIMARY KEY, position TEXT, generation INTEGER NOT NULL)")

    def _inventory_cursor(self, cursor, current):
        """Recover the server-saved scope of legacy cursors; the cursor never grants access."""
        import base64
        import json
        import sqlite3
        from .provenance import content_hash
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
                raise ValueError("cursor size")
            saved = json.loads(base64.b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4),
                                               altchars=b"-_", validate=True))
            if (not isinstance(saved, dict) or set(saved) != {"selection", "provider", "generation", "after"}
                    or saved["provider"] != "host_inventory" or not isinstance(saved["selection"], str)
                    or type(saved["generation"]) is not int or saved["generation"] < 1
                    or type(saved["after"]) is not int or saved["after"] < 0):
                raise ValueError("cursor fields")
            with sqlite3.connect(self.journal.path) as db:
                row = db.execute("SELECT selection FROM frontiers WHERE selection_key=? ORDER BY captured_at DESC LIMIT 1",
                                 (saved["selection"],)).fetchone()
            if row is None:
                raise ValueError("unknown inventory scope")
            frozen = json.loads(row[0])
            if content_hash(frozen) != saved["selection"]:
                raise ValueError("saved scope identity")
            if frozen != current:
                # Only changing all_authorized membership is a continuation. A different filter or
                # explicit scope remains a different scan, even if its namespaces overlap.
                fields = ("agent_ids", "task_ids", "work_types", "workflow_refs")
                if (frozen.get("mode") != "all_authorized" or current.get("mode") != "all_authorized"
                        or any(frozen.get(key, []) != current.get(key, []) for key in fields)):
                    raise ValueError("wrong selection")
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise DomainError("validation_error", "Cursor does not match this selection") from exc
        return _admit_inventory_scope(self.access, frozen), saved["selection"], saved["after"]

    def list_changes(self, selection, cursor=None, limit=50):
        import base64
        import json
        import sqlite3
        import time
        from .provenance import content_hash
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError("validation_error", "Limit must be from 1 through 100")
        # Limit is a maximum, not a promise to fill a page. Leave time for source
        # admission, the frozen frontier and delivery retention under MCP's 60s call.
        limit = min(limit, 20)
        self.scanner.deadline = time.monotonic() + SCAN_SECONDS
        current = resolved = resolve_selection(self.access, selection)
        selection_key = content_hash(resolved)
        pending_after = 0
        if cursor:
            resolved, selection_key, pending_after = self._inventory_cursor(cursor, current)
        with sqlite3.connect(self.journal.path) as db:
            progress = db.execute("SELECT position,generation FROM scan_progress WHERE selection_key=?", (selection_key,)).fetchone()
        position, generation = (json.loads(progress[0]) if progress and progress[0] else None,
                                progress[1] if progress else 1)
        gaps = []
        additional = [ns for ns in current["namespaces"] if ns not in resolved["namespaces"]]
        if additional:
            gaps.append({"code": "inventory_scope_expanded",
                         "detail": "Continuing the saved inventory scope. After its cursor drains, scan only additional_selection; do not restart this inventory.",
                         "additional_selection": {**selection, "mode": "explicit", "namespaces": additional}})
        if resolved.get("task_ids"):
            gaps.append({"code": "task_relation_scope_incomplete"})
        if resolved.get("agent_ids"):
            gaps.append({"code": "agent_relation_scope_incomplete"})
        with self.scanner.runtime.database.read() as uow:
            candidates = self.scanner.keys(resolved, uow, after=position, limit=limit)
            more = len(candidates) > limit
            for index, (key, namespace, kind, identity) in enumerate(candidates[:limit]):
                if index and self.scanner.expired():
                    more = True
                    break
                try:
                    _, source_gaps = self.scanner.capture(selection_key, namespace, kind, identity, uow)
                    gaps.extend(source_gaps)
                except DomainError as error:
                    gaps.append({"code": error.code, "provider": "native_archive" if kind == "transcript" else "operations"})
                position = key
        if not more:
            position, generation = None, generation + 1
        with sqlite3.connect(self.journal.path) as db:
            db.execute("INSERT INTO scan_progress(selection_key,position,generation) VALUES(?,?,?) "
                       "ON CONFLICT(selection_key) DO UPDATE SET position=excluded.position,generation=excluded.generation",
                       (selection_key, json.dumps(position) if position is not None else None, generation))
            # A source may span several inventory sweeps. Keep a cursor until
            # all saved source continuations have drained, including ones first
            # encountered on an earlier page of this sweep.
            continuing = db.execute("SELECT 1 FROM source_continuations WHERE selection_key=? "
                                    "AND value IS NOT NULL AND value!='null' LIMIT 1", (selection_key,)).fetchone()
        pending = self.journal.pending(selection_key, after=pending_after, limit=limit)
        gaps.extend(pending["gaps"])
        frontier = self.journal.frontier(resolved, selection_key, gaps=gaps,
                                         episode_ids=[ref["episode_id"] for ref in pending["items"]], source_refs=pending["items"],
                                         frozen_inventory=True)
        gaps = frontier["gaps"]
        next_cursor = None
        if more or continuing or pending["next_after"] is not None:
            payload = {"selection": selection_key, "provider": "host_inventory", "generation": generation,
                       "after": pending["next_after"] if pending["next_after"] is not None else pending_after}
            next_cursor = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        return {"schema_version": 1, "items": pending["items"], "frontier": frontier,
                "next_cursor": next_cursor, "availability": "partial" if gaps else "available",
                "complete": next_cursor is None and not gaps, "gaps": gaps}

    def get_episode(self, episode_ref, frontier, cursor=None, max_chars=160000):
        return {"schema_version": 1, **self.journal.episode(episode_ref, frontier, cursor=cursor, max_chars=max_chars)}

    def get_source(self, source_ref):
        return {"schema_version": 1, "source_ref": source_ref, "availability": "available",
                "content": self.journal.get_source(source_ref)}

    def capture_presentation(self, run_ref, records, source_refs, operation_id):
        if not isinstance(source_refs, list) or not all(isinstance(ref, Mapping) for ref in source_refs):
            raise DomainError("validation_error", "Source references must be a list")
        if any(ref.get("provider") == "native_archive" and ref.get("kind") == "transcript" for ref in source_refs):
            raise DomainError("forbidden", "Raw transcript parents are not extraction presentations")
        return self.journal.archive.capture_retention(run_ref, records, source_refs, operation_id)
