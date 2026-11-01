"""Permissive lexical candidates, model-selected X, and unranked graph sampling."""
from __future__ import annotations

import json
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


def candidates(snapshot, source_change):
    source = "\n".join(source_change[version] or "" for version in ("before", "after")).casefold()
    source_tokens = tokens(source)
    result = []
    for key, record in sorted(snapshot.nodes.items()):
        strings = [value for values in record["attributes"].values() for value in values
                   if isinstance(value, str)]
        if (source_tokens & tokens(record["summary"] + "\n" + "\n".join(strings)) or
                any(value and value.casefold() in source for value in strings)):
            result.append(snapshot.read("node", key))
    return result


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


def prepare(snapshot, source_change, runner, *, directory, max_payload_bytes=1024 * 1024,
            rng=None, on_identity=None):
    matched = candidates(snapshot, source_change)
    payload = {"source_change": source_change, "candidates": matched}
    if len(encode({"instructions": INSTRUCTIONS, "payload": payload})) > max_payload_bytes:
        raise ContextError("Bootstrap payload exceeds the configured byte limit; no candidates were omitted")
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
    return {"candidate_ids": [item["record"]["id"] for item in matched],
            "model_called": bool(matched), **sample(snapshot, selected, rng=rng)}
