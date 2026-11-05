"""Bounded, JSON-plain views of one immutable published ``Snapshot``.

Everything here is a pure function of the pinned snapshot: nothing reads a draft, the engine runtime, the working
tree or another revision, and no view depends on who is asking. Lists in compact views are cut at fixed bounds and
the cut is reported (``truncated`` / ``*_truncated`` / ``has_more``), never silent. Native node, edge, note and source
IDs are the engine's five-character IDs; ``ref`` is ``<kind>:<id>``. Notes are separate records that may link nodes and
edges: they are never vertices, adjacency, totals or expansion, and a note without subjects is an ordinary note."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import threading

from qorqut_context.schema import ContextError, record_path

SUMMARY_CHARS = 600
ENDPOINT_SUMMARY_CHARS = 200
ATTRIBUTE_NAMES = 40
ATTRIBUTE_MEMBERS = 20
ATTRIBUTE_CHARS = 200
EXCERPT_CHARS = 300
HOME_RELATIONSHIPS = 5
OBJECT_RELATIONSHIPS = 25
OBJECT_SUBJECTS = 50
SUPPORT_ITEMS = 50
SUPPORTS_ITEMS = 50
CHANGE_ITEMS = 25
KINDS = ("node", "edge", "note", "source")


def ref(kind, object_id) -> str:
    return f"{kind}:{object_id}"


def clip(text, limit):
    """``(text, truncated)`` with the text cut to at most ``limit`` characters."""
    return (text, False) if len(text) <= limit else (text[:limit], True)


def compact_attributes(attributes):
    """``(attributes, truncated)`` bounded by name count, members per name and characters per text member."""
    result, truncated = {}, False
    for index, (name, members) in enumerate(attributes.items()):
        if index >= ATTRIBUTE_NAMES:
            return result, True
        kept = []
        for member in members[:ATTRIBUTE_MEMBERS]:
            if isinstance(member, str):
                member, cut = clip(member, ATTRIBUTE_CHARS)
                truncated = truncated or cut
            kept.append(member)
        truncated = truncated or len(members) > ATTRIBUTE_MEMBERS
        result[name] = kept
    return result, truncated


def source_summary(event) -> dict:
    source = event["source"]
    return {"id": source["id"], "ref": ref("source", source["id"]), "title": source["title"],
            "source_kind": source["kind"], "origin": source["origin"], "origin_ref": source.get("origin_ref"),
            "created_at": source.get("created_at")}


def read_blobs(git, specs):
    """``{spec: bytes | None}`` for ``<revision>:<path>`` specs in one ``git cat-file --batch`` call."""
    if not specs:
        return {}
    output = git.run("cat-file", "--batch", data=("\n".join(specs) + "\n").encode("utf-8"))
    result, offset = {}, 0
    for spec in specs:
        end = output.index(b"\n", offset)
        header = output[offset:end].split()
        if header[-1:] == [b"missing"]:
            result[spec], offset = None, end + 1
            continue
        if len(header) != 3 or header[1] != b"blob":
            raise ContextError("Expected a Git blob")
        start, size = end + 1, int(header[2])
        result[spec], offset = output[start:start + size], start + size + 1
    return result


class PublishedGraph:
    """The views of one snapshot, with its derived indexes built once on first use."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.revision = snapshot.revision
        self._lock = threading.Lock()
        self._adjacency = None
        self._home_order = None
        self._times = None
        self._supports = None

    # ---- derived indexes

    def _derived(self):
        with self._lock:
            if self._adjacency is None:
                adjacency = {node_id: [] for node_id in self.snapshot.nodes}
                for edge_id, edge in sorted(self.snapshot.edges.items()):
                    for end in {edge["from_id"], edge["to_id"]}:
                        adjacency.setdefault(end, []).append(edge_id)
                self._adjacency = adjacency
                self._home_order = sorted(self.snapshot.nodes, key=lambda node_id: (-len(adjacency[node_id]), node_id))
        return self._adjacency

    def adjacency(self, node_id):
        return self._derived().get(node_id, [])

    def home_order(self):
        self._derived()
        return self._home_order

    def recorded_at(self, commit):
        """The publication time of a published commit, from one ``git log`` over the pinned history."""
        with self._lock:
            if self._times is None:
                output = self.snapshot.git.run("log", "--format=%H %ct", "--end-of-options", self.revision).decode()
                self._times = {line.split()[0]: int(line.split()[1]) for line in output.splitlines() if line.strip()}
        try:
            seconds = self._times[commit]
        except KeyError:
            raise ContextError("Commit is not in the published history") from None
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")

    def supports_of(self, source_id):
        """Current nodes, edges and notes whose present support cites an event of this source, sorted by kind and ID."""
        with self._lock:
            if self._supports is None:
                index = {}
                snapshot = self.snapshot
                for (kind, object_id), commits in snapshot.support.items():
                    if object_id not in snapshot.records(kind):
                        continue
                    for commit in commits:
                        event = snapshot.by_commit.get(commit)
                        if event is not None:
                            index.setdefault(event["source"]["id"], set()).add((kind, object_id))
                self._supports = {key: sorted(value) for key, value in index.items()}
        return self._supports.get(source_id, [])

    # ---- totals and lookup

    def current_sources(self):
        return {source_id: event for source_id, event in self.snapshot.sources.items() if event["operation"] != "remove"}

    def totals(self) -> dict:
        snapshot = self.snapshot
        return {"nodes": len(snapshot.nodes), "edges": len(snapshot.edges),
                "sources": len(self.current_sources()), "events": len(snapshot.events)}

    def locate(self, value):
        """``(kind, id)`` for ``node:ID``, ``edge:ID``, ``note:ID``, ``source:ID`` or a bare ID, or ``None`` when absent here.

        Engine IDs are unique across nodes, edges, notes, sources and events, so a bare ID is unambiguous."""
        snapshot = self.snapshot
        if not isinstance(value, str):
            return None
        kind, separator, object_id = value.partition(":")
        if not separator:
            kind, object_id = None, value
        elif kind not in KINDS:
            return None
        for candidate, present in (("node", snapshot.nodes), ("edge", snapshot.edges), ("note", snapshot.notes),
                                   ("source", snapshot.sources)):
            if kind in (None, candidate) and object_id in present:
                return candidate, object_id
        return None

    # ---- compact views

    def endpoint(self, node_id) -> dict:
        node = self.snapshot.nodes.get(node_id)
        summary, truncated = clip(node["summary"], ENDPOINT_SUMMARY_CHARS) if node else (None, False)
        return {"kind": "node", "id": node_id, "ref": ref("node", node_id), "summary": summary, "truncated": truncated}

    def entity(self, kind, object_id) -> dict:
        """The compact view of a node, edge, note or source."""
        snapshot = self.snapshot
        if kind == "note":
            return self.note_entity(object_id)
        if kind == "source":
            event = snapshot.sources[object_id]
            summary = source_summary(event)
            body = snapshot.files.get(event["source"]["path"]) if event["operation"] != "remove" else None
            text, truncated = clip(body.decode("utf-8", errors="replace"), EXCERPT_CHARS) if body is not None else (None, False)
            return {"kind": "source", **summary, "input_mode": event["input_mode"],
                    "withdrawn": event["operation"] == "remove", "excerpt": text, "truncated": truncated}
        if kind == "edge":
            return self.relationship(object_id)
        record = snapshot.nodes[object_id]
        summary, cut_summary = clip(record["summary"], SUMMARY_CHARS)
        attributes, cut_attributes = compact_attributes(record["attributes"])
        return {"kind": "node", "id": object_id, "ref": ref("node", object_id), "summary": summary,
                "attributes": attributes, "truncated": cut_summary or cut_attributes}

    def note_entity(self, note_id) -> dict:
        note = self.snapshot.notes[note_id]
        text, truncated = clip(note["text"], SUMMARY_CHARS)
        return {"kind": "note", "id": note_id, "ref": ref("note", note_id), "text": text, "truncated": truncated,
                "subject_count": len(note["objects"])}

    def relationship(self, edge_id, focus=None, depth=None) -> dict:
        edge = self.snapshot.edges[edge_id]
        summary, cut_summary = clip(edge["summary"], SUMMARY_CHARS)
        attributes, cut_attributes = compact_attributes(edge["attributes"])
        view = {"kind": "edge", "id": edge_id, "ref": ref("edge", edge_id), "summary": summary,
                "attributes": attributes, "truncated": cut_summary or cut_attributes,
                "from": self.endpoint(edge["from_id"]), "to": self.endpoint(edge["to_id"])}
        if focus is not None:
            view["direction"] = ("self" if edge["from_id"] == edge["to_id"]
                                 else "out" if edge["from_id"] == focus else "in")
        if depth is not None:
            view["depth"] = depth
        return view

    def home_node(self, node_id) -> dict:
        incident = self.adjacency(node_id)
        view = self.entity("node", node_id)
        view["relationship_count"] = len(incident)
        view["relationships"] = [self.relationship(edge_id, focus=node_id) for edge_id in incident[:HOME_RELATIONSHIPS]]
        view["relationships_truncated"] = len(incident) > HOME_RELATIONSHIPS
        return view

    # ---- full records and evidence

    def record(self, kind, object_id) -> dict:
        """The full current node, edge or note record (no bounds, no truncation flag)."""
        stored = self.snapshot.records(kind)[object_id]
        if kind == "note":
            return {"kind": "note", "id": object_id, "ref": ref("note", object_id), "text": stored["text"]}
        view = {"kind": kind, "id": object_id, "ref": ref(kind, object_id), "summary": stored["summary"],
                "attributes": {name: list(members) for name, members in stored["attributes"].items()}}
        if kind == "edge":
            view["from"], view["to"] = self.endpoint(stored["from_id"]), self.endpoint(stored["to_id"])
        return view

    def evidence(self, commit) -> dict | None:
        event = self.snapshot.by_commit.get(commit)
        if event is None:
            return None
        return {"commit": commit, "event_id": event["id"], "recorded_at": self.recorded_at(commit),
                "input_mode": event["input_mode"], "source": source_summary(event)}

    def support(self, kind, object_id) -> dict:
        commits = self.snapshot.support.get((kind, object_id), [])
        items = [item for item in (self.evidence(commit) for commit in commits[:SUPPORT_ITEMS]) if item is not None]
        return {"count": len(commits), "items": items, "truncated": len(commits) > SUPPORT_ITEMS}

    def subjects(self, note_id) -> dict:
        """The records a note links, in the note's own order, each labeled ``current`` or ``removed`` (still linked
        after the record left the graph; such a link names history and no longer opens as an object)."""
        links = self.snapshot.notes[note_id]["objects"]
        items = []
        for link in links[:OBJECT_SUBJECTS]:
            current = self.snapshot.records(link["kind"]).get(link["id"])
            summary, truncated = clip(current["summary"], ENDPOINT_SUMMARY_CHARS) if current else (None, False)
            items.append({"kind": link["kind"], "id": link["id"], "ref": ref(link["kind"], link["id"]),
                          "status": "current" if current else "removed", "summary": summary, "truncated": truncated})
        return {"count": len(links), "items": items, "truncated": len(links) > OBJECT_SUBJECTS}

    def supports(self, source_id) -> dict:
        objects = self.supports_of(source_id)
        return {"count": len(objects),
                "items": [self.entity(kind, object_id) for kind, object_id in objects[:SUPPORTS_ITEMS]],
                "truncated": len(objects) > SUPPORTS_ITEMS}

    def source_object(self, source_id) -> dict:
        event = self.snapshot.sources[source_id]
        return {"kind": "source", **source_summary(event), "input_mode": event["input_mode"],
                "operation": event["operation"], "withdrawn": event["operation"] == "remove"}

    def source_content(self, source_id, offset, limit) -> dict | None:
        """One page of the committed source text, or ``None`` after the source was withdrawn."""
        event = self.snapshot.sources[source_id]
        body = self.snapshot.files.get(event["source"]["path"]) if event["operation"] != "remove" else None
        if body is None:
            return None
        text = body.decode("utf-8", errors="replace")
        selected = text[offset:offset + limit]
        more = offset + len(selected) < len(text)
        return {"text": selected, "offset": offset, "limit": limit, "total_characters": len(text),
                "has_more": more, "next_offset": offset + len(selected) if more else None}

    # ---- history

    def object_events(self, kind, object_id):
        """Events that changed this node, edge or note, newest first, as ``(event, change)`` pairs."""
        found = [(event, change) for event in self.snapshot.events.values() for change in event["changes"]
                 if change["kind"] == kind and change["id"] == object_id]
        found.reverse()
        return found

    def source_events(self, source_id):
        found = [event for event in self.snapshot.events.values() if event["source"]["id"] == source_id]
        found.reverse()
        return found

    def history_entries(self, kind, object_id, pairs) -> list[dict]:
        """Entries for one page of a node's, edge's or note's changes, with the record before and after each."""
        snapshot = self.snapshot
        path = record_path(kind, object_id)
        commits = [snapshot.event_commits[event["id"]] for event, _ in pairs]
        blobs = read_blobs(snapshot.git, [f"{event['base_revision']}:{path}" for event, _ in pairs]
                           + [f"{commit}:{path}" for commit in commits])
        entries = []
        for (event, change), commit in zip(pairs, commits):
            before, after = blobs[f"{event['base_revision']}:{path}"], blobs[f"{commit}:{path}"]
            entries.append({
                "event_id": event["id"], "commit": commit, "recorded_at": self.recorded_at(commit),
                "input_mode": event["input_mode"], "operation": change["operation"],
                "explanation": change["explanation"],
                "citations": snapshot.resolve_refs(change["citations"], commit),
                "support_after": snapshot.resolve_refs(change["support_after"], commit),
                "developments": [{**item, "citations": snapshot.resolve_refs(item["citations"], commit)}
                                 for item in change["developments"]],
                "source": source_summary(event),
                "before": self._stored(before, kind, object_id), "after": self._stored(after, kind, object_id)})
        return entries

    @staticmethod
    def _stored(content, kind, object_id):
        if content is None:
            return None
        stored = json.loads(content)
        if kind == "note":
            return {"kind": "note", "id": object_id, "ref": ref("note", object_id), "text": stored["text"],
                    "objects": [{"kind": link["kind"], "id": link["id"], "ref": ref(link["kind"], link["id"])}
                                for link in stored["objects"]]}
        view = {"kind": kind, "id": object_id, "ref": ref(kind, object_id), "summary": stored["summary"],
                "attributes": stored["attributes"]}
        if kind == "edge":
            view["from_id"], view["to_id"] = stored["from_id"], stored["to_id"]
        return view

    def source_history_entries(self, events) -> list[dict]:
        entries = []
        for event in events:
            commit = self.snapshot.event_commits[event["id"]]
            entries.append({"event_id": event["id"], "commit": commit, "recorded_at": self.recorded_at(commit),
                            "input_mode": event["input_mode"], "operation": event["operation"],
                            "change_count": len(event["changes"])})
        return entries

    # ---- the owner change feed

    def events_newest_first(self, ref_filter=None):
        events = list(self.snapshot.events.values())
        events.reverse()
        if ref_filter is None:
            return events
        kind, object_id = ref_filter
        if kind == "source":
            return [event for event in events if event["source"]["id"] == object_id]
        return [event for event in events
                if any(change["kind"] == kind and change["id"] == object_id for change in event["changes"])]

    def change_entry(self, event) -> dict:
        snapshot = self.snapshot
        commit = snapshot.event_commits[event["id"]]
        shown = event["changes"][:CHANGE_ITEMS]
        entry = {
            "event_id": event["id"], "commit": commit, "recorded_at": self.recorded_at(commit),
            "input_mode": event["input_mode"], "operation": event["operation"],
            "submitted_by": event["submitted_by"], "source": {**source_summary(event), "path": event["source"]["path"]},
            "changes": [{"kind": change["kind"], "id": change["id"], "ref": ref(change["kind"], change["id"]),
                         "operation": change["operation"], "explanation": change["explanation"],
                         "citations": snapshot.resolve_refs(change["citations"], commit)} for change in shown],
            "change_count": len(event["changes"]), "changes_truncated": len(event["changes"]) > CHANGE_ITEMS,
            "handoff_disposition": event.get("handoff_note", {}).get("disposition"), "action": None}
        if "action" in event:
            action = event["action"]
            entry["action"] = {"action_ref": action["action_ref"], "revision_ref": action.get("revision_ref"),
                               "tags": [dict(tag) for tag in action["tags"]]}
        return entry

    # ---- expansion

    def expansion(self, origin, depth, direction) -> list[tuple[str, int, str]]:
        """``(edge_id, depth, via)`` around a node: nearest first, then by edge ID; each edge appears once.

        ``via`` is the nearer endpoint the edge was reached from, which the relationship's direction is relative to."""
        edges = self.snapshot.edges
        visited, frontier, seen, result = {origin}, [origin], set(), []
        for level in range(1, depth + 1):
            found, following = {}, set()
            for node_id in sorted(frontier):
                for edge_id in self.adjacency(node_id):
                    if edge_id in seen or edge_id in found:
                        continue
                    edge = edges[edge_id]
                    outgoing, incoming = edge["from_id"] == node_id, edge["to_id"] == node_id
                    if not ((direction in ("out", "both") and outgoing) or (direction in ("in", "both") and incoming)):
                        continue
                    found[edge_id] = node_id
                    other = edge["to_id"] if outgoing else edge["from_id"]
                    if other not in visited:
                        following.add(other)
            for edge_id in sorted(found):
                seen.add(edge_id)
                result.append((edge_id, level, found[edge_id]))
            visited |= following
            frontier = sorted(following)
        return result
