"""Transport-independent tools bound to one pending event and its published base."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import math
import re

from .input_modes import GENERAL, ACTION
from .schema import Conflict, ContextError, encode, identifier, text, value_key


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


STRING = {"type": "string"}
ID = {"type": "string", "pattern": "^[a-z0-9]{5}$"}
KIND = {"type": "string", "enum": ["node", "edge"]}
REF = {"type": "string", "pattern": "^(self|[0-9a-f]{40}|[0-9a-f]{64})$"}
REFS = {"type": "array", "items": REF}
ATTRIBUTES = {"type": "object", "additionalProperties": {
    "type": "array", "items": {"type": ["string", "number", "boolean"]}}}
PAGE = {"offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100}}
TEXT_PAGE = {**PAGE, "limit": {"type": "integer", "minimum": 1, "maximum": 64000}}
DEVELOPMENTS = {"type": "array", "items": obj({
    "date": {"type": ["string", "integer"]}, "description": STRING, "citations": REFS},
    ("date", "description", "citations"))}
DETAILS = {"explanation": STRING, "citations": REFS, "developments": DEVELOPMENTS}


def validate(value, schema, label="arguments"):
    """Validate the JSON-schema subset used by this registry, including unknown fields."""
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "boolean": type(value) is bool,
               "integer": type(value) is int,
               "number": type(value) in (int, float) and math.isfinite(value)}
    if types and not any(matches.get(kind, False) for kind in types):
        raise ContextError(f"{label} must have type {'/'.join(types)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContextError(f"{label} must be one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if not set(schema.get("required", [])) <= set(value):
            raise ContextError(f"{label} is missing required fields")
        for key, member in value.items():
            if not isinstance(key, str):
                raise ContextError(f"{label} keys must be strings")
            child = properties.get(key, schema.get("additionalProperties", True))
            if child is False:
                raise ContextError(f"Unknown field: {label}.{key}")
            if isinstance(child, dict):
                validate(member, child, f"{label}.{key}")
    elif isinstance(value, list):
        for member in value:
            validate(member, schema.get("items", {}), label + "[]")
    elif isinstance(value, str) and "pattern" in schema:
        if not re.fullmatch(schema["pattern"], value):
            raise ContextError(f"Invalid {label}")
    elif type(value) in (int, float):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ContextError(f"{label} is outside the allowed range")


def page(items, offset=0, limit=20):
    selected = items[offset:offset + limit]
    more = offset + len(selected) < len(items)
    return {"items": selected, "offset": offset, "limit": limit, "total": len(items),
            "has_more": more, "next_offset": offset + len(selected) if more else None}


def text_page(content, offset=0, limit=16000):
    if content is None:
        return {"text": None, "offset": offset, "total_characters": 0,
                "has_more": False, "truncated": False, "next_offset": None}
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextError("This source is not model-readable UTF-8") from error
    selected = content[offset:offset + limit]
    more = offset + len(selected) < len(content)
    return {"text": selected, "offset": offset, "total_characters": len(content),
            "has_more": more, "truncated": offset > 0 or more,
            "next_offset": offset + len(selected) if more else None}


class EventTools:
    """All read/write capabilities for a single propagation attempt; no publication tool."""

    def __init__(self, context, event_id, *, max_response_bytes=256 * 1024):
        self.context, self.event_id = context, identifier(event_id)
        draft = context.inspect_draft(event_id)
        self.base = context.snapshot(draft["base_revision"])
        self.input_mode = draft["input_mode"]
        self.required_reassessment = context.required_reassessment(draft)
        self.max_response_bytes = max_response_bytes
        self.completion = None
        self._finish_args = None
        self._registry = {}
        self._register()

    def _add(self, name, description, schema, handler):
        self._registry[name] = ({"type": "function", "name": name,
                                 "description": description, "inputSchema": schema}, handler)

    @property
    def definitions(self):
        return [deepcopy(spec) for spec, _ in self._registry.values()]

    def _register(self):
        self._add("search_context", "Search the published base (draft excluded) by literal words/prefixes and exact attribute members. Default limit 20, maximum 100; has_more invites a narrower query, not pagination.",
                  obj({"query": STRING, "kind": {"type": "string", "enum": ["node", "edge", "source"]},
                       "attributes": ATTRIBUTES, "limit": PAGE["limit"]}), self._search)
        self._add("read_object", "Read an effective node/edge with current support, including staged puts/removals; creation IDs are immediately readable.",
                  obj({"kind": KIND, "id": ID}, ("kind", "id")), self._read_object)
        self._add("read_draft", "Read this event's staged changes, including full records and support, with offset/limit (20 default, 100 maximum). Also reports the complete required reassessment list.",
                  obj(PAGE), self._read_draft)
        self._add("list_objects", "Page effective graph objects in stable ID order, with their support; includes staged changes. Offset/limit: 20 default, 100 maximum.",
                  obj({"kind": KIND, **PAGE}, ("kind",)), self._list_objects)
        self._add("neighbors", "Page effective incident edges, ordered by edge ID, with direction, endpoints and neighboring records. A staged node deletion does not hide its remaining edges. Default 20, max 100.",
                  obj({"id": ID, "direction": {"type": "string", "enum": ["in", "out", "both"]}, **PAGE}, ("id",)), self._neighbors)
        self._add("read_catalog", "List all node/edge attribute names, or categorical string/boolean values for one attribute, including staged vocabulary. No type filtering or numeric choices/ranges. Offset/limit: 20 default, 100 max.",
                  obj({"kind": KIND, "attribute": STRING, **PAGE}, ("kind",)), self._catalog)
        self._add("list_sources", "Page managed source metadata from the published base, including removed status, in source ID order. The pending source is supplied in the event payload. Default 20, max 100.",
                  obj(PAGE), self._sources)
        self._add("read_source", "Read a current managed source at the published base, by source ID only. Text offset/limit use characters: default 16000, max 64000; follow next_offset if truncated.",
                  obj({"id": ID, **TEXT_PAGE}, ("id",)), self._source)
        self._add("read_evidence", "Resolve a full source-event commit from the published base history to its immutable event and before/after source text. Choose version before or after. Character pages default 16000, max 64000. No paths or other revisions.",
                  obj({"commit": {"type": "string", "pattern": "^([0-9a-f]{40}|[0-9a-f]{64})$"},
                       "version": {"type": "string", "enum": ["before", "after"]}, **TEXT_PAGE}, ("commit", "version")), self._evidence)
        self._add("object_history", "Page immutable published object/source history; all self references resolve to full source-event commits. Draft excluded. Default 20, max 100.",
                  obj({"id": ID, "kind": {"type": "string", "enum": ["node", "edge", "source"]}, **PAGE}, ("id", "kind")), self._history)
        self._add("action_lookup", "Read a shared accepted action at the published base by opaque reference. Audit pages include corrections and withdrawals; current view returns its latest revision. Default 20, max 100.",
                  obj({"action_ref": STRING, "audit": {"type": "boolean"}, **PAGE}, ("action_ref",)),
                  lambda **args: self._view(self.base.action(**args), published=True))
        self._add("actions_history", "Page published Actions independently of graph subjects. Filter immutable revision tags by tag_id; global_only selects revisions with no linked business tag. Filters combine. Current view includes latest non-withdrawn Actions; audit includes all revisions and withdrawals. Default 20, max 100.",
                  obj({"tag_id": STRING, "global_only": {"type": "boolean"},
                       "audit": {"type": "boolean"}, **PAGE}),
                  lambda **args: self._view(self.base.actions_history(**args), published=True))
        self._add("action_timeline", "Page an object's shared action associations at the published base. Historical removed objects are valid. Current view shows current associations; audit includes corrections and withdrawals. Default 20, max 100.",
                  obj({"kind": KIND, "id": ID, "audit": {"type": "boolean"}, **PAGE}, ("kind", "id")),
                  lambda kind, id, **args: self._view(self.base.action_timeline(kind, id, **args), published=True))
        self._add("stage_handoff_note", "Replace this event's single main-agent note with all questions and low-confidence items. GENERAL also includes operational claims and requested work. Empty text explicitly means nothing to hand off. Required before publication; a blocked finish requires a nonempty note explaining what needs resolution. Prepared only, never delivered; bounded by source byte limit.",
                  obj({"text": STRING}, ("text",)), self._handoff)
        if self.input_mode == ACTION:
            self._add("stage_action_associations", "Replace this action's full association set. Date uses ISO or unknown 0; summary/explanations <=150 words. Associate relevant known historical or draft objects; an explicit empty set is valid when no suitable subject exists. Tags are grouping metadata, never graph subjects. Withdrawal needs empty set and previous date. Links cite self automatically.",
                      obj({"date": {"type": ["string", "integer"]}, "summary": STRING,
                           "associations": {"type": "array", "items": obj({"kind": KIND, "id": ID, "explanation": STRING}, ("kind", "id", "explanation"))}},
                          ("date", "summary", "associations")), self._associations)
        details = DETAILS if self.input_mode == ACTION else {k: v for k, v in DETAILS.items() if k != "developments"}
        development_help = " Optional dated developments are allowed." if self.input_mode == ACTION else " GENERAL input cannot record developments."
        for kind in ("node", "edge"):
            fields = {"id": ID, "summary": STRING, "attributes": ATTRIBUTES}
            required = ["summary", "attributes"]
            if kind == "edge":
                fields.update(from_id=ID, to_id=ID)
                required += ["from_id", "to_id"]
            self._add("put_" + kind, "Stage a complete " + kind + " record. Omit id to allocate a new ID; supply an existing ID to replace it. Nodes require basic_entity_type. Supply a <=150-word explanation, <=15 commit/self citations, complete support_after, and full support. Endpoints/support are validated at finish. No mutations after finish." + development_help,
                      obj({"record": obj(fields, required), "support_after": REFS, **details},
                          ("record", "support_after", "explanation", "citations")),
                      lambda kind=kind, **args: self._put(kind, **args))
            self._add("remove_" + kind, "Stage explicit removal of a " + kind + ". Supply explanation (<=150 words), commit/self citations (<=15). Node removal does not cascade; inspect neighbors and handle edges explicitly. No mutations after finish." + development_help,
                      obj({"id": ID, **details}, ("id", "explanation", "citations")),
                      lambda kind=kind, **args: self._remove(kind, **args))
        self._add("finish_propagation", "Finish with complete or blocked and a brief factual summary/reason. For complete, list the exact required reassessed kind/IDs; validates and seals the draft, returning its digest without publishing. Blocked requires a nonempty handoff note and never publishes. An identical repeat is idempotent. Then end the model turn.",
                  obj({"status": {"type": "string", "enum": ["complete", "blocked"]},
                       "summary": STRING, "reassessed": {"type": "array", "items": obj({"kind": KIND, "id": ID}, ("kind", "id"))}},
                      ("status", "summary", "reassessed")), self._finish)

    def dispatch(self, name, arguments):
        try:
            if name not in self._registry:
                raise ContextError("Unknown context tool")
            spec, handler = self._registry[name]
            validate(arguments, spec["inputSchema"])
            result = {"ok": True, "result": handler(**arguments)}
            if len(encode(result)) > self.max_response_bytes:
                raise ContextError("Tool response exceeds configured byte limit; request a smaller page or narrower query")
            return result
        except (ContextError, OSError) as error:
            message = str(error) if isinstance(error, ContextError) else "Context storage operation failed"
            for path in (self.context.runtime, self.context.root):
                message = message.replace(str(path), "[context]")
            return {"ok": False, "error": {"type": type(error).__name__, "message": message}}

    def _draft(self):
        draft = self.context.inspect_draft(self.event_id)
        if draft["base_revision"] != self.base.revision:
            raise Conflict("The event base changed")
        return draft

    def _effective(self):
        draft = self._draft()
        objects = {"node": deepcopy(self.base.nodes), "edge": deepcopy(self.base.edges)}
        support = deepcopy(self.base.support)
        for change in draft["draft"].values():
            kind, key = change["kind"], change["id"]
            if change["record"] is None:
                objects[kind].pop(key, None)
                support.pop((kind, key), None)
            else:
                objects[kind][key] = change["record"]
                support[kind, key] = change["support_after"]
        return draft, objects, support

    def _view(self, data, *, published=False):
        return {"view": "published" if published else "draft_overlay",
                "revision": self.base.revision, **data}

    def _search(self, query="", kind=None, attributes=None, limit=20):
        hits = self.context.search(query, revision=self.base.revision, kind=kind,
                                   attributes=attributes, limit=limit + 1)
        return self._view({"items": [asdict(hit) for hit in hits[:limit]],
                           "has_more": len(hits) > limit,
                           "notice": "Narrow the query to explore more matches." if len(hits) > limit else None}, published=True)

    def _read_object(self, kind, id):
        draft, objects, support = self._effective()
        change = draft["draft"].get(f"{kind}:{id}")
        if id not in objects[kind] and not change and (kind, id) not in self.base.known_objects:
            raise ContextError("No current, historical, or staged object with this kind/ID")
        return self._view({"record": objects[kind].get(id), "support": support.get((kind, id), []),
                           "removed": id not in objects[kind], "staged": change is not None,
                           "historical": (kind, id) in self.base.known_objects})

    def _read_draft(self, offset=0, limit=20):
        draft = self._draft()
        changes = sorted(draft["draft"].values(), key=lambda c: (c["kind"], c["id"]))
        return self._view({"event_id": self.event_id, "digest": draft["digest"],
                           "input_mode": self.input_mode,
                           **{key: draft[key] for key in ("action", "handoff_note", "action_associations") if key in draft},
                           "required_reassessment": self.required_reassessment,
                           "completion": self.completion, **page(changes, offset, limit)})

    def _list_objects(self, kind, offset=0, limit=20):
        _, objects, support = self._effective()
        return self._view(page([{"record": record, "support": support.get((kind, key), [])}
                                for key, record in sorted(objects[kind].items())], offset, limit))

    def _neighbors(self, id, direction="both", offset=0, limit=20):
        draft, objects, support = self._effective()
        if (id not in objects["node"] and f"node:{id}" not in draft["draft"] and
                ("node", id) not in self.base.known_objects):
            raise ContextError("No current or staged node with this ID")
        result = []
        for key, edge in sorted(objects["edge"].items()):
            outgoing, incoming = edge["from_id"] == id, edge["to_id"] == id
            if (outgoing and direction in ("out", "both")) or (incoming and direction in ("in", "both")):
                neighbor = edge["to_id"] if outgoing else edge["from_id"]
                result.append({"record": edge, "support": support.get(("edge", key), []),
                               "direction": "self" if outgoing and incoming else "out" if outgoing else "in",
                               "neighbor": {"id": neighbor, "record": objects["node"].get(neighbor),
                                            "support": support.get(("node", neighbor), [])}})
        return self._view(page(result, offset, limit))

    def _catalog(self, kind, attribute=None, offset=0, limit=20):
        catalog = deepcopy(self.base.catalogs[kind])
        for change in self._draft()["draft"].values():
            if change["kind"] != kind or change["record"] is None:
                continue
            for name, values in change["record"]["attributes"].items():
                known = catalog.setdefault(name, [])
                keys = {value_key(v) for v in known}
                for value in values:
                    if type(value) in (str, bool) and value_key(value) not in keys:
                        known.append(value)
                        keys.add(value_key(value))
        if attribute is not None and attribute not in catalog:
            raise ContextError("Unknown attribute in this catalog")
        items = sorted(catalog) if attribute is None else catalog[attribute]
        return self._view({"kind": kind, "attribute": attribute, **page(items, offset, limit)})

    def _sources(self, offset=0, limit=20):
        items = [{"source": event["source"], "input_mode": self.base.input_mode(event),
                  "removed": event["operation"] == "remove",
                  "commit": self.base.event_commits[event["id"]]}
                 for _, event in sorted(self.base.sources.items())]
        return self._view(page(items, offset, limit), published=True)

    def _source(self, id, offset=0, limit=16000):
        event = self.base.sources.get(id)
        if event is None or event["operation"] == "remove":
            raise ContextError("No current managed source with this ID at the published base")
        return self._view({"source": event["source"], "input_mode": self.base.input_mode(event),
                           "commit": self.base.event_commits[event["id"]],
                           **text_page(self.base.files[event["source"]["path"]], offset, limit)}, published=True)

    def _evidence(self, commit, version, offset=0, limit=16000):
        evidence = self.base.source_change(commit)
        return self._view({"commit": commit, "input_mode": evidence["input_mode"],
                           "event": evidence["event"], "version": version,
                           **text_page(evidence[version], offset, limit)}, published=True)

    def _history(self, id, kind, offset=0, limit=20):
        return self._view(page(self.base.history(id, kind=kind), offset, limit), published=True)

    def _mutation(self, citations, support_after=(), developments=()):
        if self.completion is not None:
            raise Conflict("This attempt is sealed; no further mutations are allowed")
        refs = list(citations) + list(support_after)
        for development in developments:
            refs.extend(development["citations"])
        if any(ref != "self" and ref not in self.base.by_commit for ref in refs):
            raise ContextError("Evidence must cite source-event commits within this published base")

    def _handoff(self, text):
        self._mutation([])
        return self.context.stage_handoff_note(self.event_id, text)

    def _associations(self, date, summary, associations):
        self._mutation([])
        return self.context.stage_action_associations(self.event_id, date, summary, associations)

    def _put(self, kind, record, **details):
        self._mutation(details["citations"], details["support_after"], details.get("developments", []))
        if "id" in record:
            draft = self._draft()
            if record["id"] not in self.base.objects(kind) and f"{kind}:{record['id']}" not in draft["draft"]:
                raise ContextError("Omit id when creating an object; creation IDs are allocated by Context")
        key = getattr(self.context, "put_" + kind)(self.event_id, record, **details)
        return {"kind": kind, "id": key, "staged": True}

    def _remove(self, kind, id, **details):
        self._mutation(details["citations"], developments=details.get("developments", []))
        getattr(self.context, "remove_" + kind)(self.event_id, id, **details)
        return {"kind": kind, "id": id, "staged": True}

    def _finish(self, status, summary, reassessed):
        text(summary, "summary")
        arguments = {"status": status, "summary": summary, "reassessed": reassessed}
        if self.completion is not None:
            if arguments != self._finish_args:
                raise Conflict("This attempt already finished with different input")
            if self.completion.get("draft_digest") != self._draft()["digest"]:
                raise Conflict("The sealed draft changed")
            return self.completion
        if status == "complete" and {(v["kind"], v["id"]) for v in reassessed} != {
                (v["kind"], v["id"]) for v in self.required_reassessment}:
            raise ContextError("reassessed must cover the exact required_reassessment set")
        draft = self.context.inspect_draft(self.event_id, validate=status == "complete")
        if status == "blocked" and not draft.get("handoff_note", {}).get("text"):
            raise ContextError("Blocked completion requires a handoff note explaining what needs resolution")
        result = {**arguments, "draft_digest": draft["digest"], "published": False}
        if len(encode({"ok": True, "result": result})) > self.max_response_bytes:
            raise ContextError("Completion response exceeds the configured byte limit")
        self.completion, self._finish_args = result, deepcopy(arguments)
        return result
