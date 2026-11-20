"""A ranked, bounded candidate shortlist, model-selected X, and unranked graph sampling."""
from __future__ import annotations

from collections import Counter
import json
import math
from pathlib import Path
import random
import re

from .schema import ContextError, encode
from .tools import validate


INSTRUCTIONS = (Path(__file__).parent / "prompts/bootstrap.md").read_text()
SELECTION_SCHEMA = {"type": "object", "properties": {
    "node_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["node_ids"], "additionalProperties": False}
INVITATION = "Feel free to explore the context base yourself."


def tokens(value):
    return {word for word in re.findall(r"[^\W_]+", value.casefold()) if len(word) >= 3}


# The shortlist is bounded by the input, never by the size of the graph. Every limit below is a count or a size of what
# one bootstrap turn is shown; none of them decides what is true about the graph.
CANDIDATE_LIMIT = 100          # candidates shown to the bootstrap turn
CARD_BYTES = 1024              # encoded size one candidate card aims to stay under
CARD_SUMMARY = 400             # characters of the summary kept on a card
EXACT_LENGTHS = (3, 80)        # a "short" attribute value: long enough to mean something, short enough to be a name
EXACT_SHARED = 10              # a short value held by more nodes than this identifies none of them ("person", "topic")
HUB_LIMIT = 20                 # the most-connected nodes are always shown: short inputs name them by a common word ("owner")
QUERY_WORDS = 400              # at most this many distinct source words score candidates; the rarest are kept
BM25_K1, BM25_B = 1.2, 0.75


def _strings(record):
    return [value for values in record["attributes"].values() for value in values if isinstance(value, str)]


def _words(value):
    return [word for word in re.findall(r"[^\W_]+", value.casefold()) if len(word) >= 3]


def _source(source_change):
    return "\n".join(source_change[version] or "" for version in ("before", "after")).casefold()


def rank(snapshot, source_change):
    """Every candidate node ID, best first, as ``(ordered_ids, pinned_ids)``; the order depends on the graph and input alone.

    Two passes. A node whose short attribute value (a name, a URL, an address) appears as whole words in the source
    comes first: it is the guard against filing a duplicate of a node that is named outright. A value shared by more
    than ``EXACT_SHARED`` nodes names none of them and does not count. Every other node sharing a word with the source
    follows by BM25, so a rare shared word outweighs a common one and a long node gains nothing from its length.
    """
    source = _source(source_change)
    documents = {key: _words(record["summary"] + "\n" + "\n".join(_strings(record)))
                 for key, record in snapshot.nodes.items()}
    holders = Counter(value for record in snapshot.nodes.values()
                      for value in {item.casefold().strip() for item in _strings(record)})
    exact = {}
    for key, record in snapshot.nodes.items():
        for value in {item.casefold().strip() for item in _strings(record)}:
            if (EXACT_LENGTHS[0] <= len(value) <= EXACT_LENGTHS[1] and holders[value] <= EXACT_SHARED
                    and value in source and re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", source)):
                # Rarer, then longer, values identify a node more surely.
                exact[key] = min(exact.get(key, (EXACT_SHARED + 1, 0)), (holders[value], -len(value)))
    frequency = Counter(word for words in documents.values() for word in set(words))
    query = sorted((word for word in set(_words(source)) if word in frequency),
                   key=lambda word: (frequency[word], word))[:QUERY_WORDS]
    count = len(documents)
    average = (sum(map(len, documents.values())) / count) if count else 0
    weight = {word: math.log(1 + (count - frequency[word] + 0.5) / (frequency[word] + 0.5)) for word in query}
    scores = {}
    for key, words in documents.items():
        terms = Counter(word for word in words if word in weight)
        if terms:
            norm = BM25_K1 * (1 - BM25_B + BM25_B * len(words) / average)
            scores[key] = sum(weight[word] * n * (BM25_K1 + 1) / (n + norm) for word, n in terms.items())
    first = sorted(exact, key=lambda key: (exact[key], -scores.get(key, 0), key))
    # The installation's personal node and the graph's hubs follow the exact matches whatever their score: an input
    # refers to them by words too common to rank ("owner", "project"), and their long records are penalised by length.
    degree = Counter(edge[end] for edge in snapshot.edges.values() for end in ("from_id", "to_id"))
    personal = getattr(snapshot, "personal_context_node", None)
    hubs = ([personal] if personal in snapshot.nodes else []) + [
        key for key, _ in sorted(degree.items(), key=lambda item: (-item[1], item[0]))[:HUB_LIMIT] if key in snapshot.nodes]
    first += [key for key in dict.fromkeys(hubs) if key not in exact]
    placed = set(first)
    rest = sorted((key for key in scores if key not in placed), key=lambda key: (-scores[key], key))
    return first + rest, first


def card(snapshot, key):
    """A node as the bootstrap turn sees it: enough to recognise it, never the whole record.

    The ID, the start of the summary and the shortest attribute values first (names and identifiers are short),
    until ``CARD_BYTES``. ``truncated`` says the record holds more; the full record is read after selection.
    """
    record = snapshot.nodes[key]
    summary = record["summary"][:CARD_SUMMARY]
    shown = {"id": record["id"], "summary": summary, "attributes": {}}
    truncated = len(record["summary"]) > CARD_SUMMARY
    values = sorted(((name, value) for name, items in record["attributes"].items() for value in items),
                    key=lambda item: (len(str(item[1])), item[0], str(item[1])))
    for name, value in values:
        shown["attributes"].setdefault(name, []).append(value)
        if len(encode(shown)) > CARD_BYTES:
            shown["attributes"][name].pop()
            if not shown["attributes"][name]:
                del shown["attributes"][name]
            truncated = True
            break
    return {"record": shown, "truncated": truncated}


def candidates(snapshot, source_change, *, limit=CANDIDATE_LIMIT):
    """``(cards, counts)``: the best ``limit`` matching nodes as cards, and how many matched in all."""
    ordered, exact = rank(snapshot, source_change)
    shown = ordered[:limit]
    return ([card(snapshot, key) for key in shown],
            {"matched": len(ordered), "exact": len(exact), "shown": len(shown)})


NOTE_SAMPLE = 40
NOTE_EXCERPT = 600


def note_sample(snapshot, source_change, selected, edge_ids, *, rng=None):
    """A bounded, unranked starting sample of existing notes, so relevant ones surface even without an entity match.

    A note is eligible when it links a selected node or a sampled edge, or shares a word with either source
    version. At most ``NOTE_SAMPLE`` are returned uniformly, as ID, excerpt and links; the model reads a whole note
    or searches notes (``kind: note``) for more. Never every note is injected into a model turn.
    """
    notes = getattr(snapshot, "notes", {})
    source_tokens = tokens("\n".join(source_change[version] or "" for version in ("before", "after")))
    linked = {("node", key) for key in selected} | {("edge", key) for key in edge_ids}
    eligible = sorted(key for key, note in notes.items()
                      if any((link["kind"], link["id"]) in linked for link in note["objects"])
                      or source_tokens & tokens(note["text"]))
    chosen = sorted((rng or random.SystemRandom()).sample(eligible, NOTE_SAMPLE)
                    if len(eligible) > NOTE_SAMPLE else eligible)
    return {"note_ids": chosen,
            "notes": [{"id": key, "text": notes[key]["text"][:NOTE_EXCERPT],
                       "text_truncated": len(notes[key]["text"]) > NOTE_EXCERPT,
                       "objects": notes[key]["objects"]} for key in chosen],
            "counts": {"eligible": len(eligible), "returned": len(chosen)},
            "truncated": len(eligible) > len(chosen),
            "notice": ("More eligible notes exist beyond this random sample. " if len(eligible) > len(chosen) else "")
                      + "Use search_context with kind note or list_linked_notes to find others."}


def sample(snapshot, selected, *, rng=None):
    rng = rng or random.SystemRandom()
    x = sorted(set(selected))
    x_set = set(x)
    neighbors = {edge[endpoint] for edge in snapshot.edges.values()
                 if edge["from_id"] in x_set or edge["to_id"] in x_set
                 for endpoint in ("from_id", "to_id")} - x_set
    y = sorted(rng.sample(sorted(neighbors), min(40, len(neighbors))))
    included = x_set | set(y)
    eligible = sorted(key for key, edge in snapshot.edges.items()
                      if edge["from_id"] in included and edge["to_id"] in included)
    edges = sorted(rng.sample(eligible, min(60, len(eligible))))
    omitted = len(neighbors) > len(y) or len(eligible) > len(edges)
    return {"revision": snapshot.revision, "X": x, "Y": y, "edge_ids": edges,
            "nodes": [snapshot.read("node", key) for key in x + y],
            "edges": [snapshot.read("edge", key) for key in edges],
            "counts": {"X": len(x), "eligible_neighbors": len(neighbors), "returned_neighbors": len(y),
                       "eligible_edges": len(eligible), "returned_edges": len(edges)},
            "truncated": {"neighbors": len(neighbors) > len(y), "edges": len(eligible) > len(edges)},
            "notice": ("More eligible context exists beyond this random sample. " if omitted else "") + INVITATION}


def _notice(counts):
    return ((f"These are the {counts['shown']} best-matching of {counts['matched']} existing nodes that share words with "
             "the source; the others were left out to bound this request. ") if counts["matched"] > counts["shown"] else
            "These are all the existing nodes that share words with the source. ") + \
        "Each candidate is a shortened card, not the whole record."


def prepare(snapshot, source_change, runner, *, directory, max_payload_bytes=1024 * 1024,
            rng=None, on_identity=None, limit=CANDIDATE_LIMIT):
    matched, counts = candidates(snapshot, source_change, limit=limit)

    def payload_for(cards):
        current = {**counts, "shown": len(cards)}
        return current, {"source_change": source_change, "candidates": cards,
                         "candidate_counts": current, "candidate_notice": _notice(current)}

    def size(cards):
        return len(encode({"instructions": INSTRUCTIONS, "payload": payload_for(cards)[1]}))

    # An oversized request drops its lowest-ranked candidates and says so; only a source that does not fit by itself fails.
    if size(matched) > max_payload_bytes:
        if size([]) > max_payload_bytes:
            raise ContextError("Bootstrap source change exceeds the configured byte limit")
        low, high = 0, len(matched)
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if size(matched[:middle]) <= max_payload_bytes else (low, middle - 1)
        matched = matched[:low]
    counts, payload = payload_for(matched)
    selected = []
    if matched:
        result = runner.run(stage="bootstrap", instructions=INSTRUCTIONS, payload=payload,
                            directory=directory, output_schema=SELECTION_SCHEMA,
                            on_identity=on_identity)
        if result.status != "completed":
            raise ContextError("Bootstrap model turn did not complete")
        try:
            selection = json.loads(result.output)
        except (ValueError, TypeError) as error:
            raise ContextError("Bootstrap did not return the required JSON selection") from error
        validate(selection, SELECTION_SCHEMA, "bootstrap selection")
        selected = list(dict.fromkeys(selection["node_ids"]))
        supplied = {item["record"]["id"] for item in matched}
        if not set(selected) <= supplied:
            raise ContextError("Bootstrap selected an ID outside the supplied candidates")
    graph = sample(snapshot, selected, rng=rng)
    return {"candidate_ids": [item["record"]["id"] for item in matched], "candidate_counts": counts,
            "model_called": bool(matched), **graph,
            "notes": note_sample(snapshot, source_change, graph["X"], graph["edge_ids"], rng=rng)}
